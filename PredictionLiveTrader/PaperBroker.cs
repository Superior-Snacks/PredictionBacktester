using System;
using PredictionBacktester.Engine;

namespace PredictionLiveTrader;

public class PaperBroker : SimulatedBroker
{
    // UPDATE: Added outcomeId parameter so the base broker knows what it's trading
    public PaperBroker(decimal initialCapital, string outcomeId) : base(initialCapital, outcomeId)
    {
    }

    public override void Buy(decimal price, decimal dollarAmount, decimal volume)
    {
        base.Buy(price, dollarAmount, volume);
        Console.ForegroundColor = ConsoleColor.Green;
        Console.WriteLine($"\n[{DateTime.Now:HH:mm:ss.fff}] [PAPER EXECUTION] BOUGHT YES @ ${price:0.00} | Size: ${dollarAmount:0.00} | Asset: {OutcomeId.Substring(0, 8)}...");
        Console.ResetColor();
    }

    public override void SellAll(decimal price, decimal volume)
    {
        decimal pnl = (price - AverageEntryPrice) * TotalTradesExecuted; // Modified slightly for proper tracking
        base.SellAll(price, volume);

        Console.ForegroundColor = pnl >= 0 ? ConsoleColor.Cyan : ConsoleColor.Red;
        Console.WriteLine($"\n[{DateTime.Now:HH:mm:ss.fff}] [PAPER CLOSED] SOLD YES @ ${price:0.00} | PnL: ${(pnl):0.00} | Total Equity: ${CashBalance:0.00} | Asset: {OutcomeId.Substring(0, 8)}...");
        Console.ResetColor();
    }

    public override void BuyNo(decimal price, decimal dollarAmount, decimal volume)
    {
        base.BuyNo(price, dollarAmount, volume);
        Console.ForegroundColor = ConsoleColor.Magenta;
        Console.WriteLine($"\n[{DateTime.Now:HH:mm:ss.fff}] [PAPER EXECUTION] BOUGHT NO @ ${(1m - price):0.00} | Size: ${dollarAmount:0.00} | Asset: {OutcomeId.Substring(0, 8)}...");
        Console.ResetColor();
    }

    public override void SellAllNo(decimal price, decimal volume)
    {
        decimal exitNoPrice = 1.00m - price;
        decimal pnl = (exitNoPrice - AverageNoEntryPrice) * TotalTradesExecuted;
        base.SellAllNo(price, volume);

        Console.ForegroundColor = pnl >= 0 ? ConsoleColor.Cyan : ConsoleColor.Red;
        Console.WriteLine($"\n[{DateTime.Now:HH:mm:ss.fff}] [PAPER CLOSED] SOLD NO @ ${exitNoPrice:0.00} | PnL: ${(pnl):0.00} | Total Equity: ${CashBalance:0.00} | Asset: {OutcomeId.Substring(0, 8)}...");
        Console.ResetColor();
    }
}