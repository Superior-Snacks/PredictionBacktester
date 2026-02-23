using System;
using System.Collections.Generic;
using System.Linq;
using PredictionBacktester.Core.Entities.Database;
using PredictionBacktester.Engine;

namespace PredictionBacktester.Strategies;

public class LiveFlashCrashSniperStrategy : ITickStrategy
{
    private readonly decimal _crashThreshold;
    private readonly long _timeWindowSeconds;
    private readonly decimal _reboundProfitMargin;
    private readonly decimal _stopLossMargin; // Kept the bagholder prevention!
    private readonly decimal _riskPercentage;

    private readonly Queue<Trade> _recentTicks;

    public LiveFlashCrashSniperStrategy(
        decimal crashThreshold = 0.15m,
        long timeWindowSeconds = 60,
        decimal reboundProfitMargin = 0.05m,
        decimal stopLossMargin = 0.15m,
        decimal riskPercentage = 0.05m) // Removed executionDelaySeconds
    {
        _crashThreshold = crashThreshold;
        _timeWindowSeconds = timeWindowSeconds;
        _reboundProfitMargin = reboundProfitMargin;
        _stopLossMargin = stopLossMargin;
        _riskPercentage = riskPercentage;

        _recentTicks = new Queue<Trade>();
    }

    public void OnTick(Trade tick, SimulatedBroker broker)
    {
        decimal currentEquity = broker.GetTotalPortfolioValue(tick.Price);
        decimal dollarsToInvest = Math.Min(currentEquity * _riskPercentage, broker.CashBalance);

        // --- 1. MAINTAIN THE TIME WINDOW ---
        _recentTicks.Enqueue(tick);
        while (_recentTicks.Count > 0 && (tick.Timestamp - _recentTicks.Peek().Timestamp) > _timeWindowSeconds)
        {
            _recentTicks.Dequeue();
        }

        if (_recentTicks.Count < 2) return;

        // --- 2. MANAGE OPEN POSITIONS (Take Profit & Stop Loss) ---
        if (broker.PositionShares > 0)
        {
            bool isTakeProfit = tick.Price >= broker.AverageEntryPrice + _reboundProfitMargin;
            bool isStopLoss = tick.Price <= broker.AverageEntryPrice - _stopLossMargin;

            if (isTakeProfit || isStopLoss)
                broker.SellAll(tick.Price, tick.Size);
        }
        else if (broker.NoPositionShares > 0)
        {
            decimal currentNoPrice = 1.00m - tick.Price;
            bool isTakeProfit = currentNoPrice >= broker.AverageNoEntryPrice + _reboundProfitMargin;
            bool isStopLoss = currentNoPrice <= broker.AverageNoEntryPrice - _stopLossMargin;

            if (isTakeProfit || isStopLoss)
                broker.SellAllNo(tick.Price, tick.Size);
        }
        // --- 3. HUNT FOR NEW CRASHES (INSTANT EXECUTION) ---
        else
        {
            decimal maxPriceInWindow = _recentTicks.Max(t => t.Price);
            decimal minPriceInWindow = _recentTicks.Min(t => t.Price);

            // YES SIGNAL
            if (maxPriceInWindow - tick.Price >= _crashThreshold && dollarsToInvest >= 1.00m)
            {
                // Instant trigger pull!
                broker.Buy(tick.Price, dollarsToInvest, tick.Size);
                _recentTicks.Clear();
                return;
            }

            // NO SIGNAL
            if (tick.Price - minPriceInWindow >= _crashThreshold && dollarsToInvest >= 1.00m)
            {
                // Instant trigger pull!
                broker.BuyNo(tick.Price, dollarsToInvest, tick.Size);
                _recentTicks.Clear();
                return;
            }
        }
    }
}