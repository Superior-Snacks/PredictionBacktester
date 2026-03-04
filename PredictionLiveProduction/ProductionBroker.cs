using System;
using System.Collections.Generic;
using System.Threading.Tasks;
using PredictionBacktester.Engine;
using PredictionBacktester.Engine.LiveExecution;
using Serilog;

namespace PredictionLiveProduction;

/// <summary>
/// Thin wrapper around PolymarketLiveBroker that enforces a hard cap
/// on the dollar amount of any single buy order.
/// </summary>
public class ProductionBroker : PolymarketLiveBroker
{
    public decimal MaxBetSize { get; set; } = decimal.MaxValue;

    public ProductionBroker(
        string strategyName,
        decimal initialCapital,
        PolymarketApiConfig config,
        Dictionary<string, string> tokenNames,
        bool negRisk = false)
        : base(strategyName, initialCapital, config, tokenNames, negRisk) { }

    /// <summary>
    /// Creates a production broker initialized with the real USDC balance from the wallet.
    /// </summary>
    public static async Task<ProductionBroker> CreateAsync(
        string strategyName,
        PolymarketApiConfig config,
        Dictionary<string, string> tokenNames,
        decimal maxBetSize,
        bool negRisk = false)
    {
        var tempClient = new PolymarketOrderClient(config);
        decimal balance = await tempClient.GetUsdcBalanceAsync();

        Console.ForegroundColor = ConsoleColor.Yellow;
        Console.WriteLine($"[LIVE] Wallet USDC balance: ${balance:0.00}");
        Console.ResetColor();

        return new ProductionBroker(strategyName, balance, config, tokenNames, negRisk)
        {
            MaxBetSize = maxBetSize
        };
    }

    public override void SubmitBuyOrder(string assetId, decimal targetPrice, decimal dollarsToInvest, LocalOrderBook book)
    {
        if (dollarsToInvest > MaxBetSize)
        {
            Log.Debug("Bet capped: ${Original:0.00} -> ${Capped:0.00} on {Asset}",
                dollarsToInvest, MaxBetSize, assetId[..Math.Min(8, assetId.Length)] + "...");
            dollarsToInvest = MaxBetSize;
        }

        base.SubmitBuyOrder(assetId, targetPrice, dollarsToInvest, book);
    }
}
