using System;
using System.Collections.Generic;
using System.IO;
using System.Text.Json;

namespace PredictionLiveTrader;

/// <summary>
/// Reads the compact binary replay format produced by ReplayPreprocessor.
/// Opens the .bin file with FileShare.ReadWrite so it can read while the preprocessor is still writing.
///
/// Record format (19 bytes):
///   int64  timestamp   (8 bytes, LE)
///   uint16 assetIndex  (2 bytes, LE)
///   int32  price       (4 bytes, LE) — price * 10000
///   int64  size        (8 bytes, LE) — size * 100
///   byte   side        (1 byte)      — 0=BUY, 1=SELL
/// </summary>
public class BinaryReplayReader : IDisposable
{
    private const int RECORD_SIZE = 23;

    private readonly FileStream _stream;
    private readonly Dictionary<ushort, string> _indexToAsset;
    private readonly byte[] _buf = new byte[RECORD_SIZE];

    public long TotalRecords { get; }
    public long Position => _stream.Position / RECORD_SIZE;

    public BinaryReplayReader(string binPath, string idxPath)
    {
        // Read index file for asset ID mapping
        _indexToAsset = new Dictionary<ushort, string>();

        string idxJson = File.ReadAllText(idxPath);
        using var doc = JsonDocument.Parse(idxJson);
        var root = doc.RootElement;

        if (root.TryGetProperty("Assets", out var assetsEl))
        {
            foreach (var prop in assetsEl.EnumerateObject())
            {
                ushort idx = prop.Value.GetUInt16();
                _indexToAsset[idx] = prop.Name;
            }
        }

        // Open with read + shared write so preprocessor can keep writing
        _stream = new FileStream(binPath, FileMode.Open, FileAccess.Read, FileShare.ReadWrite, bufferSize: 64 * 1024);
        TotalRecords = _stream.Length / RECORD_SIZE;
    }

    public bool TryReadTick(out long timestampMs, out string assetId, out decimal price, out decimal size, out string side)
    {
        timestampMs = 0;
        assetId = "";
        price = 0;
        size = 0;
        side = "";

        int bytesRead = _stream.Read(_buf, 0, RECORD_SIZE);
        if (bytesRead < RECORD_SIZE)
        {
            // Partial record or EOF — seek back if partial
            if (bytesRead > 0)
                _stream.Seek(-bytesRead, SeekOrigin.Current);
            return false;
        }

        timestampMs = BitConverter.ToInt64(_buf, 0);
        ushort assetIdx = BitConverter.ToUInt16(_buf, 8);
        int priceRaw = BitConverter.ToInt32(_buf, 10);
        long sizeRaw = BitConverter.ToInt64(_buf, 14);
        byte sideByte = _buf[22];

        if (!_indexToAsset.TryGetValue(assetIdx, out string? resolvedAsset))
        {
            assetId = $"unknown_{assetIdx}";
        }
        else
        {
            assetId = resolvedAsset;
        }

        price = priceRaw / 10000m;
        size = sizeRaw / 100m;
        side = sideByte switch
        {
            1 => "SELL",
            2 => "SNAPSHOT_CLEAR",
            _ => "BUY"
        };

        return true;
    }

    public void Dispose()
    {
        _stream.Dispose();
    }
}
