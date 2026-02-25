using System;
using System.Collections.Generic;
using System.Linq;
using PredictionBacktester.Engine;

namespace PredictionLiveTrader;

public class PaperBroker : GlobalSimulatedBroker
{
    private readonly Dictionary<string, string> _tokenNames;

    // THE FIX: Accept the dictionary of token names
    public PaperBroker(decimal initialCapital, Dictionary<string, string> tokenNames) : base(initialCapital)
    {
        _tokenNames = tokenNames;
    }

    // Helper to format the name cleanly for the console
    private string GetMarketName(string assetId)
    {
        if (_tokenNames.TryGetValue(assetId, out var name))
        {
            return name.Length > 40 ? name.Substring(0, 37) + "..." : name;
        }
        return assetId.Substring(0, 8) + "...";
    }

    public override void Buy(string assetId, decimal price, decimal dollarAmount, decimal volume)
    {
        decimal initialShares = GetPositionShares(assetId);
        base.Buy(assetId, price, dollarAmount, volume);

        if (GetPositionShares(assetId) > initialShares)
        {
            var lastTrade = TradeLedger.Last();
            Console.ForegroundColor = ConsoleColor.Green;
            Console.WriteLine($"\n[{DateTime.Now:HH:mm:ss.fff}] [PAPER EXECUTION] BOUGHT YES @ ${lastTrade.Price:0.00} | Size: ${lastTrade.DollarValue:0.00} | Asset: {GetMarketName(assetId)}");
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
            Console.WriteLine($"\n[{DateTime.Now:HH:mm:ss.fff}] [PAPER CLOSED] SOLD YES @ ${lastTrade.Price:0.00} | PnL: ${(pnl):0.00} | Total Equity: ${GetTotalPortfolioValue():0.00} | Asset: {GetMarketName(assetId)}");
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
            Console.WriteLine($"\n[{DateTime.Now:HH:mm:ss.fff}] [PAPER EXECUTION] BOUGHT NO @ ${lastTrade.Price:0.00} | Size: ${lastTrade.DollarValue:0.00} | Asset: {GetMarketName(assetId)}");
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
            Console.WriteLine($"\n[{DateTime.Now:HH:mm:ss.fff}] [PAPER CLOSED] SOLD NO @ ${lastTrade.Price:0.00} | PnL: ${(pnl):0.00} | Total Equity: ${GetTotalPortfolioValue():0.00} | Asset: {GetMarketName(assetId)}");
            Console.ResetColor();
        }
    }

    public override void ResolveMarket(string assetId, decimal outcomePrice)
    {
        decimal initialYesShares = GetPositionShares(assetId);
        decimal initialNoShares = GetNoPositionShares(assetId);
        decimal yesEntryPrice = GetAverageEntryPrice(assetId);
        decimal noEntryPrice = GetAverageNoEntryPrice(assetId);

        base.ResolveMarket(assetId, outcomePrice);

        if (initialYesShares > 0)
        {
            decimal pnl = (outcomePrice - yesEntryPrice) * initialYesShares;
            Console.ForegroundColor = pnl > 0 ? ConsoleColor.Yellow : ConsoleColor.DarkRed;
            Console.WriteLine($"\n[{DateTime.Now:HH:mm:ss.fff}] [MARKET SETTLED] YES SHARES @ ${outcomePrice:0.00} | PnL: ${(pnl):0.00} | Total Equity: ${GetTotalPortfolioValue():0.00} | Asset: {GetMarketName(assetId)}");
            Console.ResetColor();
        }

        if (initialNoShares > 0)
        {
            decimal noOutcomePrice = 1.00m - outcomePrice;
            decimal pnl = (noOutcomePrice - noEntryPrice) * initialNoShares;
            Console.ForegroundColor = pnl > 0 ? ConsoleColor.Yellow : ConsoleColor.DarkRed;
            Console.WriteLine($"\n[{DateTime.Now:HH:mm:ss.fff}] [MARKET SETTLED] NO SHARES @ ${noOutcomePrice:0.00} | PnL: ${(pnl):0.00} | Total Equity: ${GetTotalPortfolioValue():0.00} | Asset: {GetMarketName(assetId)}");
            Console.ResetColor();
        }
    }
}