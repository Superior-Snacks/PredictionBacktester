using System;
using System.Collections.Concurrent; // NEW: Required for thread safety!
using System.Collections.Generic;
using PredictionBacktester.Core.Entities;

namespace PredictionBacktester.Engine;

public class GlobalSimulatedBroker
{
    public decimal SpreadPenalty { get; private set; } = 0.015m;

    // We use a lock object to protect non-concurrent properties like CashBalance and TradeLedger
    protected readonly object _brokerLock = new object();
    public object BrokerLock => _brokerLock;

    public decimal CashBalance { get; protected set; }

    // THREAD SAFETY: Upgraded to ConcurrentDictionary
    private ConcurrentDictionary<string, decimal> _positionShares = new();
    private ConcurrentDictionary<string, decimal> _averageEntryPrices = new();
    private ConcurrentDictionary<string, decimal> _noPositionShares = new();
    private ConcurrentDictionary<string, decimal> _averageNoEntryPrices = new();
    private ConcurrentDictionary<string, decimal> _lastKnownPrices = new();

    public int TotalTradesExecuted { get; protected set; }
    public int WinningTrades { get; protected set; }
    public int LosingTrades { get; protected set; }
    public decimal PeakEquity { get; protected set; }
    public decimal MaxDrawdown { get; protected set; }

    public DateTime CurrentTime { get; set; }
    public List<ExecutedTrade> TradeLedger { get; protected set; }

    public decimal MaxParticipationRate { get; private set; } = 1.00m;

    public GlobalSimulatedBroker(decimal startingCash)
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

        // Grab the bathroom key before modifying cash or ledgers!
        lock (_brokerLock)
        {
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

            TradeLedger.Add(new ExecutedTrade { OutcomeId = assetId, Date = DateTime.Now, Side = "BUY", Price = executionPrice, Shares = actualSharesBought, DollarValue = actualDollarsSpent });
        }
    }

    public virtual void SellAll(string assetId, decimal currentPrice, decimal availableVolumeShares)
    {
        UpdateLastKnownPrice(assetId, currentPrice);

        lock (_brokerLock)
        {
            decimal currentShares = GetPositionShares(assetId);
            if (currentShares <= 0) return;

            decimal sharesToSell = Math.Min(currentShares, availableVolumeShares * MaxParticipationRate);
            if (sharesToSell <= 0) return;

            decimal executionPrice = Math.Max(currentPrice - SpreadPenalty, 0.01m);
            decimal cashReceived = sharesToSell * executionPrice;
            decimal currentAvgPrice = GetAverageEntryPrice(assetId);

            if (executionPrice > currentAvgPrice) WinningTrades++;
            else LosingTrades++;

            TradeLedger.Add(new ExecutedTrade { OutcomeId = assetId, Date = DateTime.Now, Side = "SELL", Price = executionPrice, Shares = sharesToSell, DollarValue = cashReceived });

            CashBalance += cashReceived;
            _positionShares[assetId] = currentShares - sharesToSell;

            if (_positionShares[assetId] == 0) _averageEntryPrices[assetId] = 0;
            TotalTradesExecuted++;
        }
    }

    // Apply the exact same lock structure to BuyNo and SellAllNo...
    public virtual void BuyNo(string assetId, decimal currentYesPrice, decimal dollarsToInvest, decimal availableVolumeShares)
    {
        UpdateLastKnownPrice(assetId, currentYesPrice);

        lock (_brokerLock)
        {
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

            TradeLedger.Add(new ExecutedTrade { OutcomeId = assetId, Date = DateTime.Now, Side = "BUY NO", Price = executionPrice, Shares = actualSharesBought, DollarValue = actualDollarsSpent });
        }
    }

    public virtual void SellAllNo(string assetId, decimal currentYesPrice, decimal availableVolumeShares)
    {
        UpdateLastKnownPrice(assetId, currentYesPrice);

        lock (_brokerLock)
        {
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

            TradeLedger.Add(new ExecutedTrade { OutcomeId = assetId, Date = DateTime.Now, Side = "SELL NO", Price = executionPrice, Shares = sharesToSell, DollarValue = cashReceived });

            CashBalance += cashReceived;
            _noPositionShares[assetId] = currentShares - sharesToSell;

            if (_noPositionShares[assetId] == 0) _averageNoEntryPrices[assetId] = 0;
            TotalTradesExecuted++;
        }
    }

    public virtual void ResolveMarket(string assetId, decimal outcomePrice)
    {
        UpdateLastKnownPrice(assetId, outcomePrice);

        lock (_brokerLock)
        {
            decimal yesShares = GetPositionShares(assetId);
            decimal noShares = GetNoPositionShares(assetId);

            if (yesShares > 0)
            {
                decimal yesPayout = yesShares * outcomePrice;
                CashBalance += yesPayout;

                decimal currentAvgPrice = GetAverageEntryPrice(assetId);
                if (outcomePrice > currentAvgPrice) WinningTrades++;
                else LosingTrades++;

                TradeLedger.Add(new ExecutedTrade { OutcomeId = assetId, Date = DateTime.Now, Side = "RESOLVE YES", Price = outcomePrice, Shares = yesShares, DollarValue = yesPayout });

                _positionShares[assetId] = 0;
                _averageEntryPrices[assetId] = 0;
            }

            if (noShares > 0)
            {
                decimal noPayoutPrice = 1.00m - outcomePrice;
                decimal noPayout = noShares * noPayoutPrice;
                CashBalance += noPayout;

                decimal currentAvgNoPrice = GetAverageNoEntryPrice(assetId);
                if (noPayoutPrice > currentAvgNoPrice) WinningTrades++;
                else LosingTrades++;

                TradeLedger.Add(new ExecutedTrade { OutcomeId = assetId, Date = DateTime.Now, Side = "RESOLVE NO", Price = noPayoutPrice, Shares = noShares, DollarValue = noPayout });

                _noPositionShares[assetId] = 0;
                _averageNoEntryPrices[assetId] = 0;
            }
            TotalTradesExecuted++;
        }
    }

    public decimal GetTotalPortfolioValue()
    {
        lock (_brokerLock)
        {
            decimal activeValue = 0m;
            foreach (var kvp in _positionShares)
            {
                decimal price = _lastKnownPrices.GetValueOrDefault(kvp.Key, 0m);
                activeValue += kvp.Value * price;
            }
            foreach (var kvp in _noPositionShares)
            {
                decimal price = 1.00m - _lastKnownPrices.GetValueOrDefault(kvp.Key, 0.50m);
                activeValue += kvp.Value * price;
            }
            return CashBalance + activeValue;
        }
    }
}