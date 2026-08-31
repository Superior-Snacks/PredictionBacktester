using System.Collections.Concurrent;
using System.Globalization;

namespace KalshiEvBot;

/// <summary>A candidate being watched after the fact, with the numbers it was judged on.</summary>
public sealed record FollowUp(
    DateTime EntryUtc, string Ticker, string Side, IReadOnlyList<string> Legs, int YesLegIndex,
    string Decision, string Regime, double EntryAsk, double EntryPTrue, double EntryEv, string DeVigMethod);

/// <summary>
/// Re-reads both venues at fixed offsets after a candidate, to measure whether the line moved TOWARD the
/// position or away from it.
///
/// <para><b>This is closing line value, and it is the fastest honest verdict available.</b> Settlement is
/// the ground truth but it is days away and enormous in variance — a signal at p≈0.28 has a payoff standard
/// deviation twenty times its edge, so hundreds are needed before the mean means anything. Line movement
/// resolves in a minute, has a fraction of the variance, and is what professional bettors actually track,
/// because a price that keeps moving your way is evidence you were early rather than lucky.</para>
///
/// <para><b>It separates the two failure modes we cannot otherwise tell apart.</b> A leading signal and a
/// following one look identical at the moment of detection: both are a gap between the two venues. They
/// differ entirely in what happens next.
/// <list type="bullet">
/// <item>We LED — Kalshi drifts toward our P_true. The gap closes from their side.</item>
/// <item>We FOLLOWED — our P_true collapses toward Kalshi. The gap closes from ours, and the "edge" was
///       never there. Measured 2026-08-22: a 12c Eredivisie signal whose oracle price fell 0.86 → 0.68
///       within 65 seconds, because a goal had already happened and only Kalshi knew.</item>
/// </list></para>
///
/// <para><b>Costs nothing at the venue.</b> Both readings come from memory — the oracle's quote cache and
/// the local WS book — so a checkpoint is arithmetic, not a request. The WS ask is trustworthy for this:
/// measured against REST it agreed on 59 of 59 comparisons.</para>
///
/// <para>Every candidate that clears the threshold is followed, INCLUDING the ones the guards suppressed.
/// That is the point — it is how a guard gets graded on whether it removed noise or removed edge, without
/// waiting for settlement.</para>
/// </summary>
public sealed class FollowUpTracker : IDisposable
{
    public static readonly string[] Columns =
    {
        "CheckUtc", "EntryUtc", "AgeSec", "Ticker", "Side", "Decision", "MoveRegime",
        "EntryAsk", "EntryPTrue", "EntryEvCents",
        "NowAsk", "NowPTrue", "KalshiDriftCents", "PinnacleDriftCents",
        "GapEntryCents", "GapNowCents", "GapClosedCents", "WhoClosed", "NowEvCents",
    };

    private readonly RollingCsv _csv;
    private readonly PinnacleOracle _oracle;
    private readonly KalshiBookFeed _feed;
    private readonly double[] _checkpoints;
    private readonly ConcurrentQueue<(DateTime Due, int Idx, FollowUp Entry)> _pending = new();

    public long RowsWritten => _csv.RowsWritten;
    public string Path => _csv.Path;
    public int Scheduled;

    public FollowUpTracker(PinnacleOracle oracle, KalshiBookFeed feed, string? directory = null,
                           string prefix = "EvFollowUp")
    {
        _oracle = oracle;
        _feed   = feed;
        _csv    = new RollingCsv(directory ?? Directory.GetCurrentDirectory(), prefix, Columns);
        // 20/40/60 catch the immediate race — who was ahead of whom on this tick. 300 answers a different
        // question: five minutes later, with the goal digested and both books settled, does the position
        // still look right? A gap that closes within a minute is a latency edge; one that is still there at
        // five minutes is a genuine difference of opinion, and those are worth telling apart.
        var raw = (Environment.GetEnvironmentVariable("EV_FOLLOWUP_SEC") ?? "20,40,60,300")
                  .Split(',', StringSplitOptions.RemoveEmptyEntries);
        _checkpoints = raw.Select(x => double.TryParse(x.Trim(), NumberStyles.Any, CultureInfo.InvariantCulture,
                                                       out var v) ? v : -1)
                          .Where(v => v > 0).OrderBy(v => v).ToArray();
        if (_checkpoints.Length == 0) _checkpoints = new[] { 20.0, 40.0, 60.0, 300.0 };
    }

    public string CheckpointsDescription => string.Join("/", _checkpoints.Select(c => $"{c:0}s"));

    public void Schedule(FollowUp e)
    {
        var now = DateTime.UtcNow;
        for (int i = 0; i < _checkpoints.Length; i++)
            _pending.Enqueue((now.AddSeconds(_checkpoints[i]), i, e));
        Interlocked.Increment(ref Scheduled);
    }

    public async Task RunAsync(CancellationToken ct)
    {
        while (!ct.IsCancellationRequested)
        {
            try { await Task.Delay(1000, ct); } catch (OperationCanceledException) { break; }
            var now = DateTime.UtcNow;
            var requeue = new List<(DateTime, int, FollowUp)>();
            while (_pending.TryDequeue(out var item))
            {
                if (item.Due > now) { requeue.Add(item); continue; }
                try { Sample(item.Entry, item.Idx, now); }
                catch (Exception ex) { Console.WriteLine($"[FOLLOWUP] {item.Entry.Ticker}: {ex.Message}"); }
            }
            foreach (var r in requeue) _pending.Enqueue(r);
        }
    }

    /// <summary>Current de-vigged probability of this row's YES leg, or NaN if the book is not readable now.
    /// Deliberately NOT gated on freshness: a checkpoint asks "what does the oracle say at this moment",
    /// and refusing to answer when the quote has aged would hide the very drift we are measuring.</summary>
    private double PTrueNow(FollowUp e)
    {
        var odds = new double[e.Legs.Count];
        for (int i = 0; i < e.Legs.Count; i++)
        {
            var q = _oracle.Get(e.Legs[i]);
            if (q is null || q.DecimalOdds <= 1.0) return double.NaN;
            odds[i] = q.DecimalOdds;
        }
        var d = e.DeVigMethod == "shin" ? DeVig.ShinN(odds) : DeVig.ProportionalN(odds);
        if (!d.Ok || e.YesLegIndex < 0 || e.YesLegIndex >= d.PTrue.Length) return double.NaN;
        double pYes = d.PTrue[e.YesLegIndex];
        return e.Side == "YES" ? pYes : 1.0 - pYes;
    }

    private void Sample(FollowUp e, int idx, DateTime now)
    {
        var top = _feed.Top(e.Ticker);
        double nowAsk = (double)(e.Side == "YES" ? top.YesAsk : top.NoAsk);
        double nowP   = PTrueNow(e);

        // AN UNREADABLE CHECKPOINT IS STILL A RESULT, AND IT IS NOT RANDOM. Over five minutes an in-play
        // match can simply END: Kalshi's book empties, the oracle drops the selection, and there is nothing
        // to compare. Returning silently would delete those rows — and they are not a random subset, they
        // are the fastest-resolving matches, so the surviving 300s sample would quietly be biased toward
        // slow ones. Write the row, say it was unreadable, and let the analysis decide what that means.
        if (nowAsk <= 0 || nowAsk >= 1 || !double.IsFinite(nowP))
        {
            _csv.WriteRow(new[]
            {
                now.ToString("o", CultureInfo.InvariantCulture),
                e.EntryUtc.ToString("o", CultureInfo.InvariantCulture),
                RollingCsv.N((now - e.EntryUtc).TotalSeconds, 1),
                RollingCsv.Q(e.Ticker), RollingCsv.Q(e.Side), RollingCsv.Q(e.Decision), RollingCsv.Q(e.Regime),
                RollingCsv.N(e.EntryAsk, 4), RollingCsv.N(e.EntryPTrue, 4), RollingCsv.N(e.EntryEv * 100, 2),
                "", "", "", "", RollingCsv.N((e.EntryPTrue - e.EntryAsk) * 100, 2), "", "",
                RollingCsv.Q(!double.IsFinite(nowP) ? "oracle-gone" : "book-gone"), "",
            });
            return;
        }

        double kDrift = nowAsk - e.EntryAsk;                 // + = the price we bought rose
        double pDrift = nowP   - e.EntryPTrue;               // + = the oracle got MORE confident in our side
        double gap0   = e.EntryPTrue - e.EntryAsk;           // the disagreement we acted on
        double gap1   = nowP - nowAsk;                       // what is left of it
        double closed = Math.Abs(gap0) - Math.Abs(gap1);     // + = the two venues converged

        // WHICH SIDE DID THE CONVERGING? The whole question in one field. Kalshi moving to us means we were
        // early; our own price moving to Kalshi means we were late and the edge was an artefact.
        // CONVERGENCE AND DIVERGENCE ARE NOT THE SAME EVENT. The first cut keyed on |closed|, so a gap that
        // WIDENED while Kalshi moved more was labelled "kalshi-came-to-us" — the opposite of what happened,
        // and the most flattering possible misreading of a position going against us.
        string mover = Math.Abs(kDrift) >= Math.Abs(pDrift) * 2 ? "kalshi"
                     : Math.Abs(pDrift) >= Math.Abs(kDrift) * 2 ? "us"
                     : "both";
        string who = closed > 0.005  ? (mover == "kalshi" ? "kalshi-came-to-us"
                                      : mover == "us"     ? "we-went-to-kalshi" : "both-converged")
                   : closed < -0.005 ? (mover == "kalshi" ? "diverged-kalshi-away"
                                      : mover == "us"     ? "diverged-we-moved" : "both-diverged")
                   : "neither";

        _csv.WriteRow(new[]
        {
            now.ToString("o", CultureInfo.InvariantCulture),
            e.EntryUtc.ToString("o", CultureInfo.InvariantCulture),
            RollingCsv.N((now - e.EntryUtc).TotalSeconds, 1),
            RollingCsv.Q(e.Ticker), RollingCsv.Q(e.Side), RollingCsv.Q(e.Decision), RollingCsv.Q(e.Regime),
            RollingCsv.N(e.EntryAsk, 4), RollingCsv.N(e.EntryPTrue, 4), RollingCsv.N(e.EntryEv * 100, 2),
            RollingCsv.N(nowAsk, 4), RollingCsv.N(nowP, 4),
            RollingCsv.N(kDrift * 100, 2), RollingCsv.N(pDrift * 100, 2),
            RollingCsv.N(gap0 * 100, 2), RollingCsv.N(gap1 * 100, 2), RollingCsv.N(closed * 100, 2),
            RollingCsv.Q(who),
            RollingCsv.N(EvMath.Ev(nowP, nowAsk) * 100, 2),
        });
    }

    public void Dispose() => _csv.Dispose();
}
