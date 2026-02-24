using System;
using System.Collections.Generic;
using PredictionBacktester.Core.Entities;

namespace PredictionBacktester.Engine;

public class SimulatedBroker
{
    public decimal SpreadPenalty { get; private set; } = 0.015m;
    public decimal CashBalance { get; protected set; }

    // Multi-asset properties
    private Dictionary<string, decimal> _positionShares = new();
    private Dictionary<string, decimal> _averageEntryPrices = new();
    private Dictionary<string, decimal> _noPositionShares = new();
    private Dictionary<string, decimal> _averageNoEntryPrices = new();
    private Dictionary<string, decimal> _lastKnownPrices = new(); // Tracks latest price for accurate equity

    // Metrics
    public int TotalTradesExecuted { get; protected set; }
    public int WinningTrades { get; protected set; }
    public int LosingTrades { get; protected set; }
    public decimal PeakEquity { get; protected set; }
    public decimal MaxDrawdown { get; protected set; }

    public DateTime CurrentTime { get; set; }
    public List<ExecutedTrade> TradeLedger { get; protected set; }

    public decimal MaxParticipationRate { get; private set; } = 1.00m;

    public SimulatedBroker(decimal startingCash)
    {
        CashBalance = startingCash;
        PeakEquity = startingCash;
        TradeLedger = new List<ExecutedTrade>();
    }

    public decimal GetPositionShares(string assetId) => _positionShares.GetValueOrDefault(assetId, 0m);
    public decimal GetAverageEntryPrice(string assetId) => _averageEntryPrices.GetValueOrDefault(assetId, 0m);
    public decimal GetNoPositionShares(string assetId) => _noPositionShares.GetValueOrDefault(assetId, 0m);
    public decimal GetAverageNoEntryPrice(string assetId) => _averageNoEntryPrices.GetValueOrDefault(assetId, 0m);

    public void UpdateLastKnownPrice(string assetId, decimal price)
    {
        _lastKnownPrices[assetId] = price;
    }

    public virtual void Buy(string assetId, decimal currentPrice, decimal dollarsToInvest, decimal availableVolumeShares)
    {
        UpdateLastKnownPrice(assetId, currentPrice);
        if (dollarsToInvest <= 0.01m || CashBalance < dollarsToInvest) return;

        decimal executionPrice = Math.Min(currentPrice + SpreadPenalty, 0.99m);
        decimal desiredShares = dollarsToInvest / executionPrice;
        decimal actualSharesBought = Math.Min(desiredShares, availableVolumeShares * MaxParticipationRate);

        if (actualSharesBought <= 0) return;

        decimal actualDollarsSpent = actualSharesBought * executionPrice;
        decimal currentShares = GetPositionShares(assetId);
        decimal currentAvgPrice = GetAverageEntryPrice(assetId);

        decimal totalCost = (currentShares * currentAvgPrice) + actualDollarsSpent;

        _positionShares[assetId] = currentShares + actualSharesBought;
        _averageEntryPrices[assetId] = totalCost / _positionShares[assetId];
        CashBalance -= actualDollarsSpent;

        TradeLedger.Add(new ExecutedTrade { OutcomeId = assetId, Date = CurrentTime, Side = "BUY", Price = executionPrice, Shares = actualSharesBought, DollarValue = actualDollarsSpent });
    }

    public virtual void SellAll(string assetId, decimal currentPrice, decimal availableVolumeShares)
    {
        UpdateLastKnownPrice(assetId, currentPrice);
        decimal currentShares = GetPositionShares(assetId);
        if (currentShares <= 0) return;

        decimal sharesToSell = Math.Min(currentShares, availableVolumeShares * MaxParticipationRate);
        if (sharesToSell <= 0) return;

        decimal executionPrice = Math.Max(currentPrice - SpreadPenalty, 0.01m);
        decimal cashReceived = sharesToSell * executionPrice;
        decimal currentAvgPrice = GetAverageEntryPrice(assetId);

        if (executionPrice > currentAvgPrice) WinningTrades++;
        else LosingTrades++;

        TradeLedger.Add(new ExecutedTrade { OutcomeId = assetId, Date = CurrentTime, Side = "SELL", Price = executionPrice, Shares = sharesToSell, DollarValue = cashReceived });

        CashBalance += cashReceived;
        _positionShares[assetId] = currentShares - sharesToSell;

        if (_positionShares[assetId] == 0) _averageEntryPrices[assetId] = 0;
        TotalTradesExecuted++;
    }

    // Identical signature updates for BuyNo / SellAllNo
    public virtual void BuyNo(string assetId, decimal currentYesPrice, decimal dollarsToInvest, decimal availableVolumeShares)
    {
        UpdateLastKnownPrice(assetId, currentYesPrice);
        decimal currentNoPrice = 1.00m - currentYesPrice;
        decimal executionPrice = Math.Min(currentNoPrice + SpreadPenalty, 0.99m);

        if (executionPrice <= 0.00m || dollarsToInvest <= 0.01m || CashBalance < dollarsToInvest) return;

        decimal desiredShares = dollarsToInvest / executionPrice;
        decimal actualSharesBought = Math.Min(desiredShares, availableVolumeShares * MaxParticipationRate);

        if (actualSharesBought <= 0) return;

        decimal actualDollarsSpent = actualSharesBought * executionPrice;
        decimal currentShares = GetNoPositionShares(assetId);
        decimal currentAvgPrice = GetAverageNoEntryPrice(assetId);

        decimal totalCost = (currentShares * currentAvgPrice) + actualDollarsSpent;

        _noPositionShares[assetId] = currentShares + actualSharesBought;
        _averageNoEntryPrices[assetId] = totalCost / _noPositionShares[assetId];
        CashBalance -= actualDollarsSpent;

        TradeLedger.Add(new ExecutedTrade { OutcomeId = assetId, Date = CurrentTime, Side = "BUY NO", Price = executionPrice, Shares = actualSharesBought, DollarValue = actualDollarsSpent });
    }

    public virtual void SellAllNo(string assetId, decimal currentYesPrice, decimal availableVolumeShares)
    {
        UpdateLastKnownPrice(assetId, currentYesPrice);
        decimal currentShares = GetNoPositionShares(assetId);
        if (currentShares <= 0) return;

        decimal currentNoPrice = 1.00m - currentYesPrice;
        decimal executionPrice = Math.Max(currentNoPrice - SpreadPenalty, 0.01m);
        decimal sharesToSell = Math.Min(currentShares, availableVolumeShares * MaxParticipationRate);

        if (sharesToSell <= 0) return;

        decimal cashReceived = sharesToSell * executionPrice;
        decimal currentAvgPrice = GetAverageNoEntryPrice(assetId);

        if (executionPrice > currentAvgPrice) WinningTrades++;
        else LosingTrades++;

        TradeLedger.Add(new ExecutedTrade { OutcomeId = assetId, Date = CurrentTime, Side = "SELL NO", Price = executionPrice, Shares = sharesToSell, DollarValue = cashReceived });

        CashBalance += cashReceived;
        _noPositionShares[assetId] = currentShares - sharesToSell;

        if (_noPositionShares[assetId] == 0) _averageNoEntryPrices[assetId] = 0;
        TotalTradesExecuted++;
    }

    public decimal GetTotalPortfolioValue()
    {
        decimal activeValue = 0m;
        foreach (var kvp in _positionShares)
        {
            decimal price = _lastKnownPrices.GetValueOrDefault(kvp.Key, 0m);
            activeValue += kvp.Value * price;
        }
        foreach (var kvp in _noPositionShares)
        {
            decimal price = 1.00m - _lastKnownPrices.GetValueOrDefault(kvp.Key, 1.00m);
            activeValue += kvp.Value * price;
        }
        return CashBalance + activeValue;
    }
}