using System;
using System.Collections.Generic;
using System.Linq;
using PredictionBacktester.Core.Entities;
using PredictionBacktester.Engine;

namespace PredictionBacktester.Strategies;

public class CandleSmaCrossoverStrategy : ICandleStrategy
{
    public TimeSpan Timeframe { get; }

    private readonly int _fastPeriod;
    private readonly int _slowPeriod;
    private readonly decimal _riskPercentage;

    private readonly int _volumePeriod;
    private readonly decimal _minDollarVolume;

    // --- NEW: TAKE PROFIT THRESHOLD ---
    private readonly decimal _takeProfitThreshold;

    private readonly Queue<decimal> _volumeWindow;
    private readonly Queue<decimal> _fastWindow;
    private readonly Queue<decimal> _slowWindow;
    private bool? _wasFastAboveSlow;

    public CandleSmaCrossoverStrategy(
        TimeSpan timeframe,
        int fastPeriod = 10,
        int slowPeriod = 50,
        decimal riskPercentage = 0.02m,
        int volumePeriod = 24,
        decimal minDollarVolume = 10000m,
        decimal takeProfitThreshold = 0.90m) // Defaulting to 90 cents!
    {
        Timeframe = timeframe;
        _fastPeriod = fastPeriod;
        _slowPeriod = slowPeriod;
        _riskPercentage = riskPercentage;

        _volumePeriod = volumePeriod;
        _minDollarVolume = minDollarVolume;
        _takeProfitThreshold = takeProfitThreshold;

        _volumeWindow = new Queue<decimal>();
        _fastWindow = new Queue<decimal>();
        _slowWindow = new Queue<decimal>();
        _wasFastAboveSlow = null;
    }

    public void OnCandle(Candle candle, SimulatedBroker broker)
    {
        // 1. UPDATE ROLLING VOLUME MEMORY
        decimal candleDollarVolume = candle.Volume * candle.Close;
        _volumeWindow.Enqueue(candleDollarVolume);
        if (_volumeWindow.Count > _volumePeriod) _volumeWindow.Dequeue();

        // 2. UPDATE SMA MEMORY
        _fastWindow.Enqueue(candle.Close);
        if (_fastWindow.Count > _fastPeriod) _fastWindow.Dequeue();

        _slowWindow.Enqueue(candle.Close);
        if (_slowWindow.Count > _slowPeriod) _slowWindow.Dequeue();

        if (_slowWindow.Count < _slowPeriod) return;

        // 3. CALCULATE METRICS
        decimal fastSma = _fastWindow.Average();
        decimal slowSma = _slowWindow.Average();
        decimal rollingDollarVolume = _volumeWindow.Sum();

        bool isFastAboveSlow = fastSma > slowSma;

        // 4. OMNIDIRECTIONAL TRADE LOGIC
        if (_wasFastAboveSlow.HasValue)
        {
            bool isGoldenCross = (_wasFastAboveSlow.Value == false && isFastAboveSlow == true);
            bool isDeathCross = (_wasFastAboveSlow.Value == true && isFastAboveSlow == false);

            decimal currentNoPrice = 1.00m - candle.Close;
            decimal currentEquity = broker.GetTotalPortfolioValue(candle.Close);
            decimal dollarsToInvest = Math.Min(currentEquity * _riskPercentage, broker.CashBalance);
            bool hasLiquidity = (rollingDollarVolume >= _minDollarVolume);

            // --- THE "YES" SIDE ---
            if (broker.PositionShares > 0)
            {
                bool isYesTakeProfit = (candle.Close >= _takeProfitThreshold);
                // If trend dies OR we hit our profit target, dump YES bags!
                if (isDeathCross || isYesTakeProfit) broker.SellAll(candle.Close, candle.Volume);
            }
            else if (isGoldenCross && dollarsToInvest >= 1.00m && hasLiquidity)
            {
                // Trend is up, buy YES!
                broker.Buy(candle.Close, dollarsToInvest, candle.Volume);
            }

            // --- THE "NO" SIDE ---
            if (broker.NoPositionShares > 0)
            {
                bool isNoTakeProfit = (currentNoPrice >= _takeProfitThreshold);
                // If trend reverses UP OR our NO shares hit the profit target, dump NO bags!
                if (isGoldenCross || isNoTakeProfit) broker.SellAllNo(candle.Close, candle.Volume);
            }
            else if (isDeathCross && dollarsToInvest >= 1.00m && hasLiquidity)
            {
                // Trend is down, buy NO!
                broker.BuyNo(candle.Close, dollarsToInvest, candle.Volume);
            }
        }

        _wasFastAboveSlow = isFastAboveSlow;
    }
}