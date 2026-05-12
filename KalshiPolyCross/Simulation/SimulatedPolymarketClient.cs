using System.Collections.Concurrent;
using PredictionBacktester.Engine;
using PredictionBacktester.Engine.LiveExecution;

namespace KalshiPolyCross;

/// <summary>
/// Dry-run implementation of IPolymarketOrderExecutor. Applies SimulatedFillProfile to
/// produce realistic fill outcomes (latency, slippage, partial fills, leg failures)
/// without sending any real orders.
///
/// Response JSON mirrors the live Polymarket CLOB format so PlacePolyLegAsync and
/// PlacePolySellAsync parse it correctly without modification:
///   BUY:  { "success":true, "orderID":"...", "status":"matched",
///           "takingAmount":"<shares>", "makingAmount":"<usdc_spent>" }
///   SELL: { "success":true, "orderID":"...", "status":"matched",
///           "makingAmount":"<shares_sold>", "takingAmount":"<usdc_received>" }
///
/// Maintains internal token balance state so GetTokenBalanceAsync returns values
/// consistent with simulated fills (used by ReconcileTradeAsync in dry-run).
/// </summary>
public class SimulatedPolymarketClient : IPolymarketOrderExecutor
{
    private readonly SimulatedFillProfile _profile;
    private readonly ConcurrentDictionary<string, LocalOrderBook> _books;
    private readonly ConcurrentDictionary<string, decimal> _balances = new();
    private readonly ConcurrentDictionary<string, string> _completedOrders = new();

    public SimulatedPolymarketClient(
        SimulatedFillProfile profile,
        ConcurrentDictionary<string, LocalOrderBook> books)
    {
        _profile = profile;
        _books = books;
    }

    public async Task<string> SubmitOrderAsync(
        string tokenId, decimal price, decimal size, int side,
        bool negRisk = false, string tickSize = "0.01", int feeRateBps = 0)
    {
        if (_profile.FillLatencyMsPoly > 0)
            await Task.Delay(_profile.FillLatencyMsPoly);

        bool isSell = side == 1;
        decimal filled = _profile.SimulatePolyFill(size);

        decimal fillPrice;
        if (isSell)
        {
            // Sell reversal: fill at best bid from the live book, or fall back to limit × 0.95.
            // PolyFillPrice applies sell-side slippage on top of the bid.
            decimal bestBid = _books.TryGetValue($"P:{tokenId}", out var book)
                ? book.GetBestBidPrice()
                : 0m;
            decimal basePrice = bestBid > 0m ? bestBid : price * 0.95m;
            fillPrice = _profile.GetPolyFillPrice(basePrice);
        }
        else
        {
            fillPrice = _profile.GetPolyFillPrice(price);
        }

        if (filled > 0m)
        {
            _balances.AddOrUpdate(tokenId,
                isSell ? -filled : filled,
                (_, old) => old + (isSell ? -filled : filled));
        }

        string tokenShort = tokenId[..Math.Min(8, tokenId.Length)];
        string orderId = $"SIM_P_{DateTimeOffset.UtcNow.ToUnixTimeMilliseconds()}_{tokenShort}";

        string json;
        if (filled > 0m)
        {
            // BUY:  takingAmount = shares received, makingAmount = USDC spent
            // SELL: makingAmount = shares sold,     takingAmount = USDC received
            string taking = !isSell
                ? filled.ToString("0.######")
                : (filled * fillPrice).ToString("0.######");
            string making = !isSell
                ? (filled * fillPrice).ToString("0.######")
                : filled.ToString("0.######");

            json = $"{{\"success\":true,\"orderID\":\"{orderId}\",\"status\":\"matched\"," +
                   $"\"takingAmount\":\"{taking}\",\"makingAmount\":\"{making}\"}}";
        }
        else
        {
            json = $"{{\"success\":true,\"orderID\":\"{orderId}\",\"status\":\"canceled\"," +
                   $"\"takingAmount\":\"0\",\"makingAmount\":\"0\"}}";
        }

        _completedOrders[orderId] = json;
        return json;
    }

    public Task<string> GetOrderAsync(string orderId)
    {
        // PlacePolyLegAsync only polls when the POST response has no fill amounts.
        // Simulated responses always include fill amounts, so this path is rarely hit.
        if (_completedOrders.TryGetValue(orderId, out var json))
            return Task.FromResult(json);
        return Task.FromResult("{\"success\":false,\"status\":\"canceled\"}");
    }

    public Task<decimal> GetTokenBalanceAsync(string tokenId)
    {
        decimal bal = _balances.TryGetValue(tokenId, out var b) ? Math.Max(0m, b) : 0m;
        return Task.FromResult(bal);
    }

    // Executor overrides this to $1,000 immediately after calling it in dry-run mode.
    public Task<decimal> GetUsdcBalanceAsync() => Task.FromResult(1000m);
}
