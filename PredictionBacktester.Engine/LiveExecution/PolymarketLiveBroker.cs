using System;
using System.Threading;
using System.Threading.Tasks;
using PredictionBacktester.Core.Entities;

namespace PredictionBacktester.Engine.LiveExecution;

public class PolymarketLiveBroker : GlobalSimulatedBroker
{
    public string StrategyName { get; }
    private readonly PolymarketOrderClient _orderClient;
    private readonly IReadOnlyDictionary<string, string> _tokenNames;
    private readonly IReadOnlyDictionary<string, bool> _tokenNegRisk;
    private readonly IReadOnlyDictionary<string, string> _tokenTickSizes;
    private readonly IReadOnlyDictionary<string, decimal> _tokenMinSizes;

    private readonly System.Collections.Concurrent.ConcurrentDictionary<string, int> _tokenFeeRates = new();

    private int GetFeeRate(string assetId) => _tokenFeeRates.TryGetValue(assetId, out var fee) ? fee : 0;
    public PolymarketOrderClient OrderClient => _orderClient;
    public void SetTokenFeeRate(string assetId, int feeRate) => _tokenFeeRates[assetId] = feeRate;

    public PolymarketLiveBroker(
        string strategyName, decimal initialCapital, PolymarketApiConfig config,
        IReadOnlyDictionary<string, string> tokenNames, IReadOnlyDictionary<string, bool> tokenNegRisk,
        IReadOnlyDictionary<string, string> tokenTickSizes, IReadOnlyDictionary<string, decimal> tokenMinSizes) : base(initialCapital)
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

    public static async Task<PolymarketLiveBroker> CreateAsync(
        string strategyName, PolymarketApiConfig config, IReadOnlyDictionary<string, string> tokenNames,
        IReadOnlyDictionary<string, bool> tokenNegRisk, IReadOnlyDictionary<string, string> tokenTickSizes,
        IReadOnlyDictionary<string, decimal> tokenMinSizes)
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
    public bool OrderDebugMode { get => _orderClient.DebugMode; set => _orderClient.DebugMode = value; }

    private string GetMarketName(string assetId)
    {
        if (_tokenNames.TryGetValue(assetId, out var name))
            return name.Length > 40 ? name.Substring(0, 37) + "..." : name;
        return assetId.Substring(0, 8) + "...";
    }

    public override void SubmitBuyOrder(string assetId, decimal targetPrice, decimal dollarsToInvest, LocalOrderBook book)
    {
        if (!_pendingOrders.TryAdd(assetId, true)) return;

        decimal bestAsk = book.GetBestAskPrice();
        if (bestAsk >= 0.99m || bestAsk <= 0.01m)
        {
            _pendingOrders.TryRemove(assetId, out _);
            return;
        }

        // THE MATH FIX: Force exactly 2 decimal places for size to prevent API "invalid amounts" errors
        decimal shares = Math.Round(dollarsToInvest / targetPrice, 2);
        
        decimal minSize = GetMinSize(assetId);
        if (shares < minSize)
        {
            _pendingOrders.TryRemove(assetId, out _);
            return;
        }

        // RECALCULATE dollarsToInvest strictly based on the rounded shares
        dollarsToInvest = shares * targetPrice;

        Task.Run(async () =>
        {
            try
            {
                string result = "";
                try
                {
                    result = await _orderClient.SubmitOrderAsync(
                        assetId, targetPrice, shares, 0, GetNegRisk(assetId), GetTickSize(assetId), GetFeeRate(assetId));
                }
                catch (Exception ex) when (ex.Message.Contains("invalid fee rate") && ex.Message.Contains("taker fee:"))
                {
                    var match = System.Text.RegularExpressions.Regex.Match(ex.Message, @"taker fee:\s*(\d+)");
                    if (match.Success && int.TryParse(match.Groups[1].Value, out int requiredFee))
                    {
                        SetTokenFeeRate(assetId, requiredFee);
                        if (!IsMuted) lock (ConsoleLock)
                        {
                            Console.ForegroundColor = ConsoleColor.Yellow;
                            Console.WriteLine($"\n[{DateTime.Now:HH:mm:ss.fff}] [FEE AUTOCORRECT] {GetMarketName(assetId)} requires fee {requiredFee}. Retrying buy...");
                            Console.ResetColor();
                        }
                        result = await _orderClient.SubmitOrderAsync(
                            assetId, targetPrice, shares, 0, GetNegRisk(assetId), GetTickSize(assetId), requiredFee);
                    }
                    else throw;
                }

                using var doc = System.Text.Json.JsonDocument.Parse(result);
                var root = doc.RootElement;

                if (root.TryGetProperty("success", out var successEl) && successEl.GetBoolean() == true)
                {
                    // THE KILLED FIX: Polymarket omitted the amounts. We assume requested shares were filled!
                    // If it was a partial fill, the Emergency Sync will catch it during the sell.
                    decimal actualShares = shares;
                    decimal actualDollars = dollarsToInvest;

                    // Fallbacks just in case the API *does* return them
                    if (root.TryGetProperty("taking_amount", out var takingEl) && decimal.TryParse(takingEl.ToString(), out decimal tAmt))
                        actualShares = tAmt;
                    else if (root.TryGetProperty("takingAmount", out var takingElC) && decimal.TryParse(takingElC.ToString(), out tAmt))
                        actualShares = tAmt;

                    if (root.TryGetProperty("making_amount", out var makingEl) && decimal.TryParse(makingEl.ToString(), out decimal mAmt))
                        actualDollars = mAmt;
                    else if (root.TryGetProperty("makingAmount", out var makingElC) && decimal.TryParse(makingElC.ToString(), out mAmt))
                        actualDollars = mAmt;

                    // Only abort if the API explicitly told us we got 0 shares
                    if (actualShares <= 0) 
                    {
                        if (!IsMuted) lock (ConsoleLock)
                        {
                            Console.ForegroundColor = ConsoleColor.DarkGray;
                            Console.WriteLine($"\n[{DateTime.Now:HH:mm:ss.fff}] [{StrategyName}] [IOC KILLED] Buy missed. Liquidity gone @ ${targetPrice:0.00} | {GetMarketName(assetId)}");
                            Console.ResetColor();
                        }
                        return;
                    }

                    lock (BrokerLock)
                    {
                        decimal currentShares = GetPositionShares(assetId);
                        decimal currentAvgPrice = GetAverageEntryPrice(assetId);
                        decimal totalCost = (currentShares * currentAvgPrice) + actualDollars;

                        SetPositionShares(assetId, currentShares + actualShares);
                        SetAverageEntryPrice(assetId, totalCost / (currentShares + actualShares));
                        CashBalance -= actualDollars;

                        TradeLedger.Add(new ExecutedTrade
                        {
                            OutcomeId = assetId, Date = DateTime.Now, Side = "BUY (IOC)",
                            Price = actualDollars / actualShares, Shares = actualShares, DollarValue = actualDollars
                        });
                        TotalActions++;
                    }

                    if (!IsMuted) lock (ConsoleLock)
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
                if (!IsMuted) lock (ConsoleLock)
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

    public override void SubmitSellAllOrder(string assetId, decimal targetPrice, LocalOrderBook book)
    {
        // THE MATH FIX: Strictly round the shares we are trying to sell to 2 decimal places
        decimal sharesToSell = Math.Round(GetPositionShares(assetId), 2);
        if (sharesToSell <= 0) return;

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

        Task.Run(async () =>
        {
            try
            {
                string result = "";
                int maxRetries = 10;
                
                for (int attempt = 1; attempt <= maxRetries; attempt++)
                {
                    try
                    {
                        result = await _orderClient.SubmitOrderAsync(
                            assetId, targetPrice, sharesToSell, side: 1, GetNegRisk(assetId), GetTickSize(assetId), GetFeeRate(assetId));
                        break;
                    }
                    catch (Exception ex) when (ex.Message.Contains("invalid fee rate") && ex.Message.Contains("taker fee:"))
                    {
                        var match = System.Text.RegularExpressions.Regex.Match(ex.Message, @"taker fee:\s*(\d+)");
                        if (match.Success && int.TryParse(match.Groups[1].Value, out int requiredFee))
                        {
                            SetTokenFeeRate(assetId, requiredFee);
                            if (!IsMuted) lock (ConsoleLock)
                            {
                                Console.ForegroundColor = ConsoleColor.Yellow;
                                Console.WriteLine($"\n[{DateTime.Now:HH:mm:ss.fff}] [FEE AUTOCORRECT] {GetMarketName(assetId)} requires fee {requiredFee}. Retrying sell...");
                                Console.ResetColor();
                            }
                        }
                        else throw;
                    }
                    catch (Exception ex) when (ex.Message.Contains("not enough balance") && attempt < maxRetries)
                    {
                        if (!IsMuted) lock (ConsoleLock)
                        {
                            Console.ForegroundColor = ConsoleColor.DarkYellow;
                            Console.WriteLine($"[{DateTime.Now:HH:mm:ss.fff}] [HAMMER] Tokens not here yet. Retrying in 300ms... (Attempt {attempt}/{maxRetries})");
                            Console.ResetColor();
                        }
                        await Task.Delay(300); 
                    }
                }

                if (string.IsNullOrEmpty(result)) return;

                using var doc = System.Text.Json.JsonDocument.Parse(result);
                var root = doc.RootElement;

                if (root.TryGetProperty("success", out var successEl) && successEl.GetBoolean() == true)
                {
                    // THE KILLED FIX: Assume full execution
                    decimal actualSharesSold = sharesToSell;
                    decimal cashReceived = sharesToSell * targetPrice;

                    // Fallbacks
                    if (root.TryGetProperty("making_amount", out var makingEl) && decimal.TryParse(makingEl.ToString(), out decimal mAmt))
                        actualSharesSold = mAmt;
                    else if (root.TryGetProperty("makingAmount", out var makingElC) && decimal.TryParse(makingElC.ToString(), out mAmt))
                        actualSharesSold = mAmt;

                    if (root.TryGetProperty("taking_amount", out var takingEl) && decimal.TryParse(takingEl.ToString(), out decimal tAmt))
                        cashReceived = tAmt;
                    else if (root.TryGetProperty("takingAmount", out var takingElC) && decimal.TryParse(takingElC.ToString(), out tAmt))
                        cashReceived = tAmt;

                    if (actualSharesSold <= 0) 
                    {
                        if (!IsMuted) lock (ConsoleLock)
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
                            OutcomeId = assetId, Date = DateTime.Now, Side = "SELL (IOC)",
                            Price = executionPrice, Shares = actualSharesSold, DollarValue = cashReceived
                        });

                        CashBalance += cashReceived; 
                        
                        decimal remainingShares = Math.Max(0, GetPositionShares(assetId) - actualSharesSold);
                        SetPositionShares(assetId, remainingShares);
                        if (remainingShares == 0) SetAverageEntryPrice(assetId, 0);
                        
                        TotalTradesExecuted++;
                        TotalActions++;
                    }

                    if (!IsMuted) lock (ConsoleLock)
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
                
                // THE MISSING FIX: Emergency On-Demand Sync for balance mismatches
                if (ex.Message.Contains("balance", StringComparison.OrdinalIgnoreCase) || 
                    ex.Message.Contains("insufficient", StringComparison.OrdinalIgnoreCase))
                {
                    if (!IsMuted) lock (ConsoleLock)
                    {
                        Console.ForegroundColor = ConsoleColor.Magenta;
                        Console.WriteLine($"\n[{DateTime.Now:HH:mm:ss.fff}] [EMERGENCY SYNC] Balance mismatch on {GetMarketName(assetId)}. Fetching truth from chain...");
                        Console.ResetColor();
                    }

                    try 
                    {
                        decimal realShares = await _orderClient.GetTokenBalanceAsync(assetId);
                        
                        lock (BrokerLock)
                        {
                            SetPositionShares(assetId, realShares);
                            if (realShares == 0) SetAverageEntryPrice(assetId, 0);
                        }

                        if (!IsMuted) lock (ConsoleLock)
                        {
                            Console.ForegroundColor = ConsoleColor.Magenta;
                            Console.WriteLine($"[{DateTime.Now:HH:mm:ss.fff}] [EMERGENCY SYNC] Corrected memory to {realShares:0.00} shares. Will cleanly exit on next tick.");
                            Console.ResetColor();
                        }
                    }
                    catch (Exception syncEx)
                    {
                        Console.WriteLine($"Emergency sync failed: {syncEx.Message}");
                    }
                }
                else
                {
                    if (!IsMuted) lock (ConsoleLock)
                    {
                        Console.ForegroundColor = ConsoleColor.Red;
                        Console.WriteLine($"\n[{DateTime.Now:HH:mm:ss.fff}] [{StrategyName}] [LIVE SELL FAILED] {GetMarketName(assetId)}: {ex.Message}");
                        Console.ResetColor();
                    }
                }
            }
            finally
            {
                _pendingOrders.TryRemove(assetId, out _);
            }
        });
    }
}