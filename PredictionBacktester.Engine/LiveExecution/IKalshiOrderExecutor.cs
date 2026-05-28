namespace PredictionBacktester.Engine.LiveExecution;

/// <summary>
/// Abstracts Kalshi order placement so CrossArbExecutor can work with either the
/// live KalshiOrderClient or a SimulatedKalshiClient in dry-run mode.
/// </summary>
public interface IKalshiOrderExecutor
{
    /// <summary>
    /// Places an IOC order. Returns (orderId, status, fillCount).
    /// Returns ("", "error", 0) on transient failure.
    /// </summary>
    Task<(string OrderId, string Status, decimal FillCount)> PlaceOrderAsync(
        string ticker, string side, int priceCents, int count,
        string action = "buy", string? clientOrderId = null);

    /// <summary>Polls a single order for its current status and fill count.</summary>
    Task<(string Status, decimal FillCount)> PollOrderAsync(string orderId);

    /// <summary>Returns all open market positions as (Ticker, Position) pairs.</summary>
    Task<List<(string Ticker, int Position)>> GetPositionsAsync();

    /// <summary>Returns the current account balance in cents.</summary>
    Task<long> GetBalanceCentsAsync();

    /// <summary>Returns the full market document for a given ticker. Throws on 404.</summary>
    Task<System.Text.Json.JsonDocument> GetMarketAsync(string ticker);
}
