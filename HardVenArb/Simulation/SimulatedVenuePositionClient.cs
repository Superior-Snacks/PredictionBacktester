using System.Collections.Concurrent;
using PredictionBacktester.Engine.LiveExecution;

namespace HardVenArb;

/// <summary>
/// Wraps an IKalshiOrderExecutor and allows injecting position offsets at runtime.
/// Call InjectMismatch() to make GetPositionsAsync() return data that differs from
/// locally-tracked fills. Offsets are fire-once: cleared after the first call that applies them.
///
/// Usage:
///   venueClient.InjectMismatch("KXFOO-24NOV05-T50", +1);
///   // next ReconcileTradeAsync sees 1 extra contract at venue → mismatch → halt
/// </summary>
public class SimulatedVenuePositionClient : IKalshiOrderExecutor
{
    private readonly IKalshiOrderExecutor _inner;
    private readonly ConcurrentDictionary<string, int> _pendingOffsets = new();

    public SimulatedVenuePositionClient(IKalshiOrderExecutor inner) => _inner = inner;

    /// <summary>
    /// Schedules a fire-once position offset for <paramref name="ticker"/>.
    /// Positive offset adds phantom contracts; negative hides real ones.
    /// </summary>
    public void InjectMismatch(string ticker, int offset)
    {
        _pendingOffsets[ticker] = offset;
        Console.ForegroundColor = ConsoleColor.Magenta;
        Console.WriteLine($"[MISMATCH INJECT] Kalshi {ticker}: offset {offset:+#;-#;0} queued for next reconcile");
        Console.ResetColor();
    }

    public async Task<List<(string Ticker, int Position)>> GetPositionsAsync()
    {
        var positions = await _inner.GetPositionsAsync();
        if (_pendingOffsets.IsEmpty) return positions;

        var applied = new HashSet<string>(StringComparer.Ordinal);
        var result  = positions.Select(p =>
        {
            if (_pendingOffsets.TryGetValue(p.Ticker, out int off))
            {
                applied.Add(p.Ticker);
                return (p.Ticker, p.Position + off);
            }
            return p;
        }).ToList();

        // Add phantom positions for tickers not in real positions (e.g. offset on empty book)
        foreach (var kv in _pendingOffsets)
            if (!applied.Contains(kv.Key) && kv.Value != 0)
                result.Add((kv.Key, kv.Value));

        _pendingOffsets.Clear(); // fire-once
        return result;
    }

    public Task<(string OrderId, string Status, decimal FillCount)> PlaceOrderAsync(
        string ticker, string side, int priceCents, int count,
        string action = "buy", string? clientOrderId = null)
        => _inner.PlaceOrderAsync(ticker, side, priceCents, count, action, clientOrderId);

    public Task<(string Status, decimal FillCount)> PollOrderAsync(string orderId)
        => _inner.PollOrderAsync(orderId);

    public Task<long> GetBalanceCentsAsync() => _inner.GetBalanceCentsAsync();

    public Task<System.Text.Json.JsonDocument> GetMarketAsync(string ticker)
        => _inner.GetMarketAsync(ticker);
}
