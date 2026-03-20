using System;
using System.Collections.Concurrent;
using System.Collections.Generic;
using System.IO;
using System.Threading.Channels;
using System.Threading.Tasks;
using PredictionBacktester.Engine;

namespace PredictionBacktester.Strategies
{
    public class FastMergeArbTelemetryStrategy : ILiveStrategy
    {
        public string StrategyName => "Fast_Merge_Arb_Telemetry";

        private readonly Dictionary<string, string> _tokenToMarketMap = new();
        private readonly Dictionary<string, List<string>> _marketTokens = new();
        private readonly ConcurrentDictionary<string, LocalOrderBook> _books = new();
        private readonly ConcurrentDictionary<string, ActiveArbEvent> _activeArbs = new();

        private readonly decimal _arbThreshold = 0.98m; 

        // --- CSV TELEMETRY INFRASTRUCTURE (shared across all instances) ---
        private static readonly string _csvFilePath = $"ArbTelemetry_{DateTime.UtcNow:yyyyMMdd_HHmmss}.csv";
        private static readonly Channel<string> _csvQueue = Channel.CreateUnbounded<string>();
        private static bool _csvInitialized = false;
        private static readonly object _csvInitLock = new();

        private class ActiveArbEvent
        {
            public DateTime StartTime { get; set; }
            public decimal BestSpreadCost { get; set; }
            public decimal MaxVolumeAtBestSpread { get; set; }
        }

        public FastMergeArbTelemetryStrategy(Dictionary<string, List<string>> configuredMarkets)
        {
            foreach (var market in configuredMarkets)
            {
                string marketId = market.Key;
                List<string> tokenIds = market.Value;
                _marketTokens[marketId] = tokenIds;

                foreach (var token in tokenIds)
                {
                    _tokenToMarketMap[token] = marketId;
                }
            }

            // Initialize CSV file and background writer once across all instances
            lock (_csvInitLock)
            {
                if (!_csvInitialized)
                {
                    File.WriteAllText(_csvFilePath, "Timestamp,MarketId,DurationMs,BestCost,SpreadProfit,MaxVolume,TotalPotentialProfit\n");
                    _ = Task.Run(ProcessCsvQueueAsync);
                    _csvInitialized = true;
                }
            }
        }

        public void OnBookUpdate(LocalOrderBook book, GlobalSimulatedBroker broker)
        {
            string asset = book.AssetId;
            if (!_tokenToMarketMap.TryGetValue(asset, out var marketId)) return;

            _books[asset] = book;
            EvaluateArbitrageTelemetry(marketId);
        }

        private void EvaluateArbitrageTelemetry(string marketId)
        {
            var tokenIds = _marketTokens[marketId];
            decimal totalCost = 0m;
            decimal bottleneckShares = decimal.MaxValue;

            foreach (var token in tokenIds)
            {
                if (!_books.TryGetValue(token, out var book)) return; 

                decimal bestAsk = book.GetBestAskPrice();
                decimal availableSize = book.GetBestAskSize();

                if (bestAsk >= 1.00m || availableSize <= 0)
                {
                    totalCost = 1.00m; 
                    break; 
                }

                totalCost += bestAsk;
                bottleneckShares = Math.Min(bottleneckShares, availableSize);
            }

            bool isArbAlive = totalCost > 0 && totalCost <= _arbThreshold && bottleneckShares >= 1.0m;

            if (isArbAlive)
            {
                if (!_activeArbs.TryGetValue(marketId, out var currentArb))
                {
                    _activeArbs[marketId] = new ActiveArbEvent
                    {
                        StartTime = DateTime.UtcNow,
                        BestSpreadCost = totalCost,
                        MaxVolumeAtBestSpread = bottleneckShares
                    };
                }
                else
                {
                    if (totalCost < currentArb.BestSpreadCost)
                    {
                        currentArb.BestSpreadCost = totalCost;
                        currentArb.MaxVolumeAtBestSpread = bottleneckShares;
                    }
                }
            }
            else
            {
                if (_activeArbs.TryRemove(marketId, out var closedArb))
                {
                    TimeSpan duration = DateTime.UtcNow - closedArb.StartTime;
                    
                    if (duration.TotalMilliseconds > 5)
                    {
                        LogArbAutopsy(marketId, closedArb, duration);
                    }
                }
            }
        }

        private void LogArbAutopsy(string marketId, ActiveArbEvent arbData, TimeSpan duration)
        {
            decimal profitPerShare = 1.00m - arbData.BestSpreadCost;
            decimal totalPotentialProfit = profitPerShare * arbData.MaxVolumeAtBestSpread;

            // 1. Print to the Console for your live viewing
            Console.WriteLine($"\n[TELEMETRY] ⚡ ARB CLOSED: {marketId}");
            Console.WriteLine($"   ├ Duration: {duration.TotalMilliseconds:0} ms");
            Console.WriteLine($"   ├ Best Cost: ${arbData.BestSpreadCost:0.00} (Spread: ${profitPerShare:0.00})");
            Console.WriteLine($"   └ Max Vol: {arbData.MaxVolumeAtBestSpread} shares -> Potential Profit: ${totalPotentialProfit:0.00}");

            // 2. Format the CSV row (Use Quotes around the market ID in case it contains commas)
            string csvRow = $"{DateTime.UtcNow:O},\"{marketId}\",{duration.TotalMilliseconds:0},{arbData.BestSpreadCost:0.000},{profitPerShare:0.000},{arbData.MaxVolumeAtBestSpread:0.00},{totalPotentialProfit:0.00}";
            
            // 3. Toss it into the background queue (Instant execution, zero CPU lag)
            _csvQueue.Writer.TryWrite(csvRow);
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
                    
                    // Note: Calling Flush here is completely safe!
                    // Unlike the order book logger (which fired 10,000 times a second),
                    // this only fires when an actual Arb closes (maybe a few times an hour).
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