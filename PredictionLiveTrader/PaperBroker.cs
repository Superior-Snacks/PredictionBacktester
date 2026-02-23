using System;
using System.Linq;
using PredictionBacktester.Engine;

namespace PredictionLiveTrader;

public class PaperBroker : SimulatedBroker
{
    public PaperBroker(decimal initialCapital, string outcomeId) : base(initialCapital, outcomeId)
    {
    }

    public override void Buy(decimal price, decimal dollarAmount, decimal volume)
    {
        decimal initialShares = PositionShares; // Snapshot
        base.Buy(price, dollarAmount, volume);

        // ONLY log if the base broker actually succeeded in buying shares!
        if (PositionShares > initialShares)
        {
            var lastTrade = TradeLedger.Last(); // Grab the true execution price
            Console.ForegroundColor = ConsoleColor.Green;
            Console.WriteLine($"\n[{DateTime.Now:HH:mm:ss.fff}] [PAPER EXECUTION] BOUGHT YES @ ${lastTrade.Price:0.00} | Size: ${lastTrade.DollarValue:0.00} | Asset: {OutcomeId.Substring(0, 8)}...");
            Console.ResetColor();
        }
    }

    public override void SellAll(decimal price, decimal volume)
    {
        decimal initialShares = PositionShares; // Snapshot
        decimal entryPrice = AverageEntryPrice;

        base.SellAll(price, volume);

        // ONLY log if the base broker actually succeeded in selling shares!
        if (PositionShares < initialShares)
        {
            var lastTrade = TradeLedger.Last();
            // Calculate accurate PnL using the real execution price and amount of shares sold
            decimal pnl = (lastTrade.Price - entryPrice) * (initialShares - PositionShares);

            Console.ForegroundColor = pnl >= 0 ? ConsoleColor.Cyan : ConsoleColor.Red;
            Console.WriteLine($"\n[{DateTime.Now:HH:mm:ss.fff}] [PAPER CLOSED] SOLD YES @ ${lastTrade.Price:0.00} | PnL: ${(pnl):0.00} | Total Equity: ${CashBalance:0.00} | Asset: {OutcomeId.Substring(0, 8)}...");
            Console.ResetColor();
        }
    }

    public override void BuyNo(decimal price, decimal dollarAmount, decimal volume)
    {
        decimal initialShares = NoPositionShares;
        base.BuyNo(price, dollarAmount, volume);

        if (NoPositionShares > initialShares)
        {
            var lastTrade = TradeLedger.Last();
            Console.ForegroundColor = ConsoleColor.Magenta;
            Console.WriteLine($"\n[{DateTime.Now:HH:mm:ss.fff}] [PAPER EXECUTION] BOUGHT NO @ ${lastTrade.Price:0.00} | Size: ${lastTrade.DollarValue:0.00} | Asset: {OutcomeId.Substring(0, 8)}...");
            Console.ResetColor();
        }
    }

    public override void SellAllNo(decimal price, decimal volume)
    {
        decimal initialShares = NoPositionShares;
        decimal entryPrice = AverageNoEntryPrice;

        base.SellAllNo(price, volume);

        if (NoPositionShares < initialShares)
        {
            var lastTrade = TradeLedger.Last();
            decimal pnl = (lastTrade.Price - entryPrice) * (initialShares - NoPositionShares);

            Console.ForegroundColor = pnl >= 0 ? ConsoleColor.Cyan : ConsoleColor.Red;
            Console.WriteLine($"\n[{DateTime.Now:HH:mm:ss.fff}] [PAPER CLOSED] SOLD NO @ ${lastTrade.Price:0.00} | PnL: ${(pnl):0.00} | Total Equity: ${CashBalance:0.00} | Asset: {OutcomeId.Substring(0, 8)}...");
            Console.ResetColor();
        }
    }
}