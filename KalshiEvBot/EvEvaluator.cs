using System.Collections.Concurrent;
using System.Globalization;
using System.Text.Json;
using PredictionBacktester.Engine.LiveExecution;

namespace KalshiEvBot;

/// <summary>Tunables. Every one reads the environment: these are calibration decisions, not constants, and
/// the live values come out of M1's settlement report rather than out of this file.</summary>
public sealed class EvConfig
{
    /// <summary>Minimum EV per contract to call something a signal. 3.5c was the MAKER threshold and admits
    /// 0.9% of windows as a taker — the bot would essentially never fire. 1c admits ~7%; observe there and
    /// set the live value from settlement results.</summary>
    public double EvMin           = Env("EV_MIN", 0.01);
    // ── M1 LIVE EXECUTION ─────────────────────────────────────────────────────────────────────────────
    // OFF unless --live is passed. The caps are deliberately tiny: M1 is measuring whether a found edge can
    // be BOUGHT, not trying to earn from it, and the answer should cost almost nothing. $5 a side and $10 a
    // game means a whole month of this cannot move the balance meaningfully.
    public bool   Live                 = false;
    public double LiveStakePerSideUsd  = Env("EV_LIVE_STAKE_SIDE", 5.0);
    public double LiveStakePerGameUsd  = Env("EV_LIVE_STAKE_GAME", 10.0);
    // A no-fill is free and does NOT consume the side's allowance, so the same market may be re-attempted
    // on a later signal. This stops that becoming a hot loop against one stubborn book.
    public double LiveRetryCooldownSec = Env("EV_LIVE_RETRY_COOLDOWN_SEC", 60);
    /// <summary>Hard stop on money SPENT in one local day. 0 = no cap. The per-side and per-game caps
    /// bound one market; nothing bounds the day, and an unattended run turns the shard float over
    /// repeatedly as settlements return. Resets at local midnight and survives a restart.</summary>
    public double LiveDailyUsd = Env("EV_LIVE_DAILY_USD", 0);
    /// <summary>How far below EvMin a WS-implied EV may sit and still buy a REST call. The WS ask is
    /// optimistic 95% of the time, which makes WS EV an upper bound and pre-screening safe; this slack
    /// covers the other 5%.</summary>
    public double PrescreenSlack  = Env("EV_PRESCREEN_SLACK", 0.02);
    public double MinPrice        = Env("EV_MIN_PRICE", 0.20);
    public double MaxPrice        = Env("EV_MAX_PRICE", 0.80);
    public double MaxTradeFrac    = Env("EV_MAX_TRADE_FRACTION", 0.03);
    public int    CooldownMs      = (int)Env("EV_RECHECK_COOLDOWN_MS", 15_000);
    // 4, not 2. The pool is also what absorbs a slow venue call: at 2, two stalled requests halt
    // evaluation entirely. Raising it costs burst REST rate, not average - the call volume is set by
    // book updates, not by worker count. Watch for 429s if raising further.
    public int    RestConcurrency = (int)Env("EV_REST_CONCURRENCY", 4);
    // Set = PIN the bankroll at this value and never read the live balance (see RefreshBankrollAsync).
    // Unset (0) = read the venue. Pin it before --live, or Kelly sizing drifts with the balance and the
    // telemetry stops being comparable across the M0/M1 boundary.
    public double BankrollFallback= Env("EV_BANKROLL_USD", 0);
    /// <summary>"proportional" (the spec's primary) or "shin". Both are always computed and logged; this
    /// only selects which one drives the decision.</summary>
    public string DeVigMethod     = (Environment.GetEnvironmentVariable("EV_DEVIG") ?? "proportional").ToLowerInvariant();
    /// <summary>Require BOTH de-vig methods to clear EvMin, not just the selected one.
    ///
    /// <para>Proportional and Shin disagree by up to 0.87c inside the price band (measured 2026-08-22 at
    /// 0.35-0.50: prop +0.22c against Shin -0.65c), which is most of a 1c threshold. A row that clears on
    /// one and fails the other is a coin-flip on an unverified modelling assumption, not an edge.</para>
    ///
    /// <para>Requiring agreement is robust to WHICH method is right, so it does not depend on settling that
    /// question first — and settlement will settle it anyway, since both are logged on every row.</para></summary>
    public bool RequireDeVigAgree = Env("EV_REQUIRE_DEVIG_AGREE", 1) != 0;
    /// <summary>THE KINETIC FILTER. Require our own fair value to have moved UP over the last
    /// <see cref="KineticWindowSec"/> seconds before a row counts as a signal.
    ///
    /// <para><b>Static EV cannot tell an opportunity from a falling knife.</b> A gap opens for two opposite
    /// reasons: Kalshi's price DROPPED and we have not caught up (we are last to know), or our fair value
    /// ROSE and Kalshi has not caught up (the thesis). Both look identical at a single instant — a number
    /// below our P_true — and only the direction of recent movement separates them.</para>
    ///
    /// <para>The existing PINNACLE_LED regime does NOT close this: `moveP` is an ABSOLUTE value, so it
    /// fires on our oracle moving in either direction, including down. Buying because our price is falling
    /// fast is the same mistake from the other end.</para>
    ///
    /// <para><b>Measured 2026-08-22, and the reason this guard exists:</b> of in-play P_true moves above
    /// 0.5c, <b>26 were DOWN and 0 were UP</b>. Every signal that session was on a declining side. The
    /// mechanism is specific — all three logged legs were soccer 1X2 sides in level matches, where the draw
    /// probability climbs with the clock and drags home, away AND not-tie down together. Expect this filter
    /// to suppress nearly everything at first; that is the finding, not a malfunction.</para></summary>
    public bool RequirePinnacleRising = Env("EV_REQUIRE_PINNACLE_RISING", 1) != 0;
    /// <summary>Window for the kinetic filter. Default 5s per the operator's spec. NOTE the interaction
    /// with `EV_ORACLE_POLL_MS` (default 3000): a 5s window holds only one or two polls, so it is sensitive
    /// to poll jitter. 10s holds three or four and is the safer setting if signals look erratic.</summary>
    public double KineticWindowSec = Env("EV_KINETIC_WINDOW_SEC", 5);
    /// <summary>How far P_true must have risen across the window to count as rising. Grounded, not guessed:
    /// consecutive in-play P_true samples were EXACTLY unchanged 79% of the time, p90 = 0.28c, p95 = 0.75c.
    /// P_true only moves when the odds move, so the noise floor is near zero and 0.5c sits above p90.</summary>
    public double KineticMinRise = Env("EV_KINETIC_MIN_RISE", 0.005);
    /// <summary>Require every Pinnacle leg to be under LIVE WS coverage before a row counts as a signal.
    /// A screening-only quote carries a FRESH timestamp but a DELAYED price, so the age gate cannot see it.
    /// Set 0 to log such rows as signals anyway (observation only — they are still logged either way).</summary>
    public bool RequireWsVerified = Env("EV_REQUIRE_WS_VERIFIED", 1) != 0;
    /// <summary>Largest believable gap between our P_true and Kalshi's own price. Beyond this the row is
    /// logged as IMPLAUSIBLE rather than as a signal: every such case so far has been a pairing fault, and
    /// the measured taker edge distribution puts almost nothing past 3.5c, let alone 20c.</summary>
    public double MaxDisagree     = Env("EV_MAX_DISAGREE", 0.15);
    /// <summary>Largest tolerable disagreement between the two KALSHI sources (WS book vs REST valuation)
    /// before a row stops counting as a signal.
    ///
    /// <para><b>They agree to the cent 97.3% of the time</b> (119,412 rows, 2026-08-24), so a material gap
    /// means ONE OF THEM IS STALE — and nothing here can tell which. Pricing from REST while the candidate
    /// was SCREENED from a WS book that disagrees by 8c is not a measurement of anything.</para>
    ///
    /// <para><b>The gap does not merely accompany big edges, it MANUFACTURES them.</b> The prescreen admits
    /// a row when `evWs >= EvMin - PrescreenSlack`. If the WS ask reads 8c HIGHER than REST, that row only
    /// survives when the REST-based EV is about +7c — so every stale-high WS quote that gets through
    /// arrives wearing a large apparent edge. Measured the same day: signals ran 7-of-11 past a 3c gap
    /// against a 1.1% base rate, immediately after the WS/REST agreement fell from 98% to 93%.</para></summary>
    public double MaxSourceGap    = Env("EV_MAX_WS_REST_GAP", 0.03);
    /// <summary>Only count a row as a signal while the match is actually being PLAYED.
    ///
    /// <para>Measured 2026-08-21/22: all 6 genuine signals were in-play; all 44 phantoms and the one bad
    /// SIGNAL were pre-match. That is not coincidence — a fixture days out carries a wide, unformed
    /// Pinnacle line (vig ~9% against ~4% at kickoff), a thin Kalshi book, and no pressure on either side
    /// to be right yet, so a disagreement means far less. It is also where a mispair survives longest,
    /// because nothing is moving to contradict it.</para>
    ///
    /// <para>It also protects the strategy's premise: the case for this bot is fast capital rollover, and
    /// a bet on Sunday's match ties up the stake until Sunday.</para>
    ///
    /// <para>Pre-match rows are still WRITTEN and still snapshotted — that is where the calibration volume
    /// is (336 fixtures against a handful live), and whether Pinnacle is predictive pre-match is a
    /// question worth answering rather than assuming. They just do not count as things to trade.</para></summary>
    public bool RequireInPlay     = Env("EV_REQUIRE_IN_PLAY", 1) != 0;
    /// <summary>How far Kalshi must move since the last look before "who moved first" can rule.</summary>
    public double LedMoveMin      = Env("EV_LED_MOVE_MIN", 0.03);
    /// <summary>Our oracle counts as having FOLLOWED rather than led if it moved less than this fraction
    /// of Kalshi's move. 0.34 = Kalshi moved at least three times as far as we did.</summary>
    public double LedRatio        = Env("EV_LED_RATIO", 0.34);
    /// <summary>Narrow signals to the thesis case only: our oracle moved and Kalshi has not caught up.
    /// OFF by default — until settlement says which regime pays, filtering to one destroys the
    /// comparison that would tell us.</summary>
    public bool RequirePinnacleLed = Env("EV_REQUIRE_PINNACLE_LED", 0) != 0;
    /// <summary>Ask Pinnacle directly before calling anything a signal. The only INDEPENDENT read of the
    /// oracle we have; without it screening and verification share one cache and cannot disagree.</summary>
    /// <summary>Ask Pinnacle by REST before signalling. OFF: the operator's call is to trust the WS, and
    /// the evidence supports it — mid-event Pinnacle SUSPENDS, so a re-read returns nothing exactly when
    /// it would matter, buying venue traffic for no answer. Freshness carries the weight instead, which
    /// is why the in-play age gate is now seconds rather than half a minute.</summary>
    public bool VerifyVenue       = Env("EV_VERIFY_VENUE", 0) != 0;

    public static double Env(string k, double dflt)
        => double.TryParse(Environment.GetEnvironmentVariable(k), NumberStyles.Any,
                           CultureInfo.InvariantCulture, out var v) ? v : dflt;
}

/// <summary>Running counts for the status line. Nothing here is a decision; it is how the operator tells
/// "found nothing" apart from "never looked".</summary>
public sealed class EvStats
{
    public long Screened, NoQuote, StaleOracle, Suspended, BelowPrescreen, Cooldown,
                RestCalls, RestFailed, Signals, RejectedByRest, FlooredToZero, RateLimited,
                IncompleteBook, ScreeningOnly, Implausible, PreMatch, KalshiLed, PinnacleLed,
                VenueVanished, VenueRefused, OutOfBand, NotRising, NoKineticHistory, DeVigSplit,
                SourceGap;
}

/// <summary>
/// The decision path: WS says look, Pinnacle says what it is worth, REST says what it costs.
///
/// <para>M0 places no orders. The one thing this class must get right is that the EV it logs is computed
/// from the REST ask — a CSV of WS-priced EV measures a price nobody can get, and M1 would inherit the lie
/// and grade a strategy that was never available.</para>
/// </summary>
public sealed class EvEvaluator
{
    private readonly ConcurrentDictionary<string, EvPair> _byTicker;
    private readonly PinnacleOracle _oracle;
    private readonly KalshiBookFeed _feed;
    private readonly KalshiOrderClient _kalshi;
    private LiveExecutor? _live;                 // null unless --live; M0 keeps the order API unreachable

    /// <summary>Arms live execution. Separate from the constructor so that M0 cannot reach the order API by
    /// forgetting an argument — the executor has to be handed in deliberately.</summary>
    public void EnableLive(LiveExecutor ex) => _live = ex;
    public LiveExecutor? LiveExec => _live;
    private readonly EvTelemetry _telemetry;
    private FollowUpTracker? _followUp;
    private readonly EvConfig _cfg;
    private readonly SemaphoreSlim _restGate;
    private readonly ConcurrentDictionary<string, long> _cooldownUntil = new(StringComparer.Ordinal);

    /// <summary>Last (Kalshi ask, P_true) seen per ticker+side, for the who-moved-first test below.</summary>
    private readonly ConcurrentDictionary<string, (double Ask, double PTrue)> _lastSeen = new(StringComparer.Ordinal);
    /// <summary>Short rolling P_true history per (market, side), sampled at SCREENING rate rather than at
    /// the REST cooldown. This distinction is the whole point: `_lastSeen` above only updates after a REST
    /// valuation, so at a 15s cooldown its "previous look" is 15 seconds old and a 5-second window is not
    /// expressible from it. Screening runs on every WS update and every oracle poll, so this sees the
    /// oracle move at close to its true resolution.</summary>
    private readonly ConcurrentDictionary<string, PTrueTrack> _ptrue = new(StringComparer.Ordinal);

    /// <summary>Bounded time-series of one side's de-vigged fair value.</summary>
    internal sealed class PTrueTrack
    {
        // Read once: TryRise runs on every valuation, and an env lookup per call is pure waste.
        private static readonly double MaxHoleSec = EvConfig.Env("EV_KINETIC_MAX_HOLE_SEC", 0.0);   // 0 = OFF (see TryRise)
        private readonly object _gate = new();
        private readonly List<(DateTime T, double P)> _s = new();

        public void Add(DateTime t, double p, TimeSpan keep)
        {
            lock (_gate)
            {
                // FIXED 250ms SAMPLING FLOOR, not a value-change trigger. Screening fires far faster than
                // the oracle updates — many times a second on a busy book — and an unbounded append rate
                // lets the count cap below truncate the buffer to span LESS than the window it must cover.
                // That surfaces as "cannot answer" on exactly the busiest, most interesting books, which is
                // both wrong and silent (caught by the 5000-sample self-test). The filter asks about motion
                // over SECONDS, so 250ms resolution costs nothing and bounds the buffer to ~4/second.
                if (_s.Count > 0 && (t - _s[^1].T).TotalMilliseconds < 250) return;
                _s.Add((t, p));
                var cut = t - keep;
                int i = 0; while (i < _s.Count && _s[i].T < cut) i++;
                if (i > 0) _s.RemoveRange(0, i);
                // Defensive only: at 4 samples/sec against a `keep` of 30-60s this cap can never bind.
                if (_s.Count > 1024) _s.RemoveRange(0, _s.Count - 1024);
            }
        }

        /// <summary>Change in P_true across <paramref name="window"/>. FALSE when no sample is old enough to
        /// span it — an unmeasurable window must not read as a flat one, or a market we just started
        /// watching would silently pass a filter that has nothing to say about it yet.</summary>
        public bool TryRise(DateTime now, TimeSpan window, out double rise)
        {
            rise = 0;
            lock (_gate)
            {
                if (_s.Count == 0) return false;
                var cut = now - window;
                int at = -1;
                for (int i = _s.Count - 1; i >= 0; i--)
                    if (_s[i].T <= cut) { at = i; break; }
                if (at < 0) return false;

                // A GAP IN THE SERIES IS NOT A PRICE MOVE — but this check is OFF BY DEFAULT, because
                // turning it on cost every live signal and the case it covers is already handled.
                //
                // The theory: the sidecar cycles its browser while this bot stays up, `Screen` stops
                // sampling while the oracle is stale, and measuring across the hole reads the oracle
                // CATCHING UP as a Pinnacle move — a false PINNACLE_LED.
                //
                // What actually happened when it ran at ~1s: in-play NO_KINETIC_HISTORY went 0 -> 117 per
                // five minutes, NOT_RISING collapsed 44 -> 0 (candidates stopped being EVALUABLE rather
                // than being judged), and SIGNALS hit zero with 20 in-play tickers healthy. In-play tennis
                // suspends between points, which also stops sampling, so nearly every live candidate
                // carries a hole. And the burst that motivated the guard was later explained by the
                // WS/REST source gap (34.5% of signals selected on a stale quote), not by holes at all.
                //
                // Outages longer than `keep` (~30s) need no check: pruning empties the buffer and the
                // `at < 0` return above already refuses. Only the 1-30s band was ever uncovered.
                if (MaxHoleSec > 0)
                    for (int i = at + 1; i < _s.Count; i++)
                        if ((_s[i].T - _s[i - 1].T).TotalSeconds > MaxHoleSec) return false;

                rise = _s[^1].P - _s[at].P;
                return true;
            }
        }
    }
    private readonly ConcurrentQueue<string> _queue = new();
    private readonly ConcurrentDictionary<string, byte> _queued = new(StringComparer.Ordinal);
    private readonly SemaphoreSlim _work = new(0);

    public readonly EvStats Stats = new();
    /// <summary>Tickers waiting to be screened. Lets --once know when the sweep has drained.</summary>
    public int Pending => _queued.Count;
    /// <summary>The TELEMETRY bankroll — the basis the Kelly `Contracts` column is computed on.
    ///
    /// <para><b>Deliberately fake and deliberately frozen.</b> Contracts is an analysis quantity, not an
    /// order size: live sizing is the flat per-side cap. Every row collected since 2026-08-22 was sized
    /// against 576.29, so letting this drift with the real account would silently re-base a column that
    /// months of calibration depend on — the same signal logged on two days would carry different sizes
    /// for a reason that has nothing to do with the edge. Pinned via EV_BANKROLL_USD.</para></summary>
    public double BankrollUsd { get; set; }

    /// <summary>The REAL money available on the trading shard: cash plus the bid-value of open positions
    /// there. Snapshotted once a local day. This is what gates the low-collateral floor; it never touches
    /// the telemetry above.</summary>
    public double LiveEquityUsd { get; set; }

    /// <summary>Series -> Kalshi's fee multiplier M. Empty means "not primed", and every lookup then
    /// returns the published default of 1.</summary>
    private readonly ConcurrentDictionary<string, double> _feeM = new(StringComparer.Ordinal);

    /// <summary>M for this ticker's series.
    ///
    /// <para><b>Self-healing for series that appear mid-run.</b> The pair file is re-read every couple of
    /// minutes and can introduce a series the startup prime never saw; returning a silent 1.0 forever would
    /// be exactly the assumption this whole mechanism exists to remove. An unknown series therefore kicks
    /// off a one-shot background read and answers 1.0 (the published default) until it lands, which is at
    /// most one evaluation cycle. 1.0 is also the safe direction to be wrong in: it UNDER-states the fee
    /// for a series that turns out to be dearer only if that series is dearer than standard, and the IOC
    /// limit still caps what we pay.</para></summary>
    private double FeeM(string ticker)
    {
        string series = ticker.Split('-')[0];
        if (_feeM.TryGetValue(series, out double m)) return m;
        if (_feeMPending.TryAdd(series, 0))
            _ = Task.Run(async () =>
            {
                try
                {
                    double v = await _kalshi.FeeMultiplierForAsync(series);
                    _feeM[series] = v;
                    if (Math.Abs(v - 1.0) > 1e-9)
                        Con.Line(ConsoleColor.Yellow,
                            $"[FEES] NEW series {series} has a NON-STANDARD multiplier {v:0.##} — "
                          + "EV, the IOC limit and the size are now scaled for it.");
                    else Console.WriteLine($"[FEES] new series {series}: multiplier 1 (standard).");
                }
                catch { _feeM[series] = 1.0; }
            });
        return 1.0;
    }

    /// <summary>Series with a background multiplier read in flight, so a hot loop asks once, not per tick.</summary>
    private readonly ConcurrentDictionary<string, byte> _feeMPending = new(StringComparer.Ordinal);

    /// <summary>Reads M once per watched series, before any trading.
    ///
    /// <para>Done at STARTUP rather than lazily on the order path: it is six calls for the whole session,
    /// it keeps the money path free of an extra round trip, and a multiplier that changed overnight is
    /// something to see in the banner rather than discover from a losing month. The fee comes out of a
    /// 1-2c edge, so a series quietly moving to M=2 would make every signal in it a loser while the
    /// telemetry kept reporting a profit.</para></summary>
    public async Task PrimeFeeMultipliersAsync(CancellationToken ct)
    {
        var series = _byTicker.Values.Select(p => p.KalshiTicker.Split('-')[0])
                              .Distinct(StringComparer.Ordinal)
                              .OrderBy(x => x, StringComparer.Ordinal).ToList();
        var odd = new List<string>();
        foreach (string sname in series)
        {
            if (ct.IsCancellationRequested) return;
            try
            {
                double m = await _kalshi.FeeMultiplierForAsync(sname);
                _feeM[sname] = m;
                if (Math.Abs(m - 1.0) > 1e-9) odd.Add($"{sname}={m:0.##}");
            }
            catch { _feeM[sname] = 1.0; }
        }
        Console.WriteLine($"[FEES] {series.Count} series checked, multiplier read live from "
                        + "/series/{ticker}.fee_multiplier"
                        + (odd.Count == 0 ? " — all 1 (standard 0.07 taker)."
                                          : "  NON-STANDARD: " + string.Join(", ", odd)));
        // A fee SHAPE we do not implement is worse than a multiplier we misread: no scaling fixes it.
        var badShapes = _kalshi.UnknownFeeTypes;
        if (badShapes.Count > 0)
        {
            Con.Line(ConsoleColor.Red,
                "[FEES] UNKNOWN fee_type on " + string.Join(", ", badShapes)
              + " — our fee arithmetic assumes the quadratic shape and is NOT valid for these. "
              + "Every EV on that series is suspect until this is implemented.");
        }
        if (odd.Count > 0)
        {
            Console.ForegroundColor = ConsoleColor.Yellow;
            Console.WriteLine("[FEES] a multiplier != 1 changes EV, the IOC limit and the Kelly size for "
                            + "that series. Those are applied, but check the series is still worth trading.");
            Console.ResetColor();
        }
    }
    /// <summary>Fraction of bankroll already at risk. Always 0 in M0 — nothing is ever bought — but the
    /// damping term is computed from it so M2 inherits a sizer that has been exercised, not a new one.</summary>
    public double ActiveExposureFraction { get; set; }
    public bool Verbose;

    public EvEvaluator(IEnumerable<EvPair> pairs, PinnacleOracle oracle, KalshiBookFeed feed,
                       KalshiOrderClient kalshi, EvTelemetry telemetry, EvConfig cfg)
    {
        _byTicker  = new ConcurrentDictionary<string, EvPair>(
            pairs.GroupBy(p => p.KalshiTicker).ToDictionary(g => g.Key, g => g.First()), StringComparer.Ordinal);
        _oracle    = oracle;
        _feed      = feed;
        _kalshi    = kalshi;
        _telemetry = telemetry;
        _cfg       = cfg;
        _restGate  = new SemaphoreSlim(Math.Max(1, cfg.RestConcurrency));
    }

    // ── Triggers ──────────────────────────────────────────────────────────────────────────────────────
    /// <summary>A Kalshi book moved. Coalesced: a ticker already waiting is not queued twice, so a market
    /// ticking fifty times a second still costs one evaluation.</summary>
    public void Nudge(string ticker)
    {
        if (!_byTicker.ContainsKey(ticker)) return;
        if (!_queued.TryAdd(ticker, 0)) return;
        _queue.Enqueue(ticker);
        _work.Release();
    }

    /// <summary>Pinnacle moved. Re-screens everything — a value bet can open with the Kalshi book perfectly
    /// still, and a bot that only woke on Kalshi ticks would never see those. The screen itself is free;
    /// only the ones that survive it cost a REST call.</summary>
    public void SweepAll() { foreach (var t in _byTicker.Keys) Nudge(t); }

    /// <summary>Adds or replaces pairs after a reload of cross_pairs.json. Returns how many are new.
    /// Existing entries are overwritten because the pairing job re-points a market at a new Pinnacle
    /// matchup id when a fixture is re-issued, and the stale id would price against a dead selection.</summary>
    public int UpsertPairs(IEnumerable<EvPair> pairs)
    {
        int added = 0;
        foreach (var p in pairs)
        {
            if (!_byTicker.ContainsKey(p.KalshiTicker)) added++;
            _byTicker[p.KalshiTicker] = p;
        }
        return added;
    }

    /// <summary>REPLACES the watched set with the current pair file. Returns (added, removed).
    /// The sweep runs over every entry on every oracle poll, so a set that only grows turns a fortnight's
    /// daily re-pairs into thousands of finished matches re-screened every three seconds. Settlements for
    /// dropped markets are still banked — the settlement watcher keeps its own archive.</summary>
    public (int Added, int Removed) ReplacePairs(IEnumerable<EvPair> pairs)
    {
        var fresh = pairs.GroupBy(p => p.KalshiTicker)
                         .ToDictionary(g => g.Key, g => g.First(), StringComparer.Ordinal);
        int added = fresh.Keys.Count(k => !_byTicker.ContainsKey(k));
        int removed = 0;
        foreach (var t in _byTicker.Keys.Where(k => !fresh.ContainsKey(k)).ToList())
        {
            if (_byTicker.TryRemove(t, out _)) removed++;
            _cooldownUntil.TryRemove(t, out _);
        }
        foreach (var kv in fresh) _byTicker[kv.Key] = kv.Value;
        // A RELOAD CAN INTRODUCE A WHOLE NEW SERIES, whose fee multiplier the startup prime never read.
        // Touch each one so the lazy path above resolves it now rather than on the first live signal.
        foreach (string sname in fresh.Values.Select(v => v.KalshiTicker.Split('-')[0])
                                      .Distinct(StringComparer.Ordinal))
            if (!_feeM.ContainsKey(sname)) FeeM(sname);
        return (added, removed);
    }

    /// <summary>Wired after construction: the tracker needs the oracle and feed, built alongside this.</summary>
    public void SetFollowUp(FollowUpTracker? f) => _followUp = f;

    public int PairCount => _byTicker.Count;

    /// <summary>Drops every per-ticker cooldown. Used by --verify: the markets it wants to exercise were
    /// just evaluated by the live loop, so without this the sweep makes zero REST calls and the check
    /// reports "nothing near the threshold" — a confident, wrong explanation of its own throttling.</summary>
    public void ClearCooldowns() => _cooldownUntil.Clear();

    /// <summary>Runs <c>EV_REST_CONCURRENCY</c> worker loops, not one. A single loop awaits its REST call
    /// inside the loop body, so the concurrency semaphore could never reach 2 and one slow market stalled
    /// the screening of every other.</summary>
    public Task RunAsync(CancellationToken ct)
        => Task.WhenAll(Enumerable.Range(0, Math.Max(1, _cfg.RestConcurrency)).Select(_ => WorkerAsync(ct)));

    private async Task WorkerAsync(CancellationToken ct)
    {
        while (!ct.IsCancellationRequested)
        {
            try { await _work.WaitAsync(ct); } catch (OperationCanceledException) { break; }
            if (!_queue.TryDequeue(out var ticker)) continue;
            _queued.TryRemove(ticker, out _);
            try { await EvaluateAsync(ticker, ct); }
            catch (OperationCanceledException) when (ct.IsCancellationRequested) { break; }
            catch (Exception ex) { Console.WriteLine($"[EVAL] {ticker}: {ex.GetType().Name}: {ex.Message}"); }
        }
    }

    // ── One ticker, both sides ────────────────────────────────────────────────────────────────────────
    private sealed record Screened(string Side, double PTrueProp, double PTrueShin, double PTrueUsed,
                                   double Vig, double ShinZ, double PinMine, double PinOther, double PinSum,
                                   double OracleAgeMs, double OracleDepth, bool InPlay,
                                   decimal WsAsk, decimal WsDepth, double WsBookAge, double EvWs,
                                   int NumLegs, string PinOddsAll, bool WsVerified);

    private async Task EvaluateAsync(string ticker, CancellationToken ct)
    {
        if (!_byTicker.TryGetValue(ticker, out var pair)) return;

        var candidates = new List<Screened>(2);
        foreach (string side in new[] { "YES", "NO" })
        {
            var s = Screen(pair, side);
            if (s is not null) candidates.Add(s);
        }
        if (candidates.Count == 0) return;

        // Cooldown is per TICKER, applied after the screen: one REST read prices both sides, so the cost
        // this is rationing is per market, not per side.
        long now = Environment.TickCount64;
        if (_cooldownUntil.TryGetValue(ticker, out long until) && now < until)
        {
            Interlocked.Increment(ref Stats.Cooldown);
            return;
        }
        _cooldownUntil[ticker] = now + _cfg.CooldownMs;

        decimal restYes, restNo;
        await _restGate.WaitAsync(ct);
        try
        {
            Interlocked.Increment(ref Stats.RestCalls);
            using var doc = await _kalshi.GetMarketAsync(ticker);
            var mkt = doc.RootElement.TryGetProperty("market", out var m) ? m : doc.RootElement;
            // Free shard resolution: this response already carries it, so the order never needs its own
            // lookup and never falls back to Kalshi's slower auto-routing.
            if (mkt.TryGetProperty("exchange_index", out var xi) && xi.ValueKind == JsonValueKind.Number)
                _kalshi.NoteExchangeIndex(ticker, xi.GetInt32());
            restYes = AskDollars(mkt, yes: true);
            restNo  = AskDollars(mkt, yes: false);
        }
        catch (Exception ex)
        {
            Interlocked.Increment(ref Stats.RestFailed);
            if (Verbose) Console.WriteLine($"[REST] {ticker}: {ex.GetType().Name}: {ex.Message}");
            return;
        }
        finally { _restGate.Release(); }

        foreach (var c in candidates)
        {
            decimal restAsk = c.Side == "YES" ? restYes : restNo;
            if (restAsk <= 0m || restAsk >= 1m) continue;      // no REST price = nothing to value

            // ── INDEPENDENT VENUE VERIFY, ON CANDIDATES ONLY ──────────────────────────────────────────
            // Kalshi is read twice by two different paths; Pinnacle was read once, twice. Before calling
            // anything a signal, ask the venue directly what this matchup is priced at NOW — the one check
            // that can catch a cached price the world has moved past. Gated on the row already clearing the
            // threshold, so it costs a handful of calls an hour rather than one per poll.
            var    screened = c;
            string verify   = "not-checked";
            if (EvMath.Ev(c.PTrueUsed, (double)restAsk, FeeM(pair.KalshiTicker)) >= _cfg.EvMin && _cfg.VerifyVenue)
            {
                verify = await _oracle.RefetchAsync(pair.Legs, ct);
                if (verify == "ok")
                {
                    // Re-screen against what the venue just said. A candidate that evaporates here was
                    // never there — it was our copy of the price being behind the play.
                    var fresh = Screen(pair, c.Side);
                    if (fresh is null) { Interlocked.Increment(ref Stats.VenueVanished); continue; }
                    screened = fresh;
                }
                else if (verify == "failed") { Interlocked.Increment(ref Stats.VenueRefused); }
            }
            await Record(pair, screened, restAsk, verify, ct);
        }
    }

    /// <summary>The free half: Pinnacle fair value against the WS book. Returns null (and counts why) for
    /// anything not worth a REST call.</summary>
    private Screened? Screen(EvPair pair, string side)
    {
        Interlocked.Increment(ref Stats.Screened);
        bool yes = side == "YES";
        if (!pair.LegsUsable) return null;

        // EVERY leg of the matchup must be quotable, open and fresh — not just the two named on this row.
        // On a 1X2 a missing draw price invalidates the home and away legs as well: S is wrong without it,
        // so there is nothing correct to normalise by. Deliberately NOT reading NoToken, which is the true
        // complement on a two-way and merely another leg on a three-way.
        var quotes = new OracleQuote[pair.Legs.Count];
        for (int i = 0; i < pair.Legs.Count; i++)
        {
            var q = _oracle.Get(pair.Legs[i]);
            if (q is null)          { Interlocked.Increment(ref Stats.NoQuote);     return null; }
            if (!q.Open)            { Interlocked.Increment(ref Stats.Suspended);   return null; }
            if (!_oracle.Fresh(q))  { Interlocked.Increment(ref Stats.StaleOracle); return null; }  // a value bet against a stale oracle is a bet against nothing
            quotes[i] = q;
        }

        var odds = quotes.Select(q => q.DecimalOdds).ToArray();
        var prop = DeVig.ProportionalN(odds);
        var shin = DeVig.ShinN(odds);
        if (!prop.Ok || !shin.Ok) return null;

        // ── THE INCOMPLETE-BOOK GUARD ─────────────────────────────────────────────────────────────────
        // A bookmaker never offers a negative margin, so S < 1 does not mean a generous price — it means we
        // are looking at only PART of the outcome set and dividing by a sum that is missing a leg.
        //
        // This is not hypothetical. The sidecar's live-board catalog path hardcodes `three_way=False` and
        // builds its legs from participants, and a soccer matchup exposes only home and away as
        // participants — the draw is a PRICE, not a participant. So an in-play soccer game arrives here
        // looking exactly like a tennis two-way. De-vigging 2.30/3.10 as if it were the whole book gives
        // S = 0.758 and P(home) = 0.574 against a true 0.40: a phantom edge on every leg, in the direction
        // that makes us bet.
        //
        // Deliberately keyed on the arithmetic rather than on the sport or the three_way flag, because the
        // flag is exactly what was wrong. Any future venue or market that loses a leg is caught the same way.
        if (prop.Overround < -0.005)
        {
            Interlocked.Increment(ref Stats.IncompleteBook);
            if (Verbose)
                Console.WriteLine($"      {pair.KalshiTicker} {side}: book sums to {prop.Overround + 1.0:0.000} "
                                + $"(<1) across {odds.Length} leg(s) — INCOMPLETE outcome set, not a free "
                                + "margin. Skipped. A soccer 1X2 catalogued as a two-way looks exactly like this.");
            return null;
        }

        int yi = pair.YesLegIndex;
        var mine = quotes[yi];

        // The Kalshi YES side pays on ITS leg; the NO side pays on everything else. Taking the complement as
        // 1 - P(yes) is correct for ANY number of legs — on a 1X2 it is exactly P(other team) + P(draw),
        // without needing to know which leg is which.
        double propYes = prop.PTrue[yi], shinYes = shin.PTrue[yi];
        if (propYes <= 0 || propYes >= 1) return null;
        double pProp = yes ? propYes : 1.0 - propYes;
        double pShin = yes ? shinYes : 1.0 - shinYes;
        double pTrue = _cfg.DeVigMethod == "shin" ? pShin : pProp;

        // Sample the fair value HERE — before the WS/prescreen returns below. The kinetic filter asks a
        // question about the ORACLE, so its history must not be conditional on Kalshi's book being readable
        // or on this row looking interesting; sampling only the interesting passes would build a history
        // made entirely of moments we already liked.
        _ptrue.GetOrAdd($"{pair.KalshiTicker}|{side}", _ => new PTrueTrack())
              .Add(DateTime.UtcNow, pTrue, TimeSpan.FromSeconds(Math.Max(30, _cfg.KineticWindowSec * 4)));

        var top = _feed.Top(pair.KalshiTicker);
        if (!top.HasSnapshot) return null;
        decimal wsAsk = yes ? top.YesAsk : top.NoAsk;
        if (wsAsk <= 0m || wsAsk >= 1m) return null;

        // WS EV is an UPPER BOUND (the ask reads low), so a candidate that fails here cannot pass at REST.
        double evWs = EvMath.Ev(pTrue, (double)wsAsk, FeeM(pair.KalshiTicker));
        if (evWs < _cfg.EvMin - _cfg.PrescreenSlack)
        { Interlocked.Increment(ref Stats.BelowPrescreen); return null; }

        return new Screened(side, pProp, pShin, pTrue, prop.Overround, shin.ShinZ,
                            mine.DecimalOdds,
                            odds.Length == 2 ? odds[1 - yi] : double.NaN,   // meaningful only on a two-way
                            prop.Overround + 1.0,
                            _oracle.AgeMs(mine), mine.MaxContracts, mine.Live,
                            wsAsk, yes ? top.YesAskDepth : top.NoAskDepth, top.AgeMs, evWs,
                            odds.Length,
                            string.Join(";", odds.Select(o => o.ToString("0.####", CultureInfo.InvariantCulture))),
                            quotes.All(q => q.WsVerified));
    }

    /// <summary>Values a screened candidate at the REST ask, sizes it, and logs it — signal or not.
    /// A row where the WS said +2c and REST said −2c is the most informative row in the file: it is the
    /// phantom being measured, and it is the reason both prices are columns.</summary>
    // ASYNC because M1 places the order from here, and it must be AWAITED: letting the screening loop
    // run ahead of its own IOC is how one market gets bought twice. In M0 (_live == null) nothing
    // awaits anything and this is a synchronous method wearing a Task.
    private async Task Record(EvPair pair, Screened c, decimal restAsk, string venueVerify,
                              CancellationToken ct)
    {
        double px   = (double)restAsk;
        double feeM = FeeM(pair.KalshiTicker);          // Kalshi's per-series multiplier, read at startup
        double fee  = EvMath.FeePerContract(px, feeM);
        double cost = EvMath.CostPerContract(px, feeM);
        double ev      = c.PTrueUsed  - cost;
        double evProp  = c.PTrueProp  - cost;
        double evShin  = c.PTrueShin  - cost;
        double limit   = EvMath.BreakEvenLimit(c.PTrueUsed, _cfg.EvMin, feeM);
        bool   inWin   = px >= _cfg.MinPrice && px <= _cfg.MaxPrice;
        var    size    = EvMath.Size(c.PTrueUsed, px, c.Vig, BankrollUsd, ActiveExposureFraction, _cfg.MaxTradeFrac, feeM);
        // CAPACITY: how many contracts the book actually offers at or below the break-even limit, and what
        // that is worth. This is the answer to "how big could this bet be" — the Kelly size says what we
        // would WANT, this says what is THERE, and only the smaller of the two is achievable.
        double depthToLimit = (double)_feed.DepthAtOrBetter(pair.KalshiTicker, c.Side == "YES", (decimal)limit);
        double capacityUsd  = depthToLimit * px;

        // TRI-STATE DEPTH. EV is priced from REST; depth is walked on the WS ladder. They agree to the cent
        // 98.6% of the time (9132 of 9266 rows), but when they DISAGREE the depth number is measured against
        // a book that does not contain the price we are quoting — the WS best sits ABOVE the break-even
        // limit while REST says the level is there and tradeable. The walk then truthfully returns 0, and
        // the row reads "buy at 0.58" and "0 available at 0.58" at once, which cannot both describe one book.
        //
        // 0 and "cannot say" are DIFFERENT FACTS and collapsing them is the arb bot's MarkDead bug exactly:
        // there, "read and empty" and "could not read" both came back -1 and a payload change masqueraded as
        // every market being dead. Here it would quietly log a Contracts size that was never available, and
        // M1 would weight its calibration by it.
        //
        // Measured 2026-08-22: 5 of 162 signals, and in 5 of those 5 the WS ask was above the limit — the
        // mechanism accounts for every case, and for none of the 157 rows that reported real depth.
        bool depthUnknown = depthToLimit <= 0 && (double)c.WsAsk > limit && px <= limit;
        if (depthUnknown) { depthToLimit = -1; capacityUsd = -1; }
        bool   clears  = ev >= _cfg.EvMin;

        // ── THE SCREENING-ONLY GATE ───────────────────────────────────────────────────────────────────
        // `wv` is the sidecar saying whether this selection is under LIVE WS coverage or is a SCREENING-ONLY
        // re-seed of an untabbed league. A screening-only quote carries a FRESH timestamp and a DELAYED
        // price, so the freshness gate cannot see it — it is fresh by every measure we had, and wrong.
        //
        // That is not theoretical. With the reader parked on the live soccer page, tennis leagues go
        // untabbed and re-seed from the delayed feed; observed 2026-08-21, Pinnacle read 0.378 on a
        // challenger match whose Kalshi book had already moved to 0.17 — a 20c "edge" that was simply an old
        // price. The size of the disagreement is the tell: on a complete book, twenty cents against a sharp
        // book is far likelier to be a stale oracle than an edge.
        //
        // The row is still WRITTEN — M0 exists to observe, and comparing the calibration of verified against
        // unverified rows is exactly how M1 measures what this costs. It just does not count as a signal.
        // ── THE IMPLAUSIBILITY BAND ───────────────────────────────────────────────────────────────────
        // A sharp book and a liquid prediction market do not disagree by twenty points about a football
        // match. When they appear to, the cause has been a data fault every single time: swapped team legs,
        // a wrong-game match, a stale price. Never once an edge.
        //
        // The measured taker EV distribution is the argument. Median −2.09c; only 0.9% of windows exceed
        // 3.5c. A 20c edge is not the tail of that distribution, it is a different distribution — and the
        // pairing's own price gate cannot be relied on to catch it, because its tolerance (0.25) was set to
        // reject GROSS mispairs and a 0.23 disagreement passes while still being worth 18c of phantom EV.
        //
        // Checked HERE rather than at pairing time because this uses the CURRENT prices on both sides: a
        // pair that validated cleanly at 03:00 can still be reading a frozen price at 19:00.
        //
        // The row is still WRITTEN, with its own decision. M0 exists to observe, and if these ever do settle
        // in our favour that is something M1 must be able to see rather than something we quietly deleted.
        double disagree = Math.Abs(c.PTrueUsed - px);
        bool implausible = disagree > _cfg.MaxDisagree;
        if (implausible && clears) Interlocked.Increment(ref Stats.Implausible);

        // ── WHO MOVED FIRST ───────────────────────────────────────────────────────────────────────────
        // The edge this bot exists to capture is Pinnacle moving BEFORE Kalshi. The mirror image of that —
        // Kalshi moving before our Pinnacle quote arrives — produces an identically-shaped signal pointing
        // in exactly the wrong direction, and it is far more common, because a goal suspends Pinnacle while
        // Kalshi keeps trading.
        //
        // Observed 2026-08-22, KXEREDIVISIEGAME-…-SPAFCU-SPA NO, one tick wide:
        //     17:29:51  pTrue 0.8631  kalshi 0.8600   -0.53c
        //     17:30:06  pTrue 0.8631  kalshi 0.7300  +11.93c   <- Sparta scored; only Kalshi knew
        //     17:31:11  pTrue 0.6808  kalshi 0.6800   -1.44c   <- Pinnacle caught up
        // Buying at 0.73 for something "worth" 0.86 meant buying something worth 0.68. Not a missed gain —
        // a 5c loss, on a quote 2.9s old by its own timestamp. Freshness cannot see this: the price was
        // current, the WORLD had changed.
        //
        // So compare the two sides' MOVEMENT since this market was last evaluated. Kalshi jumping while our
        // oracle sits still is not an opportunity, it is the sound of being last to know.
        double moveK = 0, moveP = 0;
        bool haveHistory = _lastSeen.TryGetValue($"{pair.KalshiTicker}|{c.Side}", out var prev);
        if (haveHistory)
        {
            moveK = Math.Abs(px - prev.Ask);
            moveP = Math.Abs(c.PTrueUsed - prev.PTrue);
        }
        _lastSeen[$"{pair.KalshiTicker}|{c.Side}"] = (px, c.PTrueUsed);
        bool kalshiLed    = haveHistory && moveK >= _cfg.LedMoveMin && moveP <= moveK * _cfg.LedRatio;
        bool pinnacleLed  = haveHistory && moveP >= _cfg.LedMoveMin && moveK <= moveP * _cfg.LedRatio;
        if (kalshiLed && clears) Interlocked.Increment(ref Stats.KalshiLed);
        if (pinnacleLed && clears) Interlocked.Increment(ref Stats.PinnacleLed);

        // THREE REGIMES, NOT TWO — and which of them pays is exactly what M0 exists to find out.
        //   PINNACLE_LED  our oracle moved, Kalshi has not      -> the thesis: we are ahead
        //   KALSHI_LED    Kalshi moved, our oracle has not      -> demonstrated adverse; suppressed
        //   STANDING      neither moved                         -> a persistent disagreement
        //   FIRST_LOOK    no history yet, cannot say
        //
        // STANDING is the interesting unknown. It is where a mispair hides, but it is also where a slow
        // Kalshi book would sit if it simply had not corrected yet — and those look identical until they
        // settle. Filtering it out now would make the data unable to answer the question, so it is LABELLED
        // and still counted, and M1 can split realised outcomes by regime and say which of the three the
        // edge actually lives in. `EV_REQUIRE_PINNACLE_LED=1` narrows to the thesis once that is known.
        string regime = !haveHistory ? "FIRST_LOOK"
                      : pinnacleLed  ? "PINNACLE_LED"
                      : kalshiLed    ? "KALSHI_LED"
                                     : "STANDING";

        // ── THE KINETIC FILTER ────────────────────────────────────────────────────────────────────────
        // Did OUR fair value rise over the window, or are we buying into a decline? See RequirePinnacleRising.
        double kineticRise = 0;
        bool   haveKinetic = _ptrue.TryGetValue($"{pair.KalshiTicker}|{c.Side}", out var track)
                          && track.TryRise(DateTime.UtcNow, TimeSpan.FromSeconds(_cfg.KineticWindowSec), out kineticRise);
        bool   pinRising   = haveKinetic && kineticRise >= _cfg.KineticMinRise;
        bool   notRising   = _cfg.RequirePinnacleRising && !pinRising;

        // Both de-vig methods must clear, not just the selected one. See RequireDeVigAgree.
        bool   devigAgree  = evProp >= _cfg.EvMin && evShin >= _cfg.EvMin;
        bool   devigSplit  = _cfg.RequireDeVigAgree && !devigAgree;

        // The two Kalshi sources disagree materially -> one is stale and we cannot tell which. See MaxSourceGap.
        bool sourceGap  = Math.Abs((double)(restAsk - c.WsAsk)) > _cfg.MaxSourceGap;

        bool prematch   = _cfg.RequireInPlay && !c.InPlay;
        bool unverified = _cfg.RequireWsVerified && !c.WsVerified;
        bool venueBad   = venueVerify == "failed";     // asked the venue, got nothing -> not confirmed
        // THE PRICE WINDOW WAS DECORATION. `inWin` was computed, printed, and used to pick a console colour,
        // but never entered this expression — so the 0.20-0.80 band suppressed nothing. Measured 2026-08-22:
        // 85 of 125 signals sat BELOW 0.20 and 21 of them below 0.05, all written with Decision=SIGNAL.
        // That matters beyond the trades it would have taken. Cheap in-play longshots decay toward zero on
        // the clock alone, so they drag the closing-line-value measurement negative no matter how good the
        // model is; and the tails are exactly where proportional de-vig is least trustworthy, because
        // favourite-longshot bias concentrates there. Leaving them labelled SIGNAL contaminated both the
        // trade set and every conclusion drawn from it.
        bool outOfBand  = !inWin;
        bool signal     = clears && !unverified && !implausible && !prematch && !kalshiLed && !venueBad
                       && !outOfBand && !notRising && !devigSplit && !sourceGap
                       && (!_cfg.RequirePinnacleLed || pinnacleLed);
        if (unverified && clears) Interlocked.Increment(ref Stats.ScreeningOnly);
        if (prematch && clears)   Interlocked.Increment(ref Stats.PreMatch);
        if (outOfBand && clears)  Interlocked.Increment(ref Stats.OutOfBand);
        if (notRising && clears)
        {
            if (haveKinetic) Interlocked.Increment(ref Stats.NotRising);
            else             Interlocked.Increment(ref Stats.NoKineticHistory);
        }
        if (devigSplit && clears) Interlocked.Increment(ref Stats.DeVigSplit);
        if (sourceGap && clears)  Interlocked.Increment(ref Stats.SourceGap);

        if (signal) Interlocked.Increment(ref Stats.Signals);
        else        Interlocked.Increment(ref Stats.RejectedByRest);
        if (size.FlooredToZero) Interlocked.Increment(ref Stats.FlooredToZero);

        // FOLLOW EVERY CANDIDATE THAT CLEARED, including the ones a guard just suppressed. Grading a guard
        // means watching what it threw away, and line movement answers that within a minute where settlement
        // takes days — so the suppressed rows are exactly the ones worth following.
        string decision = signal ? "SIGNAL"
                        : venueBad ? "VENUE_REFUSED" : implausible ? "IMPLAUSIBLE"
                        : kalshiLed ? "KALSHI_LED" : prematch ? "SIGNAL_PREMATCH"
                        : outOfBand ? "OUT_OF_BAND"
                        : sourceGap ? "SOURCE_DISAGREE"
                        : devigSplit ? "DEVIG_DISAGREE"
                        : notRising ? (haveKinetic ? "NOT_RISING" : "NO_KINETIC_HISTORY")
                        : "SIGNAL_UNVERIFIED";
        // ── M1: TAKE IT ───────────────────────────────────────────────────────────────────────────────
        // FIRES ON `signal` ONLY, and immediately — no further checks between the decision and the order.
        // Awaited rather than fire-and-forget: an IOC resolves in well under a second, and letting the
        // screening loop run ahead of its own order is how the same market gets bought twice.
        // `_live` is null in M0, so the order API is not merely unused but unreachable.
        if (signal && _live is not null)
            await _live.TryTakeAsync(pair.KalshiTicker, pair.EventId, c.Side, limit, px,
                                     c.PTrueUsed, ev,
                                     new TakeCtx((double)c.WsAsk, depthUnknown ? -1 : depthToLimit,
                                                 c.InPlay, c.OracleAgeMs, c.WsBookAge, regime), ct);

        if (clears)
            _followUp?.Schedule(new FollowUp(DateTime.UtcNow, pair.KalshiTicker, c.Side, pair.Legs,
                pair.YesLegIndex, decision, regime, px, c.PTrueUsed, ev, _cfg.DeVigMethod));

        _telemetry.Write(new EvSignal(
            DateTime.UtcNow, pair.KalshiTicker, pair.EventId, c.Side, pair.KalshiOutcome, pair.EventTitle,
            pair.SettlementDate, c.InPlay,
            c.PinMine, c.PinOther, c.PinSum, c.Vig, c.ShinZ,
            c.PTrueProp, c.PTrueShin, c.PTrueUsed, _cfg.DeVigMethod, c.OracleAgeMs, c.OracleDepth,
            c.WsAsk, restAsk, c.WsBookAge, c.WsDepth,
            fee, cost, evProp, evShin, ev, c.EvWs, limit,
            size, BankrollUsd, EvMath.OrderFee(px, size.Contracts, feeM), size.Contracts * px,
            inWin, signal ? "SIGNAL" : !clears ? "REJECTED_REST"
                          : implausible ? "IMPLAUSIBLE"
                          : venueBad ? "VENUE_REFUSED"
                          : kalshiLed ? "KALSHI_LED"
                          : outOfBand ? "OUT_OF_BAND"
                          : sourceGap ? "SOURCE_DISAGREE"
                          : devigSplit ? "DEVIG_DISAGREE"
                          : notRising ? (haveKinetic ? "NOT_RISING" : "NO_KINETIC_HISTORY")
                          : _cfg.RequirePinnacleLed && !pinnacleLed ? "NOT_PINNACLE_LED"
                          : prematch ? "SIGNAL_PREMATCH" : "SIGNAL_UNVERIFIED",
            c.NumLegs, c.PinOddsAll, c.WsVerified, depthToLimit, capacityUsd, regime, venueVerify,
            haveKinetic ? kineticRise * 100 : double.NaN, devigAgree));

        if (clears)
        {
            var col = !signal ? ConsoleColor.DarkGray : inWin ? ConsoleColor.Green : ConsoleColor.DarkYellow;
            Con.Line(col,
                $"{(signal ? "[+EV]" : "[~EV]")} {pair.KalshiTicker} {c.Side,-3} ev={ev * 100:+0.00;-0.00}c  "
              + $"pTrue={c.PTrueUsed:0.0000}  rest={restAsk:0.0000} (ws {c.WsAsk:0.0000}, "
              + $"gap {(double)(restAsk - c.WsAsk) * 100:+0.0;-0.0}c)  limit={limit:0.0000}  "
              + $"size={size.Contracts}/{(depthUnknown ? "?" : $"{depthToLimit:0}")} avail ({(depthUnknown ? "ws book does not reach the limit — REST says it is there" : $"${capacityUsd:0}")})  {regime}  vig={c.Vig:0.0000}{(c.NumLegs > 2 ? $"  [{c.NumLegs}-way]" : "")}{(inWin ? "" : "  [outside price window]")}"
              + $"{(size.FlooredToZero ? "  [floored to 0 contracts]" : "")}"
              + $"{(signal ? "" : implausible ? $"  [IMPLAUSIBLE: we say {c.PTrueUsed:0.000}, Kalshi says {px:0.000} — "
                                     + $"a {disagree * 100:0}pt gap is a pairing fault, not an edge]"
                                   : kalshiLed ? $"  [KALSHI LED: it moved {moveK * 100:0.0}c since the last "
                                                 + $"look, our oracle {moveP * 100:0.0}c — we are FOLLOWING, not ahead]"
                                   : prematch ? "  [PRE-MATCH: logged for calibration, not tradeable]"
                                   : "  [SCREENING-ONLY oracle: fresh timestamp, DELAYED price — not a signal]")}");
        }
        else if (Verbose)
        {
            Console.WriteLine($"      {pair.KalshiTicker} {c.Side,-3} ws said {c.EvWs * 100:+0.0;-0.0}c, "
                            + $"REST says {ev * 100:+0.0;-0.0}c — not a signal");
        }
    }

    /// <summary>Ask in dollars from the /markets convenience fields, dollar-string first and cents-integer
    /// as a fallback. -1 when the market quotes no ask at all.</summary>
    public static decimal AskDollars(JsonElement mkt, bool yes)
    {
        string[] dollarKeys = yes ? ["yes_ask_dollars", "yes_ask_price"] : ["no_ask_dollars", "no_ask_price"];
        foreach (var k in dollarKeys)
        {
            if (!mkt.TryGetProperty(k, out var el)) continue;
            string? s = el.ValueKind == JsonValueKind.String ? el.GetString() : el.GetRawText();
            if (decimal.TryParse(s, NumberStyles.Any, CultureInfo.InvariantCulture, out var p) && p > 0m) return p;
        }
        if (mkt.TryGetProperty(yes ? "yes_ask" : "no_ask", out var c) && c.ValueKind == JsonValueKind.Number)
        {
            decimal cents = c.GetDecimal();
            if (cents > 0m) return Math.Round(cents / 100m, 4);
        }
        return -1m;
    }
}
