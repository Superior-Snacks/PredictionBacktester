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

    // Settlement unlock: fire callback only once after lock expires
    private bool _settlementUnlockFired = false;

    // Exit guard: only fire SubmitSellAllOrder once per position (broker handles retries)
    private bool _exitFired = false;

    // No-bid timeout: if we hold a position and never see a valid bid for N ms, assume loss
    private const long NO_BID_TIMEOUT_MS = 5 * 60 * 1000; // 5 minutes
    private long _lastValidBidMs = 0;

    private readonly Queue<(long Timestamp, decimal Price)> _recentAsks;
    private decimal _lastGap;

    // Optional telemetry callbacks (wired externally for test mode)
    public Action<string, decimal, decimal>? OnCrashSpotted;      // (assetId, bestAsk, gap)
    public Action<long>? OnSustainConfirmed;                       // (sustainedMs)
    public Action<decimal, decimal>? OnBuySubmitted;               // (price, dollars)
    public Action? OnSettlementUnlocked;
    public Action<string, decimal, decimal>? OnExitTriggered;      // (reason, bidPrice, entryPrice)

    public decimal GetMaxGap() => _lastGap;

    public LiveFlashCrashSniperStrategy(
        string strategyName = "FlashCrashSniper",
        decimal crashThreshold = 0.25m,
        long timeWindowSeconds = 20,
        decimal reboundProfitMargin = 0.05m,
        decimal stopLossMargin = 0.10m,
        decimal riskPercentage = 0.05m,
        decimal entrySlippage = 0.03m,
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
        long nowMs = broker.CurrentTimeMs;
        long nowSec = nowMs / 1000;
        string assetId = book.AssetId;

        decimal bestAsk = book.GetBestAskPrice();
        decimal bestBid = book.GetBestBidPrice();
        decimal availableAskSize = book.GetBestAskSize();
        decimal availableBidSize = book.GetBestBidSize();

        broker.UpdateLastKnownPrice(assetId, bestAsk);

        // Exit logic runs FIRST — must not be blocked by entry-quality guards
        decimal positionShares = broker.GetPositionShares(assetId);
        decimal minTradeSize = broker.GetMinSize(assetId);

        if (positionShares >= minTradeSize)
        {
            // Settlement lock: shares are frozen until the blockchain settles
            if (_settlementLockMs > 0 && nowMs < _settlementUnlockTimeMs)
                return;

            // Fire settlement unlock callback once
            if (!_settlementUnlockFired && _settlementLockMs > 0)
            {
                _settlementUnlockFired = true;
                OnSettlementUnlocked?.Invoke();
            }

            // Track valid bid availability for no-bid timeout
            if (bestBid > 0.01m)
                _lastValidBidMs = nowMs;

            // No-bid timeout: held position but never seen a sellable bid for 5 minutes → assume total loss
            if (bestBid <= 0.01m)
            {
                if (_lastValidBidMs > 0 && (nowMs - _lastValidBidMs) >= NO_BID_TIMEOUT_MS)
                {
                    if (!_exitFired)
                    {
                        _exitFired = true;
                        decimal avgEntry2 = broker.GetAverageEntryPrice(assetId);
                        OnExitTriggered?.Invoke("NO_BID_TIMEOUT", 0m, avgEntry2);
                    }
                    // Force resolve at $0 — total loss
                    broker.SubmitSellAllOrder(assetId, 0.001m, book);
                }
                return;
            }

            decimal avgEntry = broker.GetAverageEntryPrice(assetId);
            bool isTakeProfit = bestBid >= avgEntry + _reboundProfitMargin;
            bool isStopLoss = bestBid <= Math.Max(avgEntry - _stopLossMargin, 0.01m);
            bool isDeadPosition = bestAsk <= 0.05m && bestBid <= 0.05m;

            if (isTakeProfit || isStopLoss || isDeadPosition)
            {
                if (!_exitFired)
                {
                    _exitFired = true;
                    string reason = isTakeProfit ? "TAKE_PROFIT" : isDeadPosition ? "DEAD_POSITION" : "STOP_LOSS";
                    OnExitTriggered?.Invoke(reason, bestBid, avgEntry);
                }
                decimal sellLimitPrice = Math.Max(bestBid - _exitSlippage, 0.001m);
                broker.SubmitSellAllOrder(assetId, sellLimitPrice, book);
            }
            return;
        }

        // Entry guards — only apply when looking for new positions
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
                OnCrashSpotted?.Invoke(assetId, bestAsk, _lastGap);
                return; // Wait for the next order book update
            }
            
            // The stopwatch is running. How long has it been?
            long timeInCrash = nowMs - _crashStartTimeMs;
            
            if (timeInCrash >= _requiredSustainMs && dollarsToInvest >= 1.00m)
            {
                OnSustainConfirmed?.Invoke(timeInCrash);

                decimal maxAffordableShares = dollarsToInvest / bestAsk;
                decimal sharesToBuy = Math.Min(maxAffordableShares, availableAskSize);
                decimal actualDollarsSpent = sharesToBuy * bestAsk;

                if (actualDollarsSpent >= 1.00m)
                {
                    OnBuySubmitted?.Invoke(bestAsk + _entrySlippage, actualDollarsSpent);
                    broker.SubmitBuyOrder(assetId, bestAsk + _entrySlippage, actualDollarsSpent, book);
                    _recentAsks.Clear();
                    _crashStartTimeMs = 0;
                    _settlementUnlockTimeMs = nowMs + _settlementLockMs;
                    _settlementUnlockFired = false;
                    _exitFired = false;
                    _lastValidBidMs = nowMs;
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