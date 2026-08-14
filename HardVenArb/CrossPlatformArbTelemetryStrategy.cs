using System.Collections.Concurrent;
using System.Text;
using System.Threading.Channels;
using PredictionBacktester.Engine;

namespace HardVenArb;

// ── Data types ────────────────────────────────────────────────────────────────

public record CrossPair(
    string PairId,
    string Label,
    string KalshiTicker,    // book keys: "K:{ticker}" and "K:{ticker}_NO"
    string HardVenYesTokenId,  // book key:  "H:{yesToken}"
    string HardVenNoTokenId,   // book key:  "H:{noToken}"
    string EventId = "",    // retained for JSON compat; not used internally
    DateOnly? SettlementDate = null,
    bool IsNegRisk = false, // passed to CLOB negRisk flag on HardVen order submission
    decimal HardVenMinSize = 1.0m, // orderMinSize from HardVen Gamma API (minimum shares per order)
    // 3-way market (e.g. soccer 1X2): ONLY the Kalshi-NO direction (K_NO_P_YES) is a complete hedge —
    // Kalshi NO(A) + book back-A covers A / Draw / B. The K_YES_P_NO direction (Kalshi YES + book back-B)
    // would miss the draw, so it's disabled for these pairs. Both tokens are still the two team moneylines.
    bool ThreeWay = false,
    // The Kalshi YES side's NAME ("Sabrina Dias"). Carried so the executor can prove the book leg is the
    // OPPOSITE participant before firing: an inverted pairing produces two bets on the SAME outcome, which
    // no price test can catch at a coin flip (2026-08-12, Dias vs Kawano Cho).
    string KalshiOutcome = ""
);

record ActiveWindow(
    string   PairId,
    string   ArbType,             // "K_YES_P_NO" or "K_NO_P_YES"
    DateTime StartTime,
    decimal  EntryGrossCost,
    decimal  EntryNetCost,
    string   EntryLegPrices,
    decimal  BestGrossCost,
    decimal  BestNetCost,
    string   BestLegPrices,
    decimal  KalshiDepth,
    decimal  HardVenDepth,
    decimal  KalshiFees,
    decimal  HardVenFees,
    long     KalshiBookAgeMs,
    long     HardVenBookAgeMs,
    decimal  KalshiMidSum,
    decimal  HardVenMidSum,
    int      KalshiDropsAtOpen,
    int      HardVenDropsAtOpen,
    int      DaysToSettlement,
    decimal  AprHoldToSettle,
    int      UpdateCount,
    bool     RestChecked   = false,
    bool     RestConfirmed = false,
    decimal  RestKalshiAsk = -1m,
    decimal  RestHardVenAsk   = -1m,
    long     RestDelayMs   = -1,
    string   OpenedBy      = "",   // which side's price move CREATED the arb: KALSHI / HARDVEN / BOTH / INITIAL
    decimal  OpenKLeg      = -1m,  // the Kalshi leg price at open (for held/move comparison at close)
    decimal  OpenPLeg      = -1m,  // the HardVen leg price at open
    bool     HardVenWsVerified = false,  // HardVen leg was under LIVE WS coverage at some point in the window
                                         // (not screening-only) → the arb is confirmed on real-time prices
    bool     OpenedInPlay  = false  // regime the window was OPENED in. A window must describe ONE regime: it is
                                    // split at a kickoff (see WENT_LIVE) so this also holds at close.
)
{
    // First eval at which each leg's ask rose ABOVE its open price (moved against you → LEFT the "within the
    // arb" zone). MaxValue = never left → within the whole window. Drives the per-leg "time HELD WITHIN the arb"
    // = (LeftWithinAt ?? closeTime) − StartTime — how long after open each side stayed at-or-better than its
    // opening price (the capturable-target window for that leg). Mutable so it updates free each eval and
    // `with`-copies carry it forward.
    public DateTime KLeftWithinAt { get; set; } = DateTime.MaxValue;
    public DateTime PLeftWithinAt { get; set; } = DateTime.MaxValue;
}

// ── Strategy ──────────────────────────────────────────────────────────────────

public class CrossPlatformArbTelemetryStrategy
{
    private volatile IReadOnlyList<CrossPair> _pairs;
    private readonly ConcurrentDictionary<string, LocalOrderBook> _books;
    private readonly decimal _arbThreshold;
    private readonly decimal _depthFloor;

    // HARDVEN_DEBUG_PRICES=1 → on every arb OPEN, dump the full 4-leg breakdown (both sides' ask/bid ladders
    // + Pinnacle decimal odds) so a suspiciously-deep window can be inspected leg-by-leg against the venues.
    private readonly bool _debugPrices = Environment.GetEnvironmentVariable("HARDVEN_DEBUG_PRICES") == "1";

    // bookKey → pair indices (fast lookup on every delta)
    private readonly Dictionary<string, List<int>> _bookKeyToPairs;
    private readonly ReaderWriterLockSlim _indexLock = new(LockRecursionPolicy.NoRecursion);

    // pairId → open window (null = no arb active)
    private readonly Dictionary<string, ActiveWindow?> _activeWindows;
    // pairId → post-open hedge monitor (prices the Kalshi unwind if the slow HardVen leg fails). One per pair;
    // a new arb open resets it. Outlives the arb window (which usually closes in <1s) up to HedgeHorizonMs.
    private readonly ConcurrentDictionary<string, HedgeMonitor> _hedgeMonitors = new();
    private readonly ConcurrentDictionary<string, (decimal Cost, string Type, decimal Depth)> _nearMiss = new();

    // ── Hedge monitor ──────────────────────────────────────────────────────────
    // Tracks ONE arb-open event's post-open price trajectory so the analyzer can price the WORST-CASE hedge
    // when the slow, irreversible HardVen leg fails to fill: in the Kalshi-first model you commit the fast,
    // reversible Kalshi leg at open, then fire HardVen; if HardVen misses you must UNWIND the Kalshi leg by
    // selling it back (or buying the opposite leg to lock). This monitor samples the unwind price for
    // HedgeHorizonMs after open — independent of the arb window, which usually closes in <1s — and the
    // analyzer's --hedge-secs picks the realization instant. OpenTime == the window StartTime (the join key).
    private sealed class HedgeMonitor
    {
        public string   PairId = "";
        public string   Label = "";
        public string   ArbType = "";          // K_YES_P_NO = hold Kalshi YES; K_NO_P_YES = hold Kalshi NO
        public DateTime OpenTime;
        public decimal  EntryKalshiAsk;          // price paid for the committed Kalshi leg at open
        public decimal  EntryHardVenAsk;         // the HardVen leg price at open (the leg that may fail)
        public decimal  EntryNetCost;
        public decimal  EntryDepth;
        public DateTime LastSampleAt = DateTime.MinValue;
    }

    // How long after an arb opens to keep sampling the unwind price (covers the 6–12s realization delay plus a
    // look at whether the position can be "fixed" back to break-even). HARDVEN_HEDGE_MONITOR_SECS: a positive
    // number overrides the 30s window; 0 = DISABLED (no hedge tape — a clean baseline / isolate other
    // telemetry); unset or invalid = 30s default.
    private static readonly int HedgeHorizonMs =
        int.TryParse(Environment.GetEnvironmentVariable("HARDVEN_HEDGE_MONITOR_SECS"), out var hs) && hs >= 0
            ? hs * 1000 : 30_000;
    private static readonly bool HedgeMonitorEnabled = HedgeHorizonMs > 0;
    private const int HedgeSampleIntervalMs = 200;   // throttle: at most one sample per pair per 200ms

    // Per-pair last-seen leg asks + when each last CHANGED — to attribute which side opened/closed a window.
    // Kalshi = fast WS side (ms); HardVen = slow ~9s poll. Updated under _windowLock on every evaluation.
    private sealed class LegMoveState
    {
        public bool Primed;
        public decimal KYes = -1m, KNo = -1m, PYes = -1m, PNo = -1m;
        public DateTime KYesAt, KNoAt, PYesAt, PNoAt;
    }
    private readonly Dictionary<string, LegMoveState> _legMoves = new(StringComparer.Ordinal);

    // ── Fee model ─────────────────────────────────────────────────────────────
    // Kalshi: 0.07 × p × (1-p) per contract.
    // HardVen:   r × (p×(1-p))^e per share — r and e from /clob-markets fd, fetched at startup.
    //   HardVenFeeRates  = base_fee per token  → feeRateBps for order submission only.
    //   HardVenFeeParams = (r, e) per token    → fee math only.
    private const decimal KalshiFeeRate = 0.07m;

    /// <summary>Shared with CrossArbExecutor — base_fee per token, used for order submission feeRateBps.</summary>
    public ConcurrentDictionary<string, int>? HardVenFeeRates { get; set; }

    /// <summary>Shared with CrossArbExecutor — (r, e) fee curve params per token, used in HardVenFee math.</summary>
    public ConcurrentDictionary<string, (decimal R, double E)>? HardVenFeeParams { get; set; }

    private static decimal KalshiFee(decimal p) => KalshiFeeRate * p * (1m - p);

    // HardVen (sportsbook) charges NO separate fee — the bookmaker's vig/overround is already baked into
    // the odds, i.e. into the price we pay (1/decimal_odds). Charging a fee on top would double-count the
    // margin. So the only per-contract fee in the net cost is Kalshi's. (HardVenFeeParams is retained for a
    // future reversible-exchange venue that DOES charge commission; a back-only book doesn't.)
    private decimal HardVenFee(decimal p, string tokenId) => 0m;

    // ── WS drop counters ─────────────────────────────────────────────────────
    private int _kalshiWsDrops;
    private int _hardvenWsDrops;

    private readonly object _windowLock = new();

    // ── VERIFY-ON-DETECTION ─────────────────────────────────────────────────────
    // The sidecar tags each /odds price 'wv' = under live WS coverage (a tab / recent push) vs SCREENING-ONLY
    // (an httpx re-seed of an untabbed tail league). We record it per HardVen token; when a window opens on a
    // screening-only leg we ask the sidecar to promote that league to a live tab (_requestHardVenVerify) so the
    // arb gets confirmed on real-time prices, and we log HardVenWsVerified so analysis can trust WS-confirmed
    // windows. Absent 'wv' (paho/REST mode, other adapters) → treated as verified (no-op).
    private readonly ConcurrentDictionary<string, bool> _hardvenVerified = new(StringComparer.Ordinal);
    private readonly ConcurrentDictionary<string, DateTime> _verifyRequested = new(StringComparer.Ordinal);
    private readonly Action<string>? _requestHardVenVerify;
    private static readonly TimeSpan VerifyDedupe = TimeSpan.FromSeconds(60);

    /// <summary>Feed hook: record a HardVen token's WS-verified flag from the /odds 'wv' tag (default true).</summary>
    public void SetHardVenVerified(string token, bool verified) => _hardvenVerified[token] = verified;

    /// <summary>Is this HardVen token under live WS coverage (wv=true) vs SCREENING-ONLY (an httpx re-seed of an
    /// untabbed tail league)? Unknown token → true (safe default; non-reader mode never emits 'wv', so nothing is
    /// treated as screening-only there). The executor gates real placement on this so a bet never fires on an
    /// unverified screening-only price.</summary>
    public bool IsHardVenVerified(string token) =>
        !_hardvenVerified.TryGetValue(token, out var v) || v;

    // Can the venue put this token's event on a BETSLIP? Only meaningful for books that publish it.
    private readonly ConcurrentDictionary<string, bool> _hardvenAccaOk = new(StringComparer.Ordinal);
    public int SlipVerifySkippedNotQuotable;   // surfaced in status output

    /// <summary>Feed hook: record whether the venue will slip-quote this token, from /odds 'acca'.</summary>
    public void SetHardVenAccaOk(string token, bool ok) => _hardvenAccaOk[token] = ok;

    private static readonly bool SkipNonAccaSamples =
        Environment.GetEnvironmentVariable("HARDVEN_SLIP_SKIP_NON_ACCA") == "1";

    /// <summary>Unknown token → true. A book that never publishes the flag must behave exactly as before,
    /// and a token we simply have not polled yet must not be silently excluded from sampling.</summary>
    public bool IsHardVenAccaOk(string token) =>
        !_hardvenAccaOk.TryGetValue(token, out var v) || v;

    /// <summary>Ask the sidecar to promote a league to a live WS tab (verify-on-detection), deduped per league.</summary>
    private void RequestVerify(string hardvenToken)
    {
        if (_requestHardVenVerify == null || string.IsNullOrEmpty(hardvenToken)) return;
        int c = hardvenToken.IndexOf(':');
        string lid = c > 0 ? hardvenToken.Substring(0, c) : hardvenToken;
        var now = DateTime.UtcNow;
        if (_verifyRequested.TryGetValue(lid, out var last) && now - last < VerifyDedupe) return;
        _verifyRequested[lid] = now;
        try { _requestHardVenVerify(lid); } catch { /* best-effort */ }
    }

    // ── CSV channels ─────────────────────────────────────────────────────────
    private readonly Channel<string> _csvChannel =
        Channel.CreateUnbounded<string>(new UnboundedChannelOptions { SingleReader = true });
    private readonly string _csvBaseName;   // DAILY rotation: file = "{base}_{yyyyMMdd}.csv" (local date)

    private readonly Channel<string> _hedgeCsvChannel =
        Channel.CreateUnbounded<string>(new UnboundedChannelOptions { SingleReader = true });
    private readonly string _hedgeCsvBaseName;

    private readonly Channel<string> _slipCsvChannel =
        Channel.CreateUnbounded<string>(new UnboundedChannelOptions { SingleReader = true });
    private readonly string _slipCsvBaseName;

    // Column headers — written by the writer task each time it opens a NEW/empty dated file (kept here so
    // rotation re-emits them). Must stay in lockstep with the row builders below.
    private const string CsvHeader =
        "StartTime,EndTime,DurationMs,PairId,Label,ArbType," +
        "EntryGrossCost,EntryNetCost,EntryLegPrices," +
        "BestGrossCost,BestNetCost,BestLegPrices,TotalFees,KalshiFees,HardVenFees,NetProfitPerShare," +
        "KalshiDepth,HardVenDepth,MaxDepth,TotalCapitalRequired,TotalPotentialProfit," +
        "KalshiBookAgeMs,HardVenBookAgeMs,KalshiMidSum,HardVenMidSum," +
        "KalshiWsDropsAtOpen,HardVenWsDropsAtOpen,DropDuringWindow," +
        "UpdateCount,ClosedBy," +
        "DaysToSettlement,AprHoldToSettle," +
        "RestChecked,RestConfirmed,RestKalshiAsk,RestHardVenAsk,RestDelayMs," +
        "OpenedBy,ClosedBySide,KalshiLegAgeMsAtClose,HardVenLegAgeMsAtClose,HardVenLegHeld,HardVenLegId," +
        "KalshiLegWithinMs,HardVenLegWithinMs," +
        "HardVenInPlay,HardVenWsVerified";
    private const string HedgeCsvHeader =
        "OpenTime,PairId,Label,ArbType,OffsetMs," +
        "EntryKalshiAsk,KalshiUnwindBid,KalshiOppositeAsk,KalshiEntryAskNow," +
        "EntryHardVenAsk,HardVenLegNow,KalshiUnwindDepth,EntryNetCost";
    // SLIP VERIFY. Separate file from the telemetry on purpose: this is a SAMPLE (one arb every few
    // minutes), not the tape. Mixing a sparse sample into the window log would let any per-row average
    // silently mean "of the few we happened to click".
    // ArbSurvived answers the actual question — was it still an arb once the venue quoted its real price.
    private const string SlipCsvHeader =
        "Time,PairId,Label,ArbType,Regime," +
        "BoardHardVenAsk,SlipHardVenAsk,SlippageAbs,SlippagePct," +
        "KalshiAskAtOpen,KalshiAskNow,NetAtBoard,NetAtSlip,ArbSurvived,MarginVsThreshold," +
        "KalshiBestDepth,HardVenBestDepth,KalshiTop3Depth,HardVenTop3Depth," +
        "QuoteMs,HardVenLegId,Error," +
        // HOLD PHASE — how long the arb survived on prices the venue would actually honour. The window
        // durations in the main telemetry are measured on BOARD prices, which overstate an arb; this is
        // the capturable life of the same window, re-read from the live slip feed until it dies.
        "HeldMs,HoldSamples,BestNetHeld,DiedBy";

    // ── Sampled slip verifier ─────────────────────────────────────────────────
    // Measures what the BOARD price is actually worth: the board is a cache the venue does not commit to,
    // the betslip is. Deliberately a SAMPLE, not a check on every arb — each quote is a real navigation
    // and click at the venue, and anti-detection is a hard constraint, so it is rate-limited globally
    // (not per pair: the venue sees one bot, not N pairs).
    // PRE-LIVE ONLY. In-play prices move on their own during the seconds a quote takes, so an in-play
    // sample cannot separate "the board lied" from "the game moved" — the one thing this exists to measure.
    private Func<string, double, Task<(decimal Price, string Error)>>? _slipQuote;
    private static readonly bool SlipVerifyEnabled =
        Environment.GetEnvironmentVariable("HARDVEN_SLIP_VERIFY") == "1";
    // THREE INDEPENDENT BUDGETS, not one. What a sample COSTS and what it is WORTH both vary:
    //   pre-live   the only regime we trade, and a parked board serves it in ~0.5s  -> sample often
    //   in-play    telemetry only under PRELIVE_ONLY, still worth measuring          -> slower
    //   rover      had to navigate to the league: seconds, and a real UI action      -> slowest
    // Separate budgets rather than one shared clock means an in-play sample can never consume the slot a
    // pre-live arb would have used — which is what "pre-live has priority" has to mean when you cannot
    // know what is coming next. The rover gate is checked ON TOP of whichever regime gate applies, so a
    // run that keeps falling back to navigation throttles itself without also throttling board quotes.
    private static int EnvSec(string name, int fallback) =>
        Math.Max(1, int.TryParse(Environment.GetEnvironmentVariable(name), out var v) && v > 0 ? v : fallback) * 1000;
    private static readonly int SlipVerifyPreLiveMs = EnvSec("HARDVEN_SLIP_VERIFY_PRELIVE_SEC", 60);
    private static readonly int SlipVerifyInPlayMs  = EnvSec("HARDVEN_SLIP_VERIFY_INPLAY_SEC", 180);
    private static readonly int SlipVerifyRoverMs   = EnvSec("HARDVEN_SLIP_VERIFY_ROVER_SEC", 300);
    // How soon a refused-without-clicking sample may be retried (see RefundSlipVerifyBudget).
    private static readonly int SlipVerifyRefundFloorMs = EnvSec("HARDVEN_SLIP_VERIFY_REFUND_FLOOR_SEC", 5);
    // HOLD THE SLIP after a successful quote and watch the arb until it stops breaking even. Costs no UI
    // action at all: the event is already subscribed on the acca channel, so the venue keeps pushing and a
    // re-quote is served from cache. 0 disables the hold and restores the single-shot sample.
    private static readonly int SlipHoldMs     = EnvSec("HARDVEN_SLIP_HOLD_SEC", 120);
    private static readonly int SlipHoldPollMs = EnvSec("HARDVEN_SLIP_HOLD_POLL_SEC", 2);
    // The net at which the held arb is declared dead. BREAK-EVEN by default, matching what the executor
    // would actually accept — the question this measures is "how long could I still have taken it", not
    // "how long did it stay as good as it first looked".
    private static readonly decimal _slipAcceptNetForHold =
        decimal.TryParse(Environment.GetEnvironmentVariable("HARDVEN_SLIP_ACCEPT_NET"),
                         System.Globalization.NumberStyles.Any,
                         System.Globalization.CultureInfo.InvariantCulture, out var _sah) && _sah > 0m
            ? _sah : 1.00m;
    private long _lastRoverTicks;
    private static readonly double SlipVerifyTimeoutSec =
        double.TryParse(Environment.GetEnvironmentVariable("HARDVEN_SLIP_VERIFY_TIMEOUT_SEC"),
                        System.Globalization.NumberStyles.Any,
                        System.Globalization.CultureInfo.InvariantCulture, out var _svt) && _svt > 0 ? _svt : 25.0;
    private long _lastSlipVerifyTicks;      // pre-live budget
    private long _lastInPlayVerifyTicks;    // in-play budget (independent of the above)
    private int  _slipVerifyInFlight;      // one at a time: the rover is a single tab
    public int   SlipVerifyCount;          // surfaced in status output

    /// <summary>Wire the betslip reader (the sidecar's /slip_quote). Set after construction because the
    /// verifier that owns it needs this strategy first. Null = sampling disabled.</summary>
    public void SetSlipVerifier(Func<string, double, Task<(decimal Price, string Error)>>? slipQuote,
                                Func<string>? slipVia = null,
                                Func<bool>? slipClicked = null,
                                Func<bool>? slipAccaFlagged = null)
    {
        _slipQuote   = slipQuote;
        _slipVia     = slipVia;      // reads back which tier served the last quote (see the rover cooldown)
        _slipClicked = slipClicked;  // ...and whether it cost a click at all (see RefundSlipVerifyBudget)
        _slipAccaFlagged = slipAccaFlagged;
    }
    private Func<string>? _slipVia;
    private Func<bool>?   _slipClicked;
    private Func<bool>?   _slipAccaFlagged;

    // ── Public stats ──────────────────────────────────────────────────────────
    public int OpenArbs   => _activeWindows.Values.Count(w => w != null);
    public int TotalPairs => _pairs.Count;

    public event Action<string, decimal, string, decimal, decimal, decimal>? OnArbOpened;

    /// <summary>Fires after every book update — subscribers (e.g. executor) use this for event-driven exit checks.</summary>
    public event Action<string>? BookUpdated;

    public CrossPair? GetPair(string pairId) => _pairs.FirstOrDefault(p => p.PairId == pairId);
    public IReadOnlyList<CrossPair> GetAllPairs() => _pairs;

    public CrossPlatformArbTelemetryStrategy(
        IReadOnlyList<CrossPair> pairs,
        ConcurrentDictionary<string, LocalOrderBook> books,
        decimal arbThreshold = 0.995m,
        decimal depthFloor   = 1m,
        Action<string>? requestHardVenVerify = null)
    {
        _pairs        = pairs;
        _books        = books;
        _arbThreshold = arbThreshold;
        _depthFloor   = depthFloor;
        _requestHardVenVerify = requestHardVenVerify;
        // Same HARDVEN_OUTPUT_TAG as the journal: two venues running side by side must not interleave
        // their telemetry, or every downstream analysis silently mixes two books' arb windows into one
        // tape. Unset = the historic names, so existing files and the analyzer keep working unchanged.
        string outTag = (Environment.GetEnvironmentVariable("HARDVEN_OUTPUT_TAG") ?? "").Trim();
        string tagSfx = outTag.Length > 0 ? "_" + outTag : "";
        _csvBaseName      = "CrossArbTelemetry" + tagSfx;
        _hedgeCsvBaseName = "CrossArbHedgeMonitor" + tagSfx;
        _slipCsvBaseName  = "CrossArbSlipVerify" + tagSfx;
        // Let the FIRST pre-live arb be sampled immediately rather than waiting out one interval — a short
        // session would otherwise collect nothing at all.
        // Both budgets start already elapsed so the FIRST arb of a session is sampled at once,
        // rather than a short run collecting nothing while it waits out an interval.
        _lastSlipVerifyTicks     = Environment.TickCount64 - SlipVerifyPreLiveMs;
        _lastInPlayVerifyTicks   = Environment.TickCount64 - SlipVerifyInPlayMs;

        _bookKeyToPairs = new Dictionary<string, List<int>>(StringComparer.Ordinal);
        _activeWindows  = new Dictionary<string, ActiveWindow?>(StringComparer.Ordinal);

        for (int i = 0; i < pairs.Count; i++)
        {
            var p = pairs[i];
            foreach (var key in new[] { $"K:{p.KalshiTicker}", $"K:{p.KalshiTicker}_NO",
                                         $"H:{p.HardVenYesTokenId}", $"H:{p.HardVenNoTokenId}" })
            {
                if (!_bookKeyToPairs.TryGetValue(key, out var list))
                    _bookKeyToPairs[key] = list = new List<int>();
                list.Add(i);
            }
            _activeWindows[p.PairId] = null;
        }

        _csvWriterTask      = Task.Run(RunCsvWriterAsync);
        // skip the hedge CSV entirely when disabled (HARDVEN_HEDGE_MONITOR_SECS=0) so no empty file is written
        _hedgeCsvWriterTask = HedgeMonitorEnabled ? Task.Run(RunHedgeCsvWriterAsync) : Task.CompletedTask;
        _slipCsvWriterTask  = SlipVerifyEnabled  ? Task.Run(RunSlipCsvWriterAsync)  : Task.CompletedTask;
        if (SlipVerifyEnabled)
            Console.WriteLine($"[SLIP VERIFY] sampling pre-live every {SlipVerifyPreLiveMs / 1000}s, "
                            + $"in-play every {SlipVerifyInPlayMs / 1000}s, "
                            + $"+{SlipVerifyRoverMs / 1000}s cooldown after any rover quote "
                            + $"-> {_slipCsvBaseName}_{CsvDate()}.csv");
        DebugLog.Discovery($"CrossPlatformArbTelemetryStrategy: initialized with {pairs.Count} pairs, threshold={arbThreshold}");
        if (_debugPrices)
            Console.WriteLine("[CROSS] HARDVEN_DEBUG_PRICES=1 — dumping the full 4-leg price breakdown on each arb open.");
    }

    // ── Public interface ──────────────────────────────────────────────────────

    public void OnBookUpdate(string bookKey)
    {
        List<int>? indices = null;
        _indexLock.EnterReadLock();
        try { _bookKeyToPairs.TryGetValue(bookKey, out indices); }
        finally { _indexLock.ExitReadLock(); }

        if (indices == null)
        {
            DebugLog.Discovery($"OnBookUpdate: no pairs registered for bookKey={bookKey}");
            return;
        }

        var pairsSnap = _pairs;
        foreach (var idx in indices) EvaluatePair(pairsSnap[idx]);
        BookUpdated?.Invoke(bookKey);
    }

    public void OnKalshiReconnect() => HandlePlatformReconnect(ref _kalshiWsDrops, "KALSHI");
    public void OnHardVenReconnect()   => HandlePlatformReconnect(ref _hardvenWsDrops,   "HARDVEN");

    private void HandlePlatformReconnect(ref int counter, string platform)
    {
        int newCount = Interlocked.Increment(ref counter);
        Console.WriteLine($"[CROSS WS] {platform} reconnect #{newCount} — closing all open windows");
        lock (_windowLock)
        {
            foreach (var pairId in _activeWindows.Keys.ToList())
            {
                if (_activeWindows[pairId] is { } w)
                {
                    DebugLog.Discovery($"HandlePlatformReconnect: closing {pairId} ({w.ArbType}) on {platform} reconnect");
                    CloseWindow(pairId, w, DateTime.UtcNow, "RECONNECT");
                    _activeWindows[pairId] = null;
                }
            }
        }
        _nearMiss.Clear();
    }

    public void UpdateRestVerification(string pairId, bool confirmed,
        decimal kalshiAsk, decimal hardvenAsk, long delayMs)
    {
        lock (_windowLock)
        {
            if (!_activeWindows.TryGetValue(pairId, out var w) || w == null)
            {
                DebugLog.Discovery($"UpdateRestVerification: window for {pairId} already closed, ignoring");
                return;
            }
            _activeWindows[pairId] = w with
            {
                RestChecked   = true,
                RestConfirmed = confirmed,
                RestKalshiAsk = kalshiAsk,
                RestHardVenAsk   = hardvenAsk,
                RestDelayMs   = delayMs
            };
            if (!confirmed)
            {
                DebugLog.Discovery($"UpdateRestVerification: {pairId} not confirmed by REST — K={kalshiAsk:0.0000} P={hardvenAsk:0.0000} in {delayMs}ms");
                return;
            }

            var pair     = _pairs.FirstOrDefault(p => p.PairId == pairId);
            string label = pair?.Label ?? pairId;
            decimal depth = Math.Min(w.KalshiDepth, w.HardVenDepth);
            string aprStr = w.AprHoldToSettle >= 0m ? $" APR={w.AprHoldToSettle:P0}" : "";
            Console.WriteLine($"[CONFIRMED ARB] {label} | {w.ArbType} | " +
                              $"K={kalshiAsk:0.0000} P={hardvenAsk:0.0000} net=${kalshiAsk + hardvenAsk:0.0000} | " +
                              $"depth={depth:0.0} (K={w.KalshiDepth:0.0}/P={w.HardVenDepth:0.0}){aprStr} | verified in {delayMs}ms");
        }
    }

    public void AddPairs(IReadOnlyList<CrossPair> newPairs)
    {
        if (newPairs.Count == 0) return;

        _indexLock.EnterWriteLock();
        try
        {
            var merged = new List<CrossPair>(_pairs);
            int baseIdx = merged.Count;
            merged.AddRange(newPairs);
            _pairs = merged.AsReadOnly();

            for (int i = 0; i < newPairs.Count; i++)
            {
                var p   = newPairs[i];
                int idx = baseIdx + i;
                foreach (var key in new[] { $"K:{p.KalshiTicker}", $"K:{p.KalshiTicker}_NO",
                                             $"H:{p.HardVenYesTokenId}", $"H:{p.HardVenNoTokenId}" })
                {
                    if (!_bookKeyToPairs.TryGetValue(key, out var list))
                        _bookKeyToPairs[key] = list = new List<int>();
                    list.Add(idx);
                }
                DebugLog.Discovery($"AddPairs: registered pair {p.PairId} ({p.Label})");
            }
        }
        finally { _indexLock.ExitWriteLock(); }

        lock (_windowLock)
        {
            foreach (var p in newPairs)
                if (!_activeWindows.ContainsKey(p.PairId))
                    _activeWindows[p.PairId] = null;
        }

        Console.WriteLine($"[CROSS] +{newPairs.Count} pair(s) loaded. Total: {_pairs.Count}");
    }

    public IEnumerable<(decimal Cost, string Label, string PairId, string ArbType, decimal Depth, bool IsLive)>
        GetNearMissSnapshot()
    {
        HashSet<string> liveIds;
        lock (_windowLock)
            liveIds = _activeWindows.Where(kv => kv.Value != null).Select(kv => kv.Key).ToHashSet();

        return _nearMiss
            .Select(kv =>
            {
                string label = _pairs.FirstOrDefault(p => p.PairId == kv.Key)?.Label ?? kv.Key;
                return (kv.Value.Cost, label, kv.Key, kv.Value.Type, kv.Value.Depth, liveIds.Contains(kv.Key));
            })
            .OrderBy(x => x.Cost);
    }

    // ── Core evaluation ───────────────────────────────────────────────────────

    private void EvaluatePair(CrossPair pair)
    {
        if (!_books.TryGetValue($"K:{pair.KalshiTicker}",    out var kYes))
        {
            DebugLog.Discovery($"EvaluatePair {pair.Label}: missing book K:{pair.KalshiTicker}");
            return;
        }
        if (!_books.TryGetValue($"K:{pair.KalshiTicker}_NO", out var kNo))
        {
            DebugLog.Discovery($"EvaluatePair {pair.Label}: missing book K:{pair.KalshiTicker}_NO");
            return;
        }
        if (!_books.TryGetValue($"H:{pair.HardVenYesTokenId}",  out var pYes))
        {
            DebugLog.Discovery($"EvaluatePair {pair.Label}: missing book P:{pair.HardVenYesTokenId[..Math.Min(8, pair.HardVenYesTokenId.Length)]}...");
            return;
        }
        if (!_books.TryGetValue($"H:{pair.HardVenNoTokenId}",   out var pNo))
        {
            DebugLog.Discovery($"EvaluatePair {pair.Label}: missing book P:{pair.HardVenNoTokenId[..Math.Min(8, pair.HardVenNoTokenId.Length)]}...");
            return;
        }

        if (!kYes.HasReceivedDelta || !kNo.HasReceivedDelta || !pYes.HasReceivedDelta || !pNo.HasReceivedDelta)
        {
            DebugLog.Discovery($"EvaluatePair {pair.Label}: waiting for first delta — kYes={kYes.HasReceivedDelta} kNo={kNo.HasReceivedDelta} pYes={pYes.HasReceivedDelta} pNo={pNo.HasReceivedDelta}");
            return;
        }
        if (kYes.IsStale() || kNo.IsStale() || pYes.IsStale() || pNo.IsStale())
        {
            DebugLog.Discovery($"EvaluatePair {pair.Label}: stale book — kYes={kYes.IsStale()} kNo={kNo.IsStale()} pYes={pYes.IsStale()} pNo={pNo.IsStale()}");
            lock (_windowLock)
            {
                if (_activeWindows.TryGetValue(pair.PairId, out var sw) && sw != null)
                {
                    DebugLog.Discovery($"EvaluatePair {pair.Label}: closing open window due to stale book");
                    CloseWindow(pair.PairId, sw, DateTime.UtcNow, "STALE_BOOK");
                    _activeWindows[pair.PairId] = null;
                }
            }
            return;
        }

        decimal kYesAsk = kYes.GetBestAskPrice();
        decimal kNoAsk  = kNo.GetBestAskPrice();
        decimal pYesAsk = pYes.GetBestAskPrice();
        decimal pNoAsk  = pNo.GetBestAskPrice();

        if (kYesAsk < 0.05m || kNoAsk < 0.05m || pYesAsk < 0.05m || pNoAsk < 0.05m)
        {
            DebugLog.Discovery($"EvaluatePair {pair.Label}: price below min — kYes={kYesAsk:0.0000} kNo={kNoAsk:0.0000} pYes={pYesAsk:0.0000} pNo={pNoAsk:0.0000}");
            return;
        }

        decimal kYesBid = kYes.GetBestBidPrice();
        decimal kNoBid  = kNo.GetBestBidPrice();
        decimal pYesBid = pYes.GetBestBidPrice();
        decimal pNoBid  = pNo.GetBestBidPrice();

        decimal kYesMid = kYesBid > 0m ? (kYesAsk + kYesBid) / 2m : kYesAsk;
        decimal kNoMid  = kNoBid  > 0m ? (kNoAsk  + kNoBid)  / 2m : kNoAsk;
        decimal pYesMid = pYesBid > 0m ? (pYesAsk + pYesBid) / 2m : pYesAsk;
        decimal pNoMid  = pNoBid  > 0m ? (pNoAsk  + pNoBid)  / 2m : pNoAsk;
        decimal kMidSum = kYesMid + kNoMid;
        decimal pMidSum = pYesMid + pNoMid;

        if (kMidSum < 0.70m || kMidSum > 1.30m)
        {
            DebugLog.Discovery($"EvaluatePair {pair.Label}: Kalshi mid-sum sanity fail — kMidSum={kMidSum:0.0000}");
            return;
        }
        // 3-way pairs: the HardVen YES/NO tokens are two of three outcomes (not complements), so their
        // mid-sum doesn't approach 1 — skip this 2-way sanity check for them (kMidSum + depth/price still guard).
        if (!pair.ThreeWay && (pMidSum < 0.70m || pMidSum > 1.30m))
        {
            DebugLog.Discovery($"EvaluatePair {pair.Label}: HardVen mid-sum sanity fail — pMidSum={pMidSum:0.0000}");
            return;
        }

        // Type A: buy Kalshi YES + buy HardVen NO
        decimal kYesFee    = KalshiFee(kYesAsk);
        decimal pNoFee     = HardVenFee(pNoAsk, pair.HardVenNoTokenId);
        decimal typeAGross = kYesAsk + pNoAsk;
        decimal typeAFees  = kYesFee + pNoFee;
        decimal typeANet   = typeAGross + typeAFees;
        decimal typeAKDepth = kYes.GetTopAskVolume(3);
        decimal typeAPDepth = pNo.GetTopAskVolume(3);
        decimal typeADepth  = Math.Min(typeAKDepth, typeAPDepth);

        // Type B: buy Kalshi NO + buy HardVen YES
        decimal kNoFee     = KalshiFee(kNoAsk);
        decimal pYesFee    = HardVenFee(pYesAsk, pair.HardVenYesTokenId);
        decimal typeBGross = kNoAsk + pYesAsk;
        decimal typeBFees  = kNoFee + pYesFee;
        decimal typeBNet   = typeBGross + typeBFees;
        decimal typeBKDepth = kNo.GetTopAskVolume(3);
        decimal typeBPDepth = pYes.GetTopAskVolume(3);
        decimal typeBDepth  = Math.Min(typeBKDepth, typeBPDepth);

        decimal bestGross, bestNet, bestKFee, bestPFee, bestKDepth, bestPDepth;
        string  bestType;
        decimal kLegPrice, pLegPrice;

        // 3-way pairs: force Type B (K_NO_P_YES) — the Kalshi-NO direction is the only complete hedge;
        // Type A (Kalshi YES + book back-opponent) would lose on a draw, so never pick it.
        if (!pair.ThreeWay && typeANet <= typeBNet)
        {
            bestGross  = typeAGross;  bestNet    = typeANet;
            bestKFee   = kYesFee;    bestPFee   = pNoFee;
            bestKDepth = typeAKDepth; bestPDepth = typeAPDepth;
            bestType   = "K_YES_P_NO";
            kLegPrice  = kYesAsk;    pLegPrice  = pNoAsk;
        }
        else
        {
            bestGross  = typeBGross;  bestNet    = typeBNet;
            bestKFee   = kNoFee;     bestPFee   = pYesFee;
            bestKDepth = typeBKDepth; bestPDepth = typeBPDepth;
            bestType   = "K_NO_P_YES";
            kLegPrice  = kNoAsk;     pLegPrice  = pYesAsk;
        }

        decimal bestDepth    = Math.Min(bestKDepth, bestPDepth);
        string  legPricesNow = $"{kLegPrice:0.0000}|{pLegPrice:0.0000}";

        _nearMiss[pair.PairId] = (bestNet, bestType, bestDepth);

        bool isArb = bestNet < _arbThreshold && bestDepth >= _depthFloor;
        DebugLog.Discovery($"EvaluatePair {pair.Label}: {bestType} net={bestNet:0.0000} depth={bestDepth:0.0} isArb={isArb}");

        bool invokeOnArbOpened = false;
        DateTime? windowJustOpened = null;   // set to the new window's StartTime when an arb opens (hedge-monitor anchor)
        int currentKalshiDrops = Volatile.Read(ref _kalshiWsDrops);
        int currentHardVenDrops   = Volatile.Read(ref _hardvenWsDrops);

        int     daysToSettle   = -1;
        decimal aprHoldSettle  = -1m;
        if (pair.SettlementDate.HasValue)
        {
            var settleUtc = pair.SettlementDate.Value.ToDateTime(TimeOnly.MinValue, DateTimeKind.Utc);
            daysToSettle = Math.Max(0, (int)(settleUtc - DateTime.UtcNow).TotalDays);
            if (daysToSettle > 0 && bestNet > 0m)
            {
                decimal netEdge    = (1m - bestNet) * bestDepth;
                decimal capitalReq = bestNet * bestDepth;
                if (capitalReq > 0m)
                    aprHoldSettle = netEdge / capitalReq * (365m / daysToSettle);
            }
        }

        lock (_windowLock)
        {
            // ── leg-movement attribution: record which of the 4 asks changed since this pair's last eval,
            // and stamp the change time, so open/close can name the moving side and gauge book "hold". ──
            DateTime evalNow = DateTime.UtcNow;
            if (!_legMoves.TryGetValue(pair.PairId, out var lm)) { lm = new LegMoveState(); _legMoves[pair.PairId] = lm; }
            bool primed = lm.Primed;
            bool kYesMoved = primed && lm.KYes != kYesAsk;
            bool kNoMoved  = primed && lm.KNo  != kNoAsk;
            bool pYesMoved = primed && lm.PYes != pYesAsk;
            bool pNoMoved  = primed && lm.PNo  != pNoAsk;
            if (lm.KYes != kYesAsk) { lm.KYes = kYesAsk; lm.KYesAt = evalNow; }
            if (lm.KNo  != kNoAsk)  { lm.KNo  = kNoAsk;  lm.KNoAt  = evalNow; }
            if (lm.PYes != pYesAsk) { lm.PYes = pYesAsk; lm.PYesAt = evalNow; }
            if (lm.PNo  != pNoAsk)  { lm.PNo  = pNoAsk;  lm.PNoAt  = evalNow; }
            lm.Primed = true;

            var existing = _activeWindows[pair.PairId];

            if (isArb)
            {
                if (existing == null)
                {
                    long kAge = kYes.LastDeltaAt > DateTime.MinValue
                        ? (long)(DateTime.UtcNow - kYes.LastDeltaAt).TotalMilliseconds : -1;
                    long pAge = pYes.LastDeltaAt > DateTime.MinValue
                        ? (long)(DateTime.UtcNow - pYes.LastDeltaAt).TotalMilliseconds : -1;

                    bool kOpenMoved = bestType == "K_YES_P_NO" ? kYesMoved : kNoMoved;
                    bool pOpenMoved = bestType == "K_YES_P_NO" ? pNoMoved  : pYesMoved;
                    string openedBy = !primed ? "INITIAL"
                                    : (kOpenMoved && pOpenMoved) ? "BOTH"
                                    : kOpenMoved ? "KALSHI"
                                    : pOpenMoved ? "HARDVEN" : "OTHER";

                    // VERIFY-ON-DETECTION: the CHOSEN HardVen leg (K_NO_P_YES holds Pinnacle YES; K_YES_P_NO holds
                    // Pinnacle NO). If it's screening-only (no live WS tab), ask the sidecar to promote its league
                    // to a tab so this arb gets confirmed on real-time prices, and flag the window accordingly.
                    string chosenHvToken = bestType == "K_NO_P_YES" ? pair.HardVenYesTokenId : pair.HardVenNoTokenId;
                    bool hvVerified = IsHardVenVerified(chosenHvToken);
                    // ASK FOR A TAB ON EVERY ARB OPEN, not only unverified ones.
                    // Coverage for PRICES is not coverage for BETTING. A league fed by the board push needs no
                    // tab to quote prices, and the tab manager therefore never opens one ("leagues already fed
                    // → NOT gaps"). But a slip quote needs a PAGE showing that league, so the first thing that
                    // ever navigates there is the quote itself — from cold, on the execution path, with the
                    // clock running. Measured 2026-08-13: Vandecasteele/Shelbayh (net 0.9853, depth 2838,
                    // pre-live, board-fed and therefore "verified") timed out after 20s in SLIP_QUOTE_FAILED
                    // and took its sibling down with it via LEG_IN_FLIGHT — while a tabbed league quoted in
                    // 562-1046ms. Perversely, a WORSE-covered league traded faster, because being unverified
                    // is what used to trigger this call.
                    // Cheap and self-limiting: request_verify returns 'already-open' when tabbed and 'at-cap'
                    // when the pool is full, so this can neither thrash nor exceed HARDVEN_TAB_MAX.
                    RequestVerify(chosenHvToken);

                    DateTime openTime = DateTime.UtcNow;
                    var w = new ActiveWindow(
                        PairId:            pair.PairId,
                        ArbType:           bestType,
                        StartTime:         openTime,
                        EntryGrossCost:    bestGross,
                        EntryNetCost:      bestNet,
                        EntryLegPrices:    legPricesNow,
                        BestGrossCost:     bestGross,
                        BestNetCost:       bestNet,
                        BestLegPrices:     legPricesNow,
                        KalshiDepth:       bestKDepth,
                        HardVenDepth:         bestPDepth,
                        KalshiFees:        bestKFee,
                        HardVenFees:          bestPFee,
                        KalshiBookAgeMs:   kAge,
                        HardVenBookAgeMs:     pAge,
                        KalshiMidSum:      kMidSum,
                        HardVenMidSum:        pMidSum,
                        KalshiDropsAtOpen: currentKalshiDrops,
                        HardVenDropsAtOpen:   currentHardVenDrops,
                        DaysToSettlement:  daysToSettle,
                        AprHoldToSettle:   aprHoldSettle,
                        UpdateCount:       1,
                        OpenedBy:          openedBy,
                        OpenKLeg:          kLegPrice,
                        OpenPLeg:          pLegPrice,
                        HardVenWsVerified: hvVerified,
                        OpenedInPlay:      _books.TryGetValue($"H:{chosenHvToken}", out var openBook) && openBook.IsLive
                    );
                    _activeWindows[pair.PairId] = w;
                    DebugLog.Discovery($"EvaluatePair {pair.Label}: ARB OPEN {bestType} net={bestNet:0.0000} depth={bestDepth:0.0} kAge={kAge}ms pAge={pAge}ms");
                    invokeOnArbOpened = true;
                    windowJustOpened   = openTime;
                    // Sampled betslip check (pre-live only, rate-limited). Returns instantly; the quote runs off-thread.
                    MaybeSlipVerify(pair, w, chosenHvToken, kLegPrice, pLegPrice);
                }
                else
                {
                    // KICKOFF SPLIT. A window must describe ONE regime. The in-play tag used to be sampled once,
                    // at close, and stamped over the whole window — so a window straddling kickoff was filed
                    // entirely as whichever regime happened to be current when it ended (pre-live if the live
                    // topic hadn't pushed yet, in-play if it had). Both are wrong, and they corrupt the analyzer,
                    // which applies placement time PER ROW by this tag (~1s pre-live vs ~8s in-play) and reads
                    // pre-live DURATION as the headline durability stat. So: close the window at the transition
                    // and let the next eval reopen a fresh one, which is then honestly tagged in-play.
                    // Telemetry-only — execution reads book.IsLive live, never this tag.
                    string liveTok = existing.ArbType == "K_YES_P_NO" ? pair.HardVenNoTokenId : pair.HardVenYesTokenId;
                    bool inPlayNow = _books.TryGetValue($"H:{liveTok}", out var liveBook) && liveBook.IsLive;
                    if (inPlayNow != existing.OpenedInPlay)
                    {
                        DebugLog.Discovery($"EvaluatePair {pair.Label}: REGIME CHANGE " +
                            $"{(existing.OpenedInPlay ? "in-play→pre" : "pre→in-play")} — splitting the window");
                        CloseWindow(pair.PairId, existing, evalNow, "WENT_LIVE");
                        _activeWindows[pair.PairId] = null;   // next eval reopens, tagged with the NEW regime
                    }
                    else
                    {
                    bool betterCost  = bestNet   < existing.BestNetCost;
                    bool betterDepth = bestDepth > Math.Min(existing.KalshiDepth, existing.HardVenDepth);
                    // each leg of THIS window's fixed ArbType, at the current asks — to detect a move against you
                    decimal kLegNow = existing.ArbType == "K_YES_P_NO" ? kYesAsk : kNoAsk;
                    decimal pLegNow = existing.ArbType == "K_YES_P_NO" ? pNoAsk  : pYesAsk;
                    _activeWindows[pair.PairId] = existing with
                    {
                        BestGrossCost = betterCost  ? bestGross    : existing.BestGrossCost,
                        BestNetCost   = betterCost  ? bestNet      : existing.BestNetCost,
                        BestLegPrices = betterCost  ? legPricesNow : existing.BestLegPrices,
                        KalshiFees    = betterCost  ? bestKFee     : existing.KalshiFees,
                        HardVenFees      = betterCost  ? bestPFee     : existing.HardVenFees,
                        KalshiDepth   = betterDepth ? bestKDepth   : existing.KalshiDepth,
                        HardVenDepth     = betterDepth ? bestPDepth   : existing.HardVenDepth,
                        UpdateCount   = existing.UpdateCount + 1,
                        // verify-on-detection: latch true once the leg's league gains live WS coverage (the tab
                        // we requested arrived) — so a window that opened screening-only but got WS-confirmed reads
                        // as verified.
                        HardVenWsVerified = existing.HardVenWsVerified ||
                            IsHardVenVerified(existing.ArbType == "K_NO_P_YES" ? pair.HardVenYesTokenId : pair.HardVenNoTokenId),
                        // FIRST eval each leg moves above its open price = when it left "within the arb" (latch once)
                        KLeftWithinAt = (existing.KLeftWithinAt == DateTime.MaxValue && existing.OpenKLeg >= 0m && kLegNow > existing.OpenKLeg) ? evalNow : existing.KLeftWithinAt,
                        PLeftWithinAt = (existing.PLeftWithinAt == DateTime.MaxValue && existing.OpenPLeg >= 0m && pLegNow > existing.OpenPLeg) ? evalNow : existing.PLeftWithinAt
                    };
                    if (betterCost)
                        DebugLog.Discovery($"EvaluatePair {pair.Label}: ARB UPDATE better net={bestNet:0.0000} (was {existing.BestNetCost:0.0000})");
                    }
                }
            }
            else if (existing != null)
            {
                bool kWinMoved = existing.ArbType == "K_YES_P_NO" ? kYesMoved : kNoMoved;
                bool pWinMoved = existing.ArbType == "K_YES_P_NO" ? pNoMoved  : pYesMoved;
                string closedSide = (kWinMoved && pWinMoved) ? "BOTH" : kWinMoved ? "KALSHI"
                                  : pWinMoved ? "HARDVEN" : "NEITHER";
                DateTime kAt = existing.ArbType == "K_YES_P_NO" ? lm.KYesAt : lm.KNoAt;
                DateTime pAt = existing.ArbType == "K_YES_P_NO" ? lm.PNoAt  : lm.PYesAt;
                long kLegAgeMs = (long)(evalNow - kAt).TotalMilliseconds;
                long pLegAgeMs = (long)(evalNow - pAt).TotalMilliseconds;
                decimal closePLeg = existing.ArbType == "K_YES_P_NO" ? pNoAsk : pYesAsk;
                bool pHeld = existing.OpenPLeg >= 0m && closePLeg == existing.OpenPLeg;   // book never moved
                DebugLog.Discovery($"EvaluatePair {pair.Label}: ARB CLOSE — net={bestNet:0.0000} above threshold, closedBySide={closedSide} bookHeld={pHeld}, was open {(DateTime.UtcNow - existing.StartTime).TotalMilliseconds:0}ms");
                CloseWindow(pair.PairId, existing, DateTime.UtcNow, "PRICE", closedSide, kLegAgeMs, pLegAgeMs, pHeld);
                _activeWindows[pair.PairId] = null;
            }
        }

        if (invokeOnArbOpened)
            OnArbOpened?.Invoke(pair.PairId, bestNet, bestType, bestDepth, kLegPrice, pLegPrice);

        if (_debugPrices && invokeOnArbOpened)
            DumpPrices(pair, bestType, bestNet, bestGross, bestKFee, bestPFee,
                       kYes, kNo, pYes, pNo, kMidSum, pMidSum);

        // ── Hedge-monitor sampling ──────────────────────────────────────────────
        // On a fresh arb open, (re)arm a monitor anchored to this window's StartTime. Then — independent of
        // whether the window is still open (it usually closes in <1s) — sample the Kalshi unwind price for
        // HedgeHorizonMs so the analyzer can price the worst-case hedge if the HardVen leg failed to fill.
        if (HedgeMonitorEnabled && windowJustOpened is { } openedAt)
        {
            _hedgeMonitors[pair.PairId] = new HedgeMonitor
            {
                PairId         = pair.PairId,
                Label          = pair.Label,
                ArbType        = bestType,
                OpenTime       = openedAt,
                EntryKalshiAsk = kLegPrice,   // the committed Kalshi leg's entry ask
                EntryHardVenAsk   = pLegPrice,   // the HardVen leg that may fail
                EntryNetCost   = bestNet,
                EntryDepth     = bestDepth
            };
        }

        if (_hedgeMonitors.TryGetValue(pair.PairId, out var hm))
        {
            DateTime hnow   = DateTime.UtcNow;
            long offsetMs   = (long)(hnow - hm.OpenTime).TotalMilliseconds;
            if (offsetMs > HedgeHorizonMs)
            {
                _hedgeMonitors.TryRemove(pair.PairId, out _);
            }
            else if ((hnow - hm.LastSampleAt).TotalMilliseconds >= HedgeSampleIntervalMs)
            {
                hm.LastSampleAt = hnow;
                bool holdYes        = hm.ArbType == "K_YES_P_NO";
                decimal unwindBid   = holdYes ? kYesBid : kNoBid;     // sell the held Kalshi leg back (Kalshi-first)
                decimal oppositeAsk = holdYes ? kNoAsk  : kYesAsk;    // or buy the opposite leg to lock
                decimal entryAskNow = holdYes ? kYesAsk : kNoAsk;     // buy the ENTRY leg now (HardVen-first late-complete)
                decimal hardvenNow  = holdYes ? pNoAsk  : pYesAsk;    // the HardVen leg now (did it return?)
                decimal unwindDepth = holdYes ? kYes.GetTopBidVolume(3) : kNo.GetTopBidVolume(3);
                EnqueueHedgeSample(hm, offsetMs, unwindBid, oppositeAsk, entryAskNow, hardvenNow, unwindDepth);
            }
        }
    }

    // NOTE: must be called while holding _windowLock
    private void CloseWindow(string pairId, ActiveWindow w, DateTime endTime, string closedBy,
        string closedSide = "", long kLegAgeMs = -1, long pLegAgeMs = -1, bool pHeld = false)
    {
        long durationMs = (long)(endTime - w.StartTime).TotalMilliseconds;
        if (durationMs < 5) return;

        // per-leg "time HELD WITHIN the arb" = how long after open each side stayed at-or-better than its open
        // price before first moving against you (MaxValue latch = never left → whole window). The OPTIMISTIC
        // capturability signal: unlike HardVenLegAgeMsAtClose (frozen-price age, resets on ANY move incl. an
        // improving one), a move to a BETTER price keeps the leg "within" — this is the leg's capturable window.
        long kWithinMs = (long)(((w.KLeftWithinAt == DateTime.MaxValue ? endTime : w.KLeftWithinAt) - w.StartTime).TotalMilliseconds);
        long pWithinMs = (long)(((w.PLeftWithinAt == DateTime.MaxValue ? endTime : w.PLeftWithinAt) - w.StartTime).TotalMilliseconds);

        var pair = _pairs.FirstOrDefault(p => p.PairId == pairId);
        if (pair == null)
        {
            DebugLog.Discovery($"CloseWindow: pair not found for pairId={pairId}, skipping CSV row");
            return;
        }

        // the bookmaker selection_id this arb's book leg actually used — the exact join key for the audit
        // tape (verify_arbs.py): K_NO_P_YES backs HardVen YES, K_YES_P_NO backs HardVen NO.
        string hardvenLegId = w.ArbType == "K_YES_P_NO" ? pair.HardVenNoTokenId : pair.HardVenYesTokenId;
        // IN-PLAY tag: the regime this window ACTUALLY LIVED IN. Taken from the window's own OpenedInPlay, not
        // sampled here at close: a close-time sample describes one instant and was being stamped over a window
        // that may have straddled kickoff. Windows are now SPLIT at the transition (closedBy=WENT_LIVE), so
        // open-regime == close-regime and this is exact. Pre-match legs are stable (near-instant capture); live
        // legs are volatile (~8s placement) — the analyzer picks its per-row timing model off this.
        bool hvInPlay = w.OpenedInPlay;

        decimal fees     = w.KalshiFees + w.HardVenFees;
        decimal profit   = 1m - w.BestNetCost;
        decimal maxDepth = Math.Min(w.KalshiDepth, w.HardVenDepth);
        bool dropDuring  = (Volatile.Read(ref _kalshiWsDrops) > w.KalshiDropsAtOpen)
                        || (Volatile.Read(ref _hardvenWsDrops)   > w.HardVenDropsAtOpen);

        string aprStr = w.AprHoldToSettle >= 0m ? $" APR={w.AprHoldToSettle:P0}" : "";
        string moveStr = "";
        if (closedBy == "PRICE" && closedSide.Length > 0)
            moveStr = closedSide == "KALSHI" && pHeld
                ? $" by KALSHI (book HELD {pLegAgeMs}ms -> CAPTURABLE)"
                : $" by {closedSide}" + (pHeld ? " (book held)" : "");
        Console.WriteLine($"[CROSS ARB CLOSE] {pair.Label} | {w.ArbType} | {durationMs}ms | " +
                          $"gross=${w.BestGrossCost:0.0000} fees=${fees:0.0000} profit/share=${profit:0.0000} | " +
                          $"opened={w.OpenedBy} closedBy={closedBy}{moveStr} | updates={w.UpdateCount}{aprStr}");

        string restKalshi = w.RestKalshiAsk >= 0 ? w.RestKalshiAsk.ToString("0.0000") : "";
        string restHardVen   = w.RestHardVenAsk   >= 0 ? w.RestHardVenAsk.ToString("0.0000")   : "";
        string restDelay  = w.RestDelayMs   >= 0 ? w.RestDelayMs.ToString()            : "";
        string dts        = w.DaysToSettlement >= 0 ? w.DaysToSettlement.ToString()       : "";
        string apr        = w.AprHoldToSettle  >= 0 ? w.AprHoldToSettle.ToString("0.0000") : "";

        string row = string.Join(",",
            w.StartTime.ToString("yyyy-MM-dd HH:mm:ss.fff"),
            endTime.ToString("yyyy-MM-dd HH:mm:ss.fff"),
            durationMs,
            Quote(pairId),
            Quote(pair.Label),
            w.ArbType,
            w.EntryGrossCost.ToString("0.0000"),
            w.EntryNetCost.ToString("0.0000"),
            Quote(w.EntryLegPrices),
            w.BestGrossCost.ToString("0.0000"),
            w.BestNetCost.ToString("0.0000"),
            Quote(w.BestLegPrices),
            fees.ToString("0.0000"),
            w.KalshiFees.ToString("0.0000"),
            w.HardVenFees.ToString("0.0000"),
            profit.ToString("0.0000"),
            w.KalshiDepth.ToString("0.00"),
            w.HardVenDepth.ToString("0.00"),
            maxDepth.ToString("0.00"),
            (maxDepth * w.BestNetCost).ToString("0.00"),
            (profit * maxDepth).ToString("0.0000"),
            w.KalshiBookAgeMs,
            w.HardVenBookAgeMs,
            w.KalshiMidSum.ToString("0.0000"),
            w.HardVenMidSum.ToString("0.0000"),
            w.KalshiDropsAtOpen,
            w.HardVenDropsAtOpen,
            dropDuring ? "1" : "0",
            w.UpdateCount,
            closedBy,
            dts,
            apr,
            w.RestChecked   ? "1" : "0",
            w.RestConfirmed ? "1" : "0",
            restKalshi,
            restHardVen,
            restDelay,
            w.OpenedBy,
            closedBy == "PRICE" ? closedSide : "",
            kLegAgeMs >= 0 ? kLegAgeMs.ToString() : "",
            pLegAgeMs >= 0 ? pLegAgeMs.ToString() : "",
            (closedBy == "PRICE" && pHeld) ? "1" : "0",
            Quote(hardvenLegId),
            kWithinMs,
            pWithinMs,
            hvInPlay ? "1" : "0",
            w.HardVenWsVerified ? "1" : "0"
        );

        EnqueueCsvRow(row);
    }

    // ── Deep price debug (HARDVEN_DEBUG_PRICES=1) ─────────────────────────────
    // Dumps all four legs the instant a window opens, so a too-good gap can be read off directly:
    // is the Kalshi side really cheap (and is its depth one fat level or spread up the ladder), and does
    // the Pinnacle decimal odds match what the venue shows? Pinn YES = the leg paired to the Kalshi-YES
    // outcome; Pinn NO = its opposite. The arb only uses one side of each book (the "chosen" line).
    private void DumpPrices(CrossPair pair, string bestType, decimal net, decimal gross,
        decimal kFee, decimal pFee, LocalOrderBook kYes, LocalOrderBook kNo,
        LocalOrderBook pYes, LocalOrderBook pNo, decimal kMidSum, decimal pMidSum)
    {
        string chosen = bestType == "K_NO_P_YES" ? "Kalshi NO + Pinn YES" : "Kalshi YES + Pinn NO";
        var sb = new StringBuilder();
        sb.AppendLine($"[PRICES] {pair.Label} | ARB {bestType} net={net:0.0000} (gross={gross:0.0000} fees K={kFee:0.0000} H={pFee:0.0000})");
        sb.AppendLine($"  Kalshi YES  K:{pair.KalshiTicker}     {FmtKalshi(kYes)}");
        sb.AppendLine($"  Kalshi NO   K:{pair.KalshiTicker}_NO  {FmtKalshi(kNo)}");
        sb.AppendLine($"  Pinn   YES  H:{pair.HardVenYesTokenId}  {FmtHardVen(pYes)}");
        sb.AppendLine($"  Pinn   NO   H:{pair.HardVenNoTokenId}   {FmtHardVen(pNo)}");
        sb.Append($"  kMidSum={kMidSum:0.0000}  pMidSum={pMidSum:0.0000}  | chosen: {chosen}");
        Console.WriteLine(sb.ToString());
    }

    private static long BookAgeMs(LocalOrderBook b) =>
        b.LastDeltaAt > DateTime.MinValue ? (long)(DateTime.UtcNow - b.LastDeltaAt).TotalMilliseconds : -1;

    // Kalshi (native binary, fast WS): show ask/bid + cumulative top-3 + the actual top-5 ask ladder so we
    // can see whether the headline depth sits at the best price or is spread across worse levels.
    private static string FmtKalshi(LocalOrderBook b)
    {
        decimal ask = b.GetBestAskPrice(), bid = b.GetBestBidPrice();
        string ladder = string.Join(" | ", b.GetTopAskLevels(5).Select(l => $"{l.Price:0.0000}×{l.Size:0.#}"));
        return $"ask={ask:0.0000} bid={bid:0.0000} top3={b.GetTopAskVolume(3):0.#} age={BookAgeMs(b)}ms  asks[{ladder}]";
    }

    // Pinnacle (single ask level = the moneyline; vig in the price): show implied ask, the decimal odds it
    // came from (1/ask, comparable to the site), and the max-risk-derived size.
    private static string FmtHardVen(LocalOrderBook b)
    {
        decimal ask = b.GetBestAskPrice();
        decimal dec = ask > 0m ? Math.Round(1m / ask, 4) : 0m;
        return $"ask={ask:0.0000} (dec {dec:0.0000}) maxc={b.GetBestAskSize():0.#} age={BookAgeMs(b)}ms";
    }

    // ── CSV infrastructure ────────────────────────────────────────────────────

    private static string Quote(string s) => $"\"{s.Replace("\"", "\"\"")}\"";

    private void EnqueueCsvRow(string row)
    {
        // Header is emitted by the writer task per dated file (see DrainWithDailyRotationAsync) — just queue the row.
        _csvChannel.Writer.TryWrite(row);
    }

    // ── Sampled slip verify ───────────────────────────────────────────────────
    /// <summary>Gate one arb open into a betslip sample: pre-live, rate-limited, one at a time. Returns
    /// immediately — the quote itself takes seconds and must never sit on the book-update path.</summary>
    private void MaybeSlipVerify(CrossPair pair, ActiveWindow w, string hvToken,
                                 decimal kLegAtOpen, decimal pLegAtOpen)
    {
        // OFF BY DEFAULT, and deliberately so. Skipping here assumes `available_for_accas: false` really
        // does make a betslip unreadable — and that has never been measured, only asserted. Acting on it
        // would silently exclude cricket, esports and boxing (154 of 301 pairs) from every check the bot
        // makes, and would do it invisibly, which is exactly how an untested assumption becomes permanent.
        // The sidecar now quotes those events anyway and reports the flag alongside the result; turn this
        // on once that data shows the flag is worth obeying.
        if (SkipNonAccaSamples && !IsHardVenAccaOk(hvToken))
        {
            Interlocked.Increment(ref SlipVerifySkippedNotQuotable);
            return;
        }
        if (!TrySlipVerifySlot(w.OpenedInPlay)) return;
        _ = Task.Run(() => RunSlipVerifyAsync(pair, w, hvToken, kLegAtOpen, pLegAtOpen));
    }

    /// <summary>The gate itself, separated so it can be exercised directly (`--slip-verify-check`): this
    /// decides how often the bot touches the venue, and getting it wrong is an anti-detection problem
    /// rather than a wrong number in a file. True = caller owns the slot and MUST release it.</summary>
    internal bool TrySlipVerifySlot(bool openedInPlay)
    {
        if (!SlipVerifyEnabled || _slipQuote is null) return false;
        // CLAIM THE SLOT BEFORE CHECKING THE INTERVAL. Two pairs can open an arb in the same millisecond
        // on different threads; checking the clock first would let both pass it and both click.
        if (Interlocked.CompareExchange(ref _slipVerifyInFlight, 1, 0) != 0) return false;
        long now     = Environment.TickCount64;
        long budget  = openedInPlay ? SlipVerifyInPlayMs : SlipVerifyPreLiveMs;
        ref long last = ref (openedInPlay ? ref _lastInPlayVerifyTicks : ref _lastSlipVerifyTicks);
        bool ok = now - Interlocked.Read(ref last) >= budget
               // ROVER COOLDOWN, on top. We cannot know in advance whether this quote will need the
               // roving tab — but we know whether the LAST one did, and a run that keeps falling back to
               // navigation should slow down as a whole rather than keep paying seconds per sample.
               && now - Interlocked.Read(ref _lastRoverTicks) >= 0;
        if (!ok)
        {
            Volatile.Write(ref _slipVerifyInFlight, 0);
            return false;
        }
        // Stamped at START, not completion: "at most every N" is measured between attempts, so a slow
        // quote does not push the next one further out.
        Interlocked.Exchange(ref last, now);
        return true;
    }

    internal void ReleaseSlipVerifySlot() => Volatile.Write(ref _slipVerifyInFlight, 0);
    internal static int SlipVerifyIntervalMsForTest => SlipVerifyPreLiveMs;

    /// <summary>Hand the regime's budget back after a refusal that COST THE VENUE NOTHING, leaving only a
    /// short floor before the next attempt.
    ///
    /// The budgets exist to ration UI ACTIONS, and most refusals are not one: the sidecar decides "the
    /// venue will not put this event on a betslip", "this event is already subscribed" and "the row
    /// renders no odds" from data it already holds, without touching the page. Charging those a full
    /// interval means a burst of unquotable arbs blinds the sampler for minutes — 2 of 12 samples on
    /// 2026-08-13 were spent on `available_for_accas: false` events, each answered in under 10ms and each
    /// costing a whole minute of pre-live coverage.
    ///
    /// The floor is what stops this becoming a retry loop: a structurally unquotable pair re-refuses in
    /// milliseconds, so without it the next arb open would always find the slot free.</summary>
    internal void RefundSlipVerifyBudget(bool openedInPlay)
    {
        long budget = openedInPlay ? SlipVerifyInPlayMs : SlipVerifyPreLiveMs;
        long target = Environment.TickCount64 - Math.Max(0, budget - SlipVerifyRefundFloorMs);
        ref long last = ref (openedInPlay ? ref _lastInPlayVerifyTicks : ref _lastSlipVerifyTicks);
        // Only ever move the clock EARLIER. A concurrent real attempt may have stamped it since, and
        // pushing that forward would hand out a second slot on top of the one it already owns.
        long cur = Interlocked.Read(ref last);
        if (target < cur) Interlocked.Exchange(ref last, target);
    }

    /// <summary>Quote the betslip, then re-test the arb on the prices the venue would actually honour.
    ///
    /// The Kalshi leg is RE-READ after the quote returns, not reused from open: the slip takes seconds and
    /// Kalshi moves in them, so pairing a fresh HardVen price with a stale Kalshi one would report an arb
    /// that no longer exists on either side. NetAtSlip is therefore "both legs, as of now".</summary>
    private async Task RunSlipVerifyAsync(CrossPair pair, ActiveWindow w, string hvToken,
                                          decimal kLegAtOpen, decimal pLegAtOpen)
    {
        var sw = System.Diagnostics.Stopwatch.StartNew();
        decimal slip = -1m; string err = "";
        try
        {
            (slip, err) = await _slipQuote!(hvToken, SlipVerifyTimeoutSec);
        }
        catch (Exception ex) { err = $"{ex.GetType().Name}: {ex.Message}"; }
        finally
        {
            // If that quote had to NAVIGATE, start the rover cooldown now. Stamped on completion (not on
            // start like the regime budgets) because the cost being throttled is the navigation itself,
            // and we only learn it happened once the answer comes back.
            string via = _slipVia?.Invoke() ?? "";
            if (via == "rover")
            {
                Interlocked.Exchange(ref _lastRoverTicks, Environment.TickCount64 + SlipVerifyRoverMs);
                Console.WriteLine($"[SLIP VERIFY] that quote used the roving tab — pausing sampling for "
                                + $"{SlipVerifyRoverMs / 1000}s");
            }
            // A refusal the venue never saw does not spend the interval — give it back (minus a floor).
            else if (slip <= 0m && !(_slipClicked?.Invoke() ?? true))
                RefundSlipVerifyBudget(w.OpenedInPlay);
            ReleaseSlipVerifySlot();
        }
        sw.Stop();

        // Live re-read of BOTH legs' books for the depth columns and the honest re-test.
        string kKey = w.ArbType == "K_YES_P_NO" ? $"K:{pair.KalshiTicker}" : $"K:{pair.KalshiTicker}_NO";
        _books.TryGetValue(kKey, out var kBook);
        _books.TryGetValue($"H:{hvToken}", out var pBook);
        decimal kNow      = kBook?.GetBestAskPrice() ?? -1m;
        decimal kBestSize = kBook?.GetBestAskSize()  ?? 0m;
        decimal pBestSize = pBook?.GetBestAskSize()  ?? 0m;
        decimal kTop3     = kBook?.GetTopAskVolume(3) ?? 0m;
        decimal pTop3     = pBook?.GetTopAskVolume(3) ?? 0m;

        decimal netBoard = kLegAtOpen + pLegAtOpen + KalshiFee(kLegAtOpen) + HardVenFee(pLegAtOpen, hvToken);
        decimal slipPct  = pLegAtOpen > 0m && slip > 0m ? (slip - pLegAtOpen) / pLegAtOpen * 100m : 0m;
        decimal kUse     = kNow > 0m ? kNow : kLegAtOpen;
        decimal netSlip  = slip > 0m ? kUse + slip + KalshiFee(kUse) + HardVenFee(slip, hvToken) : -1m;
        bool    survived = slip > 0m && netSlip < _arbThreshold;

        // Mark quotes on events the venue flagged as non-accumulator. A SUCCESS on one of these is the
        // finding: it means the flag never justified refusing, and 154 of 301 pairs go back on the menu.
        string accaNote = (_slipAccaFlagged?.Invoke() ?? false) ? "  [NON-ACCA EVENT]" : "";
        if (slip > 0m)
        {
            Interlocked.Increment(ref SlipVerifyCount);
            Console.WriteLine($"[SLIP VERIFY] {pair.Label}: board {pLegAtOpen:0.0000} -> slip {slip:0.0000} " +
                              $"({slipPct:+0.00;-0.00}%)  net ${netBoard:0.0000} -> ${netSlip:0.0000}  " +
                              $"{(survived ? "STILL AN ARB" : "GONE")}  depth K={kBestSize:0.#}/P={pBestSize:0.#}  " +
                              $"{sw.ElapsedMilliseconds}ms{accaNote}");
        }
        else
        {
            Console.WriteLine($"[SLIP VERIFY] {pair.Label}: no quote ({err}) after {sw.ElapsedMilliseconds}ms{accaNote}");
        }

        // ── HOLD: watch the arb on SLIP prices until it stops breaking even ──────────────────────────
        // The window durations in the main telemetry are measured on BOARD prices, which we know overstate
        // an arb (84% agree, the rest worse at the slip, never better). This measures the same window's
        // CAPTURABLE life instead. It needs no further UI action — the event is subscribed on the acca
        // channel, so the venue keeps pushing and each re-quote is served from the sidecar's cache.
        long heldMs = 0; int holdSamples = 0; decimal bestNetHeld = netSlip; string diedBy = "";
        if (slip > 0m && survived && SlipHoldMs > 0)
        {
            var hold = System.Diagnostics.Stopwatch.StartNew();
            while (hold.ElapsedMilliseconds < SlipHoldMs)
            {
                await Task.Delay(SlipHoldPollMs);
                decimal p2; string e2;
                try { (p2, e2) = await _slipQuote!(hvToken, SlipVerifyTimeoutSec); }
                catch (Exception ex) { p2 = -1m; e2 = ex.GetType().Name; }
                if (p2 <= 0m) { diedBy = $"QUOTE_LOST({e2})"; break; }
                holdSamples++;
                // Re-read Kalshi too: either leg moving can end the arb, and attributing it matters.
                decimal kLive = kBook?.GetBestAskPrice() ?? kUse;
                if (kLive <= 0m) kLive = kUse;
                decimal net2 = kLive + p2 + KalshiFee(kLive) + HardVenFee(p2, hvToken);
                if (net2 < bestNetHeld) bestNetHeld = net2;
                if (net2 >= _slipAcceptNetForHold)
                {
                    diedBy = Math.Abs(p2 - slip) > Math.Abs(kLive - kUse) ? "HARDVEN" : "KALSHI";
                    break;
                }
            }
            hold.Stop();
            heldMs = hold.ElapsedMilliseconds;
            if (diedBy.Length == 0) diedBy = "STILL_ALIVE_AT_LIMIT";
            Console.WriteLine($"[SLIP HOLD] {pair.Label}: stayed break-even for {heldMs / 1000.0:0.0}s "
                            + $"over {holdSamples} slip sample(s), best net ${bestNetHeld:0.0000}, ended by {diedBy}");
        }

        _slipCsvChannel.Writer.TryWrite(string.Join(",",
            DateTime.UtcNow.ToString("yyyy-MM-dd HH:mm:ss.fff"),
            // REGIME, not a constant. This column was the literal "pre-live" — a leftover from when the
            // sampler only ever fired pre-match. Once in-play sampling was added it became an outright lie:
            // on 2026-08-14 all 24 samples were filed pre-live while the main telemetry, reading the same
            // w.OpenedInPlay, had 62 of 67 windows in-play. Anything reading this file would have concluded
            // the board is honest pre-match on evidence that was almost entirely in-play.
            Quote(pair.PairId), Quote(pair.Label), w.ArbType, w.OpenedInPlay ? "in-play" : "pre-live",
            pLegAtOpen.ToString("0.0000"),
            slip > 0m ? slip.ToString("0.0000") : "",
            slip > 0m ? (slip - pLegAtOpen).ToString("0.0000") : "",
            slip > 0m ? slipPct.ToString("0.0000") : "",
            kLegAtOpen.ToString("0.0000"),
            kNow > 0m ? kNow.ToString("0.0000") : "",
            netBoard.ToString("0.0000"),
            netSlip > 0m ? netSlip.ToString("0.0000") : "",
            slip > 0m ? (survived ? "1" : "0") : "",
            netSlip > 0m ? (_arbThreshold - netSlip).ToString("0.0000") : "",
            kBestSize.ToString("0.##"), pBestSize.ToString("0.##"),
            kTop3.ToString("0.##"),     pTop3.ToString("0.##"),
            sw.ElapsedMilliseconds.ToString(),
            Quote(hvToken), Quote(err),
            heldMs.ToString(), holdSamples.ToString(),
            bestNetHeld > 0m ? bestNetHeld.ToString("0.0000") : "",
            Quote(diedBy)));
    }

    // One post-open sample of the Kalshi unwind trajectory. Columns the analyzer joins on (PairId, OpenTime)
    // and reads at OffsetMs ≈ --hedge-secs to price the worst-case hedge of a failed HardVen leg.
    //   KalshiUnwindBid   = bid of the held Kalshi leg now → sell-back price (flatten); per-share unwind P/L
    //                       = KalshiUnwindBid − EntryKalshiAsk − Kalshi entry+exit fees (can be +ve on revert)
    //   KalshiOppositeAsk = ask of the opposite Kalshi leg → buy-to-lock alternative (holds to settlement)
    //   HardVenLegNow     = the HardVen leg's price now → if it returned within the arb, you'd COMPLETE not hedge
    private void EnqueueHedgeSample(HedgeMonitor hm, long offsetMs, decimal unwindBid,
        decimal oppositeAsk, decimal entryAskNow, decimal hardvenNow, decimal unwindDepth)
    {
        // Header emitted by the writer task per dated file (DrainWithDailyRotationAsync) — just queue the row.
        string row = string.Join(",",
            hm.OpenTime.ToString("yyyy-MM-dd HH:mm:ss.fff"),
            Quote(hm.PairId),
            Quote(hm.Label),
            hm.ArbType,
            offsetMs,
            hm.EntryKalshiAsk.ToString("0.0000"),
            unwindBid.ToString("0.0000"),
            oppositeAsk.ToString("0.0000"),
            entryAskNow.ToString("0.0000"),      // current ask of the ENTRY leg — HardVen-first late-completion cost
            hm.EntryHardVenAsk.ToString("0.0000"),
            hardvenNow.ToString("0.0000"),
            unwindDepth.ToString("0.00"),
            hm.EntryNetCost.ToString("0.0000")
        );
        _hedgeCsvChannel.Writer.TryWrite(row);
    }

    public async Task ShutdownAsync()
    {
        lock (_windowLock)
        {
            var now = DateTime.UtcNow;
            foreach (var pairId in _activeWindows.Keys.ToList())
            {
                if (_activeWindows[pairId] is { } w)
                {
                    DebugLog.Discovery($"ShutdownAsync: flushing open window for {pairId}");
                    CloseWindow(pairId, w, now, "SHUTDOWN");
                    _activeWindows[pairId] = null;
                }
            }
        }

        // Hedge samples are streamed live (no per-position summary to flush), so just close the channels.
        _csvChannel.Writer.TryComplete();
        _hedgeCsvChannel.Writer.TryComplete();
        _slipCsvChannel.Writer.TryComplete();
        try { await Task.WhenAll(_csvWriterTask, _hedgeCsvWriterTask, _slipCsvWriterTask); }
        catch (Exception ex) { DebugLog.Discovery($"ShutdownAsync: CSV writer task threw — {ex.Message}"); }
    }

    private readonly Task _csvWriterTask;
    private readonly Task _hedgeCsvWriterTask;
    private readonly Task _slipCsvWriterTask;

    // File boundary = LOCAL calendar day (matches how the operator reads "per day" and the day-bounded schedule).
    private static string CsvDate() => DateTime.Now.ToString("yyyyMMdd");

    // Open the dated file APPEND (restart-safe: a same-day restart keeps the day's rows), writing the header
    // only when the file is new/empty. Returns the writer.
    private static async Task<StreamWriter> OpenDatedCsvAsync(string baseName, string date, string header)
    {
        string path  = $"{baseName}_{date}.csv";
        bool   isNew = !File.Exists(path) || new FileInfo(path).Length == 0;
        var    sw    = new StreamWriter(path, append: true, Encoding.UTF8) { AutoFlush = false };
        if (isNew) { await sw.WriteLineAsync(header); await sw.FlushAsync(); }
        return sw;
    }

    // Shared drain loop with DAILY rotation: when the local day rolls over, close the current file and open the
    // next day's — so an unattended multi-day run produces one CSV per calendar day.
    private static async Task DrainWithDailyRotationAsync(
        System.Threading.Channels.ChannelReader<string> reader, string baseName, string header, string tag)
    {
        string date = CsvDate();
        var sw = await OpenDatedCsvAsync(baseName, date, header);
        try
        {
            await foreach (var line in reader.ReadAllAsync())
            {
                string today = CsvDate();
                if (today != date)                              // day rolled over → rotate to a fresh file
                {
                    await sw.FlushAsync(); sw.Dispose();
                    date = today;
                    sw = await OpenDatedCsvAsync(baseName, date, header);
                    Console.WriteLine($"[{tag}] rotated to {baseName}_{date}.csv");
                }
                await sw.WriteLineAsync(line);
                await sw.FlushAsync();
            }
        }
        finally { try { await sw.FlushAsync(); } catch { } sw.Dispose(); }
    }

    private async Task RunCsvWriterAsync()
    {
        try
        {
            await DrainWithDailyRotationAsync(_csvChannel.Reader, _csvBaseName, CsvHeader, "CROSS CSV");
        }
        catch (Exception ex)
        {
            Console.WriteLine($"[CROSS CSV ERROR] {ex.Message}");
            DebugLog.Discovery($"RunCsvWriterAsync exception: {ex}");
        }
    }

    private async Task RunHedgeCsvWriterAsync()
    {
        try
        {
            await DrainWithDailyRotationAsync(_hedgeCsvChannel.Reader, _hedgeCsvBaseName, HedgeCsvHeader, "HEDGE CSV");
        }
        catch (Exception ex)
        {
            Console.WriteLine($"[HEDGE CSV ERROR] {ex.Message}");
            DebugLog.Discovery($"RunHedgeCsvWriterAsync exception: {ex}");
        }
    }

    private async Task RunSlipCsvWriterAsync()
    {
        try
        {
            await DrainWithDailyRotationAsync(_slipCsvChannel.Reader, _slipCsvBaseName, SlipCsvHeader, "SLIP CSV");
        }
        catch (Exception ex)
        {
            Console.WriteLine($"[SLIP CSV ERROR] {ex.Message}");
            DebugLog.Discovery($"RunSlipCsvWriterAsync exception: {ex}");
        }
    }
}
