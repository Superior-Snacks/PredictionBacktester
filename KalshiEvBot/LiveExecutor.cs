using System.Collections.Concurrent;
using System.Globalization;
using PredictionBacktester.Engine.LiveExecution;

namespace KalshiEvBot;

/// <summary>Console writes serialised across the worker pool.
///
/// <para><b>Why this exists.</b> <c>Console.ForegroundColor</c> is process-global state, so the usual
/// set-write-reset is three separate operations on a shared resource. With EV_REST_CONCURRENCY workers
/// evaluating in parallel, one thread's colour routinely paints another thread's line — the fill/miss
/// colours below would be actively misleading rather than merely untidy. Every coloured write goes
/// through here, and it restores the previous colour rather than resetting, so nesting cannot leak.</para></summary>
public static class Con
{
    public static readonly object Lock = new();

    public static void Line(ConsoleColor c, string s)
    {
        lock (Lock)
        {
            var prev = Console.ForegroundColor;
            Console.ForegroundColor = c;
            Console.WriteLine(s);
            Console.ForegroundColor = prev;
        }
    }
}

/// <summary>The book and oracle state at the instant we fired, carried into the live log so a MISS can be
/// explained rather than merely counted. Depth is the load-bearing one: it separates "we were too slow"
/// from "the size was never there", which is the difference between a latency problem and a capacity
/// ceiling — and section 7's fill rate is uninterpretable without knowing which.</summary>
public readonly record struct TakeCtx(double WsAsk, double DepthToLimit, bool InPlay,
                                      double OracleAgeMs, double WsBookAgeMs, string Regime);

/// <summary>
/// Places the real Kalshi order behind a confirmed signal — M1's only new capability.
///
/// <para><b>WHAT THIS IS FOR, AND IT IS NOT PROFIT.</b> The question M1 answers is "when we find a +EV, can
/// we actually buy it?" — the fill rate inside our slippage tolerance. Everything here is sized so that the
/// answer costs almost nothing to obtain: $5 a side, $10 a game, one FILLED entry per side. A month of this
/// cannot make or lose meaningful money, and that is the point.</para>
///
/// <para><b>IOC, ALWAYS.</b> <see cref="KalshiOrderClient.PlaceOrderAsync"/> sends
/// <c>time_in_force=immediate_or_cancel</c>, so an order either fills now at our limit or dies. Nothing ever
/// rests on the book. That matters more than it sounds: a resting order is a promise to trade at a price we
/// liked seconds ago, and the whole thesis is that prices move.</para>
///
/// <para><b>THE LIMIT PRICE IS THE SLIPPAGE TOLERANCE.</b> We send <c>BreakEvenLimit(p_true, EvMin)</c> — the
/// worst price that still clears our minimum edge. An IOC limit fills at the BEST available price up to that
/// limit, so a fill is +EV by construction and a book that has moved against us simply does not fill. There
/// is no separate slippage check because the venue enforces it atomically, which no round-trip of ours could.</para>
///
/// <para><b>WHY IT FIRES AFTER THE REST CHECK, NOT BEFORE.</b> Ordering on the WS prescreen would remove a
/// round trip, but measured 2026-08-26 over 462,681 rows: WS and REST quote an IDENTICAL price 97.2% of the
/// time, and 97.4% of REST rejections are candidates the 2c prescreen slack let through, NOT prices that
/// moved. So the REST check costs ~2.6% of rejections in latency and earns the other 97.4% in filtering.
/// Firing before it would mean ~428,000 orders against candidates that were never +EV.</para>
///
/// <para><b>A NO-FILL COSTS NOTHING AND IS NOT SPENT.</b> Only a fill consumes a side's allowance, so a
/// market may be re-attempted on a later signal until one lands. Without that, the measurement would be
/// "fill rate of first attempts", which understates what a real bot could achieve. A cooldown stops the
/// re-attempt becoming a hot loop.</para>
/// </summary>
public sealed class LiveExecutor
{
    private readonly KalshiOrderClient _kalshi;
    private readonly EvLiveLog _log;
    private readonly EvConfig _cfg;
    private readonly LivePositionStore? _store;
    // StakedUsd is a decimal, which has no Interlocked. With EV_REST_CONCURRENCY workers a bare += can
    // lose an update, so both it and the persist below happen under one lock.
    private readonly object _bookLock = new();

    // A side is CLOSED once it has filled. Keyed ticker|side, so both sides of one game can each hold a
    // position — hence the per-game cap below, which is what actually bounds a game's exposure.
    private readonly ConcurrentDictionary<string, string> _filled = new(StringComparer.Ordinal);
    private readonly ConcurrentDictionary<string, decimal> _spentByEvent = new(StringComparer.Ordinal);
    private readonly ConcurrentDictionary<string, DateTime> _lastAttempt = new(StringComparer.Ordinal);
    // One order at a time per ticker+side. Two WS deltas can land on the same market within milliseconds and
    // both clear; without this the "one entry" rule is decided by a race.
    private readonly ConcurrentDictionary<string, byte> _inFlight = new(StringComparer.Ordinal);

    public long Attempted, Filled, NoFill, Rejected, Skipped;
    public decimal StakedUsd;

    public LiveExecutor(KalshiOrderClient kalshi, EvLiveLog log, EvConfig cfg, LivePositionStore? store = null)
    {
        _kalshi = kalshi;
        _log = log;
        _cfg = cfg;
        _store = store;
        // RECONCILE THE FEE AGAINST THE VENUE ON EVERY FILL. The multiplier is read live, but the 0.07
        // rate, the quadratic shape and the centicent rounding are hardcoded from a dated schedule and no
        // API field exposes them. This is the only thing that would notice if any of them changed.
        _kalshi.FeeObserved = (tk, filled, avgYes, feePaid) =>
        {
            if (filled <= 0 || feePaid <= 0) return;
            double px = (double)avgYes;
            if (px <= 0 || px >= 1) return;
            // WITH the series multiplier, or a legitimately dearer series would look like a formula change.
            double modelled = EvMath.OrderFee(px, (int)filled, _kalshi.CachedFeeMultiplier(tk)) / (double)filled;
            double actual   = (double)feePaid;
            double gapC     = (actual - modelled) * 100.0;
            if (Math.Abs(gapC) > 0.05)          // half a hundredth of a cent per contract
                Con.Line(ConsoleColor.Red,
                    $"[FEE!] {tk}: venue charged {actual:0.0000}/contract, we modelled {modelled:0.0000} "
                  + $"({gapC:+0.00;-0.00}c). The fee formula has changed — EV is now WRONG.");
        };
        // RESUME, do not restart. Without this the per-side and per-game caps silently become per-PROCESS,
        // and an unattended restart re-enters markets already bought.
        if (_store is not null)
        {
            var (filled, spent) = _store.Load();
            foreach (var (k, v) in filled) _filled[k] = v;
            foreach (var (k, v) in spent)  _spentByEvent[k] = v;
        }
    }

    /// <summary>Contracts to buy: the per-side cap, further bounded by what the game has left, floored to a
    /// whole contract. Returns 0 when the budget cannot buy even one — Kalshi's minimum is 1.</summary>
    private int SizeFor(string eventId, double limitPrice)
    {
        if (limitPrice <= 0 || limitPrice >= 1) return 0;
        decimal spent = _spentByEvent.TryGetValue(eventId, out var s) ? s : 0m;
        decimal room = Math.Min((decimal)_cfg.LiveStakePerSideUsd, (decimal)_cfg.LiveStakePerGameUsd - spent);
        if (room <= 0) return 0;
        return (int)Math.Floor(room / (decimal)limitPrice);
    }

    /// <summary>Fire-and-record. Never throws: a venue error must not take the screening loop down with it.
    /// Returns true only when contracts were actually bought.</summary>
    public async Task<bool> TryTakeAsync(string ticker, string eventId, string side,
                                         double limitPrice, double restAsk, double pTrue, double ev,
                                         TakeCtx ctx, CancellationToken ct)
    {
        string key = ticker + "|" + side;
        string why = "";
        var t0 = DateTime.UtcNow;
        // DECLARED OUT HERE so the catch below can record what was actually attempted. Scoped inside the
        // try, they were unreachable from the handler and every venue error logged Requested=0 - which
        // reads as "not an attempt" and silently drops errored orders out of the fill-rate denominator.
        int attemptCount = 0;
        double attemptPx = 0;

        if (_filled.ContainsKey(key)) why = "side already filled";
        else if (_lastAttempt.TryGetValue(key, out var last)
                 && (t0 - last).TotalSeconds < _cfg.LiveRetryCooldownSec) why = "cooldown";
        else if (!_inFlight.TryAdd(key, 0)) why = "order in flight";

        if (why.Length > 0)
        {
            Interlocked.Increment(ref Skipped);
            return false;
        }

        try
        {
            // PRICE IN WHOLE CENTS, ROUNDED DOWN. Kalshi prices in integer cents; rounding UP would pay a
            // cent more than the limit we computed and could turn a 1c edge negative.
            int limitCents = (int)Math.Floor(limitPrice * 100.0);
            if (limitCents < 1 || limitCents > 99) { Interlocked.Increment(ref Skipped); return false; }
            int count = SizeFor(eventId, limitCents / 100.0);
            attemptCount = count; attemptPx = limitCents / 100.0;
            if (count < 1)
            {
                Interlocked.Increment(ref Skipped);
                _log.Write(new EvLiveRow(t0, ticker, eventId, side, limitCents / 100.0, restAsk, pTrue, ev,
                                         0, "", "budget-exhausted", 0, 0, 0, 0, 0, ctx));
                return false;
            }

            // THE FEE KALSHI CHARGES IS NOT THE FEE THE EV ASSUMED, and the gap is worst exactly here.
            // EvMath.Ev prices the MARGINAL fee (rate*p*(1-p), no count), but the venue rounds the whole
            // order UP to the cent: 3 contracts at 50c pay $0.06 against the $0.0525 the EV assumed. Spread
            // over 3 contracts that is 0.25c each — a quarter of a 1c edge, invisible in the telemetry
            // because no column has ever carried it. At micro-bet size it is the single largest correction
            // to the quoted edge, so it is measured on every order and logged for §8 to total up.
            double pxDollars  = limitCents / 100.0;
            double feeCharged = EvMath.OrderFee(pxDollars, count);
            double feeAssumed = EvMath.FeePerContract(pxDollars) * count;
            double dragPerCtr = count > 0 ? (feeCharged - feeAssumed) / count : 0.0;
            // A BACKSTOP, NOT A FILTER. It refuses only a buy the rounding has pushed to genuinely
            // negative EV — never one that merely dipped below EvMin — because M1's whole job is to
            // measure the fill rate, and a guard that trims the sample would corrupt that measurement to
            // save a fraction of a cent. At these sizes (drag <= 0.25c against EvMin 1c) it should
            // essentially never fire; if §8 shows it firing, the stake is too small to trade at all.
            if (ev - dragPerCtr <= 0)
            {
                Interlocked.Increment(ref Skipped);
                _log.Write(new EvLiveRow(t0, ticker, eventId, side, pxDollars, restAsk, pTrue, ev,
                                         count, "", "fee-rounding-negative", 0, 0, 0, feeCharged, feeAssumed, ctx));
                Con.Line(ConsoleColor.DarkYellow,
                    $"[SKIP] {ticker} {side}: rounded fee ${feeCharged:0.00} on {count} contract(s) "
                  + $"erases the {ev * 100:0.0}c edge.");
                return false;
            }

            _lastAttempt[key] = t0;
            Interlocked.Increment(ref Attempted);

            // client_order_id makes the attempt idempotent at the venue: a retry after a timeout cannot
            // become a second position.
            string coid = $"ev-{ticker}-{side}-{t0:yyyyMMddHHmmssfff}";
            var (orderId, status, fillCount, avgFill) =
                await _kalshi.PlaceOrderAsync(ticker, side.ToLowerInvariant(), limitCents, count,
                                              "buy", coid).WaitAsync(ct);

            double ms = (DateTime.UtcNow - t0).TotalMilliseconds;
            bool got = fillCount > 0;
            if (got)
            {
                Interlocked.Increment(ref Filled);
                _filled[key] = DateTime.UtcNow.ToString("o", CultureInfo.InvariantCulture);
                decimal cost = fillCount * (avgFill > 0 ? avgFill : (decimal)(limitCents / 100.0));
                _spentByEvent.AddOrUpdate(eventId, cost, (_, prev) => prev + cost);
                lock (_bookLock)
                {
                    StakedUsd += cost;
                    // Persist BEFORE anything else can fail. A position that exists at the venue but not in
                    // our record is the one state we must never be in: it is the double-entry we just paid
                    // to prevent.
                    _store?.Save(_filled, _spentByEvent);
                }
            }
            else Interlocked.Increment(ref NoFill);

            string depth = ctx.DepthToLimit < 0 ? "?" : $"{ctx.DepthToLimit:0}";
            if (got)
                Con.Line(ConsoleColor.Blue,
                    $"[FILL] {ticker} {side,-3} {fillCount:0}/{count} @ {(avgFill > 0 ? avgFill : (decimal)pxDollars):0.00} "
                  + $"(limit {pxDollars:0.00}, ws {ctx.WsAsk:0.00})  ev {ev * 100:+0.0;-0.0}c  "
                  + $"${(double)fillCount * (double)(avgFill > 0 ? avgFill : (decimal)pxDollars):0.00}  "
                  + $"{ms:0}ms{(fillCount < count ? $"  [PARTIAL: {depth} showing at the limit]" : "")}");
            else
                Con.Line(ConsoleColor.Yellow,
                    $"[MISS] {ticker} {side,-3} 0/{count} @ limit {pxDollars:0.00} (ws said {ctx.WsAsk:0.00}, "
                  + $"rest {restAsk:0.00})  ev {ev * 100:+0.0;-0.0}c  {ms:0}ms  "
                  + $"depth {depth} at the limit — {(status.Length > 0 ? status : "no fill")}");

            _log.Write(new EvLiveRow(t0, ticker, eventId, side, limitCents / 100.0, restAsk, pTrue, ev,
                                     count, orderId, got ? "filled" : (status.Length > 0 ? status : "no-fill"),
                                     (double)fillCount, (double)avgFill, ms, feeCharged, feeAssumed, ctx));
            return got;
        }
        catch (Exception ex)
        {
            Interlocked.Increment(ref Rejected);
            _log.Write(new EvLiveRow(t0, ticker, eventId, side,
                                     attemptPx > 0 ? attemptPx : limitPrice, restAsk, pTrue, ev,
                                     attemptCount, "", "error:" + ex.GetType().Name, 0, 0,
                                     (DateTime.UtcNow - t0).TotalMilliseconds, 0, 0, ctx));
            Con.Line(ConsoleColor.Red,
                $"[ERR ] {ticker} {side}: order FAILED ({ex.GetType().Name}: {ex.Message}) — screening continues.");
            return false;
        }
        finally
        {
            _inFlight.TryRemove(key, out _);
        }
    }

    public string Summary() =>
        $"live: attempted {Attempted} filled {Filled} no-fill {NoFill} err {Rejected} skipped {Skipped} "
      + $"staked ${StakedUsd:0.00}"
      + (Attempted > 0 ? $" (fill rate {100.0 * Filled / Attempted:0.0}%)" : "");
}

/// <summary>One live-path attempt. Deliberately a SEPARATE file from EvTelemetry: the calibration dataset's
/// schema must not shift mid-collection, and these rows answer a different question (could we buy?) from the
/// telemetry's (was it +EV?).</summary>
public readonly record struct EvLiveRow(
    DateTime At, string Ticker, string EventId, string Side,
    double LimitPrice, double RestAsk, double PTrue, double Ev,
    int Requested, string OrderId, string Status, double FillCount, double AvgFillPrice, double LatencyMs,
    double FeeCharged = 0, double FeeAssumed = 0, TakeCtx Ctx = default);

public sealed class EvLiveLog : IDisposable
{
    public static readonly string[] Columns =
    {
        "At", "Ticker", "EventId", "Side", "LimitPrice", "RestAsk", "PTrue", "EvCents",
        "Requested", "OrderId", "Status", "FillCount", "AvgFillPrice", "LatencyMs", "SlippageCents",
        "FeeChargedUsd", "FeeAssumedUsd", "FeeDragCentsPerCtr",
        "WsAsk", "DepthToLimit", "InPlay", "OracleAgeMs", "WsBookAgeMs", "Regime",
    };

    private readonly RollingCsv _csv;
    public long RowsWritten => _csv.RowsWritten;
    public string Path => _csv.Path;

    public EvLiveLog(string? directory = null)
        => _csv = new RollingCsv(directory ?? Directory.GetCurrentDirectory(), "EvLive", Columns);

    public void Write(EvLiveRow r)
    {
        // SLIPPAGE AGAINST THE PRICE WE SCREENED, not against the limit. The limit is the worst we would
        // accept; what we want to know is how far the fill landed from the ask we valued the signal at.
        // Positive = we paid MORE than the screened ask.
        double slip = (r.FillCount > 0 && r.AvgFillPrice > 0) ? (r.AvgFillPrice - r.RestAsk) * 100.0 : double.NaN;
        _csv.WriteRow(new[]
        {
            r.At.ToString("o", CultureInfo.InvariantCulture),
            RollingCsv.Q(r.Ticker), RollingCsv.Q(r.EventId), RollingCsv.Q(r.Side),
            RollingCsv.N(r.LimitPrice, 4), RollingCsv.N(r.RestAsk, 4), RollingCsv.N(r.PTrue, 4),
            RollingCsv.N(r.Ev * 100, 2),
            r.Requested.ToString(CultureInfo.InvariantCulture),
            RollingCsv.Q(r.OrderId), RollingCsv.Q(r.Status),
            RollingCsv.N(r.FillCount, 2), RollingCsv.N(r.AvgFillPrice, 4), RollingCsv.N(r.LatencyMs, 1),
            double.IsNaN(slip) ? "" : RollingCsv.N(slip, 2),
            RollingCsv.N(r.FeeCharged, 4), RollingCsv.N(r.FeeAssumed, 4),
            // What the rounding actually cost per contract, in cents - the column section 8 sums.
            RollingCsv.N(r.Requested > 0 ? (r.FeeCharged - r.FeeAssumed) / r.Requested * 100.0 : 0, 3),
            RollingCsv.N(r.Ctx.WsAsk, 4), RollingCsv.N(r.Ctx.DepthToLimit, 0),
            r.Ctx.InPlay ? "1" : "0", RollingCsv.N(r.Ctx.OracleAgeMs, 0),
            RollingCsv.N(r.Ctx.WsBookAgeMs, 0), RollingCsv.Q(r.Ctx.Regime ?? ""),
        });
    }

    public void Dispose() => _csv.Dispose();
}
