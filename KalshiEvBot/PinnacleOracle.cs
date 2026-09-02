using System.Collections.Concurrent;
using System.Globalization;
using System.Text.Json;

namespace KalshiEvBot;

/// <summary>
/// The venue's own report on its ODDS SOCKET, published on every /odds poll. Separate from quote age on
/// purpose: a quiet market on a healthy feed and a dead feed full of recently-cached quotes look identical
/// to an age check, and only one of them is safe to trade against.
/// </summary>
public readonly record struct FeedHealth(
    bool Alive, bool Connected, string Source, double LastFrameAge,
    int Subscribed, int ActiveLeagues, int LiveMsgs, int PreMsgs)
{
    public bool Known => !string.IsNullOrEmpty(Source);
    public override string ToString()
        => !Known ? "feed (not reported)"
         : $"{Source} {(Alive ? "alive" : "DOWN")}{(Connected ? "" : " disconnected")}"
         + (double.IsFinite(LastFrameAge) ? $", last frame {LastFrameAge:0}s ago" : ", no frame yet")
         + $", {Subscribed}/{ActiveLeagues} league(s) subscribed, {LiveMsgs + PreMsgs} msg(s)";
}

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
    private readonly HashSet<string> _tokenSet;
    private readonly object _tokenLock = new();
    private readonly int _pollMs, _chunk;
    private readonly double _maxAgeSec, _maxAgeInPlaySec;

    public volatile bool IsConnected;
    public volatile bool SessionReady = true;
    public volatile int  StaleCount;
    private bool _feedPolicy, _feedAlive = true;
    private int  _lastReady = -1;
    private string _lastError = "";
    private int _sameErrorCount;

    // ── POLL WATCHDOG ────────────────────────────────────────────────────────────────────────────────
    // Dropping the interval buys DETECTION LATENCY (a Pinnacle move sits unseen between polls) and buys it
    // from the sidecar's event loop, which also runs the Pinnacle WS reader. That trade can go NEGATIVE:
    // if /odds serialisation starts competing with the reader, quotes get STALER and we have paid for a
    // worse oracle. Measured baseline before the change (2026-09-01): in-play quote age p50 41ms, p90 545ms.
    //
    // So the loop measures itself. `PollMs*` is our load on the sidecar; `QuoteAgeMs*` is what we get back.
    // If the first rises and the second follows, the interval is too low for this machine.
    private readonly object _statLock = new();
    private readonly Queue<double> _pollMsHist = new();
    private readonly Queue<double> _ageMsHist  = new();
    private readonly Queue<bool>   _satHist    = new();   // saturation over the RECENT window
    private const int StatWindow = 240;
    private long _polls, _saturated, _errors;
    private DateTime _lastHealth = DateTime.UtcNow;

    private static double Pct(Queue<double> q, double p) => Percentile(q, p);

    /// <summary>Median round-trip of one full poll (all chunks). Our LOAD on the sidecar.</summary>
    public double PollMsP50 { get { lock (_statLock) return Pct(_pollMsHist, 0.5); } }
    public double PollMsP90 { get { lock (_statLock) return Pct(_pollMsHist, 0.9); } }
    /// <summary>Median age of the quotes the poll returned. What we GET for that load.</summary>
    public double QuoteAgeMsP50 { get { lock (_statLock) return Pct(_ageMsHist, 0.5); } }
    /// <summary>Polls that took LONGER than the interval — the loop cannot hold the requested cadence.</summary>
    public long SaturatedPolls { get { lock (_statLock) return _saturated; } }
    private bool _sawUnverified, _wvNoticeShown;

    public PinnacleOracle(string sidecarBaseUrl, IEnumerable<string> tokens)
    {
        _base   = (sidecarBaseUrl ?? "").TrimEnd('/');
        _tokens = tokens.Distinct(StringComparer.Ordinal).ToList();
        _tokenSet = new HashSet<string>(_tokens, StringComparer.Ordinal);
        _pollMs = EnvInt("EV_ORACLE_POLL_MS", 3000);
        _chunk  = EnvInt("EV_ORACLE_CHUNK", 200);
        _maxAgeSec = EnvInt("EV_ORACLE_MAX_AGE_MS", 30_000) / 1000.0;
        _maxAgeInPlaySec = EnvInt("EV_ORACLE_MAX_AGE_INPLAY_MS", 5_000) / 1000.0;
    }

    private static int EnvInt(string k, int dflt)
        => int.TryParse(Environment.GetEnvironmentVariable(k), NumberStyles.Any,
                        CultureInfo.InvariantCulture, out var v) && v > 0 ? v : dflt;

    /// <summary>Raised after every successful poll. Pinnacle moving is a signal source in its own right:
    /// a value bet can appear with the Kalshi book completely still, and a bot that only woke on Kalshi
    /// ticks would never see those.</summary>
    public event Action? OnPolled;

    public OracleQuote? Get(string token) => _quotes.TryGetValue(token, out var q) ? q : null;

    /// <summary>Adds selections discovered after startup (a pair reload). Safe from any thread.
    /// Without this a run longer than a day polls only the matches that existed when it started.</summary>
    public int AddTokens(IEnumerable<string> tokens)
    {
        int added = 0;
        lock (_tokenLock)
            foreach (var t in tokens)
                if (!string.IsNullOrWhiteSpace(t) && !_tokenSet.Contains(t))
                { _tokenSet.Add(t); _tokens.Add(t); added++; }
        return added;
    }

    /// <summary>
    /// REPLACES the polled set with the current pair file's selections. Returns (added, removed).
    ///
    /// <para>Adding without ever removing is what breaks a long run. Each daily re-pair brings a fresh slate
    /// — at ~800 soccer pairs that is ~870 selections a day — while yesterday's finished matches stay in the
    /// list forever. A fortnight becomes eleven thousand selections polled every three seconds, nearly all
    /// of them games that ended days ago, and the poll grows without bound until it cannot keep cadence.</para>
    ///
    /// <para>Safe to drop them here because the settlement watcher keeps its OWN archive of every ticker it
    /// has ever seen: results still get banked for markets that have left the board. This list is the LIVE
    /// watchlist, not the record.</para>
    /// </summary>
    public (int Added, int Removed) SetTokens(IEnumerable<string> tokens)
    {
        var want = new HashSet<string>(tokens.Where(t => !string.IsNullOrWhiteSpace(t)), StringComparer.Ordinal);
        lock (_tokenLock)
        {
            int added = want.Count(t => !_tokenSet.Contains(t));
            var gone  = _tokenSet.Where(t => !want.Contains(t)).ToList();
            _tokens.Clear();
            _tokens.AddRange(want);
            _tokenSet.Clear();
            foreach (var t in want) _tokenSet.Add(t);
            foreach (var t in gone) _quotes.TryRemove(t, out _);   // else the cache grows as the list shrinks
            return (added, gone.Count);
        }
    }

    public int TokenCount { get { lock (_tokenLock) return _tokens.Count; } }

    public int QuoteCount => _quotes.Count;

    /// <summary>Last reported odds-socket health. Default (Known=false) until the venue publishes it.</summary>
    public FeedHealth Feed { get; private set; }

    /// <summary>
    /// Is this quote recent enough to be a fair value? Two policies, because the same symptom means
    /// opposite things at different venues. Pinnacle serves the last-known price when its own fetch fails,
    /// with the timestamp frozen at the last success, so there an old ts really can mean "our session died"
    /// and per-quote age is the right gate. A push-only venue stamps ts only when the event actually moves,
    /// so there an old ts means a quiet market and the same gate would discard every pre-match price. When
    /// the sidecar declares the feed policy we follow the socket instead.
    /// </summary>
    public bool Fresh(OracleQuote q)
    {
        if (_feedPolicy) return _feedAlive && q.VenueTsUnix > 0;
        double age = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds() / 1000.0 - q.VenueTsUnix;
        // IN-PLAY GETS A FAR TIGHTER GATE, because there the two things age cannot distinguish diverge.
        //
        // 30s was set for pre-match, where a quiet market is genuinely quiet and a price hours old is still
        // the right price. In-play on a subscribed league, silence means one of two things and neither is
        // "nothing has changed": the market is SUSPENDED (Pinnacle stops quoting the instant something
        // happens), or we are not really covered. Both are exactly when the price is about to move.
        //
        // Measured 2026-08-22: a quote 2.9s old and marked open was the last tick BEFORE a goal, while
        // Kalshi had already repriced 13c. The market then went quiet for 65s — the suspension — and
        // reopened 18c away. Under a 30s gate that pre-goal price stayed "fresh" the whole way down.
        //
        // 5s is a STARTING value, not a measured one. Every row logs OracleAgeMs, so M1 can bucket realised
        // outcomes by age and say where the real cut is instead of us guessing twice.
        return age <= (q.Live ? _maxAgeInPlaySec : _maxAgeSec);
    }

    public double AgeMs(OracleQuote q)
        => q.VenueTsUnix <= 0 ? -1
         : Math.Round(DateTimeOffset.UtcNow.ToUnixTimeMilliseconds() - q.VenueTsUnix * 1000.0);

    /// <summary>
    /// Forces an INDEPENDENT re-read of these selections from Pinnacle, then refreshes the local quotes.
    /// Returns the sidecar's tri-state: "ok" (the venue answered), "failed" (we asked and got nothing),
    /// "unsupported" (this book has no independent price read), or "" if the call itself failed.
    ///
    /// <para><b>Why this is not optional.</b> Kalshi is checked against itself twice — the WS finds a
    /// candidate and REST prices it, two separate reads that can and do disagree. Pinnacle had no equivalent:
    /// screening and "verification" both came from one sidecar cache, and a cache cannot disagree with
    /// itself. The arb bot measured that exact failure — its HardVen leg confirmed 110/110 while the
    /// independently-checked Kalshi leg disagreed 76% of the time — and concluded the gap was
    /// instrumentation, not venue quality.</para>
    ///
    /// <para>It is what would have caught the 2026-08-22 Eredivisie phantom: a goal had moved Kalshi while
    /// our cached Pinnacle price was still pre-goal but only 2.9s old. Age could not see it; asking the
    /// venue again would have.</para>
    ///
    /// <para>Called only on a candidate that already clears the threshold — a few dozen times an hour, not
    /// on the 3s poll — and it reuses the same authed REST call the sidecar's own backstop makes, so it adds
    /// no new request shape to the venue.</para>
    /// </summary>
    public async Task<string> RefetchAsync(IEnumerable<string> tokens, CancellationToken ct = default)
    {
        var list = tokens.Distinct(StringComparer.Ordinal).ToList();
        if (list.Count == 0 || string.IsNullOrWhiteSpace(_base)) return "";
        try
        {
            string q = Uri.EscapeDataString(string.Join(",", list));
            using var resp = await _http.GetAsync($"{_base}/odds?selections={q}&fresh=1", ct);
            if (!resp.IsSuccessStatusCode) return "";
            string body = await resp.Content.ReadAsStringAsync(ct);
            Apply(body);                                   // refresh the cache with what the venue just said
            using var doc = JsonDocument.Parse(body);
            return doc.RootElement.TryGetProperty("venue_refetch", out var v) && v.ValueKind == JsonValueKind.String
                 ? (v.GetString() ?? "") : "";
        }
        catch (Exception ex)
        {
            if (_verboseRefetch) Console.WriteLine($"[ORACLE] refetch failed: {ex.GetType().Name}: {ex.Message}");
            return "";
        }
    }

    private static readonly bool _verboseRefetch =
        Environment.GetEnvironmentVariable("EV_VERBOSE_REFETCH") == "1";

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
            var sw = System.Diagnostics.Stopwatch.StartNew();
            try
            {
                bool anyOk = false; int stale = 0;
                List<string> toks;
                lock (_tokenLock) toks = _tokens.ToList();   // snapshot: the list can grow mid-poll
                for (int i = 0; i < toks.Count; i += _chunk)
                {
                    string q = Uri.EscapeDataString(string.Join(",", toks.Skip(i).Take(_chunk)));
                    using var resp = await _http.GetAsync($"{_base}/odds?selections={q}", ct);
                    if (!resp.IsSuccessStatusCode) continue;
                    stale += Apply(await resp.Content.ReadAsStringAsync(ct));
                    anyOk = true;
                }
                IsConnected = anyOk;
                StaleCount  = stale;
                if (anyOk) RecordPollStats(sw.Elapsed.TotalMilliseconds);
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
                // Throttled: a sidecar that is simply not running fails every poll, and at a 3s cadence
                // that buries every other line in the log under the same sentence. Say it once, then
                // periodically with a count so the condition stays visible without drowning the console.
                string msg = $"{ex.GetType().Name}: {ex.Message}";
                _sameErrorCount = msg == _lastError ? _sameErrorCount + 1 : 1;
                _lastError = msg;
                if (_sameErrorCount == 1 || _sameErrorCount % 100 == 0)
                    Console.WriteLine($"[ORACLE] poll error: {msg}"
                                    + (_sameErrorCount > 1 ? $"  (x{_sameErrorCount})" : ""));
                lock (_statLock) _errors++;
            }

            // SLEEP THE REMAINDER, not the whole interval. Delaying a fixed _pollMs AFTER the work makes the
            // real cadence interval+duration — invisible at 3000ms with a 100ms poll, but at 500ms a 300ms
            // poll silently becomes 800ms and the change you thought you made is 60% undone.
            int left = PollDelayMs(_pollMs, sw.Elapsed.TotalMilliseconds);
            if (left > 1)
            {
                try { await Task.Delay(left, ct); } catch (OperationCanceledException) { break; }
            }
            else
            {
                // Cannot hold the cadence: the poll itself outlasts the interval. Yield briefly so the loop
                // cannot starve everything else, and count it — SaturatedPolls is the number that says the
                // interval is set below what this machine and sidecar can actually serve.
                lock (_statLock) _saturated++;
                try { await Task.Delay(1, ct); } catch (OperationCanceledException) { break; }
            }
            ReportHealth();
        }
        IsConnected = false;
    }

    /// <summary>
    /// Milliseconds left to wait to hold <paramref name="intervalMs"/> between poll STARTS.
    ///
    /// <para>Sleeping the full interval AFTER the work makes the true cadence interval+duration. That is
    /// invisible at 3000ms with a 100ms poll (3% off) and material at 500ms with a 300ms poll (60% off) —
    /// the interval you set is not the interval you get, and the effect grows exactly as you tighten it.</para>
    ///
    /// <para>Clamped to [0, interval]: never negative, and never longer than the interval even if the clock
    /// jumps backwards.</para>
    /// </summary>
    internal static int PollDelayMs(int intervalMs, double elapsedMs)
        => (int)Math.Clamp(intervalMs - elapsedMs, 0, intervalMs);

    /// <summary>Percentile of a rolling window; NaN when empty. Internal so the self-test can pin it.</summary>
    internal static double Percentile(IEnumerable<double> src, double p)
    {
        var v = src.ToArray();
        if (v.Length == 0) return double.NaN;
        Array.Sort(v);
        // NEAREST-RANK (ceil), not truncation. `(int)(p*(n-1))` rounds DOWN, so p90 of a five-sample window
        // returns the fourth value — it UNDERSTATES the tail. In a latency watchdog that is the wrong
        // direction to be wrong in: this number exists to notice trouble early, and a p90 that flatters
        // itself is worse than no p90 at all.
        int idx = (int)Math.Ceiling(p * v.Length) - 1;
        return v[Math.Clamp(idx, 0, v.Length - 1)];
    }

    private void RecordPollStats(double ms)
    {
        // Quote age is sampled from the cache we just refreshed, so it measures what the sidecar HANDED US,
        // which is the thing that must not degrade. Sampled rather than exhaustive: at two polls a second
        // over a few hundred selections the median is identical and the cost is not.
        double age = double.NaN;
        var ages = new List<double>();
        foreach (var q in _quotes.Values)
        {
            ages.Add(AgeMs(q));
            if (ages.Count >= 256) break;
        }
        if (ages.Count > 0) { ages.Sort(); age = ages[ages.Count / 2]; }

        lock (_statLock)
        {
            _polls++;
            _pollMsHist.Enqueue(ms);
            while (_pollMsHist.Count > StatWindow) _pollMsHist.Dequeue();
            _satHist.Enqueue(ms >= _pollMs);
            while (_satHist.Count > StatWindow) _satHist.Dequeue();
            if (double.IsFinite(age))
            {
                _ageMsHist.Enqueue(age);
                while (_ageMsHist.Count > StatWindow) _ageMsHist.Dequeue();
            }
        }
    }

    /// <summary>
    /// Periodic health line, and a LOUD one when the poll interval is hurting rather than helping.
    ///
    /// <para>Two failure shapes, and they need different responses. SATURATION (the poll outlasts its own
    /// interval) means the cadence is simply unservable — raise it. DEGRADED QUOTES (poll time fine, but
    /// the ages we are handed have climbed) means we are crowding the sidecar's event loop and the WS
    /// reader is losing, which is the expensive failure because it makes the oracle worse while looking
    /// like it is working.</para>
    /// </summary>
    private void ReportHealth()
    {
        double everyMin = EnvInt("EV_ORACLE_HEALTH_MIN", 10);
        if (everyMin <= 0 || (DateTime.UtcNow - _lastHealth).TotalMinutes < everyMin) return;
        _lastHealth = DateTime.UtcNow;

        double p50, p90, p99, age; long polls, sat, err; int satRecent, satWindow;
        lock (_statLock)
        {
            p50 = Pct(_pollMsHist, 0.5); p90 = Pct(_pollMsHist, 0.9); age = Pct(_ageMsHist, 0.5);
            p99 = Pct(_pollMsHist, 0.99);
            polls = _polls; sat = _saturated; err = _errors;
            satRecent = _satHist.Count(x => x); satWindow = _satHist.Count;
        }
        if (!double.IsFinite(p50)) return;

        // DUTY CYCLE is the number that says whether there is room, so lead with it rather than making
        // the reader divide two figures in their head.
        // p99 as well as p50/p90, because THE TAIL IS WHAT SATURATES. Deciding whether a tighter
        // interval is safe needs to know how often a poll lands near it, and a median of 83ms with a
        // p90 of 99ms says nothing about the excursions that actually breach the deadline.
        string line = $"[ORACLE] health: poll {p50:0}/{p90:0}/{p99:0}ms (p50/p90/p99) of a {_pollMs}ms interval "
                    + $"({100.0 * p50 / Math.Max(1, _pollMs):0}% duty), quote age p50 {age:0}ms, "
                    + $"{polls} poll(s), {sat} saturated, {err} error(s)";

        // AgeCeiling is a WARNING line, not a gate: the trading guard is EV_ORACLE_MAX_AGE_INPLAY_MS and
        // this must never quietly change what the bot trades. Default 250ms sits well above the 41ms
        // baseline and well below the 1000ms gate, so it fires on real degradation and not on noise.
        double ageCeil = EnvInt("EV_ORACLE_AGE_WARN_MS", 250);

        // A RATE OVER A RECENT WINDOW, NOT A CUMULATIVE COUNT.
        //
        // The first cut warned on `sat > 0` against a counter that never resets, so ONE transient — a GC
        // pause, an OS scheduling blip — made this fire on every health line for the rest of the run,
        // telling the operator to raise the interval. Observed 2026-09-01: 2 saturated polls out of
        // 10,625 (0.019%) at 83/99ms against a 500ms interval — a 17% duty cycle with quote age
        // unchanged at 41ms, i.e. a comfortable configuration that this line nagged about until the
        // interval was raised on its advice.
        //
        // A monitor that cries wolf gets ignored, which costs more than the thing it watches for. So
        // saturation only counts as trouble when it is HAPPENING: a nontrivial share of the last few
        // hundred polls. The cumulative total stays in the health line as information, not as an alarm.
        double satPct = EnvInt("EV_ORACLE_SAT_WARN_PCT", 2);
        bool satBad = satWindow >= 30 && 100.0 * satRecent / satWindow >= satPct;
        bool bad = satBad || p50 > _pollMs * 0.5 || age > ageCeil;
        if (bad)
        {
            Con.Line(ConsoleColor.Yellow, line);
            if (satBad)
                Con.Line(ConsoleColor.Yellow, $"[ORACLE] {satRecent} of the last {satWindow} poll(s) "
                       + $"outran the {_pollMs}ms interval ({100.0 * satRecent / satWindow:0.#}%) — the "
                       + "cadence is below what the sidecar can currently serve. Raise EV_ORACLE_POLL_MS.");
            else if (p50 > _pollMs * 0.5)
                Con.Line(ConsoleColor.Yellow, $"[ORACLE] a poll now costs {p50:0}ms of a {_pollMs}ms "
                       + "interval — little headroom left before saturation.");
            if (age > ageCeil)
                Con.Line(ConsoleColor.Yellow, $"[ORACLE] quote age p50 {age:0}ms is above the {ageCeil:0}ms "
                       + "warning line (baseline 41ms). Polling harder may be CROWDING the sidecar's WS "
                       + "reader — that makes the oracle worse, not faster. Raise EV_ORACLE_POLL_MS and "
                       + "see if it recovers.");
        }
        else Console.WriteLine(line);
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

            // The venue's own view of its odds SOCKET, distinct from any one quote's age. A market can be
            // quiet for minutes on a healthy feed, and every cached quote can look recent on a dead one —
            // so a bot that only watches quote age cannot tell a slow evening from a socket that dropped.
            string S(string k) => fe.TryGetProperty(k, out var v) && v.ValueKind == JsonValueKind.String
                                ? (v.GetString() ?? "") : "";
            int? I(string k) => fe.TryGetProperty(k, out var v) && v.ValueKind == JsonValueKind.Number
                              ? v.GetInt32() : null;
            Feed = new FeedHealth(
                Alive       : _feedAlive,
                Connected   : !fe.TryGetProperty("connected", out var cn) || cn.ValueKind != JsonValueKind.False,
                Source      : S("source"),
                LastFrameAge: fe.TryGetProperty("last_frame_age", out var lf) && lf.ValueKind == JsonValueKind.Number
                              ? lf.GetDouble() : double.NaN,
                Subscribed  : I("subscribed_leagues") ?? -1,
                ActiveLeagues: I("active_leagues") ?? -1,
                LiveMsgs    : I("live_msgs") ?? -1,
                PreMsgs     : I("pre_msgs") ?? -1);
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
            if (!q.WsVerified) _sawUnverified = true;
        }

        // THE SCREENING-ONLY GATE GOES QUIET IN DEDICATED-WS MODE, AND ITS SILENCE LOOKS LIKE SUCCESS.
        // `ws_verified_map` reports every selection verified unless the sidecar is in window-reader mode
        // ("paho/REST subscribe every active league -> all True"). That is honest — a dedicated connection
        // really does subscribe every active league — but it means the guard that caught the +19c phantoms
        // can no longer discriminate, and `unverified 0` on the status line would read as "the phantoms
        // stopped" rather than "the detector stopped". Say which it is, once.
        if (!_wvNoticeShown && _quotes.Count >= 20 && !_sawUnverified)
        {
            _wvNoticeShown = true;
            Console.WriteLine($"[ORACLE] every one of {_quotes.Count} selection(s) reports WS-verified. That is "
                            + "expected in DEDICATED-WS mode (the sidecar subscribes each active league "
                            + "directly), but it means the screening-only gate cannot flag anything here — "
                            + "quote FRESHNESS is the only remaining guard against a delayed price.");
        }
        return stale;
    }
}
