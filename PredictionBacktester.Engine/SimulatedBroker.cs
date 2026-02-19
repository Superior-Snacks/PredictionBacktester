using PredictionBacktester.Core.Entities.Database;

namespace PredictionBacktester.Engine;

public class SimulatedBroker
{
    public decimal CashBalance { get; private set; }
    public decimal PositionShares { get; private set; }
    public decimal AverageEntryPrice { get; private set; } // Needed for Win Rate

    // --- NEW PERFORMANCE METRICS ---
    public int TotalTradesExecuted { get; private set; }
    public int WinningTrades { get; private set; }
    public int LosingTrades { get; private set; }
    public decimal PeakEquity { get; private set; }
    public decimal MaxDrawdown { get; private set; }

    // --- NEW LEDGER PROPERTIES ---
    public string MarketId { get; private set; }
    public DateTime CurrentTime { get; set; } // The Engine will update this on every tick
    public List<ExecutedTrade> TradeLedger { get; private set; }

    public SimulatedBroker(decimal startingCash, string marketId = "Unknown")
    {
        CashBalance = startingCash;
        PeakEquity = startingCash;
        PositionShares = 0;
        TotalTradesExecuted = 0;

        MarketId = marketId;
        TradeLedger = new List<ExecutedTrade>();
    }

    public void Buy(decimal currentPrice, decimal dollarsToInvest)
    {
        // 1. SAFETY NET: Don't execute if the trade is essentially $0
        if (dollarsToInvest <= 0.01m || CashBalance < dollarsToInvest)
        {
            return;
        }

        decimal sharesBought = dollarsToInvest / currentPrice;

        decimal totalCost = (PositionShares * AverageEntryPrice) + dollarsToInvest;
        PositionShares += sharesBought;

        // 2. SAFETY NET: Prevent divide-by-zero crashes
        if (PositionShares > 0)
        {
            AverageEntryPrice = totalCost / PositionShares;
        }

        CashBalance -= dollarsToInvest;

        // WRITE THE RECEIPT!
        TradeLedger.Add(new ExecutedTrade
        {
            MarketId = MarketId,
            Date = CurrentTime,
            Side = "BUY",
            Price = currentPrice,
            Shares = sharesBought,
            DollarValue = dollarsToInvest
        });
    }

    public void SellAll(decimal currentPrice)
    {
        if (PositionShares <= 0) return;

        decimal cashReceived = PositionShares * currentPrice;

        // Did we win or lose?
        if (currentPrice > AverageEntryPrice) WinningTrades++;
        else LosingTrades++;

        // WRITE THE RECEIPT!
        TradeLedger.Add(new ExecutedTrade
        {
            MarketId = MarketId,
            Date = CurrentTime,
            Side = "SELL",
            Price = currentPrice,
            Shares = PositionShares,
            DollarValue = cashReceived
        });

        CashBalance += cashReceived;
        PositionShares = 0;
        AverageEntryPrice = 0;
        TotalTradesExecuted++;

        UpdateEquityCurve(currentPrice); // Check for new peaks or valleys!
    }

    public decimal GetTotalPortfolioValue(decimal currentPrice)
    {
        return CashBalance + (PositionShares * currentPrice);
    }

    // --- THE MAX DRAWDOWN CALCULATOR ---
    public void UpdateEquityCurve(decimal currentPrice)
    {
        decimal currentEquity = GetTotalPortfolioValue(currentPrice);

        // 1. Did we hit a new all-time high?
        if (currentEquity > PeakEquity)
        {
            PeakEquity = currentEquity;
        }

        // 2. How far are we currently down from our all-time high?
        decimal currentDrawdown = (PeakEquity - currentEquity) / PeakEquity;

        // 3. Is this the worst drop we've ever experienced?
        if (currentDrawdown > MaxDrawdown)
        {
            MaxDrawdown = currentDrawdown;
        }
    }
}