using System.Collections.Concurrent;
using System.Globalization;

namespace KalshiEvBot;

/// <summary>A candidate being watched after the fact, with the numbers it was judged on.</summary>
public sealed record FollowUp(
    DateTime EntryUtc, string Ticker, string Side, IReadOnlyList<string> Legs, int YesLegIndex,
    string Decision, string Regime, double EntryAsk, double EntryPTrue, double EntryEv, string DeVigMethod,
    // WsDepthToLimit at entry. -1 = the WS ask sat above the limit (nobody offering at our price); a row
    // written before this existed reads NaN. Section 9 splits on it, because the two populations converge
    // differently and only one of them can be bought.
    double EntryDepth = double.NaN);

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
        // NowBid: the price we could SELL at right now (1 - the other side's ask on the WS book). NowAsk is
        // the most flattering reference for a buyer's closing-line value; the bid is the most conservative.
        // If the +1.5c the ask shows at T+20 survives on the bid, the market repriced; if not, the spread
        // widened. EntryDepth: see the FollowUp record.
        "NowBid", "EntryDepth",
        // APPENDED 2026-10-05. The oracle is now read by the SIGNAL path's rules (every leg quoted, open,
        // fresh); NowPTrue and everything derived from it are blank when that fails, and PinState says why:
        // open | suspended | stale | gone. PinLegs is each leg as cached - odds/status/live|pre/seconds since
        // its price last moved - and NowPTrueRaw what the cache would have said regardless, so a broken
        // reading stays visible instead of silently becoming a "Pinnacle move".
        "PinState", "PinLegs", "NowPTrueRaw",
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
        // 5 and 10 added 2026-09-01: an in-play tennis book reprices in seconds, so the interesting
        // part of the convergence curve is BEFORE 20s — by then most of the move has happened and the
        // measurement is of the tail rather than the event. The sampler already wakes every second, and
        // both readings come from memory, so extra checkpoints cost arithmetic and no venue request.
        var raw = (Environment.GetEnvironmentVariable("EV_FOLLOWUP_SEC") ?? "5,10,20,40,60,300")
                  .Split(',', StringSplitOptions.RemoveEmptyEntries);
        _checkpoints = raw.Select(x => double.TryParse(x.Trim(), NumberStyles.Any, CultureInfo.InvariantCulture,
                                                       out var v) ? v : -1)
                          .Where(v => v > 0).OrderBy(v => v).ToArray();
        if (_checkpoints.Length == 0) _checkpoints = new[] { 5.0, 10.0, 20.0, 40.0, 60.0, 300.0 };
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

    /// <summary>The oracle's view of one row's side at a checkpoint. <c>P</c> is the de-vigged P_true read by
    /// the SIGNAL path's rules - every leg quoted, open and fresh - else NaN. <c>Raw</c> is the de-vigged value
    /// of whatever is cached, valid or not. <c>State</c>: open | suspended | stale | gone. <c>Legs</c>: each
    /// leg as "odds/status/live|pre/seconds-since-its-price-moved" ("-" = not quoted).</summary>
    internal readonly record struct OracleRead(double P, double Raw, string State, string Legs);

    /// <summary>
    /// Reads the oracle at a checkpoint BY THE SIGNAL PATH'S RULES.
    ///
    /// <para><b>Why it used to read anything at all, and why that broke.</b> The first version was deliberately
    /// NOT gated - "a checkpoint asks what the oracle says at this moment, and refusing to answer when the quote
    /// has aged would hide the very drift we are measuring". But it also took suspended legs and, through the
    /// sidecar's keepalive re-seed, the frozen PRE-MATCH line served as an in-play price. Measured 2026-10-05:
    /// of 47 in-play readings that jumped >=10c within 12s of a signal, 39 (83%) sat exactly on that market's
    /// pre-match price, Kalshi did not follow a single one, and the price came back on the next push. Those
    /// were read as "Pinnacle snapping against us" - a third of the winner's curse, a yellow radar - and they
    /// were not prices at all. A checkpoint now gives a P_true only when the signal path would have accepted
    /// it as one, and says what it saw (<c>State</c>, <c>Legs</c>, <c>Raw</c>) when it would not.</para>
    /// </summary>
    internal static OracleRead ReadOracle(PinnacleOracle oracle, FollowUp e, DateTime now)
    {
        var odds = new double[e.Legs.Count];
        var legs = new string[e.Legs.Count];
        bool anyMissing = false, anySuspended = false, anyStale = false;
        for (int i = 0; i < e.Legs.Count; i++)
        {
            var q = oracle.Get(e.Legs[i]);
            if (q is null) { legs[i] = "-"; anyMissing = true; continue; }
            string moved = q.OddsChangedUtc == DateTime.MinValue ? "?"
                         : (now - q.OddsChangedUtc).TotalSeconds.ToString("0.0", CultureInfo.InvariantCulture) + "s";
            legs[i] = $"{q.DecimalOdds.ToString("0.###", CultureInfo.InvariantCulture)}/{q.Status}/{(q.Live ? "live" : "pre")}/{moved}";
            odds[i] = q.DecimalOdds;
            if (q.DecimalOdds <= 1.0) anyMissing = true;
            else if (!q.Open) anySuspended = true;
            else if (!oracle.Fresh(q)) anyStale = true;
        }
        double raw = double.NaN;
        if (!anyMissing)
        {
            var d = e.DeVigMethod == "shin" ? DeVig.ShinN(odds) : DeVig.ProportionalN(odds);
            if (d.Ok && e.YesLegIndex >= 0 && e.YesLegIndex < d.PTrue.Length)
            {
                double pYes = d.PTrue[e.YesLegIndex];
                raw = e.Side == "YES" ? pYes : 1.0 - pYes;
            }
        }
        string state = anyMissing || !double.IsFinite(raw) ? "gone"
                     : anySuspended ? "suspended"
                     : anyStale ? "stale" : "open";
        return new OracleRead(state == "open" ? raw : double.NaN, raw, state, string.Join("|", legs));
    }

    private void Sample(FollowUp e, int idx, DateTime now)
    {
        var top = _feed.Top(e.Ticker);
        double nowAsk = (double)(e.Side == "YES" ? top.YesAsk : top.NoAsk);
        // Kalshi is a YES-book: the NO ask IS 1 - the YES bid, so our side's bid is 1 - the other side's ask.
        double otherAsk = (double)(e.Side == "YES" ? top.NoAsk : top.YesAsk);
        double nowBid   = otherAsk > 0 && otherAsk < 1 ? 1.0 - otherAsk : double.NaN;
        _csv.WriteRow(Row(e, now, nowAsk, nowBid, ReadOracle(_oracle, e, now)));
    }

    /// <summary>One follow-up row. Internal so the self-test can check every case without a live feed.</summary>
    internal static string[] Row(FollowUp e, DateTime now, double nowAsk, double nowBid, OracleRead o)
    {
        // AN UNREADABLE CHECKPOINT IS STILL A RESULT, AND IT IS NOT RANDOM. Over five minutes an in-play
        // match can simply END: Kalshi's book empties, the oracle drops the selection, and there is nothing
        // to compare. Returning silently would delete those rows — and they are not a random subset, they
        // are the fastest-resolving matches, so the surviving 300s sample would quietly be biased toward
        // slow ones. Write the row, say it was unreadable, and let the analysis decide what that means.
        //
        // EACH VENUE IS WRITTEN WHEN IT IS READABLE, INDEPENDENTLY (2026-10-05). An unreadable oracle used to
        // blank Kalshi's reading too - but Kalshi's move is the closing-line value, and it does not need
        // Pinnacle. With the oracle now refusing suspended legs (a point is being played most of the time
        // in-play), keeping that rule would have thrown away the CLV of exactly those seconds.
        bool book = nowAsk > 0 && nowAsk < 1;
        bool pin  = double.IsFinite(o.P);
        double gap0 = e.EntryPTrue - e.EntryAsk;              // the disagreement we acted on
        string gapNow = "", closedS = "", nowEv = "", who;
        if (book && pin)
        {
            double kDrift = nowAsk - e.EntryAsk;              // + = the price we bought rose
            double pDrift = o.P    - e.EntryPTrue;            // + = the oracle got MORE confident in our side
            double gap1   = o.P - nowAsk;                     // what is left of it
            double closed = Math.Abs(gap0) - Math.Abs(gap1);  // + = the two venues converged

            // WHICH SIDE DID THE CONVERGING? The whole question in one field. Kalshi moving to us means we were
            // early; our own price moving to Kalshi means we were late and the edge was an artefact.
            // CONVERGENCE AND DIVERGENCE ARE NOT THE SAME EVENT. The first cut keyed on |closed|, so a gap that
            // WIDENED while Kalshi moved more was labelled "kalshi-came-to-us" — the opposite of what happened,
            // and the most flattering possible misreading of a position going against us.
            string mover = Math.Abs(kDrift) >= Math.Abs(pDrift) * 2 ? "kalshi"
                         : Math.Abs(pDrift) >= Math.Abs(kDrift) * 2 ? "us"
                         : "both";
            who = closed > 0.005  ? (mover == "kalshi" ? "kalshi-came-to-us"
                                   : mover == "us"     ? "we-went-to-kalshi" : "both-converged")
                : closed < -0.005 ? (mover == "kalshi" ? "diverged-kalshi-away"
                                   : mover == "us"     ? "diverged-we-moved" : "both-diverged")
                : "neither";
            gapNow  = RollingCsv.N(gap1 * 100, 2);
            closedS = RollingCsv.N(closed * 100, 2);
            nowEv   = RollingCsv.N(EvMath.Ev(o.P, nowAsk) * 100, 2);
        }
        else who = !pin ? $"oracle-{o.State}" : "book-gone";

        return new[]
        {
            now.ToString("o", CultureInfo.InvariantCulture),
            e.EntryUtc.ToString("o", CultureInfo.InvariantCulture),
            RollingCsv.N((now - e.EntryUtc).TotalSeconds, 1),
            RollingCsv.Q(e.Ticker), RollingCsv.Q(e.Side), RollingCsv.Q(e.Decision), RollingCsv.Q(e.Regime),
            RollingCsv.N(e.EntryAsk, 4), RollingCsv.N(e.EntryPTrue, 4), RollingCsv.N(e.EntryEv * 100, 2),
            book ? RollingCsv.N(nowAsk, 4) : "", pin ? RollingCsv.N(o.P, 4) : "",
            book ? RollingCsv.N((nowAsk - e.EntryAsk) * 100, 2) : "", pin ? RollingCsv.N((o.P - e.EntryPTrue) * 100, 2) : "",
            RollingCsv.N(gap0 * 100, 2), gapNow, closedS,
            RollingCsv.Q(who),
            nowEv,
            book ? RollingCsv.N(nowBid, 4) : "", RollingCsv.N(e.EntryDepth, 0),
            RollingCsv.Q(o.State), RollingCsv.Q(o.Legs), double.IsFinite(o.Raw) ? RollingCsv.N(o.Raw, 4) : "",
        };
    }

    public void Dispose() => _csv.Dispose();
}
