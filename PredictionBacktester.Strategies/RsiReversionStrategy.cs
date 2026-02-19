using System;
using System.Collections.Generic;
using System.Linq;
using PredictionBacktester.Core.Entities;
using PredictionBacktester.Engine;

namespace PredictionBacktester.Strategies;

public class RsiReversionStrategy : ICandleStrategy
{
    public TimeSpan Timeframe { get; }

    private readonly int _rsiPeriod;
    private readonly decimal _oversoldThreshold;
    private readonly decimal _overboughtThreshold;

    private readonly decimal _riskPercentage;
    private readonly int _volumePeriod;
    private readonly decimal _minDollarVolume;
    private readonly decimal _takeProfitThreshold;

    private readonly Queue<decimal> _volumeWindow;
    private decimal _previousClose;
    private int _candleCount;

    // RSI Math state
    private decimal _averageGain;
    private decimal _averageLoss;
    private decimal _currentRsi;

    public RsiReversionStrategy(
        TimeSpan timeframe,
        int rsiPeriod = 14,
        decimal oversold = 30m,
        decimal overbought = 70m,
        decimal riskPercentage = 0.05m, // Defaulting to your new 5% optimal risk!
        int volumePeriod = 24,
        decimal minDollarVolume = 10000m,
        decimal takeProfitThreshold = 0.85m) // Defaulting to your 85-cent sweet spot!
    {
        Timeframe = timeframe;
        _rsiPeriod = rsiPeriod;
        _oversoldThreshold = oversold;
        _overboughtThreshold = overbought;

        _riskPercentage = riskPercentage;
        _volumePeriod = volumePeriod;
        _minDollarVolume = minDollarVolume;
        _takeProfitThreshold = takeProfitThreshold;

        _volumeWindow = new Queue<decimal>();
        _previousClose = 0;
        _candleCount = 0;
        _currentRsi = 50m; // Neutral starting point
    }

    public void OnCandle(Candle candle, SimulatedBroker broker)
    {
        // 1. UPDATE VOLUME MEMORY
        _volumeWindow.Enqueue(candle.Volume * candle.Close);
        if (_volumeWindow.Count > _volumePeriod) _volumeWindow.Dequeue();

        // 2. CALCULATE RSI (Wilder's Smoothing Method)
        if (_candleCount == 0)
        {
            _previousClose = candle.Close;
            _candleCount++;
            return;
        }

        decimal change = candle.Close - _previousClose;
        decimal gain = change > 0 ? change : 0;
        decimal loss = change < 0 ? -change : 0;

        if (_candleCount <= _rsiPeriod)
        {
            // Simple moving average for the first 'N' periods
            _averageGain = ((_averageGain * (_candleCount - 1)) + gain) / _candleCount;
            _averageLoss = ((_averageLoss * (_candleCount - 1)) + loss) / _candleCount;
        }
        else
        {
            // Wilder's Smoothing for all subsequent periods
            _averageGain = ((_averageGain * (_rsiPeriod - 1)) + gain) / _rsiPeriod;
            _averageLoss = ((_averageLoss * (_rsiPeriod - 1)) + loss) / _rsiPeriod;
        }

        _previousClose = candle.Close;
        _candleCount++;

        if (_candleCount > _rsiPeriod)
        {
            if (_averageLoss == 0) _currentRsi = 100m;
            else
            {
                decimal rs = _averageGain / _averageLoss;
                _currentRsi = 100m - (100m / (1m + rs));
            }

            ExecuteTradeLogic(candle, broker);
        }
    }

    private void ExecuteTradeLogic(Candle candle, SimulatedBroker broker)
    {
        decimal currentNoPrice = 1.00m - candle.Close;
        decimal currentEquity = broker.GetTotalPortfolioValue(candle.Close);
        decimal dollarsToInvest = Math.Min(currentEquity * _riskPercentage, broker.CashBalance);
        bool hasLiquidity = (_volumeWindow.Sum() >= _minDollarVolume);

        bool isOversold = _currentRsi < _oversoldThreshold;   // YES price crashed
        bool isOverbought = _currentRsi > _overboughtThreshold; // YES price spiked

        // --- THE "YES" SIDE ---
        if (broker.PositionShares > 0)
        {
            if (isOverbought || candle.Close >= _takeProfitThreshold) broker.SellAll(candle.Close, candle.Volume);
        }
        else if (isOversold && dollarsToInvest >= 1.00m && hasLiquidity)
        {
            broker.Buy(candle.Close, dollarsToInvest, candle.Volume);
        }

        // --- THE "NO" SIDE ---
        if (broker.NoPositionShares > 0)
        {
            if (isOversold || currentNoPrice >= _takeProfitThreshold) broker.SellAllNo(candle.Close, candle.Volume);
        }
        else if (isOverbought && dollarsToInvest >= 1.00m && hasLiquidity)
        {
            // If YES is Overbought (too high), we buy NO expecting a crash!
            broker.BuyNo(candle.Close, dollarsToInvest, candle.Volume);
        }
    }
}