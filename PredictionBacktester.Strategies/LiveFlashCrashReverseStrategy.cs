using System;
using System.Collections.Generic;
using System.Linq;
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

        // Always use the Ask for portfolio valuation of the YES token side
        broker.UpdateLastKnownPrice(assetId, bestAsk);

        if (bestAsk >= 1.00m || bestAsk <= 0.00m || availableAskSize <= 0 || availableBidSize <= 0) return;

        // THE FIX: The Maximum Spread Filter
        // If the spread is wider than 5 cents, the market makers have pulled liquidity. DO NOT TRADE!
        if (bestAsk - bestBid > 0.05m) return;

        _recentAsks.Enqueue((now, bestAsk));
        while (_recentAsks.Count > 0 && (now - _recentAsks.Peek().Timestamp) > _timeWindowSeconds)
        {
            _recentAsks.Dequeue();
        }

        if (_recentAsks.Count < 2) return;

        decimal noPositionShares = broker.GetNoPositionShares(assetId);

        // --- 1. MANAGE OPEN NO POSITIONS ---
        if (noPositionShares > 0)
        {
            decimal avgNoEntry = broker.GetAverageNoEntryPrice(assetId);

            // To SELL NO, you sell to NO Buyers (YES Sellers -> bestAsk)
            decimal currentNoPrice = 1.00m - bestAsk;

            bool isTakeProfit = currentNoPrice >= avgNoEntry + _continueProfitMargin;
            bool isStopLoss = currentNoPrice <= avgNoEntry - _reboundStopMargin;

            if (isTakeProfit || isStopLoss)
            {
                // To exit a NO position, you dump into the YES Sellers (availableAskSize)
                broker.SellAllNo(assetId, bestAsk, availableAskSize);
            }
            return;
        }

        // --- 2. TRIGGER NO ENTRY ---
        decimal maxAskInWindow = _recentAsks.Max(x => x.Price);

        if (maxAskInWindow - bestAsk >= _crashThreshold)
        {
            decimal currentEquity = broker.GetTotalPortfolioValue();
            decimal dollarsToInvest = Math.Min(currentEquity * _riskPercentage, broker.CashBalance);

            if (dollarsToInvest >= 1.00m)
            {
                // THE FIX: To BUY NO, you buy from NO Sellers (YES Buyers -> bestBid & availableBidSize)
                broker.BuyNo(assetId, bestBid, dollarsToInvest, availableBidSize);
                _recentAsks.Clear();
            }
        }
    }
}