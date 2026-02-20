using PredictionBacktester.Core.Entities;
using PredictionBacktester.Core.Entities.Database;
using PredictionBacktester.Data.Database;
using Microsoft.EntityFrameworkCore;

namespace PredictionBacktester.Data.Repositories;

public class PolymarketRepository
{
    private readonly PolymarketDbContext _dbContext;

    public PolymarketRepository(PolymarketDbContext dbContext)
    {
        _dbContext = dbContext;
    }

    public async Task<List<string>> GetActiveOutcomesInDateRangeAsync(DateTime startDate, DateTime endDate, string keyword = null)
    {
        long startUnix = ((DateTimeOffset)startDate).ToUnixTimeSeconds();
        long endUnix = ((DateTimeOffset)endDate).ToUnixTimeSeconds();

        var query = _dbContext.Trades
            .Join(_dbContext.Outcomes, t => t.OutcomeId, o => o.OutcomeId, (t, o) => new { t, o.MarketId })
            .Join(_dbContext.Markets, x => x.MarketId, m => m.MarketId, (x, m) => new { x.t, m.Title })
            .Where(x => x.t.Timestamp >= startUnix && x.t.Timestamp <= endUnix);

        // THE DOMAIN FILTER!
        if (!string.IsNullOrEmpty(keyword))
        {
            query = query.Where(x => EF.Functions.Like(x.Title, $"%{keyword}%"));
        }

        return await query
            .Select(x => x.t.OutcomeId)
            .Distinct()
            .ToListAsync();
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

    /// <summary>
    /// Finds markets that hit the offset limit (e.g., have exactly 3500 trades) 
    /// and returns their ConditionId and the Timestamp of their oldest trade.
    /// </summary>
    /// <summary>
    /// Joins Trades to Outcomes to find the total trades per MARKET, 
    /// returning the ConditionId and the Timestamp of the oldest trade.
    /// </summary>
    public async Task<Dictionary<string, long>> GetIncompleteMarketsAsync()
    {
        var incompleteMarkets = await _dbContext.Outcomes
            // 1. Join Outcomes to Trades using the OutcomeId
            .Join(_dbContext.Trades,
                  outcome => outcome.OutcomeId,
                  trade => trade.OutcomeId,
                  (outcome, trade) => new { outcome.MarketId, trade.Timestamp })
            // 2. Group by the Parent Market
            .GroupBy(x => x.MarketId)
            // 3. Select the aggregations
            .Select(g => new
            {
                MarketId = g.Key,
                TradeCount = g.Count(),
                OldestTimestamp = g.Min(x => x.Timestamp)
            })
            // 4. Now filter by the API limit!
            .Where(x => x.TradeCount >= 3000)
            // 5. Instantly map it to a Dictionary
            .ToDictionaryAsync(x => x.MarketId, x => x.OldestTimestamp);

        return incompleteMarkets;
    }

    /// <summary>
    /// Scans the database for any markets that had active trading volume within a specific date range.
    /// </summary>
    public async Task<List<string>> GetActiveMarketsInDateRangeAsync(DateTime startDate, DateTime endDate)
    {
        long startUnix = ((DateTimeOffset)startDate).ToUnixTimeSeconds();
        long endUnix = ((DateTimeOffset)endDate).ToUnixTimeSeconds();

        var activeMarketIds = await _dbContext.Trades
            .Where(t => t.Timestamp >= startUnix && t.Timestamp <= endUnix)
            .Select(t => t.OutcomeId)
            .Distinct() // Get only the unique Outcome IDs to speed up the join
            .Join(_dbContext.Outcomes,
                  outcomeId => outcomeId,
                  outcome => outcome.OutcomeId,
                  (outcomeId, outcome) => outcome.MarketId)
            .Distinct() // Narrow it down to just the unique parent Market IDs
            .ToListAsync();

        return activeMarketIds;
    }

    public async Task<long?> GetNewestTradeTimestampAsync(string marketId)
    {
        var outcomeIds = await _dbContext.Outcomes
            .Where(o => o.MarketId == marketId)
            .Select(o => o.OutcomeId)
            .ToListAsync();

        if (outcomeIds.Count == 0) return null;

        var newestTrade = await _dbContext.Trades
            .Where(t => outcomeIds.Contains(t.OutcomeId))
            .OrderByDescending(t => t.Timestamp)
            .Select(t => t.Timestamp)
            .FirstOrDefaultAsync();

        return newestTrade == 0 ? null : newestTrade;
    }

    /// <summary>
    /// Grabs every market in our database that is still actively trading.
    /// </summary>
    public async Task<List<string>> GetOpenMarketIdsAsync()
    {
        return await _dbContext.Markets
            .Where(m => !m.IsClosed)
            .Select(m => m.MarketId)
            .ToListAsync();
    }

    /// <summary>
    /// Flips the switch so we never query this market's API again.
    /// </summary>
    public async Task MarkMarketClosedAsync(string conditionId)
    {
        var market = await _dbContext.Markets.FirstOrDefaultAsync(m => m.MarketId == conditionId);
        if (market != null && !market.IsClosed)
        {
            market.IsClosed = true;
            await _dbContext.SaveChangesAsync();
        }
    }
}