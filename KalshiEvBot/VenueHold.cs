using System.Globalization;
using System.Text.Json;

namespace KalshiEvBot;

/// <summary>
/// Whether Kalshi is taking orders, as the order path sees it, and what to do while it is not. Split out of
/// LiveExecutor 2026-11-05 so it can be tested with no venue and no clock.
///
/// <para><b>Two kinds of hold.</b> CLOSED: Kalshi's own status says trading is off — the weekly maintenance
/// (Thursdays 03:00-05:00 ET) or an unplanned halt. Nothing is sent; the status is read every
/// EV_VENUE_POLL_SEC (30s) and orders resume the moment it says trading is on. FAILING: the status says trading
/// is on, or cannot be read, but orders keep failing — 60s doubling to 10 min, then one order as the test,
/// because a gateway that is refusing us is not improved by asking faster. Screening and telemetry continue
/// through both. Held signals are COUNTED, never queued: a price from inside a closure is not one to buy at
/// the reopen.</para>
///
/// <para><b>Why the poll.</b> 2026-11-05: Kalshi reopened at 10:00:00 and the bot noticed at 10:17. A hold
/// could end only when a real order went through, after a backoff that had grown to 10 minutes, so the first
/// minutes of an open venue were spent waiting — 4 signals across seven Thursdays. A status read is one GET.</para>
///
/// <para><b>Why the closure log.</b> While Kalshi is closed its book is frozen and Pinnacle keeps moving, so
/// the screen logs "signals" nobody could buy: 39 on 2026-11-05, up to +12c, every one gone when Kalshi
/// repriced at the open. --resolve sets aside the scheduled window by the clock; an unplanned closure is known
/// only here, so each closed period is appended to ev_venue_closures.jsonl for the report to set aside the
/// same way.</para>
/// </summary>
public sealed class VenueHold
{
    public const string FileName = "ev_venue_closures.jsonl";
    public static string DefaultPath => Path.Combine(Directory.GetCurrentDirectory(), FileName);
    private static readonly object FileLock = new();

    /// <summary>One closed period: from the first refused order of the streak to the moment Kalshi was seen
    /// trading again (or the bot stopped).</summary>
    public readonly record struct Closure(DateTime Start, DateTime End, string Reason, int Refused, long Held, string EndedBy);

    private readonly Func<Task<(bool ExchangeActive, bool TradingActive, DateTime? Resume)>> _status;
    private readonly Func<DateTime> _now;
    private readonly double _pollSec;
    private readonly object _lock = new();

    private DateTime _until = DateTime.MinValue;       // no order before this, whichever kind of hold
    private string _why = "";                          // the refusal that started this hold; "" = no hold
    private bool _closed;                              // Kalshi said trading is off
    private DateTime _closedSince, _streakStart, _nextPoll;
    private int _consecutive;
    private double _backoffSec = 60;
    private int _polling;                              // 1 while a status read is in flight
    private long _held, _heldAtLastNote, _holds;

    /// <summary>One line per hold/release, for Discord. Optional.</summary>
    public Action<string>? Notify { get; set; }
    /// <summary>Where closed periods are appended; null = not recorded.</summary>
    public string? ClosuresPath { get; set; } = DefaultPath;

    public long Held => Interlocked.Read(ref _held);
    public long Holds => Interlocked.Read(ref _holds);
    public bool Closed { get { lock (_lock) return _closed; } }

    public VenueHold(Func<Task<(bool ExchangeActive, bool TradingActive, DateTime? Resume)>> status,
                     Func<DateTime>? clock = null, double pollSec = 0)
    {
        _status  = status;
        _now     = clock ?? (() => DateTime.UtcNow);
        _pollSec = pollSec > 0 ? pollSec : Math.Max(5, EvConfig.Env("EV_VENUE_POLL_SEC", 30));
    }

    /// <summary>Kalshi itself saying the exchange or its trading is shut, as opposed to refusing this order.</summary>
    public static bool IsPauseCode(string tag)
        => tag.Contains("exchange_is_paused", StringComparison.Ordinal)
        || tag.Contains("trading_is_paused", StringComparison.Ordinal);

    /// <summary>Called before every order. True = held: do not send it. While closed, a due status read happens
    /// HERE as well as on the background tick, so the first signal after a reopen is not the one that waits.</summary>
    public async Task<bool> GateAsync()
    {
        bool due;
        lock (_lock) due = _closed && _now() >= _nextPoll;
        if (due) await PollAsync();
        lock (_lock) { if (_now() >= _until) return false; }
        Interlocked.Increment(ref _held);
        return true;
    }

    /// <summary>While closed: read Kalshi's status at most every EV_VENUE_POLL_SEC and release the hold the moment
    /// it says trading is on. A no-op otherwise, so it is safe on a tight timer. Never throws.</summary>
    public async Task PollAsync()
    {
        lock (_lock)
        {
            if (!_closed || _now() < _nextPoll) return;
            _nextPoll = _now().AddSeconds(_pollSec);
        }
        if (Interlocked.Exchange(ref _polling, 1) == 1) return;          // one read at a time
        try
        {
            var st = await _status();
            Closure? ended = null;
            lock (_lock)
            {
                if (st.ExchangeActive && st.TradingActive)
                {
                    if (_closed) ended = EndClosureLocked(_now(), "Kalshi's status says trading is on");
                }
                // Still closed: keep the hold from lapsing into a test order while the status can answer for it.
                else if (_closed && _until < _now().AddSeconds(2 * _pollSec)) _until = _now().AddSeconds(2 * _pollSec);
            }
            if (ended is { } e) Announce(e);
        }
        catch { /* could not ask: the hold stands, and lapses into the old one-order test if this keeps failing */ }
        finally { Volatile.Write(ref _polling, 0); }
    }

    /// <summary>An order that failed. One 4xx is a datum; a 5xx, three in a row, or Kalshi saying it is paused
    /// gets one status read, and the answer decides which kind of hold follows.</summary>
    public async Task OnFailureAsync(Exception ex, string tag, DateTime attemptAt)
    {
        int n;
        lock (_lock) { if (_consecutive == 0) _streakStart = attemptAt; n = ++_consecutive; }
        bool serverSide = ex is HttpRequestException h && h.StatusCode is { } sc && (int)sc >= 500;
        bool paused = IsPauseCode(tag);
        if (!serverSide && !paused && n < 3) return;                      // one 4xx is a datum, not a closure

        bool? open = null; DateTime? resume = null; string said;
        try
        {
            var st = await _status();
            open   = st.TradingActive && st.ExchangeActive;
            resume = st.Resume;
            said   = open.Value ? "exchange reports trading ACTIVE - orders are failing anyway"
                                : "exchange reports trading NOT active" + (resume is { } r0 ? $", est. resume {r0:HH:mm}Z" : "");
        }
        catch (Exception sx) { said = $"status check failed ({sx.GetType().Name})"; }

        // "Paused" from an exchange that reports trading ON is one market, not the venue: hold nothing until the
        // usual three in a row. (The evaluator reads each market's own status, so these should rarely get here.)
        if (open == true && !serverSide && n < 3) return;

        var now = _now();
        bool closed = open == false;
        double sec = closed && resume is { } r && r > now ? Math.Clamp((r - now).TotalSeconds + 15, 60, 3600) : _backoffSec;
        bool announce, isClosed;
        DateTime until;
        Closure? ended = null;
        lock (_lock)
        {
            // Trading is back on although this order failed: the closure is over, whatever comes next.
            if (open == true && _closed) ended = EndClosureLocked(now, "Kalshi's status says trading is on");
            bool becameClosed = closed && !_closed;
            announce = _why.Length == 0 || becameClosed;
            if (becameClosed)
            {
                _closed      = true;
                _closedSince = _streakStart;
                _nextPoll    = now.AddSeconds(_pollSec);
            }
            isClosed    = _closed;          // unknown status (open == null) keeps whatever we last knew
            until       = _until = now.AddSeconds(sec);
            _why        = tag;
            _backoffSec = Math.Min(_backoffSec * 2, 600);
            _holds++;
        }
        if (ended is { } e) Announce(e);

        DateTime? windowEnd = Calibration.KalshiMaintenanceEndUtc(now);
        string est = resume is { } r1 ? $", Kalshi estimates {r1:HH:mm}Z" : "";
        if (isClosed)
        {
            Con.Line(ConsoleColor.Yellow,
                $"[HOLD ] {n} order(s) refused ({tag}); "
              + (windowEnd is { } we ? $"Kalshi's weekly maintenance (Thu 03:00-05:00 ET, ends {we:HH:mm}Z)"
                                     : "Kalshi reports trading OFF outside the weekly maintenance window")
              + $"{est}. Orders held; asking Kalshi's status every {_pollSec:0}s, resumes by itself. Screening continues.");
            if (announce)
                Say(windowEnd is { } we2
                    ? $"⏸️ Kalshi's weekly maintenance (Thu 03:00–05:00 ET): no orders until it reopens, expected ~{we2:HH:mm} UTC. "
                      + $"Signals meanwhile are skipped, not queued; the bot checks Kalshi's status every {_pollSec:0}s and resumes by itself."
                    : $"⏸️ Kalshi has stopped trading ({tag}) and this is NOT the scheduled maintenance{est}. "
                      + $"Signals meanwhile are skipped, not queued; the bot checks Kalshi's status every {_pollSec:0}s and resumes by itself.");
        }
        else
        {
            Con.Line(ConsoleColor.Yellow,
                $"[HOLD ] {n} consecutive order failure(s) ({tag}); {said}. "
              + $"Holding orders for {sec / 60:0.0} min (until {until:HH:mm:ss}Z); screening continues.");
            if (announce)
                Say($"⏸️ Kalshi not taking orders ({tag}); {said}. Holding orders {sec / 60:0.0} min, "
                  + "then one test order (the wait doubles while it keeps failing, up to 10 min).");
        }
    }

    /// <summary>An order the venue accepted, filled or not: it is open, so any hold ends.</summary>
    public void OnAccepted()
    {
        Closure? ended = null;
        long held = 0;
        bool wasFailing = false;
        lock (_lock)
        {
            if (_closed) ended = EndClosureLocked(_now(), "an order went through");
            else if (_why.Length > 0)
            {
                wasFailing = true;
                held = _held - _heldAtLastNote;
                _heldAtLastNote = _held;
                _why = "";
            }
            _consecutive = 0;
            _backoffSec  = 60;
            _until       = DateTime.MinValue;
        }
        if (ended is { } e) Announce(e);
        else if (wasFailing)
        {
            Con.Line(ConsoleColor.Green, $"[HOLD ] venue accepting orders again ({held} signal(s) were skipped during the hold).");
            Say($"✅ Kalshi accepting orders again ({held} signal(s) skipped meanwhile, not queued).");
        }
    }

    /// <summary>Shutdown while closed: record the closure up to now, so --resolve still sets its rows aside.</summary>
    public void CloseOpen()
    {
        Closure? ended;
        lock (_lock) ended = _closed ? EndClosureLocked(_now(), "the bot stopped") : null;
        if (ended is not { } e) return;
        Write(e);
        Con.Line(ConsoleColor.Yellow, $"[HOLD ] stopping while Kalshi is closed - closure {e.Start:HH:mm:ss}-{e.End:HH:mm:ss}Z recorded.");
    }

    /// <summary>Caller holds <see cref="_lock"/>. Clears every piece of hold state and returns what to record.</summary>
    private Closure EndClosureLocked(DateTime end, string how)
    {
        var c = new Closure(_closedSince, end, _why, _consecutive, _held - _heldAtLastNote, how);
        _heldAtLastNote = _held;
        _closed      = false;
        _why         = "";
        _consecutive = 0;
        _backoffSec  = 60;
        _until       = DateTime.MinValue;
        return c;
    }

    private void Announce(Closure c)
    {
        Write(c);
        Con.Line(ConsoleColor.Green,
            $"[HOLD ] Kalshi is trading again ({c.EndedBy}) - orders resume. Closed {c.Start:HH:mm:ss}-{c.End:HH:mm:ss}Z: "
          + $"{c.Refused} order(s) refused, {c.Held} signal(s) skipped (never queued).");
        Say($"✅ Kalshi trading again at {c.End:HH:mm} UTC, orders resume. While it was closed: {c.Refused} order(s) refused, "
          + $"{c.Held} signal(s) skipped (dropped, not queued).");
    }

    private void Say(string m) { try { Notify?.Invoke(m); } catch { } }

    private void Write(Closure c)
    {
        if (ClosuresPath is not { Length: > 0 } path) return;
        try
        {
            string line = JsonSerializer.Serialize(new
            {
                start    = c.Start.ToString("o", CultureInfo.InvariantCulture),
                end      = c.End.ToString("o", CultureInfo.InvariantCulture),
                reason   = c.Reason,
                refused  = c.Refused,
                held     = c.Held,
                ended_by = c.EndedBy,
            });
            lock (FileLock) File.AppendAllText(path, line + "\n");
        }
        catch (Exception ex)
        {
            Con.Line(ConsoleColor.DarkYellow,
                $"[HOLD ] could not record the closure ({ex.GetType().Name}) - --resolve will not set its signals aside.");
        }
    }

    /// <summary>Every recorded closure as (start, end) in UTC, oldest first. Unreadable lines are skipped; a
    /// missing file means none.</summary>
    public static List<(DateTime Start, DateTime End)> Load(string? path = null)
    {
        var list = new List<(DateTime Start, DateTime End)>();
        path ??= DefaultPath;
        if (!File.Exists(path)) return list;
        foreach (string line in File.ReadAllLines(path))
        {
            if (string.IsNullOrWhiteSpace(line)) continue;
            try
            {
                using var doc = JsonDocument.Parse(line);
                var r = doc.RootElement;
                if (Utc(r, "start") is { } s && Utc(r, "end") is { } e && e >= s) list.Add((s, e));
            }
            catch (JsonException) { }
        }
        list.Sort();
        return list;
    }

    /// <summary>True when <paramref name="at"/> falls inside any of <paramref name="closures"/> (ends included).</summary>
    public static bool Contains(IReadOnlyList<(DateTime Start, DateTime End)> closures, DateTime at)
    {
        if (at == DateTime.MinValue || closures.Count == 0) return false;
        var u = at.Kind == DateTimeKind.Local ? at.ToUniversalTime() : DateTime.SpecifyKind(at, DateTimeKind.Utc);
        foreach (var (s, e) in closures)
            if (u >= s && u <= e) return true;
        return false;
    }

    private static DateTime? Utc(JsonElement r, string k)
        => r.TryGetProperty(k, out var v) && v.ValueKind == JsonValueKind.String
           && DateTime.TryParse(v.GetString(), CultureInfo.InvariantCulture,
                                DateTimeStyles.AdjustToUniversal | DateTimeStyles.AssumeUniversal, out var t)
           ? t : null;
}
