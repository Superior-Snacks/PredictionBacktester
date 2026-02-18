using PredictionBacktester.Core.Entities.Database;
using PredictionBacktester.Engine;
namespace PredictionBacktester.Strategies;

public class DipBuyingStrategy : IStrategy
{
    public void OnTick(Trade tick, SimulatedBroker broker)
    {
        // If we have no shares and the price dips below 40 cents, BUY $100 worth!
        if (broker.PositionShares == 0 && tick.Price < 0.40m)
        {
            broker.Buy(tick.Price, 100m);
            Console.WriteLine($"[BUY] Bought shares at ${tick.Price}. Remaining Cash: ${broker.CashBalance:F2}");
        }

        // If we hold shares and the price spikes above 60 cents, SELL EVERYTHING!
        else if (broker.PositionShares > 0 && tick.Price > 0.60m)
        {
            broker.SellAll(tick.Price);
            Console.WriteLine($"[SELL] Sold shares at ${tick.Price}. New Cash: ${broker.CashBalance:F2}");
        }
    }
}