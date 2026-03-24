using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Globalization;
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
    private const int WARMUP_SKIP_THRESHOLD = 50; // After this many consecutive []-lines with no new asset, stop parsing them

    public static void Run(string dataDirectory, List<StrategyConfig> strategyConfigs)
    {
        // Auto-detect binary format
        string binPath = Path.Combine(dataDirectory, "replay.bin");
        string idxPath = Path.Combine(dataDirectory, "replay.idx");
        if (File.Exists(binPath) && File.Exists(idxPath))
        {
            RunBinary(binPath, idxPath, strategyConfigs);
            return;
        }

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
        bool hasDeferredOrders = false; // Fast check to avoid iterating brokers every tick

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
        long skippedBookSnapshots = 0;
        var overallSw = Stopwatch.StartNew();

        int maxNameLength = strategyConfigs.Max(c => c.Name.Length);

        // Pre-calculate total compressed size for progress bar
        long totalCompressedBytes = gzFiles.Sum(f => new FileInfo(f).Length);
        long bytesProcessedSoFar = 0;

        // Track whether all assets are warmed up — after N consecutive [] lines with no new assets, skip parsing them
        int consecutiveNoNewAsset = 0;
        bool assetsWarmedUp = false;

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

                // Fast peek: skip full book snapshot lines (start with '[') — they're huge JSON arrays
                int jsonStart = pipeIndex + 1;
                if (jsonStart >= line.Length) continue;

                char firstChar = line[jsonStart];
                while (firstChar == ' ' && jsonStart < line.Length - 1) firstChar = line[++jsonStart];

                if (firstChar == '[')
                {
                    fileTicks++;
                    totalTicks++;

                    // Once warmed up, skip ALL book snapshot lines without parsing
                    if (assetsWarmedUp)
                    {
                        skippedBookSnapshots++;
                        goto progressCheck;
                    }

                    int prevCount = orderBooks.Count;
                    try
                    {
                        using var doc = JsonDocument.Parse(line.AsMemory(pipeIndex + 1));
                        foreach (var entry in doc.RootElement.EnumerateArray())
                        {
                            if (!entry.TryGetProperty("asset_id", out var idEl)) continue;
                            string? assetId = idEl.GetString();
                            if (string.IsNullOrEmpty(assetId)) continue;

                            if (orderBooks.ContainsKey(assetId)) continue;

                            EnsureAssetInitialized(assetId, orderBooks, activeStrategies, strategyConfigs, tokenNames);

                            if (entry.TryGetProperty("bids", out var bidsEl) && entry.TryGetProperty("asks", out var asksEl))
                            {
                                orderBooks[assetId].ProcessBookUpdate(bidsEl, asksEl);
                                totalBookSnapshots++;
                            }
                        }
                    }
                    catch (JsonException) { }

                    // Track consecutive no-new-asset lines
                    if (orderBooks.Count == prevCount)
                    {
                        consecutiveNoNewAsset++;
                        if (consecutiveNoNewAsset >= WARMUP_SKIP_THRESHOLD)
                        {
                            assetsWarmedUp = true;
                            Console.WriteLine($"\r  Assets warmed up ({orderBooks.Count} assets). Skipping future book snapshots.                    ");
                        }
                    }
                    else
                    {
                        consecutiveNoNewAsset = 0;
                    }

                    goto progressCheck;
                }

                fileTicks++;
                totalTicks++;

                // Drain deferred orders only when there are some pending
                if (hasDeferredOrders)
                {
                    hasDeferredOrders = false;
                    foreach (var broker in brokers.Values)
                    {
                        broker.ReplayTimeMs = tickTimestampMs;
                        broker.DrainDeferredOrders(orderBooks);
                        if (broker.HasDeferredOrders) hasDeferredOrders = true;
                    }
                }

                // Fast string-based extraction instead of full JSON parse for price_change lines
                // Most lines are price_change events with a known structure
                if (TryProcessPriceChangeFast(line, pipeIndex + 1, tickTimestampMs, orderBooks, activeStrategies,
                    strategyConfigs, tokenNames, brokers, ref totalPriceChanges, ref hasDeferredOrders))
                {
                    goto progressCheck;
                }

                // Fallback to full JSON parse for unknown formats
                try
                {
                    using var doc = JsonDocument.Parse(line.AsMemory(pipeIndex + 1));
                    var root = doc.RootElement;

                    if (root.ValueKind == JsonValueKind.Object)
                    {
                        if (root.TryGetProperty("event_type", out var eventTypeEl))
                        {
                            string? eventType = eventTypeEl.GetString();

                            if (eventType == "book" && root.TryGetProperty("asset_id", out var assetIdEl))
                            {
                                string? assetId = assetIdEl.GetString();
                                if (!string.IsNullOrEmpty(assetId))
                                {
                                    EnsureAssetInitialized(assetId, orderBooks, activeStrategies, strategyConfigs, tokenNames);
                                    if (root.TryGetProperty("bids", out var bidsEl) && root.TryGetProperty("asks", out var asksEl))
                                    {
                                        orderBooks[assetId].ProcessBookUpdate(bidsEl, asksEl);
                                        totalBookSnapshots++;
                                    }
                                }
                            }
                            else if (eventType == "price_change" && root.TryGetProperty("price_changes", out var changesEl))
                            {
                                ProcessPriceChanges(changesEl, tickTimestampMs, orderBooks, activeStrategies, strategyConfigs,
                                    tokenNames, brokers, ref totalPriceChanges, ref hasDeferredOrders);
                            }
                        }
                        else if (root.TryGetProperty("price_changes", out var changesEl2))
                        {
                            ProcessPriceChanges(changesEl2, tickTimestampMs, orderBooks, activeStrategies, strategyConfigs,
                                tokenNames, brokers, ref totalPriceChanges, ref hasDeferredOrders);
                        }
                    }
                }
                catch (JsonException) { }

                progressCheck:
                // Progress bar based on compressed bytes read (updated every ~2MB)
                long currentPos = fileStream.Position;
                if (currentPos - lastReportBytes >= 2 * 1024 * 1024)
                {
                    lastReportBytes = currentPos;
                    double overallPercent = (double)(bytesProcessedSoFar + currentPos) / totalCompressedBytes * 100;
                    double elapsedSec = overallSw.Elapsed.TotalSeconds;
                    double bytesPerSec = (bytesProcessedSoFar + currentPos) / Math.Max(elapsedSec, 0.001);
                    long bytesRemaining = totalCompressedBytes - bytesProcessedSoFar - currentPos;
                    double etaSec = bytesRemaining / Math.Max(bytesPerSec, 1);
                    TimeSpan eta = TimeSpan.FromSeconds(etaSec);

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
        Console.WriteLine($"  Total ticks:        {totalTicks:N0}");
        Console.WriteLine($"  Book snapshots:     {totalBookSnapshots:N0}");
        Console.WriteLine($"  Skipped snapshots:  {skippedBookSnapshots:N0}");
        Console.WriteLine($"  Price changes:      {totalPriceChanges:N0}");
        Console.WriteLine($"  Unique assets:      {orderBooks.Count:N0}");
        Console.WriteLine($"  Elapsed:            {overallSw.Elapsed.TotalSeconds:0.1}s");
        Console.WriteLine($"  Throughput:         {totalTicks / Math.Max(overallSw.Elapsed.TotalSeconds, 0.001):N0} ticks/sec");
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

    /// <summary>
    /// Fast string-based parser for price_change lines. Avoids JsonDocument allocation entirely.
    /// Extracts asset_id, price, size, side via substring search — ~10x faster than JsonDocument.Parse.
    /// Returns true if the line was successfully handled, false to fall back to full JSON parse.
    /// </summary>
    private static bool TryProcessPriceChangeFast(
        string line, int jsonOffset,
        long tickTimestampMs,
        Dictionary<string, LocalOrderBook> orderBooks,
        Dictionary<string, List<ILiveStrategy>> activeStrategies,
        List<StrategyConfig> strategyConfigs,
        Dictionary<string, string> tokenNames,
        Dictionary<string, ReplayBroker> brokers,
        ref long totalPriceChanges,
        ref bool hasDeferredOrders)
    {
        // Look for "price_changes" key — if not present, bail to full parser
        int pcIdx = line.IndexOf("\"price_changes\"", jsonOffset, StringComparison.Ordinal);
        if (pcIdx < 0) return false;

        // Parse each entry in the price_changes array by scanning for key markers
        // Format: {"asset_id":"...","price":"0.50","size":"100","side":"BUY"}
        int searchFrom = pcIdx;
        while (true)
        {
            // Find next asset_id
            int aidKey = line.IndexOf("\"asset_id\":\"", searchFrom, StringComparison.Ordinal);
            if (aidKey < 0) break;

            int aidStart = aidKey + 12; // length of "asset_id":"
            int aidEnd = line.IndexOf('"', aidStart);
            if (aidEnd < 0) break;

            string assetId = line.Substring(aidStart, aidEnd - aidStart);
            searchFrom = aidEnd + 1;

            // Find price (search from current position — fields are in order)
            string? priceStr = ExtractQuotedValue(line, "\"price\":\"", ref searchFrom);
            if (priceStr == null) break;

            string? sizeStr = ExtractQuotedValue(line, "\"size\":\"", ref searchFrom);
            if (sizeStr == null) break;

            string? sideStr = ExtractQuotedValue(line, "\"side\":\"", ref searchFrom);
            if (sideStr == null) break;

            // Process this price change
            EnsureAssetInitialized(assetId, orderBooks, activeStrategies, strategyConfigs, tokenNames);

            if (!decimal.TryParse(priceStr, NumberStyles.Number, CultureInfo.InvariantCulture, out decimal price)) continue;
            if (!decimal.TryParse(sizeStr, NumberStyles.Number, CultureInfo.InvariantCulture, out decimal size)) continue;

            orderBooks[assetId].UpdatePriceLevel(sideStr, price, size);
            totalPriceChanges++;

            foreach (var broker in brokers.Values)
                broker.ResetConsumedLiquidity(assetId);

            var book = orderBooks[assetId];
            var strategies = activeStrategies[assetId];
            foreach (var strategy in strategies)
            {
                brokers[strategy.StrategyName].ReplayTimeMs = tickTimestampMs;
                strategy.OnBookUpdate(book, brokers[strategy.StrategyName]);
            }

            if (!hasDeferredOrders)
            {
                foreach (var broker in brokers.Values)
                {
                    if (broker.HasDeferredOrders) { hasDeferredOrders = true; break; }
                }
            }
        }

        return true;
    }

    /// <summary>Extract the quoted value after a key marker like "price":". Advances searchFrom past the closing quote.</summary>
    private static string? ExtractQuotedValue(string line, string keyMarker, ref int searchFrom)
    {
        int keyIdx = line.IndexOf(keyMarker, searchFrom, StringComparison.Ordinal);
        if (keyIdx < 0) return null;
        int valStart = keyIdx + keyMarker.Length;
        int valEnd = line.IndexOf('"', valStart);
        if (valEnd < 0) return null;
        searchFrom = valEnd + 1;
        return line.Substring(valStart, valEnd - valStart);
    }

    private static void ProcessPriceChanges(
        JsonElement changesEl,
        long tickTimestampMs,
        Dictionary<string, LocalOrderBook> orderBooks,
        Dictionary<string, List<ILiveStrategy>> activeStrategies,
        List<StrategyConfig> strategyConfigs,
        Dictionary<string, string> tokenNames,
        Dictionary<string, ReplayBroker> brokers,
        ref long totalPriceChanges,
        ref bool hasDeferredOrders)
    {
        foreach (var change in changesEl.EnumerateArray())
        {
            if (!change.TryGetProperty("asset_id", out var idEl)) continue;
            string? assetId = idEl.GetString();
            if (string.IsNullOrEmpty(assetId)) continue;

            // Initialize asset on first price_change (not just book snapshots)
            EnsureAssetInitialized(assetId, orderBooks, activeStrategies, strategyConfigs, tokenNames);

            decimal price = decimal.Parse(change.GetProperty("price").GetString() ?? "0", CultureInfo.InvariantCulture);
            decimal size = decimal.Parse(change.GetProperty("size").GetString() ?? "0", CultureInfo.InvariantCulture);
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
                brokers[strategy.StrategyName].ReplayTimeMs = tickTimestampMs;
                strategy.OnBookUpdate(book, brokers[strategy.StrategyName]);
            }

            // Check if any orders were queued
            if (!hasDeferredOrders)
            {
                foreach (var broker in brokers.Values)
                {
                    if (broker.HasDeferredOrders) { hasDeferredOrders = true; break; }
                }
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

        tokenNames.TryAdd(assetId, assetId[..Math.Min(12, assetId.Length)] + "...");
    }

    /// <summary>
    /// Fast binary replay path — reads preprocessed .bin + .idx files.
    /// No JSON parsing, no decompression. Just reads 19-byte records and feeds to strategies.
    /// </summary>
    private static void RunBinary(string binPath, string idxPath, List<StrategyConfig> strategyConfigs)
    {
        Console.Clear();
        Console.WriteLine("=========================================");
        Console.WriteLine("  BINARY REPLAY — PREPROCESSED DATA      ");
        Console.WriteLine($"  Latency: {REPLAY_LATENCY_MS}ms        ");
        Console.WriteLine("=========================================");

        var tokenNames = new Dictionary<string, string>();
        var tokenMinSizes = new Dictionary<string, decimal>();
        const decimal MAX_BET_SIZE = 1000.00m;

        var brokers = new Dictionary<string, ReplayBroker>();
        foreach (var config in strategyConfigs)
        {
            var broker = new ReplayBroker(config.Name, config.StartingCapital, tokenNames, tokenMinSizes, MAX_BET_SIZE, REPLAY_LATENCY_MS);
            broker.IsMuted = true;
            brokers[config.Name] = broker;
        }

        var orderBooks = new Dictionary<string, LocalOrderBook>();
        var activeStrategies = new Dictionary<string, List<ILiveStrategy>>();
        bool hasDeferredOrders = false;

        int maxNameLength = strategyConfigs.Max(c => c.Name.Length);

        using var reader = new BinaryReplayReader(binPath, idxPath);
        long totalRecords = reader.TotalRecords;
        long recordsProcessed = 0;
        long totalPriceChanges = 0;
        var overallSw = Stopwatch.StartNew();
        long lastReportRecord = 0;

        Console.WriteLine($"  {totalRecords:N0} records in binary file");
        Console.WriteLine();

        while (reader.TryReadTick(out long tickTimestampMs, out string assetId, out decimal price, out decimal size, out string side))
        {
            recordsProcessed++;

            EnsureAssetInitialized(assetId, orderBooks, activeStrategies, strategyConfigs, tokenNames);

            // Drain deferred orders
            if (hasDeferredOrders)
            {
                hasDeferredOrders = false;
                foreach (var broker in brokers.Values)
                {
                    broker.ReplayTimeMs = tickTimestampMs;
                    broker.DrainDeferredOrders(orderBooks);
                    if (broker.HasDeferredOrders) hasDeferredOrders = true;
                }
            }

            // Update order book
            orderBooks[assetId].UpdatePriceLevel(side, price, size);
            totalPriceChanges++;

            // Reset consumed liquidity
            foreach (var broker in brokers.Values)
                broker.ResetConsumedLiquidity(assetId);

            // Feed to strategies
            var book = orderBooks[assetId];
            var strategies = activeStrategies[assetId];
            foreach (var strategy in strategies)
            {
                brokers[strategy.StrategyName].ReplayTimeMs = tickTimestampMs;
                strategy.OnBookUpdate(book, brokers[strategy.StrategyName]);
            }

            if (!hasDeferredOrders)
            {
                foreach (var broker in brokers.Values)
                {
                    if (broker.HasDeferredOrders) { hasDeferredOrders = true; break; }
                }
            }

            // Progress bar every 500K records
            if (recordsProcessed - lastReportRecord >= 500_000)
            {
                lastReportRecord = recordsProcessed;
                double percent = (double)recordsProcessed / totalRecords * 100;
                double elapsedSec = overallSw.Elapsed.TotalSeconds;
                double recsPerSec = recordsProcessed / Math.Max(elapsedSec, 0.001);
                long remaining = totalRecords - recordsProcessed;
                TimeSpan eta = TimeSpan.FromSeconds(remaining / Math.Max(recsPerSec, 1));

                int barWidth = 30;
                int filled = (int)(percent / 100 * barWidth);
                string bar = new string('#', filled) + new string('-', barWidth - filled);
                Console.Write($"\r  [{bar}] {percent:0.1}% | {recordsProcessed:N0}/{totalRecords:N0} | {recsPerSec:N0}/sec | ETA: {eta:hh\\:mm\\:ss}   ");
            }
        }

        // Final drain
        foreach (var broker in brokers.Values)
            broker.DrainDeferredOrders(orderBooks);

        overallSw.Stop();

        Console.WriteLine($"\n\n=========================================");
        Console.WriteLine("  BINARY REPLAY COMPLETE");
        Console.WriteLine("=========================================");
        Console.WriteLine($"  Records processed:  {recordsProcessed:N0}");
        Console.WriteLine($"  Price changes:      {totalPriceChanges:N0}");
        Console.WriteLine($"  Unique assets:      {orderBooks.Count:N0}");
        Console.WriteLine($"  Elapsed:            {overallSw.Elapsed.TotalSeconds:0.1}s");
        Console.WriteLine($"  Throughput:         {recordsProcessed / Math.Max(overallSw.Elapsed.TotalSeconds, 0.001):N0} records/sec");
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
}
