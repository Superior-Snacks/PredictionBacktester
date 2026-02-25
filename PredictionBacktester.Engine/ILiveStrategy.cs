using PredictionBacktester.Engine;

namespace PredictionBacktester.Engine;

public interface ILiveStrategy
{
    string StrategyName { get; } // NEW: Identify the strategy!
    void OnBookUpdate(LocalOrderBook book, GlobalSimulatedBroker broker);
}