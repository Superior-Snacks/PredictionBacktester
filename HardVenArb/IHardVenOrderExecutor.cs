namespace HardVenArb;

/// <summary>
/// Abstracts the second-venue ("HardVen" — a rotating betting site) order surface so
/// CrossArbExecutor can work against either a live <c>HardVenOrderClient</c> or a future
/// simulated client. The shape mirrors the Kalshi↔Polymarket bot's venue interface exactly so
/// the executor is venue-agnostic; only the implementation changes per betting site.
///
/// NOTE: the parameter names (<c>tokenId</c>, <c>negRisk</c>, <c>tickSize</c>) are carried over
/// from the Polymarket shape and may be re-interpreted when the real venue is built — they are
/// kept here only so the executor's positional calls line up. Reshape freely when implementing.
/// </summary>
public interface IHardVenOrderExecutor
{
    /// <summary>Submits a FAK order. Returns the raw JSON response string; empty on failure.</summary>
    Task<string> SubmitOrderAsync(
        string tokenId, decimal price, decimal size, int side,
        bool negRisk = false, string tickSize = "0.01", int feeRateBps = 0);

    /// <summary>Same as <see cref="SubmitOrderAsync"/>, but places an EXACT account-currency stake.
    ///
    /// <para><b>Why this exists.</b> StakeLadder deliberately snaps the book stake to a round rung (€5, €10,
    /// €50) because a bettor staking €4.62 is a trivially detectable bot signature. But the executor works in
    /// USD-payout contracts, so the rung was converted to contracts and the client then converted BACK — and
    /// the two floorings destroyed the round number (a €5 rung reached the slip as €4.62). Passing the rung
    /// through preserves it: the BOOK side is a human round number and the KALSHI side absorbs the remainder,
    /// which is correct anyway — Kalshi is an API nobody eyeballs, and its integer contract count is where a
    /// non-round quantity belongs.</para>
    ///
    /// <para>Default implementation ignores the stake and falls back, so simulated/legacy clients need no
    /// change.</para></summary>
    Task<string> SubmitOrderWithStakeAsync(
        string tokenId, decimal price, decimal size, int side, decimal stakeAccount,
        bool negRisk = false, string tickSize = "0.01", int feeRateBps = 0)
        => SubmitOrderAsync(tokenId, price, size, side, negRisk, tickSize, feeRateBps);

    /// <summary>Fetches the current state of an order by its ID.</summary>
    Task<string> GetOrderAsync(string orderId);

    /// <summary>Returns the venue position/token balance for a specific market leg.</summary>
    Task<decimal> GetTokenBalanceAsync(string tokenId);

    /// <summary>Returns the venue cash (collateral) balance.</summary>
    Task<decimal> GetUsdcBalanceAsync();

    /// <summary>Forces the venue to refresh its cached balance for a leg. Best-effort after a buy, before a sell.</summary>
    Task UpdateBalanceAllowanceAsync(string tokenId);

    /// <summary>Fetches the taker fee rate in basis points for a market. 0 for fee-free / on failure.</summary>
    Task<int> GetTakerFeeAsync(string tokenId);

    /// <summary>Fetches fee-curve params (r, e) for the formula fee = r × (p×(1-p))^e per share. (0.03, 1.0) on failure.</summary>
    Task<(decimal R, double E)> GetFeeParamsAsync(string tokenId);

    /// <summary>Fetches the market tick-size string (e.g. "0.01"). "0.01" on failure.</summary>
    Task<string> GetTickSizeAsync(string tokenId);

    /// <summary>How did the venue leg on <paramref name="tokenId"/> actually FINISH — win, loss, or
    /// <b>void</b>? Called when a position settles so the bot books the real outcome instead of assuming the
    /// arb resolved symmetrically. A void (e.g. a tennis retirement refunds the sportsbook bet while the
    /// exchange still settles) is otherwise invisible and silently misprices the trade.
    ///
    /// <para><paramref name="sinceUtcIso"/> is the position's entry time, used to pick the right bet.
    /// Returns null when unavailable — the caller then logs the settlement without an outcome, as before.
    /// Default implementation returns null so simulated/legacy clients need no change.</para></summary>
    Task<string?> FindVenueBetAsync(string tokenId, string sinceUtcIso) => Task.FromResult<string?>(null);
}
