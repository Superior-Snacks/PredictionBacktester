using System;
using System.Linq;
using PredictionBacktester.Engine;

namespace PredictionLiveTrader;

public class PaperBroker : GlobalSimulatedBroker
{
    public PaperBroker(decimal initialCapital) : base(initialCapital)
    {
    }

    public override void Buy(string assetId, decimal price, decimal dollarAmount, decimal volume)
    {
        decimal initialShares = GetPositionShares(assetId);
        base.Buy(assetId, price, dollarAmount, volume);

        if (GetPositionShares(assetId) > initialShares)
        {
            var lastTrade = TradeLedger.Last();
            Console.ForegroundColor = ConsoleColor.Green;
            Console.WriteLine($"\n[{DateTime.Now:HH:mm:ss.fff}] [PAPER EXECUTION] BOUGHT YES @ ${lastTrade.Price:0.00} | Size: ${lastTrade.DollarValue:0.00} | Asset: {assetId.Substring(0, 8)}...");
            Console.ResetColor();
        }
    }

    public override void SellAll(string assetId, decimal price, decimal volume)
    {
        decimal initialShares = GetPositionShares(assetId);
        decimal entryPrice = GetAverageEntryPrice(assetId);

        base.SellAll(assetId, price, volume);

        if (GetPositionShares(assetId) < initialShares)
        {
            var lastTrade = TradeLedger.Last();
            decimal pnl = (lastTrade.Price - entryPrice) * (initialShares - GetPositionShares(assetId));

            Console.ForegroundColor = pnl >= 0 ? ConsoleColor.Cyan : ConsoleColor.Red;
            Console.WriteLine($"\n[{DateTime.Now:HH:mm:ss.fff}] [PAPER CLOSED] SOLD YES @ ${lastTrade.Price:0.00} | PnL: ${(pnl):0.00} | Total Equity: ${GetTotalPortfolioValue():0.00} | Asset: {assetId.Substring(0, 8)}...");
            Console.ResetColor();
        }
    }

    public override void BuyNo(string assetId, decimal price, decimal dollarAmount, decimal volume)
    {
        decimal initialShares = GetNoPositionShares(assetId);
        base.BuyNo(assetId, price, dollarAmount, volume);

        if (GetNoPositionShares(assetId) > initialShares)
        {
            var lastTrade = TradeLedger.Last();
            Console.ForegroundColor = ConsoleColor.Magenta;
            Console.WriteLine($"\n[{DateTime.Now:HH:mm:ss.fff}] [PAPER EXECUTION] BOUGHT NO @ ${lastTrade.Price:0.00} | Size: ${lastTrade.DollarValue:0.00} | Asset: {assetId.Substring(0, 8)}...");
            Console.ResetColor();
        }
    }

    public override void SellAllNo(string assetId, decimal price, decimal volume)
    {
        decimal initialShares = GetNoPositionShares(assetId);
        decimal entryPrice = GetAverageNoEntryPrice(assetId);

        base.SellAllNo(assetId, price, volume);

        if (GetNoPositionShares(assetId) < initialShares)
        {
            var lastTrade = TradeLedger.Last();
            decimal pnl = (lastTrade.Price - entryPrice) * (initialShares - GetNoPositionShares(assetId));

            Console.ForegroundColor = pnl >= 0 ? ConsoleColor.Cyan : ConsoleColor.Red;
            Console.WriteLine($"\n[{DateTime.Now:HH:mm:ss.fff}] [PAPER CLOSED] SOLD NO @ ${lastTrade.Price:0.00} | PnL: ${(pnl):0.00} | Total Equity: ${GetTotalPortfolioValue():0.00} | Asset: {assetId.Substring(0, 8)}...");
            Console.ResetColor();
        }
    }
}