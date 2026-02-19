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
    public async Task<PortfolioResult> RunPortfolioSimulationAsync(List<string> marketIds, DateTime startDate, DateTime endDate, IStrategy strategy, bool isSilent = false)
    {
        if (!isSilent)
        {
            Console.WriteLine($"\n--- STARTING PORTFOLIO BACKTEST ---");
            Console.WriteLine($"Strategy: {strategy.GetType().Name}");
            Console.WriteLine($"Markets Analyzed: {marketIds.Count}\n");
        }

        long startUnix = ((DateTimeOffset)startDate).ToUnixTimeSeconds();
        long endUnix = ((DateTimeOffset)endDate).ToUnixTimeSeconds();

        var masterLedger = new List<ExecutedTrade>();

        // --- THE HEDGE FUND AGGREGATORS ---
        decimal initialAllocationPerMarket = 1000m;
        decimal totalStartingCapital = marketIds.Count * initialAllocationPerMarket;
        decimal totalEndingCapital = 0;
        int grandTotalTrades = 0;
        int grandWinningTrades = 0;
        int grandLosingTrades = 0;

        foreach (var conditionId in marketIds)
        {
            var trades = await _dbContext.Trades
                .Join(_dbContext.Outcomes, t => t.OutcomeId, o => o.OutcomeId, (t, o) => new { t, o.MarketId })
                .Where(x => x.MarketId == conditionId && x.t.Timestamp >= startUnix && x.t.Timestamp <= endUnix)
                .Select(x => x.t)
                .OrderBy(t => t.Timestamp)
                .ToListAsync();

            if (trades.Count == 0) continue;

            // Give THIS specific market its own isolated $1,000 broker!
            var localBroker = new SimulatedBroker(initialAllocationPerMarket, conditionId); 
            Candle currentCandle = null;
            long candleDurationSeconds = strategy is ICandleStrategy cStrat ? (long)cStrat.Timeframe.TotalSeconds : 0;
            decimal finalTickPrice = 0;

            foreach (var tick in trades)
            {
                finalTickPrice = tick.Price;
                localBroker.CurrentTime = DateTimeOffset.FromUnixTimeSeconds(tick.Timestamp).DateTime;

                if (strategy is ITickStrategy tickStrategy)
                {
                    tickStrategy.OnTick(tick, localBroker);
                }
                else if (strategy is ICandleStrategy candleStrategy)
                {
                    if (currentCandle == null || tick.Timestamp >= currentCandle.OpenTimestamp + candleDurationSeconds)
                    {
                        if (currentCandle != null) candleStrategy.OnCandle(currentCandle, localBroker);
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

            if (strategy is ICandleStrategy finalStrat && currentCandle != null)
            {
                finalStrat.OnCandle(currentCandle, localBroker);
            }

            // At the end of this market's timeline, collect the results!
            totalEndingCapital += localBroker.GetTotalPortfolioValue(finalTickPrice);
            grandTotalTrades += localBroker.TotalTradesExecuted;
            grandWinningTrades += localBroker.WinningTrades;
            grandLosingTrades += localBroker.LosingTrades;

            masterLedger.AddRange(localBroker.TradeLedger);
        }

        // --- PRINT TRUE PORTFOLIO RESULTS ---
        decimal totalReturn = totalStartingCapital > 0 ? ((totalEndingCapital - totalStartingCapital) / totalStartingCapital) * 100m : 0;
        decimal winRate = grandTotalTrades > 0 ? ((decimal)grandWinningTrades / grandTotalTrades) * 100m : 0;

        if (!isSilent)
        {
            Console.WriteLine($"=========================================");
            Console.WriteLine($"          TRUE PORTFOLIO REPORT          ");
            Console.WriteLine($"=========================================");
            Console.WriteLine($"Total Markets Traded: {marketIds.Count}");
            Console.WriteLine($"Initial Capital:      ${totalStartingCapital:F2} ($1k per market)");
            Console.WriteLine($"Ending Capital:       ${totalEndingCapital:F2}");
            Console.WriteLine($"Total Return:         {totalReturn:F2}%");
            Console.WriteLine($"-----------------------------------------");
            Console.WriteLine($"Total Trades:         {grandTotalTrades}");
            Console.WriteLine($"Winning Trades:       {grandWinningTrades}");
            Console.WriteLine($"Losing Trades:        {grandLosingTrades}");
            Console.WriteLine($"Win Rate:             {winRate:F2}%");
            Console.WriteLine($"=========================================");
        }

        // 3. RETURN THE SCORECARD
        return new PortfolioResult
        {
            TotalReturn = totalReturn,
            WinRate = winRate,
            TotalTrades = grandTotalTrades
        };
        /*
        // Save the CSV to your Desktop!
        string desktopPath = Environment.GetFolderPath(Environment.SpecialFolder.Desktop);
        string filePath = Path.Combine(desktopPath, "Polymarket_Portfolio_Trades.csv");
        TradeExporter.ExportToCsv(masterLedger, filePath);

        Console.WriteLine($"\n[DATA SAVED] Exported {masterLedger.Count} detailed trades.");
        Console.WriteLine($"FILE PATH: {filePath}"); // <-- THIS WILL REVEAL THE HIDING SPOT

        Console.WriteLine($"\n[DATA SAVED] Exported {masterLedger.Count} detailed trades to your Desktop!");*/
    }
}