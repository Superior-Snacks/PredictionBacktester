using System;
using System.Threading;
using System.Threading.Tasks;
using PredictionBacktester.Core.Entities;

namespace PredictionBacktester.Engine.LiveExecution;

/// <summary>
/// Live broker that places real orders on the Polymarket CLOB API.
/// Extends GlobalSimulatedBroker so strategies can query positions and the
/// dashboard/CSV export works identically to paper trading.
/// </summary>
public class PolymarketLiveBroker : GlobalSimulatedBroker
{
    public string StrategyName { get; }
    private readonly PolymarketOrderClient _orderClient;
    private readonly Dictionary<string, string> _tokenNames;
    private readonly Dictionary<string, bool> _tokenNegRisk;
    private readonly Dictionary<string, string> _tokenTickSizes;
    private readonly Dictionary<string, decimal> _tokenMinSizes;

    public PolymarketLiveBroker(
        string strategyName,
        decimal initialCapital,
        PolymarketApiConfig config,
        Dictionary<string, string> tokenNames,
        Dictionary<string, bool> tokenNegRisk,
        Dictionary<string, string> tokenTickSizes,
        Dictionary<string, decimal> tokenMinSizes) : base(initialCapital)
    {
        StrategyName = strategyName;
        _orderClient = new PolymarketOrderClient(config);
        _tokenNames = tokenNames;
        _tokenNegRisk = tokenNegRisk;
        _tokenTickSizes = tokenTickSizes;
        _tokenMinSizes = tokenMinSizes;
        StrategyLabel = strategyName;
        AssetNameResolver = GetMarketName;
    }

    /// <summary>
    /// Creates a live broker initialized with the real USDC balance from the wallet.
    /// </summary>
    public static async Task<PolymarketLiveBroker> CreateAsync(
        string strategyName,
        PolymarketApiConfig config,
        Dictionary<string, string> tokenNames,
        Dictionary<string, bool> tokenNegRisk,
        Dictionary<string, string> tokenTickSizes,
        Dictionary<string, decimal> tokenMinSizes)
    {
        var client = new PolymarketOrderClient(config);
        decimal balance = await client.GetUsdcBalanceAsync();

        Console.ForegroundColor = ConsoleColor.Yellow;
        Console.WriteLine($"[LIVE] Wallet USDC balance: ${balance:0.00}");
        Console.ResetColor();

        return new PolymarketLiveBroker(strategyName, balance, config, tokenNames, tokenNegRisk, tokenTickSizes, tokenMinSizes);
    }

    private bool GetNegRisk(string assetId) => _tokenNegRisk.GetValueOrDefault(assetId, false);
    private string GetTickSize(string assetId) => _tokenTickSizes.GetValueOrDefault(assetId, "0.01");
    public decimal GetMinSize(string assetId) => _tokenMinSizes.GetValueOrDefault(assetId, 1.00m);

    public bool OrderDebugMode
    {
        get => _orderClient.DebugMode;
        set => _orderClient.DebugMode = value;
    }

    private string GetMarketName(string assetId)
    {
        if (_tokenNames.TryGetValue(assetId, out var name))
            return name.Length > 40 ? name.Substring(0, 37) + "..." : name;
        return assetId.Substring(0, 8) + "...";
    }

    /// <summary>
    /// Override: Places a real BUY order on the Polymarket CLOB.
    /// No latency simulation — the real network latency IS the latency.
    /// </summary>
    public override void SubmitBuyOrder(string assetId, decimal targetPrice, decimal dollarsToInvest, LocalOrderBook book)
    {
        // Guard: don't queue duplicate orders for the same asset
        if (!_pendingOrders.TryAdd(assetId, true)) return;

        decimal bestAsk = book.GetBestAskPrice();
        if (bestAsk >= 0.99m || bestAsk <= 0.01m)
        {
            _pendingOrders.TryRemove(assetId, out _);
            return;
        }

        // Calculate shares from dollars and price
        decimal shares = dollarsToInvest / targetPrice;
        decimal minSize = GetMinSize(assetId);
        
        // FIX: Compare calculated shares against the minimum share size
        if (shares < minSize)
        {
            _pendingOrders.TryRemove(assetId, out _);
            return;
        }

        Task.Run(async () =>
        {
            try
            {
                string result = await _orderClient.SubmitOrderAsync(
                    assetId, targetPrice, shares, side: 0, GetNegRisk(assetId), GetTickSize(assetId));
                // 1. Parse the JSON receipt from Polymarket
                using var doc = System.Text.Json.JsonDocument.Parse(result);
                var root = doc.RootElement;

                if (root.TryGetProperty("success", out var successEl) && successEl.GetBoolean() == true)
                {
                    decimal actualShares = 0m;
                    decimal actualDollars = 0m;

                    // BUY: Taking Amount = Shares received, Making Amount = USDC spent
                    if (root.TryGetProperty("takingAmount", out var takingEl) && decimal.TryParse(takingEl.GetString(), out decimal tAmt))
                        actualShares = tAmt;

                    if (root.TryGetProperty("makingAmount", out var makingEl) && decimal.TryParse(makingEl.GetString(), out decimal mAmt))
                        actualDollars = mAmt;

                    // If IOC completely missed (0 shares), abort.
                    if (actualShares <= 0) 
                    {
                        lock (ConsoleLock)
                        {
                            Console.ForegroundColor = ConsoleColor.DarkGray;
                            Console.WriteLine($"\n[{DateTime.Now:HH:mm:ss.fff}] [{StrategyName}] [IOC KILLED] Buy missed. Liquidity gone @ ${targetPrice:0.00} | {GetMarketName(assetId)}");
                            Console.ResetColor();
                        }
                        return;
                    }

                    // 2. Update memory using the ACTUAL fill amounts
                    lock (BrokerLock)
                    {
                        decimal currentShares = GetPositionShares(assetId);
                        decimal currentAvgPrice = GetAverageEntryPrice(assetId);
                        decimal totalCost = (currentShares * currentAvgPrice) + actualDollars;

                        SetPositionShares(assetId, currentShares + actualShares);
                        SetAverageEntryPrice(assetId, totalCost / (currentShares + actualShares));
                        CashBalance -= actualDollars; // Subtract exact USDC spent

                        TradeLedger.Add(new ExecutedTrade
                        {
                            OutcomeId = assetId,
                            Date = DateTime.Now,
                            Side = "BUY (IOC)",
                            Price = actualDollars / actualShares, // Calculate true execution price
                            Shares = actualShares,
                            DollarValue = actualDollars
                        });
                        TotalActions++;
                    }

                    lock (ConsoleLock)
                    {
                        Console.ForegroundColor = ConsoleColor.Green;
                        Console.WriteLine($"\n[{DateTime.Now:HH:mm:ss.fff}] [{StrategyName}] [LIVE BUY IOC] {actualShares:0.00} shares @ ${actualDollars / actualShares:0.00} | ${actualDollars:0.00} | {GetMarketName(assetId)}");
                        Console.ResetColor();
                    }
                }
            }
            catch (Exception ex)
            {
                Interlocked.Increment(ref _rejectedOrders);
                lock (ConsoleLock)
                {
                    Console.ForegroundColor = ConsoleColor.Red;
                    Console.WriteLine($"\n[{DateTime.Now:HH:mm:ss.fff}] [{StrategyName}] [LIVE BUY FAILED] {GetMarketName(assetId)}: {ex.Message}");
                    Console.ResetColor();
                }
            }
            finally
            {
                _pendingOrders.TryRemove(assetId, out _);
            }
        });
    }

    /// <summary>
    /// Override: Places a real SELL order on the Polymarket CLOB.
    /// </summary>
    public override void SubmitSellAllOrder(string assetId, decimal targetPrice, LocalOrderBook book)
    {
        decimal sharesToSell = GetPositionShares(assetId);
        if (sharesToSell <= 0) return;

        // NEW: Check for dust before doing anything else
        decimal minSize = GetMinSize(assetId);
        if (sharesToSell < minSize)
        {
            if (!IsMuted) lock (ConsoleLock)
            {
                Console.ForegroundColor = ConsoleColor.DarkGray;
                Console.WriteLine($"\n[{DateTime.Now:HH:mm:ss.fff}] [{StrategyName}] [DUST] {sharesToSell:0.00} shares is below minimum ({minSize}). Holding to settlement.");
                Console.ResetColor();
            }
            return;
        }

        if (!_pendingOrders.TryAdd(assetId, true)) return;

        decimal bestBid = book.GetBestBidPrice();
        if (bestBid >= 0.99m || bestBid <= 0.01m)
        {
            _pendingOrders.TryRemove(assetId, out _);
            return;
        }

        Task.Run(async () =>
        {
            try
            {
                decimal sharesToSell = GetPositionShares(assetId);
                if (sharesToSell <= 0) return;

                string result = "";
                int maxRetries = 10; // Try 10 times
                
                // Aggressive rapid-fire retry loop
                for (int attempt = 1; attempt <= maxRetries; attempt++)
                {
                    try
                    {
                        result = await _orderClient.SubmitOrderAsync(
                            assetId, targetPrice, sharesToSell, side: 1, GetNegRisk(assetId), GetTickSize(assetId));
                        break; // Success! The tokens arrived.
                    }
                    catch (Exception ex) when (ex.Message.Contains("not enough balance") && attempt < maxRetries)
                    {
                        if (!IsMuted) lock (ConsoleLock)
                        {
                            Console.ForegroundColor = ConsoleColor.DarkYellow;
                            Console.WriteLine($"[{DateTime.Now:HH:mm:ss.fff}] [HAMMER] Tokens not here yet. Retrying in 300ms... (Attempt {attempt}/{maxRetries})");
                            Console.ResetColor();
                        }
                        
                        // Wait just 300ms and immediately slam the API again
                        await Task.Delay(300); 
                    }
                }

                if (string.IsNullOrEmpty(result)) 
                {
                    lock (ConsoleLock)
                    {
                        Console.ForegroundColor = ConsoleColor.Red;
                        Console.WriteLine($"[{DateTime.Now:HH:mm:ss.fff}] [SELL FAILED] Tokens never arrived after 3 seconds of retrying.");
                        Console.ResetColor();
                    }
                    return;
                }

                // --- Rest of your exact same IOC parsing logic goes here ---
                using var doc = System.Text.Json.JsonDocument.Parse(result);
                var root = doc.RootElement;

                if (root.TryGetProperty("success", out var successEl) && successEl.GetBoolean() == true)
                {
                    decimal actualSharesSold = 0m;
                    decimal cashReceived = 0m;

                    // SELL: Making Amount = Shares given, Taking Amount = USDC received
                    if (root.TryGetProperty("makingAmount", out var makingEl) && decimal.TryParse(makingEl.GetString(), out decimal mAmt))
                        actualSharesSold = mAmt;

                    if (root.TryGetProperty("takingAmount", out var takingEl) && decimal.TryParse(takingEl.GetString(), out decimal tAmt))
                        cashReceived = tAmt;

                    if (actualSharesSold <= 0) 
                    {
                        lock (ConsoleLock)
                        {
                            Console.ForegroundColor = ConsoleColor.DarkGray;
                            Console.WriteLine($"\n[{DateTime.Now:HH:mm:ss.fff}] [{StrategyName}] [IOC KILLED] Sell missed. Retrying next tick... | {GetMarketName(assetId)}");
                            Console.ResetColor();
                        }
                        return;
                    }

                    decimal pnl = 0m;
                    lock (BrokerLock)
                    {
                        decimal entryPrice = GetAverageEntryPrice(assetId);
                        pnl = cashReceived - (actualSharesSold * entryPrice);
                        decimal executionPrice = cashReceived / actualSharesSold;

                        if (executionPrice > entryPrice) WinningTrades++;
                        else LosingTrades++;

                        TradeLedger.Add(new ExecutedTrade
                        {
                            OutcomeId = assetId,
                            Date = DateTime.Now,
                            Side = "SELL (IOC)",
                            Price = executionPrice,
                            Shares = actualSharesSold,
                            DollarValue = cashReceived
                        });

                        CashBalance += cashReceived; // Add exact USDC received
                        
                        // Deduct the exact shares we managed to sell
                        decimal remainingShares = Math.Max(0, GetPositionShares(assetId) - actualSharesSold);
                        SetPositionShares(assetId, remainingShares);
                        
                        if (remainingShares == 0) SetAverageEntryPrice(assetId, 0);
                        
                        TotalTradesExecuted++;
                        TotalActions++;
                    }

                    lock (ConsoleLock)
                    {
                        Console.ForegroundColor = pnl >= 0 ? ConsoleColor.Cyan : ConsoleColor.Red;
                        Console.WriteLine($"\n[{DateTime.Now:HH:mm:ss.fff}] [{StrategyName}] [LIVE SELL IOC] {actualSharesSold:0.00} shares | PnL: ${pnl:0.00} | Received: ${cashReceived:0.00} | {GetMarketName(assetId)}");
                        Console.ResetColor();
                    }
                }
            }
            catch (Exception ex)
            {
                Interlocked.Increment(ref _rejectedOrders);
                lock (ConsoleLock)
                {
                    Console.ForegroundColor = ConsoleColor.Red;
                    Console.WriteLine($"\n[{DateTime.Now:HH:mm:ss.fff}] [{StrategyName}] [LIVE SELL FAILED] {GetMarketName(assetId)}: {ex.Message}");
                    Console.ResetColor();
                }
            }
            finally
            {
                _pendingOrders.TryRemove(assetId, out _);
            }
        });
    }
}
