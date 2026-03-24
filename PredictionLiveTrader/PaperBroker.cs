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
    private readonly HashSet<string> _dustWarned = new();
    public decimal SlippageCents { get; set; } = 0.01m;

    // Per-token fee rates (basis points) and exponents — fetched from Polymarket API
    private readonly Dictionary<string, int> _tokenFeeRates;
    // Default exponent for fee calculation — Polymarket uses 1 for most categories
    private const int DEFAULT_FEE_EXPONENT = 1;

    public PaperBroker(string strategyName, decimal initialCapital, Dictionary<string, string> tokenNames,
        Dictionary<string, decimal> tokenMinSizes, decimal maxBetSize,
        Dictionary<string, int>? tokenFeeRates = null) : base(initialCapital)
    {
        StrategyName = strategyName;
        _tokenNames = tokenNames;
        _tokenMinSizes = tokenMinSizes;
        _maxBetSize = maxBetSize;
        _tokenFeeRates = tokenFeeRates ?? new Dictionary<string, int>();
        StrategyLabel = strategyName;
        AssetNameResolver = GetMarketName;
        // Disable the flat spread penalty — we use Polymarket's fee formula instead
        SpreadPenalty = 0m;
    }

    public override decimal GetMinSize(string assetId) => _tokenMinSizes.GetValueOrDefault(assetId, 1.00m);

    // Helper to format the name cleanly for the console
    private string GetMarketName(string assetId)
    {
        if (_tokenNames.TryGetValue(assetId, out var name))
        {
            return name.Length > 40 ? name.Substring(0, 37) + "..." : name;
        }
        return assetId.Substring(0, 8) + "...";
    }

    /// <summary>
    /// Calculates Polymarket taker fee in USDC.
    /// Formula: fee = C × p × feeRate × (p × (1 - p))^exponent
    /// Where C = shares, p = price, feeRate from API (converted from bps).
    /// Rounded to 4 decimal places per Polymarket spec.
    /// </summary>
    private decimal CalculateFeeUsdc(string assetId, decimal shares, decimal price)
    {
        if (!_tokenFeeRates.TryGetValue(assetId, out int feeRateBps) || feeRateBps <= 0)
            return 0m;

        // feeRateBps from the API is already the raw fee_rate_bps value.
        // The formula uses a decimal feeRate derived from the bps value.
        // Per the docs, Crypto has feeRate=0.25 with bps=1000 currently,
        // but post March 30 the mapping changes. We use the formula directly
        // with feeRate = feeRateBps / 10000 when bps maps to a percentage,
        // but Polymarket's feeRate in the formula is NOT bps/10000 — it's
        // a separate parameter. We need to map known bps to (feeRate, exponent).
        //
        // Known mappings (current, pre March 30 2026):
        //   Crypto:  bps=1000 -> feeRate=0.25, exponent=2
        //   Sports:  bps=175  -> feeRate=0.0175, exponent=1
        //
        // Post March 30 2026 mappings (all exponent=1 unless noted):
        //   Crypto:  feeRate=0.072, exponent=1
        //   Sports:  feeRate=0.03, exponent=1
        //   Finance: feeRate=0.04, exponent=1
        //   Politics: feeRate=0.04, exponent=1
        //   Economics: feeRate=0.03, exponent=0.5
        //   Culture: feeRate=0.05, exponent=1
        //   Weather: feeRate=0.025, exponent=0.5
        //   Other:   feeRate=0.2, exponent=2
        //   Mentions: feeRate=0.25, exponent=2
        //   Tech:    feeRate=0.04, exponent=1
        //
        // Since the API just returns a bps number, we compute the effective feeRate
        // by working backwards: the API returns the peak effective rate as bps.
        // For exponent=1: peak = feeRate * 0.25 (at p=0.5), so feeRate = bps/10000 / 0.25 = bps/2500
        // For exponent=2: peak = feeRate * 0.0625 (at p=0.5), so feeRate = bps/10000 / 0.0625 = bps/625
        //
        // Actually, let's just use the simple formula with known mappings:
        decimal feeRate;
        decimal exponent;

        // Map known fee_rate_bps values to (feeRate, exponent) pairs
        switch (feeRateBps)
        {
            // Current (pre March 30 2026)
            case 1000: feeRate = 0.25m; exponent = 2m; break;      // Crypto (peak 1.56%)
            case 175:  feeRate = 0.0175m; exponent = 1m; break;    // Sports (peak 0.44%)

            // Post March 30 2026
            case 180:  feeRate = 0.072m; exponent = 1m; break;     // Crypto (peak 1.80%)
            case 75:   feeRate = 0.03m; exponent = 1m; break;      // Sports (peak 0.75%)
            case 100:  feeRate = 0.04m; exponent = 1m; break;      // Finance/Politics/Tech (peak 1.00%)
            case 150:  feeRate = 0.03m; exponent = 0.5m; break;    // Economics (peak 1.50%)
            case 125:  feeRate = 0.05m; exponent = 1m; break;      // Culture (peak 1.25%)
            // Weather (peak 1.25%) has same bps as Culture, different params
            // Other (peak 1.25%) feeRate=0.2, exponent=2
            // Mentions (peak 1.56%) feeRate=0.25, exponent=2 — same bps as current Crypto

            default:
                // Generic fallback: treat bps as a simple linear fee rate
                // fee = C * p * (bps/10000) * p*(1-p)
                feeRate = feeRateBps / 10000m;
                exponent = 1m;
                break;
        }

        decimal pq = price * (1m - price); // p × (1 - p)

        // (p × (1-p))^exponent
        decimal pqPow;
        if (exponent == 1m)
            pqPow = pq;
        else if (exponent == 2m)
            pqPow = pq * pq;
        else if (exponent == 0.5m)
            pqPow = (decimal)Math.Sqrt((double)pq);
        else
            pqPow = (decimal)Math.Pow((double)pq, (double)exponent);

        decimal feeUsdc = shares * price * feeRate * pqPow;

        // Rounded to 4 decimal places, minimum 0.0001
        feeUsdc = Math.Round(feeUsdc, 4);
        if (feeUsdc > 0m && feeUsdc < 0.0001m) feeUsdc = 0m;

        return feeUsdc;
    }

    public override decimal Buy(string assetId, decimal price, decimal dollarAmount, decimal volume)
    {
        decimal initialShares = GetPositionShares(assetId);
        decimal filled = base.Buy(assetId, price, dollarAmount, volume);

        // Apply taker fee on buys: Polymarket collects fees in SHARES
        // fee_shares = fee_usdc / price
        if (filled > 0)
        {
            decimal feeUsdc = CalculateFeeUsdc(assetId, filled, price);
            if (feeUsdc > 0)
            {
                decimal feeShares = feeUsdc / price;
                decimal adjustedShares = Math.Floor((filled - feeShares) * 100m) / 100m; // Floor to 2dp to match chain

                if (adjustedShares > 0 && adjustedShares < filled)
                {
                    // Reduce position by the fee shares
                    lock (BrokerLock)
                    {
                        SetPositionShares(assetId, GetPositionShares(assetId) - (filled - adjustedShares));
                    }
                    filled = adjustedShares;
                }
            }

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
            // Apply taker fee on sells: Polymarket collects fees in USDC
            decimal feeUsdc = CalculateFeeUsdc(assetId, filled, price);
            if (feeUsdc > 0)
            {
                lock (BrokerLock)
                {
                    CashBalance -= feeUsdc;
                }
            }

            _dustWarned.Remove(assetId);
            var lastTrade = TradeLedger.Last();
            decimal pnl = (lastTrade.Price - entryPrice) * (initialShares - GetPositionShares(assetId)) - feeUsdc;

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
            // Apply taker fee on NO buys (same formula, fees in shares)
            decimal boughtShares = GetNoPositionShares(assetId) - initialShares;
            decimal feeUsdc = CalculateFeeUsdc(assetId, boughtShares, price);
            if (feeUsdc > 0)
            {
                decimal feeShares = feeUsdc / price;
                decimal adjustedShares = Math.Floor((boughtShares - feeShares) * 100m) / 100m;
                if (adjustedShares > 0 && adjustedShares < boughtShares)
                {
                    lock (BrokerLock)
                    {
                        SetNoPositionShares(assetId, initialShares + adjustedShares);
                    }
                }
            }

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
            // Apply taker fee on NO sells (fees in USDC)
            decimal soldShares = initialShares - GetNoPositionShares(assetId);
            decimal feeUsdc = CalculateFeeUsdc(assetId, soldShares, price);
            if (feeUsdc > 0)
            {
                lock (BrokerLock)
                {
                    CashBalance -= feeUsdc;
                }
            }

            var lastTrade = TradeLedger.Last();
            decimal pnl = (lastTrade.Price - entryPrice) * soldShares - feeUsdc;

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

        if (LatencyMs > 0)
        {
            if (!_pendingOrders.TryAdd(assetId, true)) return;
            Task.Run(async () =>
            {
                try
                {
                    await Task.Delay(LatencyMs);
                    ExecuteWalkBuy(assetId, targetPrice, dollarsToInvest, book);
                }
                finally { _pendingOrders.TryRemove(assetId, out _); }
            });
            return;
        }

        ExecuteWalkBuy(assetId, targetPrice, dollarsToInvest, book);
    }

    private void ExecuteWalkBuy(string assetId, decimal targetPrice, decimal dollarsToInvest, LocalOrderBook book)
    {
        decimal bestAsk = book.GetBestAskPrice();
        if (bestAsk >= 0.99m || bestAsk <= 0.01m) return;

        decimal maxPrice = Math.Min(targetPrice + SlippageCents, 0.99m);
        var result = book.WalkAsks(maxPrice, dollarsToInvest, MaxParticipationRate);

        if (result.TotalShares <= 0)
        {
            Interlocked.Increment(ref _rejectedOrders);
            return;
        }

        // Feed VWAP into base.Buy() for position bookkeeping + fee calculation
        decimal filled = Buy(assetId, result.Vwap, result.TotalCost, result.TotalShares);
        if (filled > 0) ConsumeAsk(assetId, filled);
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
            if (!IsMuted && _dustWarned.Add(assetId)) lock (ConsoleLock)
            {
                Console.ForegroundColor = ConsoleColor.DarkGray;
                Console.WriteLine($"[{DateTime.Now:HH:mm:ss.fff}] [{StrategyName}] [PAPER DUST] {sharesToSell:0.00} shares below min ({minSize}). Holding to settlement.");
                Console.ResetColor();
            }
            return;
        }

        if (LatencyMs > 0)
        {
            if (!_pendingOrders.TryAdd(assetId, true)) return;
            Task.Run(async () =>
            {
                try
                {
                    await Task.Delay(LatencyMs);
                    ExecuteWalkSell(assetId, book);
                }
                finally { _pendingOrders.TryRemove(assetId, out _); }
            });
            return;
        }

        ExecuteWalkSell(assetId, book);
    }

    private void ExecuteWalkSell(string assetId, LocalOrderBook book)
    {
        decimal sharesToSell = Math.Round(GetPositionShares(assetId), 2);
        if (sharesToSell <= 0) return;

        decimal currentBid = book.GetBestBidPrice();
        if (currentBid <= 0.01m || currentBid >= 0.99m) return;

        // Sell at any price — mirrors production FAK with floor at 0.01
        var result = book.WalkBids(0.01m, sharesToSell, MaxParticipationRate);

        if (result.TotalShares <= 0) return;

        decimal filled = SellAll(assetId, result.Vwap, result.TotalShares);
        if (filled > 0) ConsumeBid(assetId, filled);
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
