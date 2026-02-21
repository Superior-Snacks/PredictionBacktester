using System;
using System.Collections.Generic;
using PredictionBacktester.Core.Entities;
using PredictionBacktester.Engine;

namespace PredictionBacktester.Strategies;

public class PureBuyNoStrategy : ICandleStrategy
{
    public TimeSpan Timeframe { get; }
    private readonly decimal _riskPercentage;
    private bool _hasTraded;

    public PureBuyNoStrategy(TimeSpan timeframe, decimal riskPercentage = 0.05m)
    {
        Timeframe = timeframe;
        _riskPercentage = riskPercentage;
        _hasTraded = false;
    }

    public void OnCandle(Candle candle, SimulatedBroker broker)
    {
        // We only want to execute on the very first candle we ever see
        if (!_hasTraded)
        {
            decimal currentEquity = broker.GetTotalPortfolioValue(candle.Close);
            decimal dollarsToInvest = Math.Min(currentEquity * _riskPercentage, broker.CashBalance);

            // Do not buy NO if it's already basically $1.00 (too much risk, zero reward)
            decimal currentNoPrice = 1.00m - candle.Close;
            if (currentNoPrice < 0.98m && dollarsToInvest >= 1.00m)
            {
                broker.BuyNo(candle.Close, dollarsToInvest, candle.Volume);
                _hasTraded = true; // Lock it. We hold this to the grave.
            }
        }
    }
}