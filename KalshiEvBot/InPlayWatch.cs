using System.Globalization;
using System.Text;
using System.Text.Json;

namespace KalshiEvBot;

/// <summary>
/// IN-PLAY WATCH (2026-11-18). While the Pinnacle session is up, the bot must see EVERY tennis match Pinnacle is
/// pricing in play. Silent while that holds; one alert per match when it does not.
///
/// <para><b>Why it exists.</b> Each in-play blind spot found so far was found days late, by hand: the hand-off that
/// stranded 1 match in 4 until the 90-minute re-pair (11-10), the retire step that emptied rows once their listed
/// start passed (11-12), the price gate that cleared in-play rows on a price frozen in a session break (11-18).
/// Measured against Pinnacle's own feed (11-11 recording), the bot then followed 58 of the 62 matches Pinnacle
/// priced live. The other unseen matches are not ours: about half of Kalshi's ITF matches never get a live
/// Pinnacle price at all (a live listing with no markets, or no listing), and those must stay SILENT, or the watch
/// is noise. So the rule is narrow: alert only when Pinnacle IS pricing a match live and the bot is not seeing it.</para>
///
/// <para><b>Paired matches</b> (every <c>EV_INPLAY_WATCH_SEC</c>, default 60): a tennis moneyline from 5 min before
/// its listed start to <c>EV_INPLAY_WATCH_LOOKBACK_H</c> after it is SEEN when any of its selections reaches the bot
/// live, open and fresh. Otherwise the sidecar is asked what it holds for the YES token (<c>/debug/redirect</c>,
/// read-only, local - no venue traffic): the token itself, where the in-play redirect would send it, and every live
/// listing with the same two players in the league. Any of those live, open and pushed within
/// <c>EV_INPLAY_WATCH_MAX_PUSH_AGE_SEC</c> (300) = Pinnacle prices it live = a MISS. A miss that lasts
/// <c>EV_INPLAY_WATCH_ALERT_MIN</c> (3) is alerted once; when the bot then sees it, one "resolved" line follows.</para>
///
/// <para><b>Matches never paired.</b> The pairing run logs every tennis event Pinnacle's board lists that it left
/// unpaired (<c>pair_misses.jsonl</c>, see pair_pinnacle.board_misses). Only the LATEST run counts, so a match the
/// next run pairs stops counting. One alert per event per day when the sidecar shows Pinnacle pricing it live, or,
/// when the sidecar holds nothing for its league, once its listed start has passed.</para>
///
/// <para><b>Never noisy by construction.</b> Nothing is judged outside a session, nor in its first
/// <c>EV_INPLAY_WATCH_GRACE_MIN</c> (5) while the feed re-subscribes; a miss never spans a break; at most
/// <c>EV_INPLAY_WATCH_MAX_ALERTS_H</c> (8) alerts an hour, the rest summarised. Every state change is in
/// <c>EvInPlayWatch_*.csv</c>. <c>EV_INPLAY_WATCH=0</c> turns it off.</para>
/// </summary>
public sealed class InPlayWatch
{
    public enum State { Seen, NoLivePrice, Miss }
    public enum Act { None, Alert, Recovered }

    /// <summary>One match's watch state. Public for the self-test.</summary>
    public sealed class Track
    {
        public State Last = State.NoLivePrice;
        public bool Known, Alerted, RecoveryPosted, AlertSent;
        public DateTime? MissSince;
        public DateTime AlertedAt, LastChange;
    }

    private readonly string _sidecar;
    private readonly PinnacleOracle _oracle;
    private readonly List<EvPair> _ml;
    private readonly string _missesPath;
    private readonly Func<string, Task> _notify;
    private readonly HttpClient _http = new() { Timeout = TimeSpan.FromSeconds(3) };
    private readonly RollingCsv _log;
    private readonly Dictionary<string, Track> _tracks = new(StringComparer.Ordinal);
    private readonly HashSet<string> _alertedToday = new(StringComparer.Ordinal);
    private readonly Queue<DateTime> _sent = new();
    private DateTime? _liveSince;
    private string _day = "";

    public int EverySec { get; } = (int)Math.Max(15, EvConfig.Env("EV_INPLAY_WATCH_SEC", 60));
    public int AlertAfterSec { get; } = (int)Math.Max(60, EvConfig.Env("EV_INPLAY_WATCH_ALERT_MIN", 3) * 60);
    public int MaxPushAgeSec { get; } = (int)Math.Max(30, EvConfig.Env("EV_INPLAY_WATCH_MAX_PUSH_AGE_SEC", 300));
    public double LookbackH { get; } = Math.Max(1, EvConfig.Env("EV_INPLAY_WATCH_LOOKBACK_H", 12));
    public double GraceMin { get; } = Math.Max(0, EvConfig.Env("EV_INPLAY_WATCH_GRACE_MIN", 5));
    public int MaxAlertsPerHour { get; } = (int)Math.Max(1, EvConfig.Env("EV_INPLAY_WATCH_MAX_ALERTS_H", 8));
    public bool Recovery { get; } = EvConfig.Env("EV_INPLAY_WATCH_RECOVERY", 1) > 0;

    public long Passes, Judged, Misses, Alerts;

    public static bool Enabled => EvConfig.Env("EV_INPLAY_WATCH", 1) > 0;

    public InPlayWatch(string sidecarBase, PinnacleOracle oracle, List<EvPair> moneylinePairs, string pairsPath,
                       Func<string, Task> notify, string? logDir = null)
    {
        _sidecar = (sidecarBase ?? "").TrimEnd('/');
        _oracle = oracle;
        _ml = moneylinePairs;
        _notify = notify;
        _missesPath = Path.Combine(Path.GetDirectoryName(Path.GetFullPath(pairsPath)) ?? ".", "pair_misses.jsonl");
        _log = new RollingCsv(logDir ?? Directory.GetCurrentDirectory(), "EvInPlayWatch",
                              new[] { "Timestamp", "Kind", "EventId", "Ticker", "State", "Minutes", "Detail" }, lazy: true);
    }

    public async Task RunAsync(CancellationToken ct)
    {
        Console.WriteLine($"[INPLAY ] watch ON: every {EverySec}s while the Pinnacle session is up (after a {GraceMin:0}-min "
                        + $"warm-up), alert when Pinnacle prices a tennis match live and the bot has not seen it for "
                        + $"{AlertAfterSec / 60.0:0.#} min; silent otherwise. Log: EvInPlayWatch_*.csv (EV_INPLAY_WATCH=0 = off).");
        while (!ct.IsCancellationRequested)
        {
            try { await Task.Delay(TimeSpan.FromSeconds(EverySec), ct); } catch (OperationCanceledException) { break; }
            try { await PassAsync(DateTime.UtcNow, ct); }
            catch (OperationCanceledException) { break; }
            catch (Exception ex) { Console.WriteLine($"[INPLAY ] pass error: {ex.GetType().Name}: {ex.Message}"); }
        }
    }

    private bool SessionLive()
        => _oracle.IsConnected && _oracle.SessionReady
           && (!_oracle.Feed.Known || (_oracle.Feed.Alive && _oracle.Feed.Connected));

    private async Task PassAsync(DateTime now, CancellationToken ct)
    {
        string day = now.ToString("yyyyMMdd", CultureInfo.InvariantCulture);
        if (day != _day) { _day = day; _alertedToday.Clear(); }
        if (!SessionLive())
        {
            _liveSince = null;
            foreach (var t in _tracks.Values) t.MissSince = null;            // a miss never spans a break
            return;
        }
        _liveSince ??= now;
        if ((now - _liveSince.Value).TotalMinutes < GraceMin) return;      // the window just opened: re-subscribing
        Passes++;

        List<EvPair> pairs;
        lock (_ml)
            pairs = _ml.Where(p => !p.IsDerivative && !p.ThreeWay && EvSports.Of(p.KalshiTicker) == "tennis").ToList();
        var alerts = new List<string>();
        var present = new HashSet<string>(StringComparer.Ordinal);
        foreach (var g in pairs.GroupBy(p => string.IsNullOrEmpty(p.EventId) ? p.KalshiTicker : p.EventId))
        {
            var rows = g.ToList();
            var start = rows.Select(p => p.StartUtc).FirstOrDefault(s => s.HasValue);
            if (start is DateTime st && (now < st.AddMinutes(-5) || now > st.AddHours(LookbackH))) continue;
            present.Add(g.Key);
            var quotes = rows.SelectMany(p => new[] { p.YesToken, p.NoToken }).Distinct(StringComparer.Ordinal)
                             .Select(_oracle.Get).OfType<OracleQuote>().ToList();
            State state;
            string detail = "";
            if (quotes.Any(q => q.Live && q.Open && _oracle.Fresh(q))) state = State.Seen;
            else
            {
                var j = await RedirectAsync(rows[0].YesToken, ct);
                if (j is null) continue;                                    // the sidecar did not answer: next pass
                string? where = PinnacleLivePriced(j.Value, MaxPushAgeSec);
                state = where is null ? State.NoLivePrice : State.Miss;
                if (where is not null)
                    detail = $"Pinnacle is pricing it live ({where}); the bot has {BotView(quotes)}; "
                           + $"sidecar redirect {RedirectView(j.Value)}";
            }
            Judged++;
            if (state == State.Miss) Misses++;
            if (!_tracks.TryGetValue(g.Key, out var tr)) _tracks[g.Key] = tr = new Track();
            var prev = tr.Last;
            bool wasKnown = tr.Known;
            double mins = tr.MissSince is DateTime ms ? (now - ms).TotalMinutes : 0;
            var act = Step(tr, state, now, TimeSpan.FromSeconds(AlertAfterSec));
            if (!wasKnown || prev != state)
                Log(now, "paired", g.Key, rows[0].KalshiTicker, state.ToString(), mins, detail);
            if (act == Act.Alert && _alertedToday.Add("p:" + g.Key))
            {
                double since = tr.MissSince is DateTime m2 ? (now - m2).TotalMinutes : 0;
                tr.AlertSent = true;
                Log(now, "paired", g.Key, rows[0].KalshiTicker, "ALERT", since, detail);
                alerts.Add($"👁 **In-play miss** `{g.Key}` ({rows[0].EventTitle}): {detail}. Unseen for {since:0} min.");
            }
            else if (act == Act.Recovered && Recovery && tr.AlertSent)
            {
                double since = (now - tr.AlertedAt).TotalMinutes;
                Log(now, "paired", g.Key, rows[0].KalshiTicker, "RESOLVED", since, "");
                alerts.Add($"👁 resolved: `{g.Key}` reaches the bot live again ({since:0} min after the alert).");
            }
        }
        foreach (var k in _tracks.Keys.Where(k => !present.Contains(k)).ToList()) _tracks.Remove(k);
        await UnpairedAsync(now, alerts, ct);
        await SendAsync(alerts, now);
    }

    /// <summary>One watch step for a match. Alert once a miss has lasted <paramref name="alertAfter"/>; once per
    /// match (a second episode is in the log, not on Discord); "recovered" once, only after an alert and only when
    /// the bot SEES it again - a match that simply ends (Pinnacle stops pricing it) is not a recovery.</summary>
    public static Act Step(Track t, State state, DateTime now, TimeSpan alertAfter)
    {
        if (!t.Known || t.Last != state) t.LastChange = now;
        t.Known = true;
        t.Last = state;
        if (state == State.Miss)
        {
            t.MissSince ??= now;
            if (!t.Alerted && now - t.MissSince.Value >= alertAfter)
            {
                t.Alerted = true;
                t.AlertedAt = now;
                return Act.Alert;
            }
            return Act.None;
        }
        t.MissSince = null;
        if (state == State.Seen && t.Alerted && !t.RecoveryPosted)
        {
            t.RecoveryPosted = true;
            return Act.Recovered;
        }
        return Act.None;
    }

    /// <summary>Where Pinnacle is pricing this fixture LIVE, per the sidecar's <c>/debug/redirect</c> answer, or null.
    /// Live, open and pushed within <paramref name="maxPushAgeSec"/>: the token itself, the redirect target, or any
    /// listing with the same two players in the league. "(Games)" twins are skipped - they price games, not the match.</summary>
    public static string? PinnacleLivePriced(JsonElement j, int maxPushAgeSec)
    {
        if (j.ValueKind != JsonValueKind.Object) return null;
        if (j.TryGetProperty("parent", out var pa) && Priced(pa, maxPushAgeSec))
            return $"its own token {Str(pa, "id")}";
        if (j.TryGetProperty("redirect_target", out var rt) && Priced(rt, maxPushAgeSec))
            return $"the in-play redirect target {Str(rt, "id")}";
        if (j.TryGetProperty("same_players_in_league", out var sp) && sp.ValueKind == JsonValueKind.Array)
            foreach (var x in sp.EnumerateArray())
            {
                if (Str(x, "units") == "Games") continue;
                if (x.TryGetProperty("home_cached", out var hc) && Priced(hc, maxPushAgeSec))
                    return $"live listing {Str(x, "mid")}";
            }
        return null;
    }

    private static bool Priced(JsonElement c, int maxAge)
        => c.ValueKind == JsonValueKind.Object
           && c.TryGetProperty("live", out var l) && l.ValueKind == JsonValueKind.True
           && Str(c, "status") == "open"
           && (!c.TryGetProperty("age_s", out var a) || a.ValueKind != JsonValueKind.Number || a.GetDouble() <= maxAge);

    private static string Str(JsonElement e, string k)
        => e.ValueKind == JsonValueKind.Object && e.TryGetProperty(k, out var v)
           ? (v.ValueKind == JsonValueKind.String ? v.GetString() ?? "" : v.ValueKind == JsonValueKind.Null ? "" : v.GetRawText())
           : "";

    private string BotView(List<OracleQuote> quotes)
    {
        if (quotes.Count == 0) return "no quote for it";
        var q = quotes.OrderByDescending(x => x.ReceivedUtc).First();
        double age = _oracle.AgeMs(q) / 1000.0;
        return $"a {(q.Live ? "live" : "pre-match")} quote, {q.Status}, {(age >= 0 ? $"{age:0}s old" : "age unknown")}";
    }

    private static string RedirectView(JsonElement j)
    {
        string red = Str(j, "redirect");
        if (red.Length == 0) return "none";
        return j.TryGetProperty("redirect_target", out var t) && t.ValueKind == JsonValueKind.Object
            ? $"{red} ({(Str(t, "live") == "true" ? "live" : "not live")}, {Str(t, "status")})"
            : $"{red} (not cached)";
    }

    private async Task<JsonElement?> RedirectAsync(string sid, CancellationToken ct)
    {
        if (string.IsNullOrEmpty(sid) || _sidecar.Length == 0) return null;
        try
        {
            string body = await _http.GetStringAsync($"{_sidecar}/debug/redirect?sid={Uri.EscapeDataString(sid)}", ct);
            using var doc = JsonDocument.Parse(body);
            return doc.RootElement.Clone();
        }
        catch (OperationCanceledException) when (ct.IsCancellationRequested) { throw; }
        catch { return null; }
    }

    // ── Matches the pairing run left unpaired although Pinnacle's board lists them ─────────────────────────
    private async Task UnpairedAsync(DateTime now, List<string> alerts, CancellationToken ct)
    {
        var recs = LatestMisses(ReadTail(_missesPath), out DateTime runAt);
        if (recs.Count == 0 || now - runAt > TimeSpan.FromHours(2)) return;
        foreach (var r in recs)
        {
            string ev = Str(r, "event_id");
            if (ev.Length == 0 || _alertedToday.Contains("u:" + ev)) continue;
            r.TryGetProperty("board", out var board);
            var j = await RedirectAsync(Str(board, "token"), ct);
            if (j is null) continue;
            string? where = PinnacleLivePriced(j.Value, MaxPushAgeSec);
            bool cached = j.Value.TryGetProperty("parent", out var pa) && pa.ValueKind == JsonValueKind.Object;
            DateTime? st = ParseUtc(Str(board, "start"));
            string? why = where is not null ? $"Pinnacle is pricing it LIVE ({where})"
                        : !cached && st is DateTime s && s <= now && now - s <= TimeSpan.FromHours(3)
                            ? $"listed to start {s:HH:mm} UTC; live state unknown (the sidecar holds nothing for it)"
                        : null;
            if (why is null) continue;
            _alertedToday.Add("u:" + ev);
            string names = board.ValueKind == JsonValueKind.Object && board.TryGetProperty("names", out var nm)
                           && nm.ValueKind == JsonValueKind.Array
                ? string.Join(" vs ", nm.EnumerateArray().Select(x => x.GetString())) : "?";
            Log(now, "unpaired", ev, Str(r, "tickers"), "UNPAIRED", 0, $"{why} | {Str(r, "why")}");
            alerts.Add($"👁 **Unpaired in-play match** `{ev}` ({Str(r, "kalshi")} = {names}, {Str(board, "league")}): "
                     + $"{why}. The {runAt:HH:mm} pairing run left it unpaired - {Str(r, "why")}.");
        }
    }

    /// <summary>The miss records of the LATEST pairing run in <c>pair_misses.jsonl</c> (each run writes a
    /// <c>{"at", "run"}</c> record, then its misses with the same "at").</summary>
    public static List<JsonElement> LatestMisses(IEnumerable<string> lines, out DateTime runAt)
    {
        runAt = DateTime.MinValue;
        string? at = null;
        var recs = new List<JsonElement>();
        foreach (var line in lines)
        {
            if (string.IsNullOrWhiteSpace(line)) continue;
            JsonElement j;
            try { using var d = JsonDocument.Parse(line); j = d.RootElement.Clone(); }
            catch { continue; }
            if (j.ValueKind != JsonValueKind.Object) continue;
            string a = Str(j, "at");
            if (j.TryGetProperty("run", out _)) { at = a; recs.Clear(); continue; }
            if (at is not null && a == at) recs.Add(j);
        }
        if (at is not null && ParseUtc(at) is DateTime t) runAt = t;
        return recs;
    }

    private static DateTime? ParseUtc(string s)
        => DateTime.TryParse(s, CultureInfo.InvariantCulture,
                             DateTimeStyles.AdjustToUniversal | DateTimeStyles.AssumeUniversal, out var t) ? t : null;

    private static IEnumerable<string> ReadTail(string path, int bytes = 256 * 1024)
    {
        try
        {
            if (!File.Exists(path)) return Array.Empty<string>();
            using var fs = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.ReadWrite);
            long from = Math.Max(0, fs.Length - bytes);
            fs.Seek(from, SeekOrigin.Begin);
            using var r = new StreamReader(fs, Encoding.UTF8);
            var lines = r.ReadToEnd().Split('\n').ToList();
            if (from > 0 && lines.Count > 0) lines.RemoveAt(0);               // a partial first line
            return lines;
        }
        catch { return Array.Empty<string>(); }
    }

    private void Log(DateTime now, string kind, string ev, string ticker, string state, double minutes, string detail)
    {
        try
        {
            _log.WriteRow(new[] { now.ToString("o", CultureInfo.InvariantCulture), kind, RollingCsv.Q(ev), RollingCsv.Q(ticker),
                                  state, RollingCsv.N(minutes, 1), RollingCsv.Q(detail) });
        }
        catch { }
    }

    private async Task SendAsync(List<string> alerts, DateTime now)
    {
        if (alerts.Count == 0) return;
        while (_sent.Count > 0 && now - _sent.Peek() > TimeSpan.FromHours(1)) _sent.Dequeue();
        int room = Math.Max(0, Math.Min(3, MaxAlertsPerHour - _sent.Count));
        foreach (var a in alerts.Take(room))
        {
            _sent.Enqueue(now);
            Alerts++;
            Con.Line(ConsoleColor.Yellow, "[INPLAY ] " + a.Replace("**", "").Replace("`", ""));
            await _notify(a);
        }
        int held = alerts.Count - room;
        if (held > 0)
        {
            // Said once, with the alerts that did go out; at the hourly cap the console and the CSV carry it alone.
            string s = $"👁 {held} more in-play alert(s) held back by the rate cap - see EvInPlayWatch_*.csv.";
            Con.Line(ConsoleColor.Yellow, "[INPLAY ] " + s);
            if (room > 0) await _notify(s);
        }
    }
}
