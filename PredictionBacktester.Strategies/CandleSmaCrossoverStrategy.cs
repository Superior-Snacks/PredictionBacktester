using System;
using System.Collections.Generic;
using System.Linq;
using PredictionBacktester.Core.Entities;
using PredictionBacktester.Engine; // References your broker and interfaces

namespace PredictionBacktester.Strategies;

public class CandleSmaCrossoverStrategy : ICandleStrategy
{
    // The engine will read this property to know how to group the ticks!
    public TimeSpan Timeframe { get; }

    private readonly int _fastPeriod;
    private readonly int _slowPeriod;
    private readonly decimal _riskPercentage;

    private readonly Queue<decimal> _fastWindow;
    private readonly Queue<decimal> _slowWindow;

    private bool? _wasFastAboveSlow;

    public CandleSmaCrossoverStrategy(TimeSpan timeframe, int fastPeriod = 10, int slowPeriod = 50, decimal riskPercentage = 0.02m)
    {
        Timeframe = timeframe;
        _fastPeriod = fastPeriod;
        _slowPeriod = slowPeriod;
        _riskPercentage = riskPercentage;

        _fastWindow = new Queue<decimal>();
        _slowWindow = new Queue<decimal>();
        _wasFastAboveSlow = null;
    }

    public void OnCandle(Candle candle, SimulatedBroker broker)
    {
        // 1. In Candlestick trading, moving averages are almost always calculated using the CLOSE price
        _fastWindow.Enqueue(candle.Close);
        if (_fastWindow.Count > _fastPeriod) _fastWindow.Dequeue();

        _slowWindow.Enqueue(candle.Close);
        if (_slowWindow.Count > _slowPeriod) _slowWindow.Dequeue();

        if (_slowWindow.Count < _slowPeriod) return;

        // 2. Calculate SMAs based on the hourly candle closes
        decimal fastSma = _fastWindow.Average();
        decimal slowSma = _slowWindow.Average();


        bool isFastAboveSlow = fastSma > slowSma;

        // 3. Trade Logic
        if (_wasFastAboveSlow.HasValue)
        {
            if (_wasFastAboveSlow.Value == false && isFastAboveSlow == true)
            {
                if (broker.PositionShares == 0)
                {
                    decimal currentEquity = broker.GetTotalPortfolioValue(candle.Close);
                    decimal dollarsToInvest = currentEquity * _riskPercentage;
                    dollarsToInvest = Math.Min(dollarsToInvest, broker.CashBalance);

                    // NEW: Only place the bet if we are risking at least $1.00
                    if (dollarsToInvest >= 1.00m)
                    {
                        broker.Buy(candle.Close, dollarsToInvest);

                        var date = DateTimeOffset.FromUnixTimeSeconds(candle.OpenTimestamp).DateTime;
                        Console.WriteLine($"[BUY] {date} | Price: ${candle.Close:F3} | Size: ${dollarsToInvest:F2}");
                    }
                }
            }
            else if (_wasFastAboveSlow.Value == true && isFastAboveSlow == false)
            {
                if (broker.PositionShares > 0)
                {
                    broker.SellAll(candle.Close);
                    var date = DateTimeOffset.FromUnixTimeSeconds(candle.OpenTimestamp).DateTime;
                    Console.WriteLine($"[CANDLE DEATH CROSS] {date} | Sell at ${candle.Close:F3}");
                }
            }
        }

        _wasFastAboveSlow = isFastAboveSlow;
    }
}