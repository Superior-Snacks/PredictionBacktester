namespace HardVenArb;

/// <summary>
/// Bet sizing for the HardVen (Pinnacle) leg. Two jobs:
///
/// <para><b>1. The ladder.</b> Real bettors stake round numbers. A bot staking €37.42 because that is exactly
/// what the arb supported is a trivially detectable signature, and the whole point of
/// <see cref="MaxDepthFraction"/> is to not look like an arber. So the stake is snapped DOWN to a rung:
/// multiples of 10 up to 100, then of 50 up to 250, then of 100 above that.</para>
///
/// <para><b>2. Distance from the book's max.</b> Repeatedly betting at or near Pinnacle's posted maximum is
/// the classic sharp/arber tell and is what gets an account stake-limited. <see cref="MaxDepthFraction"/>
/// (default 1/3, env <c>HARDVEN_MAX_DEPTH_FRACTION</c>) caps us at a fraction of what the book would accept.</para>
///
/// <para><b>Units.</b> The ladder is denominated in the BOOK'S ACCOUNT CURRENCY (Pinnacle = EUR) — that is the
/// number actually typed into the bet slip, so that is where round numbers need to land. At
/// <c>HARDVEN_FX_TO_USD=1.08</c> a `100` rung is ~$108 of risk. Everything the executor handles is in
/// USD-payout contracts, so <see cref="SizeBet"/> does the conversion in both directions.</para>
///
/// <para>Snapping is always DOWNWARD, for the same reason the stake floors in
/// <see cref="HardVenOrderClient"/>: the HardVen leg is IRREVERSIBLE, so any residue must land on the Kalshi
/// side where recovery can simply reverse it.</para>
/// </summary>
public static class StakeLadder
{
    /// <summary>Fraction of the book's accepted max we are willing to take. Default 1/3 — never bet the max.</summary>
    public static readonly decimal MaxDepthFraction = ReadFraction("HARDVEN_MAX_DEPTH_FRACTION", 1m / 3m);

    /// <summary>Smallest rung. Below this there is no bet worth placing.</summary>
    public const decimal MinRung = 10m;

    /// <summary>
    /// Snap a stake DOWN to the nearest valid rung: multiples of 10 below 100, of 50 below 300 (so 250 is a
    /// rung), of 100 above. Returns 0 when the ceiling cannot even fund the smallest rung — the caller must
    /// then skip the trade rather than place an off-ladder bet.
    /// </summary>
    public static decimal Snap(decimal maxStake)
    {
        if (maxStake < MinRung) return 0m;
        if (maxStake < 100m)    return Math.Floor(maxStake / 10m)  * 10m;    // 10,20,…,90
        if (maxStake < 300m)    return Math.Floor(maxStake / 50m)  * 50m;    // 100,150,200,250
        return                         Math.Floor(maxStake / 100m) * 100m;   // 300,400,…
    }

    /// <summary>
    /// Full sizing decision for one arb. Takes the ceilings the executor already knows (in USD-payout
    /// contracts) plus the raw HardVen book depth, applies the max-fraction rule and the ladder, and returns
    /// the contract count to trade on BOTH legs.
    /// </summary>
    /// <param name="hardvenPrice">HardVen leg ask = 1/odds (unitless).</param>
    /// <param name="fxToUsd">USD per account unit (EUR→USD ≈ 1.08).</param>
    /// <param name="contractCeiling">Max contracts allowed by maxBet + balance (already computed upstream).</param>
    /// <param name="kalshiDepth">Kalshi ask depth at our limit, in contracts — a hard ceiling.</param>
    /// <param name="hardvenDepth">HardVen ask depth in contracts (= Pinnacle max_stake × odds × fx). The
    /// fraction rule applies to THIS, not to Kalshi — Kalshi is an exchange with no account-limiting risk.</param>
    /// <returns>Contracts to trade (0 = skip), and the account-currency stake that produces them.</returns>
    public static (int Contracts, decimal StakeAccount) SizeBet(
        decimal hardvenPrice, decimal fxToUsd, int contractCeiling,
        decimal kalshiDepth, decimal hardvenDepth)
    {
        if (hardvenPrice <= 0m || fxToUsd <= 0m || contractCeiling <= 0) return (0, 0m);

        // Ceiling in contracts: the tightest of what we'll spend, what Kalshi can fill, and our
        // self-imposed fraction of what Pinnacle would accept.
        decimal hardvenAllowed = hardvenDepth * MaxDepthFraction;
        decimal ceiling = Math.Min(contractCeiling, Math.Min(kalshiDepth, hardvenAllowed));
        if (ceiling <= 0m) return (0, 0m);

        // contracts → account-currency stake, snapped down to a rung.
        decimal stakeCeiling = ceiling * hardvenPrice / fxToUsd;
        decimal stake        = Snap(stakeCeiling);
        if (stake <= 0m) return (0, 0m);

        // Rung → contracts. Floor again: the contract count must not exceed what the rung actually buys,
        // or the Kalshi leg would be oversized relative to the HardVen payout it is hedging.
        int contracts = (int)Math.Floor(stake * fxToUsd / hardvenPrice);
        if (contracts <= 0) return (0, 0m);

        // Defensive: flooring above can only reduce, but never let rounding push us back over a hard ceiling.
        if (contracts > ceiling) contracts = (int)Math.Floor(ceiling);
        return (contracts, stake);
    }

    private static decimal ReadFraction(string envVar, decimal fallback)
    {
        string? raw = Environment.GetEnvironmentVariable(envVar);
        if (decimal.TryParse(raw, System.Globalization.NumberStyles.Any,
                System.Globalization.CultureInfo.InvariantCulture, out decimal v) && v > 0m && v <= 1m)
            return v;
        return fallback;
    }
}
