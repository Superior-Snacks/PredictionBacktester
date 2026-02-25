using System;
using System.Collections.Generic;
using System.Linq;
using PredictionBacktester.Core.Entities.Database;
using PredictionBacktester.Engine;

namespace PredictionBacktester.Strategies;

public class LiveFlashCrashReverseStrategy : ILiveStrategy
{
    public string StrategyName { get; }
    private readonly decimal _crashThreshold;
    private readonly long _timeWindowSeconds;
    private readonly decimal _reboundStopMargin;
    private readonly decimal _continueProfitMargin;
    private readonly decimal _riskPercentage;

    private readonly Queue<(long Timestamp, decimal Price)> _recentAsks;

    public LiveFlashCrashReverseStrategy(
        string strategyName = "FlashCrashReverse",
        decimal crashThreshold = 0.15m,
        long timeWindowSeconds = 60,
        decimal continueProfitMargin = 0.15m,
        decimal reboundStopMargin = 0.05m,
        decimal riskPercentage = 0.05m)
    {
        StrategyName = strategyName;
        _crashThreshold = crashThreshold;
        _timeWindowSeconds = timeWindowSeconds;
        _continueProfitMargin = continueProfitMargin;
        _reboundStopMargin = reboundStopMargin;
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

        broker.UpdateLastKnownPrice(assetId, bestAsk);

        if (bestAsk >= 1.00m || availableAskSize <= 0) return;

        _recentAsks.Enqueue((now, bestAsk));
        while (_recentAsks.Count > 0 && (now - _recentAsks.Peek().Timestamp) > _timeWindowSeconds)
        {
            _recentAsks.Dequeue();
        }

        if (_recentAsks.Count < 2) return;

        // Since the PaperBroker allows us to hold "NO" shares, we will use BuyNo/SellAllNo for Shorting
        decimal noPositionShares = broker.GetNoPositionShares(assetId);

        if (noPositionShares > 0)
        {
            decimal avgNoEntry = broker.GetAverageNoEntryPrice(assetId);
            decimal currentNoPrice = 1.00m - bestAsk; // Approximate NO price

            // Profit if the YES price keeps falling (NO price goes up)
            bool isTakeProfit = currentNoPrice >= avgNoEntry + _continueProfitMargin;
            // Stop Loss if the YES price rebounds (NO price goes down)
            bool isStopLoss = currentNoPrice <= avgNoEntry - _reboundStopMargin;

            if (isTakeProfit || isStopLoss)
            {
                broker.SellAllNo(assetId, bestAsk, availableAskSize);
            }
            return;
        }

        decimal maxAskInWindow = _recentAsks.Max(x => x.Price);

        // TRIGGER: If the YES price crashes, we Buy NO (expecting it to keep crashing)
        if (maxAskInWindow - bestAsk >= _crashThreshold)
        {
            decimal currentEquity = broker.GetTotalPortfolioValue();
            decimal dollarsToInvest = Math.Min(currentEquity * _riskPercentage, broker.CashBalance);

            if (dollarsToInvest >= 1.00m)
            {
                broker.BuyNo(assetId, bestAsk, dollarsToInvest, availableAskSize);
                _recentAsks.Clear();
            }
        }
    }
}