using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Globalization;
using System.IO;
using System.IO.Compression;
using System.Linq;
using System.Text.Json;

namespace PredictionLiveTrader;

/// <summary>
/// Preprocesses raw .gz WebSocket replay files into a compact binary format.
/// Extracts only the fields needed by the strategy (timestamp, asset_id, price, size, side).
/// Resumable via checkpoint in the .idx file. Output is append-only and readable while running.
///
/// Binary record format (19 bytes):
///   int64  timestamp   (8 bytes, LE) — Unix ms
///   uint16 assetIndex  (2 bytes, LE) — index into asset table
///   int32  price       (4 bytes, LE) — price * 10000
///   int64  size        (8 bytes, LE) — size * 100
///   byte   side        (1 byte)      — 0=BUY, 1=SELL
/// </summary>
static class ReplayPreprocessor
{
    private const int RECORD_SIZE = 23;
    private const int FLUSH_INTERVAL = 50_000;

    public static void Run(string inputDir)
    {
        Console.WriteLine("=========================================");
        Console.WriteLine("  REPLAY PREPROCESSOR — .GZ → .BIN");
        Console.WriteLine("=========================================");

        string binPath = Path.Combine(inputDir, "replay.bin");
        string idxPath = Path.Combine(inputDir, "replay.idx");

        // Load existing index + checkpoint
        var assetMap = new Dictionary<string, ushort>();
        string? checkpointFile = null;
        long checkpointLine = 0;

        if (File.Exists(idxPath))
        {
            try
            {
                var idx = JsonSerializer.Deserialize<IndexFile>(File.ReadAllText(idxPath));
                if (idx?.Assets != null)
                {
                    foreach (var kvp in idx.Assets)
                        assetMap[kvp.Key] = kvp.Value;
                }
                if (idx?.Checkpoint != null)
                {
                    checkpointFile = idx.Checkpoint.File;
                    checkpointLine = idx.Checkpoint.Line;
                    Console.WriteLine($"  Resuming from: {checkpointFile} line {checkpointLine:N0}");
                    Console.WriteLine($"  {assetMap.Count} assets in index, {new FileInfo(binPath).Length / RECORD_SIZE:N0} records in bin");
                }
            }
            catch { /* Corrupt index — start fresh */ }
        }

        // Discover .gz files
        var gzFiles = Directory.GetFiles(inputDir, "*.gz").OrderBy(f => f).ToArray();
        if (gzFiles.Length == 0)
        {
            Console.WriteLine($"No .gz files found in '{inputDir}'!");
            return;
        }

        Console.WriteLine($"  Found {gzFiles.Length} data file(s)");

        long totalCompressedBytes = gzFiles.Sum(f => new FileInfo(f).Length);
        long bytesProcessedSoFar = 0;
        long totalRecords = 0;
        var overallSw = Stopwatch.StartNew();

        // Graceful shutdown
        bool shutdownRequested = false;
        Console.CancelKeyPress += (_, e) =>
        {
            e.Cancel = true;
            shutdownRequested = true;
            Console.WriteLine("\n  Shutdown requested — flushing and saving checkpoint...");
        };

        // Open output (append mode)
        using var outStream = new FileStream(binPath, FileMode.OpenOrCreate, FileAccess.Write, FileShare.Read, bufferSize: 64 * 1024);
        outStream.Seek(0, SeekOrigin.End);

        var buffer = new byte[RECORD_SIZE];
        int recordsSinceFlush = 0;
        string currentFile = "";
        long currentLine = 0;

        foreach (var gzFile in gzFiles)
        {
            string fileName = Path.GetFileName(gzFile);
            long fileSize = new FileInfo(gzFile).Length;

            // Skip files before checkpoint
            if (checkpointFile != null)
            {
                int cmp = string.Compare(fileName, checkpointFile, StringComparison.Ordinal);
                if (cmp < 0)
                {
                    bytesProcessedSoFar += fileSize;
                    continue;
                }
            }

            currentFile = fileName;
            currentLine = 0;
            long lastReportBytes = 0;

            Console.WriteLine($"\n  Processing {fileName} ({fileSize / (1024.0 * 1024 * 1024):0.1} GB)");

            using var fileStream = new FileStream(gzFile, FileMode.Open, FileAccess.Read, FileShare.Read, bufferSize: 1024 * 1024);
            using var gzStream = new GZipStream(fileStream, CompressionMode.Decompress);
            using var reader = new StreamReader(gzStream, bufferSize: 1024 * 256);

            string? line;
            while ((line = reader.ReadLine()) != null)
            {
                currentLine++;

                // Skip lines before checkpoint
                if (checkpointFile != null && fileName == checkpointFile && currentLine <= checkpointLine)
                    continue;

                if (shutdownRequested) break;

                int pipeIndex = line.IndexOf('|');
                if (pipeIndex < 0) continue;

                if (!long.TryParse(line.AsSpan(0, pipeIndex), out long timestampMs)) continue;

                int jsonStart = pipeIndex + 1;
                if (jsonStart >= line.Length) continue;

                char firstChar = line[jsonStart];
                while (firstChar == ' ' && jsonStart < line.Length - 1) firstChar = line[++jsonStart];

                if (firstChar == '[')
                {
                    // Book snapshot — explode into individual price-level records
                    try
                    {
                        using var doc = JsonDocument.Parse(line.AsMemory(pipeIndex + 1));
                        foreach (var entry in doc.RootElement.EnumerateArray())
                        {
                            if (!entry.TryGetProperty("asset_id", out var idEl)) continue;
                            string? assetId = idEl.GetString();
                            if (string.IsNullOrEmpty(assetId)) continue;

                            ushort assetIdx = GetOrAddAsset(assetMap, assetId);

                            if (entry.TryGetProperty("bids", out var bidsEl))
                            {
                                foreach (var level in bidsEl.EnumerateArray())
                                {
                                    if (TryParsePriceLevel(level, out decimal price, out decimal size))
                                    {
                                        WriteRecord(buffer, outStream, timestampMs, assetIdx, price, size, 0); // BUY
                                        totalRecords++;
                                        recordsSinceFlush++;
                                    }
                                }
                            }
                            if (entry.TryGetProperty("asks", out var asksEl))
                            {
                                foreach (var level in asksEl.EnumerateArray())
                                {
                                    if (TryParsePriceLevel(level, out decimal price, out decimal size))
                                    {
                                        WriteRecord(buffer, outStream, timestampMs, assetIdx, price, size, 1); // SELL
                                        totalRecords++;
                                        recordsSinceFlush++;
                                    }
                                }
                            }
                        }
                    }
                    catch (JsonException) { }
                }
                else if (TryProcessPriceChangesFast(line, pipeIndex + 1, timestampMs, assetMap, buffer, outStream, ref totalRecords, ref recordsSinceFlush))
                {
                    // Handled by fast path
                }
                else
                {
                    // Fallback: full JSON parse for book events and unknown formats
                    try
                    {
                        using var doc = JsonDocument.Parse(line.AsMemory(pipeIndex + 1));
                        var root = doc.RootElement;

                        if (root.ValueKind == JsonValueKind.Object)
                        {
                            string? eventType = root.TryGetProperty("event_type", out var etEl) ? etEl.GetString() : null;

                            if (eventType == "book" && root.TryGetProperty("asset_id", out var aidEl))
                            {
                                string? assetId = aidEl.GetString();
                                if (!string.IsNullOrEmpty(assetId))
                                {
                                    ushort assetIdx = GetOrAddAsset(assetMap, assetId);
                                    ExplodeBookLevels(root, timestampMs, assetIdx, buffer, outStream, ref totalRecords, ref recordsSinceFlush);
                                }
                            }
                            else if (root.TryGetProperty("price_changes", out var changesEl))
                            {
                                ProcessPriceChangesJson(changesEl, timestampMs, assetMap, buffer, outStream, ref totalRecords, ref recordsSinceFlush);
                            }
                        }
                    }
                    catch (JsonException) { }
                }

                // Periodic flush
                if (recordsSinceFlush >= FLUSH_INTERVAL)
                {
                    outStream.Flush();
                    SaveIndex(idxPath, assetMap, currentFile, currentLine);
                    recordsSinceFlush = 0;
                }

                // Progress bar
                long currentPos = fileStream.Position;
                if (currentPos - lastReportBytes >= 2 * 1024 * 1024)
                {
                    lastReportBytes = currentPos;
                    double overallPercent = (double)(bytesProcessedSoFar + currentPos) / totalCompressedBytes * 100;
                    double elapsedSec = overallSw.Elapsed.TotalSeconds;
                    double bytesPerSec = (bytesProcessedSoFar + currentPos) / Math.Max(elapsedSec, 0.001);
                    long bytesRemaining = totalCompressedBytes - bytesProcessedSoFar - currentPos;
                    TimeSpan eta = TimeSpan.FromSeconds(bytesRemaining / Math.Max(bytesPerSec, 1));

                    int barWidth = 30;
                    int filled = (int)(overallPercent / 100 * barWidth);
                    string bar = new string('#', filled) + new string('-', barWidth - filled);
                    Console.Write($"\r  [{bar}] {overallPercent:0.1}% | {totalRecords:N0} records | ETA: {eta:hh\\:mm\\:ss}   ");
                }
            }

            if (shutdownRequested) break;

            // Clear checkpoint file restriction after fully processing the checkpoint file
            if (checkpointFile != null && fileName == checkpointFile)
                checkpointFile = null;

            bytesProcessedSoFar += fileSize;
        }

        // Final flush + save
        outStream.Flush();
        SaveIndex(idxPath, assetMap, currentFile, currentLine);

        overallSw.Stop();
        long binSize = new FileInfo(binPath).Length;
        Console.WriteLine($"\n\n  Done! {totalRecords:N0} records in {overallSw.Elapsed.TotalSeconds:0.1}s");
        Console.WriteLine($"  Output: {binSize / (1024.0 * 1024):0.1} MB ({binSize / RECORD_SIZE:N0} records)");
        Console.WriteLine($"  Assets: {assetMap.Count}");
        if (shutdownRequested)
            Console.WriteLine($"  Checkpoint saved — run again to resume.");
    }

    private static bool TryProcessPriceChangesFast(
        string line, int jsonOffset, long timestampMs,
        Dictionary<string, ushort> assetMap, byte[] buffer, FileStream outStream,
        ref long totalRecords, ref int recordsSinceFlush)
    {
        int pcIdx = line.IndexOf("\"price_changes\"", jsonOffset, StringComparison.Ordinal);
        if (pcIdx < 0) return false;

        int searchFrom = pcIdx;
        while (true)
        {
            int aidKey = line.IndexOf("\"asset_id\":\"", searchFrom, StringComparison.Ordinal);
            if (aidKey < 0) break;

            int aidStart = aidKey + 12;
            int aidEnd = line.IndexOf('"', aidStart);
            if (aidEnd < 0) break;

            string assetId = line.Substring(aidStart, aidEnd - aidStart);
            searchFrom = aidEnd + 1;

            string? priceStr = ExtractQuotedValue(line, "\"price\":\"", ref searchFrom);
            if (priceStr == null) break;

            string? sizeStr = ExtractQuotedValue(line, "\"size\":\"", ref searchFrom);
            if (sizeStr == null) break;

            string? sideStr = ExtractQuotedValue(line, "\"side\":\"", ref searchFrom);
            if (sideStr == null) break;

            if (!decimal.TryParse(priceStr, NumberStyles.Number, CultureInfo.InvariantCulture, out decimal price)) continue;
            if (!decimal.TryParse(sizeStr, NumberStyles.Number, CultureInfo.InvariantCulture, out decimal size)) continue;

            ushort assetIdx = GetOrAddAsset(assetMap, assetId);
            byte side = sideStr == "SELL" ? (byte)1 : (byte)0;

            WriteRecord(buffer, outStream, timestampMs, assetIdx, price, size, side);
            totalRecords++;
            recordsSinceFlush++;
        }

        return true;
    }

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

    private static void ProcessPriceChangesJson(
        JsonElement changesEl, long timestampMs,
        Dictionary<string, ushort> assetMap, byte[] buffer, FileStream outStream,
        ref long totalRecords, ref int recordsSinceFlush)
    {
        foreach (var change in changesEl.EnumerateArray())
        {
            if (!change.TryGetProperty("asset_id", out var idEl)) continue;
            string? assetId = idEl.GetString();
            if (string.IsNullOrEmpty(assetId)) continue;

            string priceStr = change.GetProperty("price").GetString() ?? "0";
            string sizeStr = change.GetProperty("size").GetString() ?? "0";
            string sideStr = change.GetProperty("side").GetString() ?? "BUY";

            if (!decimal.TryParse(priceStr, NumberStyles.Number, CultureInfo.InvariantCulture, out decimal price)) continue;
            if (!decimal.TryParse(sizeStr, NumberStyles.Number, CultureInfo.InvariantCulture, out decimal size)) continue;

            ushort assetIdx = GetOrAddAsset(assetMap, assetId);
            byte side = sideStr == "SELL" ? (byte)1 : (byte)0;

            WriteRecord(buffer, outStream, timestampMs, assetIdx, price, size, side);
            totalRecords++;
            recordsSinceFlush++;
        }
    }

    private static void ExplodeBookLevels(
        JsonElement root, long timestampMs, ushort assetIdx,
        byte[] buffer, FileStream outStream,
        ref long totalRecords, ref int recordsSinceFlush)
    {
        if (root.TryGetProperty("bids", out var bidsEl))
        {
            foreach (var level in bidsEl.EnumerateArray())
            {
                if (TryParsePriceLevel(level, out decimal price, out decimal size))
                {
                    WriteRecord(buffer, outStream, timestampMs, assetIdx, price, size, 0);
                    totalRecords++;
                    recordsSinceFlush++;
                }
            }
        }
        if (root.TryGetProperty("asks", out var asksEl))
        {
            foreach (var level in asksEl.EnumerateArray())
            {
                if (TryParsePriceLevel(level, out decimal price, out decimal size))
                {
                    WriteRecord(buffer, outStream, timestampMs, assetIdx, price, size, 1);
                    totalRecords++;
                    recordsSinceFlush++;
                }
            }
        }
    }

    private static bool TryParsePriceLevel(JsonElement level, out decimal price, out decimal size)
    {
        price = 0;
        size = 0;

        string? priceStr = level.TryGetProperty("price", out var pEl) ? pEl.GetString() : null;
        string? sizeStr = level.TryGetProperty("size", out var sEl) ? sEl.GetString() : null;

        if (priceStr == null || sizeStr == null) return false;

        return decimal.TryParse(priceStr, NumberStyles.Number, CultureInfo.InvariantCulture, out price)
            && decimal.TryParse(sizeStr, NumberStyles.Number, CultureInfo.InvariantCulture, out size);
    }

    private static void WriteRecord(byte[] buf, FileStream stream, long timestamp, ushort assetIdx, decimal price, decimal size, byte side)
    {
        BitConverter.TryWriteBytes(buf.AsSpan(0, 8), timestamp);
        BitConverter.TryWriteBytes(buf.AsSpan(8, 2), assetIdx);
        BitConverter.TryWriteBytes(buf.AsSpan(10, 4), (int)(price * 10000m));
        BitConverter.TryWriteBytes(buf.AsSpan(14, 8), (long)(size * 100m));
        buf[22] = side;
        stream.Write(buf, 0, RECORD_SIZE);
    }

    private static ushort GetOrAddAsset(Dictionary<string, ushort> map, string assetId)
    {
        if (map.TryGetValue(assetId, out ushort idx))
            return idx;
        idx = (ushort)map.Count;
        map[assetId] = idx;
        return idx;
    }

    private static void SaveIndex(string idxPath, Dictionary<string, ushort> assetMap, string currentFile, long currentLine)
    {
        var idx = new IndexFile
        {
            Assets = assetMap,
            Checkpoint = new CheckpointInfo { File = currentFile, Line = currentLine }
        };
        string json = JsonSerializer.Serialize(idx, new JsonSerializerOptions { WriteIndented = true });
        File.WriteAllText(idxPath, json);
    }

    // JSON serialization classes
    private class IndexFile
    {
        public Dictionary<string, ushort> Assets { get; set; } = new();
        public CheckpointInfo? Checkpoint { get; set; }
    }

    private class CheckpointInfo
    {
        public string File { get; set; } = "";
        public long Line { get; set; }
    }
}
