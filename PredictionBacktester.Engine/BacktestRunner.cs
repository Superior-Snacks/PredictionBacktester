using PredictionBacktester.Core.Entities.Database;
using PredictionBacktester.Data.Database;
using Microsoft.EntityFrameworkCore;

namespace PredictionBacktester.Engine;

public class BacktestRunner
{
    private readonly PolymarketDbContext _dbContext;

    public BacktestRunner(PolymarketDbContext dbContext)
    {
        _dbContext = dbContext;
    }

    /// <summary>
    /// Runs a simulation on a specific market.
    /// </summary>
    public async Task RunMarketSimulationAsync(string marketId)
    {
        Console.WriteLine($"\n--- STARTING BACKTEST FOR MARKET: {marketId} ---");

        // 1. Fetch the Market and Outcomes so we know what we are trading
        var market = await _dbContext.Markets
            .Include(m => m.Outcomes)
            .FirstOrDefaultAsync(m => m.MarketId == marketId);

        if (market == null)
        {
            Console.WriteLine("Market not found in database.");
            return;
        }

        Console.WriteLine($"Question: {market.Title}");

        // 2. Load ALL trades for this market into RAM, ordered by Time (Oldest to Newest)
        // We use AsNoTracking() because we are just reading the data, which makes it massively faster.
        var trades = await _dbContext.Trades
            .AsNoTracking()
            .Where(t => t.OutcomeId == market.Outcomes.First().OutcomeId) // Let's just track the "Yes" outcome for now
            .OrderBy(t => t.Timestamp)
            .ToListAsync();

        Console.WriteLine($"Loaded {trades.Count} historical ticks into memory.");

        // 3. THE TIME MACHINE LOOP
        foreach (var tick in trades)
        {
            // Here is where we will eventually pass the tick to your Strategy!
            // Example: _strategy.OnTick(tick);

            // For now, let's just prove the time machine works by printing every 100th trade
            if (trades.IndexOf(tick) % 100 == 0)
            {
                var date = DateTimeOffset.FromUnixTimeSeconds(tick.Timestamp).DateTime;
                Console.WriteLine($"[Time: {date}] Price moved to ${tick.Price} (Volume: {tick.Size})");
            }
        }

        Console.WriteLine($"--- BACKTEST COMPLETE ---");
    }
}