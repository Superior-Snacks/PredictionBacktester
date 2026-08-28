using System.Globalization;

namespace KalshiEvBot;

/// <summary>What the sizer decided, and enough of its working to audit the decision from the CSV alone.</summary>
public readonly record struct SizeResult(
    double KellyF, double Alpha, double Beta, double Fraction,
    double TargetUsd, int Contracts, bool FlooredToZero);

/// <summary>
/// Fee, expected value and Kelly sizing. Pure functions over doubles — no venue, no clock, no state — so
/// the whole of the bot's arithmetic is testable with no network (see <see cref="SelfTest"/>).
/// </summary>
public static class EvMath
{
    /// <summary>Kalshi's per-contract fee multiplier. Env-overridable because it is ASSUMED, not confirmed:
    /// 0.07 is the published formula, but the taker multiplier has never been checked against one of our own
    /// fills (EVBOT_TODO.md §6). It is the largest single line item against a 1-2c edge, so when it is
    /// confirmed the correction must not need a rebuild.</summary>
    /// <para><b>CONFIRMED 2026-08-28</b> by a real fill (order 01a0480d): 5 contracts at 0.5400 on the
    /// Tennis &amp; Baseball shard moved the balance exactly $2.7870 = $2.70 cost + $0.0870 fee, and
    /// 0.07 x 5 x 0.54 x 0.46 = 0.08694 rounds up to $0.0870. The published schedule (7 Jul 2026) gives
    /// <c>fees = roundup(M x 0.07 x C x P x (1-P))</c> where the round-up is to a CENTICENT on
    /// <i>fee + positionCost</i> — i.e. on the ORDER TOTAL, not per contract. M is per SERIES and is read
    /// live from <c>GET /series/{ticker}.fee_multiplier</c>; all six tennis series read 1 on that date,
    /// but Kalshi can change one at any time, so nothing here assumes it.</para>
    public static readonly double FeeRate =
        double.TryParse(Environment.GetEnvironmentVariable("EV_FEE_RATE"),
                        NumberStyles.Any, CultureInfo.InvariantCulture, out var r) && r >= 0 ? r : 0.07;

    /// <summary>Marginal fee per contract: rate * p * (1-p). Peaks at the money (1.75c at p=0.50 on the
    /// default rate) and is cheapest at the wings (0.33c at 0.05) — so the 0.20-0.80 operating window buys
    /// its protection from de-vig error by paying the most expensive part of this arc.</summary>
    public static double FeePerContract(double p, double m = 1.0) => m * FeeRate * p * (1.0 - p);

    /// <summary>Fee on a whole order, as Kalshi actually charges it: the PER-CONTRACT fee ceiled to
    /// $0.0001, then multiplied by the count.
    ///
    /// <para><b>MEASURED, not assumed</b> (2026-08-28, order 01a0480d, Tennis &amp; Baseball shard):
    /// 5 contracts filled at 0.5400 reported <c>average_fee_paid = 0.0174</c> and moved the balance by
    /// exactly $2.7870 = $2.70 + $0.0870. The model fee is 0.07 x 0.54 x 0.46 = 0.017388, so the venue
    /// ceils PER CONTRACT to a hundredth of a cent (0.017388 -> 0.0174) and multiplies — it does not ceil
    /// the order total to the cent.</para>
    ///
    /// <para><b>What this corrects.</b> The previous model ceiled the whole order to the cent, which
    /// overstated the fee by up to a cent per ORDER — 0.2c per contract on a 5-lot, and the entire basis
    /// of the "fee rounding drag" that section 8 projected. That drag is largely an artefact of this
    /// function, not a cost the venue charges. EV itself was never affected: <see cref="Ev"/> prices
    /// <see cref="FeePerContract"/>, which is the unrounded marginal fee and was always right.</para>
    ///
    /// <para>The same fill also confirmed the 0.07 multiplier on the sports shard, closing the
    /// "assumed, not confirmed" caveat on <see cref="FeeRate"/>.</para></summary>
    public static double OrderFee(double p, int count, double m = 1.0)
        => count <= 0 ? 0.0
         : Math.Ceiling(Math.Round(m * FeeRate * count * p * (1.0 - p) * 10000.0, 9)) / 10000.0;

    /// <summary>All-in cost of owning one contract: the price crossed plus the fee paid to cross.</summary>
    public static double CostPerContract(double execPrice, double m = 1.0)
        => execPrice + FeePerContract(execPrice, m);

    /// <summary>Expected value per contract, fee included. Positive means the price is worth taking.</summary>
    public static double Ev(double pTrue, double execPrice, double m = 1.0)
        => pTrue - CostPerContract(execPrice, m);

    /// <summary>
    /// The highest price at which this signal still clears <paramref name="evMin"/> — i.e. the IOC limit.
    ///
    /// <para>This is the number that makes the bot safe against its own data. The Kalshi WS book reads ~4c
    /// optimistic (EVBOT_TODO.md §4) and even REST is a snapshot, so the order must not be priced at
    /// "the ask we saw". Priced HERE instead, a book that is better than we thought fills cheaply, a book
    /// that is worse does not fill at all, and neither outcome can be a losing trade. The limit is the
    /// protection; the quote is only ever a reason to look.</para>
    ///
    /// <para>Solves p + rate*p*(1-p) = pTrue - evMin for the root in [0,1]:
    /// p = [(1+r) - sqrt((1+r)^2 - 4rT)] / 2r, and the r -&gt; 0 limit p = T.</para>
    /// </summary>
    public static double BreakEvenLimit(double pTrue, double evMin, double m = 1.0)
    {
        double t = pTrue - evMin;
        if (t <= 0) return 0.0;
        double r = m * FeeRate;
        if (r <= 0) return Math.Min(t, 1.0);
        double disc = (1 + r) * (1 + r) - 4 * r * t;
        if (disc < 0) return 0.0;                      // no price clears the threshold
        double p = ((1 + r) - Math.Sqrt(disc)) / (2 * r);
        return Math.Clamp(p, 0.0, 1.0);
    }

    /// <summary>Full-Kelly fraction on a binary contract that costs <c>P_cost</c> and pays 1.
    /// Uses the FEE-INCLUSIVE cost in the denominator as well as the numerator — pricing the odds off the
    /// bare quote while charging the fee only in EV over-sizes every bet slightly, and Kelly compounds.
    /// Clamped at 0: a negative f is the opposite bet, which this bot does not take.</summary>
    public static double FullKelly(double pTrue, double execPrice, double m = 1.0)
    {
        double cost = CostPerContract(execPrice, m);
        if (cost <= 0 || cost >= 1.0) return 0.0;
        return Math.Max(0.0, (pTrue - cost) / (1.0 - cost));
    }

    /// <summary>Bayesian shrinkage on the ORACLE's confidence. A wider Pinnacle vig means a less certain
    /// fair value, so bet less of Kelly. At the measured V = 0.0345 this is ~0.20 (fifth-Kelly). Clamped
    /// above at 0.35 so a crossed book (V &lt; 0) cannot talk the sizer into betting MORE than full-alpha.</summary>
    public static double Alpha(double overround) => Math.Clamp(0.35 * (1.0 - overround / 0.08), 0.10, 0.35);

    /// <summary>Damping for positions held simultaneously: Kelly assumes bets resolve one at a time, and
    /// concurrent ones share the drawdown. Full size while under 10% of bankroll is at risk, tapering to
    /// zero by 30%.</summary>
    public static double Beta(double activeExposureFraction)
        => activeExposureFraction <= 0.10 ? 1.0
         : Math.Max(0.0, 1.0 - (activeExposureFraction - 0.10) / 0.20);

    /// <summary>
    /// Full sizing chain: Kelly -> shrinkage -> damping -> hard 3% cap -> whole contracts.
    ///
    /// <para><c>FlooredToZero</c> is reported rather than swallowed. At a small bankroll the final floor()
    /// turns a perfectly good signal into no trade, and a bot that skips those in silence looks identical
    /// to one that is finding nothing — which is the wrong diagnosis to reach at exactly the moment the
    /// account is smallest.</para>
    /// </summary>
    public static SizeResult Size(double pTrue, double execPrice, double overround,
                                  double bankrollUsd, double activeExposureFraction,
                                  double maxFractionPerTrade = 0.03, double m = 1.0)
    {
        double f     = FullKelly(pTrue, execPrice, m);
        double alpha = Alpha(overround);
        double beta  = Beta(activeExposureFraction);
        double frac  = Math.Min(maxFractionPerTrade, f * alpha * beta);
        if (frac <= 0 || bankrollUsd <= 0 || execPrice <= 0)
            return new SizeResult(f, alpha, beta, Math.Max(0, frac), 0, 0, false);

        double target = bankrollUsd * frac;
        int contracts = (int)Math.Floor(target / execPrice);
        return new SizeResult(f, alpha, beta, frac, target, contracts,
                              FlooredToZero: contracts == 0 && target > 0);
    }
}
