namespace PredictionBacktester.Engine.LiveExecution;

/// <summary>
/// Abstracts Polymarket order placement so CrossArbExecutor can work with either the
/// live PolymarketOrderClient or a SimulatedPolymarketClient in dry-run mode.
/// </summary>
public interface IPolymarketOrderExecutor
{
    /// <summary>
    /// Submits a FAK order. Returns the raw JSON response string.
    /// Returns empty string on failure.
    /// </summary>
    Task<string> SubmitOrderAsync(
        string tokenId, decimal price, decimal size, int side,
        bool negRisk = false, string tickSize = "0.01", int feeRateBps = 0);

    /// <summary>Fetches the current state of an order by its ID.</summary>
    Task<string> GetOrderAsync(string orderId);

    /// <summary>Returns the on-chain conditional token balance for the proxy wallet.</summary>
    Task<decimal> GetTokenBalanceAsync(string tokenId);

    /// <summary>Returns the deposited USDC collateral balance.</summary>
    Task<decimal> GetUsdcBalanceAsync();

    /// <summary>
    /// Forces the CLOB to refresh its cached balance for a conditional token.
    /// Call best-effort after a buy fill and before a sell.
    /// </summary>
    Task UpdateBalanceAllowanceAsync(string tokenId);

    /// <summary>
    /// Fetches the taker fee rate in basis points for a specific token.
    /// Returns 0 for fee-free markets or on failure.
    /// </summary>
    Task<int> GetTakerFeeAsync(string tokenId);
}
