using System;
using System.Collections.Generic;
using System.Linq;
using PredictionBacktester.Core.Entities;
using PredictionBacktester.Engine;

namespace PredictionBacktester.Strategies;

public class BollingerBreakoutStrategy : ICandleStrategy
{
    public TimeSpan Timeframe { get; }

    private readonly int _period;
    private readonly decimal _multiplier;
    private readonly decimal _riskPercentage;
    private readonly int _volumePeriod;
    private readonly decimal _minDollarVolume;
    private readonly decimal _takeProfitThreshold;
    private readonly decimal _stopLossPercentage;

    private readonly Queue<decimal> _volumeWindow;
    private readonly Queue<decimal> _closeWindow;

    public BollingerBreakoutStrategy(
        TimeSpan timeframe,
        int period = 20,
        decimal multiplier = 2.0m, // How many standard deviations wide the bands are
        decimal riskPercentage = 0.05m,
        int volumePeriod = 24,
        decimal minDollarVolume = 10000m,
        decimal takeProfitThreshold = 0.85m,
        decimal stopLossPercentage = 0.20m)
    {
        Timeframe = timeframe;
        _period = period;
        _multiplier = multiplier;

        _riskPercentage = riskPercentage;
        _volumePeriod = volumePeriod;
        _minDollarVolume = minDollarVolume;
        _takeProfitThreshold = takeProfitThreshold;
        _stopLossPercentage = stopLossPercentage;

        _volumeWindow = new Queue<decimal>();
        _closeWindow = new Queue<decimal>();
    }

    public void OnCandle(Candle candle, SimulatedBroker broker)
    {
        // 1. Maintain Volume Filter
        _volumeWindow.Enqueue(candle.Volume * candle.Close);
        if (_volumeWindow.Count > _volumePeriod) _volumeWindow.Dequeue();

        // 2. Maintain Close Prices for Bollinger Math
        _closeWindow.Enqueue(candle.Close);
        if (_closeWindow.Count > _period) _closeWindow.Dequeue();

        // Wait until we have enough data to calculate the bands
        if (_closeWindow.Count >= _period)
        {
            ExecuteTradeLogic(candle, broker);
        }
    }

    private void ExecuteTradeLogic(Candle candle, SimulatedBroker broker)
    {
        // --- BOLLINGER BAND MATH ---
        decimal sma = _closeWindow.Average();

        decimal sumOfSquares = _closeWindow.Select(val => (val - sma) * (val - sma)).Sum();
        decimal standardDeviation = (decimal)Math.Sqrt((double)(sumOfSquares / _closeWindow.Count));

        decimal upperBand = sma + (_multiplier * standardDeviation);
        decimal lowerBand = sma - (_multiplier * standardDeviation);
        // ---------------------------

        decimal currentNoPrice = 1.00m - candle.Close;
        decimal currentEquity = broker.GetTotalPortfolioValue(candle.Close);
        decimal dollarsToInvest = Math.Min(currentEquity * _riskPercentage, broker.CashBalance);
        bool hasLiquidity = (_volumeWindow.Sum() >= _minDollarVolume);

        // --- THE "YES" SIDE (Fading the Crash) ---
        if (broker.PositionShares > 0)
        {
            bool isStopLoss = candle.Close <= (broker.AverageEntryPrice * (1m - _stopLossPercentage));

            // NEW EXIT: Take profit the exact second the price snaps back up to the moving average
            bool reversionHit = candle.Close >= sma;

            if (candle.Close >= _takeProfitThreshold || isStopLoss || reversionHit)
                broker.SellAll(candle.Close, candle.Volume);
        }
        else if (candle.Close < lowerBand && dollarsToInvest >= 1.00m && hasLiquidity)
        {
            // THE FLIP: Price violently crashed BELOW the lower band. Buy the panic!
            broker.Buy(candle.Close, dollarsToInvest, candle.Volume);
        }

        // --- THE "NO" SIDE (Fading the Hype) ---
        if (broker.NoPositionShares > 0)
        {
            bool isNoStopLoss = currentNoPrice <= (broker.AverageNoEntryPrice * (1m - _stopLossPercentage));

            // NEW EXIT: Take profit the exact second the price crashes back down to the moving average
            bool noReversionHit = candle.Close <= sma;

            if (currentNoPrice >= _takeProfitThreshold || isNoStopLoss || noReversionHit)
                broker.SellAllNo(candle.Close, candle.Volume);
        }
        else if (candle.Close > upperBand && dollarsToInvest >= 1.00m && hasLiquidity)
        {
            // THE FLIP: Price violently spiked ABOVE the upper band. Short the hype! (Buy NO)
            broker.BuyNo(candle.Close, dollarsToInvest, candle.Volume);
        }
    }
}