/*using System;
using System.Collections.Generic;
using System.Linq;
using PredictionBacktester.Core.Entities.Database;
using PredictionBacktester.Engine;

namespace PredictionBacktester.Strategies;

public class FlashCrashSniperStrategy : ITickStrategy
{
    private readonly decimal _crashThreshold;
    private readonly long _timeWindowSeconds;
    private readonly decimal _reboundProfitMargin;
    private readonly decimal _riskPercentage;

    // A queue to track every tick that happened in the last X seconds
    private readonly Queue<Trade> _recentTicks;

    public FlashCrashSniperStrategy(
        decimal crashThreshold = 0.15m, // Price must drop 15 cents
        long timeWindowSeconds = 60,    // ...within 60 seconds
        decimal reboundProfitMargin = 0.05m, // Sell as soon as we make 5 cents profit
        decimal riskPercentage = 0.05m)
    {
        _crashThreshold = crashThreshold;
        _timeWindowSeconds = timeWindowSeconds;
        _reboundProfitMargin = reboundProfitMargin;
        _riskPercentage = riskPercentage;

        _recentTicks = new Queue<Trade>();
    }

    public void OnTick(Trade tick, SimulatedBroker broker)
    {
        // 1. Maintain the time window (kick out ticks older than 60 seconds)
        _recentTicks.Enqueue(tick);
        while (_recentTicks.Count > 0 && (tick.Timestamp - _recentTicks.Peek().Timestamp) > _timeWindowSeconds)
        {
            _recentTicks.Dequeue();
        }

        if (_recentTicks.Count < 2) return;

        // 2. Look for the flash crash!
        decimal maxPriceInWindow = _recentTicks.Max(t => t.Price);
        decimal priceDrop = maxPriceInWindow - tick.Price;

        decimal currentEquity = broker.GetTotalPortfolioValue(tick.Price);
        decimal dollarsToInvest = Math.Min(currentEquity * _riskPercentage, broker.CashBalance);

        // --- THE "YES" SIDE (Buying the dip) ---
        if (broker.PositionShares > 0)
        {
            // High Frequency Take Profit: We don't hold this! As soon as it bounces up by our margin, dump it!
            if (tick.Price >= broker.AverageEntryPrice + _reboundProfitMargin)
            {
                broker.SellAll(tick.Price, tick.Size);
            }
        }
        else if (priceDrop >= _crashThreshold && dollarsToInvest >= 1.00m)
        {
            // FLASH CRASH DETECTED! Someone fat-fingered or panic sold!
            broker.Buy(tick.Price, dollarsToInvest, tick.Size);

            // Clear the window so we don't accidentally buy the same crash twice
            _recentTicks.Clear();
            return;
        }

        // --- THE "NO" SIDE (Shorting a sudden fake spike) ---
        if (broker.NoPositionShares > 0)
        {
            decimal currentNoPrice = 1.00m - tick.Price;
            if (currentNoPrice >= broker.AverageNoEntryPrice + _reboundProfitMargin)
            {
                broker.SellAllNo(tick.Price, tick.Size);
            }
        }
        else if (tick.Price - _recentTicks.Min(t => t.Price) >= _crashThreshold && dollarsToInvest >= 1.00m)
        {
            // FLASH SPIKE DETECTED! Price shot up 15 cents in 60 seconds. Fade the fakeout! (Buy NO)
            broker.BuyNo(tick.Price, dollarsToInvest, tick.Size);
            _recentTicks.Clear();
            return;
        }
    }
}*/

using System;
using System.Collections.Generic;
using System.Linq;
using PredictionBacktester.Core.Entities.Database;
using PredictionBacktester.Engine;

namespace PredictionBacktester.Strategies;

public class FlashCrashSniperStrategy : ITickStrategy
{
    private readonly decimal _crashThreshold;
    private readonly long _timeWindowSeconds;
    private readonly decimal _reboundProfitMargin;
    private readonly decimal _stopLossMargin; // NEW: Bagholder prevention
    private readonly long _executionDelaySeconds; // NEW: Latency simulator
    private readonly decimal _riskPercentage;

    private readonly Queue<Trade> _recentTicks;

    // State for our pending delayed orders
    private long _pendingBuyYesTimestamp = 0;
    private long _pendingBuyNoTimestamp = 0;

    public FlashCrashSniperStrategy(
        decimal crashThreshold = 0.15m,
        long timeWindowSeconds = 60,
        decimal reboundProfitMargin = 0.05m,
        decimal stopLossMargin = 0.15m, // Stop out if we drop 15 cents below entry
        long executionDelaySeconds = 2, // Wait 2 seconds before filling the order!
        decimal riskPercentage = 0.05m)
    {
        _crashThreshold = crashThreshold;
        _timeWindowSeconds = timeWindowSeconds;
        _reboundProfitMargin = reboundProfitMargin;
        _stopLossMargin = stopLossMargin;
        _executionDelaySeconds = executionDelaySeconds;
        _riskPercentage = riskPercentage;

        _recentTicks = new Queue<Trade>();
    }

    public void OnTick(Trade tick, SimulatedBroker broker)
    {
        decimal currentEquity = broker.GetTotalPortfolioValue(tick.Price);
        decimal dollarsToInvest = Math.Min(currentEquity * _riskPercentage, broker.CashBalance);

        // --- 1. EXECUTE PENDING DELAYED ORDERS ---
        // If we have a pending YES order, and the required latency time has passed
        if (_pendingBuyYesTimestamp > 0 && tick.Timestamp >= _pendingBuyYesTimestamp)
        {
            if (broker.PositionShares == 0 && dollarsToInvest >= 1.00m)
            {
                // We execute at THIS tick's price (slippage) and THIS tick's size (liquidity limits)
                broker.Buy(tick.Price, dollarsToInvest, tick.Size);
            }
            _pendingBuyYesTimestamp = 0; // Clear the pending order
        }

        // If we have a pending NO order, and the required latency time has passed
        if (_pendingBuyNoTimestamp > 0 && tick.Timestamp >= _pendingBuyNoTimestamp)
        {
            if (broker.NoPositionShares == 0 && dollarsToInvest >= 1.00m)
            {
                broker.BuyNo(tick.Price, dollarsToInvest, tick.Size);
            }
            _pendingBuyNoTimestamp = 0;
        }

        // --- 2. MAINTAIN THE TIME WINDOW ---
        _recentTicks.Enqueue(tick);
        while (_recentTicks.Count > 0 && (tick.Timestamp - _recentTicks.Peek().Timestamp) > _timeWindowSeconds)
        {
            _recentTicks.Dequeue();
        }

        if (_recentTicks.Count < 2) return;

        // --- 3. MANAGE OPEN POSITIONS (Take Profit & Stop Loss) ---
        if (broker.PositionShares > 0)
        {
            bool isTakeProfit = tick.Price >= broker.AverageEntryPrice + _reboundProfitMargin;
            bool isStopLoss = tick.Price <= broker.AverageEntryPrice - _stopLossMargin;

            if (isTakeProfit || isStopLoss)
                broker.SellAll(tick.Price, tick.Size); // Sell into current available liquidity
        }
        else if (broker.NoPositionShares > 0)
        {
            decimal currentNoPrice = 1.00m - tick.Price;
            bool isTakeProfit = currentNoPrice >= broker.AverageNoEntryPrice + _reboundProfitMargin;
            bool isStopLoss = currentNoPrice <= broker.AverageNoEntryPrice - _stopLossMargin;

            if (isTakeProfit || isStopLoss)
                broker.SellAllNo(tick.Price, tick.Size);
        }
        // --- 4. HUNT FOR NEW CRASHES (If we don't have pending orders) ---
        else
        {
            decimal maxPriceInWindow = _recentTicks.Max(t => t.Price);
            decimal minPriceInWindow = _recentTicks.Min(t => t.Price);

            // YES SIGNAL
            if (maxPriceInWindow - tick.Price >= _crashThreshold && _pendingBuyYesTimestamp == 0)
            {
                // TRIGGER PULLED! But we must wait X seconds for the API to process it.
                _pendingBuyYesTimestamp = tick.Timestamp + _executionDelaySeconds;
                _recentTicks.Clear();
                return;
            }

            // NO SIGNAL
            if (tick.Price - minPriceInWindow >= _crashThreshold && _pendingBuyNoTimestamp == 0)
            {
                _pendingBuyNoTimestamp = tick.Timestamp + _executionDelaySeconds;
                _recentTicks.Clear();
                return;
            }
        }
    }
}