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

    /// <summary>
    /// Damping for positions held SIMULTANEOUSLY. Kelly's fraction is optimal only if bets resolve one at a
    /// time — bet, settle, re-size against the new bankroll, bet again. Hold twenty at once and they all
    /// draw on one bankroll, so a bad run compounds into a drawdown Kelly never priced because Kelly assumed
    /// you would have sized down after each loss. That matters more here than in a generic book: our
    /// concurrent positions are tennis matches on one slate, priced by one oracle through one de-vig model,
    /// so when it is wrong it is wrong across all of them at once.
    ///
    /// <para><b>The knee and zero points are NOT derived.</b> The correct treatment is multi-asset Kelly
    /// with a correlation matrix; this is a hand-chosen proxy for it. Defaults here are the ORIGINAL
    /// 0.10/0.30 so <see cref="Size"/> — the frozen telemetry basis — cannot move. The live path passes its
    /// own, gentler pair.</para>
    ///
    /// <para>Exposure is measured as money SPENT, which for a binary contract is exactly the maximum loss,
    /// so it is the right quantity even though it reads like a cost.</para>
    /// </summary>
    public static double Beta(double activeExposureFraction, double knee = 0.10, double zero = 0.30)
    {
        if (activeExposureFraction <= knee) return 1.0;
        if (zero <= knee) return 0.0;                       // degenerate config: refuse rather than divide
        return Math.Max(0.0, 1.0 - (activeExposureFraction - knee) / (zero - knee));
    }

    /// <summary>
    /// Full sizing chain: Kelly -> shrinkage -> damping -> hard 3% cap -> whole contracts.
    ///
    /// <para><c>FlooredToZero</c> is reported rather than swallowed. At a small bankroll the final floor()
    /// turns a perfectly good signal into no trade, and a bot that skips those in silence looks identical
    /// to one that is finding nothing — which is the wrong diagnosis to reach at exactly the moment the
    /// account is smallest.</para>
    /// </summary>
    /// <summary>
    /// The stake to actually PLACE, in dollars. Same Kelly chain as <see cref="Size"/>, three differences —
    /// each of which exists because this one spends money and that one does not.
    ///
    /// <para><b>1. It sizes off REAL equity, never the pinned telemetry bankroll.</b> `BankrollUsd` is frozen
    /// at EV_BANKROLL_USD so the CSV's Contracts column stays comparable across the whole dataset; it is
    /// deliberately fake money. Sizing a real order off it was measured on 2026-09-07 as a 2.24x overbet —
    /// $576.29 pinned against $257.16 actually on the shard.</para>
    ///
    /// <para><b>2. Contracts are floored against COST, not price.</b> `Size` divides the target by the ask,
    /// so the real outlay is target + fee and every order quietly overspends its own Kelly target. Small
    /// (~0.4%) but it is a systematic overbet in the same direction as everything else here.</para>
    ///
    /// <para><b>3. Bounded WITHOUT distorting the size Kelly asked for.</b> `maxUsd` cuts the stake — a
    /// risk cap, and capping bets LESS than Kelly wants, which can only cost upside. `minUsd` REFUSES: a
    /// stake under the floor is skipped, never raised to it.</para>
    ///
    /// <para><b>Raising a stake to the floor would bet MORE than Kelly says on exactly the signals Kelly
    /// likes least</b> — over-betting the weakest edges is the one direction a bound must not err in, and
    /// it makes the floor rather than the model decide the size. Measured 2026-09-07: at $257 equity a $5
    /// floor would have decided 93% of stakes, at $576 65%. That is flat staking wearing Kelly's name.</para>
    ///
    /// <para>So the floor GATES rather than inflates: below it there is no bet, and the size of every bet
    /// placed is the one the maths asked for (or the ceiling, downward).</para>
    /// </summary>
    public static double LiveStakeUsd(double pTrue, double execPrice, double overround,
                                      double equityUsd, double activeExposureFraction,
                                      double minUsd, double maxUsd,
                                      double maxFractionPerTrade = 0.03, double m = 1.0,
                                      double kellyFraction = 0.0,
                                      double betaKnee = 0.10, double betaZero = 0.30,
                                      double maxEdge = 0.0)
    {
        if (equityUsd <= 0 || execPrice <= 0 || execPrice >= 1) return 0.0;
        // EDGE SHRINKAGE. Kelly's numerator is the edge, and Kelly scales the stake linearly with it — so
        // a +6.7c signal is staked at more than three times a +2c one. That is right if +6.7c is real.
        // Measured 2026-09-12 it is where the model is least trustworthy: the win-rate-vs-P_true gap
        // widens with quoted edge (0 points at 1-2c, 11 at 4-5c, 20 past the implausibility band), and
        // on real fills the contract-weighted edge is ~0 while the order-weighted edge is +1.1c — the
        // biggest orders are the worst ones. The tail of the EV distribution is where a stale quote, a
        // swapped leg or a mispair hides, and Kelly amplifies exactly that.
        //
        // So the edge FED TO KELLY is capped. The signal still fires on its full EV (that gate is
        // elsewhere); only the SIZE stops growing past `maxEdge`. A Bayesian reading: our posterior on a
        // +6.7c edge is not +6.7c, it is "at least a few cents and probably less than claimed". 0 = off,
        // which keeps every existing caller and the telemetry sizer exactly as they were.
        double pForKelly = maxEdge > 0 ? Math.Min(pTrue, CostPerContract(execPrice, m) + maxEdge) : pTrue;
        double f    = FullKelly(pForKelly, execPrice, m);

        // `kellyFraction > 0` OVERRIDES Alpha with a flat fraction (0.25 = quarter Kelly). Alpha is a
        // VIG-based shrinkage — it scales with how wide Pinnacle's book is, as a proxy for how confident
        // the oracle is — and it is NOT a discount for the strategy being unproven. Those are different
        // quantities that happen to land near each other (Alpha p50 = 0.157, i.e. ~1/6.4, against the
        // docstring's claimed ~0.20). Anyone choosing "quarter Kelly" means the flat one, so make that
        // sayable instead of leaving it to be approximated by tuning a vig curve.
        double shrink = kellyFraction > 0 ? kellyFraction : Alpha(overround);
        double frac = Math.Min(maxFractionPerTrade,
                               f * shrink * Beta(activeExposureFraction, betaKnee, betaZero));
        if (frac <= 0) return 0.0;

        double stake = equityUsd * frac;
        if (maxUsd > 0) stake = Math.Min(stake, maxUsd);   // cap DOWN: costs upside, never over-bets
        return stake < minUsd ? 0.0 : stake;               // REFUSE below the floor; never round up to it
    }

    /// <summary>Whole contracts a dollar stake buys, priced at COST (ask + marginal fee) so the outlay does
    /// not exceed the stake. Kalshi's minimum is one contract, so this floors to 0 rather than rounding.</summary>
    public static int ContractsFor(double stakeUsd, double execPrice, double m = 1.0, int maxContracts = 0)
    {
        double cost = CostPerContract(execPrice, m);
        if (cost <= 0 || stakeUsd <= 0) return 0;
        int n = (int)Math.Floor(stakeUsd / cost);
        // A CONTRACT CAP, NOT JUST A DOLLAR CAP. $25 buys 113 contracts of a 22c dog and 41 of a 60c
        // favourite; the dog position pays out $113 or $0 and the favourite $41 or $0. The dollar ceiling
        // bounds the loss; it does nothing for the variance, which is what actually compounds a bankroll
        // down. Capping the count bounds the payout swing per position regardless of price. 0 = off.
        return maxContracts > 0 ? Math.Min(n, maxContracts) : n;
    }

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
