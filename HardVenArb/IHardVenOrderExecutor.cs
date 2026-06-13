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
}
