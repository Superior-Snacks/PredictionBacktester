using PredictionBacktester.Core.Entities.Database;

namespace PredictionBacktester.Engine;

public interface IStrategy
{
    /// <summary>
    /// Evaluates the current market tick and decides whether to buy or sell via the broker.
    /// </summary>
    void Execute(Trade tick, SimulatedBroker broker);
}