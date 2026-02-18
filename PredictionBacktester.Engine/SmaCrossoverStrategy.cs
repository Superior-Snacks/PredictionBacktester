using System.Collections.Generic;
using System.Linq;
using PredictionBacktester.Core.Entities.Database;

namespace PredictionBacktester.Engine;

public class SmaCrossoverStrategy : IStrategy
{
    private readonly int _fastPeriod;
    private readonly int _slowPeriod;

    // Queues act as our rolling memory windows
    private readonly Queue<decimal> _fastWindow;
    private readonly Queue<decimal> _slowWindow;

    // We must remember the PREVIOUS tick's state to detect a crossover
    private bool? _wasFastAboveSlow;

    public SmaCrossoverStrategy(int fastPeriod = 10, int slowPeriod = 50)
    {
        _fastPeriod = fastPeriod;
        _slowPeriod = slowPeriod;
        _fastWindow = new Queue<decimal>();
        _slowWindow = new Queue<decimal>();
        _wasFastAboveSlow = null;
    }

    public void Execute(Trade tick, SimulatedBroker broker)
    {
        // 1. Update our rolling memory with the newest price
        _fastWindow.Enqueue(tick.Price);
        if (_fastWindow.Count > _fastPeriod)
        {
            _fastWindow.Dequeue(); // Drop the oldest price
        }

        _slowWindow.Enqueue(tick.Price);
        if (_slowWindow.Count > _slowPeriod)
        {
            _slowWindow.Dequeue(); // Drop the oldest price
        }

        // 2. If the slow window isn't full yet, we don't have enough data to trade
        if (_slowWindow.Count < _slowPeriod)
        {
            return;
        }

        // 3. Calculate the current Simple Moving Averages
        decimal fastSma = _fastWindow.Average();
        decimal slowSma = _slowWindow.Average();

        bool isFastAboveSlow = fastSma > slowSma;

        // 4. Check for Momentum Crossovers!
        if (_wasFastAboveSlow.HasValue)
        {
            // THE GOLDEN CROSS: Fast SMA was below, but just crossed ABOVE Slow SMA
            if (_wasFastAboveSlow.Value == false && isFastAboveSlow == true)
            {
                if (broker.PositionShares == 0) // Only buy if we don't already own shares
                {
                    broker.Buy(tick.Price, 100m);
                    Console.WriteLine($"[GOLDEN CROSS] Buy at ${tick.Price:F3} | Fast SMA: {fastSma:F3}, Slow SMA: {slowSma:F3}");
                }
            }

            // THE DEATH CROSS: Fast SMA was above, but just crossed BELOW Slow SMA
            else if (_wasFastAboveSlow.Value == true && isFastAboveSlow == false)
            {
                if (broker.PositionShares > 0) // Only sell if we have something to dump
                {
                    broker.SellAll(tick.Price);
                    Console.WriteLine($"[DEATH CROSS] Sell at ${tick.Price:F3} | Fast SMA: {fastSma:F3}, Slow SMA: {slowSma:F3}");
                }
            }
        }

        // 5. Save the current state so we can compare it against the next tick
        _wasFastAboveSlow = isFastAboveSlow;
    }
}