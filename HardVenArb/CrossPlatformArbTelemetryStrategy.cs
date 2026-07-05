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
    bool ThreeWay = false
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
    decimal  OpenPLeg      = -1m   // the HardVen leg price at open
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

    // ── CSV channels ─────────────────────────────────────────────────────────
    private readonly Channel<string> _csvChannel =
        Channel.CreateUnbounded<string>(new UnboundedChannelOptions { SingleReader = true });
    private readonly string _csvBaseName;   // DAILY rotation: file = "{base}_{yyyyMMdd}.csv" (local date)

    private readonly Channel<string> _hedgeCsvChannel =
        Channel.CreateUnbounded<string>(new UnboundedChannelOptions { SingleReader = true });
    private readonly string _hedgeCsvBaseName;

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
        "HardVenInPlay";
    private const string HedgeCsvHeader =
        "OpenTime,PairId,Label,ArbType,OffsetMs," +
        "EntryKalshiAsk,KalshiUnwindBid,KalshiOppositeAsk,KalshiEntryAskNow," +
        "EntryHardVenAsk,HardVenLegNow,KalshiUnwindDepth,EntryNetCost";

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
        decimal depthFloor   = 1m)
    {
        _pairs        = pairs;
        _books        = books;
        _arbThreshold = arbThreshold;
        _depthFloor   = depthFloor;
        _csvBaseName      = "CrossArbTelemetry";
        _hedgeCsvBaseName = "CrossArbHedgeMonitor";

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
                        OpenPLeg:          pLegPrice
                    );
                    _activeWindows[pair.PairId] = w;
                    DebugLog.Discovery($"EvaluatePair {pair.Label}: ARB OPEN {bestType} net={bestNet:0.0000} depth={bestDepth:0.0} kAge={kAge}ms pAge={pAge}ms");
                    invokeOnArbOpened = true;
                    windowJustOpened   = openTime;
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
                        // FIRST eval each leg moves above its open price = when it left "within the arb" (latch once)
                        KLeftWithinAt = (existing.KLeftWithinAt == DateTime.MaxValue && existing.OpenKLeg >= 0m && kLegNow > existing.OpenKLeg) ? evalNow : existing.KLeftWithinAt,
                        PLeftWithinAt = (existing.PLeftWithinAt == DateTime.MaxValue && existing.OpenPLeg >= 0m && pLegNow > existing.OpenPLeg) ? evalNow : existing.PLeftWithinAt
                    };
                    if (betterCost)
                        DebugLog.Discovery($"EvaluatePair {pair.Label}: ARB UPDATE better net={bestNet:0.0000} (was {existing.BestNetCost:0.0000})");
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
        // IN-PLAY tag: was the HardVen game live (vs pre-match) when this window closed? Pre-match legs are stable
        // (near-instant capture); live legs are volatile (the ~8s placement / auto-cancel model). Drives per-window
        // timing in the analyzer so a stable pre-match hold isn't judged by the same clock as a live flicker.
        bool hvInPlay = _books.TryGetValue($"H:{hardvenLegId}", out var hvBook) && hvBook.IsLive;

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
            hvInPlay ? "1" : "0"
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
        try { await Task.WhenAll(_csvWriterTask, _hedgeCsvWriterTask); }
        catch (Exception ex) { DebugLog.Discovery($"ShutdownAsync: CSV writer task threw — {ex.Message}"); }
    }

    private readonly Task _csvWriterTask;
    private readonly Task _hedgeCsvWriterTask;

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
}
