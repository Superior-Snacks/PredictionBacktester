using System;
using System.Collections.Generic;
using PredictionBacktester.Core.Entities;

namespace PredictionBacktester.Engine;

public class SimulatedBroker
{
    public decimal SpreadPenalty { get; private set; } = 0.015m; // 1.5 cents per trade!
    public decimal CashBalance { get; private set; }

    // YES properties
    public decimal PositionShares { get; private set; }
    public decimal AverageEntryPrice { get; private set; }

    // NO properties
    public decimal NoPositionShares { get; private set; }
    public decimal AverageNoEntryPrice { get; private set; }

    // Metrics
    public int TotalTradesExecuted { get; private set; }
    public int WinningTrades { get; private set; }
    public int LosingTrades { get; private set; }
    public decimal PeakEquity { get; private set; }
    public decimal MaxDrawdown { get; private set; }

    // Ledger properties
    public string OutcomeId { get; private set; }
    public DateTime CurrentTime { get; set; }
    public List<ExecutedTrade> TradeLedger { get; private set; }

    public SimulatedBroker(decimal startingCash, string outcomeId = "Unknown")
    {
        CashBalance = startingCash;
        PeakEquity = startingCash;
        PositionShares = 0;
        TotalTradesExecuted = 0;

        OutcomeId = outcomeId;
        TradeLedger = new List<ExecutedTrade>();
    }

    public decimal MaxParticipationRate { get; private set; } = 0.10m;

    public virtual void Buy(decimal currentPrice, decimal dollarsToInvest, decimal availableVolumeShares)
    {
        if (dollarsToInvest <= 0.01m || CashBalance < dollarsToInvest) return;

        // REALITY CHECK: Apply the penalty
        decimal executionPrice = Math.Min(currentPrice + SpreadPenalty, 0.99m);

        decimal desiredShares = dollarsToInvest / executionPrice;
        decimal maxAllowedShares = availableVolumeShares * MaxParticipationRate;
        decimal actualSharesBought = Math.Min(desiredShares, maxAllowedShares);

        if (actualSharesBought <= 0) return;

        // FIX: Charge the actual execution price!
        decimal actualDollarsSpent = actualSharesBought * executionPrice;
        decimal totalCost = (PositionShares * AverageEntryPrice) + actualDollarsSpent;

        PositionShares += actualSharesBought;
        AverageEntryPrice = totalCost / PositionShares;
        CashBalance -= actualDollarsSpent;

        // FIX: Log the execution price!
        TradeLedger.Add(new ExecutedTrade { OutcomeId = OutcomeId, Date = CurrentTime, Side = "BUY", Price = executionPrice, Shares = actualSharesBought, DollarValue = actualDollarsSpent });
    }

    public virtual void SellAll(decimal currentPrice, decimal availableVolumeShares)
    {
        if (PositionShares <= 0) return;

        decimal maxAllowedSharesToSell = availableVolumeShares * MaxParticipationRate;
        decimal sharesToSell = Math.Min(PositionShares, maxAllowedSharesToSell);

        if (sharesToSell <= 0) return;

        // REALITY CHECK: Apply the penalty
        decimal executionPrice = Math.Max(currentPrice - SpreadPenalty, 0.01m);
        decimal cashReceived = sharesToSell * executionPrice;

        // FIX: Check if we won AFTER the fee
        if (executionPrice > AverageEntryPrice) WinningTrades++;
        else LosingTrades++;

        // FIX: Log the execution price!
        TradeLedger.Add(new ExecutedTrade { OutcomeId = OutcomeId, Date = CurrentTime, Side = "SELL", Price = executionPrice, Shares = sharesToSell, DollarValue = cashReceived });

        CashBalance += cashReceived;
        PositionShares -= sharesToSell;

        if (PositionShares == 0) AverageEntryPrice = 0;
        TotalTradesExecuted++;
    }

    public virtual void BuyNo(decimal currentYesPrice, decimal dollarsToInvest, decimal availableVolumeShares)
    {
        decimal currentNoPrice = 1.00m - currentYesPrice;

        // FIX: Apply the penalty to the NO price!
        decimal executionPrice = Math.Min(currentNoPrice + SpreadPenalty, 0.99m);

        if (executionPrice <= 0.00m || dollarsToInvest <= 0.01m || CashBalance < dollarsToInvest) return;

        decimal desiredShares = dollarsToInvest / executionPrice;
        decimal maxAllowedShares = availableVolumeShares * MaxParticipationRate;
        decimal actualSharesBought = Math.Min(desiredShares, maxAllowedShares);

        if (actualSharesBought <= 0) return;

        // FIX: Charge the execution price!
        decimal actualDollarsSpent = actualSharesBought * executionPrice;
        decimal totalCost = (NoPositionShares * AverageNoEntryPrice) + actualDollarsSpent;

        NoPositionShares += actualSharesBought;
        AverageNoEntryPrice = totalCost / NoPositionShares;
        CashBalance -= actualDollarsSpent;

        // FIX: Log the execution price!
        TradeLedger.Add(new ExecutedTrade { OutcomeId = OutcomeId, Date = CurrentTime, Side = "BUY NO", Price = executionPrice, Shares = actualSharesBought, DollarValue = actualDollarsSpent });
    }

    public virtual void SellAllNo(decimal currentYesPrice, decimal availableVolumeShares)
    {
        if (NoPositionShares <= 0) return;

        decimal currentNoPrice = 1.00m - currentYesPrice;

        // FIX: Apply the penalty to the NO price!
        decimal executionPrice = Math.Max(currentNoPrice - SpreadPenalty, 0.01m);

        decimal maxAllowedSharesToSell = availableVolumeShares * MaxParticipationRate;
        decimal sharesToSell = Math.Min(NoPositionShares, maxAllowedSharesToSell);

        if (sharesToSell <= 0) return;

        // FIX: Calculate cash received using the penalized execution price
        decimal cashReceived = sharesToSell * executionPrice;

        // FIX: Determine win/loss based on execution price
        if (executionPrice > AverageNoEntryPrice) WinningTrades++;
        else LosingTrades++;

        // FIX: Log the execution price!
        TradeLedger.Add(new ExecutedTrade { OutcomeId = OutcomeId, Date = CurrentTime, Side = "SELL NO", Price = executionPrice, Shares = sharesToSell, DollarValue = cashReceived });

        CashBalance += cashReceived;
        NoPositionShares -= sharesToSell;

        if (NoPositionShares == 0) AverageNoEntryPrice = 0;
        TotalTradesExecuted++;
    }

    public decimal GetTotalPortfolioValue(decimal currentYesPrice)
    {
        decimal currentNoPrice = 1.00m - currentYesPrice;
        decimal yesValue = PositionShares * currentYesPrice;
        decimal noValue = NoPositionShares * currentNoPrice;

        return CashBalance + yesValue + noValue;
    }

    public void UpdateEquityCurve(decimal currentPrice)
    {
        decimal currentEquity = GetTotalPortfolioValue(currentPrice);

        if (currentEquity > PeakEquity)
        {
            PeakEquity = currentEquity;
        }

        decimal currentDrawdown = (PeakEquity - currentEquity) / PeakEquity;

        if (currentDrawdown > MaxDrawdown)
        {
            MaxDrawdown = currentDrawdown;
        }
    }
}