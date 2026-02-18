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

    public SimulatedBroker(decimal startingCash)
    {
        CashBalance = startingCash;
        PeakEquity = startingCash;
        PositionShares = 0;
        TotalTradesExecuted = 0;
    }

    public void Buy(decimal currentPrice, decimal dollarsToInvest)
    {
        if (CashBalance < dollarsToInvest) return;

        decimal sharesBought = dollarsToInvest / currentPrice;

        // Calculate new average entry price (in case we scale in)
        decimal totalCost = (PositionShares * AverageEntryPrice) + dollarsToInvest;
        PositionShares += sharesBought;
        AverageEntryPrice = totalCost / PositionShares;

        CashBalance -= dollarsToInvest;
    }

    public void SellAll(decimal currentPrice)
    {
        if (PositionShares <= 0) return;

        decimal cashReceived = PositionShares * currentPrice;

        // Did we win or lose?
        if (currentPrice > AverageEntryPrice) WinningTrades++;
        else LosingTrades++;

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