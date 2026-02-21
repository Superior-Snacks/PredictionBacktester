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
        }
    }
}