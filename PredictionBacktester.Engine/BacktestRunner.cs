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

        // Start with $1,000 of fake money
        var broker = new SimulatedBroker(1000m);

        Console.WriteLine($"Starting Balance: ${broker.CashBalance}");

        foreach (var tick in trades)
        {
            // --- OUR DUMB STRATEGY ---

            // If we have no shares and the price is cheap, BUY $100 worth!
            if (broker.PositionShares == 0 && tick.Price < 0.40m)
            {
                broker.Buy(tick.Price, 100m);
                Console.WriteLine($"[BUY] Bought shares at ${tick.Price}. Remaining Cash: ${broker.CashBalance:F2}");
            }

            // If we hold shares and the price is high, SELL EVERYTHING!
            else if (broker.PositionShares > 0 && tick.Price > 0.60m)
            {
                broker.SellAll(tick.Price);
                Console.WriteLine($"[SELL] Sold shares at ${tick.Price}. New Cash: ${broker.CashBalance:F2}");
            }
        }

        // 4. THE RESULTS
        decimal finalPrice = trades.Last().Price;
        decimal finalPortfolioValue = broker.GetTotalPortfolioValue(finalPrice);

        Console.WriteLine($"\n--- BACKTEST COMPLETE ---");
        Console.WriteLine($"Total Trades Executed: {broker.TotalTradesExecuted}");
        Console.WriteLine($"Ending Portfolio Value: ${finalPortfolioValue:F2}");
        Console.WriteLine($"Total Return: {((finalPortfolioValue - 1000m) / 1000m) * 100m:F2}%");
        Console.WriteLine($"--- BACKTEST COMPLETE ---");
    }
}