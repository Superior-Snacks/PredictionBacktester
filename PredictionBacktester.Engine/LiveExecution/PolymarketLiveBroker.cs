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
        if (shares <= 0 || dollarsToInvest < minSize)
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

                // If we get here, the order was accepted by the CLOB
                // Record it locally so position tracking and CSV export work
                lock (BrokerLock)
                {
                    decimal executionPrice = targetPrice;
                    decimal actualDollars = shares * executionPrice;

                    if (actualDollars > CashBalance)
                    {
                        shares = CashBalance / executionPrice;
                        actualDollars = shares * executionPrice;
                    }

                    if (shares <= 0) return;

                    // Update local position tracking
                    decimal currentShares = GetPositionShares(assetId);
                    decimal currentAvgPrice = GetAverageEntryPrice(assetId);
                    decimal totalCost = (currentShares * currentAvgPrice) + actualDollars;

                    SetPositionShares(assetId, currentShares + shares);
                    SetAverageEntryPrice(assetId, totalCost / (currentShares + shares));
                    CashBalance -= actualDollars;

                    TradeLedger.Add(new ExecutedTrade
                    {
                        OutcomeId = assetId,
                        Date = DateTime.Now,
                        Side = "BUY",
                        Price = executionPrice,
                        Shares = shares,
                        DollarValue = actualDollars
                    });
                    TotalActions++;
                }

                lock (ConsoleLock)
                {
                    Console.ForegroundColor = ConsoleColor.Green;
                    Console.WriteLine($"\n[{DateTime.Now:HH:mm:ss.fff}] [{StrategyName}] [LIVE BUY] {shares:0.00} shares @ ${targetPrice:0.00} | ${shares * targetPrice:0.00} | {GetMarketName(assetId)}");
                    Console.WriteLine($"  API Response: {result}");
                    Console.ResetColor();
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
        decimal currentShares = GetPositionShares(assetId);
        if (currentShares <= 0) return;

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
                // Sell all shares we're holding
                decimal sharesToSell = GetPositionShares(assetId);
                if (sharesToSell <= 0)
                {
                    _pendingOrders.TryRemove(assetId, out _);
                    return;
                }

                string result = await _orderClient.SubmitOrderAsync(
                    assetId, targetPrice, sharesToSell, side: 1, GetNegRisk(assetId), GetTickSize(assetId));

                // Record locally
                decimal cashReceived;
                decimal pnl;
                lock (BrokerLock)
                {
                    decimal entryPrice = GetAverageEntryPrice(assetId);
                    cashReceived = sharesToSell * targetPrice;
                    pnl = (targetPrice - entryPrice) * sharesToSell;

                    if (targetPrice > entryPrice) WinningTrades++;
                    else LosingTrades++;

                    TradeLedger.Add(new ExecutedTrade
                    {
                        OutcomeId = assetId,
                        Date = DateTime.Now,
                        Side = "SELL",
                        Price = targetPrice,
                        Shares = sharesToSell,
                        DollarValue = cashReceived
                    });

                    CashBalance += cashReceived;
                    SetPositionShares(assetId, 0);
                    SetAverageEntryPrice(assetId, 0);
                    TotalTradesExecuted++;
                    TotalActions++;
                }

                lock (ConsoleLock)
                {
                    Console.ForegroundColor = pnl >= 0 ? ConsoleColor.Cyan : ConsoleColor.Red;
                    Console.WriteLine($"\n[{DateTime.Now:HH:mm:ss.fff}] [{StrategyName}] [LIVE SELL] {sharesToSell:0.00} shares @ ${targetPrice:0.00} | PnL: ${pnl:0.00} | ${cashReceived:0.00} | {GetMarketName(assetId)}");
                    Console.WriteLine($"  API Response: {result}");
                    Console.ResetColor();
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
