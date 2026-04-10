using System;
using System.Collections.Concurrent;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Threading.Channels;
using System.Threading.Tasks;
using PredictionBacktester.Engine;

namespace PredictionBacktester.Strategies
{
    public class FastMergeArbTelemetryStrategy : ILiveStrategy
    {
        public string StrategyName => "Fast_Merge_Arb_Telemetry";

        private readonly Dictionary<string, string> _tokenToEventMap = new();
        private readonly Dictionary<string, List<string>> _eventTokens = new();
        private readonly ConcurrentDictionary<string, LocalOrderBook> _books = new();
        private readonly ConcurrentDictionary<string, ActiveArbEvent> _activeArbs = new();

        // --- EXECUTION-ALIGNED PARAMETERS (match PolymarketCategoricalArbStrategy) ---
        private readonly decimal _maxInvestmentPerTrade;
        private readonly decimal _slippageCents;
        private readonly decimal _minProfitPerSet;
        private readonly decimal _depthFloorShares;
        private readonly double _feeRate;
        private readonly double _feeExponent;

        /// <summary>
        /// Fired when a new arb window opens. Args: (eventId, netCost, legs, depth).
        /// Subscribers can use this to trigger a live REST depth verification.
        /// </summary>
        public event Action<string, decimal, int, decimal>? OnArbOpened;

        // --- NEAR-MISS TRACKING ---
        private record NearMissEntry(decimal BestNetCost, int PricedLegs, int TotalLegs, decimal BottleneckShares);
        private readonly ConcurrentDictionary<string, NearMissEntry> _bestNetCostSeen = new();
        private DateTime _lastNearMissReport = DateTime.MinValue;
        private const int NEAR_MISS_REPORT_INTERVAL_SEC = 60; // Print top near-misses every 60s

        // --- CSV TELEMETRY INFRASTRUCTURE ---
        private static readonly string _csvFilePath = $"ArbTelemetry_{DateTime.UtcNow:yyyyMMdd_HHmmss}.csv";
        private static readonly Channel<string> _csvQueue = Channel.CreateUnbounded<string>();
        private static bool _csvInitialized = false;
        private static readonly object _csvInitLock = new();

        private class ActiveArbEvent
        {
            public DateTime StartTime { get; set; }
            public decimal EntryNetCost { get; set; }
            public decimal BestGrossCost { get; set; }
            public decimal BestNetCost { get; set; }
            public decimal MaxVolumeAtBestSpread { get; set; }
            public int NumLegs { get; set; }
            public string LegPrices { get; set; }
        }

        public FastMergeArbTelemetryStrategy(
            Dictionary<string, List<string>> configuredEvents,
            decimal maxInvestmentPerTrade = 50.00m,
            decimal slippageCents = 0.02m,
            decimal minProfitPerSet = 0.02m,
            decimal depthFloorShares = 5m,
            double feeRate = 0.04,
            double feeExponent = 1.0)
        {
            _maxInvestmentPerTrade = maxInvestmentPerTrade;
            _slippageCents = slippageCents;
            _minProfitPerSet = minProfitPerSet;
            _depthFloorShares = depthFloorShares;
            _feeRate = feeRate;
            _feeExponent = feeExponent;

            foreach (var evt in configuredEvents)
            {
                string eventId = evt.Key;
                List<string> yesTokenIds = evt.Value;
                _eventTokens[eventId] = yesTokenIds;

                foreach (var token in yesTokenIds)
                {
                    _tokenToEventMap[token] = eventId;
                }
            }

            lock (_csvInitLock)
            {
                if (!_csvInitialized)
                {
                    // Updated CSV Headers with all new quantitative fields
                    File.WriteAllText(_csvFilePath, "StartTime,EndTime,DurationMs,EventId,NumLegs,LegPrices,EntryNetCost,BestGrossCost,TotalFees,BestNetCost,NetProfitPerShare,MaxVolume,TotalCapitalRequired,TotalPotentialProfit\n");
                    _ = Task.Run(ProcessCsvQueueAsync);
                    _csvInitialized = true;
                }
            }
        }

        public void OnBookUpdate(LocalOrderBook book, GlobalSimulatedBroker broker)
        {
            string asset = book.AssetId;
            if (!_tokenToEventMap.TryGetValue(asset, out var eventId)) return;

            _books[asset] = book;
            EvaluateArbitrageTelemetry(eventId);
        }

        /// <summary>
        /// Clears all transient state after a WebSocket reconnect.
        /// Prevents phantom arbs with inflated durations from spanning disconnects.
        /// </summary>
        public void OnReconnect()
        {
            _books.Clear();
            _activeArbs.Clear();
            _bestNetCostSeen.Clear();
        }

        /// <summary>
        /// Registers a new event discovered at runtime. Thread-safe.
        /// </summary>
        public void RegisterEvent(string eventId, List<string> tokenIds)
        {
            lock (_eventTokens)
            {
                if (_eventTokens.ContainsKey(eventId)) return;
                _eventTokens[eventId] = tokenIds;
                foreach (var token in tokenIds)
                    _tokenToEventMap[token] = eventId;
            }
        }

        private decimal CalculateFeePerShare(decimal price)
        {
            double p = (double)price;
            double fee = p * _feeRate * Math.Pow(p * (1.0 - p), _feeExponent);
            return Math.Round((decimal)fee, 4);
        }

        private void EvaluateArbitrageTelemetry(string eventId)
        {
            var yesTokenIds = _eventTokens[eventId];

            decimal totalGrossCost = 0m;
            decimal totalFeeCost = 0m;
            decimal totalMidSum = 0m;   // sum of (bid+ask)/2 — must be near $1.00 for true categorical
            decimal bottleneckShares = decimal.MaxValue;

            int legs = 0;
            string currentLegPrices = "";

            bool allLegsHaveBooks = true;
            int pricedLegs = 0;

            foreach (var token in yesTokenIds)
            {
                if (!_books.TryGetValue(token, out var book))
                {
                    allLegsHaveBooks = false;
                    totalGrossCost += 1.00m;
                    legs++;
                    continue;
                }

                if (!book.HasReceivedDelta) { allLegsHaveBooks = false; legs++; continue; }

                decimal bestAsk = book.GetBestAskPrice();
                if (bestAsk >= 1.00m) { allLegsHaveBooks = false; legs++; continue; }

                decimal bestBid = book.GetBestBidPrice();
                totalMidSum += bestBid > 0m ? (bestAsk + bestBid) / 2m : bestAsk;

                // Walk the ask side with slippage — matches execution strategy
                decimal maxPrice = Math.Min(bestAsk + _slippageCents, 0.99m);
                var walkResult = book.WalkAsks(maxPrice, _maxInvestmentPerTrade, 1.0m);

                if (walkResult.TotalShares <= 0)
                {
                    allLegsHaveBooks = false;
                    totalGrossCost += 1.00m;
                }
                else
                {
                    // Use VWAP (not bestAsk) for accurate cost — matches execution strategy
                    totalGrossCost += walkResult.Vwap;
                    totalFeeCost += CalculateFeePerShare(walkResult.Vwap);
                    bottleneckShares = Math.Min(bottleneckShares, walkResult.TotalShares);
                    currentLegPrices += $"{walkResult.Vwap:0.0000}|";
                    pricedLegs++;
                }

                legs++;
            }

            decimal totalNetCost = totalGrossCost + totalFeeCost;

            // Near-miss tracking: always update if we have at least some priced legs
            if (legs == yesTokenIds.Count && pricedLegs >= 2)
            {
                var entry = new NearMissEntry(totalNetCost, pricedLegs, yesTokenIds.Count,
                    bottleneckShares == decimal.MaxValue ? 0m : bottleneckShares);
                _bestNetCostSeen.AddOrUpdate(eventId, entry, (_, prev) =>
                    (pricedLegs > prev.PricedLegs ||
                     (pricedLegs == prev.PricedLegs && totalNetCost < prev.BestNetCost))
                        ? entry : prev);

                if ((DateTime.UtcNow - _lastNearMissReport).TotalSeconds >= NEAR_MISS_REPORT_INTERVAL_SEC)
                {
                    _lastNearMissReport = DateTime.UtcNow;
                    PrintNearMissReport();
                }
            }

            decimal profitPerSet = 1.00m - totalNetCost;

            // Arb detection: aligned with execution strategy thresholds.
            // Require average price per leg >= $0.10 to reject near-settled markets
            // where the winner's asks are gone and only stale low-price orders remain.
            decimal avgPricePerLeg = totalNetCost / yesTokenIds.Count;

            // Categorical sanity check: sum of midpoints must be near $1.00.
            // Spread/correlated markets (e.g. soccer "wins by >1.5" AND ">2.5") can have
            // all legs resolve NO (0-0 draw), so they are NOT mutually exclusive.
            // Their mid-sum will be ~0.40-0.60. True categoricals sum to ~0.95-1.05.
            bool isCategoricallySound = pricedLegs == yesTokenIds.Count && totalMidSum >= 0.80m;

            bool isArbAlive = allLegsHaveBooks && isCategoricallySound
                && avgPricePerLeg >= 0.10m
                && totalNetCost < 0.995m
                && profitPerSet >= _minProfitPerSet
                && bottleneckShares >= _depthFloorShares;

            if (isArbAlive)
            {
                if (!_activeArbs.TryGetValue(eventId, out var currentArb))
                {
                    // NEW ARB DETECTED
                    _activeArbs[eventId] = new ActiveArbEvent
                    {
                        StartTime = DateTime.UtcNow,
                        EntryNetCost = totalNetCost,
                        BestGrossCost = totalGrossCost,
                        BestNetCost = totalNetCost,
                        MaxVolumeAtBestSpread = bottleneckShares,
                        NumLegs = legs,
                        LegPrices = currentLegPrices.TrimEnd('|')
                    };
                    Console.WriteLine($"[TELEMETRY] ARB OPEN: {eventId} | Cost ${totalNetCost:0.0000} | MidSum ${totalMidSum:0.0000} | {legs}L | Depth {bottleneckShares:0.0}");
                    foreach (var t in yesTokenIds)
                    {
                        if (!_books.TryGetValue(t, out var db)) continue;
                        Console.WriteLine($"  leg {t[..Math.Min(20,t.Length)],-20} bestAsk={db.GetBestAskPrice():0.0000}  bestBid={db.GetBestBidPrice():0.0000}");
                    }
                    OnArbOpened?.Invoke(eventId, totalNetCost, legs, bottleneckShares);
                }
                else
                {
                    // EXISTING ARB - Update if it deepened
                    if (totalNetCost < currentArb.BestNetCost)
                    {
                        currentArb.BestGrossCost = totalGrossCost;
                        currentArb.BestNetCost = totalNetCost;
                        currentArb.MaxVolumeAtBestSpread = bottleneckShares;
                        currentArb.LegPrices = currentLegPrices.TrimEnd('|'); // Save the prices that formed the peak spread
                    }
                }
            }
            else
            {
                // ARB CLOSED
                if (_activeArbs.TryRemove(eventId, out var closedArb))
                {
                    DateTime endTime = DateTime.UtcNow;
                    TimeSpan duration = endTime - closedArb.StartTime;
                    
                    // Only log arbs that survived longer than 5ms (filtering out micro-glitches)
                    if (duration.TotalMilliseconds > 5)
                    {
                        LogArbAutopsy(eventId, closedArb, duration, endTime);
                    }
                }
            }
        }

        private void LogArbAutopsy(string eventId, ActiveArbEvent arbData, TimeSpan duration, DateTime endTime)
        {
            // Calculate best fees retrospectively
            decimal bestFees = arbData.BestNetCost - arbData.BestGrossCost;
            
            decimal netProfitPerShare = 1.00m - arbData.BestNetCost;
            decimal totalPotentialProfit = netProfitPerShare * arbData.MaxVolumeAtBestSpread;
            decimal capitalRequired = arbData.MaxVolumeAtBestSpread * arbData.BestNetCost;

            // Timestamp formatting
            string startStr = arbData.StartTime.ToString("HH:mm:ss.fff");
            string endStr = endTime.ToString("HH:mm:ss.fff");

            // Console output
            Console.WriteLine($"\n[TELEMETRY] ⚡ ARB CLOSED: {eventId}");
            Console.WriteLine($"   ├ Time: {startStr} to {endStr} ({duration.TotalMilliseconds:0} ms)");
            Console.WriteLine($"   ├ Legs: {arbData.NumLegs} [{arbData.LegPrices}]");
            Console.WriteLine($"   ├ Spread: Entry ${arbData.EntryNetCost:0.0000} -> Peak ${arbData.BestNetCost:0.0000}");
            Console.WriteLine($"   ├ Peak Cost Breakdown: Gross ${arbData.BestGrossCost:0.0000} + Fees ${bestFees:0.0000}");
            Console.WriteLine($"   └ Capital Req: ${capitalRequired:0.00} -> Net Profit: ${totalPotentialProfit:0.00} (Vol: {arbData.MaxVolumeAtBestSpread})");

            // Format the CSV row securely (quoting strings with commas or pipes)
            string csvRow = $"{startStr},{endStr},{duration.TotalMilliseconds:0},\"{eventId}\",{arbData.NumLegs},\"{arbData.LegPrices}\",{arbData.EntryNetCost:0.0000},{arbData.BestGrossCost:0.0000},{bestFees:0.0000},{arbData.BestNetCost:0.0000},{netProfitPerShare:0.0000},{arbData.MaxVolumeAtBestSpread:0.00},{capitalRequired:0.00},{totalPotentialProfit:0.00}";
            
            // Toss it into the background queue (Instant execution, zero CPU lag)
            _csvQueue.Writer.TryWrite(csvRow);
        }

        public void PrintNearMissReport()
        {
            int totalEvents = _eventTokens.Count;
            int totalTokens = _tokenToEventMap.Count;
            int booksLoaded = _books.Count;
            int eventsTracked = _bestNetCostSeen.Count;

            // Split into fully-priced vs partial
            var fullyPriced = _bestNetCostSeen
                .Where(kv => kv.Value.PricedLegs == kv.Value.TotalLegs
                          && kv.Value.BestNetCost / kv.Value.TotalLegs >= 0.10m)
                .OrderBy(kv => kv.Value.BestNetCost)
                .Take(10)
                .ToList();

            int partialCount = eventsTracked - fullyPriced.Count;

            int currentlyOpen = _activeArbs.Count;
            Console.WriteLine($"\n[TELEMETRY] --- TOP 10 CLOSEST TO ARB (< $1.00 = profit) ---");
            Console.WriteLine($"  Books: {booksLoaded}/{totalTokens} tokens | Fully priced: {fullyPriced.Count} | Partial: {partialCount} | Total events: {totalEvents} | Open arbs: {currentlyOpen}");
            if (fullyPriced.Count == 0)
            {
                Console.WriteLine("  (no events with all legs priced yet)");
            }
            else
            {
                foreach (var kv in fullyPriced)
                {
                    var e = kv.Value;
                    decimal gap = e.BestNetCost - 1.00m;
                    string status = gap < 0 ? "ARB!" : $"+${gap:0.0000} away";
                    string depthStr = e.BottleneckShares < 1m   ? $"THIN({e.BottleneckShares:0.00})"
                                    : e.BottleneckShares < 10m  ? $"low({e.BottleneckShares:0.0})"
                                    : $"{e.BottleneckShares:0.0}";
                    string liveTag = _activeArbs.ContainsKey(kv.Key) ? " *** LIVE ***" : "";
                    Console.WriteLine($"  ${e.BestNetCost:0.0000} ({status}) [{e.TotalLegs}L] depth={depthStr}{liveTag} | Event: {kv.Key}");
                }
            }
            Console.WriteLine();
        }

        // --- BACKGROUND WRITER TASK ---
        private async Task ProcessCsvQueueAsync()
        {
            try
            {
                using var writer = new StreamWriter(_csvFilePath, append: true);
                
                await foreach (var line in _csvQueue.Reader.ReadAllAsync())
                {
                    await writer.WriteLineAsync(line);
                    await writer.FlushAsync(); 
                }
            }
            catch (Exception ex)
            {
                Console.WriteLine($"[TELEMETRY LOGGER ERROR] {ex.Message}");
            }
        }
    }
}