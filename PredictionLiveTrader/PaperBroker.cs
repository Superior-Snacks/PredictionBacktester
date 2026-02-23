using System;
using PredictionBacktester.Core.Entities.Database; // To use the Trade object
using PredictionBacktester.Engine; // To implement IBroker (if you extracted an interface, otherwise just match the methods)

namespace PredictionLiveTrader;

public class PaperBroker : SimulatedBroker
{
    public PaperBroker(decimal initialCapital) : base(initialCapital)
    {
    }

    // We override the Buy and Sell methods to print live console alerts!
    public override void Buy(decimal price, decimal dollarAmount, decimal volume)
    {
        base.Buy(price, dollarAmount, volume);
        Console.ForegroundColor = ConsoleColor.Green;
        Console.WriteLine($"[{DateTime.Now:HH:mm:ss.fff}] [PAPER EXECUTION] BOUGHT YES @ ${price:0.00} | Size: ${dollarAmount:0.00}");
        Console.ResetColor();
    }

    public override void SellAll(decimal price, decimal volume)
    {
        decimal pnl = (price - AverageEntryPrice) * PositionShares;
        base.SellAll(price, volume);

        Console.ForegroundColor = pnl >= 0 ? ConsoleColor.Cyan : ConsoleColor.Red;
        Console.WriteLine($"[{DateTime.Now:HH:mm:ss.fff}] [PAPER CLOSED] SOLD YES @ ${price:0.00} | PnL: ${(pnl):0.00} | Total Equity: ${CashBalance:0.00}");
        Console.ResetColor();
    }

    public override void BuyNo(decimal price, decimal dollarAmount, decimal volume)
    {
        base.BuyNo(price, dollarAmount, volume);
        Console.ForegroundColor = ConsoleColor.Magenta;
        Console.WriteLine($"[{DateTime.Now:HH:mm:ss.fff}] [PAPER EXECUTION] BOUGHT NO @ ${(1m - price):0.00} | Size: ${dollarAmount:0.00}");
        Console.ResetColor();
    }

    public override void SellAllNo(decimal price, decimal volume)
    {
        decimal exitNoPrice = 1.00m - price;
        decimal pnl = (exitNoPrice - AverageNoEntryPrice) * NoPositionShares;
        base.SellAllNo(price, volume);

        Console.ForegroundColor = pnl >= 0 ? ConsoleColor.Cyan : ConsoleColor.Red;
        Console.WriteLine($"[{DateTime.Now:HH:mm:ss.fff}] [PAPER CLOSED] SOLD NO @ ${exitNoPrice:0.00} | PnL: ${(pnl):0.00} | Total Equity: ${CashBalance:0.00}");
        Console.ResetColor();
    }
}