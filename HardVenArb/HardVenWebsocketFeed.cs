namespace HardVenArb;

/// <summary>
/// STUB book feed for the HardVen betting-site venue — the open slot to fill in.
///
/// Keeps the exact public shape Program.cs and the connection watchdog depend on (ctor,
/// <see cref="IsConnected"/>, <see cref="LastMessageAt"/>, <see cref="EnqueueSubscribe"/>,
/// <see cref="RunAsync"/>), but does nothing yet: it logs once and idles. <see cref="IsConnected"/>
/// stays false, so the watchdog reports the HardVen side down and no <c>"H:"</c> books are populated
/// (hence no cross-arb window can open until this is implemented).
///
/// TODO: connect to the betting-site feed, subscribe to the pair markets, and populate the
/// "H:{marketId}" LocalOrderBooks via <c>_state</c> (mirror KalshiPolyCross/PolymarketWebsocketFeed),
/// calling <c>_telemetry.OnBookUpdate(bookKey)</c> on each live delta.
/// </summary>
public class HardVenWebsocketFeed
{
    private readonly string _wsUrl;
    private readonly List<string> _tokens;
    private readonly MarketStateTracker _state;
    private readonly CrossPlatformArbTelemetryStrategy _telemetry;
    private readonly int _batchSize;
    private readonly int _pingIntervalMs;

    /// <summary>True while subscribed and receiving. Stub: never connects, so always false.</summary>
    public volatile bool IsConnected = false;

    private long _lastMessageTicks = DateTime.UtcNow.Ticks;
    /// <summary>UTC timestamp of the last received message. Stub: only set at construction.</summary>
    public DateTime LastMessageAt => new DateTime(Volatile.Read(ref _lastMessageTicks), DateTimeKind.Utc);

    public HardVenWebsocketFeed(
        string wsUrl,
        List<string> tokens,
        MarketStateTracker state,
        CrossPlatformArbTelemetryStrategy telemetry,
        int batchSize,
        int pingIntervalMs)
    {
        _wsUrl          = wsUrl;
        _tokens         = tokens;
        _state          = state;
        _telemetry      = telemetry;
        _batchSize      = batchSize;
        _pingIntervalMs = pingIntervalMs;
    }

    /// <summary>Queues tokens to subscribe once the live feed exists. Stub: tracks them for later.</summary>
    public void EnqueueSubscribe(IEnumerable<string> tokens)
    {
        var list = tokens.Where(t => !_tokens.Contains(t)).ToList();
        if (list.Count > 0) _tokens.AddRange(list);
    }

    public async Task RunAsync(CancellationToken ct)
    {
        Console.WriteLine("[HARDVEN WS] STUB — not implemented; HardVen books will not populate. " +
                          "Implement HardVenWebsocketFeed to enable cross-arb detection.");
        // Idle until shutdown so the watchdog/orchestration loop keeps running.
        while (!ct.IsCancellationRequested)
        {
            try { await Task.Delay(1000, ct); }
            catch (OperationCanceledException) { break; }
        }
    }
}
