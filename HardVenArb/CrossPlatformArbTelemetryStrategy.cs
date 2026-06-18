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
    long     RestDelayMs   = -1
);

record HypotheticalPosition(
    string    PairId,
    string    Label,
    string    ArbType,
    DateTime  EntryTime,
    DateOnly? SettlementDate,
    decimal   EntryCostPerShare,
    decimal   Shares,
    decimal   CapitalRequired,
    int       DaysToSettlement,
    decimal   AprHoldToSettle,
    bool      ExitSignalLogged = false
);

// ── Strategy ──────────────────────────────────────────────────────────────────

public class CrossPlatformArbTelemetryStrategy
{
    private volatile IReadOnlyList<CrossPair> _pairs;
    private readonly ConcurrentDictionary<string, LocalOrderBook> _books;
    private readonly decimal _arbThreshold;
    private readonly decimal _depthFloor;

    // bookKey → pair indices (fast lookup on every delta)
    private readonly Dictionary<string, List<int>> _bookKeyToPairs;
    private readonly ReaderWriterLockSlim _indexLock = new(LockRecursionPolicy.NoRecursion);

    // pairId → open window (null = no arb active)
    private readonly Dictionary<string, ActiveWindow?> _activeWindows;
    private readonly ConcurrentDictionary<string, HypotheticalPosition?> _hypotheticalPositions = new();
    private readonly ConcurrentDictionary<string, (decimal Cost, string Type, decimal Depth)> _nearMiss = new();

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
    private decimal HardVenFee(decimal p, string tokenId)
    {
        if (HardVenFeeParams?.TryGetValue(tokenId, out var fp) == true)
            return fp.R * (decimal)Math.Pow((double)(p * (1m - p)), fp.E);
        return 0.03m * p * (1m - p); // fallback: Sports r=0.03, e=1
    }

    private const decimal HurdleRateApr        = 0.20m;
    private const decimal MinProfitCaptureRatio = 0.70m;

    // ── WS drop counters ─────────────────────────────────────────────────────
    private int _kalshiWsDrops;
    private int _hardvenWsDrops;

    private readonly object _windowLock = new();

    // ── CSV channels ─────────────────────────────────────────────────────────
    private readonly Channel<string> _csvChannel =
        Channel.CreateUnbounded<string>(new UnboundedChannelOptions { SingleReader = true });
    private readonly string _csvPath;
    private bool _headerWritten;

    private readonly Channel<string> _exitCsvChannel =
        Channel.CreateUnbounded<string>(new UnboundedChannelOptions { SingleReader = true });
    private readonly string _exitCsvPath;
    private bool _exitHeaderWritten;

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
        string ts    = DateTime.UtcNow.ToString("yyyyMMdd_HHmmss");
        _csvPath     = $"CrossArbTelemetry_{ts}.csv";
        _exitCsvPath = $"CrossArbExitMonitor_{ts}.csv";

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

        _csvWriterTask     = Task.Run(RunCsvWriterAsync);
        _exitCsvWriterTask = Task.Run(RunExitCsvWriterAsync);
        DebugLog.Discovery($"CrossPlatformArbTelemetryStrategy: initialized with {pairs.Count} pairs, threshold={arbThreshold}");
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
            var existing = _activeWindows[pair.PairId];

            if (isArb)
            {
                if (existing == null)
                {
                    long kAge = kYes.LastDeltaAt > DateTime.MinValue
                        ? (long)(DateTime.UtcNow - kYes.LastDeltaAt).TotalMilliseconds : -1;
                    long pAge = pYes.LastDeltaAt > DateTime.MinValue
                        ? (long)(DateTime.UtcNow - pYes.LastDeltaAt).TotalMilliseconds : -1;

                    var w = new ActiveWindow(
                        PairId:            pair.PairId,
                        ArbType:           bestType,
                        StartTime:         DateTime.UtcNow,
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
                        UpdateCount:       1
                    );
                    _activeWindows[pair.PairId] = w;
                    DebugLog.Discovery($"EvaluatePair {pair.Label}: ARB OPEN {bestType} net={bestNet:0.0000} depth={bestDepth:0.0} kAge={kAge}ms pAge={pAge}ms");
                    invokeOnArbOpened = true;
                }
                else
                {
                    bool betterCost  = bestNet   < existing.BestNetCost;
                    bool betterDepth = bestDepth > Math.Min(existing.KalshiDepth, existing.HardVenDepth);
                    _activeWindows[pair.PairId] = existing with
                    {
                        BestGrossCost = betterCost  ? bestGross    : existing.BestGrossCost,
                        BestNetCost   = betterCost  ? bestNet      : existing.BestNetCost,
                        BestLegPrices = betterCost  ? legPricesNow : existing.BestLegPrices,
                        KalshiFees    = betterCost  ? bestKFee     : existing.KalshiFees,
                        HardVenFees      = betterCost  ? bestPFee     : existing.HardVenFees,
                        KalshiDepth   = betterDepth ? bestKDepth   : existing.KalshiDepth,
                        HardVenDepth     = betterDepth ? bestPDepth   : existing.HardVenDepth,
                        UpdateCount   = existing.UpdateCount + 1
                    };
                    if (betterCost)
                        DebugLog.Discovery($"EvaluatePair {pair.Label}: ARB UPDATE better net={bestNet:0.0000} (was {existing.BestNetCost:0.0000})");
                }
            }
            else if (existing != null)
            {
                DebugLog.Discovery($"EvaluatePair {pair.Label}: ARB CLOSE — net={bestNet:0.0000} above threshold, was open {(DateTime.UtcNow - existing.StartTime).TotalMilliseconds:0}ms");
                CloseWindow(pair.PairId, existing, DateTime.UtcNow, "PRICE");
                _activeWindows[pair.PairId] = null;
            }
        }

        if (invokeOnArbOpened)
            OnArbOpened?.Invoke(pair.PairId, bestNet, bestType, bestDepth, kLegPrice, pLegPrice);

        var posPrefix = pair.PairId + "\x00";
        foreach (var kvp in _hypotheticalPositions)
        {
            if (kvp.Value == null || !kvp.Key.StartsWith(posPrefix, StringComparison.Ordinal)) continue;
            decimal kBid = kvp.Value.ArbType == "K_YES_P_NO" ? kYesBid : kNoBid;
            decimal pBid = kvp.Value.ArbType == "K_YES_P_NO" ? pNoBid  : pYesBid;
            EvaluateExit(kvp.Key, pair, kvp.Value, kBid, pBid);
        }
    }

    // ── Exit position monitoring ──────────────────────────────────────────────

    private void EvaluateExit(string posKey, CrossPair pair, HypotheticalPosition pos, decimal kBid, decimal pBid)
    {
        decimal bidSum = kBid + pBid;
        if (bidSum <= 0m) return;

        string  hardvenToken     = pos.ArbType == "K_YES_P_NO" ? pair.HardVenNoTokenId : pair.HardVenYesTokenId;
        decimal exitFees      = (KalshiFee(kBid) + HardVenFee(pBid, hardvenToken)) * pos.Shares;
        double  daysElapsed   = Math.Max((DateTime.UtcNow - pos.EntryTime).TotalDays, 1.0 / 1440.0);
        double  daysRemaining = Math.Max(pos.DaysToSettlement - daysElapsed, 0.001);

        decimal profitIfExit = (bidSum - pos.EntryCostPerShare) * pos.Shares - exitFees;
        decimal realizedApr  = profitIfExit / pos.CapitalRequired * (365m / (decimal)daysElapsed);
        decimal aprRemaining = (1.00m - bidSum) / bidSum * (365m / (decimal)daysRemaining);
        decimal holdProfit   = (1.00m - pos.EntryCostPerShare) * pos.Shares;
        decimal captureRatio = holdProfit > 0m ? profitIfExit / holdProfit : 0m;

        bool exitDecision = profitIfExit > 0m
                         && aprRemaining  < HurdleRateApr
                         && captureRatio  >= MinProfitCaptureRatio;

        DebugLog.Discovery($"EvaluateExit {pair.Label}: bidSum={bidSum:0.0000} profit={profitIfExit:+0.0000;-0.0000} aprRemaining={aprRemaining:P1} capture={captureRatio:P0} exit={exitDecision}");

        if (exitDecision && !pos.ExitSignalLogged)
        {
            _hypotheticalPositions[posKey] = pos with { ExitSignalLogged = true };
            Console.WriteLine($"[EXIT SIGNAL] {pair.Label} | {pos.ArbType} | " +
                              $"bid_sum={bidSum:0.0000} profit={profitIfExit:+0.00;-0.00} capture={captureRatio:P0} " +
                              $"realized_APR={realizedApr:P1} apr_remaining={aprRemaining:P1} " +
                              $"(<{HurdleRateApr:P0} hurdle, ≥{MinProfitCaptureRatio:P0} capture) | " +
                              $"{daysElapsed:F1}d elapsed / {daysRemaining:F1}d remaining");
            EnqueueExitCsvRow(pos, bidSum, exitFees, profitIfExit, realizedApr, aprRemaining,
                              daysElapsed, daysRemaining, "HURDLE_BREACHED");
        }

        if (pos.SettlementDate.HasValue && daysRemaining <= 0.5)
        {
            _hypotheticalPositions[posKey] = null;
            decimal settleProfit  = (1.00m - pos.EntryCostPerShare) * pos.Shares;
            decimal settleRealApr = settleProfit / pos.CapitalRequired * (365m / (decimal)daysElapsed);
            EnqueueExitCsvRow(pos, 1.00m, 0m, settleProfit, settleRealApr, 0m,
                              daysElapsed, daysRemaining, "SETTLEMENT");
        }
    }

    // NOTE: must be called while holding _windowLock
    private void CloseWindow(string pairId, ActiveWindow w, DateTime endTime, string closedBy)
    {
        long durationMs = (long)(endTime - w.StartTime).TotalMilliseconds;
        if (durationMs < 5) return;

        var pair = _pairs.FirstOrDefault(p => p.PairId == pairId);
        if (pair == null)
        {
            DebugLog.Discovery($"CloseWindow: pair not found for pairId={pairId}, skipping CSV row");
            return;
        }

        decimal fees     = w.KalshiFees + w.HardVenFees;
        decimal profit   = 1m - w.BestNetCost;
        decimal maxDepth = Math.Min(w.KalshiDepth, w.HardVenDepth);
        bool dropDuring  = (Volatile.Read(ref _kalshiWsDrops) > w.KalshiDropsAtOpen)
                        || (Volatile.Read(ref _hardvenWsDrops)   > w.HardVenDropsAtOpen);

        string aprStr = w.AprHoldToSettle >= 0m ? $" APR={w.AprHoldToSettle:P0}" : "";
        Console.WriteLine($"[CROSS ARB CLOSE] {pair.Label} | {w.ArbType} | {durationMs}ms | " +
                          $"gross=${w.BestGrossCost:0.0000} fees=${fees:0.0000} profit/share=${profit:0.0000} | " +
                          $"updates={w.UpdateCount} closedBy={closedBy}{aprStr}");

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
            restDelay
        );

        EnqueueCsvRow(row);

        bool staleAtOpen = (w.KalshiBookAgeMs > 30_000) || (w.HardVenBookAgeMs > 30_000);
        if (w.DaysToSettlement > 0 && maxDepth >= _depthFloor && profit > 0m && !staleAtOpen && !dropDuring)
        {
            string posKey = $"{pairId}\x00{w.StartTime.Ticks}";
            _hypotheticalPositions[posKey] = new HypotheticalPosition(
                PairId:            pairId,
                Label:             pair.Label,
                ArbType:           w.ArbType,
                EntryTime:         w.StartTime,
                SettlementDate:    pair.SettlementDate,
                EntryCostPerShare: w.BestNetCost,
                Shares:            maxDepth,
                CapitalRequired:   maxDepth * w.BestNetCost,
                DaysToSettlement:  w.DaysToSettlement,
                AprHoldToSettle:   w.AprHoldToSettle
            );
            DebugLog.Discovery($"CloseWindow {pair.Label}: created hypothetical position dts={w.DaysToSettlement} profit={profit:0.0000} shares={maxDepth:0.0}");
        }
    }

    // ── CSV infrastructure ────────────────────────────────────────────────────

    private static string Quote(string s) => $"\"{s.Replace("\"", "\"\"")}\"";

    private void EnqueueCsvRow(string row)
    {
        if (!_headerWritten)
        {
            _headerWritten = true;
            string header =
                "StartTime,EndTime,DurationMs,PairId,Label,ArbType," +
                "EntryGrossCost,EntryNetCost,EntryLegPrices," +
                "BestGrossCost,BestNetCost,BestLegPrices,TotalFees,KalshiFees,HardVenFees,NetProfitPerShare," +
                "KalshiDepth,HardVenDepth,MaxDepth,TotalCapitalRequired,TotalPotentialProfit," +
                "KalshiBookAgeMs,HardVenBookAgeMs,KalshiMidSum,HardVenMidSum," +
                "KalshiWsDropsAtOpen,HardVenWsDropsAtOpen,DropDuringWindow," +
                "UpdateCount,ClosedBy," +
                "DaysToSettlement,AprHoldToSettle," +
                "RestChecked,RestConfirmed,RestKalshiAsk,RestHardVenAsk,RestDelayMs";
            _csvChannel.Writer.TryWrite(header);
        }
        _csvChannel.Writer.TryWrite(row);
    }

    private void EnqueueExitCsvRow(HypotheticalPosition pos, decimal bidSum, decimal exitFees,
        decimal profitIfExit, decimal realizedApr, decimal aprRemaining,
        double daysElapsed, double daysRemaining, string trigger)
    {
        if (!_exitHeaderWritten)
        {
            _exitHeaderWritten = true;
            _exitCsvChannel.Writer.TryWrite(
                "EntryTime,ExitSnapshotTime,PairId,Label,ArbType," +
                "DaysToSettlement,AprHoldToSettle," +
                "DaysElapsed,DaysRemaining," +
                "EntryCostPerShare,Shares,CapitalRequired," +
                "BidSum,ExitFees,ProfitIfExit," +
                "RealizedApr,AprRemaining,ExitDecision,Trigger");
        }
        string row = string.Join(",",
            pos.EntryTime.ToString("yyyy-MM-dd HH:mm:ss.fff"),
            DateTime.UtcNow.ToString("yyyy-MM-dd HH:mm:ss.fff"),
            Quote(pos.PairId),
            Quote(pos.Label),
            pos.ArbType,
            pos.DaysToSettlement,
            pos.AprHoldToSettle.ToString("0.0000"),
            daysElapsed.ToString("F2"),
            daysRemaining.ToString("F2"),
            pos.EntryCostPerShare.ToString("0.0000"),
            pos.Shares.ToString("0.00"),
            pos.CapitalRequired.ToString("0.00"),
            bidSum.ToString("0.0000"),
            exitFees.ToString("0.0000"),
            profitIfExit.ToString("0.0000"),
            realizedApr.ToString("0.0000"),
            aprRemaining.ToString("0.0000"),
            aprRemaining < HurdleRateApr ? "1" : "0",
            trigger
        );
        _exitCsvChannel.Writer.TryWrite(row);
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

        foreach (var (_, pos) in _hypotheticalPositions)
        {
            if (pos == null) continue;
            double daysElapsed   = (DateTime.UtcNow - pos.EntryTime).TotalDays;
            double daysRemaining = Math.Max(pos.DaysToSettlement - daysElapsed, 0.0);
            EnqueueExitCsvRow(pos, 0m, 0m, 0m, 0m,
                pos.AprHoldToSettle, daysElapsed, daysRemaining, "SHUTDOWN");
        }

        _csvChannel.Writer.TryComplete();
        _exitCsvChannel.Writer.TryComplete();
        try { await Task.WhenAll(_csvWriterTask, _exitCsvWriterTask); }
        catch (Exception ex) { DebugLog.Discovery($"ShutdownAsync: CSV writer task threw — {ex.Message}"); }
    }

    private readonly Task _csvWriterTask;
    private readonly Task _exitCsvWriterTask;

    private async Task RunCsvWriterAsync()
    {
        try
        {
            using var sw = new StreamWriter(_csvPath, append: false, Encoding.UTF8) { AutoFlush = false };
            await foreach (var line in _csvChannel.Reader.ReadAllAsync())
            {
                await sw.WriteLineAsync(line);
                await sw.FlushAsync();
            }
        }
        catch (Exception ex)
        {
            Console.WriteLine($"[CROSS CSV ERROR] {ex.Message}");
            DebugLog.Discovery($"RunCsvWriterAsync exception: {ex}");
        }
    }

    private async Task RunExitCsvWriterAsync()
    {
        try
        {
            using var sw = new StreamWriter(_exitCsvPath, append: false, Encoding.UTF8) { AutoFlush = false };
            await foreach (var line in _exitCsvChannel.Reader.ReadAllAsync())
            {
                await sw.WriteLineAsync(line);
                await sw.FlushAsync();
            }
        }
        catch (Exception ex)
        {
            Console.WriteLine($"[EXIT CSV ERROR] {ex.Message}");
            DebugLog.Discovery($"RunExitCsvWriterAsync exception: {ex}");
        }
    }
}
