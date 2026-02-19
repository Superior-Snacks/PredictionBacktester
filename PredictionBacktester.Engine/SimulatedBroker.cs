using PredictionBacktester.Core.Entities.Database;

namespace PredictionBacktester.Engine;

public class SimulatedBroker
{
    public decimal CashBalance { get; private set; }
    // Existing YES properties
    public decimal PositionShares { get; private set; }
    public decimal AverageEntryPrice { get; private set; }

    // --- NEW: NO SIDE PROPERTIES ---
    public decimal NoPositionShares { get; private set; }
    public decimal AverageNoEntryPrice { get; private set; }

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

    // We strictly limit our algorithm to 10% of the historical volume
    public decimal MaxParticipationRate { get; private set; } = 0.10m;

    public void Buy(decimal currentPrice, decimal dollarsToInvest, decimal availableVolumeShares)
    {
        if (dollarsToInvest <= 0.01m || CashBalance < dollarsToInvest) return;

        // 1. How many shares do we WANT?
        decimal desiredShares = dollarsToInvest / currentPrice;

        // 2. How many shares are we ALLOWED to take?
        decimal maxAllowedShares = availableVolumeShares * MaxParticipationRate;

        // 3. The Reality Check: Take the smaller of the two!
        decimal actualSharesBought = Math.Min(desiredShares, maxAllowedShares);

        if (actualSharesBought <= 0) return;

        // 4. Recalculate the actual dollars spent based on our limited fill
        decimal actualDollarsSpent = actualSharesBought * currentPrice;
        decimal totalCost = (PositionShares * AverageEntryPrice) + actualDollarsSpent;

        PositionShares += actualSharesBought;
        AverageEntryPrice = totalCost / PositionShares;
        CashBalance -= actualDollarsSpent;

        TradeLedger.Add(new ExecutedTrade
        {
            MarketId = MarketId,
            Date = CurrentTime,
            Side = "BUY",
            Price = currentPrice,
            Shares = actualSharesBought,
            DollarValue = actualDollarsSpent
        });
    }

    public void SellAll(decimal currentPrice, decimal availableVolumeShares)
    {
        if (PositionShares <= 0) return;

        // The Reality Check: We might not be able to dump our whole bag at once!
        decimal maxAllowedSharesToSell = availableVolumeShares * MaxParticipationRate;
        decimal sharesToSell = Math.Min(PositionShares, maxAllowedSharesToSell);

        if (sharesToSell <= 0) return;

        decimal cashReceived = sharesToSell * currentPrice;

        if (currentPrice > AverageEntryPrice) WinningTrades++;
        else LosingTrades++;

        TradeLedger.Add(new ExecutedTrade
        {
            MarketId = MarketId,
            Date = CurrentTime,
            Side = "SELL",
            Price = currentPrice,
            Shares = sharesToSell,
            DollarValue = cashReceived
        });

        CashBalance += cashReceived;
        PositionShares -= sharesToSell; // Subtract what we sold, keep the rest!

        if (PositionShares == 0) AverageEntryPrice = 0;
        TotalTradesExecuted++;
    }

    // --- NEW: THE "NO" SIDE EXECUTORS ---
    public void BuyNo(decimal currentYesPrice, decimal dollarsToInvest, decimal availableVolumeShares)
    {
        // MATHEMATICAL INVERSION: If YES is $0.80, NO is $0.20!
        decimal currentNoPrice = 1.00m - currentYesPrice;

        // Safety checks: We can't buy at $0.00, and don't buy if we're broke
        if (currentNoPrice <= 0.00m || dollarsToInvest <= 0.01m || CashBalance < dollarsToInvest) return;

        decimal desiredShares = dollarsToInvest / currentNoPrice;
        decimal maxAllowedShares = availableVolumeShares * MaxParticipationRate;
        decimal actualSharesBought = Math.Min(desiredShares, maxAllowedShares);

        if (actualSharesBought <= 0) return;

        decimal actualDollarsSpent = actualSharesBought * currentNoPrice;
        decimal totalCost = (NoPositionShares * AverageNoEntryPrice) + actualDollarsSpent;

        NoPositionShares += actualSharesBought;
        AverageNoEntryPrice = totalCost / NoPositionShares;
        CashBalance -= actualDollarsSpent;

        TradeLedger.Add(new ExecutedTrade { MarketId = MarketId, Date = CurrentTime, Side = "BUY NO", Price = currentNoPrice, Shares = actualSharesBought, DollarValue = actualDollarsSpent });
    }

    public void SellAllNo(decimal currentYesPrice, decimal availableVolumeShares)
    {
        if (NoPositionShares <= 0) return;

        decimal currentNoPrice = 1.00m - currentYesPrice;
        decimal maxAllowedSharesToSell = availableVolumeShares * MaxParticipationRate;
        decimal sharesToSell = Math.Min(NoPositionShares, maxAllowedSharesToSell);

        if (sharesToSell <= 0) return;

        decimal cashReceived = sharesToSell * currentNoPrice;

        if (currentNoPrice > AverageNoEntryPrice) WinningTrades++;
        else LosingTrades++;

        TradeLedger.Add(new ExecutedTrade { MarketId = MarketId, Date = CurrentTime, Side = "SELL NO", Price = currentNoPrice, Shares = sharesToSell, DollarValue = cashReceived });

        CashBalance += cashReceived;
        NoPositionShares -= sharesToSell;

        if (NoPositionShares == 0) AverageNoEntryPrice = 0;
        TotalTradesExecuted++;
    }

    // --- UPDATE YOUR PORTFOLIO CALCULATOR ---
    public decimal GetTotalPortfolioValue(decimal currentYesPrice)
    {
        decimal currentNoPrice = 1.00m - currentYesPrice;
        decimal yesValue = PositionShares * currentYesPrice;
        decimal noValue = NoPositionShares * currentNoPrice;

        return CashBalance + yesValue + noValue;
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