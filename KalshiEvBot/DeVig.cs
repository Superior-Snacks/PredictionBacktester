namespace KalshiEvBot;

/// <summary>Output of one de-vig of a two-way book. <c>PTrue</c> is for the FIRST leg passed in.</summary>
/// <param name="PTrue">Fair probability of leg A, vig removed.</param>
/// <param name="Overround">V = S - 1, the book's margin. 0.0345 was the measured Pinnacle median.</param>
/// <param name="ShinZ">Shin's insider-trading parameter. 0 for the proportional method, and 0 for Shin
/// too whenever the book has no vig to attribute.</param>
public readonly record struct DeVigResult(double PTrue, double Overround, double ShinZ);

/// <summary>De-vig of an n-way book. <c>PTrue[i]</c> corresponds to <c>odds[i]</c> and the vector sums to 1.</summary>
public readonly record struct DeVigN(double[] PTrue, double Overround, double ShinZ)
{
    public bool Ok => PTrue.Length > 0;
}

/// <summary>
/// Turns a sportsbook price into a fair probability. Works on any number of outcomes: a tennis two-way, a
/// soccer 1X2, or anything else the venue lists as one mutually-exclusive set.
///
/// <para><b>The n-way form is the real one; the two-way methods delegate to it.</b> Keeping two separate
/// implementations would mean the tennis path and the soccer path could drift, and a de-vig bug does not
/// announce itself — <c>P_true</c> IS the edge, so an error here is the strategy being quietly wrong in a
/// consistent direction rather than anything that shows up as a crash.</para>
///
/// <para>Both methods are computed on every signal and BOTH are logged. That is not indecision: proportional
/// de-vig is known to be biased along the favourite–longshot axis, and the disagreement between the two is a
/// free running measure of how much the choice contributes. It matters MORE on a three-way, where one leg
/// can sit at 0.98 and another at 0.01 — precisely where proportional is least trustworthy.</para>
///
/// <para>Everything here is a pure function of the odds, which is why it lives in its own file: it is the
/// one part of the bot that can be checked completely offline (see <see cref="SelfTest"/>).</para>
/// </summary>
public static class DeVig
{
    /// <summary>Booked (vig-inclusive) probability implied by decimal odds. 0 for a nonsense quote.</summary>
    public static double Implied(double decimalOdds) => decimalOdds > 1.0 ? 1.0 / decimalOdds : 0.0;

    /// <summary>True once every leg is quotable. A book missing a leg cannot be de-vigged at all — there is
    /// no complete S to divide by — so the caller must skip rather than invent the rest. On a 1X2 that means
    /// a missing draw price invalidates the home and away legs too, not just the draw.</summary>
    public static bool Quotable(IReadOnlyList<double> odds)
        => odds.Count >= 2 && odds.All(o => o > 1.0);

    public static bool Quotable(double oddsA, double oddsB) => Quotable(new[] { oddsA, oddsB });

    // ── n-way ─────────────────────────────────────────────────────────────────────────────────────────
    /// <summary>
    /// Proportional (multiplicative) de-vig: scale every booked probability down by S so they sum to 1.
    /// Assumes the margin is spread in proportion to price.
    /// </summary>
    public static DeVigN ProportionalN(IReadOnlyList<double> odds)
    {
        if (!Quotable(odds)) return new DeVigN(Array.Empty<double>(), 0, 0);
        var q = odds.Select(Implied).ToArray();
        double s = q.Sum();
        return new DeVigN(q.Select(x => x / s).ToArray(), s - 1.0, 0.0);
    }

    /// <summary>
    /// Shin de-vig: models the margin as the book's defence against insider order flow, so it attributes
    /// more of it to the longshot and does NOT scale every leg by the same factor.
    ///
    /// <para>pi_i = [ sqrt(z^2 + 4(1-z) q_i^2 / S) - z ] / (2(1-z)), with z chosen so the pi sum to 1.
    /// Solved by bisection rather than a closed form: the bracket is guaranteed because g(0) = sqrt(S) - 1
    /// is positive whenever the book carries margin and g decreases in z, a hundred iterations cost nothing
    /// at this call rate, and a bisection is obviously right by inspection where a transcribed closed form
    /// is a silent risk on a number that becomes the edge. The same code serves 2 legs or 3.</para>
    ///
    /// <para>With no margin to attribute (S &lt;= 1) Shin degenerates to z = 0, where the formula collapses
    /// to the proportional answer — returned directly rather than bisecting toward a negative z. S &lt; 1
    /// means the book itself is crossed, which is a data fault worth seeing in the log, not a probability
    /// to be clever about.</para>
    /// </summary>
    public static DeVigN ShinN(IReadOnlyList<double> odds)
    {
        if (!Quotable(odds)) return new DeVigN(Array.Empty<double>(), 0, 0);
        var q = odds.Select(Implied).ToArray();
        double s = q.Sum();
        if (s <= 1.0) return new DeVigN(q.Select(x => x / s).ToArray(), s - 1.0, 0.0);

        double Pi(double qi, double z) => (Math.Sqrt(z * z + 4.0 * (1.0 - z) * qi * qi / s) - z) / (2.0 * (1.0 - z));
        double G(double z) => z <= 0 ? Math.Sqrt(s) - 1.0 : q.Sum(qi => Pi(qi, z)) - 1.0;

        double lo = 0.0, hi = 0.999;
        if (G(hi) > 0) return new DeVigN(q.Select(x => x / s).ToArray(), s - 1.0, 0.0);  // degrade, never throw
        for (int i = 0; i < 100; i++)
        {
            double mid = 0.5 * (lo + hi);
            if (G(mid) > 0) lo = mid; else hi = mid;
        }
        double zStar = 0.5 * (lo + hi);
        return new DeVigN(q.Select(qi => Pi(qi, zStar)).ToArray(), s - 1.0, zStar);
    }

    // ── two-way convenience, delegating to the n-way form ─────────────────────────────────────────────
    public static DeVigResult Proportional(double oddsA, double oddsB)
    {
        var r = ProportionalN(new[] { oddsA, oddsB });
        return r.Ok ? new DeVigResult(r.PTrue[0], r.Overround, r.ShinZ) : new DeVigResult(0, 0, 0);
    }

    public static DeVigResult Shin(double oddsA, double oddsB)
    {
        var r = ShinN(new[] { oddsA, oddsB });
        return r.Ok ? new DeVigResult(r.PTrue[0], r.Overround, r.ShinZ) : new DeVigResult(0, 0, 0);
    }
}
