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
    public async Task RunMarketSimulationAsync(string marketId, DateTime startDate, DateTime endDate, IStrategy strategy, decimal initialCapital = 1000m)
    {
        Console.WriteLine($"\n--- STARTING SINGLE MARKET SIMULATION ---");
        Console.WriteLine($"Market: {marketId}");
        Console.WriteLine($"Strategy: {strategy.GetType().Name}\n");

        long startUnix = ((DateTimeOffset)startDate).ToUnixTimeSeconds();
        long endUnix = ((DateTimeOffset)endDate).ToUnixTimeSeconds();

        // Note: Make sure your Single Market query joins Outcomes if you need ConditionIds, 
        // or just queries the Trades table directly if you are using the raw MarketId.
        var trades = await _dbContext.Trades
            .Where(t => t.MarketId == marketId && t.Timestamp >= startUnix && t.Timestamp <= endUnix)
            .OrderBy(t => t.Timestamp)
            .ToListAsync();

        if (trades.Count == 0)
        {
            Console.WriteLine("No trades found for this market in the specified date range.");
            return;
        }

        // Give the microscope a clean $1,000 wallet
        var broker = new SimulatedBroker(initialCapital, marketId); 
        Candle currentCandle = null;
        long candleDurationSeconds = strategy is ICandleStrategy cStrat ? (long)cStrat.Timeframe.TotalSeconds : 0;
        decimal finalTickPrice = 0;

        foreach (var tick in trades)
        {
            finalTickPrice = tick.Price;

            // Sync the clock so the ledger prints the correct dates!
            broker.CurrentTime = DateTimeOffset.FromUnixTimeSeconds(tick.Timestamp).DateTime;

            if (strategy is ITickStrategy tickStrategy)
            {
                tickStrategy.OnTick(tick, broker);
            }
            else if (strategy is ICandleStrategy candleStrategy)
            {
                if (currentCandle == null || tick.Timestamp >= currentCandle.OpenTimestamp + candleDurationSeconds)
                {
                    if (currentCandle != null) candleStrategy.OnCandle(currentCandle, broker);
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
            finalStrat.OnCandle(currentCandle, broker);
        }

        // --- THE REALITY CHECK (FORCED LIQUIDATION) ---
        // Liquidate any leftover bags at the final known price so the scorecard is honest!
        broker.SellAll(finalTickPrice, decimal.MaxValue);
        broker.SellAllNo(finalTickPrice, decimal.MaxValue);

        // --- THE MICROSCOPE LEDGER PRINT-OUT ---
        Console.WriteLine("=== CHRONOLOGICAL TRADE LEDGER ===");
        if (broker.TradeLedger.Count == 0)
        {
            Console.WriteLine("No trades executed. The strategy stayed flat.");
        }
        else
        {
            foreach (var t in broker.TradeLedger)
            {
                // Format: [2024-08-01 14:00:00] BUY YES  | Price: $0.450 | Shares: 150.50 | Value: $67.72
                Console.WriteLine($"[{t.Date:yyyy-MM-dd HH:mm:ss}] {t.Side,-8} | Price: ${t.Price:F3} | Shares: {t.Shares,8:F2} | Value: ${t.DollarValue,7:F2}");
            }
        }

        // --- THE MICROSCOPE SCORECARD ---
        // 3. UPDATE THE SCORECARD MATH
        decimal totalEndingCapital = broker.GetTotalPortfolioValue(finalTickPrice);
        decimal totalReturn = ((totalEndingCapital - initialCapital) / initialCapital) * 100m;

        Console.WriteLine($"\n=========================================");
        Console.WriteLine($"          MARKET SCORECARD               ");
        Console.WriteLine($"=========================================");
        Console.WriteLine($"Initial Capital:      ${initialCapital:F2}"); // Dynamic!
        Console.WriteLine($"Ending Capital:       ${totalEndingCapital:F2}");
        Console.WriteLine($"Net Profit:           ${totalEndingCapital - initialCapital:F2}"); // Dynamic!
        Console.WriteLine($"Return:               {totalReturn:F2}%");
    }

    public async Task<PortfolioResult> RunPortfolioSimulationAsync(
        List<string> marketIds, 
        DateTime startDate, 
        DateTime endDate, 
        IStrategy strategy, 
        bool isSilent = false, 
        decimal initialAllocationPerMarket = 1000m)
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

            // ... (End of the tick/candle loop)
            if (strategy is ICandleStrategy finalStrat && currentCandle != null)
            {
                finalStrat.OnCandle(currentCandle, localBroker);
            }

            // --- NEW: THE REALITY CHECK (FORCED LIQUIDATION) ---
            // If the market is over and we are still holding bags, force sell everything at the final price!
            localBroker.SellAll(finalTickPrice, decimal.MaxValue);
            localBroker.SellAllNo(finalTickPrice, decimal.MaxValue);

            // At the end of this market's timeline, collect the results!
            totalEndingCapital += localBroker.GetTotalPortfolioValue(finalTickPrice);
            grandTotalTrades += localBroker.TotalTradesExecuted;
            // ...

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