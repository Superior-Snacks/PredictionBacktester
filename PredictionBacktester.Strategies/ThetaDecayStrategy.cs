using System;
using System.Collections.Generic;
using System.Linq;
using PredictionBacktester.Core.Entities;
using PredictionBacktester.Engine;

namespace PredictionBacktester.Strategies;

public class ThetaDecayStrategy : ICandleStrategy
{
    public TimeSpan Timeframe { get; }

    private readonly decimal _lotteryTicketThreshold;
    private readonly int _flatlinePeriod;
    private readonly decimal _riskPercentage;
    private readonly decimal _stopLossPercentage;

    private readonly Queue<decimal> _closeWindow;
    private bool _hasTraded; // NEW: The Anti-Churn Lock!

    public ThetaDecayStrategy(
        TimeSpan timeframe,
        decimal lotteryTicketThreshold = 0.10m,
        int flatlinePeriod = 24,
        decimal riskPercentage = 0.05m,
        decimal stopLossPercentage = 0.50m)
    {
        Timeframe = timeframe;
        _lotteryTicketThreshold = lotteryTicketThreshold;
        _flatlinePeriod = flatlinePeriod;
        _riskPercentage = riskPercentage;
        _stopLossPercentage = stopLossPercentage;

        _closeWindow = new Queue<decimal>();
        _hasTraded = false; // Start with a clean slate
    }

    public void OnCandle(Candle candle, SimulatedBroker broker)
    {
        _closeWindow.Enqueue(candle.Close);
        if (_closeWindow.Count > _flatlinePeriod) _closeWindow.Dequeue();

        if (_closeWindow.Count >= _flatlinePeriod)
        {
            ExecuteTradeLogic(candle, broker);
        }
    }

    private void ExecuteTradeLogic(Candle candle, SimulatedBroker broker)
    {
        decimal currentNoPrice = 1.00m - candle.Close;
        decimal currentEquity = broker.GetTotalPortfolioValue(candle.Close);
        decimal dollarsToInvest = Math.Min(currentEquity * _riskPercentage, broker.CashBalance);

        // --- THE "NO" SIDE (Acting as the House) ---
        if (broker.NoPositionShares > 0)
        {
            // If a miracle actually happens and the dead asset violently spikes, cut the loss.
            bool isStopLoss = currentNoPrice <= (broker.AverageNoEntryPrice * (1m - _stopLossPercentage));

            // Notice: No Take Profit! We hold until the Stop Loss hits, OR the market expires!
            if (isStopLoss)
                broker.SellAllNo(candle.Close, candle.Volume);
        }
        else if (dollarsToInvest >= 1.00m && !_hasTraded) // NEW: Only enter if we haven't traded this yet!
        {
            bool isFlatlined = _closeWindow.All(price => price <= _lotteryTicketThreshold);

            if (isFlatlined)
            {
                broker.BuyNo(candle.Close, dollarsToInvest, candle.Volume);
                _hasTraded = true; // Lock the door behind us so we never churn!
            }
        }
    }
}