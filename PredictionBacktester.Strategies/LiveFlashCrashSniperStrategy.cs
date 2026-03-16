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
    private readonly decimal _entrySlippage;
    private readonly decimal _exitSlippage;
    
    // NEW: The Anti-Spoofing Stopwatch
    private readonly long _requiredSustainMs;
    private long _crashStartTimeMs = 0;

    // Settlement lock: blocks sells for N ms after a buy
    private readonly long _settlementLockMs;
    private long _settlementUnlockTimeMs = 0;

    private readonly Queue<(long Timestamp, decimal Price)> _recentAsks;
    private decimal _lastGap;

    public decimal GetMaxGap() => _lastGap;

    public LiveFlashCrashSniperStrategy(
        string strategyName = "FlashCrashSniper",
        decimal crashThreshold = 0.25m,
        long timeWindowSeconds = 20,
        decimal reboundProfitMargin = 0.05m,
        decimal stopLossMargin = 0.10m,
        decimal riskPercentage = 0.05m,
        decimal entrySlippage = 0.01m,
        decimal exitSlippage = 0.03m,
        long requiredSustainMs = 0,
        long settlementLockMs = 0)
    {
        StrategyName = strategyName;
        _crashThreshold = crashThreshold;
        _timeWindowSeconds = timeWindowSeconds;
        _reboundProfitMargin = reboundProfitMargin;
        _stopLossMargin = stopLossMargin;
        _riskPercentage = riskPercentage;
        _entrySlippage = entrySlippage;
        _exitSlippage = exitSlippage;
        _requiredSustainMs = requiredSustainMs;
        _settlementLockMs = settlementLockMs;

        _recentAsks = new Queue<(long, decimal)>();
    }

    public void OnBookUpdate(LocalOrderBook book, GlobalSimulatedBroker broker)
    {
        long nowSec = DateTimeOffset.UtcNow.ToUnixTimeSeconds();
        long nowMs = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds(); // Needed for the stopwatch
        string assetId = book.AssetId;

        decimal bestAsk = book.GetBestAskPrice();
        decimal bestBid = book.GetBestBidPrice();
        decimal availableAskSize = book.GetBestAskSize();
        decimal availableBidSize = book.GetBestBidSize();

        broker.UpdateLastKnownPrice(assetId, bestAsk);

        if (bestAsk >= 1.00m || bestAsk <= 0.00m || availableAskSize <= 0 || availableBidSize <= 0) return;

        if (bestAsk - bestBid > 0.05m) return;

        // 1. Memory: Always record the price first
        _recentAsks.Enqueue((nowSec, bestAsk));
        while (_recentAsks.Count > 0 && (nowSec - _recentAsks.Peek().Timestamp) > _timeWindowSeconds)
        {
            _recentAsks.Dequeue();
        }

        if (_recentAsks.Count < 2) return;

        decimal currentEquity = broker.GetTotalPortfolioValue();
        decimal dollarsToInvest = Math.Min(currentEquity * _riskPercentage, broker.CashBalance);
        decimal positionShares = broker.GetPositionShares(assetId);

        decimal minTradeSize = broker.GetMinSize(assetId);

        // 2. Exit Logic (dust positions are ignored — treat as closed)
        if (positionShares >= minTradeSize)
        {
            // Settlement lock: shares are frozen until the blockchain settles
            if (_settlementLockMs > 0 && nowMs < _settlementUnlockTimeMs)
                return;

            decimal avgEntry = broker.GetAverageEntryPrice(assetId);
            bool isTakeProfit = bestBid >= avgEntry + _reboundProfitMargin;
            bool isStopLoss = bestBid <= avgEntry - _stopLossMargin;

            if (isTakeProfit || isStopLoss)
            {
                decimal sellLimitPrice = Math.Max(bestBid - _exitSlippage, 0.001m);
                broker.SubmitSellAllOrder(assetId, sellLimitPrice, book);
            }
            return;
        }

        // 3. Entry Logic (With Stopwatch)
        decimal maxAskInWindow = _recentAsks.Max(x => x.Price);
        _lastGap = maxAskInWindow - bestAsk;
        bool isFlashCrash = _lastGap >= _crashThreshold;

        if (isFlashCrash)
        {
            if (_crashStartTimeMs == 0)
            {
                // Start the stopwatch!
                _crashStartTimeMs = nowMs;
                return; // Wait for the next order book update
            }
            
            // The stopwatch is running. How long has it been?
            long timeInCrash = nowMs - _crashStartTimeMs;
            
            if (timeInCrash >= _requiredSustainMs && dollarsToInvest >= 1.00m)
            {
                decimal maxAffordableShares = dollarsToInvest / bestAsk;
                decimal sharesToBuy = Math.Min(maxAffordableShares, availableAskSize);
                decimal actualDollarsSpent = sharesToBuy * bestAsk;

                if (actualDollarsSpent >= 1.00m)
                {
                    broker.SubmitBuyOrder(assetId, bestAsk + _entrySlippage, actualDollarsSpent, book);
                    _recentAsks.Clear();
                    _crashStartTimeMs = 0;
                    _settlementUnlockTimeMs = nowMs + _settlementLockMs;
                }
            }
        }
        else
        {
            // Price bounced back! It was a spoof or a micro-dip. Reset the stopwatch.
            _crashStartTimeMs = 0;
        }
    }
}