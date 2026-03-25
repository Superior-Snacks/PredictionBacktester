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
    private const int SELL_COOLDOWN_SECONDS = 5;
    private const int SETTLEMENT_COOLDOWN_SECONDS = 15; // Cooldown after HAMMER exhaustion — tokens haven't settled on-chain yet

    private int GetFeeRate(string assetId) => _tokenFeeRates.TryGetValue(assetId, out var fee) ? fee : 0;
    public PolymarketOrderClient OrderClient => _orderClient;
    public void SetTokenFeeRate(string assetId, int feeRate) => _tokenFeeRates[assetId] = feeRate;

    // UserStream fill signals — primary fill detection path
    private readonly System.Collections.Concurrent.ConcurrentDictionary<string, TaskCompletionSource<(decimal Size, decimal Price)>> _fillSignals = new();
    private const int WS_FILL_TIMEOUT_MS = 5000; // Wait up to 5s for UserStream fill before falling back to REST poll

    public void SignalFill(string assetId, decimal size, decimal price)
    {
        if (_fillSignals.TryRemove(assetId, out var tcs))
            tcs.TrySetResult((size, price));
    }

    // Optional telemetry callbacks (wired externally for test mode)
    public Action<decimal, decimal, string>? OnBuyFilled;           // (shares, price, fillSource)
    public Action? OnSellSubmitted;
    public Action<decimal, decimal, string, decimal>? OnSellFilled; // (shares, price, fillSource, pnl)

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
            if (!IsMuted) lock (ConsoleLock)
            {
                Console.ForegroundColor = ConsoleColor.DarkGray;
                Console.WriteLine($"[{DateTime.Now:HH:mm:ss.fff}] [{StrategyName}] [SKIP] Buy rejected — price {bestAsk:0.00} out of range | {GetMarketName(assetId)}");
                Console.ResetColor();
            }
            _pendingOrders.TryRemove(assetId, out _); _blockedLogSent.TryRemove(assetId, out _);
            return;
        }

        // Allow 1 cent of positive slippage to cross the spread and guarantee the fill
        targetPrice = Math.Min(0.99m, targetPrice + 0.01m); 
        
        decimal shares = Math.Round(dollarsToInvest / targetPrice, 4);
        
        decimal minSize = GetMinSize(assetId);
        if (shares < minSize)
        {
            if (!IsMuted) lock (ConsoleLock)
            {
                Console.ForegroundColor = ConsoleColor.DarkGray;
                Console.WriteLine($"[{DateTime.Now:HH:mm:ss.fff}] [{StrategyName}] [SKIP] Buy rejected — {shares:0.00} shares below min {minSize} | {GetMarketName(assetId)}");
                Console.ResetColor();
            }
            _pendingOrders.TryRemove(assetId, out _); _blockedLogSent.TryRemove(assetId, out _);
            return;
        }

        // RECALCULATE dollarsToInvest based on the strict 2-decimal API rule
        dollarsToInvest = Math.Round(shares * targetPrice, 2);

        Task.Run(async () =>
        {
            try
            {
                // Register UserStream fill signal BEFORE posting — so WebSocket can signal even during HTTP round-trip
                var fillSignal = new TaskCompletionSource<(decimal Size, decimal Price)>(TaskCreationOptions.RunContinuationsAsynchronously);
                _fillSignals[assetId] = fillSignal;

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

                    decimal actualShares = 0;
                    decimal actualDollars = 0;
                    string fillSource = "";

                    // --- Step 1: Check if UserStream already delivered the fill during the HTTP round-trip ---
                    if (fillSignal.Task.IsCompleted)
                    {
                        var wsData = fillSignal.Task.Result;
                        actualShares = wsData.Size;
                        actualDollars = wsData.Size * wsData.Price;
                        fillSource = "WS-INSTANT";
                    }
                    // --- Step 2: Try reading fill data from the POST response ---
                    else if (TryExtractFillFromResponse(root, isSell: false, out decimal respShares, out decimal respDollars) && respShares > 0)
                    {
                        actualShares = respShares;
                        actualDollars = respDollars > 0 ? respDollars : respShares * targetPrice;
                        fillSource = "RESPONSE";
                        _fillSignals.TryRemove(assetId, out _); // Don't need WS anymore
                    }
                    // --- Step 3: Wait for UserStream fill (primary path for delayed/matched-without-data) ---
                    else
                    {
                        if (!IsMuted && (status == "delayed" || status == "unmatched")) lock (ConsoleLock)
                        {
                            Console.ForegroundColor = ConsoleColor.DarkYellow;
                            Console.WriteLine($"\n[{DateTime.Now:HH:mm:ss.fff}] [{StrategyName}] [{status.ToUpper()}] Buy {orderId} — waiting for UserStream fill ({WS_FILL_TIMEOUT_MS}ms)...");
                            Console.ResetColor();
                        }

                        var completed = await Task.WhenAny(fillSignal.Task, Task.Delay(WS_FILL_TIMEOUT_MS));

                        if (completed == fillSignal.Task)
                        {
                            var wsData = fillSignal.Task.Result;
                            actualShares = wsData.Size;
                            actualDollars = wsData.Size * wsData.Price;
                            fillSource = "WS";
                        }
                        else
                        {
                            // --- Step 4: Single REST poll as last resort ---
                            _fillSignals.TryRemove(assetId, out _);

                            if (!string.IsNullOrEmpty(orderId))
                            {
                                try
                                {
                                    string pollResult = await _orderClient.GetOrderAsync(orderId);
                                    using var pollDoc = System.Text.Json.JsonDocument.Parse(pollResult);
                                    var pollRoot = pollDoc.RootElement;
                                    JsonElement orderData = pollRoot.ValueKind == JsonValueKind.Array && pollRoot.GetArrayLength() > 0
                                        ? pollRoot[0] : pollRoot;

                                    string pollStatus = orderData.TryGetProperty("status", out var ps) ? ps.GetString() ?? "" : "";

                                    if (pollStatus == "matched" || pollStatus == "live")
                                    {
                                        if (orderData.TryGetProperty("size_matched", out var smEl) && decimal.TryParse(smEl.ToString(), out decimal sm))
                                            actualShares = sm;
                                        else if (orderData.TryGetProperty("taker_amount_matched", out var takerEl) && decimal.TryParse(takerEl.ToString(), out sm))
                                            actualShares = sm;

                                        if (orderData.TryGetProperty("maker_amount_matched", out var makerEl) && decimal.TryParse(makerEl.ToString(), out decimal md))
                                            actualDollars = md;

                                        fillSource = "POLL";
                                    }
                                    else
                                    {
                                        // Canceled/expired/unmatched — register ghost, UserStream may still deliver
                                        _ghostOrders[assetId] = new GhostOrder(assetId, orderId, "BUY", targetPrice, shares, DateTime.UtcNow);
                                        if (!IsMuted) lock (ConsoleLock)
                                        {
                                            Console.ForegroundColor = ConsoleColor.DarkYellow;
                                            Console.WriteLine($"\n[{DateTime.Now:HH:mm:ss.fff}] [{StrategyName}] [GHOST] Buy {orderId} status='{pollStatus}'. Ghost registered.");
                                            Console.ResetColor();
                                        }
                                    }
                                }
                                catch (Exception pollEx)
                                {
                                    _ghostOrders[assetId] = new GhostOrder(assetId, orderId, "BUY", targetPrice, shares, DateTime.UtcNow);
                                    if (!IsMuted) lock (ConsoleLock)
                                    {
                                        Console.ForegroundColor = ConsoleColor.DarkYellow;
                                        Console.WriteLine($"[{DateTime.Now:HH:mm:ss.fff}] [{StrategyName}] [POLL FAILED] Buy poll error for {GetMarketName(assetId)}: {pollEx.Message}. Ghost registered.");
                                        Console.ResetColor();
                                    }
                                }
                            }
                        }
                    }

                    // Clean up signal if still registered
                    _fillSignals.TryRemove(assetId, out _);

                    if (actualShares <= 0)
                    {
                        Interlocked.Increment(ref _missedBuys);
                        if (!IsMuted && !_ghostOrders.ContainsKey(assetId)) lock (ConsoleLock)
                        {
                            Console.ForegroundColor = ConsoleColor.DarkGray;
                            Console.WriteLine($"\n[{DateTime.Now:HH:mm:ss.fff}] [{StrategyName}] [FAK KILLED] Buy missed. Liquidity gone @ ${targetPrice:0.00} | {GetMarketName(assetId)}");
                            Console.ResetColor();
                        }
                        return;
                    }

                    // Fee adjustment: on buys, Polymarket collects fees in SHARES
                    int feeRateBps = GetFeeRate(assetId);
                    if (feeRateBps > 0 && actualShares > 0 && actualDollars > 0)
                    {
                        decimal execPrice = actualDollars / actualShares;
                        decimal pq = execPrice * (1m - execPrice);

                        decimal feeRate;
                        int exponent;
                        if (feeRateBps == 1000) { feeRate = 0.25m; exponent = 2; }
                        else                    { feeRate = 0.0175m; exponent = 1; }

                        decimal pqPow = pq;
                        for (int e = 1; e < exponent; e++) pqPow *= pq;

                        decimal feeShares = actualShares * feeRate * pqPow;
                        decimal adjustedShares = Math.Floor((actualShares - feeShares) * 100m) / 100m;

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
                        Console.WriteLine($"\n[{DateTime.Now:HH:mm:ss.fff}] [{StrategyName}] [LIVE BUY FAK] {actualShares:0.00} shares @ exactly ${exactExecutionPrice:0.000} | ${actualDollars:0.00} | via {fillSource} | {GetMarketName(assetId)}");
                        Console.ResetColor();
                    }

                    OnBuyFilled?.Invoke(actualShares, actualDollars / actualShares, fillSource);

                    // Immediately tell the CLOB to refresh its cached balance — gives the
                    // settlement lock window a head start so sells succeed on first attempt
                    _ = _orderClient.UpdateBalanceAllowanceAsync(assetId);
                }
                else
                {
                    // API returned success=false — log the error
                    _fillSignals.TryRemove(assetId, out _);
                    string errorMsg = root.TryGetProperty("errorMsg", out var errEl) ? errEl.GetString() ?? "" : "";
                    Interlocked.Increment(ref _rejectedOrders);
                    if (!IsMuted) lock (ConsoleLock)
                    {
                        Console.ForegroundColor = ConsoleColor.Red;
                        Console.WriteLine($"\n[{DateTime.Now:HH:mm:ss.fff}] [{StrategyName}] [BUY REJECTED] {GetMarketName(assetId)}: {errorMsg}");
                        Console.ResetColor();
                    }
                }
            }
            catch (Exception ex)
            {
                _fillSignals.TryRemove(assetId, out _);
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
                if (!_ghostOrders.ContainsKey(assetId))
                    _pendingOrders.TryRemove(assetId, out _);
                _blockedLogSent.TryRemove(assetId, out _);
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
        {
            if (OrderDebugMode && !IsMuted) lock (ConsoleLock)
            {
                Console.ForegroundColor = ConsoleColor.DarkGray;
                Console.WriteLine($"[{DateTime.Now:HH:mm:ss.fff}] [{StrategyName}] [COOLDOWN] Sell skipped — {cooldownUntil - DateTimeOffset.UtcNow.ToUnixTimeSeconds()}s remaining | {GetMarketName(assetId)}");
                Console.ResetColor();
            }
            return;
        }

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
            if (!IsMuted) lock (ConsoleLock)
            {
                Console.ForegroundColor = ConsoleColor.DarkGray;
                Console.WriteLine($"[{DateTime.Now:HH:mm:ss.fff}] [{StrategyName}] [SKIP] Sell rejected — no bid liquidity (best bid {bestBid:0.00}) | {GetMarketName(assetId)}");
                Console.ResetColor();
            }
            _pendingOrders.TryRemove(assetId, out _); _blockedLogSent.TryRemove(assetId, out _);
            return;
        }

        // Floor the price — we want OUT, especially on stop-loss
        targetPrice = 0.01m;

        Task.Run(async () =>
        {
            try
            {
                OnSellSubmitted?.Invoke();

                // Force CLOB to refresh its cached balance from on-chain state
                try { await _orderClient.UpdateBalanceAllowanceAsync(assetId); }
                catch { /* best-effort */ }

                // Register UserStream fill signal BEFORE posting
                var fillSignal = new TaskCompletionSource<(decimal Size, decimal Price)>(TaskCreationOptions.RunContinuationsAsynchronously);
                _fillSignals[assetId] = fillSignal;

                string result = "";
                int maxRetries = 20;
                int retryDelayMs = 500;

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
                        if (!IsMuted && (attempt == 1 || attempt % 5 == 0)) lock (ConsoleLock)
                        {
                            Console.ForegroundColor = ConsoleColor.DarkYellow;
                            Console.WriteLine($"[{DateTime.Now:HH:mm:ss.fff}] [HAMMER] Tokens not settled yet. Retrying in {retryDelayMs}ms... (Attempt {attempt}/{maxRetries})");
                            Console.ResetColor();
                        }
                        await Task.Delay(retryDelayMs);
                    }
                }

                if (string.IsNullOrEmpty(result)) return;

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
                    string status = root.TryGetProperty("status", out var statusEl) ? statusEl.GetString() ?? "" : "";
                    string orderId = root.TryGetProperty("orderID", out var orderIdEl) ? orderIdEl.GetString() ?? "" : "";

                    decimal actualSharesSold = 0;
                    decimal cashReceived = 0;
                    string fillSource = "";

                    // --- Step 1: Check if UserStream already delivered the fill during the HTTP round-trip ---
                    if (fillSignal.Task.IsCompleted)
                    {
                        var wsData = fillSignal.Task.Result;
                        actualSharesSold = wsData.Size;
                        cashReceived = wsData.Size * wsData.Price;
                        fillSource = "WS-INSTANT";
                    }
                    // --- Step 2: Try reading fill data from the POST response ---
                    else if (TryExtractFillFromResponse(root, isSell: true, out decimal respShares, out decimal respDollars) && respShares > 0)
                    {
                        actualSharesSold = respShares;
                        cashReceived = respDollars > 0 ? respDollars : respShares * bestBid; // estimate if missing
                        fillSource = "RESPONSE";
                        _fillSignals.TryRemove(assetId, out _);
                    }
                    // --- Step 3: Wait for UserStream fill ---
                    else
                    {
                        if (!IsMuted && (status == "delayed" || status == "unmatched")) lock (ConsoleLock)
                        {
                            Console.ForegroundColor = ConsoleColor.DarkYellow;
                            Console.WriteLine($"\n[{DateTime.Now:HH:mm:ss.fff}] [{StrategyName}] [{status.ToUpper()}] Sell {orderId} — waiting for UserStream fill ({WS_FILL_TIMEOUT_MS}ms)...");
                            Console.ResetColor();
                        }

                        var completed = await Task.WhenAny(fillSignal.Task, Task.Delay(WS_FILL_TIMEOUT_MS));

                        if (completed == fillSignal.Task)
                        {
                            var wsData = fillSignal.Task.Result;
                            actualSharesSold = wsData.Size;
                            cashReceived = wsData.Size * wsData.Price;
                            fillSource = "WS";
                        }
                        else
                        {
                            // --- Step 4: Single REST poll as last resort ---
                            _fillSignals.TryRemove(assetId, out _);

                            if (!string.IsNullOrEmpty(orderId))
                            {
                                try
                                {
                                    string pollResult = await _orderClient.GetOrderAsync(orderId);
                                    using var pollDoc = System.Text.Json.JsonDocument.Parse(pollResult);
                                    var pollRoot = pollDoc.RootElement;
                                    JsonElement orderData = pollRoot.ValueKind == JsonValueKind.Array && pollRoot.GetArrayLength() > 0
                                        ? pollRoot[0] : pollRoot;

                                    string pollStatus = orderData.TryGetProperty("status", out var ps) ? ps.GetString() ?? "" : "";

                                    if (pollStatus == "matched" || pollStatus == "live")
                                    {
                                        if (orderData.TryGetProperty("size_matched", out var smEl) && decimal.TryParse(smEl.ToString(), out decimal sm))
                                            actualSharesSold = sm;
                                        else if (orderData.TryGetProperty("maker_amount_matched", out var makerEl) && decimal.TryParse(makerEl.ToString(), out sm))
                                            actualSharesSold = sm;

                                        if (orderData.TryGetProperty("taker_amount_matched", out var takerEl) && decimal.TryParse(takerEl.ToString(), out decimal md))
                                            cashReceived = md;

                                        fillSource = "POLL";
                                    }
                                    else
                                    {
                                        _ghostOrders[assetId] = new GhostOrder(assetId, orderId, "SELL", targetPrice, sharesToSell, DateTime.UtcNow);
                                        if (!IsMuted) lock (ConsoleLock)
                                        {
                                            Console.ForegroundColor = ConsoleColor.DarkYellow;
                                            Console.WriteLine($"\n[{DateTime.Now:HH:mm:ss.fff}] [{StrategyName}] [GHOST] Sell {orderId} status='{pollStatus}'. Ghost registered.");
                                            Console.ResetColor();
                                        }
                                    }
                                }
                                catch (Exception pollEx)
                                {
                                    _ghostOrders[assetId] = new GhostOrder(assetId, orderId, "SELL", targetPrice, sharesToSell, DateTime.UtcNow);
                                    if (!IsMuted) lock (ConsoleLock)
                                    {
                                        Console.ForegroundColor = ConsoleColor.DarkYellow;
                                        Console.WriteLine($"[{DateTime.Now:HH:mm:ss.fff}] [{StrategyName}] [POLL FAILED] Sell poll error for {GetMarketName(assetId)}: {pollEx.Message}. Ghost registered.");
                                        Console.ResetColor();
                                    }
                                }
                            }
                        }
                    }

                    // Clean up signal if still registered
                    _fillSignals.TryRemove(assetId, out _);

                    if (actualSharesSold <= 0)
                    {
                        Interlocked.Increment(ref _missedSells);
                        if (!_ghostOrders.ContainsKey(assetId))
                            _sellCooldownUntil[assetId] = DateTimeOffset.UtcNow.ToUnixTimeSeconds() + SELL_COOLDOWN_SECONDS;
                        if (!IsMuted && !_ghostOrders.ContainsKey(assetId)) lock (ConsoleLock)
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
                        Console.WriteLine($"\n[{DateTime.Now:HH:mm:ss.fff}] [{StrategyName}] [LIVE SELL FAK] {actualSharesSold:0.00} shares @ exactly ${exactExecutionPrice:0.000} | PnL: ${pnl:0.00} | via {fillSource} | {GetMarketName(assetId)}");
                        Console.ResetColor();
                    }

                    OnSellFilled?.Invoke(actualSharesSold, cashReceived / actualSharesSold, fillSource, pnl);
                }
                else
                {
                    // API returned success=false — log the error
                    _fillSignals.TryRemove(assetId, out _);
                    string errorMsg = root.TryGetProperty("errorMsg", out var errEl) ? errEl.GetString() ?? "" : "";
                    Interlocked.Increment(ref _rejectedOrders);

                    if (errorMsg.Contains("no orders found to match", StringComparison.OrdinalIgnoreCase) ||
                        errorMsg.Contains("FAK", StringComparison.OrdinalIgnoreCase))
                    {
                        _sellCooldownUntil[assetId] = DateTimeOffset.UtcNow.ToUnixTimeSeconds() + SELL_COOLDOWN_SECONDS;
                    }

                    if (!IsMuted) lock (ConsoleLock)
                    {
                        Console.ForegroundColor = ConsoleColor.Red;
                        Console.WriteLine($"\n[{DateTime.Now:HH:mm:ss.fff}] [{StrategyName}] [SELL REJECTED] {GetMarketName(assetId)}: {errorMsg}");
                        Console.ResetColor();
                    }
                }
            }
            catch (Exception ex)
            {
                _fillSignals.TryRemove(assetId, out _);
                Interlocked.Increment(ref _rejectedOrders);

                if (ex.Message.Contains("not enough balance", StringComparison.OrdinalIgnoreCase))
                {
                    // Settlement pending — HAMMER retries exhausted but tokens haven't arrived on-chain yet.
                    // Do NOT emergency sync (would zero out a valid position). Just set a long cooldown.
                    _sellCooldownUntil[assetId] = DateTimeOffset.UtcNow.ToUnixTimeSeconds() + SETTLEMENT_COOLDOWN_SECONDS;
                    if (!IsMuted) lock (ConsoleLock)
                    {
                        Console.ForegroundColor = ConsoleColor.DarkYellow;
                        Console.WriteLine($"\n[{DateTime.Now:HH:mm:ss.fff}] [{StrategyName}] [SETTLEMENT PENDING] Tokens not settled. Cooldown {SETTLEMENT_COOLDOWN_SECONDS}s | {GetMarketName(assetId)}");
                        Console.ResetColor();
                    }
                }
                else if (ex.Message.Contains("balance", StringComparison.OrdinalIgnoreCase) ||
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
                if (!_ghostOrders.ContainsKey(assetId))
                    _pendingOrders.TryRemove(assetId, out _);
                _blockedLogSent.TryRemove(assetId, out _);
            }
        });
    }

    /// <summary>
    /// Called by the UserStream when a trade fill is detected on our account.
    /// Only reconciles if there's a matching ghost order (polling timed out).
    /// Returns true if a ghost order was reconciled.
    /// </summary>
    /// <summary>
    /// Tries to extract fill amounts from the POST /order response.
    /// Returns true if shares > 0.
    /// For BUY:  takingAmount = shares received, makingAmount = USDC spent
    /// For SELL: takingAmount = USDC received,  makingAmount = shares sold
    /// </summary>
    private static bool TryExtractFillFromResponse(JsonElement root, bool isSell, out decimal shares, out decimal dollars)
    {
        shares = 0;
        dollars = 0;

        decimal takingVal = 0, makingVal = 0;

        if (root.TryGetProperty("takingAmount", out var takingEl) || root.TryGetProperty("taking_amount", out takingEl))
        {
            string? val = takingEl.ValueKind == JsonValueKind.String ? takingEl.GetString() : takingEl.ToString();
            if (!string.IsNullOrEmpty(val)) decimal.TryParse(val, out takingVal);
        }

        if (root.TryGetProperty("makingAmount", out var makingEl) || root.TryGetProperty("making_amount", out makingEl))
        {
            string? val = makingEl.ValueKind == JsonValueKind.String ? makingEl.GetString() : makingEl.ToString();
            if (!string.IsNullOrEmpty(val)) decimal.TryParse(val, out makingVal);
        }

        if (isSell)
        {
            shares = makingVal;  // maker provides shares
            dollars = takingVal; // taker receives USDC
        }
        else
        {
            shares = takingVal;  // taker receives shares
            dollars = makingVal; // maker provides USDC
        }

        return shares > 0;
    }

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