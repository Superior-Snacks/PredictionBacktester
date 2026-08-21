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
    public static readonly double FeeRate =
        double.TryParse(Environment.GetEnvironmentVariable("EV_FEE_RATE"),
                        NumberStyles.Any, CultureInfo.InvariantCulture, out var r) && r >= 0 ? r : 0.07;

    /// <summary>Marginal fee per contract: rate * p * (1-p). Peaks at the money (1.75c at p=0.50 on the
    /// default rate) and is cheapest at the wings (0.33c at 0.05) — so the 0.20-0.80 operating window buys
    /// its protection from de-vig error by paying the most expensive part of this arc.</summary>
    public static double FeePerContract(double p) => FeeRate * p * (1.0 - p);

    /// <summary>Fee on a whole order, rounded UP to the cent as Kalshi charges it. Differs from
    /// <see cref="FeePerContract"/> x count only at tiny sizes — where it matters most, because a
    /// single contract at 0.50 is charged 2c, not 1.75c, and M2 will trade at exactly that size.</summary>
    /// <para>The inner Round is not cosmetic. 0.07 x 100 x 0.5 x 0.5 evaluates to 175.00000000000003 cents
    /// in binary floating point, and a bare Ceiling turns that into 176 — an invented cent on every order
    /// that lands exactly on a boundary, always against us. Snap off the representation noise first.</para>
    public static double OrderFee(double p, int count)
        => count <= 0 ? 0.0 : Math.Ceiling(Math.Round(FeeRate * count * p * (1.0 - p) * 100.0, 9)) / 100.0;

    /// <summary>All-in cost of owning one contract: the price crossed plus the fee paid to cross.</summary>
    public static double CostPerContract(double execPrice) => execPrice + FeePerContract(execPrice);

    /// <summary>Expected value per contract, fee included. Positive means the price is worth taking.</summary>
    public static double Ev(double pTrue, double execPrice) => pTrue - CostPerContract(execPrice);

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
    public static double BreakEvenLimit(double pTrue, double evMin)
    {
        double t = pTrue - evMin;
        if (t <= 0) return 0.0;
        double r = FeeRate;
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
    public static double FullKelly(double pTrue, double execPrice)
    {
        double cost = CostPerContract(execPrice);
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
                                  double maxFractionPerTrade = 0.03)
    {
        double f     = FullKelly(pTrue, execPrice);
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
