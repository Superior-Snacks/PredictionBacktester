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

    // Freshness window: a sidecar quote is trusted only while its per-selection 'ts' (the last SUCCESSFUL
    // bookmaker fetch) is this recent. A healthy quote is re-fetched on every poll (≤ ~5s old); when the
    // bookmaker session dies the sidecar re-serves the frozen last quote with a frozen ts → it ages past
    // this and the book is cleared. Override with HARDVEN_QUOTE_MAX_AGE_MS (default 30000).
    private readonly double _quoteMaxAgeSec;
    private int _staleAccum;            // stale quotes seen during the in-progress poll
    private int _lastStaleCount = -1;   // last reported count, for transition logging
    /// <summary>Count of HardVen quotes whose sidecar timestamp is older than the freshness window (stale).</summary>
    public volatile int StaleQuoteCount;

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

        double maxAgeMs = double.TryParse(Environment.GetEnvironmentVariable("HARDVEN_QUOTE_MAX_AGE_MS"),
            NumberStyles.Any, CultureInfo.InvariantCulture, out var m) && m > 0 ? m : 30_000;
        _quoteMaxAgeSec = maxAgeMs / 1000.0;
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
                    _staleAccum = 0;
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
                    if (anyOk)
                    {
                        Volatile.Write(ref _lastMessageTicks, DateTime.UtcNow.Ticks);
                        ReportStaleness(snapshot.Count);
                    }
                }
            }
            catch (OperationCanceledException) when (ct.IsCancellationRequested) { break; }  // real shutdown
            catch (OperationCanceledException ex)
            {
                // HttpClient.Timeout (15s) throws TaskCanceledException : OperationCanceledException. That is
                // NOT a shutdown — a slow sidecar response (during a Cloudflare recovery / page reload / heavy
                // poll) must NOT exit the feed, because exiting cancels the WHOLE bot. Treat it as transient.
                IsConnected = false;
                DebugLog.Feed($"HardVenFeed poll timeout (sidecar slow, not a shutdown): {ex.Message}");
            }
            catch (Exception ex)
            {
                IsConnected = false;
                DebugLog.Feed($"HardVenFeed poll error: {ex.GetType().Name}: {ex.Message}");
            }

            try { await Task.Delay(_pollIntervalMs, ct); }
            catch (OperationCanceledException) when (ct.IsCancellationRequested) { break; }
        }
        IsConnected = false;
    }

    private void ApplyOdds(string json)
    {
        using var doc = JsonDocument.Parse(json);
        if (!doc.RootElement.TryGetProperty("selections", out var sels) ||
            sels.ValueKind != JsonValueKind.Object)
            return;

        double nowUnix = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds() / 1000.0;

        foreach (var prop in sels.EnumerateObject())
        {
            string bookKey = $"H:{prop.Name}";
            if (!_state.Books.TryGetValue(bookKey, out var book)) continue;   // not a subscribed token

            var s        = prop.Value;
            string status = s.TryGetProperty("status", out var st) ? (st.GetString() ?? "open") : "open";
            decimal odds  = s.TryGetProperty("decimal_odds", out var od) && od.TryGetDecimal(out var o) ? o : 0m;
            decimal size  = s.TryGetProperty("max_contracts", out var mc) && mc.TryGetDecimal(out var c) ? c : 0m;
            decimal price = odds > 0m ? Math.Round(1m / odds, 6) : 0m;   // per-$1-contract cost = 1/odds

            // FRESHNESS GATE: the sidecar serves the LAST-known quote (same status/price) when its bookmaker
            // fetch fails (dead Cloudflare/login session), with the per-selection 'ts' frozen at the last
            // SUCCESSFUL fetch. Without this gate the poller calls MarkDeltaReceived() every cycle, so the
            // book never looks stale and live-Kalshi-vs-frozen-HardVen logs as a fat PHANTOM arb. So trust a
            // quote ONLY while its ts is recent; else clear the book AND don't advance its staleness clock.
            double ts     = s.TryGetProperty("ts", out var tsEl) && tsEl.TryGetDouble(out var tv) ? tv : 0;
            double ageSec = ts > 0 ? nowUnix - ts : double.MaxValue;
            bool   fresh  = ageSec <= _quoteMaxAgeSec;
            if (!fresh) _staleAccum++;

            // A fresh, live, valid price → one ask level. Anything else (stale/suspended/missing/garbage) →
            // empty book so GetBestAskPrice() returns 1.00 and no arb can fire on it. ProcessBookUpdate clears
            // stale levels, so a moving sportsbook price never leaves a phantom ask. Price/size are strings.
            string asks = (fresh && status == "open" && price > 0m && price < 1m && size > 0m)
                ? $"[{{\"price\":\"{price.ToString(CultureInfo.InvariantCulture)}\"," +
                  $"\"size\":\"{Math.Round(size, 2).ToString(CultureInfo.InvariantCulture)}\"}}]"
                : "[]";
            using var sd = JsonDocument.Parse($"{{\"bids\":[],\"asks\":{asks}}}");
            book.ProcessBookUpdate(sd.RootElement.GetProperty("bids"), sd.RootElement.GetProperty("asks"));
            if (fresh) book.MarkDeltaReceived();   // only advance the staleness clock on a genuinely recent quote
            _telemetry.OnBookUpdate(bookKey);
        }
    }

    /// <summary>One-line console note when HardVen quotes flip stale↔fresh (session frozen / recovered).</summary>
    private void ReportStaleness(int polled)
    {
        int stale = _staleAccum;
        StaleQuoteCount = stale;
        if (stale == _lastStaleCount) return;
        if (stale > 0 && _lastStaleCount <= 0)
            Console.WriteLine($"[HARDVEN] WARNING: {stale}/{polled} HardVen quote(s) STALE (bookmaker session " +
                              "frozen / expired?) — those books cleared; no arb can fire on a stale quote. " +
                              "Refresh the bookmaker session.");
        else if (stale == 0 && _lastStaleCount > 0)
            Console.WriteLine($"[HARDVEN] HardVen quotes fresh again — all {polled} polled books live.");
        _lastStaleCount = stale;
    }
}
