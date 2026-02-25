using System;
using System.Collections.Generic;
using System.Linq;
using PredictionBacktester.Engine;

namespace PredictionBacktester.Strategies;

public class MeanReversionStatArbStrategy : ILiveStrategy
{
    public string StrategyName { get; }

    private readonly int _rollingWindowSize;
    private readonly decimal _entryZScore;
    private readonly decimal _exitZScore;
    private readonly decimal _riskPercentage;

    // We keep a rolling window of prices to calculate the Mean and StdDev
    private readonly Queue<decimal> _priceHistory;

    public MeanReversionStatArbStrategy(
        string strategyName = "StatArb_MeanRev",
        int rollingWindowSize = 100,      // Look at the last 100 ticks
        decimal entryZScore = -2.0m,      // Buy when price drops 2 Standard Deviations below normal
        decimal exitZScore = 0.0m,        // Sell when it returns to the Mean (0 StdDev)
        decimal riskPercentage = 0.05m)
    {
        StrategyName = strategyName;
        _rollingWindowSize = rollingWindowSize;
        _entryZScore = entryZScore;
        _exitZScore = exitZScore;
        _riskPercentage = riskPercentage;

        _priceHistory = new Queue<decimal>();
    }

    public void OnBookUpdate(LocalOrderBook book, GlobalSimulatedBroker broker)
    {
        string assetId = book.AssetId;
        decimal bestAsk = book.GetBestAskPrice();
        decimal bestBid = book.GetBestBidPrice();
        decimal availableAskSize = book.GetBestAskSize();
        decimal availableBidSize = book.GetBestBidSize();

        broker.UpdateLastKnownPrice(assetId, bestAsk);

        if (bestAsk >= 1.00m || bestAsk <= 0.00m || availableAskSize <= 0) return;

        // 1. Maintain the rolling window of prices
        _priceHistory.Enqueue(bestAsk);
        if (_priceHistory.Count > _rollingWindowSize)
        {
            _priceHistory.Dequeue();
        }

        // We need a full window of data before we can calculate accurate statistics
        if (_priceHistory.Count < _rollingWindowSize) return;

        // 2. Calculate the Mean (Simple Moving Average)
        decimal mean = _priceHistory.Average();

        // 3. Calculate the Standard Deviation
        double sumOfSquares = _priceHistory.Select(val => Math.Pow((double)(val - mean), 2)).Sum();
        double standardDeviation = Math.Sqrt(sumOfSquares / _priceHistory.Count);

        if (standardDeviation == 0) return; // Prevent division by zero if price is totally flat

        // 4. Calculate the Z-Score: How far is the current price from the Mean?
        decimal currentZScore = (bestAsk - mean) / (decimal)standardDeviation;

        decimal positionShares = broker.GetPositionShares(assetId);

        // --- MANAGE OPEN POSITIONS ---
        if (positionShares > 0)
        {
            // If the Z-Score reverts to our exit target (e.g., 0.0 = back to the mean)
            if (currentZScore >= _exitZScore)
            {
                broker.SellAll(assetId, bestBid, availableBidSize);
            }
            // Add an emergency time/price stop-loss if it goes 4 StdDevs against us
            else if (currentZScore <= -4.0m)
            {
                broker.SellAll(assetId, bestBid, availableBidSize);
            }
            return;
        }

        // --- TRIGGER NEW ENTRY ---
        // If the price artificially crashes past our Z-Score threshold (e.g., -2.0)
        if (currentZScore <= _entryZScore)
        {
            decimal currentEquity = broker.GetTotalPortfolioValue();
            decimal dollarsToInvest = Math.Min(currentEquity * _riskPercentage, broker.CashBalance);

            // Lowered minimum to $0.10 so it catches all tiny algorithmic anomalies
            if (dollarsToInvest >= 0.10m)
            {
                decimal maxAffordableShares = dollarsToInvest / bestAsk;
                decimal sharesToBuy = Math.Min(maxAffordableShares, availableAskSize);
                decimal actualDollarsSpent = sharesToBuy * bestAsk;

                if (actualDollarsSpent >= 0.10m)
                {
                    broker.Buy(assetId, bestAsk, actualDollarsSpent, sharesToBuy);
                }
            }
        }
    }
}