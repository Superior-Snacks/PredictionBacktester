using System.Collections.Concurrent;
using System.Text;
using PredictionBacktester.Engine;

namespace KalshiPolyCross;

// ── Data types ────────────────────────────────────────────────────────────────

public record CrossPair(
    string PairId,          // e.g. "CROSS_KXPRESNOMD-28-KHAR__poly_abc123"
    string Label,           // human-readable e.g. "Kamala Dem Nom 2028"
    string KalshiTicker,    // book keys: "K:{ticker}" and "K:{ticker}_NO"
    string PolyYesTokenId,  // book key:  "P:{yesToken}"
    string PolyNoTokenId    // book key:  "P:{noToken}"
);

record ActiveWindow(
    string PairId,
    string ArbType,           // "K_YES_P_NO" or "K_NO_P_YES"
    DateTime StartTime,
    decimal EntryCost,
    decimal BestCost,
    decimal BestDepth
);

// ── Strategy ─────────────────────────────────────────────────────────────────

public class CrossPlatformArbTelemetryStrategy
{
    private readonly IReadOnlyList<CrossPair> _pairs;
    private readonly ConcurrentDictionary<string, LocalOrderBook> _books;
    private readonly decimal _arbThreshold;
    private readonly decimal _depthFloor;

    // bookKey → list of pair indices that reference it (for fast lookup)
    private readonly Dictionary<string, List<int>> _bookKeyToPairs;

    // pairId → open window (null if no arb currently active)
    private readonly Dictionary<string, ActiveWindow?> _activeWindows;

    // Near-miss tracking: pairId → (bestCost, arbType) for console report
    private readonly ConcurrentDictionary<string, (decimal Cost, string Type, decimal Depth)> _nearMiss = new();

    private readonly string _csvPath;
    private readonly object _csvLock = new();
    private bool _headerWritten;

    // ── Public stats ──────────────────────────────────────────────────────────
    public int OpenArbs  => _activeWindows.Values.Count(w => w != null);
    public int TotalPairs => _pairs.Count;

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
        _csvPath      = $"CrossArbTelemetry_{DateTime.UtcNow:yyyyMMdd_HHmmss}.csv";

        _bookKeyToPairs  = new Dictionary<string, List<int>>(StringComparer.Ordinal);
        _activeWindows   = new Dictionary<string, ActiveWindow?>(StringComparer.Ordinal);

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
    }

    // Called by both WS tasks after updating a book.
    public void OnBookUpdate(string bookKey)
    {
        if (!_bookKeyToPairs.TryGetValue(bookKey, out var indices)) return;
        foreach (var idx in indices)
            EvaluatePair(_pairs[idx]);
    }

    public void OnReconnect()
    {
        // Close all open windows on reconnect — stale book data
        lock (_csvLock)
        {
            foreach (var pairId in _activeWindows.Keys.ToList())
            {
                if (_activeWindows[pairId] is { } w)
                {
                    CloseWindow(pairId, w, DateTime.UtcNow);
                    _activeWindows[pairId] = null;
                }
            }
        }
    }

    // Near-miss report data: list of (cost, label, pairId, arbType, depth) sorted by cost ascending
    public IEnumerable<(decimal Cost, string Label, string PairId, string ArbType, decimal Depth)> GetNearMissSnapshot()
        => _nearMiss
            .Select(kv => (kv.Value.Cost, _pairs.FirstOrDefault(p => p.PairId == kv.Key)?.Label ?? kv.Key,
                           kv.Key, kv.Value.Type, kv.Value.Depth))
            .OrderBy(x => x.Cost);

    // ── Core evaluation ───────────────────────────────────────────────────────

    private void EvaluatePair(CrossPair pair)
    {
        if (!_books.TryGetValue($"K:{pair.KalshiTicker}",    out var kYes)) return;
        if (!_books.TryGetValue($"K:{pair.KalshiTicker}_NO", out var kNo))  return;
        if (!_books.TryGetValue($"P:{pair.PolyYesTokenId}",  out var pYes)) return;
        if (!_books.TryGetValue($"P:{pair.PolyNoTokenId}",   out var pNo))  return;

        // Both platforms must have received at least one live delta
        bool kalshiReady = kYes.HasReceivedDelta;
        bool polyReady   = pYes.HasReceivedDelta && pNo.HasReceivedDelta;
        if (!kalshiReady || !polyReady) return;

        // Stale guard: no update in 120s
        if (kYes.IsStale() || pYes.IsStale() || pNo.IsStale()) return;

        decimal kYesAsk = kYes.GetBestAskPrice();
        decimal kNoAsk  = kNo.GetBestAskPrice();
        decimal pYesAsk = pYes.GetBestAskPrice();
        decimal pNoAsk  = pNo.GetBestAskPrice();

        // Type A: buy Kalshi YES + buy Poly NO
        decimal typeACost = kYesAsk + pNoAsk;
        // Type B: buy Kalshi NO  + buy Poly YES
        decimal typeBCost = kNoAsk  + pYesAsk;

        decimal bestCost;
        string  bestType;
        decimal typeADepth = Math.Min(kYes.GetTopAskVolume(3), pNo.GetTopAskVolume(3));
        decimal typeBDepth = Math.Min(kNo.GetTopAskVolume(3),  pYes.GetTopAskVolume(3));

        if (typeACost <= typeBCost) { bestCost = typeACost; bestType = "K_YES_P_NO"; }
        else                        { bestCost = typeBCost; bestType = "K_NO_P_YES"; }

        decimal bestDepth = bestType == "K_YES_P_NO" ? typeADepth : typeBDepth;

        // Update near-miss tracker (always, regardless of threshold)
        _nearMiss[pair.PairId] = (bestCost, bestType, bestDepth);

        bool isArb = bestCost < _arbThreshold && bestDepth >= _depthFloor;
        var  existing = _activeWindows[pair.PairId];

        if (isArb)
        {
            if (existing == null)
            {
                // Open new window
                var w = new ActiveWindow(pair.PairId, bestType, DateTime.UtcNow, bestCost, bestCost, bestDepth);
                _activeWindows[pair.PairId] = w;
                Console.WriteLine($"[CROSS ARB OPEN] {pair.Label} | {bestType} | cost=${bestCost:0.0000} | depth={bestDepth:0.0}");
            }
            else
            {
                // Extend: update best cost if improved
                if (bestCost < existing.BestCost || bestDepth > existing.BestDepth)
                {
                    _activeWindows[pair.PairId] = existing with
                    {
                        BestCost  = Math.Min(existing.BestCost, bestCost),
                        BestDepth = Math.Max(existing.BestDepth, bestDepth)
                    };
                }
            }
        }
        else if (existing != null)
        {
            // Arb closed
            CloseWindow(pair.PairId, existing, DateTime.UtcNow);
            _activeWindows[pair.PairId] = null;
        }
    }

    private void CloseWindow(string pairId, ActiveWindow w, DateTime endTime)
    {
        long durationMs = (long)(endTime - w.StartTime).TotalMilliseconds;
        if (durationMs < 5) return; // filter micro-glitches

        var pair = _pairs.FirstOrDefault(p => p.PairId == pairId);
        if (pair == null) return;

        string kLeg = w.ArbType == "K_YES_P_NO" ? $"K:{pair.KalshiTicker}"    : $"K:{pair.KalshiTicker}_NO";
        string pLeg = w.ArbType == "K_YES_P_NO" ? $"P:{pair.PolyNoTokenId}"   : $"P:{pair.PolyYesTokenId}";

        decimal grossCost  = w.BestCost;
        decimal fees       = 0m;          // cross-platform fee model TBD
        decimal netCost    = grossCost + fees;
        decimal profit     = 1m - netCost;
        decimal capital    = w.BestDepth * netCost;

        Console.WriteLine($"[CROSS ARB CLOSE] {pair.Label} | {w.ArbType} | {durationMs}ms | profit/share=${profit:0.0000}");

        WriteRow(
            startTime:     w.StartTime,
            endTime:       endTime,
            durationMs:    durationMs,
            eventId:       pairId,
            numLegs:       2,
            legTickers:    $"{kLeg}|{pLeg}",
            legPrices:     w.ArbType == "K_YES_P_NO"
                               ? $"{_books.GetValueOrDefault($"K:{pair.KalshiTicker}")?.GetBestAskPrice():0.0000}|{_books.GetValueOrDefault($"P:{pair.PolyNoTokenId}")?.GetBestAskPrice():0.0000}"
                               : $"{_books.GetValueOrDefault($"K:{pair.KalshiTicker}_NO")?.GetBestAskPrice():0.0000}|{_books.GetValueOrDefault($"P:{pair.PolyYesTokenId}")?.GetBestAskPrice():0.0000}",
            entryCost:     w.EntryCost,
            bestGross:     grossCost,
            totalFees:     fees,
            bestNet:       netCost,
            profitPerShare: profit,
            maxVolume:     w.BestDepth,
            totalCapital:  capital,
            totalProfit:   profit * w.BestDepth,
            restChecked:   false,
            restConfirmed: false,
            restSum:       -1m,
            restDepth:     -1m,
            restDelayMs:   -1,
            arbType:       w.ArbType
        );
    }

    // ── CSV writing ───────────────────────────────────────────────────────────

    private void WriteRow(
        DateTime startTime, DateTime endTime, long durationMs,
        string eventId, int numLegs, string legTickers, string legPrices,
        decimal entryCost, decimal bestGross, decimal totalFees, decimal bestNet,
        decimal profitPerShare, decimal maxVolume, decimal totalCapital, decimal totalProfit,
        bool restChecked, bool restConfirmed, decimal restSum, decimal restDepth, long restDelayMs,
        string arbType)
    {
        lock (_csvLock)
        {
            bool isNew = !_headerWritten;
            using var sw = new StreamWriter(_csvPath, append: true, Encoding.UTF8);

            if (isNew)
            {
                sw.WriteLine("StartTime,EndTime,DurationMs,EventId,NumLegs,LegTickers,LegPrices," +
                             "EntryNetCost,BestGrossCost,TotalFees,BestNetCost,NetProfitPerShare," +
                             "MaxVolume,TotalCapitalRequired,TotalPotentialProfit," +
                             "RestChecked,RestConfirmed,RestYesAskSum,RestMinDepth,RestCheckDelayMs," +
                             "ArbType");
                _headerWritten = true;
            }

            sw.WriteLine(string.Join(",",
                startTime.ToString("yyyy-MM-dd HH:mm:ss.fff"),
                endTime.ToString("yyyy-MM-dd HH:mm:ss.fff"),
                durationMs,
                eventId,
                numLegs,
                legTickers,
                legPrices,
                entryCost.ToString("0.0000"),
                bestGross.ToString("0.0000"),
                totalFees.ToString("0.0000"),
                bestNet.ToString("0.0000"),
                profitPerShare.ToString("0.0000"),
                maxVolume.ToString("0.00"),
                totalCapital.ToString("0.00"),
                totalProfit.ToString("0.0000"),
                restChecked  ? "1" : "0",
                restConfirmed ? "1" : "0",
                restSum < 0 ? "" : restSum.ToString("0.0000"),
                restDepth < 0 ? "" : restDepth.ToString("0.00"),
                restDelayMs < 0 ? "" : restDelayMs.ToString(),
                arbType
            ));
        }
    }
}
