using System;
using System.Collections.Generic;
using System.Threading;
using System.Threading.Tasks;
using System.Text.Json;
using PredictionBacktester.Core.Entities;

namespace PredictionBacktester.Engine.LiveExecution;

/// <summary>
/// Represents an order that the polling loop timed out on — may have filled on-chain.
/// </summary>
public record GhostOrder(string AssetId, string OrderId, string Side, decimal TargetPrice, decimal RequestedShares, DateTime CreatedAt);

public class PolymarketLiveBroker : GlobalSimulatedBroker
{
    public string StrategyName { get; }
    private readonly PolymarketOrderClient _orderClient;
    private readonly IReadOnlyDictionary<string, string> _tokenNames;
    private readonly IReadOnlyDictionary<string, bool> _tokenNegRisk;
    private readonly IReadOnlyDictionary<string, string> _tokenTickSizes;
    private readonly IReadOnlyDictionary<string, decimal> _tokenMinSizes;

    // Ghost orders: polling timed out, but order may have filled on-chain
    private readonly System.Collections.Concurrent.ConcurrentDictionary<string, GhostOrder> _ghostOrders = new();
    // Tracks whether we've already logged a BLOCKED message for each asset (prevents log spam)
    private readonly System.Collections.Concurrent.ConcurrentDictionary<string, bool> _blockedLogSent = new();
    public int GhostOrderCount => _ghostOrders.Count;

    private readonly System.Collections.Concurrent.ConcurrentDictionary<string, int> _tokenFeeRates = new();

    // Cooldown after FAK sell failures (no liquidity) — prevents rapid-fire retries into empty books
    private readonly System.Collections.Concurrent.ConcurrentDictionary<string, long> _sellCooldownUntil = new();
    private const int SELL_COOLDOWN_SECONDS = 30;

    private int GetFeeRate(string assetId) => _tokenFeeRates.TryGetValue(assetId, out var fee) ? fee : 0;
    public PolymarketOrderClient OrderClient => _orderClient;
    public void SetTokenFeeRate(string assetId, int feeRate) => _tokenFeeRates[assetId] = feeRate;

    // UserStream fill signals — now passes exact execution size and price
    private readonly System.Collections.Concurrent.ConcurrentDictionary<string, TaskCompletionSource<(decimal Size, decimal Price)>> _fillSignals = new();

    public void SignalFill(string assetId, decimal size, decimal price)
    {
        if (_fillSignals.TryRemove(assetId, out var tcs))
            tcs.TrySetResult((size, price));
    }

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
    public override decimal GetMinSize(string assetId) => _tokenMinSizes.GetValueOrDefault(assetId, 1.00m);
    public bool OrderDebugMode { get => _orderClient.DebugMode; set => _orderClient.DebugMode = value; }

    private string GetMarketName(string assetId)
    {
        if (_tokenNames.TryGetValue(assetId, out var name))
            return name.Length > 40 ? name.Substring(0, 37) + "..." : name;
        return assetId.Substring(0, 8) + "...";
    }

    public override void SubmitBuyOrder(string assetId, decimal targetPrice, decimal dollarsToInvest, LocalOrderBook book)
    {
        if (!_pendingOrders.TryAdd(assetId, true))
        {
            if (OrderDebugMode && !IsMuted && _blockedLogSent.TryAdd(assetId, true)) lock (ConsoleLock)
            {
                bool hasGhost = _ghostOrders.ContainsKey(assetId);
                Console.ForegroundColor = ConsoleColor.DarkGray;
                Console.WriteLine($"[{DateTime.Now:HH:mm:ss.fff}] [{StrategyName}] [BLOCKED] Buy skipped — {(hasGhost ? "ghost order pending" : "order in flight")} | {GetMarketName(assetId)}");
                Console.ResetColor();
            }
            return;
        }

        decimal bestAsk = book.GetBestAskPrice();
        if (bestAsk >= 0.99m || bestAsk <= 0.01m)
        {
            _pendingOrders.TryRemove(assetId, out _); _blockedLogSent.TryRemove(assetId, out _);
            return;
        }

        // Allow 1 cent of positive slippage to cross the spread and guarantee the fill
        targetPrice = Math.Min(0.99m, targetPrice + 0.01m); 
        
        decimal shares = Math.Round(dollarsToInvest / targetPrice, 4);
        
        decimal minSize = GetMinSize(assetId);
        if (shares < minSize)
        {
            _pendingOrders.TryRemove(assetId, out _); _blockedLogSent.TryRemove(assetId, out _);
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

                        // Register a signal so UserStream can wake us up instantly
                        var fillSignal = new TaskCompletionSource<(decimal Size, decimal Price)>(TaskCreationOptions.RunContinuationsAsynchronously);
                        _fillSignals[assetId] = fillSignal;

                        for (int i = 0; i < 180; i++) // 180 × 500ms = 90 seconds
                        {
                            var delayTask = Task.Delay(500);
                            var completedTask = await Task.WhenAny(delayTask, fillSignal.Task);

                            // --- THE PHANTOM REFUND FIX ---
                            if (completedTask == fillSignal.Task)
                            {
                                var wsData = fillSignal.Task.Result;
                                actualShares = wsData.Size;
                                actualDollars = wsData.Size * wsData.Price;
                                settled = true;

                                if (!IsMuted) lock (ConsoleLock)
                                {
                                    Console.ForegroundColor = ConsoleColor.Cyan;
                                    Console.WriteLine($"\n[{DateTime.Now:HH:mm:ss.fff}] [{StrategyName}] [USER STREAM] Buy order confirmed instantly via WebSocket.");
                                    Console.ResetColor();
                                }
                                break; // Skip the REST API entirely!
                            }

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
                                    if (!IsMuted) lock (ConsoleLock)
                                    {
                                        Console.ForegroundColor = ConsoleColor.DarkYellow;
                                        Console.WriteLine($"\n[{DateTime.Now:HH:mm:ss.fff}] [{StrategyName}] [POLL] Buy {orderId} API returned '{currentStatus}' at iteration {i}/180 | {GetMarketName(assetId)}");
                                        Console.ResetColor();
                                    }
                                    // API says killed, but Polymarket can deliver shares despite this.
                                    // Register ghost so UserStream or next sync can catch a phantom fill.
                                    _ghostOrders[assetId] = new GhostOrder(assetId, orderId, "BUY", targetPrice, shares, DateTime.UtcNow);
                                    actualShares = 0;
                                    settled = true;
                                    break;
                                }
                            }
                            catch { /* Ignore parsing or network timeouts during polling */ }
                        }

                        // Clean up the signal
                        _fillSignals.TryRemove(assetId, out _);

                        // On timeout: one final poll attempt before giving up
                        if (!settled)
                        {
                            try
                            {
                                string finalPoll = await _orderClient.GetOrderAsync(orderId);
                                using var finalDoc = System.Text.Json.JsonDocument.Parse(finalPoll);
                                var finalRoot = finalDoc.RootElement;
                                JsonElement finalData = finalRoot;
                                if (finalRoot.ValueKind == JsonValueKind.Array && finalRoot.GetArrayLength() > 0)
                                    finalData = finalRoot[0];

                                string finalStatus = finalData.TryGetProperty("status", out var finalStatusEl) ? finalStatusEl.GetString() : "";

                                if (finalStatus == "matched" || finalStatus == "live")
                                {
                                    if (finalData.TryGetProperty("taker_amount_matched", out var takerEl) && decimal.TryParse(takerEl.ToString(), out decimal matchedShares))
                                        actualShares = matchedShares;
                                    if (finalData.TryGetProperty("maker_amount_matched", out var makerEl) && decimal.TryParse(makerEl.ToString(), out decimal matchedUsdc))
                                        actualDollars = matchedUsdc;

                                    if (!IsMuted) lock (ConsoleLock)
                                    {
                                        Console.ForegroundColor = ConsoleColor.Yellow;
                                        Console.WriteLine($"\n[{DateTime.Now:HH:mm:ss.fff}] [{StrategyName}] [LATE FILL] Buy {orderId} filled after timeout! Shares: {actualShares:0.00}");
                                        Console.ResetColor();
                                    }
                                }
                                else
                                {
                                    actualShares = 0;
                                    // Register as ghost order — UserStream may catch the fill later
                                    _ghostOrders[assetId] = new GhostOrder(assetId, orderId, "BUY", targetPrice, shares, DateTime.UtcNow);
                                    if (!IsMuted) lock (ConsoleLock)
                                    {
                                        Console.ForegroundColor = ConsoleColor.DarkYellow;
                                        Console.WriteLine($"\n[{DateTime.Now:HH:mm:ss.fff}] [{StrategyName}] [GHOST] Buy {orderId} still '{finalStatus}' after 90s. Registered as ghost order — UserStream watching.");
                                        Console.ResetColor();
                                    }
                                }
                            }
                            catch
                            {
                                // Network error on final check — register ghost rather than assume killed
                                actualShares = 0;
                                _ghostOrders[assetId] = new GhostOrder(assetId, orderId, "BUY", targetPrice, shares, DateTime.UtcNow);
                            }
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
                        Interlocked.Increment(ref _missedBuys);
                        if (!IsMuted) lock (ConsoleLock)
                        {
                            Console.ForegroundColor = ConsoleColor.DarkGray;
                            Console.WriteLine($"\n[{DateTime.Now:HH:mm:ss.fff}] [{StrategyName}] [FAK KILLED] Buy missed. Liquidity gone @ ${targetPrice:0.00} | {GetMarketName(assetId)}");
                            Console.ResetColor();
                        }
                        return; // Exits safely without touching ledger
                    }

                    // Fee adjustment: on buys, Polymarket collects fees in SHARES.
                    // Formula: fee_usdc = C × p × feeRate × (p × (1-p))^exponent
                    // fee_shares = fee_usdc / p = C × feeRate × (p × (1-p))^exponent
                    // Crypto: feeRate=0.25, exponent=2 | Sports: feeRate=0.0175, exponent=1
                    int feeRateBps = GetFeeRate(assetId);
                    if (feeRateBps > 0 && actualShares > 0 && actualDollars > 0)
                    {
                        decimal execPrice = actualDollars / actualShares;
                        decimal pq = execPrice * (1m - execPrice); // p × (1-p)

                        // Determine fee params from feeRateBps
                        decimal feeRate;
                        int exponent;
                        if (feeRateBps == 1000) { feeRate = 0.25m; exponent = 2; }       // Crypto
                        else                    { feeRate = 0.0175m; exponent = 1; }      // Sports

                        decimal pqPow = pq;
                        for (int e = 1; e < exponent; e++) pqPow *= pq;

                        decimal feeShares = actualShares * feeRate * pqPow;
                        decimal adjustedShares = Math.Floor((actualShares - feeShares) * 100m) / 100m; // Floor to 2dp to match chain

                        if (!IsMuted) lock (ConsoleLock)
                        {
                            Console.ForegroundColor = ConsoleColor.DarkYellow;
                            Console.WriteLine($"[{DateTime.Now:HH:mm:ss.fff}] [{StrategyName}] [FEE] {feeRateBps}bps @ p={execPrice:0.00}: {actualShares:0.00} gross - {feeShares:0.0000} fee = {adjustedShares:0.00} net shares");
                            Console.ResetColor();
                        }
                        actualShares = adjustedShares;
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
                // Only release the pending lock if there's no ghost order for this asset.
                if (!_ghostOrders.ContainsKey(assetId))
                    _pendingOrders.TryRemove(assetId, out _); _blockedLogSent.TryRemove(assetId, out _);
            }
        });
    }

    public override void SubmitSellAllOrder(string assetId, decimal targetPrice, LocalOrderBook book)
    {
        decimal sharesToSell = Math.Round(GetPositionShares(assetId), 2);
        if (sharesToSell <= 0) return;

        // Check sell cooldown — after FAK failures (no liquidity), wait before retrying
        if (_sellCooldownUntil.TryGetValue(assetId, out long cooldownUntil) &&
            DateTimeOffset.UtcNow.ToUnixTimeSeconds() < cooldownUntil)
            return;

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

        if (!_pendingOrders.TryAdd(assetId, true))
        {
            if (OrderDebugMode && !IsMuted && _blockedLogSent.TryAdd(assetId, true)) lock (ConsoleLock)
            {
                bool hasGhost = _ghostOrders.ContainsKey(assetId);
                Console.ForegroundColor = ConsoleColor.DarkGray;
                Console.WriteLine($"[{DateTime.Now:HH:mm:ss.fff}] [{StrategyName}] [BLOCKED] Sell skipped — {(hasGhost ? "ghost order pending" : "order in flight")} | {GetMarketName(assetId)}");
                Console.ResetColor();
            }
            return;
        }

        // --- SELL PRICING ---
        // FAK sell: the limit price is the MINIMUM we'll accept.
        // Set it to 0.01 (floor) so the order matches any buyer on the book.
        // The exchange fills at the actual best bid, not our limit — so we won't
        // get ripped off, we just guarantee the FAK won't be killed for price reasons.
        decimal bestBid = book.GetBestBidPrice();

        // No liquidity — don't sell into an empty book
        if (bestBid <= 0.01m)
        {
            _pendingOrders.TryRemove(assetId, out _); _blockedLogSent.TryRemove(assetId, out _);
            return;
        }

        // Floor the price — we want OUT, especially on stop-loss
        targetPrice = 0.01m;

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

                        /// Register a signal so UserStream can wake us up instantly
                        var fillSignal = new TaskCompletionSource<(decimal Size, decimal Price)>(TaskCreationOptions.RunContinuationsAsynchronously);
                        _fillSignals[assetId] = fillSignal;

                        for (int i = 0; i < 180; i++) // 180 × 500ms = 90 seconds
                        {
                            var delayTask = Task.Delay(500);
                            var completedTask = await Task.WhenAny(delayTask, fillSignal.Task);

                            // --- THE PHANTOM REFUND FIX ---
                            if (completedTask == fillSignal.Task)
                            {
                                var wsData = fillSignal.Task.Result;
                                actualSharesSold = wsData.Size;
                                cashReceived = wsData.Size * wsData.Price;
                                settled = true;
                                
                                if (!IsMuted) lock (ConsoleLock)
                                {
                                    Console.ForegroundColor = ConsoleColor.Cyan;
                                    Console.WriteLine($"\n[{DateTime.Now:HH:mm:ss.fff}] [{StrategyName}] [USER STREAM] Sell order confirmed instantly via WebSocket.");
                                    Console.ResetColor();
                                }
                                break; // Skip the REST API entirely!
                            }

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
                                    if (!IsMuted) lock (ConsoleLock)
                                    {
                                        Console.ForegroundColor = ConsoleColor.DarkYellow;
                                        Console.WriteLine($"\n[{DateTime.Now:HH:mm:ss.fff}] [{StrategyName}] [POLL] Sell {orderId} API returned '{currentStatus}' at iteration {i}/180 | {GetMarketName(assetId)}");
                                        Console.ResetColor();
                                    }
                                    _ghostOrders[assetId] = new GhostOrder(assetId, orderId, "SELL", targetPrice, sharesToSell, DateTime.UtcNow);
                                    actualSharesSold = 0;
                                    cashReceived = 0;
                                    settled = true;
                                    break;
                                }
                            }
                            catch { /* Ignore polling errors */ }
                        }

                        // Clean up the signal
                        _fillSignals.TryRemove(assetId, out _);

                        // On timeout: one final poll attempt before giving up
                        if (!settled)
                        {
                            try
                            {
                                string finalPoll = await _orderClient.GetOrderAsync(orderId);
                                using var finalDoc = System.Text.Json.JsonDocument.Parse(finalPoll);
                                var finalRoot = finalDoc.RootElement;
                                JsonElement finalData = finalRoot;
                                if (finalRoot.ValueKind == JsonValueKind.Array && finalRoot.GetArrayLength() > 0)
                                    finalData = finalRoot[0];

                                string finalStatus = finalData.TryGetProperty("status", out var finalStatusEl) ? finalStatusEl.GetString() : "";

                                if (finalStatus == "matched" || finalStatus == "live")
                                {
                                    if (finalData.TryGetProperty("maker_amount_matched", out var makerEl) && decimal.TryParse(makerEl.ToString(), out decimal matchedShares))
                                        actualSharesSold = matchedShares;
                                    if (finalData.TryGetProperty("taker_amount_matched", out var takerEl) && decimal.TryParse(takerEl.ToString(), out decimal matchedUsdc))
                                        cashReceived = matchedUsdc;

                                    if (!IsMuted) lock (ConsoleLock)
                                    {
                                        Console.ForegroundColor = ConsoleColor.Yellow;
                                        Console.WriteLine($"\n[{DateTime.Now:HH:mm:ss.fff}] [{StrategyName}] [LATE FILL] Sell {orderId} filled after timeout! Shares: {actualSharesSold:0.00}");
                                        Console.ResetColor();
                                    }
                                }
                                else
                                {
                                    actualSharesSold = 0;
                                    cashReceived = 0;
                                    // Register as ghost order — UserStream may catch the fill later
                                    _ghostOrders[assetId] = new GhostOrder(assetId, orderId, "SELL", targetPrice, sharesToSell, DateTime.UtcNow);
                                    if (!IsMuted) lock (ConsoleLock)
                                    {
                                        Console.ForegroundColor = ConsoleColor.DarkYellow;
                                        Console.WriteLine($"\n[{DateTime.Now:HH:mm:ss.fff}] [{StrategyName}] [GHOST] Sell {orderId} still '{finalStatus}' after 90s. Registered as ghost order — UserStream watching.");
                                        Console.ResetColor();
                                    }
                                }
                            }
                            catch
                            {
                                actualSharesSold = 0;
                                cashReceived = 0;
                                _ghostOrders[assetId] = new GhostOrder(assetId, orderId, "SELL", targetPrice, sharesToSell, DateTime.UtcNow);
                            }
                        }
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
                        Interlocked.Increment(ref _missedSells);
                        _sellCooldownUntil[assetId] = DateTimeOffset.UtcNow.ToUnixTimeSeconds() + SELL_COOLDOWN_SECONDS;
                        if (!IsMuted) lock (ConsoleLock)
                        {
                            Console.ForegroundColor = ConsoleColor.DarkGray;
                            Console.WriteLine($"\n[{DateTime.Now:HH:mm:ss.fff}] [{StrategyName}] [FAK KILLED] Sell missed. Cooldown {SELL_COOLDOWN_SECONDS}s | {GetMarketName(assetId)}");
                            Console.ResetColor();
                        }
                        return;
                    }

                    // Sell succeeded — clear any cooldown
                    _sellCooldownUntil.TryRemove(assetId, out _);

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
                    // FAK killed (no liquidity) — apply cooldown to prevent rapid-fire retries
                    if (ex.Message.Contains("no orders found to match", StringComparison.OrdinalIgnoreCase) ||
                        ex.Message.Contains("FAK", StringComparison.OrdinalIgnoreCase))
                    {
                        _sellCooldownUntil[assetId] = DateTimeOffset.UtcNow.ToUnixTimeSeconds() + SELL_COOLDOWN_SECONDS;
                    }

                    if (!IsMuted) lock (ConsoleLock)
                    {
                        Console.ForegroundColor = ConsoleColor.Red;
                        Console.WriteLine($"\n[{DateTime.Now:HH:mm:ss.fff}] [{StrategyName}] [LIVE SELL FAILED] {GetMarketName(assetId)}: {ex.Message} (cooldown {SELL_COOLDOWN_SECONDS}s)");
                        Console.ResetColor();
                    }
                }
            }
            finally
            {
                // Only release the pending lock if there's no ghost order for this asset.
                // If a ghost sell was registered, keep the lock so the strategy can't
                // fire another sell before ReconcileGhostFill runs.
                if (!_ghostOrders.ContainsKey(assetId))
                    _pendingOrders.TryRemove(assetId, out _); _blockedLogSent.TryRemove(assetId, out _);
            }
        });
    }

    /// <summary>
    /// Called by the UserStream when a trade fill is detected on our account.
    /// Only reconciles if there's a matching ghost order (polling timed out).
    /// Returns true if a ghost order was reconciled.
    /// </summary>
    public bool ReconcileGhostFill(UserTradeEvent fill)
    {
        // Only act if there's a ghost order for this asset
        if (!_ghostOrders.TryRemove(fill.TokenId, out var ghost))
            return false; // No ghost order — this fill was already handled by the polling loop

        // Verify the side matches
        if (!ghost.Side.Equals(fill.Side, StringComparison.OrdinalIgnoreCase))
        {
            // Side mismatch — put it back and ignore
            _ghostOrders.TryAdd(fill.TokenId, ghost);
            return false;
        }

        decimal fillDollars = fill.Size * fill.Price;

        lock (BrokerLock)
        {
            if (fill.Side.Equals("BUY", StringComparison.OrdinalIgnoreCase))
            {
                decimal currentShares = GetPositionShares(fill.TokenId);
                decimal currentAvgPrice = GetAverageEntryPrice(fill.TokenId);
                decimal totalCost = (currentShares * currentAvgPrice) + fillDollars;

                SetPositionShares(fill.TokenId, currentShares + fill.Size);
                SetAverageEntryPrice(fill.TokenId, totalCost / (currentShares + fill.Size));
                CashBalance -= fillDollars;

                TradeLedger.Add(new ExecutedTrade
                {
                    OutcomeId = fill.TokenId, Date = DateTime.Now, Side = "BUY (GHOST)",
                    Price = fill.Price, Shares = fill.Size, DollarValue = fillDollars
                });
                TotalActions++;
            }
            else // SELL
            {
                decimal entryPrice = GetAverageEntryPrice(fill.TokenId);
                decimal pnl = fillDollars - (fill.Size * entryPrice);

                if (fill.Price > entryPrice) WinningTrades++;
                else LosingTrades++;

                CashBalance += fillDollars;
                decimal remainingShares = Math.Max(0, GetPositionShares(fill.TokenId) - fill.Size);
                SetPositionShares(fill.TokenId, remainingShares);
                if (remainingShares == 0) SetAverageEntryPrice(fill.TokenId, 0);

                TradeLedger.Add(new ExecutedTrade
                {
                    OutcomeId = fill.TokenId, Date = DateTime.Now, Side = "SELL (GHOST)",
                    Price = fill.Price, Shares = fill.Size, DollarValue = fillDollars
                });
                TotalTradesExecuted++;
                TotalActions++;
            }
        }

        // Release the pending order lock now that the ghost is reconciled.
        // This allows the strategy to submit new orders for this asset.
        _pendingOrders.TryRemove(fill.TokenId, out _); _blockedLogSent.TryRemove(fill.TokenId, out _);

        if (!IsMuted) lock (ConsoleLock)
        {
            Console.ForegroundColor = ConsoleColor.Magenta;
            Console.WriteLine($"\n[{DateTime.Now:HH:mm:ss.fff}] [{StrategyName}] [GHOST RECONCILED] {fill.Side} {fill.Size:0.00} shares @ ${fill.Price:0.000} | {GetMarketName(fill.TokenId)} | Order: {ghost.OrderId}");
            Console.ResetColor();
        }

        return true;
    }

    /// <summary>
    /// Cleans up ghost orders older than the specified age (e.g. stale orders that will never fill).
    /// Returns count of purged entries.
    /// </summary>
    public int PurgeStaleGhostOrders(TimeSpan maxAge)
    {
        int purged = 0;
        var cutoff = DateTime.UtcNow - maxAge;
        foreach (var kvp in _ghostOrders)
        {
            if (kvp.Value.CreatedAt < cutoff)
            {
                if (_ghostOrders.TryRemove(kvp.Key, out var staleGhost))
                {
                    // Release the pending order lock that was held for this ghost
                    _pendingOrders.TryRemove(kvp.Key, out _); _blockedLogSent.TryRemove(kvp.Key, out _);
                    purged++;

                    if (!IsMuted) lock (ConsoleLock)
                    {
                        Console.ForegroundColor = ConsoleColor.DarkGray;
                        Console.WriteLine($"[{DateTime.Now:HH:mm:ss.fff}] [{StrategyName}] [GHOST PURGED] {staleGhost.Side} ghost for {GetMarketName(kvp.Key)} expired after {maxAge.TotalMinutes:0}m — order lock released.");
                        Console.ResetColor();
                    }
                }
            }
        }
        return purged;
    }
}