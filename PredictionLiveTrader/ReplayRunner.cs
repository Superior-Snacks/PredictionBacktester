using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.IO.Compression;
using System.Linq;
using System.Text.Json;
using PredictionBacktester.Engine;

namespace PredictionLiveTrader;

/// <summary>
/// Replays recorded .gz WebSocket data through the same strategy pipeline as the live paper trader.
/// Uses replay timestamps for realistic latency simulation — orders are queued and executed
/// when the replay clock advances past the latency window, so price slippage is real.
/// Usage: dotnet run --project PredictionLiveTrader -- --replay MarketData1week
/// </summary>
static class ReplayRunner
{
    private const int REPLAY_LATENCY_MS = 250; // Same as live paper trader default

    public static void Run(string dataDirectory, List<StrategyConfig> strategyConfigs)
    {
        Console.Clear();
        Console.WriteLine("=========================================");
        Console.WriteLine("  REPLAY BACKTESTER — LOCAL .GZ DATA     ");
        Console.WriteLine($"  Latency: {REPLAY_LATENCY_MS}ms        ");
        Console.WriteLine("=========================================");

        // --- Setup: same as live paper trader ---
        var tokenNames = new Dictionary<string, string>();
        var tokenMinSizes = new Dictionary<string, decimal>();
        const decimal MAX_BET_SIZE = 1000.00m;

        var brokers = new Dictionary<string, ReplayBroker>();
        foreach (var config in strategyConfigs)
        {
            var broker = new ReplayBroker(config.Name, config.StartingCapital, tokenNames, tokenMinSizes, MAX_BET_SIZE, REPLAY_LATENCY_MS);
            broker.IsMuted = true; // Suppress per-trade console output during replay
            brokers[config.Name] = broker;
        }

        var orderBooks = new Dictionary<string, LocalOrderBook>();
        var activeStrategies = new Dictionary<string, List<ILiveStrategy>>();

        // --- Find and sort .gz files ---
        var gzFiles = Directory.GetFiles(dataDirectory, "*.gz").OrderBy(f => f).ToArray();
        if (gzFiles.Length == 0)
        {
            Console.WriteLine($"No .gz files found in '{dataDirectory}'!");
            return;
        }

        Console.WriteLine($"Found {gzFiles.Length} data file(s):");
        foreach (var f in gzFiles)
            Console.WriteLine($"  {Path.GetFileName(f)}");
        Console.WriteLine();

        long totalTicks = 0;
        long totalPriceChanges = 0;
        long totalBookSnapshots = 0;
        var overallSw = Stopwatch.StartNew();

        int maxNameLength = strategyConfigs.Max(c => c.Name.Length);

        // Pre-calculate total compressed size for progress bar
        long totalCompressedBytes = gzFiles.Sum(f => new FileInfo(f).Length);
        long bytesProcessedSoFar = 0;

        foreach (var gzFile in gzFiles)
        {
            string fileName = Path.GetFileName(gzFile);
            long fileSize = new FileInfo(gzFile).Length;
            Console.WriteLine($"\n--- Replaying {fileName} ({fileSize / (1024.0 * 1024 * 1024):0.1} GB) ---");

            long fileTicks = 0;
            var fileSw = Stopwatch.StartNew();
            long lastReportBytes = 0;

            using var fileStream = new FileStream(gzFile, FileMode.Open, FileAccess.Read, FileShare.Read, bufferSize: 1024 * 1024);
            using var gzStream = new GZipStream(fileStream, CompressionMode.Decompress);
            using var reader = new StreamReader(gzStream, bufferSize: 1024 * 256);

            string? line;
            while ((line = reader.ReadLine()) != null)
            {
                // Format: {unixTimestampMs}|{json}
                int pipeIndex = line.IndexOf('|');
                if (pipeIndex < 0) continue;

                if (!long.TryParse(line.AsSpan(0, pipeIndex), out long tickTimestampMs)) continue;

                string json = line[(pipeIndex + 1)..];
                if (string.IsNullOrWhiteSpace(json)) continue;

                fileTicks++;
                totalTicks++;

                // Advance the replay clock on all brokers & drain deferred orders
                foreach (var broker in brokers.Values)
                {
                    broker.ReplayTimeMs = tickTimestampMs;
                    broker.DrainDeferredOrders(orderBooks);
                }

                try
                {
                    using var doc = JsonDocument.Parse(json);
                    var root = doc.RootElement;

                    if (root.ValueKind == JsonValueKind.Array)
                    {
                        // Book snapshot: [{market, asset_id, bids, asks, ...}, ...]
                        foreach (var entry in root.EnumerateArray())
                        {
                            if (!entry.TryGetProperty("asset_id", out var assetIdEl)) continue;
                            string? assetId = assetIdEl.GetString();
                            if (string.IsNullOrEmpty(assetId)) continue;

                            EnsureAssetInitialized(assetId, orderBooks, activeStrategies, strategyConfigs, tokenNames);

                            if (entry.TryGetProperty("bids", out var bidsEl) && entry.TryGetProperty("asks", out var asksEl))
                            {
                                orderBooks[assetId].ProcessBookUpdate(bidsEl, asksEl);
                                totalBookSnapshots++;
                            }
                        }
                    }
                    else if (root.ValueKind == JsonValueKind.Object)
                    {
                        ProcessObjectMessage(root, orderBooks, activeStrategies, strategyConfigs,
                            tokenNames, brokers, ref totalBookSnapshots, ref totalPriceChanges);
                    }
                }
                catch (JsonException)
                {
                    // Skip malformed lines
                }

                // Progress bar based on compressed bytes read (updated every ~2MB)
                long currentPos = fileStream.Position;
                if (currentPos - lastReportBytes >= 2 * 1024 * 1024)
                {
                    lastReportBytes = currentPos;
                    double filePercent = (double)currentPos / fileSize * 100;
                    double overallPercent = (double)(bytesProcessedSoFar + currentPos) / totalCompressedBytes * 100;
                    double elapsedSec = overallSw.Elapsed.TotalSeconds;
                    double bytesPerSec = (bytesProcessedSoFar + currentPos) / Math.Max(elapsedSec, 0.001);
                    long bytesRemaining = totalCompressedBytes - bytesProcessedSoFar - currentPos;
                    double etaSec = bytesRemaining / Math.Max(bytesPerSec, 1);
                    TimeSpan eta = TimeSpan.FromSeconds(etaSec);

                    // Build progress bar
                    int barWidth = 30;
                    int filled = (int)(overallPercent / 100 * barWidth);
                    string bar = new string('#', filled) + new string('-', barWidth - filled);

                    Console.Write($"\r  [{bar}] {overallPercent:0.1}% | {fileTicks:N0} ticks | ETA: {eta:hh\\:mm\\:ss}   ");
                }
            }

            bytesProcessedSoFar += fileSize;
            fileSw.Stop();
            Console.WriteLine($"\r  {fileTicks:N0} ticks in {fileSw.Elapsed.TotalSeconds:0.1}s ({fileTicks / Math.Max(fileSw.Elapsed.TotalSeconds, 0.001):N0}/sec)                              ");
        }

        // Final drain — execute any remaining deferred orders
        foreach (var broker in brokers.Values)
            broker.DrainDeferredOrders(orderBooks);

        overallSw.Stop();

        // --- Results ---
        Console.WriteLine("\n\n=========================================");
        Console.WriteLine("  REPLAY COMPLETE");
        Console.WriteLine("=========================================");
        Console.WriteLine($"  Total ticks:     {totalTicks:N0}");
        Console.WriteLine($"  Book snapshots:  {totalBookSnapshots:N0}");
        Console.WriteLine($"  Price changes:   {totalPriceChanges:N0}");
        Console.WriteLine($"  Unique assets:   {orderBooks.Count:N0}");
        Console.WriteLine($"  Elapsed:         {overallSw.Elapsed.TotalSeconds:0.1}s");
        Console.WriteLine($"  Throughput:      {totalTicks / Math.Max(overallSw.Elapsed.TotalSeconds, 0.001):N0} ticks/sec");
        Console.WriteLine();

        // --- Strategy Leaderboard ---
        Console.WriteLine("  STRATEGY LEADERBOARD (Sorted by Equity)");
        Console.WriteLine("  " + new string('-', 110));

        var results = strategyConfigs
            .Select(c =>
            {
                var b = brokers[c.Name];
                decimal equity = b.GetTotalPortfolioValue();
                decimal pnl = equity - c.StartingCapital;
                decimal realizedPnl = b.CashBalance - c.StartingCapital;
                decimal mtm = equity - b.CashBalance;
                return new { c.Name, Equity = equity, PnL = pnl, Realized = realizedPnl, MTM = mtm, b.TotalActions, b.TotalTradesExecuted, b.WinningTrades, b.LosingTrades, b.RejectedOrders };
            })
            .OrderByDescending(x => x.Equity)
            .ToList();

        foreach (var r in results)
        {
            Console.ForegroundColor = r.PnL >= 0 ? ConsoleColor.Green : ConsoleColor.Red;
            Console.WriteLine($"  [{r.Name.PadRight(maxNameLength)}] Equity: ${r.Equity:0.00} | PnL: ${r.PnL:0.00} (Real: ${r.Realized:0.00} + MTM: ${r.MTM:0.00}) | Actions: {r.TotalActions} Exits: {r.TotalTradesExecuted} (W:{r.WinningTrades} L:{r.LosingTrades}) Rej: {r.RejectedOrders}");
        }
        Console.ResetColor();
        Console.WriteLine();

        // --- Export CSV ---
        string csvFilename = $"ReplayTrades_{DateTime.Now:yyyyMMdd_HHmmss}.csv";
        try
        {
            using var writer = new StreamWriter(csvFilename);
            writer.WriteLine("Timestamp,StrategyName,StartingCapital,MarketName,AssetId,Side,ExecutionPrice,Shares,DollarValue");

            int totalTrades = 0;
            foreach (var config in strategyConfigs)
            {
                var broker = brokers[config.Name];
                foreach (var trade in broker.TradeLedger)
                {
                    string marketName = tokenNames.GetValueOrDefault(trade.OutcomeId, "Unknown");
                    marketName = $"\"{marketName.Replace("\"", "\"\"")}\"";
                    writer.WriteLine($"{trade.Date:O},{config.Name},{config.StartingCapital},{marketName},{trade.OutcomeId},{trade.Side},{trade.Price},{trade.Shares},{trade.DollarValue}");
                    totalTrades++;
                }
            }

            Console.ForegroundColor = ConsoleColor.Green;
            Console.WriteLine($"  {totalTrades} trades exported to {csvFilename}");
            Console.ResetColor();
        }
        catch (Exception ex)
        {
            Console.ForegroundColor = ConsoleColor.Red;
            Console.WriteLine($"  Export failed: {ex.Message}");
            Console.ResetColor();
        }
    }

    private static void ProcessObjectMessage(
        JsonElement root,
        Dictionary<string, LocalOrderBook> orderBooks,
        Dictionary<string, List<ILiveStrategy>> activeStrategies,
        List<StrategyConfig> strategyConfigs,
        Dictionary<string, string> tokenNames,
        Dictionary<string, ReplayBroker> brokers,
        ref long totalBookSnapshots,
        ref long totalPriceChanges)
    {
        if (root.TryGetProperty("event_type", out var eventTypeEl))
        {
            string? eventType = eventTypeEl.GetString();

            if (eventType == "book" && root.TryGetProperty("asset_id", out var assetIdEl))
            {
                string? assetId = assetIdEl.GetString();
                if (string.IsNullOrEmpty(assetId)) return;

                EnsureAssetInitialized(assetId, orderBooks, activeStrategies, strategyConfigs, tokenNames);

                if (root.TryGetProperty("bids", out var bidsEl) && root.TryGetProperty("asks", out var asksEl))
                {
                    orderBooks[assetId].ProcessBookUpdate(bidsEl, asksEl);
                    totalBookSnapshots++;
                }
            }
            else if (eventType == "price_change" && root.TryGetProperty("price_changes", out var changesEl))
            {
                ProcessPriceChanges(changesEl, orderBooks, activeStrategies, strategyConfigs, tokenNames, brokers, ref totalPriceChanges);
            }
        }
        else if (root.TryGetProperty("price_changes", out var changesEl2))
        {
            // price_change without event_type wrapper
            ProcessPriceChanges(changesEl2, orderBooks, activeStrategies, strategyConfigs, tokenNames, brokers, ref totalPriceChanges);
        }
    }

    private static void ProcessPriceChanges(
        JsonElement changesEl,
        Dictionary<string, LocalOrderBook> orderBooks,
        Dictionary<string, List<ILiveStrategy>> activeStrategies,
        List<StrategyConfig> strategyConfigs,
        Dictionary<string, string> tokenNames,
        Dictionary<string, ReplayBroker> brokers,
        ref long totalPriceChanges)
    {
        foreach (var change in changesEl.EnumerateArray())
        {
            if (!change.TryGetProperty("asset_id", out var idEl)) continue;
            string? assetId = idEl.GetString();
            if (string.IsNullOrEmpty(assetId) || !orderBooks.ContainsKey(assetId)) continue;

            decimal price = decimal.Parse(change.GetProperty("price").GetString() ?? "0");
            decimal size = decimal.Parse(change.GetProperty("size").GetString() ?? "0");
            string side = change.GetProperty("side").GetString() ?? "";

            orderBooks[assetId].UpdatePriceLevel(side, price, size);
            totalPriceChanges++;

            // Reset consumed liquidity (same as live)
            foreach (var broker in brokers.Values)
                broker.ResetConsumedLiquidity(assetId);

            // Feed to all strategies
            var book = orderBooks[assetId];
            var strategies = activeStrategies[assetId];
            foreach (var strategy in strategies)
            {
                strategy.OnBookUpdate(book, brokers[strategy.StrategyName]);
            }
        }
    }

    private static void EnsureAssetInitialized(
        string assetId,
        Dictionary<string, LocalOrderBook> orderBooks,
        Dictionary<string, List<ILiveStrategy>> activeStrategies,
        List<StrategyConfig> strategyConfigs,
        Dictionary<string, string> tokenNames)
    {
        if (orderBooks.ContainsKey(assetId)) return;

        orderBooks[assetId] = new LocalOrderBook(assetId);
        activeStrategies[assetId] = strategyConfigs
            .Select(c => c.Factory())
            .ToList();

        // Use truncated asset ID as name since we don't have API access
        tokenNames.TryAdd(assetId, assetId[..Math.Min(12, assetId.Length)] + "...");
    }
}
