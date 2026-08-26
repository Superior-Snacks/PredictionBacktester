using System.Collections.Concurrent;
using System.Globalization;
using PredictionBacktester.Engine.LiveExecution;

namespace KalshiEvBot;

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

    // A side is CLOSED once it has filled. Keyed ticker|side, so both sides of one game can each hold a
    // position — hence the per-game cap below, which is what actually bounds a game's exposure.
    private readonly ConcurrentDictionary<string, byte> _filled = new(StringComparer.Ordinal);
    private readonly ConcurrentDictionary<string, decimal> _spentByEvent = new(StringComparer.Ordinal);
    private readonly ConcurrentDictionary<string, DateTime> _lastAttempt = new(StringComparer.Ordinal);
    // One order at a time per ticker+side. Two WS deltas can land on the same market within milliseconds and
    // both clear; without this the "one entry" rule is decided by a race.
    private readonly ConcurrentDictionary<string, byte> _inFlight = new(StringComparer.Ordinal);

    public long Attempted, Filled, NoFill, Rejected, Skipped;
    public decimal StakedUsd;

    public LiveExecutor(KalshiOrderClient kalshi, EvLiveLog log, EvConfig cfg)
    {
        _kalshi = kalshi;
        _log = log;
        _cfg = cfg;
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
                                         CancellationToken ct)
    {
        string key = ticker + "|" + side;
        string why = "";
        var t0 = DateTime.UtcNow;

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
            if (count < 1)
            {
                Interlocked.Increment(ref Skipped);
                _log.Write(new EvLiveRow(t0, ticker, eventId, side, limitCents / 100.0, restAsk, pTrue, ev,
                                         0, "", "budget-exhausted", 0, 0, 0));
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
                _filled[key] = 0;                                   // side closed for this game
                decimal cost = fillCount * (avgFill > 0 ? avgFill : (decimal)(limitCents / 100.0));
                _spentByEvent.AddOrUpdate(eventId, cost, (_, prev) => prev + cost);
                StakedUsd += cost;
            }
            else Interlocked.Increment(ref NoFill);

            _log.Write(new EvLiveRow(t0, ticker, eventId, side, limitCents / 100.0, restAsk, pTrue, ev,
                                     count, orderId, got ? "filled" : (status.Length > 0 ? status : "no-fill"),
                                     (double)fillCount, (double)avgFill, ms));
            return got;
        }
        catch (Exception ex)
        {
            Interlocked.Increment(ref Rejected);
            _log.Write(new EvLiveRow(t0, ticker, eventId, side, limitPrice, restAsk, pTrue, ev, 0, "",
                                     "error:" + ex.GetType().Name, 0, 0,
                                     (DateTime.UtcNow - t0).TotalMilliseconds));
            Console.WriteLine($"[LIVE] {ticker} {side}: order FAILED ({ex.GetType().Name}: {ex.Message}) "
                            + "— screening continues.");
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
    int Requested, string OrderId, string Status, double FillCount, double AvgFillPrice, double LatencyMs);

public sealed class EvLiveLog : IDisposable
{
    public static readonly string[] Columns =
    {
        "At", "Ticker", "EventId", "Side", "LimitPrice", "RestAsk", "PTrue", "EvCents",
        "Requested", "OrderId", "Status", "FillCount", "AvgFillPrice", "LatencyMs", "SlippageCents",
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
        });
    }

    public void Dispose() => _csv.Dispose();
}
