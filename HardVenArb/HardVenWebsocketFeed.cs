using System.Globalization;
using System.Net.Http;
using System.Text.Json;

namespace HardVenArb;

/// <summary>
/// HardVen book feed — polls the sidecar "private API" (<c>GET /odds</c>) over HTTP and pushes each
/// sportsbook selection's current price into its <c>"H:{id}"</c> <see cref="LocalOrderBook"/> as a single
/// ask level: <b>price = 1/decimalOdds</b> (the per-$1-contract cost), <b>size = max_contracts</b>. This
/// replaces the Polymarket WebSocket feed — a scraped sportsbook is polled, not streamed, and pre-match
/// cadence (seconds) is fine. The feed is book-agnostic: it only knows the sidecar contract, never the
/// specific sportsbook (that lives behind the sidecar's BookAdapter).
///
/// Class name kept for drop-in compatibility with Program.cs wiring; it is an HTTP poller, not a WS feed.
/// </summary>
public class HardVenWebsocketFeed
{
    private readonly string  _sidecarBase;       // e.g. http://127.0.0.1:8787
    private readonly List<string> _tokens;       // sportsbook selection ids to poll
    private readonly object  _tokensLock = new();
    private readonly MarketStateTracker _state;
    private readonly CrossPlatformArbTelemetryStrategy _telemetry;
    private readonly int     _maxPerRequest;     // max selection ids per /odds call
    private readonly int     _pollIntervalMs;
    private readonly HttpClient _http = new() { Timeout = TimeSpan.FromSeconds(15) };

    /// <summary>True while the last sidecar poll succeeded.</summary>
    public volatile bool IsConnected = false;

    private long _lastMessageTicks = DateTime.UtcNow.Ticks;
    /// <summary>UTC time of the last successful sidecar response (watchdog staleness check).</summary>
    public DateTime LastMessageAt => new DateTime(Volatile.Read(ref _lastMessageTicks), DateTimeKind.Utc);

    public HardVenWebsocketFeed(
        string sidecarBaseUrl,
        List<string> tokens,
        MarketStateTracker state,
        CrossPlatformArbTelemetryStrategy telemetry,
        int maxPerRequest,
        int pollIntervalMs)
    {
        _sidecarBase    = (sidecarBaseUrl ?? "").TrimEnd('/');
        _tokens         = tokens;
        _state          = state;
        _telemetry      = telemetry;
        _maxPerRequest  = maxPerRequest  > 0 ? maxPerRequest  : 200;
        _pollIntervalMs = pollIntervalMs > 0 ? pollIntervalMs : 9_000;
    }

    /// <summary>Adds selection ids to poll (hot-reload). Safe from any thread.</summary>
    public void EnqueueSubscribe(IEnumerable<string> tokens)
    {
        lock (_tokensLock)
            foreach (var t in tokens)
                if (!_tokens.Contains(t)) _tokens.Add(t);
    }

    public async Task RunAsync(CancellationToken ct)
    {
        if (string.IsNullOrWhiteSpace(_sidecarBase))
        {
            Console.WriteLine("[HARDVEN] No sidecar URL (HARDVEN_SIDECAR_URL) — HardVen feed disabled.");
            return;
        }
        Console.WriteLine($"[HARDVEN] Polling sidecar {_sidecarBase}/odds every {_pollIntervalMs} ms");

        while (!ct.IsCancellationRequested)
        {
            try
            {
                List<string> snapshot;
                lock (_tokensLock) snapshot = _tokens.ToList();

                if (snapshot.Count == 0)
                {
                    // Nothing paired yet — health-check so the status dashboard shows the sidecar is up.
                    using var resp = await _http.GetAsync($"{_sidecarBase}/health", ct);
                    IsConnected = resp.IsSuccessStatusCode;
                    if (IsConnected) Volatile.Write(ref _lastMessageTicks, DateTime.UtcNow.Ticks);
                }
                else
                {
                    bool anyOk = false;
                    for (int i = 0; i < snapshot.Count; i += _maxPerRequest)
                    {
                        var chunk = snapshot.Skip(i).Take(_maxPerRequest);
                        string q  = Uri.EscapeDataString(string.Join(",", chunk));
                        using var resp = await _http.GetAsync($"{_sidecarBase}/odds?selections={q}", ct);
                        if (!resp.IsSuccessStatusCode) continue;
                        ApplyOdds(await resp.Content.ReadAsStringAsync(ct));
                        anyOk = true;
                    }
                    IsConnected = anyOk;
                    if (anyOk) Volatile.Write(ref _lastMessageTicks, DateTime.UtcNow.Ticks);
                }
            }
            catch (OperationCanceledException) { break; }
            catch (Exception ex)
            {
                IsConnected = false;
                DebugLog.Feed($"HardVenFeed poll error: {ex.GetType().Name}: {ex.Message}");
            }

            try { await Task.Delay(_pollIntervalMs, ct); }
            catch (OperationCanceledException) { break; }
        }
        IsConnected = false;
    }

    private void ApplyOdds(string json)
    {
        using var doc = JsonDocument.Parse(json);
        if (!doc.RootElement.TryGetProperty("selections", out var sels) ||
            sels.ValueKind != JsonValueKind.Object)
            return;

        foreach (var prop in sels.EnumerateObject())
        {
            string bookKey = $"H:{prop.Name}";
            if (!_state.Books.TryGetValue(bookKey, out var book)) continue;   // not a subscribed token

            var s        = prop.Value;
            string status = s.TryGetProperty("status", out var st) ? (st.GetString() ?? "open") : "open";
            decimal odds  = s.TryGetProperty("decimal_odds", out var od) && od.TryGetDecimal(out var o) ? o : 0m;
            decimal size  = s.TryGetProperty("max_contracts", out var mc) && mc.TryGetDecimal(out var c) ? c : 0m;
            decimal price = odds > 0m ? Math.Round(1m / odds, 6) : 0m;   // per-$1-contract cost = 1/odds

            // A live, valid price → one ask level. Anything else (suspended/missing/garbage) → empty book
            // so GetBestAskPrice() returns 1.00 and no arb can fire on it. ProcessBookUpdate clears stale
            // levels, so a moving sportsbook price never leaves a phantom ask. Price/size are strings.
            string asks = (status == "open" && price > 0m && price < 1m && size > 0m)
                ? $"[{{\"price\":\"{price.ToString(CultureInfo.InvariantCulture)}\"," +
                  $"\"size\":\"{Math.Round(size, 2).ToString(CultureInfo.InvariantCulture)}\"}}]"
                : "[]";
            using var sd = JsonDocument.Parse($"{{\"bids\":[],\"asks\":{asks}}}");
            book.ProcessBookUpdate(sd.RootElement.GetProperty("bids"), sd.RootElement.GetProperty("asks"));
            book.MarkDeltaReceived();
            _telemetry.OnBookUpdate(bookKey);
        }
    }
}
