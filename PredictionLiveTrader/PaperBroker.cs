using System;
using System.Collections.Generic;
using System.Linq;
using PredictionBacktester.Engine;

namespace PredictionLiveTrader;

public class PaperBroker : GlobalSimulatedBroker
{
    public string StrategyName { get; }
    private readonly Dictionary<string, string> _tokenNames;
    private readonly Dictionary<string, decimal> _tokenMinSizes;
    private readonly decimal _maxBetSize;

    public PaperBroker(string strategyName, decimal initialCapital, Dictionary<string, string> tokenNames,
        Dictionary<string, decimal> tokenMinSizes, decimal maxBetSize) : base(initialCapital)
    {
        StrategyName = strategyName;
        _tokenNames = tokenNames;
        _tokenMinSizes = tokenMinSizes;
        _maxBetSize = maxBetSize;
        StrategyLabel = strategyName;
        AssetNameResolver = GetMarketName;
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

            if (!IsMuted) lock (ConsoleLock)
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

            if (!IsMuted) lock (ConsoleLock)
            {
                Console.ForegroundColor = pnl >= 0 ? ConsoleColor.Cyan : ConsoleColor.Red;
                Console.WriteLine($"\n[{DateTime.Now:HH:mm:ss.fff}] [{StrategyName}] [PAPER CLOSED] SOLD YES @ ${lastTrade.Price:0.00} | PnL: ${pnl:0.00} | Total Equity: ${GetTotalPortfolioValue():0.00} | Asset: {GetMarketName(assetId)}");
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

            if (!IsMuted) lock (ConsoleLock)
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

            if (!IsMuted) lock (ConsoleLock)
            {
                Console.ForegroundColor = pnl >= 0 ? ConsoleColor.Cyan : ConsoleColor.Red;
                Console.WriteLine($"\n[{DateTime.Now:HH:mm:ss.fff}] [{StrategyName}] [PAPER CLOSED] SOLD NO @ ${lastTrade.Price:0.00} | PnL: ${pnl:0.00} | Total Equity: ${GetTotalPortfolioValue():0.00} | Asset: {GetMarketName(assetId)}");
                Console.ResetColor();
            }
        }
    }

    public override void SubmitBuyOrder(string assetId, decimal targetPrice, decimal dollarsToInvest, LocalOrderBook book)
    {
        // Max bet cap (mirrors ProductionBroker)
        if (dollarsToInvest > _maxBetSize)
            dollarsToInvest = _maxBetSize;

        // Price boundary check (mirrors PolymarketLiveBroker)
        decimal bestAsk = book.GetBestAskPrice();
        if (bestAsk >= 0.99m || bestAsk <= 0.01m) return;

        // Share rounding + min size check (mirrors PolymarketLiveBroker)
        decimal shares = Math.Round(dollarsToInvest / targetPrice, 2);
        decimal minSize = _tokenMinSizes.GetValueOrDefault(assetId, 1.00m);
        if (shares < minSize) return;

        // Recalculate dollars after rounding (mirrors PolymarketLiveBroker)
        dollarsToInvest = shares * targetPrice;

        base.SubmitBuyOrder(assetId, targetPrice, dollarsToInvest, book);
    }

    public override void SubmitSellAllOrder(string assetId, decimal targetPrice, LocalOrderBook book)
    {
        // Share rounding (mirrors PolymarketLiveBroker)
        decimal sharesToSell = Math.Round(GetPositionShares(assetId), 2);
        if (sharesToSell <= 0) return;

        // Dust detection — hold to settlement (mirrors PolymarketLiveBroker)
        decimal minSize = _tokenMinSizes.GetValueOrDefault(assetId, 1.00m);
        if (sharesToSell < minSize)
        {
            if (!IsMuted) lock (ConsoleLock)
            {
                Console.ForegroundColor = ConsoleColor.DarkGray;
                Console.WriteLine($"[{DateTime.Now:HH:mm:ss.fff}] [{StrategyName}] [PAPER DUST] {sharesToSell:0.00} shares below min ({minSize}). Holding to settlement.");
                Console.ResetColor();
            }
            return;
        }

        base.SubmitSellAllOrder(assetId, targetPrice, book);
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

            if (!IsMuted) lock (ConsoleLock)
            {
                Console.ForegroundColor = pnl > 0 ? ConsoleColor.Yellow : ConsoleColor.DarkRed;
                Console.WriteLine($"\n[{DateTime.Now:HH:mm:ss.fff}] [{StrategyName}] [MARKET SETTLED] YES SHARES @ ${outcomePrice:0.00} | PnL: ${pnl:0.00} | Total Equity: ${GetTotalPortfolioValue():0.00} | Asset: {GetMarketName(assetId)}");
                Console.ResetColor();
            }
        }

        if (initialNoShares > 0)
        {
            decimal noOutcomePrice = 1.00m - outcomePrice;
            decimal pnl = (noOutcomePrice - noEntryPrice) * initialNoShares;

            if (!IsMuted) lock (ConsoleLock)
            {
                Console.ForegroundColor = pnl > 0 ? ConsoleColor.Yellow : ConsoleColor.DarkRed;
                Console.WriteLine($"\n[{DateTime.Now:HH:mm:ss.fff}] [{StrategyName}] [MARKET SETTLED] NO SHARES @ ${noOutcomePrice:0.00} | PnL: ${pnl:0.00} | Total Equity: ${GetTotalPortfolioValue():0.00} | Asset: {GetMarketName(assetId)}");
                Console.ResetColor();
            }
        }
    }
}