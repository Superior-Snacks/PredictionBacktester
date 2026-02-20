using System;
using System.Collections.Generic;
using System.Linq;
using PredictionBacktester.Core.Entities;
using PredictionBacktester.Engine;

namespace PredictionBacktester.Strategies;

public class HybridConfluenceStrategy : ICandleStrategy
{
    public TimeSpan Timeframe { get; }

    private readonly int _rsiPeriod;
    private readonly int _smaPeriod;
    private readonly decimal _oversoldThreshold;
    private readonly decimal _overboughtThreshold;
    private readonly decimal _riskPercentage;
    private readonly int _volumePeriod;
    private readonly decimal _minDollarVolume;
    private readonly decimal _takeProfitThreshold;

    private readonly Queue<decimal> _volumeWindow;
    private readonly Queue<decimal> _closeWindow; // For the SMA Compass
    private decimal _previousClose;
    private decimal _averageGain;
    private decimal _averageLoss;
    private int _candleCount;
    private decimal _currentRsi;

    public HybridConfluenceStrategy(
        TimeSpan timeframe,
        int rsiPeriod = 14,
        int smaPeriod = 50, // The Compass!
        decimal oversold = 30m,
        decimal overbought = 70m,
        decimal riskPercentage = 0.05m,
        int volumePeriod = 24,
        decimal minDollarVolume = 10000m,
        decimal takeProfitThreshold = 0.85m)
    {
        Timeframe = timeframe;
        _rsiPeriod = rsiPeriod;
        _smaPeriod = smaPeriod;
        _oversoldThreshold = oversold;
        _overboughtThreshold = overbought;
        _riskPercentage = riskPercentage;
        _volumePeriod = volumePeriod;
        _minDollarVolume = minDollarVolume;
        _takeProfitThreshold = takeProfitThreshold;

        _volumeWindow = new Queue<decimal>();
        _closeWindow = new Queue<decimal>();
        _previousClose = 0;
        _averageGain = 0;
        _averageLoss = 0;
        _candleCount = 0;
        _currentRsi = 50m;
    }

    public void OnCandle(Candle candle, SimulatedBroker broker)
    {
        // 1. Maintain Volume Filter
        _volumeWindow.Enqueue(candle.Volume * candle.Close);
        if (_volumeWindow.Count > _volumePeriod) _volumeWindow.Dequeue();

        // 2. Maintain SMA Compass
        _closeWindow.Enqueue(candle.Close);
        if (_closeWindow.Count > _smaPeriod) _closeWindow.Dequeue();

        // 3. Maintain RSI Trigger
        if (_previousClose > 0)
        {
            decimal change = candle.Close - _previousClose;
            decimal gain = Math.Max(change, 0);
            decimal loss = Math.Max(-change, 0);

            if (_candleCount < _rsiPeriod)
            {
                _averageGain = ((_averageGain * _candleCount) + gain) / (_candleCount + 1);
                _averageLoss = ((_averageLoss * _candleCount) + loss) / (_candleCount + 1);
            }
            else
            {
                _averageGain = ((_averageGain * (_rsiPeriod - 1)) + gain) / _rsiPeriod;
                _averageLoss = ((_averageLoss * (_rsiPeriod - 1)) + loss) / _rsiPeriod;
            }

            if (_averageLoss == 0) _currentRsi = 100m;
            else
            {
                decimal rs = _averageGain / _averageLoss;
                _currentRsi = 100m - (100m / (1m + rs));
            }
        }

        _previousClose = candle.Close;
        _candleCount++;

        // Wait until both the RSI and the SMA have enough data to fire
        if (_candleCount >= Math.Max(_rsiPeriod, _smaPeriod))
        {
            ExecuteTradeLogic(candle, broker);
        }
    }

    private void ExecuteTradeLogic(Candle candle, SimulatedBroker broker)
    {
        decimal currentNoPrice = 1.00m - candle.Close;
        decimal currentEquity = broker.GetTotalPortfolioValue(candle.Close);
        decimal dollarsToInvest = Math.Min(currentEquity * _riskPercentage, broker.CashBalance);
        bool hasLiquidity = (_volumeWindow.Sum() >= _minDollarVolume);

        // Read the gauges!
        decimal currentSma = _closeWindow.Average();
        bool isOversold = _currentRsi < _oversoldThreshold;
        bool isOverbought = _currentRsi > _overboughtThreshold;

        // --- THE CONFLUENCE CHECK ---
        bool isUptrend = candle.Close > currentSma;
        bool isDowntrend = candle.Close < currentSma;

        // --- THE "YES" SIDE ---
        if (broker.PositionShares > 0)
        {
            if (isOverbought || candle.Close >= _takeProfitThreshold)
                broker.SellAll(candle.Close, candle.Volume);
        }
        else if (isOversold && isUptrend && dollarsToInvest >= 1.00m && hasLiquidity) // REQUIRE UPTREND
        {
            broker.Buy(candle.Close, dollarsToInvest, candle.Volume);
        }

        // --- THE "NO" SIDE ---
        if (broker.NoPositionShares > 0)
        {
            if (isOversold || currentNoPrice >= _takeProfitThreshold)
                broker.SellAllNo(candle.Close, candle.Volume);
        }
        else if (isOverbought && isDowntrend && dollarsToInvest >= 1.00m && hasLiquidity) // REQUIRE DOWNTREND
        {
            broker.BuyNo(candle.Close, dollarsToInvest, candle.Volume);
        }
    }
}