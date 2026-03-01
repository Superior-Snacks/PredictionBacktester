using System;
using System.Collections.Generic;
using System.Linq;
using PredictionBacktester.Engine;

namespace PredictionLiveTrader;

public class PaperBroker : GlobalSimulatedBroker
{
    public string StrategyName { get; }
    private readonly Dictionary<string, string> _tokenNames;

    // THE FIX: Accept the dictionary of token names
    public PaperBroker(string strategyName, decimal initialCapital, Dictionary<string, string> tokenNames) : base(initialCapital)
    {
        StrategyName = strategyName;
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

    public override decimal Buy(string assetId, decimal price, decimal dollarAmount, decimal volume)
    {
        decimal initialShares = GetPositionShares(assetId);
        decimal filled = base.Buy(assetId, price, dollarAmount, volume);

        if (GetPositionShares(assetId) > initialShares)
        {
            var lastTrade = TradeLedger.Last();

            if (!IsMuted)
            {
            Console.ForegroundColor = ConsoleColor.Green;
            Console.WriteLine($"\n[{DateTime.Now:HH:mm:ss.fff}] [{StrategyName}] [PAPER EXECUTION] BOUGHT YES @ ${lastTrade.Price:0.00} | Size: ${lastTrade.DollarValue:0.00} | Asset: {GetMarketName(assetId)}");
            Console.ResetColor();
            }
        }
        return filled;
    }

    public override decimal SellAll(string assetId, decimal price, decimal volume)
    {
        decimal initialShares = GetPositionShares(assetId);
        decimal entryPrice = GetAverageEntryPrice(assetId);

        decimal filled = base.SellAll(assetId, price, volume);

        if (GetPositionShares(assetId) < initialShares)
        {
            var lastTrade = TradeLedger.Last();
            decimal pnl = (lastTrade.Price - entryPrice) * (initialShares - GetPositionShares(assetId));

            if (!IsMuted)
            {
            Console.ForegroundColor = pnl >= 0 ? ConsoleColor.Cyan : ConsoleColor.Red;
            Console.WriteLine($"\n[{DateTime.Now:HH:mm:ss.fff}] [{StrategyName}] [PAPER CLOSED] SOLD YES @ ${lastTrade.Price:0.00} | PnL: ${(pnl):0.00} | Total Equity: ${GetTotalPortfolioValue():0.00} | Asset: {GetMarketName(assetId)}");
            Console.ResetColor();
            }
        }
        return filled;
    }

    public override void BuyNo(string assetId, decimal price, decimal dollarAmount, decimal volume)
    {
        decimal initialShares = GetNoPositionShares(assetId);
        base.BuyNo(assetId, price, dollarAmount, volume);

        if (GetNoPositionShares(assetId) > initialShares)
        {
            var lastTrade = TradeLedger.Last();

            if (!IsMuted)
            {
            Console.ForegroundColor = ConsoleColor.Magenta;
            Console.WriteLine($"\n[{DateTime.Now:HH:mm:ss.fff}] [{StrategyName}] [PAPER EXECUTION] BOUGHT NO @ ${lastTrade.Price:0.00} | Size: ${lastTrade.DollarValue:0.00} | Asset: {GetMarketName(assetId)}");
            Console.ResetColor();   
            }
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

            if (!IsMuted)
            {
            Console.ForegroundColor = pnl >= 0 ? ConsoleColor.Cyan : ConsoleColor.Red;
            Console.WriteLine($"\n[{DateTime.Now:HH:mm:ss.fff}] [{StrategyName}] [PAPER CLOSED] SOLD NO @ ${lastTrade.Price:0.00} | PnL: ${(pnl):0.00} | Total Equity: ${GetTotalPortfolioValue():0.00} | Asset: {GetMarketName(assetId)}");
            Console.ResetColor();
            }
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

            if (!IsMuted)
            {
            Console.ForegroundColor = pnl > 0 ? ConsoleColor.Yellow : ConsoleColor.DarkRed;
            Console.WriteLine($"\n[{DateTime.Now:HH:mm:ss.fff}] [{StrategyName}] [MARKET SETTLED] YES SHARES @ ${outcomePrice:0.00} | PnL: ${(pnl):0.00} | Total Equity: ${GetTotalPortfolioValue():0.00} | Asset: {GetMarketName(assetId)}");
            Console.ResetColor(); 
            }
        }

        if (initialNoShares > 0)
        {
            decimal noOutcomePrice = 1.00m - outcomePrice;
            decimal pnl = (noOutcomePrice - noEntryPrice) * initialNoShares;

            if (!IsMuted)
            {
            Console.ForegroundColor = pnl > 0 ? ConsoleColor.Yellow : ConsoleColor.DarkRed;
            Console.WriteLine($"\n[{DateTime.Now:HH:mm:ss.fff}] [{StrategyName}] [MARKET SETTLED] NO SHARES @ ${noOutcomePrice:0.00} | PnL: ${(pnl):0.00} | Total Equity: ${GetTotalPortfolioValue():0.00} | Asset: {GetMarketName(assetId)}");
            Console.ResetColor();   
            }
        }
    }
}