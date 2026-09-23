using System.Globalization;

namespace KalshiEvBot;

/// <summary>
/// One row per filled order: what was resting on the book when we screened it, and what we ACTUALLY paid,
/// level by level, read back from Kalshi's own fill records.
///
/// <para><b>Why this exists.</b> Until 2026-09-22 the order log carried one price per order — the average —
/// so a fill 3c above the screened ask was indistinguishable between two completely different worlds:</para>
/// <list type="bullet">
/// <item>the book REPRICED before our IOC landed, and every contract paid the one new price; or</item>
/// <item>we CONSUMED the ladder, taking progressively worse levels because the order was bigger than the top.</item>
/// </list>
/// <para>Both were observed on 2026-09-22 within three orders: one 25-lot filled 25@0.43 against a screened
/// 0.40 (repriced), another filled 3@0.46 + 2.5@0.48 + 19.5@0.49 (laddered). The distinction decides whether
/// raising <c>EV_LIVE_MAX_CONTRACTS</c> is safe: repricing does not care how big the order is, ladder
/// consumption is caused by it. A projection built on the average price cannot tell them apart.</para>
///
/// <para><b>Off the order path.</b> Written from a detached task AFTER the order has resolved and the EvLive
/// row is on disk, so the extra REST call cannot delay an IOC, and a failure here loses a diagnostic rather
/// than a trade. Roughly one call per fill — a few dozen a day against the same REST budget the resolve uses.</para>
///
/// <para><b>Field shapes are OBSERVED, not assumed</b> (probed 2026-09-22): <c>/portfolio/fills?order_id=</c>
/// returns <c>{cursor, fills[]}</c> where each fill carries <c>count_fp</c> (decimal string — fractional
/// fills are real), <c>yes_price_dollars</c> / <c>no_price_dollars</c>, <c>fee_cost</c>, <c>is_taker</c>,
/// <c>order_id</c> and <c>created_time</c>.</para>
/// </summary>
public sealed class FillLadderLog : IDisposable
{
    public static readonly string[] Columns =
    {
        "At", "OrderId", "Ticker", "Side", "Requested", "Filled",
        "ScreenedAsk", "LimitPrice", "AvgFillPrice", "WalkCents",
        "Levels", "PaidLadder", "BookAtScreen", "Verdict", "FeeTotalUsd",
    };

    private readonly RollingCsv _csv;

    public FillLadderLog(string? directory = null, string prefix = "EvFillLadder")
        => _csv = new RollingCsv(directory ?? Directory.GetCurrentDirectory(), prefix, Columns);

    public string Path => _csv.Path;
    public long RowsWritten => _csv.RowsWritten;

    /// <summary>"19.50@0.4900|2.50@0.4800" — cheapest first, so it reads in the order the book was taken.</summary>
    public static string Ladder(IEnumerable<(decimal Price, decimal Count)> levels)
        => string.Join("|", levels.GroupBy(l => l.Price)
                                  .OrderBy(g => g.Key)
                                  .Select(g => $"{g.Sum(x => x.Count).ToString("0.##", CultureInfo.InvariantCulture)}"
                                             + $"@{g.Key.ToString("0.0000", CultureInfo.InvariantCulture)}"));

    /// <summary>What the walk actually was. The whole point of the file.</summary>
    public static string Classify(decimal screenedAsk, IReadOnlyList<(decimal Price, decimal Count)> levels)
    {
        if (levels.Count == 0) return "no-fills-returned";
        var prices = levels.Select(l => l.Price).Distinct().ToList();
        decimal worst = prices.Max();
        if (prices.Count > 1) return "LADDER";                       // several prices: we ate through levels
        if (screenedAsk <= 0) return "single-price";
        if (worst <= screenedAsk) return "AT-OR-BETTER";             // got the screened price (or better)
        return "REPRICED";                                           // one price, worse than screened: it moved
    }

    public void Write(DateTime utc, string orderId, string ticker, string side, int requested, decimal filled,
                      decimal screenedAsk, decimal limitPrice, decimal avgFill,
                      IReadOnlyList<(decimal Price, decimal Count)> levels, string bookAtScreen, decimal feeTotal)
    {
        _csv.WriteRow(new[]
        {
            utc.ToString("o", CultureInfo.InvariantCulture), RollingCsv.Q(orderId), RollingCsv.Q(ticker),
            RollingCsv.Q(side), requested.ToString(CultureInfo.InvariantCulture), RollingCsv.N(filled, 2),
            RollingCsv.N(screenedAsk, 4), RollingCsv.N(limitPrice, 4), RollingCsv.N(avgFill, 4),
            RollingCsv.N((avgFill - screenedAsk) * 100m, 2),
            levels.Select(l => l.Price).Distinct().Count().ToString(CultureInfo.InvariantCulture),
            RollingCsv.Q(Ladder(levels)), RollingCsv.Q(bookAtScreen),
            RollingCsv.Q(Classify(screenedAsk, levels)), RollingCsv.N(feeTotal, 4),
        });
    }

    public void Dispose() => _csv.Dispose();
}
