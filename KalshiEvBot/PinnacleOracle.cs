using System.Collections.Concurrent;
using System.Globalization;
using System.Text.Json;

namespace KalshiEvBot;

/// <summary>One Pinnacle selection as the sidecar last served it, plus when WE received it.</summary>
public sealed record OracleQuote(
    double DecimalOdds, double MaxContracts, string Status, bool Live,
    double VenueTsUnix, DateTime ReceivedUtc, bool WsVerified)
{
    public bool Open => Status == "open" && DecimalOdds > 1.0;
}

/// <summary>
/// The fair-value oracle: polls the EXISTING HardVenArb sidecar for Pinnacle prices via GET /odds.
///
/// <para><b>The sidecar is shared, never copied.</b> It owns the browser session and the Pinnacle login,
/// which is the most fragile thing in the system; a second copy means a second browser and a second login
/// on one account. This class is a thin HTTP reader against a process that is already running.</para>
///
/// <para><b>No traffic reaches Pinnacle from here.</b> The sidecar already holds the WS open and answers
/// from what it was pushed, so polling it is a localhost call. That is the whole reason this is a poll and
/// not a re-seed: the bot must add no request to the venue it is watching.</para>
/// </summary>
public sealed class PinnacleOracle
{
    private readonly string _base;
    private readonly HttpClient _http = new() { Timeout = TimeSpan.FromSeconds(15) };
    private readonly ConcurrentDictionary<string, OracleQuote> _quotes = new(StringComparer.Ordinal);
    private readonly List<string> _tokens;
    private readonly int _pollMs, _chunk;
    private readonly double _maxAgeSec;

    public volatile bool IsConnected;
    public volatile bool SessionReady = true;
    public volatile int  StaleCount;
    private bool _feedPolicy, _feedAlive = true;
    private int  _lastReady = -1;

    public PinnacleOracle(string sidecarBaseUrl, IEnumerable<string> tokens)
    {
        _base   = (sidecarBaseUrl ?? "").TrimEnd('/');
        _tokens = tokens.Distinct(StringComparer.Ordinal).ToList();
        _pollMs = EnvInt("EV_ORACLE_POLL_MS", 3000);
        _chunk  = EnvInt("EV_ORACLE_CHUNK", 200);
        _maxAgeSec = EnvInt("EV_ORACLE_MAX_AGE_MS", 30_000) / 1000.0;
    }

    private static int EnvInt(string k, int dflt)
        => int.TryParse(Environment.GetEnvironmentVariable(k), NumberStyles.Any,
                        CultureInfo.InvariantCulture, out var v) && v > 0 ? v : dflt;

    /// <summary>Raised after every successful poll. Pinnacle moving is a signal source in its own right:
    /// a value bet can appear with the Kalshi book completely still, and a bot that only woke on Kalshi
    /// ticks would never see those.</summary>
    public event Action? OnPolled;

    public OracleQuote? Get(string token) => _quotes.TryGetValue(token, out var q) ? q : null;

    public int QuoteCount => _quotes.Count;

    /// <summary>
    /// Is this quote recent enough to be a fair value? Two policies, because the same symptom means
    /// opposite things at different venues. Pinnacle serves the last-known price when its own fetch fails,
    /// with the timestamp frozen at the last success, so there an old ts really can mean "our session died"
    /// and per-quote age is the right gate. A push-only venue stamps ts only when the event actually moves,
    /// so there an old ts means a quiet market and the same gate would discard every pre-match price. When
    /// the sidecar declares the feed policy we follow the socket instead.
    /// </summary>
    public bool Fresh(OracleQuote q)
        => _feedPolicy ? (_feedAlive && q.VenueTsUnix > 0)
                       : (DateTimeOffset.UtcNow.ToUnixTimeMilliseconds() / 1000.0 - q.VenueTsUnix) <= _maxAgeSec;

    public double AgeMs(OracleQuote q)
        => q.VenueTsUnix <= 0 ? -1
         : Math.Round(DateTimeOffset.UtcNow.ToUnixTimeMilliseconds() - q.VenueTsUnix * 1000.0);

    public async Task RunAsync(CancellationToken ct)
    {
        if (string.IsNullOrWhiteSpace(_base))
        {
            Console.WriteLine("[ORACLE] No sidecar URL (HARDVEN_SIDECAR_URL) — cannot value anything.");
            return;
        }
        Console.WriteLine($"[ORACLE] Polling {_base}/odds for {_tokens.Count} selection(s) every {_pollMs} ms");

        while (!ct.IsCancellationRequested)
        {
            try
            {
                bool anyOk = false; int stale = 0;
                for (int i = 0; i < _tokens.Count; i += _chunk)
                {
                    string q = Uri.EscapeDataString(string.Join(",", _tokens.Skip(i).Take(_chunk)));
                    using var resp = await _http.GetAsync($"{_base}/odds?selections={q}", ct);
                    if (!resp.IsSuccessStatusCode) continue;
                    stale += Apply(await resp.Content.ReadAsStringAsync(ct));
                    anyOk = true;
                }
                IsConnected = anyOk;
                StaleCount  = stale;
                if (anyOk) { try { OnPolled?.Invoke(); } catch { /* a subscriber must not kill the poll */ } }
            }
            catch (OperationCanceledException) when (ct.IsCancellationRequested) { break; }
            catch (OperationCanceledException)
            {
                // HttpClient.Timeout also throws this. A slow sidecar is not a shutdown, and exiting here
                // would take the whole bot down with it.
                IsConnected = false;
            }
            catch (Exception ex)
            {
                IsConnected = false;
                Console.WriteLine($"[ORACLE] poll error: {ex.GetType().Name}: {ex.Message}");
            }

            try { await Task.Delay(_pollMs, ct); } catch (OperationCanceledException) { break; }
        }
        IsConnected = false;
    }

    /// <summary>Parses one /odds response into the quote cache. Returns how many quotes were stale.</summary>
    private int Apply(string json)
    {
        using var doc = JsonDocument.Parse(json);
        var root = doc.RootElement;

        if (root.TryGetProperty("session_ready", out var sr) &&
            (sr.ValueKind == JsonValueKind.True || sr.ValueKind == JsonValueKind.False))
        {
            SessionReady = sr.GetBoolean();
            int cur = SessionReady ? 1 : 0;
            if (cur != _lastReady)
            {
                Console.WriteLine(SessionReady
                    ? "[ORACLE] sidecar session READY — Pinnacle login captured; odds will flow."
                    : "[ORACLE] sidecar session DROPPED — no fair value until it re-logs in.");
                _lastReady = cur;
            }
        }

        if (root.TryGetProperty("feed", out var fe) && fe.ValueKind == JsonValueKind.Object)
        {
            _feedPolicy = fe.TryGetProperty("quote_age_policy", out var qp)
                       && string.Equals(qp.GetString(), "feed", StringComparison.OrdinalIgnoreCase);
            _feedAlive  = !fe.TryGetProperty("alive", out var al) || al.ValueKind != JsonValueKind.False;
        }

        if (!root.TryGetProperty("selections", out var sels) || sels.ValueKind != JsonValueKind.Object) return 0;

        var now = DateTime.UtcNow;
        int stale = 0;
        foreach (var prop in sels.EnumerateObject())
        {
            var s = prop.Value;
            double D(string k) => s.TryGetProperty(k, out var v) && v.ValueKind == JsonValueKind.Number
                                ? v.GetDouble() : 0.0;
            var q = new OracleQuote(
                DecimalOdds : D("decimal_odds"),
                MaxContracts: D("max_contracts"),
                Status      : s.TryGetProperty("status", out var st) ? (st.GetString() ?? "open") : "open",
                Live        : s.TryGetProperty("live", out var lv) && lv.ValueKind == JsonValueKind.True,
                VenueTsUnix : D("ts"),
                ReceivedUtc : now,
                WsVerified  : !s.TryGetProperty("wv", out var wv) || wv.ValueKind != JsonValueKind.False);
            _quotes[prop.Name] = q;
            if (!Fresh(q)) stale++;
        }
        return stale;
    }
}
