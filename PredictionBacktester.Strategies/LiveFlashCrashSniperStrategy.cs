using System;
using System.Collections.Generic;
using System.Linq;
using PredictionBacktester.Core.Entities.Database;
using PredictionBacktester.Engine;

namespace PredictionBacktester.Strategies;

public class LiveFlashCrashSniperStrategy : ILiveStrategy
{
    public string StrategyName { get; }
    private readonly decimal _crashThreshold;
    private readonly long _timeWindowSeconds;
    private readonly decimal _reboundProfitMargin;
    private readonly decimal _stopLossMargin;
    private readonly decimal _riskPercentage;

    private readonly Queue<(long Timestamp, decimal Price)> _recentAsks;
    private volatile bool _pendingBuy = false;
    private volatile bool _pendingSell = false;

    public LiveFlashCrashSniperStrategy(
        string strategyName = "FlashCrashSniper",
        decimal crashThreshold = 0.15m,
        long timeWindowSeconds = 60,
        decimal reboundProfitMargin = 0.05m,
        decimal stopLossMargin = 0.15m,
        decimal riskPercentage = 0.05m)
    {
        StrategyName = strategyName;
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

        // Uses the global portfolio equity across all markets
        decimal currentEquity = broker.GetTotalPortfolioValue();
        decimal dollarsToInvest = Math.Min(currentEquity * _riskPercentage, broker.CashBalance);

        // Fetch positions specifically for THIS asset
        decimal positionShares = broker.GetPositionShares(assetId);

        if (positionShares > 0)
        {
            if (_pendingSell) return;

            decimal avgEntry = broker.GetAverageEntryPrice(assetId);
            bool isTakeProfit = bestBid >= avgEntry + _reboundProfitMargin;
            bool isStopLoss = bestBid <= avgEntry - _stopLossMargin;

            if (isTakeProfit || isStopLoss)
            {
                _pendingSell = true;
                broker.SubmitSellAllOrder(assetId, bestBid, book, () => _pendingSell = false);
            }
            return;
        }

        decimal maxAskInWindow = _recentAsks.Max(x => x.Price);

        if (_pendingBuy) return;

        if (maxAskInWindow - bestAsk >= _crashThreshold && dollarsToInvest >= 1.00m)
        {
            decimal maxAffordableShares = dollarsToInvest / bestAsk;
            decimal sharesToBuy = Math.Min(maxAffordableShares, availableAskSize);
            decimal actualDollarsSpent = sharesToBuy * bestAsk;

            if (actualDollarsSpent >= 1.00m)
            {
                _pendingBuy = true;
                broker.SubmitBuyOrder(assetId, bestAsk, actualDollarsSpent, book, () => _pendingBuy = false);
                _recentAsks.Clear();
            }
        }
    }
}