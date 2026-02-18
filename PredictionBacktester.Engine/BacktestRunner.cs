using Microsoft.EntityFrameworkCore;
using PredictionBacktester.Core.Entities;
using PredictionBacktester.Core.Entities.Database;
using PredictionBacktester.Data.Database;

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
    public async Task RunMarketSimulationAsync(string marketId, IStrategy strategy)
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

        // --- CANDLE BUILDER STATE ---
        Candle currentCandle = null;
        long candleDurationSeconds = 0;

        // If it's a candle strategy, extract the requested timeframe in seconds
        if (strategy is ICandleStrategy cStrat)
        {
            candleDurationSeconds = (long)cStrat.Timeframe.TotalSeconds;
        }

        foreach (var tick in trades)
        {
            // ROUTE 1: If it's a Tick Strategy, feed it the raw tick immediately!
            if (strategy is ITickStrategy tickStrategy)
            {
                tickStrategy.OnTick(tick, broker);
            }

            // ROUTE 2: If it's a Candle Strategy, build the candle on the fly!
            if (strategy is ICandleStrategy candleStrategy)
            {
                // If we don't have a candle yet, or the tick has crossed into a new time window
                if (currentCandle == null || tick.Timestamp >= currentCandle.OpenTimestamp + candleDurationSeconds)
                {
                    // If we just finished building a previous candle, hand it to the strategy!
                    if (currentCandle != null)
                    {
                        candleStrategy.OnCandle(currentCandle, broker);
                    }

                    // Start a brand new candle with this tick's data
                    currentCandle = new Candle
                    {
                        OpenTimestamp = tick.Timestamp,
                        Open = tick.Price,
                        High = tick.Price,
                        Low = tick.Price,
                        Close = tick.Price,
                        Volume = tick.Size
                    };
                }
                else
                {
                    // The tick belongs to the current candle time window, so just update the High/Low/Close/Volume
                    currentCandle.High = Math.Max(currentCandle.High, tick.Price);
                    currentCandle.Low = Math.Min(currentCandle.Low, tick.Price);
                    currentCandle.Close = tick.Price;
                    currentCandle.Volume += tick.Size;
                }
            }
        }

        // Catch the final pending candle when the loop finishes!
        if (strategy is ICandleStrategy finalStrategy && currentCandle != null)
        {
            finalStrategy.OnCandle(currentCandle, broker);
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
    public async Task RunPortfolioSimulationAsync(List<string> marketIds, DateTime startDate, DateTime endDate, IStrategy strategy)
    {
        Console.WriteLine($"\n--- STARTING PORTFOLIO BACKTEST ---");
        Console.WriteLine($"Strategy: {strategy.GetType().Name}");
        Console.WriteLine($"Period: {startDate.ToShortDateString()} to {endDate.ToShortDateString()}");
        Console.WriteLine($"Markets Analyzed: {marketIds.Count}\n");

        long startUnix = ((DateTimeOffset)startDate).ToUnixTimeSeconds();
        long endUnix = ((DateTimeOffset)endDate).ToUnixTimeSeconds();

        // 1. Initialize a single Master Broker for the whole portfolio ($10,000 starting cash)
        var masterBroker = new SimulatedBroker(10000m);
        decimal finalTickPrice = 0;

        // 2. Loop through every requested market
        foreach (var conditionId in marketIds)
        {
            // Fetch ONLY the trades within our requested date range
            var trades = await _dbContext.Trades
                .Join(_dbContext.Outcomes, t => t.OutcomeId, o => o.OutcomeId, (t, o) => new { t, o.MarketId })
                .Where(x => x.MarketId == conditionId && x.t.Timestamp >= startUnix && x.t.Timestamp <= endUnix)
                .Select(x => x.t)
                .OrderBy(t => t.Timestamp)
                .ToListAsync();

            if (trades.Count == 0) continue;

            // --- THE TIME MACHINE CORE ---
            Candle currentCandle = null;
            long candleDurationSeconds = strategy is ICandleStrategy cStrat ? (long)cStrat.Timeframe.TotalSeconds : 0;

            foreach (var tick in trades)
            {
                // Update the equity curve on every single tick to catch intra-trade drawdowns!
                masterBroker.UpdateEquityCurve(tick.Price);
                finalTickPrice = tick.Price;

                if (strategy is ITickStrategy tickStrategy)
                {
                    tickStrategy.OnTick(tick, masterBroker);
                }
                else if (strategy is ICandleStrategy candleStrategy)
                {
                    if (currentCandle == null || tick.Timestamp >= currentCandle.OpenTimestamp + candleDurationSeconds)
                    {
                        if (currentCandle != null) candleStrategy.OnCandle(currentCandle, masterBroker);
                        currentCandle = new Candle { OpenTimestamp = tick.Timestamp, Open = tick.Price, High = tick.Price, Low = tick.Price, Close = tick.Price, Volume = tick.Size };
                    }
                    else
                    {
                        currentCandle.High = Math.Max(currentCandle.High, tick.Price);
                        currentCandle.Low = Math.Min(currentCandle.Low, tick.Price);
                        currentCandle.Close = tick.Price;
                        currentCandle.Volume += tick.Size;
                    }
                }
            }

            // Close out the final candle for this market
            if (strategy is ICandleStrategy finalStrat && currentCandle != null)
            {
                finalStrat.OnCandle(currentCandle, masterBroker);
            }
        }

        // 3. GENERATE THE DETAILED REPORT
        decimal finalPortfolioValue = masterBroker.GetTotalPortfolioValue(finalTickPrice);
        decimal totalReturn = ((finalPortfolioValue - 10000m) / 10000m) * 100m;

        // Prevent division by zero if no trades happened
        decimal winRate = masterBroker.TotalTradesExecuted > 0
            ? ((decimal)masterBroker.WinningTrades / masterBroker.TotalTradesExecuted) * 100m
            : 0;

        Console.WriteLine($"=========================================");
        Console.WriteLine($"          PORTFOLIO REPORT               ");
        Console.WriteLine($"=========================================");
        Console.WriteLine($"Initial Capital:   $10,000.00");
        Console.WriteLine($"Ending Capital:    ${finalPortfolioValue:F2}");
        Console.WriteLine($"Total Return:      {totalReturn:F2}%");
        Console.WriteLine($"Peak Equity:       ${masterBroker.PeakEquity:F2}");
        Console.WriteLine($"Max Drawdown:      -{masterBroker.MaxDrawdown * 100m:F2}%");
        Console.WriteLine($"-----------------------------------------");
        Console.WriteLine($"Total Trades:      {masterBroker.TotalTradesExecuted}");
        Console.WriteLine($"Winning Trades:    {masterBroker.WinningTrades}");
        Console.WriteLine($"Losing Trades:     {masterBroker.LosingTrades}");
        Console.WriteLine($"Win Rate:          {winRate:F2}%");
        Console.WriteLine($"=========================================");
    }
}