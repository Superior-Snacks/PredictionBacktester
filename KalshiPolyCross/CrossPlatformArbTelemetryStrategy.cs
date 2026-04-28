using System.Collections.Concurrent;
using System.Text;
using System.Threading.Channels;
using PredictionBacktester.Engine;

namespace KalshiPolyCross;

// ── Data types ────────────────────────────────────────────────────────────────

public record CrossPair(
    string PairId,          // unique key
    string Label,           // human-readable
    string KalshiTicker,    // book keys: "K:{ticker}" and "K:{ticker}_NO"
    string PolyYesTokenId,  // book key:  "P:{yesToken}"
    string PolyNoTokenId,   // book key:  "P:{noToken}"
    string EventId = "",     // optional: groups mutually-exclusive legs for blended categorical arb
    DateOnly? SettlementDate = null  // optional: Kalshi market expiry for APR calculations
);

record BlendedWindow(
    string   EventId,
    DateTime StartTime,
    decimal  EntryNetCost,
    string   EntryLegChoices,   // "K,P,K" — which platform was cheapest per leg at open
    string   EntryLegPrices,    // "0.3200,0.2500,0.4100"
    decimal  BestNetCost,
    string   BestLegChoices,
    string   BestLegPrices,
    decimal  MinDepth,
    int      KalshiDropsAtOpen,
    int      PolyDropsAtOpen,
    int      UpdateCount
);

record ActiveWindow(
    string   PairId,
    string   ArbType,             // "K_YES_P_NO" or "K_NO_P_YES"
    DateTime StartTime,
    // ── Entry snapshot (prices at window open) ────────────────────────────────
    decimal  EntryGrossCost,
    decimal  EntryNetCost,
    string   EntryLegPrices,      // "kPrice|pPrice" at open
    // ── Best peak (updated each tick if cost improves) ────────────────────────
    decimal  BestGrossCost,
    decimal  BestNetCost,
    string   BestLegPrices,       // "kPrice|pPrice" at best point
    decimal  KalshiDepth,         // K-side ask depth (contracts) at best
    decimal  PolyDepth,           // P-side ask depth (shares)    at best
    decimal  KalshiFees,          // Kalshi leg fee at best
    decimal  PolyFees,            // Polymarket leg fee at best
    // ── Book health at window open ────────────────────────────────────────────
    long     KalshiBookAgeMs,     // ms since last K delta at open
    long     PolyBookAgeMs,       // ms since last P delta at open
    decimal  KalshiMidSum,        // (kYesMid + kNoMid) — should ≈ 1.00 for healthy K book
    decimal  PolyMidSum,          // (pYesMid + pNoMid) — should ≈ 1.00 for healthy P book
    // ── WS reliability at window open ─────────────────────────────────────────
    int      KalshiDropsAtOpen,
    int      PolyDropsAtOpen,
    // ── Settlement / APR ──────────────────────────────────────────────────────
    int      DaysToSettlement,    // -1 if unknown
    decimal  AprHoldToSettle,     // annualised return if held to settlement; -1 if unknown
    // ── Live tracking ─────────────────────────────────────────────────────────
    int      UpdateCount,
    // ── REST verification (filled async after open via UpdateRestVerification) ─
    bool     RestChecked   = false,
    bool     RestConfirmed = false,
    decimal  RestKalshiAsk = -1m,
    decimal  RestPolyAsk   = -1m,
    long     RestDelayMs   = -1
);

// Represents a hypothetical position entered at a window's best price, tracked
// after the arb window closes to monitor early-exit opportunities.
record HypotheticalPosition(
    string    PairId,
    string    Label,
    string    ArbType,
    DateTime  EntryTime,
    DateOnly? SettlementDate,
    decimal   EntryCostPerShare,  // BestNetCost (includes entry fees)
    decimal   Shares,
    decimal   CapitalRequired,
    int       DaysToSettlement,
    decimal   AprHoldToSettle,
    bool      ExitSignalLogged = false
);

// ── Strategy ─────────────────────────────────────────────────────────────────

public class CrossPlatformArbTelemetryStrategy
{
    private volatile IReadOnlyList<CrossPair> _pairs;
    private readonly ConcurrentDictionary<string, LocalOrderBook> _books;
    private readonly decimal _arbThreshold;
    private readonly decimal _depthFloor;

    // bookKey → pair indices that reference it (fast lookup on every delta)
    private readonly Dictionary<string, List<int>> _bookKeyToPairs;
    private readonly ReaderWriterLockSlim _indexLock = new(LockRecursionPolicy.NoRecursion);

    // pairId → open window (null = no arb active)
    private readonly Dictionary<string, ActiveWindow?> _activeWindows;

    // pairId → hypothetical position being tracked for exit signals (null = none)
    private readonly ConcurrentDictionary<string, HypotheticalPosition?> _hypotheticalPositions = new();

    // Near-miss: pairId → (netCost, arbType, depth)
    private readonly ConcurrentDictionary<string, (decimal Cost, string Type, decimal Depth)> _nearMiss = new();

    // ── Blended categorical arb ───────────────────────────────────────────────
    private readonly Dictionary<string, List<CrossPair>> _eventGroups;
    private readonly Dictionary<string, List<string>>    _bookKeyToEvents;
    private readonly Dictionary<string, BlendedWindow?>  _blendedWindows;
    private readonly ConcurrentDictionary<string, (decimal Cost, string LegChoices, decimal Depth)> _blendedNearMiss = new();

    private readonly Channel<string> _blendedCsvChannel =
        Channel.CreateUnbounded<string>(new UnboundedChannelOptions { SingleReader = true });
    private readonly string _blendedCsvPath;
    private bool _blendedHeaderWritten;

    // ── Fee model ──────────────────────────────────────────────────────────────
    private const decimal KalshiFeeRate   = 0.07m;
    // Post-March-30-2026 fee structure. Politics = 0.04 (most cross-arb pairs);
    // Sports = 0.03. Exponent = 1 for both. Fee formula: p * feeRate * (p*(1-p))^exponent
    private const decimal PolyFeeRate     = 0.04m;
    private const double  PolyFeeExponent = 1.0;

    private static decimal KalshiFee(decimal p) => KalshiFeeRate * p * (1m - p);
    private static decimal PolyFee(decimal p)
        => p * PolyFeeRate * (decimal)Math.Pow((double)(p * (1m - p)), PolyFeeExponent);

    // Exit decision: fire when BOTH conditions hold:
    //   1. APR remaining from holding < hurdle (remaining profit not worth the capital tie-up)
    //   2. Profit captured so far >= min ratio of theoretical hold-to-settle profit
    //      (prevents exiting too early just because settlement is imminent)
    private const decimal HurdleRateApr        = 0.20m; // 20% annualised
    private const decimal MinProfitCaptureRatio = 0.70m; // must have captured ≥70% of max profit

    // ── WS drop counters ──────────────────────────────────────────────────────
    private int _kalshiWsDrops;
    private int _polyWsDrops;

    // ── Locks ──────────────────────────────────────────────────────────────────
    private readonly object _windowLock = new();

    // ── CSV channels ──────────────────────────────────────────────────────────
    private readonly Channel<string> _csvChannel =
        Channel.CreateUnbounded<string>(new UnboundedChannelOptions { SingleReader = true });
    private readonly string _csvPath;
    private bool _headerWritten;

    private readonly Channel<string> _exitCsvChannel =
        Channel.CreateUnbounded<string>(new UnboundedChannelOptions { SingleReader = true });
    private readonly string _exitCsvPath;
    private bool _exitHeaderWritten;

    // ── Public stats ───────────────────────────────────────────────────────────
    public int OpenArbs   => _activeWindows.Values.Count(w => w != null);
    public int TotalPairs => _pairs.Count;

    public event Action<string, decimal, string, decimal>? OnArbOpened;

    /// <summary>Returns the CrossPair for a given pairId (thread-safe volatile read).</summary>
    public CrossPair? GetPair(string pairId) => _pairs.FirstOrDefault(p => p.PairId == pairId);

    private readonly bool _blendedEnabled;

    public CrossPlatformArbTelemetryStrategy(
        IReadOnlyList<CrossPair> pairs,
        ConcurrentDictionary<string, LocalOrderBook> books,
        decimal arbThreshold   = 0.995m,
        decimal depthFloor     = 1m,
        bool    blendedEnabled = true)
    {
        _pairs          = pairs;
        _books          = books;
        _arbThreshold   = arbThreshold;
        _depthFloor     = depthFloor;
        _blendedEnabled = blendedEnabled;
        string ts     = DateTime.UtcNow.ToString("yyyyMMdd_HHmmss");
        _csvPath         = $"CrossArbTelemetry_{ts}.csv";
        _blendedCsvPath  = $"CrossArbBlended_{ts}.csv";
        _exitCsvPath     = $"CrossArbExitMonitor_{ts}.csv";

        _bookKeyToPairs = new Dictionary<string, List<int>>(StringComparer.Ordinal);
        _activeWindows  = new Dictionary<string, ActiveWindow?>(StringComparer.Ordinal);

        for (int i = 0; i < pairs.Count; i++)
        {
            var p = pairs[i];
            foreach (var key in new[] { $"K:{p.KalshiTicker}", $"K:{p.KalshiTicker}_NO",
                                         $"P:{p.PolyYesTokenId}", $"P:{p.PolyNoTokenId}" })
            {
                if (!_bookKeyToPairs.TryGetValue(key, out var list))
                    _bookKeyToPairs[key] = list = new List<int>();
                list.Add(i);
            }
            _activeWindows[p.PairId] = null;
        }

        _eventGroups    = new Dictionary<string, List<CrossPair>>(StringComparer.Ordinal);
        _bookKeyToEvents = new Dictionary<string, List<string>>(StringComparer.Ordinal);
        _blendedWindows  = new Dictionary<string, BlendedWindow?>(StringComparer.Ordinal);

        foreach (var p in pairs.Where(p => !string.IsNullOrEmpty(p.EventId)))
        {
            if (!_eventGroups.TryGetValue(p.EventId, out var grp))
                _eventGroups[p.EventId] = grp = new List<CrossPair>();
            grp.Add(p);
        }
        foreach (var (evId, grp) in _eventGroups)
        {
            _blendedWindows[evId] = null;
            foreach (var p in grp)
                foreach (var key in new[] { $"K:{p.KalshiTicker}", $"P:{p.PolyYesTokenId}" })
                {
                    if (!_bookKeyToEvents.TryGetValue(key, out var evList))
                        _bookKeyToEvents[key] = evList = new List<string>();
                    if (!evList.Contains(evId)) evList.Add(evId);
                }
        }
        if (_blendedEnabled && _eventGroups.Count > 0)
            Console.WriteLine($"[BLENDED] {_eventGroups.Count} event group(s) with " +
                              $"{_eventGroups.Values.Sum(g => g.Count)} total legs registered for blended arb");

        _csvWriterTask        = Task.Run(RunCsvWriterAsync);
        _blendedCsvWriterTask = Task.Run(RunBlendedCsvWriterAsync);
        _exitCsvWriterTask    = Task.Run(RunExitCsvWriterAsync);
    }

    // ── Public interface ───────────────────────────────────────────────────────

    public void OnBookUpdate(string bookKey)
    {
        List<int>?    indices  = null;
        List<string>? eventIds = null;
        _indexLock.EnterReadLock();
        try
        {
            _bookKeyToPairs.TryGetValue(bookKey, out indices);
            _bookKeyToEvents.TryGetValue(bookKey, out eventIds);
        }
        finally { _indexLock.ExitReadLock(); }

        var pairsSnap = _pairs;
        if (indices  != null) foreach (var idx   in indices)  EvaluatePair(pairsSnap[idx]);
        if (_blendedEnabled && eventIds != null) foreach (var evId in eventIds) EvaluateBlendedGroup(evId, _eventGroups[evId]);
    }

    public void OnKalshiReconnect() => HandlePlatformReconnect(ref _kalshiWsDrops, "KALSHI");
    public void OnPolyReconnect()   => HandlePlatformReconnect(ref _polyWsDrops,   "POLY");

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
                    CloseWindow(pairId, w, DateTime.UtcNow, "RECONNECT");
                    _activeWindows[pairId] = null;
                }
            }
            foreach (var evId in _blendedWindows.Keys.ToList())
            {
                if (_blendedWindows[evId] is { } bw && _eventGroups.TryGetValue(evId, out var grp))
                {
                    CloseBlendedWindow(evId, bw, grp, DateTime.UtcNow, "RECONNECT");
                    _blendedWindows[evId] = null;
                }
            }
        }
        _nearMiss.Clear();
        _blendedNearMiss.Clear();
    }

    public void UpdateRestVerification(string pairId, bool confirmed,
        decimal kalshiAsk, decimal polyAsk, long delayMs)
    {
        lock (_windowLock)
        {
            if (!_activeWindows.TryGetValue(pairId, out var w) || w == null) return;
            _activeWindows[pairId] = w with
            {
                RestChecked   = true,
                RestConfirmed = confirmed,
                RestKalshiAsk = kalshiAsk,
                RestPolyAsk   = polyAsk,
                RestDelayMs   = delayMs
            };
            if (!confirmed) return;

            var pair     = _pairs.FirstOrDefault(p => p.PairId == pairId);
            string label = pair?.Label ?? pairId;
            decimal depth = Math.Min(w.KalshiDepth, w.PolyDepth);
            string aprStr = w.AprHoldToSettle >= 0m ? $" APR={w.AprHoldToSettle:P0}" : "";
            Console.WriteLine($"[CONFIRMED ARB] {label} | {w.ArbType} | " +
                              $"K={kalshiAsk:0.0000} P={polyAsk:0.0000} net=${kalshiAsk + polyAsk:0.0000} | " +
                              $"depth={depth:0.0} (K={w.KalshiDepth:0.0}/P={w.PolyDepth:0.0}){aprStr} | verified in {delayMs}ms");
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
                                             $"P:{p.PolyYesTokenId}", $"P:{p.PolyNoTokenId}" })
                {
                    if (!_bookKeyToPairs.TryGetValue(key, out var list))
                        _bookKeyToPairs[key] = list = new List<int>();
                    list.Add(idx);
                }

                if (!string.IsNullOrEmpty(p.EventId))
                {
                    if (!_eventGroups.TryGetValue(p.EventId, out var grp))
                        _eventGroups[p.EventId] = grp = new List<CrossPair>();
                    grp.Add(p);
                    foreach (var key in new[] { $"K:{p.KalshiTicker}", $"P:{p.PolyYesTokenId}" })
                    {
                        if (!_bookKeyToEvents.TryGetValue(key, out var evList))
                            _bookKeyToEvents[key] = evList = new List<string>();
                        if (!evList.Contains(p.EventId)) evList.Add(p.EventId);
                    }
                }
            }
        }
        finally { _indexLock.ExitWriteLock(); }

        lock (_windowLock)
        {
            foreach (var p in newPairs)
            {
                if (!_activeWindows.ContainsKey(p.PairId))
                    _activeWindows[p.PairId] = null;
                if (!string.IsNullOrEmpty(p.EventId) && !_blendedWindows.ContainsKey(p.EventId))
                    _blendedWindows[p.EventId] = null;
            }
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

    public IEnumerable<(decimal Cost, string EventId, string LegChoices, decimal Depth, bool IsLive)>
        GetBlendedNearMissSnapshot()
    {
        HashSet<string> liveIds;
        lock (_windowLock)
            liveIds = _blendedWindows.Where(kv => kv.Value != null).Select(kv => kv.Key).ToHashSet();

        return _blendedNearMiss
            .Select(kv => (kv.Value.Cost, kv.Key, kv.Value.LegChoices, kv.Value.Depth, liveIds.Contains(kv.Key)))
            .OrderBy(x => x.Cost);
    }

    // ── Core evaluation ────────────────────────────────────────────────────────

    private void EvaluatePair(CrossPair pair)
    {
        if (!_books.TryGetValue($"K:{pair.KalshiTicker}",    out var kYes)) return;
        if (!_books.TryGetValue($"K:{pair.KalshiTicker}_NO", out var kNo))  return;
        if (!_books.TryGetValue($"P:{pair.PolyYesTokenId}",  out var pYes)) return;
        if (!_books.TryGetValue($"P:{pair.PolyNoTokenId}",   out var pNo))  return;

        if (!kYes.HasReceivedDelta || !kNo.HasReceivedDelta || !pYes.HasReceivedDelta || !pNo.HasReceivedDelta) return;
        if (kYes.IsStale() || kNo.IsStale() || pYes.IsStale() || pNo.IsStale()) return;

        decimal kYesAsk = kYes.GetBestAskPrice();
        decimal kNoAsk  = kNo.GetBestAskPrice();
        decimal pYesAsk = pYes.GetBestAskPrice();
        decimal pNoAsk  = pNo.GetBestAskPrice();

        if (kYesAsk < 0.05m || kNoAsk < 0.05m || pYesAsk < 0.05m || pNoAsk < 0.05m) return;

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

        if (kMidSum < 0.70m || kMidSum > 1.30m) return;
        if (pMidSum < 0.70m || pMidSum > 1.30m) return;

        // Type A: buy Kalshi YES + buy Poly NO
        decimal kYesFee    = KalshiFee(kYesAsk);
        decimal pNoFee     = PolyFee(pNoAsk);
        decimal typeAGross = kYesAsk + pNoAsk;
        decimal typeAFees  = kYesFee + pNoFee;
        decimal typeANet   = typeAGross + typeAFees;
        decimal typeAKDepth = kYes.GetTopAskVolume(3);
        decimal typeAPDepth = pNo.GetTopAskVolume(3);
        decimal typeADepth  = Math.Min(typeAKDepth, typeAPDepth);

        // Type B: buy Kalshi NO + buy Poly YES
        decimal kNoFee     = KalshiFee(kNoAsk);
        decimal pYesFee    = PolyFee(pYesAsk);
        decimal typeBGross = kNoAsk + pYesAsk;
        decimal typeBFees  = kNoFee + pYesFee;
        decimal typeBNet   = typeBGross + typeBFees;
        decimal typeBKDepth = kNo.GetTopAskVolume(3);
        decimal typeBPDepth = pYes.GetTopAskVolume(3);
        decimal typeBDepth  = Math.Min(typeBKDepth, typeBPDepth);

        decimal bestGross, bestNet, bestKFee, bestPFee, bestKDepth, bestPDepth;
        string  bestType;
        decimal kLegPrice, pLegPrice;

        if (typeANet <= typeBNet)
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

        bool invokeOnArbOpened = false;
        int currentKalshiDrops = Volatile.Read(ref _kalshiWsDrops);
        int currentPolyDrops   = Volatile.Read(ref _polyWsDrops);

        // Settlement / APR at window open
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
                        PolyDepth:         bestPDepth,
                        KalshiFees:        bestKFee,
                        PolyFees:          bestPFee,
                        KalshiBookAgeMs:   kAge,
                        PolyBookAgeMs:     pAge,
                        KalshiMidSum:      kMidSum,
                        PolyMidSum:        pMidSum,
                        KalshiDropsAtOpen: currentKalshiDrops,
                        PolyDropsAtOpen:   currentPolyDrops,
                        DaysToSettlement:  daysToSettle,
                        AprHoldToSettle:   aprHoldSettle,
                        UpdateCount:       1
                    );
                    _activeWindows[pair.PairId] = w;

                    invokeOnArbOpened = true;
                }
                else
                {
                    bool betterCost  = bestNet   < existing.BestNetCost;
                    bool betterDepth = bestDepth > Math.Min(existing.KalshiDepth, existing.PolyDepth);
                    _activeWindows[pair.PairId] = existing with
                    {
                        BestGrossCost = betterCost  ? bestGross    : existing.BestGrossCost,
                        BestNetCost   = betterCost  ? bestNet      : existing.BestNetCost,
                        BestLegPrices = betterCost  ? legPricesNow : existing.BestLegPrices,
                        KalshiFees    = betterCost  ? bestKFee     : existing.KalshiFees,
                        PolyFees      = betterCost  ? bestPFee     : existing.PolyFees,
                        KalshiDepth   = betterDepth ? bestKDepth   : existing.KalshiDepth,
                        PolyDepth     = betterDepth ? bestPDepth   : existing.PolyDepth,
                        UpdateCount   = existing.UpdateCount + 1
                    };
                }
            }
            else if (existing != null)
            {
                CloseWindow(pair.PairId, existing, DateTime.UtcNow, "PRICE");
                _activeWindows[pair.PairId] = null;
            }
        }

        if (invokeOnArbOpened)
            OnArbOpened?.Invoke(pair.PairId, bestNet, bestType, bestDepth);

        // Exit monitoring: evaluate ALL open hypothetical positions for this pair.
        // Each entry is stored under a compound key (pairId + \x00 + entryTicks) so
        // multiple re-entries on the same pair are tracked independently.
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

        decimal exitFees   = (KalshiFee(kBid) + PolyFee(pBid)) * pos.Shares;
        double  daysElapsed  = Math.Max((DateTime.UtcNow - pos.EntryTime).TotalDays, 1.0 / 1440.0);
        double  daysRemaining = Math.Max(pos.DaysToSettlement - daysElapsed, 0.001);

        // profit_if_exit_now: gross proceeds minus all-in entry cost minus exit fees
        // EntryCostPerShare already includes entry fees (BestNetCost = gross + entry fees)
        decimal profitIfExit   = (bidSum - pos.EntryCostPerShare) * pos.Shares - exitFees;
        decimal realizedApr    = profitIfExit / pos.CapitalRequired * (365m / (decimal)daysElapsed);
        decimal aprRemaining   = (1.00m - bidSum) / bidSum * (365m / (decimal)daysRemaining);

        // Profit capture: how much of the theoretical hold-to-settle profit has been realised?
        // holdProfit excludes exit fees (settlement is free); profitIfExit includes exit fees.
        decimal holdProfit     = (1.00m - pos.EntryCostPerShare) * pos.Shares;
        decimal captureRatio   = holdProfit > 0m ? profitIfExit / holdProfit : 0m;

        bool    exitDecision   = profitIfExit > 0m
                              && aprRemaining  < HurdleRateApr
                              && captureRatio  >= MinProfitCaptureRatio;

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

        // Remove position once settlement is imminent.
        // At settlement there are no exit fees — one leg resolves to $1, the other $0.
        if (pos.SettlementDate.HasValue && daysRemaining <= 0.5)
        {
            _hypotheticalPositions[posKey] = null;
            decimal settleProfit    = (1.00m - pos.EntryCostPerShare) * pos.Shares;
            decimal settleRealApr   = settleProfit / pos.CapitalRequired * (365m / (decimal)daysElapsed);
            EnqueueExitCsvRow(pos, 1.00m, 0m, settleProfit, settleRealApr, 0m,
                              daysElapsed, daysRemaining, "SETTLEMENT");
        }
    }

    private void EvaluateBlendedGroup(string eventId, List<CrossPair> group)
    {
        decimal totalNet = 0m;
        decimal minDepth = decimal.MaxValue;
        var legChoices = new List<string>(group.Count);
        var legPrices  = new List<string>(group.Count);

        foreach (var pair in group)
        {
            if (!_books.TryGetValue($"K:{pair.KalshiTicker}",   out var kYes)) return;
            if (!_books.TryGetValue($"P:{pair.PolyYesTokenId}", out var pYes)) return;
            if (!kYes.HasReceivedDelta || !pYes.HasReceivedDelta) return;
            if (kYes.IsStale() || pYes.IsStale()) return;

            decimal kYesAsk = kYes.GetBestAskPrice();
            decimal pYesAsk = pYes.GetBestAskPrice();
            if (kYesAsk < 0.02m || pYesAsk < 0.02m) return;

            decimal kNet = kYesAsk + KalshiFee(kYesAsk);
            decimal pNet = pYesAsk + PolyFee(pYesAsk);

            if (kNet <= pNet)
            {
                totalNet += kNet;
                minDepth  = Math.Min(minDepth, kYes.GetTopAskVolume(3));
                legChoices.Add("K");
                legPrices.Add(kYesAsk.ToString("0.0000"));
            }
            else
            {
                totalNet += pNet;
                minDepth  = Math.Min(minDepth, pYes.GetTopAskVolume(3));
                legChoices.Add("P");
                legPrices.Add(pYesAsk.ToString("0.0000"));
            }
        }

        if (minDepth == decimal.MaxValue) minDepth = 0m;
        string choices = string.Join(",", legChoices);
        string prices  = string.Join(",", legPrices);
        _blendedNearMiss[eventId] = (totalNet, choices, minDepth);

        bool isArb = totalNet < _arbThreshold && minDepth >= _depthFloor;
        int currentKDrops = Volatile.Read(ref _kalshiWsDrops);
        int currentPDrops = Volatile.Read(ref _polyWsDrops);

        lock (_windowLock)
        {
            var existing = _blendedWindows.TryGetValue(eventId, out var bw) ? bw : null;

            if (isArb)
            {
                if (existing == null)
                {
                    var w = new BlendedWindow(
                        EventId:           eventId,
                        StartTime:         DateTime.UtcNow,
                        EntryNetCost:      totalNet,
                        EntryLegChoices:   choices,
                        EntryLegPrices:    prices,
                        BestNetCost:       totalNet,
                        BestLegChoices:    choices,
                        BestLegPrices:     prices,
                        MinDepth:          minDepth,
                        KalshiDropsAtOpen: currentKDrops,
                        PolyDropsAtOpen:   currentPDrops,
                        UpdateCount:       1
                    );
                    _blendedWindows[eventId] = w;
                    if (_blendedEnabled)
                        Console.WriteLine($"[BLENDED ARB OPEN ] {eventId} ({group.Count} legs) | " +
                                          $"net=${totalNet:0.0000} choices={choices} prices={prices} | depth={minDepth:0.0}");
                }
                else
                {
                    bool betterCost  = totalNet < existing.BestNetCost;
                    bool betterDepth = minDepth > existing.MinDepth;
                    _blendedWindows[eventId] = existing with
                    {
                        BestNetCost    = betterCost  ? totalNet : existing.BestNetCost,
                        BestLegChoices = betterCost  ? choices  : existing.BestLegChoices,
                        BestLegPrices  = betterCost  ? prices   : existing.BestLegPrices,
                        MinDepth       = betterDepth ? minDepth : existing.MinDepth,
                        UpdateCount    = existing.UpdateCount + 1
                    };
                }
            }
            else if (existing != null)
            {
                CloseBlendedWindow(eventId, existing, group, DateTime.UtcNow, "PRICE");
                _blendedWindows[eventId] = null;
            }
        }
    }

    // NOTE: must be called while holding _windowLock
    private void CloseBlendedWindow(string eventId, BlendedWindow w, List<CrossPair> group, DateTime endTime, string closedBy)
    {
        long durationMs = (long)(endTime - w.StartTime).TotalMilliseconds;
        if (durationMs < 5) return;

        decimal profit   = 1m - w.BestNetCost;
        bool dropDuring  = (Volatile.Read(ref _kalshiWsDrops) > w.KalshiDropsAtOpen) || (Volatile.Read(ref _polyWsDrops) > w.PolyDropsAtOpen);
        string legTickers = string.Join(";", group.Select(p => p.KalshiTicker));

        if (_blendedEnabled)
            Console.WriteLine($"[BLENDED ARB CLOSE] {eventId} | {durationMs}ms | " +
                              $"net=${w.BestNetCost:0.0000} profit/set=${profit:0.0000} | " +
                              $"depth={w.MinDepth:0.0} closedBy={closedBy}");

        string row = string.Join(",",
            w.StartTime.ToString("yyyy-MM-dd HH:mm:ss.fff"),
            endTime.ToString("yyyy-MM-dd HH:mm:ss.fff"),
            durationMs,
            Quote(eventId),
            group.Count,
            Quote(legTickers),
            w.EntryNetCost.ToString("0.0000"),
            Quote(w.EntryLegChoices),
            Quote(w.EntryLegPrices),
            w.BestNetCost.ToString("0.0000"),
            Quote(w.BestLegChoices),
            Quote(w.BestLegPrices),
            w.MinDepth.ToString("0.00"),
            (w.MinDepth * w.BestNetCost).ToString("0.00"),
            (profit * w.MinDepth).ToString("0.0000"),
            w.KalshiDropsAtOpen,
            w.PolyDropsAtOpen,
            dropDuring ? "1" : "0",
            w.UpdateCount,
            closedBy
        );
        EnqueueBlendedCsvRow(row);
    }

    // NOTE: must be called while holding _windowLock
    private void CloseWindow(string pairId, ActiveWindow w, DateTime endTime, string closedBy)
    {
        long durationMs = (long)(endTime - w.StartTime).TotalMilliseconds;
        if (durationMs < 5) return;

        var pair = _pairs.FirstOrDefault(p => p.PairId == pairId);
        if (pair == null) return;

        decimal fees     = w.KalshiFees + w.PolyFees;
        decimal profit   = 1m - w.BestNetCost;
        decimal maxDepth = Math.Min(w.KalshiDepth, w.PolyDepth);
        bool dropDuring  = (Volatile.Read(ref _kalshiWsDrops) > w.KalshiDropsAtOpen) || (Volatile.Read(ref _polyWsDrops) > w.PolyDropsAtOpen);

        string aprStr = w.AprHoldToSettle >= 0m ? $" APR={w.AprHoldToSettle:P0}" : "";
        Console.WriteLine($"[CROSS ARB CLOSE] {pair.Label} | {w.ArbType} | {durationMs}ms | " +
                          $"gross=${w.BestGrossCost:0.0000} fees=${fees:0.0000} profit/share=${profit:0.0000} | " +
                          $"updates={w.UpdateCount} closedBy={closedBy}{aprStr}");

        string restKalshi = w.RestKalshiAsk >= 0 ? w.RestKalshiAsk.ToString("0.0000") : "";
        string restPoly   = w.RestPolyAsk   >= 0 ? w.RestPolyAsk.ToString("0.0000")   : "";
        string restDelay  = w.RestDelayMs   >= 0 ? w.RestDelayMs.ToString()            : "";
        string dts        = w.DaysToSettlement >= 0 ? w.DaysToSettlement.ToString() : "";
        string apr        = w.AprHoldToSettle  >= 0 ? w.AprHoldToSettle.ToString("0.0000") : "";

        string row = string.Join(",",
            w.StartTime.ToString("yyyy-MM-dd HH:mm:ss.fff"),
            endTime.ToString("yyyy-MM-dd HH:mm:ss.fff"),
            durationMs,
            Quote(pairId),
            Quote(pair.Label),
            w.ArbType,
            // entry
            w.EntryGrossCost.ToString("0.0000"),
            w.EntryNetCost.ToString("0.0000"),
            Quote(w.EntryLegPrices),
            // best
            w.BestGrossCost.ToString("0.0000"),
            w.BestNetCost.ToString("0.0000"),
            Quote(w.BestLegPrices),
            fees.ToString("0.0000"),
            w.KalshiFees.ToString("0.0000"),
            w.PolyFees.ToString("0.0000"),
            profit.ToString("0.0000"),
            // depth
            w.KalshiDepth.ToString("0.00"),
            w.PolyDepth.ToString("0.00"),
            maxDepth.ToString("0.00"),
            (maxDepth * w.BestNetCost).ToString("0.00"),
            (profit * maxDepth).ToString("0.0000"),
            // book health
            w.KalshiBookAgeMs,
            w.PolyBookAgeMs,
            w.KalshiMidSum.ToString("0.0000"),
            w.PolyMidSum.ToString("0.0000"),
            // WS reliability
            w.KalshiDropsAtOpen,
            w.PolyDropsAtOpen,
            dropDuring ? "1" : "0",
            // tracking
            w.UpdateCount,
            closedBy,
            // settlement / APR
            dts,
            apr,
            // REST
            w.RestChecked   ? "1" : "0",
            w.RestConfirmed ? "1" : "0",
            restKalshi,
            restPoly,
            restDelay
        );

        EnqueueCsvRow(row);

        // Create a hypothetical position for exit monitoring if we have settlement data and real depth.
        // Skip stale-book windows — the arb was a ghost and the profit is fictitious.
        // Key includes entry ticks so multiple arb windows on the same pair are tracked independently.
        bool staleAtOpen = (w.KalshiBookAgeMs > 30_000) || (w.PolyBookAgeMs > 30_000);
        if (w.DaysToSettlement > 0 && maxDepth >= _depthFloor && profit > 0m && !staleAtOpen && !dropDuring)
        {
            string posKey = $"{pairId}\x00{w.StartTime.Ticks}";
            _hypotheticalPositions[posKey] = new HypotheticalPosition(
                PairId:           pairId,
                Label:            pair.Label,
                ArbType:          w.ArbType,
                EntryTime:        w.StartTime,
                SettlementDate:   pair.SettlementDate,
                EntryCostPerShare: w.BestNetCost,
                Shares:           maxDepth,
                CapitalRequired:  maxDepth * w.BestNetCost,
                DaysToSettlement: w.DaysToSettlement,
                AprHoldToSettle:  w.AprHoldToSettle
            );
        }
    }

    // ── CSV infrastructure ─────────────────────────────────────────────────────

    private static string Quote(string s) => $"\"{s.Replace("\"", "\"\"")}\"";

    private void EnqueueCsvRow(string row)
    {
        if (!_headerWritten)
        {
            _headerWritten = true;
            string header =
                "StartTime,EndTime,DurationMs,PairId,Label,ArbType," +
                "EntryGrossCost,EntryNetCost,EntryLegPrices," +
                "BestGrossCost,BestNetCost,BestLegPrices,TotalFees,KalshiFees,PolyFees,NetProfitPerShare," +
                "KalshiDepth,PolyDepth,MaxDepth,TotalCapitalRequired,TotalPotentialProfit," +
                "KalshiBookAgeMs,PolyBookAgeMs,KalshiMidSum,PolyMidSum," +
                "KalshiWsDropsAtOpen,PolyWsDropsAtOpen,DropDuringWindow," +
                "UpdateCount,ClosedBy," +
                "DaysToSettlement,AprHoldToSettle," +
                "RestChecked,RestConfirmed,RestKalshiAsk,RestPolyAsk,RestDelayMs";
            _csvChannel.Writer.TryWrite(header);
        }
        _csvChannel.Writer.TryWrite(row);
    }

    private void EnqueueBlendedCsvRow(string row)
    {
        if (!_blendedHeaderWritten)
        {
            _blendedHeaderWritten = true;
            _blendedCsvChannel.Writer.TryWrite(
                "StartTime,EndTime,DurationMs,EventId,NumLegs,LegTickers," +
                "EntryNetCost,EntryLegChoices,EntryLegPrices," +
                "BestNetCost,BestLegChoices,BestLegPrices," +
                "MinDepth,TotalCapitalRequired,TotalPotentialProfit," +
                "KalshiWsDropsAtOpen,PolyWsDropsAtOpen,DropDuringWindow," +
                "UpdateCount,ClosedBy");
        }
        _blendedCsvChannel.Writer.TryWrite(row);
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
        // Flush any still-open arb windows to the main CSV
        lock (_windowLock)
        {
            var now = DateTime.UtcNow;
            foreach (var pairId in _activeWindows.Keys.ToList())
            {
                if (_activeWindows[pairId] is { } w)
                {
                    CloseWindow(pairId, w, now, "SHUTDOWN");
                    _activeWindows[pairId] = null;
                }
            }
            if (_blendedEnabled)
            {
                foreach (var evId in _blendedWindows.Keys.ToList())
                {
                    if (_blendedWindows[evId] is { } bw && _eventGroups.TryGetValue(evId, out var grp))
                    {
                        CloseBlendedWindow(evId, bw, grp, now, "SHUTDOWN");
                        _blendedWindows[evId] = null;
                    }
                }
            }
        }

        // Flush any remaining hypothetical positions to the exit CSV
        foreach (var (pairId, pos) in _hypotheticalPositions)
        {
            if (pos == null) continue;
            double daysElapsed   = (DateTime.UtcNow - pos.EntryTime).TotalDays;
            double daysRemaining = Math.Max(pos.DaysToSettlement - daysElapsed, 0.0);
            // Log with zero exit values on shutdown (no live book data available)
            EnqueueExitCsvRow(pos, 0m, 0m, 0m, 0m,
                pos.AprHoldToSettle, daysElapsed, daysRemaining, "SHUTDOWN");
        }

        _csvChannel.Writer.TryComplete();
        _blendedCsvChannel.Writer.TryComplete();
        _exitCsvChannel.Writer.TryComplete();
        await Task.WhenAll(_csvWriterTask, _blendedCsvWriterTask, _exitCsvWriterTask);
    }

    private readonly Task _csvWriterTask;
    private readonly Task _blendedCsvWriterTask;
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
        catch (Exception ex) { Console.WriteLine($"[CROSS CSV ERROR] {ex.Message}"); }
    }

    private async Task RunBlendedCsvWriterAsync()
    {
        try
        {
            using var sw = new StreamWriter(_blendedCsvPath, append: false, Encoding.UTF8) { AutoFlush = false };
            await foreach (var line in _blendedCsvChannel.Reader.ReadAllAsync())
            {
                await sw.WriteLineAsync(line);
                await sw.FlushAsync();
            }
        }
        catch (Exception ex) { Console.WriteLine($"[BLENDED CSV ERROR] {ex.Message}"); }
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
        catch (Exception ex) { Console.WriteLine($"[EXIT CSV ERROR] {ex.Message}"); }
    }
}
