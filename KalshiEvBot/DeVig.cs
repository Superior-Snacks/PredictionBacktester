namespace KalshiEvBot;

/// <summary>Output of one de-vig of a two-way book. <c>PTrue</c> is for the FIRST leg passed in.</summary>
/// <param name="PTrue">Fair probability of leg A, vig removed.</param>
/// <param name="Overround">V = S - 1, the book's margin. 0.0345 was the measured Pinnacle median.</param>
/// <param name="ShinZ">Shin's insider-trading parameter. 0 for the proportional method, and 0 for Shin
/// too whenever the book has no vig to attribute.</param>
public readonly record struct DeVigResult(double PTrue, double Overround, double ShinZ);

/// <summary>
/// Turns a two-way sportsbook price into a fair probability.
///
/// <para>Both methods are computed on every signal and BOTH are logged. This is not indecision: P_true
/// <i>is</i> the edge, so a bias in the de-vig is not a rounding error, it is the strategy being wrong in a
/// consistent direction — and one that settlement data alone cannot separate from a bad oracle. Logging the
/// pair makes the disagreement between them a free, continuous measure of how much the choice contributes.
/// Pick the one to trade on from M1's calibration report, not from here.</para>
///
/// <para>Everything here is a pure function of two numbers, which is the whole reason it lives in its own
/// file: it is the one part of the bot that can be checked completely offline (see <see cref="SelfTest"/>).</para>
/// </summary>
public static class DeVig
{
    /// <summary>Booked (vig-inclusive) probability implied by decimal odds. 0 for a nonsense quote.</summary>
    public static double Implied(double decimalOdds) => decimalOdds > 1.0 ? 1.0 / decimalOdds : 0.0;

    /// <summary>True once both legs are quotable. A one-sided book cannot be de-vigged at all — there is
    /// no S to divide by — so the caller must skip rather than invent the other side.</summary>
    public static bool Quotable(double oddsA, double oddsB) => oddsA > 1.0 && oddsB > 1.0;

    /// <summary>
    /// Proportional (a.k.a. multiplicative) de-vig: scale both booked probabilities down by S so they sum
    /// to 1. Assumes the margin is spread evenly in proportion to price.
    /// </summary>
    public static DeVigResult Proportional(double oddsA, double oddsB)
    {
        if (!Quotable(oddsA, oddsB)) return new DeVigResult(0, 0, 0);
        double qa = Implied(oddsA), qb = Implied(oddsB), s = qa + qb;
        return new DeVigResult(qa / s, s - 1.0, 0.0);
    }

    /// <summary>
    /// Shin de-vig: models the margin as the book's defence against insider order flow and attributes more
    /// of it to the longshot, so it does NOT scale the two legs by the same factor.
    ///
    /// <para>pi_i = [ sqrt(z^2 + 4(1-z) q_i^2 / S) - z ] / (2(1-z)), with z chosen so the pi sum to 1.
    /// Solved by bisection rather than the two-outcome closed form: a hundred iterations cost nothing at
    /// this call rate, and a bisection is obviously right by inspection where the closed form is a
    /// transcription risk on a number that silently becomes the edge.</para>
    ///
    /// <para>With no margin to attribute (S &lt;= 1) Shin degenerates to z = 0, where the formula collapses
    /// to the proportional answer — so this returns proportional directly rather than bisecting toward a
    /// negative z. S &lt; 1 means the book itself is crossed, which is a data fault worth seeing in the log,
    /// not a probability to be clever about.</para>
    /// </summary>
    public static DeVigResult Shin(double oddsA, double oddsB)
    {
        if (!Quotable(oddsA, oddsB)) return new DeVigResult(0, 0, 0);
        double qa = Implied(oddsA), qb = Implied(oddsB), s = qa + qb;
        if (s <= 1.0) return new DeVigResult(qa / s, s - 1.0, 0.0);

        // g(z) = sum(pi_i(z)) - 1. g(0) = sqrt(S) - 1 > 0 here, and g decreases in z, so the root is bracketed.
        double G(double z)
        {
            if (z <= 0) return Math.Sqrt(s) - 1.0;
            double d = 2.0 * (1.0 - z);
            double PiOf(double q) => (Math.Sqrt(z * z + 4.0 * (1.0 - z) * q * q / s) - z) / d;
            return PiOf(qa) + PiOf(qb) - 1.0;
        }

        double lo = 0.0, hi = 0.999;
        if (G(hi) > 0) return new DeVigResult(qa / s, s - 1.0, 0.0);   // unreachable for sane books; degrade, never throw
        for (int i = 0; i < 100; i++)
        {
            double mid = 0.5 * (lo + hi);
            if (G(mid) > 0) lo = mid; else hi = mid;
        }
        double zStar = 0.5 * (lo + hi);
        double den   = 2.0 * (1.0 - zStar);
        double piA   = (Math.Sqrt(zStar * zStar + 4.0 * (1.0 - zStar) * qa * qa / s) - zStar) / den;
        return new DeVigResult(piA, s - 1.0, zStar);
    }
}
