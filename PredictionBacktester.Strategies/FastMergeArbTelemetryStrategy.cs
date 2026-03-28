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

        // --- NEAR-MISS TRACKING ---
        // Tracks the tightest spread seen per event (closest to arb)
        private readonly ConcurrentDictionary<string, decimal> _bestNetCostSeen = new();
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

        public FastMergeArbTelemetryStrategy(Dictionary<string, List<string>> configuredEvents)
        {
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

        // Polynomial Fee Formula
        private decimal CalculateFeePerShare(decimal price)
        {
            double p = (double)price;
            double feeRate = 0.04; // Baseline Rate
            double exponent = 1.0; // Baseline Exponent
            double fee = p * feeRate * Math.Pow(p * (1.0 - p), exponent);
            return Math.Round((decimal)fee, 4);
        }

        private void EvaluateArbitrageTelemetry(string eventId)
        {
            var yesTokenIds = _eventTokens[eventId];
            
            decimal totalGrossCost = 0m;
            decimal totalFeeCost = 0m;
            decimal bottleneckShares = decimal.MaxValue;
            
            int legs = 0;
            string currentLegPrices = "";

            foreach (var token in yesTokenIds)
            {
                if (!_books.TryGetValue(token, out var book)) return; 

                decimal bestAsk = book.GetBestAskPrice();
                decimal availableSize = book.GetBestAskSize();

                if (bestAsk >= 1.00m || availableSize <= 0)
                {
                    totalGrossCost = 1.00m; 
                    break; 
                }

                totalGrossCost += bestAsk;
                totalFeeCost += CalculateFeePerShare(bestAsk);
                bottleneckShares = Math.Min(bottleneckShares, availableSize);
                
                legs++;
                currentLegPrices += $"{bestAsk:0.000}|";
            }

            decimal totalNetCost = totalGrossCost + totalFeeCost;

            // Track tightest spread per event (only for fully-priced events)
            if (legs == yesTokenIds.Count && totalGrossCost > 0)
            {
                _bestNetCostSeen.AddOrUpdate(eventId, totalNetCost, (_, prev) => Math.Min(prev, totalNetCost));

                // Periodic near-miss report
                if ((DateTime.UtcNow - _lastNearMissReport).TotalSeconds >= NEAR_MISS_REPORT_INTERVAL_SEC)
                {
                    _lastNearMissReport = DateTime.UtcNow;
                    PrintNearMissReport();
                }
            }

            // Arb is alive if cost (including fees) is strictly less than $1.00
            bool isArbAlive = totalGrossCost > 0 && totalNetCost < 1.00m && bottleneckShares >= 1.0m;

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
            var sorted = _bestNetCostSeen
                .OrderBy(kv => kv.Value)
                .Take(10)
                .ToList();

            if (sorted.Count == 0) return;

            Console.WriteLine($"\n[TELEMETRY] --- TOP 10 CLOSEST TO ARB (< $1.00 = profit) ---");
            foreach (var kv in sorted)
            {
                decimal gap = kv.Value - 1.00m;
                string status = gap < 0 ? "ARB!" : $"+${gap:0.0000} away";
                Console.WriteLine($"  ${kv.Value:0.0000} ({status}) | Event: {kv.Key}");
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