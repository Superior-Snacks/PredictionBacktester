using PredictionBacktester.Core.Entities;
using PredictionBacktester.Core.Entities.Database;

namespace PredictionBacktester.Engine;

// The blank parent (The "Plug Socket")
public interface IStrategy { }

// Flavor 1: For your Whale-Tracking / High-Frequency strategies
public interface ITickStrategy : IStrategy
{
    void OnTick(Trade tick, SimulatedBroker broker);
}

// Flavor 2: For your Moving Average / Trend strategies
public interface ICandleStrategy : IStrategy
{
    // The strategy tells the engine what size candles it wants (e.g., 1 Hour)
    TimeSpan Timeframe { get; }

    void OnCandle(Candle candle, SimulatedBroker broker);
}