using System;
using System.Collections.Generic;
using System.Threading;
using System.Threading.Tasks;
using System.Text.Json;
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

    public IEnumerable<string> AllTrackedAssets => _tokenNames.Keys;

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

        // Allow 1 cent of positive slippage to cross the spread and guarantee the fill
        targetPrice = Math.Min(0.99m, targetPrice + 0.01m); 
        
        decimal shares = Math.Round(dollarsToInvest / targetPrice, 4);
        
        decimal minSize = GetMinSize(assetId);
        if (shares < minSize)
        {
            _pendingOrders.TryRemove(assetId, out _);
            return;
        }

        // RECALCULATE dollarsToInvest based on the strict 2-decimal API rule
        dollarsToInvest = Math.Round(shares * targetPrice, 2);

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

                // --- RAW DEBUG LOGGING ---
                if (OrderDebugMode && !string.IsNullOrEmpty(result))
                {
                    if (!IsMuted) lock (ConsoleLock)
                    {
                        Console.ForegroundColor = ConsoleColor.DarkMagenta;
                        Console.WriteLine($"\n[{DateTime.Now:HH:mm:ss.fff}] [ORDER DEBUG] RAW BUY RESPONSE:\n{result}");
                        Console.ResetColor();
                    }
                }

                using var doc = System.Text.Json.JsonDocument.Parse(result);
                var root = doc.RootElement;

                if (root.TryGetProperty("success", out var successEl) && successEl.GetBoolean() == true)
                {
                    string status = root.TryGetProperty("status", out var statusEl) ? statusEl.GetString() : "";
                    string orderId = root.TryGetProperty("orderID", out var orderIdEl) ? orderIdEl.GetString() : "";

                    decimal actualShares = shares;
                    decimal actualDollars = dollarsToInvest;

                    // --- CONCURRENT-SAFE DELAYED SETTLEMENT LOOP ---
                    if (status == "delayed" && !string.IsNullOrEmpty(orderId))
                    {
                        if (!IsMuted) lock (ConsoleLock)
                        {
                            Console.ForegroundColor = ConsoleColor.DarkYellow;
                            Console.WriteLine($"\n[{DateTime.Now:HH:mm:ss.fff}] [{StrategyName}] [DELAYED] Buy Order {orderId} pending. Polling API...");
                            Console.ResetColor();
                        }

                        bool settled = false;

                        for (int i = 0; i < 120; i++)
                        {
                            await Task.Delay(500); // 500ms delay between checks
                            
                            try 
                            {
                                string pollResult = await _orderClient.GetOrderAsync(orderId);
                                using var pollDoc = System.Text.Json.JsonDocument.Parse(pollResult);
                                var pollRoot = pollDoc.RootElement;

                                // Handle array or single object response
                                JsonElement orderData = pollRoot;
                                if (pollRoot.ValueKind == JsonValueKind.Array && pollRoot.GetArrayLength() > 0)
                                {
                                    orderData = pollRoot[0];
                                }

                                string currentStatus = orderData.TryGetProperty("status", out var currStatusEl) ? currStatusEl.GetString() : "";

                                if (currentStatus == "matched" || currentStatus == "live")
                                {
                                    if (orderData.TryGetProperty("taker_amount_matched", out var takerEl) && decimal.TryParse(takerEl.ToString(), out decimal matchedShares))
                                    {
                                        actualShares = matchedShares;
                                    }
                                    
                                    if (orderData.TryGetProperty("maker_amount_matched", out var makerEl) && decimal.TryParse(makerEl.ToString(), out decimal matchedUsdc))
                                    {
                                        actualDollars = matchedUsdc;
                                    }
                                    
                                    settled = true;
                                    break;
                                }
                                else if (currentStatus == "canceled" || currentStatus == "expired" || currentStatus == "unmatched")
                                {
                                    actualShares = 0;
                                    settled = true;
                                    break;
                                }
                            }
                            catch { /* Ignore parsing or network timeouts during polling */ }
                        }

                        if (!settled) 
                        {
                            actualShares = 0; // Abort if completely timed out
                        }
                    }
                    else 
                    {
                        // Handle instant fills
                        if (root.TryGetProperty("takingAmount", out var takingElC) && decimal.TryParse(takingElC.ToString(), out decimal tAmt))
                            actualShares = tAmt;
                        else if (root.TryGetProperty("taking_amount", out var takingEl) && decimal.TryParse(takingEl.ToString(), out tAmt))
                            actualShares = tAmt;

                        if (root.TryGetProperty("makingAmount", out var makingElC) && decimal.TryParse(makingElC.ToString(), out decimal mAmt))
                            actualDollars = mAmt;
                        else if (root.TryGetProperty("making_amount", out var makingEl) && decimal.TryParse(makingEl.ToString(), out mAmt))
                            actualDollars = mAmt;
                    }

                    if (actualShares <= 0) 
                    {
                        if (!IsMuted) lock (ConsoleLock)
                        {
                            Console.ForegroundColor = ConsoleColor.DarkGray;
                            Console.WriteLine($"\n[{DateTime.Now:HH:mm:ss.fff}] [{StrategyName}] [FAK KILLED] Buy missed. Liquidity gone @ ${targetPrice:0.00} | {GetMarketName(assetId)}");
                            Console.ResetColor();
                        }
                        return; // Exits safely without touching ledger
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
                            OutcomeId = assetId, Date = DateTime.Now, Side = "BUY (FAK)",
                            Price = actualDollars / actualShares, Shares = actualShares, DollarValue = actualDollars
                        });
                        TotalActions++;
                    }

                    if (!IsMuted) lock (ConsoleLock)
                    {
                        decimal exactExecutionPrice = actualDollars / actualShares;
                        Console.ForegroundColor = ConsoleColor.Green;
                        Console.WriteLine($"\n[{DateTime.Now:HH:mm:ss.fff}] [{StrategyName}] [LIVE BUY FAK] {actualShares:0.00} shares @ exactly ${exactExecutionPrice:0.000} | ${actualDollars:0.00} | {GetMarketName(assetId)}");
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

        // --- NEW SLIPPAGE LOGIC ---
        // Instead of a hardcoded -0.01, find the actual best buyer on the book.
        // If the best buyer is way worse than your target price, you still want to get out.
        decimal bestBid = book.GetBestBidPrice();
        
        // If the book is completely empty, default to the lowest possible tick
        if (bestBid <= 0) bestBid = 0.01m; 
        
        // We will accept whichever is worse: our target price - 1 cent, OR the actual best bid.
        // This guarantees the FAK order will hit existing liquidity immediately.
        targetPrice = Math.Min(Math.Max(0.01m, targetPrice - 0.01m), bestBid);

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

                // --- RAW DEBUG LOGGING ---
                if (OrderDebugMode)
                {
                    if (!IsMuted) lock (ConsoleLock)
                    {
                        Console.ForegroundColor = ConsoleColor.DarkMagenta;
                        Console.WriteLine($"\n[{DateTime.Now:HH:mm:ss.fff}] [ORDER DEBUG] RAW SELL RESPONSE:\n{result}");
                        Console.ResetColor();
                    }
                }

                using var doc = System.Text.Json.JsonDocument.Parse(result);
                var root = doc.RootElement;

                if (root.TryGetProperty("success", out var successEl) && successEl.GetBoolean() == true)
                {
                    string status = root.TryGetProperty("status", out var statusEl) ? statusEl.GetString() : "";
                    string orderId = root.TryGetProperty("orderID", out var orderIdEl) ? orderIdEl.GetString() : "";

                    decimal actualSharesSold = sharesToSell;
                    decimal cashReceived = sharesToSell * targetPrice;

                    // --- CONCURRENT-SAFE DELAYED SETTLEMENT LOOP (FOR SELLS) ---
                    if (status == "delayed" && !string.IsNullOrEmpty(orderId))
                    {
                        if (!IsMuted) lock (ConsoleLock)
                        {
                            Console.ForegroundColor = ConsoleColor.DarkYellow;
                            Console.WriteLine($"\n[{DateTime.Now:HH:mm:ss.fff}] [{StrategyName}] [DELAYED] Sell Order {orderId} pending. Polling API...");
                            Console.ResetColor();
                        }

                        bool settled = false;

                        for (int i = 0; i < 120; i++)
                        {
                            await Task.Delay(500); 
                            try 
                            {
                                string pollResult = await _orderClient.GetOrderAsync(orderId);
                                using var pollDoc = System.Text.Json.JsonDocument.Parse(pollResult);
                                var pollRoot = pollDoc.RootElement;

                                JsonElement orderData = pollRoot;
                                if (pollRoot.ValueKind == JsonValueKind.Array && pollRoot.GetArrayLength() > 0)
                                    orderData = pollRoot[0];

                                string currentStatus = orderData.TryGetProperty("status", out var currStatusEl) ? currStatusEl.GetString() : "";

                                if (currentStatus == "matched" || currentStatus == "live")
                                {
                                    if (orderData.TryGetProperty("maker_amount_matched", out var makerEl) && decimal.TryParse(makerEl.ToString(), out decimal matchedShares))
                                        actualSharesSold = matchedShares;
                                        
                                    if (orderData.TryGetProperty("taker_amount_matched", out var takerEl) && decimal.TryParse(takerEl.ToString(), out decimal matchedUsdc))
                                        cashReceived = matchedUsdc;
                                        
                                    settled = true;
                                    break;
                                }
                                else if (currentStatus == "canceled" || currentStatus == "expired" || currentStatus == "unmatched")
                                {
                                    actualSharesSold = 0;
                                    cashReceived = 0;
                                    settled = true;
                                    break;
                                }
                            }
                            catch { /* Ignore polling errors */ }
                        }
                        if (!settled) { actualSharesSold = 0; cashReceived = 0; }
                    }
                    else
                    {
                        // Instant Sell Fill
                        if (root.TryGetProperty("makingAmount", out var makingElC) && decimal.TryParse(makingElC.ToString(), out decimal mAmt))
                            actualSharesSold = mAmt;
                        else if (root.TryGetProperty("making_amount", out var makingEl) && decimal.TryParse(makingEl.ToString(), out mAmt))
                            actualSharesSold = mAmt;

                        if (root.TryGetProperty("takingAmount", out var takingElC) && decimal.TryParse(takingElC.ToString(), out decimal tAmt))
                            cashReceived = tAmt;
                        else if (root.TryGetProperty("taking_amount", out var takingEl) && decimal.TryParse(takingEl.ToString(), out tAmt))
                            cashReceived = tAmt;
                    }

                    if (actualSharesSold <= 0) 
                    {
                        if (!IsMuted) lock (ConsoleLock)
                        {
                            Console.ForegroundColor = ConsoleColor.DarkGray;
                            Console.WriteLine($"\n[{DateTime.Now:HH:mm:ss.fff}] [{StrategyName}] [FAK KILLED] Sell missed. Retrying next tick... | {GetMarketName(assetId)}");
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
                            OutcomeId = assetId, Date = DateTime.Now, Side = "SELL (FAK)",
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
                        decimal exactExecutionPrice = cashReceived / actualSharesSold;
                        Console.ForegroundColor = pnl >= 0 ? ConsoleColor.Cyan : ConsoleColor.Red;
                        Console.WriteLine($"\n[{DateTime.Now:HH:mm:ss.fff}] [{StrategyName}] [LIVE SELL FAK] {actualSharesSold:0.00} shares @ exactly ${exactExecutionPrice:0.000} | PnL: ${pnl:0.00} | {GetMarketName(assetId)}");
                        Console.ResetColor();
                    }
                }
            }
            catch (Exception ex)
            {
                Interlocked.Increment(ref _rejectedOrders);
                
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