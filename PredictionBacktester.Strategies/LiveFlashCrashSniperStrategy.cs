using System;
using System.Collections.Generic;
using System.Linq;
using PredictionBacktester.Core.Entities.Database;
using PredictionBacktester.Engine;

namespace PredictionBacktester.Strategies;

public class LiveFlashCrashSniperStrategy : ILiveStrategy
{
    private readonly decimal _crashThreshold;
    private readonly long _timeWindowSeconds;
    private readonly decimal _reboundProfitMargin;
    private readonly decimal _stopLossMargin;
    private readonly decimal _riskPercentage;

    private readonly Queue<(long Timestamp, decimal Price)> _recentAsks;

    public LiveFlashCrashSniperStrategy(
        decimal crashThreshold = 0.15m,
        long timeWindowSeconds = 60,
        decimal reboundProfitMargin = 0.05m,
        decimal stopLossMargin = 0.15m,
        decimal riskPercentage = 0.05m)
    {
        _crashThreshold = crashThreshold;
        _timeWindowSeconds = timeWindowSeconds;
        _reboundProfitMargin = reboundProfitMargin;
        _stopLossMargin = stopLossMargin;
        _riskPercentage = riskPercentage;

        _recentAsks = new Queue<(long, decimal)>();
    }

    public void OnBookUpdate(LocalOrderBook book, GlobalSimulatedBroker broker)
    {
        long now = DateTimeOffset.UtcNow.ToUnixTimeSeconds();
        string assetId = book.AssetId;

        decimal bestAsk = book.GetBestAskPrice();
        decimal bestBid = book.GetBestBidPrice();
        decimal availableAskSize = book.GetBestAskSize();
        decimal availableBidSize = book.GetBestBidSize();

        // Always keep the global broker updated on this asset's latest price!
        broker.UpdateLastKnownPrice(assetId, bestAsk);

        if (bestAsk >= 1.00m || availableAskSize <= 0) return;

        _recentAsks.Enqueue((now, bestAsk));
        while (_recentAsks.Count > 0 && (now - _recentAsks.Peek().Timestamp) > _timeWindowSeconds)
        {
            _recentAsks.Dequeue();
        }

        if (_recentAsks.Count < 2) return;

        // Uses the global portfolio equity across all markets
        decimal currentEquity = broker.GetTotalPortfolioValue();
        decimal dollarsToInvest = Math.Min(currentEquity * _riskPercentage, broker.CashBalance);

        // Fetch positions specifically for THIS asset
        decimal positionShares = broker.GetPositionShares(assetId);

        if (positionShares > 0)
        {
            decimal avgEntry = broker.GetAverageEntryPrice(assetId);
            bool isTakeProfit = bestBid >= avgEntry + _reboundProfitMargin;
            bool isStopLoss = bestBid <= avgEntry - _stopLossMargin;

            if (isTakeProfit || isStopLoss)
            {
                broker.SellAll(assetId, bestBid, availableBidSize);
            }
            return;
        }

        decimal maxAskInWindow = _recentAsks.Max(x => x.Price);

        if (maxAskInWindow - bestAsk >= _crashThreshold && dollarsToInvest >= 1.00m)
        {
            decimal maxAffordableShares = dollarsToInvest / bestAsk;
            decimal sharesToBuy = Math.Min(maxAffordableShares, availableAskSize);
            decimal actualDollarsSpent = sharesToBuy * bestAsk;

            if (actualDollarsSpent >= 1.00m)
            {
                broker.Buy(assetId, bestAsk, actualDollarsSpent, sharesToBuy);
                _recentAsks.Clear();
            }
        }
    }
}