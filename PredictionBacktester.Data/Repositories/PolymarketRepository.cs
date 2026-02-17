using PredictionBacktester.Core.Entities;
using PredictionBacktester.Core.Entities.Database;
using PredictionBacktester.Data.Database;

namespace PredictionBacktester.Data.Repositories;

public class PolymarketRepository
{
    private readonly PolymarketDbContext _dbContext;

    public PolymarketRepository(PolymarketDbContext dbContext)
    {
        _dbContext = dbContext;
    }

    /// <summary>
    /// Saves the market and its outcomes to the database if they don't already exist.
    /// </summary>
    public async Task<bool> SaveMarketAsync(PolymarketMarketResponse apiMarket)
    {
        // Check if we already saved this market to avoid duplicate key errors
        if (_dbContext.Markets.Any(m => m.MarketId == apiMarket.ConditionId))
        {
            return false; // <-- Return false so the main loop knows we skipped it!
        }

        var dbMarket = new Market
        {
            MarketId = apiMarket.ConditionId,
            Title = apiMarket.Question,
            EndDate = apiMarket.EndDate,
            IsClosed = apiMarket.IsClosed
        };

        // 3. Map the Outcomes
        if (apiMarket.Outcomes != null && apiMarket.ClobTokenIds != null)
        {
            for (int i = 0; i < apiMarket.Outcomes.Length; i++)
            {
                // Sometimes arrays don't match perfectly in weird API responses, so we check bounds
                if (i < apiMarket.ClobTokenIds.Length)
                {
                    dbMarket.Outcomes.Add(new Outcome
                    {
                        OutcomeId = apiMarket.ClobTokenIds[i],
                        MarketId = apiMarket.ConditionId,
                        OutcomeName = apiMarket.Outcomes[i]
                    });
                }
            }
        }

        _dbContext.Markets.Add(dbMarket);
        await _dbContext.SaveChangesAsync();

        return true;
    }

    /// <summary>
    /// Saves a massive list of raw trades efficiently.
    /// </summary>
    public async Task SaveTradesAsync(List<PolymarketTradeResponse> apiTrades)
    {
        if (apiTrades == null || !apiTrades.Any()) return;

        var dbTrades = new List<Trade>();

        foreach (var t in apiTrades)
        {
            dbTrades.Add(new Trade
            {
                OutcomeId = t.AssetId,
                ProxyWallet = t.ProxyWallet,
                Side = t.Side,
                Price = t.Price,
                Size = t.Size,
                Timestamp = t.Timestamp,
                TransactionHash = t.TransactionHash
            });
        }

        // PRO-TIP: AddRange is massively faster than Add in a loop because it 
        // tracks the whole batch in memory and executes a single SQL transaction.
        _dbContext.Trades.AddRange(dbTrades);
        await _dbContext.SaveChangesAsync();
    }
}