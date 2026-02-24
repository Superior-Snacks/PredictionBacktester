using PredictionBacktester.Engine;

namespace PredictionBacktester.Engine;

public interface ILiveStrategy
{
    // Every live strategy MUST implement this method!
    void OnBookUpdate(LocalOrderBook book, GlobalSimulatedBroker broker);
}