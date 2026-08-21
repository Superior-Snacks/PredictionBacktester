using System.Collections.Concurrent;
using System.Globalization;
using System.Net.Http;
using System.Text;
using System.Text.Json;
using PredictionBacktester.Engine;

namespace HardVenArb;

/// <summary>Outcome of pressing an armed camp. Distinguishes the three states the sidecar reports, because
/// they demand three different responses and collapsing them is how a naked leg gets created.</summary>
public sealed record CampFireResult(
    bool    Placed,          // confirmed on the account — safe to hedge against
    decimal Odds,            // the decimal odds the bet was ACCEPTED at (not the armed price)
    decimal StakeAccount,    // account-currency stake actually staked
    string  BetId,
    bool    Ambiguous,       // pressed, but acceptance never confirmed — state UNKNOWN, must NOT be hedged
    string  Reason);

/// <summary>
/// The in-play camp brain: decides WHERE to park the one live Pinnacle tab's armed betslip, when to move it,
/// and when to give up and go back to browsing. The sidecar owns the mechanics (<c>/camp/start</c>,
/// <c>/camp/fire</c>, <c>/camp/status</c>, <c>/camp/stop</c>); this owns the policy.
///
/// <para><b>Why camping at all.</b> Measured over 206 in-play windows on 2026-08-16: they came from just 13
/// pairs, NOT ONE produced a single isolated arb, 94% of windows were a repeat on a pair already seen, and the
/// median gap to the next window on the same pair was 41s. So in-play opportunity is concentrated and
/// recurring — which means the expensive part of execution (navigate → find the row → click → type → confirm,
/// seconds long while an in-play line moves under it) can be paid ONCE, up front, instead of inside every
/// window. Pre-arming turns the in-play book leg into a single press. That is the only reason in-play is
/// reachable at all.</para>
///
/// <para><b>The lifecycle</b>, which is deliberately simple:</para>
/// <code>
///   ROVING   browse the live list calmly, camp nothing. The startup state.
///     │  first in-play arb window opens          → arm on THAT moneyline
///   CAMPED   armed slip held; the next window on it costs one press
///     │  a window opens on the armed selection   → the executor presses (see FireAsync)
///     │  another game scores clearly better      → relocate (needs a MARGIN, see below)
///     │  nothing happens for ~10 min             → release, back to ROVING
///     │  the popover died under us               → back to ROVING
/// </code>
///
/// <para><b>Scoring, and what "a game with especially good arb holds" means.</b> A window is only worth
/// camping for if it could actually have been TAKEN, so score is built from closes, not opens: when a window
/// closes, if it lived at least <c>HARDVEN_CAMP_MIN_HOLD_MS</c> (a press is ~1s of UI, so anything shorter was
/// never catchable however fast the camp was), it contributes <c>edge × depth</c> — the dollars that were
/// genuinely on the table — decayed with a half-life. The result is a per-pair estimate of recent catchable
/// money per unit time, which is exactly the quantity a camp should be maximising. Frequency, edge size, depth
/// and hold length all move it in the right direction without needing four separate weights.</para>
///
/// <para><b>Relocation needs a margin, not just a lead.</b> Switching costs a navigation, a re-arm, and a gap
/// in coverage on a pair that has been producing. So a challenger must beat the incumbent by
/// <c>HARDVEN_CAMP_SWITCH_MARGIN</c>× (default 2×) and the incumbent must have had a minimum tenure. Without
/// both, two comparable games trade the camp back and forth and neither is ever actually armed when a window
/// opens.</para>
/// </summary>
public sealed class CampManager
{
    // ── collaborators ─────────────────────────────────────────────────────────
    private readonly string _sidecar;
    private readonly CrossPlatformArbTelemetryStrategy _telemetry;
    private readonly ConcurrentDictionary<string, LocalOrderBook> _books;
    private readonly DiscordNotifier _discord;
    private readonly bool _previewOnly;      // dry-run: arm and relocate for real, never press Place
    private readonly decimal _arbThreshold;  // net cost a window opens under — the baseline edge is measured from

    /// <summary>Called when a press could not be confirmed either way. Wired to the executor's hard halt: a
    /// bet that MAY be live and is definitely unhedged must stop the bot, not be absorbed as a free miss.</summary>
    private Action<string>? _onUnconfirmed;
    public void SetUnconfirmedHandler(Action<string>? h) => _onUnconfirmed = h;

    // ── FIRE-FIRST (HARDVEN_CAMP_FIRE_FIRST=1) ───────────────────────────────────────────────────────
    // Camping was built on the premise that the UI drive is too slow to do inside a window, so the cost is
    // paid once up front and the window costs one press. That premise is worth testing rather than assuming:
    // press->confirm measured 10.9-11.7s, and 79% of in-play windows hold their entry price past 11s — so an
    // arb detected NOW may well still be there after an arm.
    //
    // This does not replace camping, it front-runs it. Arm as usual, then immediately ask whether the arb is
    // still on; if it is, take it. If the price moved on the way, the arm has still happened and the camp
    // behaves exactly as before — so the worst case is the current behaviour, not a lost window.
    //
    // The re-check is the executor's, not ours: it owns the ladder, the balance, the Kalshi depth and the
    // floor, and a second opinion computed here would be a second definition of "is this an arb".
    // Carries the ARMED SLIP's price with it. The book is not the authority for the Pinnacle leg here —
    // the panel is, which is the whole reason the periodic book re-seed was switched off in-play. Passing
    // the slip price means fire-first works on a market whose BOOK is stale or missing entirely, which on
    // 2026-08-21 was 98 of 104 of them.
    private Func<string, string, decimal, CancellationToken, Task>? _onArmedTryFire;
    /// <summary>Called right after a camp arms, when fire-first is on. Wired to the executor's own
    /// re-evaluation of that pair, so nothing here decides what counts as an arb.</summary>
    public void SetArmedTryFireHandler(Func<string, string, decimal, CancellationToken, Task>? h)
        => _onArmedTryFire = h;

    // ── MOVE FOR A LIVE, TAKEABLE ARB ────────────────────────────────────────────────────────────────
    // The score decides where to SIT; this decides when to GO. The executor calls it when an in-play arb
    // clears the execution floor on a game the camp is not on — the one situation where abandoning a hold
    // is obviously right, because there is a tradeable edge on screen and the camp is pointed elsewhere.
    //
    // Bypasses the 2x score margin and the minimum dwell on purpose: both exist to stop the camp chasing
    // NOISE, and a floor-clearing arb is the opposite of noise. It does NOT bypass the arming machinery —
    // the move goes through the ordinary path, so verification, the placeability probe and fire-first all
    // still run.
    //
    // Rate-limited per pair so a market that sits below the floor for a minute cannot produce a stream of
    // moves; and refused outright while a press is in flight, because relocating mid-fire would clear the
    // slip being pressed.
    private readonly ConcurrentDictionary<string, DateTime> _moveAsk = new();
    public void RequestMove(string pairId, string token, string label, string arbType, decimal net)
    {
        if (!FireFirst) return;               // the move only pays if arriving means trying immediately
        if (Phase is CampPhase.Off or CampPhase.Firing or CampPhase.Arming) return;
        if (_campToken == token) return;      // already here
        var now = DateTime.UtcNow;
        if (_moveAsk.TryGetValue(pairId, out var last) && (now - last).TotalSeconds < MoveCooldownSec) return;
        _moveAsk[pairId] = now;
        Console.ForegroundColor = ConsoleColor.Cyan;
        Console.WriteLine($"[CAMP] MOVE: {label} is showing a takeable arb (net {net:0.0000}) and the camp is " +
                          $"{(AnyCampArmed ? $"on {_campLabel}" : "roving")} — going there now rather than " +
                          $"waiting for it to out-score over ten minutes.");
        Console.ResetColor();
        _ = Task.Run(async () =>
        {
            try
            {
                if (AnyCampArmed) await ReleaseAsync("moving to a takeable arb", CancellationToken.None);
                bool claimed = false;
                lock (_lock) { if (_phase == CampPhase.Roving) { _phase = CampPhase.Arming; claimed = true; } }
                if (claimed)
                    await ArmAsync(pairId, token, label, arbType, "takeable arb on this game",
                                   CancellationToken.None);
            }
            catch (Exception ex)
            {
                Console.WriteLine($"[CAMP] move to {label} failed ({ex.GetType().Name}: {ex.Message}) — " +
                                  $"the camper carries on as before.");
                lock (_lock) { if (_phase == CampPhase.Arming) _phase = CampPhase.Roving; }
            }
        });
    }
    private static readonly int MoveCooldownSec =
        EnvInt("HARDVEN_CAMP_MOVE_COOLDOWN_SEC", 45);
    private static readonly bool FireFirst =
        (Environment.GetEnvironmentVariable("HARDVEN_CAMP_FIRE_FIRST") ?? "0").Trim() == "1";

    // Arming drives the UI (find the row, click, type) and can run for tens of seconds on a scroll-miss, so it
    // gets its own long-timeout client. Status/stop are instant reads and must not be stuck behind an arm.
    private readonly HttpClient _uiHttp   = new() { Timeout = TimeSpan.FromSeconds(90) };
    private readonly HttpClient _fastHttp = new() { Timeout = TimeSpan.FromSeconds(15) };
    // Firing has its own client: camp_fire waits up to 15s for /bets/straight and then polls the account's bet
    // list to establish acceptance (the POST answers PENDING_ACCEPTANCE with no bet id). Timing out the HTTP
    // call underneath that would leave a placed bet with no record on this side — the exact failure that cost
    // a leg on 2026-08-12.
    private readonly HttpClient _fireHttp = new() { Timeout = TimeSpan.FromSeconds(120) };

    // ── tunables ──────────────────────────────────────────────────────────────
    private readonly decimal _armStake;       // account-currency stake typed at arm time (fire re-types if sized differently)
    private readonly double  _halfLifeSec;
    private readonly long    _minHoldMs;
    private readonly double  _switchMargin;
    private readonly int     _minTenureSec;
    private readonly int     _idleSec;
    private readonly int     _maxSec;
    private readonly int     _rearmSec;
    private readonly int     _healthSec;
    private readonly double  _depthCapContracts;
    // Consecutive not-tradeable health checks before a camp is declared dead. At the default 30s cadence,
    // 3 is ~90s of a market being offline — far longer than a between-points suspension, short enough that a
    // genuinely dead game does not hold the tab for the full idle budget.
    private readonly int     _deadChecks;

    /// <summary>Mirrors the executor's HARDVEN_MONEYLINE_ONLY gate, because a camp on a leg the executor will
    /// never place is worse than no camp at all: the slip holds, the game produces windows, every one of them
    /// is skipped as a derivative, and the idle clock keeps resetting on that activity — so the camp sits there
    /// looking busy and productive while being structurally incapable of a single bet. Derivative pairs are
    /// loaded into the SAME pair list as moneylines (derivative_pairs.json is merged in), so this is not a
    /// theoretical case; on a tennis slate the spread/total lines outnumber the moneylines.</summary>
    private readonly bool _moneylineOnly = Environment.GetEnvironmentVariable("HARDVEN_MONEYLINE_ONLY") != "0";

    // ── state (all under _lock) ───────────────────────────────────────────────
    private readonly object _lock = new();
    private CampPhase _phase = CampPhase.Off;
    private string    _campPairId = "";
    private string    _campToken  = "";      // the Pinnacle selection_id actually armed
    private string    _campLabel  = "";
    private string    _campArbType = "";
    private DateTime  _armedAt;
    private DateTime  _lastActionAt;         // arm, fire attempt, or a window opening on the camped selection
    private DateTime  _lastHealthAt;
    private int       _idleBudgetSec;        // _idleSec ± jitter, re-rolled per camp
    // Consecutive health checks that found the armed panel present but NOT tradeable. In-play tennis
    // suspends between points constantly, so one bad read proves nothing — only a run of them does.
    private int       _untradeableStreak;
    private DateTime  _rearmAfter = DateTime.MaxValue;
    private string    _rearmPairId = "";

    private int _armCount, _relocCount, _releaseCount, _lostCount, _fireCount, _placedCount, _ambiguousCount;
    private int _belowFloorCount;   // fills the venue booked under the arb's floor — see FireAsync

    // pairId → decayed catchable-money score
    private readonly ConcurrentDictionary<string, PairScore> _scores = new(StringComparer.Ordinal);
    // Windows still open, keyed pairId|arbType, so the close can be priced against what it opened at.
    private readonly ConcurrentDictionary<string, PendingWindow> _pending = new(StringComparer.Ordinal);

    // Only one sidecar camp operation at a time. Arm/relocate/release all drive the same single tab, and two
    // of them interleaved produce a camp whose recorded state and actual state disagree.
    private readonly SemaphoreSlim _gate = new(1, 1);
    private readonly Random _rng = new();
    private CancellationTokenSource? _cts;
    private Task? _supervisor;

    public enum CampPhase { Off, Roving, Arming, Camped, Firing }

    private sealed class PairScore
    {
        public string   Label = "";
        public double   Value;               // decayed $ of catchable edge
        public DateTime ValueAt = DateTime.UtcNow;
        public int      Opens, Takeable;
        public DateTime LastOpen = DateTime.MinValue;
        // Which HardVen leg the arbs on this pair keep buying. Repeats DO switch sides (only 1 of 13 pairs
        // stayed on one), but a dominant side runs 70-88% — so a re-arm follows the tally rather than the
        // most recent window, and the minority case stays cheap: parked on the game, the other cell is a
        // click away rather than a navigation.
        public int      YesSideOpens, NoSideOpens;
    }

    private readonly record struct PendingWindow(string PairId, string ArbType, double Edge, double Depth, DateTime OpenedAt);

    public CampManager(
        string sidecarBaseUrl,
        CrossPlatformArbTelemetryStrategy telemetry,
        ConcurrentDictionary<string, LocalOrderBook> books,
        DiscordNotifier discord,
        decimal arbThreshold,
        bool previewOnly)
    {
        _sidecar      = (sidecarBaseUrl ?? "").TrimEnd('/');
        _telemetry    = telemetry;
        _books        = books;
        _discord      = discord;
        _arbThreshold = arbThreshold;
        _previewOnly  = previewOnly;

        _armStake    = EnvDec("HARDVEN_CAMP_ARM_STAKE", StakeLadder.MinRung);
        _halfLifeSec = (double)EnvDec("HARDVEN_CAMP_SCORE_HALFLIFE_SEC", 600m);
        _minHoldMs   = EnvInt("HARDVEN_CAMP_MIN_HOLD_MS", 1500);
        _switchMargin = (double)EnvDec("HARDVEN_CAMP_SWITCH_MARGIN", 2.0m);
        _minTenureSec = EnvInt("HARDVEN_CAMP_MIN_TENURE_SEC", 120);
        _idleSec      = EnvInt("HARDVEN_CAMP_IDLE_SEC", 600);
        _maxSec       = EnvInt("HARDVEN_CAMP_MAX_SEC", 3600);
        _rearmSec     = EnvInt("HARDVEN_CAMP_REARM_SEC", 20);
        _healthSec    = EnvInt("HARDVEN_CAMP_HEALTH_SEC", 30);
        _depthCapContracts = (double)EnvDec("HARDVEN_CAMP_DEPTH_CAP", 50m);
        _deadChecks        = EnvInt("HARDVEN_CAMP_DEAD_CHECKS", 3);
    }

    // ── public surface ────────────────────────────────────────────────────────

    /// <summary>The Pinnacle selection currently armed, or "" when nothing is. Read by the order client to
    /// decide press-vs-drive, so it must reflect the sidecar's real state and never a stale intention.</summary>
    public string ArmedToken { get { lock (_lock) return _phase == CampPhase.Camped || _phase == CampPhase.Firing ? _campToken : ""; } }

    public bool IsArmedOn(string token) =>
        !string.IsNullOrEmpty(token) && string.Equals(ArmedToken, token, StringComparison.Ordinal);

    /// <summary>True while a camp exists at all — used to refuse UI-driving bets that would navigate the one
    /// live tab and take the armed slip with it.</summary>
    public bool AnyCampArmed => ArmedToken.Length > 0;

    public CampPhase Phase { get { lock (_lock) return _phase; } }

    /// <summary>Put the sidecar into in-play mode (one live tab, tab manager held, camp-aware idle) and start
    /// the supervisor. ROVING from here: nothing is camped until the first in-play window opens.</summary>
    public async Task<bool> StartAsync(CancellationToken ct)
    {
        var (ok, body) = await PostAsync(_fastHttp, "/inplay/start", null, ct);
        if (!ok)
        {
            Console.ForegroundColor = ConsoleColor.Red;
            Console.WriteLine($"[CAMP] could not enter in-play mode — {body}. Camping is OFF; the bot will run " +
                              "pre-live as usual.");
            Console.ResetColor();
            return false;
        }
        lock (_lock) { _phase = CampPhase.Roving; }
        _cts = CancellationTokenSource.CreateLinkedTokenSource(ct);
        _supervisor = Task.Run(() => SupervisorLoopAsync(_cts.Token));
        Console.ForegroundColor = ConsoleColor.Cyan;
        Console.WriteLine($"[CAMP] IN-PLAY mode: roving. Will arm on the first in-play arb, then hold it. " +
                          $"arm stake={_armStake:0.##} · idle release ~{_idleSec}s · relocate at {_switchMargin:0.#}x " +
                          $"after {_minTenureSec}s · hard cap {_maxSec}s");
        Console.ResetColor();
        return true;
    }

    public async Task StopAsync()
    {
        try { _cts?.Cancel(); } catch { }
        if (_supervisor != null) { try { await _supervisor; } catch { } }
        // Release the slip before handing the browser back: leaving an armed Quick Bet behind after shutdown
        // means a stake sitting on a live game with nothing watching it.
        if (AnyCampArmed) { try { await ReleaseAsync("shutdown", CancellationToken.None, force: true); } catch { } }
        try { await PostAsync(_fastHttp, "/inplay/stop", null, CancellationToken.None); } catch { }
        lock (_lock) { _phase = CampPhase.Off; }
    }

    // ── telemetry hooks ───────────────────────────────────────────────────────

    /// <summary>Wire to <c>telemetry.OnArbOpened</c>. Runs on the feed thread — records state and hands any
    /// sidecar work to a background task, never blocks the book update.</summary>
    public void OnArbOpened(string pairId, decimal netCost, string arbType, decimal depth,
                            decimal kLegAsk, decimal pLegAsk)
    {
        if (Phase == CampPhase.Off) return;
        var pair = _telemetry.GetPair(pairId);
        if (pair == null) return;
        string token = TokenFor(pair, arbType);
        if (token.Length == 0) return;
        // IN-PLAY ONLY (a camp is a live-tab construct; a pre-match window is executed the normal way), and
        // PLACEABLE ONLY — a derivative window is real money that was on the table and completely unreachable,
        // so scoring it would rank a spread-heavy match above one the bot can actually trade.
        if (!IsCampable(token)) return;

        double edge = Math.Max(0d, (double)(_arbThreshold - netCost));
        _pending[$"{pairId}|{arbType}"] = new PendingWindow(pairId, arbType, edge, (double)depth, DateTime.UtcNow);

        var sc = _scores.GetOrAdd(pairId, _ => new PairScore { Label = pair.Label });
        lock (sc)
        {
            sc.Label = pair.Label;
            sc.Opens++;
            sc.LastOpen = DateTime.UtcNow;
            if (arbType == "K_YES_P_NO") sc.NoSideOpens++; else sc.YesSideOpens++;
        }

        bool armNow = false;
        lock (_lock)
        {
            if (_phase == CampPhase.Roving)
            {
                armNow = true;
                _phase = CampPhase.Arming;      // claim it here so two simultaneous opens can't both arm
            }
            else if (_phase == CampPhase.Camped && pairId == _campPairId)
            {
                // The camp is doing its job even if the executor's gates end up refusing this particular
                // window — the game is producing. That is "action" for the idle clock.
                _lastActionAt = DateTime.UtcNow;
            }
        }
        if (armNow)
            _ = Task.Run(() => ArmAsync(pairId, token, pair.Label, arbType, "first in-play arb",
                                        _cts?.Token ?? CancellationToken.None));
    }

    /// <summary>Wire to <c>telemetry.OnArbClosed</c>. The close is where a window's HOLD becomes known, and the
    /// hold is what decides whether it was ever catchable — so this, not the open, is what feeds the score.</summary>
    public void OnArbClosed(string pairId, string arbType, long durationMs, bool inPlay)
    {
        if (Phase == CampPhase.Off) return;
        if (!_pending.TryRemove($"{pairId}|{arbType}", out var w)) return;
        if (!inPlay) return;
        var sc = _scores.GetOrAdd(pairId, _ => new PairScore());
        // A window shorter than one press was never takeable, however well-placed the camp was. Counting it
        // would rank a game that flickers 40 unreachable windows above one that holds three real ones.
        if (durationMs < _minHoldMs) return;
        double catchable = w.Edge * Math.Min(w.Depth, _depthCapContracts);
        if (catchable <= 0d) return;
        lock (sc)
        {
            sc.Value = Decayed(sc.Value, sc.ValueAt) + catchable;
            sc.ValueAt = DateTime.UtcNow;
            sc.Takeable++;
        }
    }

    // ── the money press ───────────────────────────────────────────────────────

    /// <summary>Press PLACE BET on the armed slip. Called by <see cref="HardVenOrderClient"/> in place of the
    /// UI drive when the leg being bought is the one already armed.
    ///
    /// <para><paramref name="minOdds"/> is a FLOOR, not a target. The panel re-quotes continuously and Place
    /// stays enabled through the change, so a press accepts whatever is current — higher decimal odds favour a
    /// backer, so anything at or above the price the arb was sized on is fine, and below it the edge the trade
    /// was justified by no longer exists. The sidecar applies the same floor again to the odds-changed
    /// re-prompt, so a move against us between press and submit is declined rather than accepted.</para></summary>
    public async Task<CampFireResult> FireAsync(string token, decimal minOdds, decimal stakeAccount,
                                                CancellationToken ct = default)
    {
        if (!IsArmedOn(token))
            return new CampFireResult(false, 0m, 0m, "", false, "no camp armed on this selection");
        if (_previewOnly)
            return new CampFireResult(false, 0m, 0m, "", false,
                "dry run — the camp is armed and would have been pressed, but preview mode places nothing");

        lock (_lock)
        {
            if (_phase != CampPhase.Camped)
                return new CampFireResult(false, 0m, 0m, "", false, $"camp is {_phase}, not pressable");
            _phase = CampPhase.Firing;
            _lastActionAt = DateTime.UtcNow;
            _fireCount++;
        }

        string payload = JsonSerializer.Serialize(new
        {
            min_odds = Math.Round(minOdds, 4),
            stake    = stakeAccount > 0m ? (decimal?)Math.Round(stakeAccount, 2) : null,
            confirm  = "yes",
        });

        string pairId, label;
        lock (_lock) { pairId = _campPairId; label = _campLabel; }

        try
        {
            var (ok, body) = await PostAsync(_fireHttp, "/camp/fire", payload, ct);
            // camp_fire ALWAYS releases the camp in its finally — the slip is consumed by a placement, and a
            // failed press leaves a panel we no longer know the state of. So the camp is gone either way.
            ClearCampAfterFire(pairId);

            if (!ok)
                return new CampFireResult(false, 0m, 0m, "", false, $"/camp/fire failed: {Truncate(body)}");

            using var doc = JsonDocument.Parse(body);
            var root = doc.RootElement;
            bool okFlag    = Flag(root, "ok");
            bool fired     = Flag(root, "fired");
            bool confirmed = Flag(root, "confirmed");
            bool accepted  = Flag(root, "accepted");
            string reason  = Str(root, "error");
            decimal odds   = Dec(root, "odds");
            decimal stake  = Dec(root, "stake");
            string betId   = Str(root, "bet_id");
            // WHICH ROUTE ESTABLISHED THIS. "bets-list" is the page's own GET /0.1/bets; "dom-receipt" is
            // the fallback reading a bet id straight off the panel, used when the bet list did not answer.
            // Both are real confirmations, but they are not equally well understood - the DOM route has no
            // venue-reported PRICE behind it, so `odds` there is the panel price we pressed against rather
            // than a reported fill. Printed so a run that quietly starts leaning on the fallback is visible
            // in the log instead of looking identical to a normal fire.
            string cSource = Str(root, "confirm_source");

            if (okFlag && accepted)
            {
                Interlocked.Increment(ref _placedCount);
                Console.ForegroundColor = ConsoleColor.Green;
                Console.WriteLine($"[CAMP FIRE] {label} · {stake:0.##} @ {odds:0.000} · bet {betId}"
                                + (string.IsNullOrEmpty(cSource) || cSource == "bets-list"
                                       ? "" : $" · confirmed via {cSource}"));
                if (cSource == "dom-receipt")
                    Console.WriteLine($"[CAMP FIRE] {label}: the account bet list never answered — this was " +
                                      $"confirmed off the betslip panel. The bet IS on (the id is the venue's " +
                                      $"own), but {odds:0.000} is the price we pressed against, not a reported " +
                                      $"fill. Reconcile it against My Bets.");
                Console.ResetColor();

                // THE FLOOR IS NOT ENFORCED AT THE VENUE ON THIS PATH. `/bet` sends max_odds and Pinnacle
                // itself refuses a worse price; a camp press sends nothing — it clicks a button and takes
                // what the venue books. Observed 2026-08-19: floor 1.745, booked 1.581, and every local
                // check had passed. So the only defence left is to notice afterwards and STOP, because a
                // second fill on the same broken assumption is the same loss again.
                if (odds > 0m && minOdds > 0m && odds < minOdds - 0.0001m)
                {
                    Interlocked.Increment(ref _belowFloorCount);
                    Console.ForegroundColor = ConsoleColor.Red;
                    Console.WriteLine($"[CAMP FIRE ⚠] {label}: BOOKED AT {odds:0.000}, BELOW THE {minOdds:0.000} " +
                                      $"floor the arb was sized on. The pre-press check passed, so the venue " +
                                      $"priced this after the click — the local floor cannot protect an in-play " +
                                      $"fill. Halting rather than repeating it.");
                    Console.ResetColor();
                    _ = _discord.AlertAsync($"🚨 **CAMP FILLED BELOW FLOOR** — {label}\n" +
                                            $"booked {odds:0.000} vs floor {minOdds:0.000}. The venue re-priced " +
                                            $"after the press. Trading halted.");
                    _onUnconfirmed?.Invoke($"{label}: booked {odds:0.000} below the {minOdds:0.000} floor");
                }
                return new CampFireResult(true, odds, stake, betId, false, "accepted");
            }

            // PRESSED BUT UNCONFIRMED. The sidecar's own instruction is do NOT hedge against this, and it is
            // right: a hedge placed against a bet that does not exist is a naked directional position, which is
            // strictly worse than an unhedged book leg we know about. So this returns "not placed" — no Kalshi
            // order is sent — and shouts, because it is the one outcome that needs a human to reconcile.
            if (fired && !confirmed)
            {
                Interlocked.Increment(ref _ambiguousCount);
                Console.ForegroundColor = ConsoleColor.Red;
                Console.WriteLine($"[CAMP FIRE ⚠] {label}: PLACE BET was pressed and acceptance was never " +
                                  $"confirmed — {reason}. NO Kalshi hedge was sent. Reconcile this against " +
                                  $"My Bets by hand before the game settles.");
                Console.ResetColor();
                _ = _discord.AlertAsync($"⚠️ **CAMP FIRE UNCONFIRMED** — {label}\nPressed Place, no confirmation " +
                                        $"({reason}). No hedge sent. **Check My Bets manually.**");
                _onUnconfirmed?.Invoke($"{label}: {reason}");
                return new CampFireResult(false, odds, stake, betId, true, $"unconfirmed: {reason}");
            }

            // Clean refusal — declined on a moved price, Place disabled below the venue minimum, slip gone.
            // Nothing was placed, so this is a free miss and the executor's book-first path sends no Kalshi leg.
            Console.WriteLine($"[CAMP FIRE] {label}: no bet — {reason}");
            return new CampFireResult(false, odds, stake, betId, false, reason.Length > 0 ? reason : "not fired");
        }
        catch (Exception ex)
        {
            // A timeout here is NOT proof nothing was placed: camp_fire may still be waiting on the account's
            // bet list. Treat it as ambiguous for exactly the same reason as above.
            ClearCampAfterFire(pairId);
            Interlocked.Increment(ref _ambiguousCount);
            Console.ForegroundColor = ConsoleColor.Red;
            Console.WriteLine($"[CAMP FIRE ⚠] {label}: {ex.GetType().Name} talking to the sidecar mid-press " +
                              $"({ex.Message}) — the bet MAY be live. No hedge sent.");
            Console.ResetColor();
            _ = _discord.AlertAsync($"⚠️ **CAMP FIRE UNKNOWN** — {label}\n{ex.GetType().Name}: {ex.Message}. " +
                                    "No hedge sent. **Check My Bets manually.**");
            _onUnconfirmed?.Invoke($"{label}: {ex.GetType().Name} mid-press");
            return new CampFireResult(false, 0m, 0m, "", true, $"sidecar error mid-press: {ex.Message}");
        }
    }

    private void ClearCampAfterFire(string pairId)
    {
        lock (_lock)
        {
            _phase       = CampPhase.Roving;
            _campToken   = ""; _campPairId = ""; _campLabel = ""; _campArbType = "";
            // The tape says repeats keep coming (median gap 41s), so the pair that just produced is usually
            // still the best target. Re-arm shortly rather than waiting for the next window to be detected —
            // otherwise the very next repeat pays the full navigate-find-click cost the camp exists to avoid.
            _rearmAfter  = DateTime.UtcNow.AddSeconds(_rearmSec);
            _rearmPairId = pairId;
        }
    }

    // ── supervisor ────────────────────────────────────────────────────────────

    private async Task SupervisorLoopAsync(CancellationToken ct)
    {
        while (!ct.IsCancellationRequested)
        {
            try
            {
                await Task.Delay(5_000, ct);
                await TickAsync(ct);
            }
            catch (OperationCanceledException) { return; }
            catch (Exception ex)
            {
                Console.WriteLine($"[CAMP] supervisor: {ex.GetType().Name}: {ex.Message}");
                try { await Task.Delay(15_000, ct); } catch { return; }
            }
        }
    }

    private async Task TickAsync(CancellationToken ct)
    {
        CampPhase phase; string pairId, label, token; DateTime armedAt, lastAction, lastHealth; int idleBudget;
        DateTime rearmAfter; string rearmPair;
        lock (_lock)
        {
            phase = _phase; pairId = _campPairId; label = _campLabel; token = _campToken;
            armedAt = _armedAt; lastAction = _lastActionAt; lastHealth = _lastHealthAt;
            idleBudget = _idleBudgetSec; rearmAfter = _rearmAfter; rearmPair = _rearmPairId;
        }
        var now = DateTime.UtcNow;

        if (phase == CampPhase.Roving)
        {
            // The only thing ROVING does on a timer is honour a pending re-arm after a fire. Otherwise it waits
            // for a window to open, which is what the user asked for: browse calmly, camp on the next arb.
            if (now >= rearmAfter && rearmPair.Length > 0)
            {
                lock (_lock) { _rearmAfter = DateTime.MaxValue; _rearmPairId = ""; }
                var best = BestTarget();
                string target = best.PairId.Length > 0 ? best.PairId : rearmPair;
                var pair = _telemetry.GetPair(target);
                if (pair != null)
                {
                    string arbType = DominantArbType(target);
                    string tok = TokenFor(pair, arbType);
                    if (tok.Length > 0 && IsCampable(tok))
                    {
                        bool claimed = false;
                        lock (_lock) { if (_phase == CampPhase.Roving) { _phase = CampPhase.Arming; claimed = true; } }
                        if (claimed)
                            await ArmAsync(target, tok, pair.Label, arbType,
                                           target == rearmPair ? "re-arm after fire" : "re-arm on the best target", ct);
                    }
                }
            }
            return;
        }
        if (phase != CampPhase.Camped) return;   // Arming / Firing own themselves

        // ── health: is the popover actually still there? ──────────────────────
        // camp_status READS the DOM rather than echoing the flag written at arm time — that distinction is the
        // whole value of the check. An earlier version reported armed:true for 13 minutes on a camp the page
        // had already navigated away from, which on a money path means pressing Place on whatever is there.
        if ((now - lastHealth).TotalSeconds >= _healthSec)
        {
            lock (_lock) { _lastHealthAt = now; }
            var (ok, body) = await GetAsync(_fastHttp, "/camp/status", ct);
            bool alive = false, camping = false; string lost = "";
            bool? tradeable = null; string whyNot = "";
            if (ok)
            {
                try
                {
                    using var doc = JsonDocument.Parse(body);
                    camping = Flag(doc.RootElement, "camping");
                    alive   = camping && Flag(doc.RootElement, "armed");
                    lost    = Str(doc.RootElement, "lost");
                    whyNot  = Str(doc.RootElement, "why");
                    if (doc.RootElement.TryGetProperty("tradeable", out var tv) &&
                        (tv.ValueKind == JsonValueKind.True || tv.ValueKind == JsonValueKind.False))
                        tradeable = tv.ValueKind == JsonValueKind.True;

                    // ── IS THE ARMED SLIP KEEPING UP WITH THE FEED? ───────────────────────────────
                    // The open question behind the whole camp design: an armed Quick Bet might re-quote
                    // more slowly than the board, in which case the press is committing to a laggy price
                    // no matter how carefully it is re-read. Nothing measured it, because the slip
                    // verifier (which does exactly this comparison) is REFUSED while camping — a second
                    // popover would destroy the camp.
                    //
                    // But the health check already fetches the panel price every 30s, and the WS book for
                    // the same token is sitting in memory. Comparing two numbers we already hold costs
                    // nothing and no venue traffic at all.
                    decimal slipOdds = Dec(doc.RootElement, "price");
                    // THE PRICE BEING UNREADABLE IS ITSELF A RESULT, and it used to produce no line at all —
                    // the comparison below simply skipped and the camp looked healthy. Two very different
                    // causes hide behind it: the market is suspended (normal in-play, and the dead-market
                    // streak will handle it), or the panel changed shape and _CAMP_PRICE_RX no longer
                    // matches — which silently disables the floor check that stands between us and a bad
                    // fill. Printing the panel text is what tells them apart.
                    if (slipOdds <= 0m && alive)
                    {
                        long feedAge = _books.TryGetValue($"H:{token}", out var fb)
                            ? (long)(DateTime.UtcNow - fb.LastDeltaAt).TotalMilliseconds : -1;
                        string head = Str(doc.RootElement, "text_head");
                        Console.ForegroundColor = ConsoleColor.Yellow;
                        Console.WriteLine($"[CAMP SLIP] {label}: NO PRICE readable on the armed slip " +
                                          $"(feed age {feedAge}ms — {(feedAge >= 0 && feedAge < 30_000 ? "the feed IS live, so this is the panel, not the market" : "the feed is quiet too, so the market is probably suspended")}). " +
                                          (head.Length > 0 ? $"panel: {head}" : "panel text unavailable."));
                        Console.ResetColor();
                    }
                    if (slipOdds > 1.0m && _books.TryGetValue($"H:{token}", out var hb))
                    {
                        decimal wsPrice = hb.GetBestAskPrice();
                        if (wsPrice > 0m)
                        {
                            decimal wsOdds = 1m / wsPrice;
                            decimal diffPct = (slipOdds - wsOdds) / wsOdds * 100m;
                            long ageMs = (long)(DateTime.UtcNow - hb.LastDeltaAt).TotalMilliseconds;
                            // How many times the slip has re-quoted, and how long the CURRENT number has
                            // stood. A slip that has not moved in minutes while the feed is ticking is the
                            // clearest possible statement that camping presses a stale price.
                            int updates = (int)Dec(doc.RootElement, "price_updates");
                            long staticMs = (long)Dec(doc.RootElement, "price_static_ms");
                            bool frozen = staticMs > 120_000 && ageMs < 30_000;
                            // LOG EVERY SAMPLE, not just the alarming ones. This started as an
                            // exception report (>=1% divergence or frozen), which meant a slip that
                            // tracked the feed produced NOTHING — indistinguishable from the check not
                            // running at all. For a measurement whose entire purpose is "does the slip
                            // keep up", the quiet samples are the evidence, so they get printed too and
                            // the loud cases just carry an extra clause. One line per camp per 30s.
                            Console.WriteLine($"[CAMP SLIP-vs-WS] {label}: slip {slipOdds:0.000} vs feed " +
                                              $"{wsOdds:0.000} ({diffPct:+0.0;-0.0}%), feed age {ageMs}ms | " +
                                              $"slip re-quotes={updates}, unchanged for {staticMs / 1000}s" +
                                              (frozen ? "  <- SLIP LOOKS FROZEN while the feed is live"
                                                      : Math.Abs(diffPct) >= 5m
                                                        ? "  <- the slip is NOT tracking the feed"
                                                      : Math.Abs(diffPct) >= 1.0m ? "  <- drifting" : ""));
                        }
                    }
                }
                catch { }
            }

            // ── THE MARKET WENT OFFLINE UNDER US ──────────────────────────────
            // The panel is still there, so the popover check says healthy, but the price is gone or Place is
            // dead. A camp in that state is worth less than no camp: it holds the one tab, blocks every other
            // in-play arb at the pre-live gate, and its own pair keeps resetting the idle clock — so it can
            // ride all the way to the hard cap doing nothing. Measured 2026-08-18: 25 minutes of exactly that.
            //
            // A STREAK, not a sample. Tennis moneylines suspend between points, and treating one bad read as
            // death would abandon healthy camps several times a game. `tradeable == null` (a read error) is
            // explicitly NOT counted — unknown must never be evidence.
            if (alive && tradeable == false)
            {
                int streak = ++_untradeableStreak;
                if (streak < _deadChecks)
                {
                    Console.WriteLine($"[CAMP] {label}: not tradeable ({whyNot}) — {streak}/{_deadChecks} " +
                                      "consecutive checks. Suspensions are normal in-play; holding for now.");
                }
                else
                {
                    Console.ForegroundColor = ConsoleColor.Yellow;
                    Console.WriteLine($"[CAMP] {label}: market OFFLINE for {streak} checks ({whyNot}) — " +
                                      "releasing and moving to the best live target.");
                    Console.ResetColor();
                    await ReleaseAsync($"market offline ({whyNot})", ct, quiet: true);
                    await ArmBestAvailableAsync("previous camp went offline", ct);
                    return;
                }
            }
            else if (tradeable == true)
            {
                _untradeableStreak = 0;
            }
            // A FAILED STATUS CALL IS NOT A HEALTHY CAMP. With `ok` false every branch below was skipped,
            // so a sidecar that had died produced no camp logging whatsoever — the camp simply looked fine
            // and silent, which is exactly how an hour of dry run can be spent measuring nothing. Observed
            // 2026-08-19: the sidecar pipeline broke and the bot kept reporting an armed camp.
            if (!ok)
            {
                Console.ForegroundColor = ConsoleColor.Yellow;
                Console.WriteLine($"[CAMP] {label}: /camp/status did not answer ({Truncate(body)}) — the camp " +
                                  "state is UNKNOWN and nothing is being measured. Is the sidecar up?");
                Console.ResetColor();
                return;
            }
            if (ok && !alive)
            {
                bool took = false;
                lock (_lock)
                {
                    // A press that started since the snapshot owns the slip; leave it alone.
                    if (_phase == CampPhase.Camped)
                    {
                        _phase = CampPhase.Roving;
                        _campToken = ""; _campPairId = ""; _campLabel = ""; _campArbType = "";
                        took = true;
                    }
                }
                if (!took) return;
                Interlocked.Increment(ref _lostCount);
                Console.ForegroundColor = ConsoleColor.Yellow;
                Console.WriteLine($"[CAMP] LOST on {label} after {(now - armedAt).TotalMinutes:0.0}m — " +
                                  $"{(lost.Length > 0 ? lost : camping ? "the Quick Bet is gone" : "the sidecar is no longer camping")}. " +
                                  "Back to roving; the next in-play arb re-arms.");
                Console.ResetColor();
                // The sidecar may still hold _camping with a dead popover (its own idle watcher clears it, but
                // this can get here first). Stop explicitly so betslip trimming resumes.
                if (camping) await ReleaseAsync("lost", ct);
                return;
            }
        }

        // ── hard ceiling ──────────────────────────────────────────────────────
        // A pair that keeps producing windows the gates refuse would otherwise hold the camp forever, because
        // an open counts as action. This is the backstop that guarantees the tab eventually moves on.
        if ((now - armedAt).TotalSeconds >= _maxSec)
        {
            await ReleaseAsync($"held {(now - armedAt).TotalMinutes:0}m (hard cap)", ct);
            return;
        }

        // ── idle release ──────────────────────────────────────────────────────
        if ((now - lastAction).TotalSeconds >= idleBudget)
        {
            await ReleaseAsync($"nothing for {(now - lastAction).TotalMinutes:0.0}m", ct);
            return;
        }

        // ── relocation ────────────────────────────────────────────────────────
        if ((now - armedAt).TotalSeconds < _minTenureSec) return;
        if (!TryPickRelocation(pairId, out var move)) return;

        Interlocked.Increment(ref _relocCount);
        Console.ForegroundColor = ConsoleColor.Cyan;
        Console.WriteLine($"[CAMP] RELOCATE {label} (${move.FromScore:0.00}) → {move.Label} (${move.ToScore:0.00}) — " +
                          $"{move.ToScore / Math.Max(move.FromScore, 1e-9):0.#}x better over the last " +
                          $"{_halfLifeSec / 60:0}m half-life");
        Console.ResetColor();
        await ReleaseAsync("relocating", ct, quiet: true);
        bool got = false;
        lock (_lock) { if (_phase == CampPhase.Roving) { _phase = CampPhase.Arming; got = true; } }
        if (got) await ArmAsync(move.PairId, move.Token, move.Label, move.ArbType, "better target", ct);
    }

    internal readonly record struct Relocation(
        string PairId, string Token, string Label, string ArbType, double FromScore, double ToScore);

    /// <summary>Should the camp move, and to what? Pure decision, no I/O — the tenure check is the caller's,
    /// everything else is here so it can be exercised without a browser.
    ///
    /// <para>The MARGIN is what makes this stable. A bare "is anyone ahead?" test hands the camp to whichever
    /// game happened to close a window most recently, and two comparable matches then swap it back and forth,
    /// paying a navigation each time and never actually being armed when a window opens. Requiring a clear
    /// multiple means the camp only moves when the tape says the other game is a different class of target,
    /// not merely a nose ahead.</para></summary>
    internal bool TryPickRelocation(string incumbentPairId, out Relocation move)
    {
        move = default;
        var lead = BestTarget();
        if (lead.PairId.Length == 0 || lead.PairId == incumbentPairId) return false;
        double incumbent = ScoreOf(incumbentPairId);
        if (lead.Score < Math.Max(incumbent, 1e-9) * _switchMargin) return false;
        var lp = _telemetry.GetPair(lead.PairId);
        if (lp == null) return false;
        string lArb = DominantArbType(lead.PairId);
        string lTok = TokenFor(lp, lArb);
        if (lTok.Length == 0 || !IsCampable(lTok)) return false;
        move = new Relocation(lead.PairId, lTok, lp.Label, lArb, incumbent, lead.Score);
        return true;
    }

    // ── test surface (same assembly only) ─────────────────────────────────────
    internal double ScoreForTest(string pairId) => ScoreOf(pairId);
    internal string DominantArbTypeForTest(string pairId) => DominantArbType(pairId);
    internal (string PairId, double Score) BestTargetForTest() => BestTarget();
    /// <summary>Force the phase, so the open/close handlers can be exercised without a sidecar.</summary>
    internal void SetPhaseForTest(CampPhase p, string pairId = "", string token = "", string label = "")
    {
        lock (_lock)
        {
            _phase = p; _campPairId = pairId; _campToken = token; _campLabel = label;
            _armedAt = _lastActionAt = _lastHealthAt = DateTime.UtcNow;
            _idleBudgetSec = _idleSec;
        }
    }

    // ── sidecar operations ────────────────────────────────────────────────────

    /// <summary>Move straight to the best scoring live target, rather than dropping to ROVING and waiting for
    /// a fresh window to trigger a camp. Used when the current camp is abandoned for a reason that says
    /// nothing about the rest of the board — a dead market, not a quiet one. Silently does nothing when there
    /// is no scored target yet, which correctly leaves the normal event-driven path to pick it up.</summary>
    private async Task ArmBestAvailableAsync(string why, CancellationToken ct)
    {
        var best = BestTarget();
        if (best.PairId.Length == 0) return;
        var pair = _telemetry.GetPair(best.PairId);
        if (pair == null) return;
        string arbType = DominantArbType(best.PairId);
        string tok = TokenFor(pair, arbType);
        if (tok.Length == 0 || !IsCampable(tok)) return;
        bool claimed = false;
        lock (_lock) { if (_phase == CampPhase.Roving) { _phase = CampPhase.Arming; claimed = true; } }
        if (claimed) await ArmAsync(best.PairId, tok, pair.Label, arbType, why, ct);
    }

    private async Task ArmAsync(string pairId, string token, string label, string arbType, string why,
                                CancellationToken ct)
    {
        // Declared OUTSIDE the try so the fire-first block after the `finally` can still see it — the
        // armed slip's price is the Pinnacle leg fire-first prices against, so it has to outlive the gate.
        decimal armedOdds = 0m;
        await _gate.WaitAsync(ct);
        try
        {
            string payload = JsonSerializer.Serialize(new { selection_id = token, stake = (double)_armStake });
            Console.WriteLine($"[CAMP] arming on {label} ({why}) — {token} @ stake {_armStake:0.##}…");
            var (ok, body) = await PostAsync(_uiHttp, "/camp/start", payload, ct);
            bool armed = false; string err = Truncate(body); decimal odds = 0m;
            bool? placeable = null; decimal maxBet = 0m;
            if (ok)
            {
                try
                {
                    using var doc = JsonDocument.Parse(body);
                    armed = Flag(doc.RootElement, "ok") && Flag(doc.RootElement, "armed");
                    err   = Str(doc.RootElement, "error");
                    odds  = Dec(doc.RootElement, "odds");
                    armedOdds = odds;
                    maxBet = Dec(doc.RootElement, "max_bet");
                    if (doc.RootElement.TryGetProperty("placeable", out var pl) &&
                        (pl.ValueKind == JsonValueKind.True || pl.ValueKind == JsonValueKind.False))
                        placeable = pl.ValueKind == JsonValueKind.True;
                }
                catch { }
            }
            if (!armed)
            {
                lock (_lock) { if (_phase == CampPhase.Arming) _phase = CampPhase.Roving; }
                Console.ForegroundColor = ConsoleColor.Yellow;
                Console.WriteLine($"[CAMP] could not arm {label}: {err}. Staying on rove — the next in-play arb tries again.");
                Console.ResetColor();
                return;
            }
            var now = DateTime.UtcNow;
            lock (_lock)
            {
                _phase = CampPhase.Camped;
                _campPairId = pairId; _campToken = token; _campLabel = label; _campArbType = arbType;
                _armedAt = now; _lastActionAt = now; _lastHealthAt = now;
                // Jitter the release so it is not metronomic. A camp that is abandoned at exactly 600.0s every
                // time is a schedule, and a schedule is a signature.
                _idleBudgetSec = (int)(_idleSec * (0.8 + _rng.NextDouble() * 0.4));
                _untradeableStreak = 0;
                _armCount++;
            }
            Console.ForegroundColor = ConsoleColor.Cyan;
            Console.WriteLine($"[CAMP] ARMED on {label} @ {odds:0.000} ({SideName(arbType)}) — the next window here " +
                              $"is one press. Releasing in ~{_idleBudgetSec / 60.0:0.0}m if nothing happens." +
                              (maxBet > 0m ? $" Book max bet {maxBet:0.##}." : ""));
            Console.ResetColor();
            // A camp that cannot be pressed is worse than no camp: it holds the tab, blocks every other
            // in-play arb through the pre-live gate, and only reveals itself at the first window.
            if (placeable == false)
            {
                Console.ForegroundColor = ConsoleColor.Yellow;
                Console.WriteLine($"[CAMP] ⚠ {label}: PLACE BET is DISABLED at the {_armStake:0.##} arm stake — " +
                                  $"below Pinnacle's minimum for {odds:0.000}. The press re-types the ladder's rung " +
                                  $"first, so this only fires if that rung is LARGER" +
                                  (StakeLadder.MaxStakeAccount > 0m
                                     ? $" — and HARDVEN_STAKE_MAX pins it to {StakeLadder.MaxStakeAccount:0.##}, so it will not."
                                     : ".") );
                Console.ResetColor();
            }
            _ = _discord.AlertAsync($"⛺ camped on **{label}** ({SideName(arbType)} @ {odds:0.000}) — {why}");
        }
        finally { _gate.Release(); }

        // TRY IT NOW. Outside the gate deliberately: the fire path takes the same lock to press, so holding
        // it here would deadlock against the thing we are asking to run. Every arm gets this — the first one
        // and every relocation — because a relocation only happens when another game looks better, which is
        // exactly when its window is most likely to still be open.
        if (FireFirst && _onArmedTryFire is not null)
        {
            try
            {
                Console.WriteLine($"[CAMP] fire-first: armed on {label} — asking the executor whether the arb " +
                                  $"is still on before settling in to camp.");
                await _onArmedTryFire(pairId, token, armedOdds, ct);
            }
            catch (OperationCanceledException) { }
            catch (Exception ex)
            {
                Console.WriteLine($"[CAMP] fire-first re-check failed ({ex.GetType().Name}: {ex.Message}) — " +
                                  $"the camp is armed and unaffected; it will fire on the next window as usual.");
            }
        }
    }

    /// <summary>Drop the camp and go back to browsing. Refuses while a press is in flight unless
    /// <paramref name="force"/> — the supervisor decides to release from a snapshot taken seconds earlier, and
    /// a fire that started in between must not have the slip pulled out from under it.</summary>
    private async Task ReleaseAsync(string why, CancellationToken ct, bool quiet = false, bool force = false)
    {
        string label;
        lock (_lock)
        {
            if (!force && _phase != CampPhase.Camped) return;
            label = _campLabel;
            _phase = CampPhase.Roving;
            _campToken = ""; _campPairId = ""; _campLabel = ""; _campArbType = "";
            _releaseCount++;
        }
        await _gate.WaitAsync(ct);
        try { await PostAsync(_fastHttp, "/camp/stop", null, ct); }
        catch { }
        finally { _gate.Release(); }
        if (!quiet)
            Console.WriteLine($"[CAMP] released {label} — {why}. Roving; will camp on the next in-play arb.");
    }

    // ── scoring helpers ───────────────────────────────────────────────────────

    private double Decayed(double value, DateTime at)
    {
        if (value <= 0d) return 0d;
        double elapsed = (DateTime.UtcNow - at).TotalSeconds;
        if (elapsed <= 0d) return value;
        return value * Math.Pow(0.5, elapsed / Math.Max(1d, _halfLifeSec));
    }

    private double ScoreOf(string pairId) =>
        _scores.TryGetValue(pairId, out var s) ? DecayedLocked(s) : 0d;

    private double DecayedLocked(PairScore s) { lock (s) return Decayed(s.Value, s.ValueAt); }

    /// <summary>Highest-scoring pair that is currently live and paired. Restricted to live books because a
    /// score earned an hour ago on a match that has since finished is not a camp target.</summary>
    private (string PairId, double Score) BestTarget()
    {
        string best = ""; double bestVal = 0d;
        foreach (var kv in _scores)
        {
            double v = DecayedLocked(kv.Value);
            if (v <= bestVal) continue;
            var p = _telemetry.GetPair(kv.Key);
            if (p == null) continue;
            if (!IsCampable(TokenFor(p, DominantArbType(kv.Key)))) continue;
            best = kv.Key; bestVal = v;
        }
        return (best, bestVal);
    }

    /// <summary>The arb direction this pair's windows keep taking, which decides WHICH cell to arm. Ties go to
    /// the Kalshi-NO side (it backs the HardVen YES leg) purely for determinism.</summary>
    private string DominantArbType(string pairId)
    {
        if (!_scores.TryGetValue(pairId, out var s)) return "K_NO_P_YES";
        lock (s) return s.NoSideOpens > s.YesSideOpens ? "K_YES_P_NO" : "K_NO_P_YES";
    }

    private static string TokenFor(CrossPair p, string arbType) =>
        arbType == "K_YES_P_NO" ? (p.HardVenNoTokenId ?? "") : (p.HardVenYesTokenId ?? "");

    private static string SideName(string arbType) =>
        arbType == "K_YES_P_NO" ? "backing the NO side" : "backing the YES side";

    private bool IsTokenLive(string token) =>
        token.Length > 0 && _books.TryGetValue($"H:{token}", out var b) && b.IsLive && !b.IsDead;

    /// <summary>Can this selection be camped at all? Live, and placeable by the book — see _moneylineOnly.</summary>
    private bool IsCampable(string token) =>
        IsTokenLive(token) && (!_moneylineOnly || CrossArbExecutor.IsStraightMoneyline(token));

    // ── status ────────────────────────────────────────────────────────────────

    public string StatusLine()
    {
        CampPhase phase; string label, side; DateTime armedAt, lastAction; int budget;
        lock (_lock)
        {
            phase = _phase; label = _campLabel; side = _campArbType;
            armedAt = _armedAt; lastAction = _lastActionAt; budget = _idleBudgetSec;
        }
        if (phase == CampPhase.Off) return "camp: off";
        string head = phase switch
        {
            CampPhase.Roving => "roving (nothing armed)",
            CampPhase.Arming => "arming…",
            CampPhase.Firing => $"FIRING on {label}",
            _                => $"camped on {label} ({SideName(side)}) for {(DateTime.UtcNow - armedAt).TotalMinutes:0.0}m, " +
                                $"idle {(DateTime.UtcNow - lastAction).TotalMinutes:0.0}/{budget / 60.0:0.0}m",
        };
        return $"camp: {head} | arms={_armCount} relocs={_relocCount} released={_releaseCount} lost={_lostCount} " +
               $"fires={_fireCount} placed={_placedCount}" + (_ambiguousCount > 0 ? $" ⚠unconfirmed={_ambiguousCount}" : "")
               + (_belowFloorCount > 0 ? $" ⚠belowFloor={_belowFloorCount}" : "");
    }

    /// <summary>The current camp shortlist, best first — what the relocation decision is actually looking at.</summary>
    public IEnumerable<(string Label, double Score, int Opens, int Takeable, DateTime LastOpen)> TopTargets(int n)
        => _scores.Select(kv =>
           {
               lock (kv.Value)
                   return (kv.Value.Label, Decayed(kv.Value.Value, kv.Value.ValueAt),
                           kv.Value.Opens, kv.Value.Takeable, kv.Value.LastOpen);
           })
           .Where(t => t.Item2 > 0d)
           .OrderByDescending(t => t.Item2)
           .Take(n);

    // ── plumbing ──────────────────────────────────────────────────────────────

    private async Task<(bool Ok, string Body)> PostAsync(HttpClient http, string path, string? json, CancellationToken ct)
    {
        try
        {
            using HttpContent? body = json is null ? null : new StringContent(json, Encoding.UTF8, "application/json");
            using var resp = await http.PostAsync(_sidecar + path, body, ct);
            string text = await resp.Content.ReadAsStringAsync(ct);
            return (resp.IsSuccessStatusCode, text);
        }
        catch (Exception ex) { return (false, $"{ex.GetType().Name}: {ex.Message}"); }
    }

    private async Task<(bool Ok, string Body)> GetAsync(HttpClient http, string path, CancellationToken ct)
    {
        try
        {
            using var resp = await http.GetAsync(_sidecar + path, ct);
            string text = await resp.Content.ReadAsStringAsync(ct);
            return (resp.IsSuccessStatusCode, text);
        }
        catch (Exception ex) { return (false, $"{ex.GetType().Name}: {ex.Message}"); }
    }

    private static bool Flag(JsonElement el, string name) =>
        el.ValueKind == JsonValueKind.Object && el.TryGetProperty(name, out var v) && v.ValueKind == JsonValueKind.True;

    private static string Str(JsonElement el, string name) =>
        el.ValueKind == JsonValueKind.Object && el.TryGetProperty(name, out var v) && v.ValueKind == JsonValueKind.String
            ? v.GetString() ?? "" : "";

    private static decimal Dec(JsonElement el, string name)
    {
        if (el.ValueKind != JsonValueKind.Object || !el.TryGetProperty(name, out var v)) return 0m;
        if (v.ValueKind == JsonValueKind.Number && v.TryGetDecimal(out var n)) return n;
        if (v.ValueKind == JsonValueKind.String &&
            decimal.TryParse(v.GetString(), NumberStyles.Any, CultureInfo.InvariantCulture, out var s)) return s;
        return 0m;
    }

    private static string Truncate(string s) => s.Length <= 200 ? s : s[..200];

    private static decimal EnvDec(string name, decimal fallback)
    {
        string? raw = Environment.GetEnvironmentVariable(name);
        return decimal.TryParse(raw, NumberStyles.Any, CultureInfo.InvariantCulture, out var v) && v > 0m ? v : fallback;
    }

    private static int EnvInt(string name, int fallback)
    {
        string? raw = Environment.GetEnvironmentVariable(name);
        return int.TryParse(raw, NumberStyles.Any, CultureInfo.InvariantCulture, out var v) && v > 0 ? v : fallback;
    }
}
