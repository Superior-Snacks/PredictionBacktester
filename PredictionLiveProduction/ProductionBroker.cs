using System;
using System.Collections.Generic;
using System.Linq;
using System.Threading.Tasks;
using PredictionBacktester.Engine;
using PredictionBacktester.Engine.LiveExecution;
using Serilog;
using System.IO;
using System.Text.Json;

namespace PredictionLiveProduction;

/// <summary>
/// Thin wrapper around PolymarketLiveBroker that enforces a hard cap
/// on the dollar amount of any single buy order and provides on-chain state sync.
/// </summary>
public class ProductionBroker : PolymarketLiveBroker
{
    public decimal MaxBetSize { get; set; } = decimal.MaxValue;
    private readonly PolymarketOrderClient _queryClient;

    public ProductionBroker(
        string strategyName,
        decimal initialCapital,
        PolymarketApiConfig config,
        Dictionary<string, string> tokenNames,
        Dictionary<string, bool> tokenNegRisk,
        Dictionary<string, string> tokenTickSizes,
        Dictionary<string, decimal> tokenMinSizes,
        PolymarketOrderClient queryClient)
        : base(strategyName, initialCapital, config, tokenNames, tokenNegRisk, tokenTickSizes, tokenMinSizes)
    {
        _queryClient = queryClient;
    }

    /// <summary>
    /// Creates a production broker initialized with the real USDC balance from the wallet.
    /// </summary>
    public static async Task<ProductionBroker> CreateAsync(
        string strategyName,
        PolymarketApiConfig config,
        Dictionary<string, string> tokenNames,
        Dictionary<string, bool> tokenNegRisk,
        Dictionary<string, string> tokenTickSizes,
        Dictionary<string, decimal> tokenMinSizes,
        decimal maxBetSize)
    {
        var client = new PolymarketOrderClient(config);
        decimal balance = await client.GetUsdcBalanceAsync();

        Console.ForegroundColor = ConsoleColor.Yellow;
        Console.WriteLine($"[LIVE] Wallet USDC balance: ${balance:0.00}");
        Console.ResetColor();

        return new ProductionBroker(strategyName, balance, config, tokenNames, tokenNegRisk, tokenTickSizes, tokenMinSizes, client)
        {
            MaxBetSize = maxBetSize
        };
    }

    public override void SubmitBuyOrder(string assetId, decimal targetPrice, decimal dollarsToInvest, LocalOrderBook book)
    {
        if (dollarsToInvest > MaxBetSize)
        {
            Log.Information("[CAP] ${Original:0.00} -> ${Capped:0.00} on {Asset}",
                dollarsToInvest, MaxBetSize, assetId[..Math.Min(8, assetId.Length)] + "...");
            dollarsToInvest = MaxBetSize;
        }

        decimal minSize = GetMinSize(assetId);
        decimal shares = dollarsToInvest / targetPrice;
        if (shares < minSize)
        {
            Log.Information("[REJECT] {Shares:0.00} shares (${Amount:0.00}) below market minimum {Min} shares on {Asset}",
                shares, dollarsToInvest, minSize, assetId[..Math.Min(8, assetId.Length)] + "...");
            return;
        }

        base.SubmitBuyOrder(assetId, targetPrice, dollarsToInvest, book);
    }

    /// <summary>
    /// Syncs the broker's internal USDC cash balance with the on-chain wallet balance.
    /// Returns the difference (positive = chain has more, negative = chain has less).
    /// </summary>
    public async Task<decimal> SyncCashBalanceAsync()
    {
        decimal onChainBalance = await _queryClient.GetUsdcBalanceAsync();
        decimal drift;
        lock (BrokerLock)
        {
            drift = onChainBalance - CashBalance;
            CashBalance = onChainBalance;
        }
        return drift;
    }

    /// <summary>
    /// Syncs on-chain conditional token positions with the broker's internal state.
    /// For each subscribed token, queries ERC-1155 balanceOf and updates if there's a mismatch.
    /// Returns a list of (tokenId, localShares, onChainShares) for any mismatches found.
    /// </summary>
    public async Task<List<(string tokenId, decimal localShares, decimal onChainShares)>> SyncPositionsAsync(IEnumerable<string> tokenIds)
    {
        var mismatches = new List<(string, decimal, decimal)>();

        foreach (string tokenId in tokenIds)
        {
            try
            {
                decimal onChainShares = await _queryClient.GetTokenBalanceAsync(tokenId);
                decimal localShares = GetPositionShares(tokenId);

                if (Math.Abs(onChainShares - localShares) > 0.01m)
                {
                    lock (BrokerLock)
                    {
                        SetPositionShares(tokenId, onChainShares);

                        if (onChainShares == 0)
                            SetAverageEntryPrice(tokenId, 0); // Position gone — clear entry price
                        else if (localShares == 0 && onChainShares > 0)
                            SetAverageEntryPrice(tokenId, 0.50m); // Discovered untracked shares — neutral price
                    }

                    mismatches.Add((tokenId, localShares, onChainShares));
                }
            }
            catch (Exception ex)
            {
                Log.Warning("Failed to query on-chain balance for {Token}: {Error}",
                    tokenId[..Math.Min(8, tokenId.Length)] + "...", ex.Message);
            }
        }

        return mismatches;
    }

    /// <summary>
    /// Full state sync: cash + held positions. Logs results.
    /// If fullDiscovery is true, scans ALL tokens to find manual/untracked positions.
    /// </summary>
    public async Task RunFullSyncAsync(IEnumerable<string> tokenIds, Dictionary<string, string>? tokenNames = null, bool fullDiscovery = false)
    {
        Log.Information("[SYNC] Starting on-chain state reconciliation...");

        // 1. Sync cash
        decimal oldCash = CashBalance;
        decimal cashDrift = await SyncCashBalanceAsync();
        if (Math.Abs(cashDrift) > 0.01m)
            Log.Warning("[SYNC] Cash drift: {Drift:+0.00;-0.00} | Local: ${Old:0.00} -> On-chain: ${New:0.00}",
                cashDrift, oldCash, CashBalance);
        else
            Log.Information("[SYNC] Cash OK: ${Balance:0.00}", CashBalance);

        // 2. Sync positions 
        var heldTokens = fullDiscovery 
            ? tokenIds.ToList() 
            : tokenIds.Where(t => GetPositionShares(t) > 0 || GetNoPositionShares(t) > 0).ToList();

        if (heldTokens.Count > 0)
            Log.Information("[SYNC] Checking {Count} position(s) on-chain...", heldTokens.Count);

        var mismatches = await SyncPositionsAsync(heldTokens);

        // ==========================================
        // THE FIX: Explicitly log the open positions!
        // ==========================================
        var activePositions = heldTokens.Where(t => GetPositionShares(t) > 0 || GetNoPositionShares(t) > 0).ToList();
        
        if (activePositions.Count > 0)
        {
            Log.Information("[SYNC] --- CURRENT OPEN POSITIONS ---");
            foreach (var tokenId in activePositions)
            {
                string name = tokenNames?.GetValueOrDefault(tokenId) ?? tokenId[..Math.Min(8, tokenId.Length)] + "...";
                decimal yesShares = GetPositionShares(tokenId);
                decimal noShares = GetNoPositionShares(tokenId);
                
                if (yesShares > 0)
                    Log.Information("       -> {Shares:0.00} YES shares | {Name}", yesShares, name);
                if (noShares > 0)
                    Log.Information("       -> {Shares:0.00} NO shares  | {Name}", noShares, name);
            }
            Log.Information("[SYNC] --------------------------------");
        }

        if (mismatches.Count > 0)
        {
            foreach (var (tokenId, local, onChain) in mismatches)
            {
                string name = tokenNames?.GetValueOrDefault(tokenId) ?? tokenId[..Math.Min(8, tokenId.Length)] + "...";
                decimal drift = onChain - local;
                Log.Warning("[SYNC] Position drift: {Name} | Local: {Local:0.00} -> On-chain: {OnChain:0.00} | Drift: {Drift:+0.00;-0.00} shares",
                    name, local, onChain, drift);
            }
        }
        else
        {
            Log.Information("[SYNC] All positions OK ({Count} checked).", heldTokens.Count);
        }

        Log.Information("[SYNC] Reconciliation complete.");
    }

    // --- PERSISTENT MEMORY ---
    public class PositionState
    {
        public decimal YesShares { get; set; }
        public decimal YesEntryPrice { get; set; }
        public decimal NoShares { get; set; }
        public decimal NoEntryPrice { get; set; }
    }

    public void SaveState(IEnumerable<string> tokenIds, string filepath = "bot_state.json")
    {
        var state = new Dictionary<string, PositionState>();
        foreach (var assetId in tokenIds)
        {
            decimal yesShares = GetPositionShares(assetId);
            decimal noShares = GetNoPositionShares(assetId);

            if (yesShares > 0 || noShares > 0)
            {
                state[assetId] = new PositionState
                {
                    YesShares = yesShares,
                    YesEntryPrice = GetAverageEntryPrice(assetId),
                    NoShares = noShares,
                    NoEntryPrice = GetAverageNoEntryPrice(assetId)
                };
            }
        }
        
        string json = JsonSerializer.Serialize(state, new JsonSerializerOptions { WriteIndented = true });
        File.WriteAllText(filepath, json);
    }

    public void LoadState(string filepath = "bot_state.json")
    {
        if (!File.Exists(filepath)) return;

        try
        {
            string json = File.ReadAllText(filepath);
            var state = JsonSerializer.Deserialize<Dictionary<string, PositionState>>(json);

            if (state != null)
            {
                lock (BrokerLock)
                {
                    foreach (var kvp in state)
                    {
                        if (kvp.Value.YesShares > 0)
                        {
                            SetPositionShares(kvp.Key, kvp.Value.YesShares);
                            SetAverageEntryPrice(kvp.Key, kvp.Value.YesEntryPrice);
                        }
                        if (kvp.Value.NoShares > 0)
                        {
                            SetNoPositionShares(kvp.Key, kvp.Value.NoShares);
                            SetAverageNoEntryPrice(kvp.Key, kvp.Value.NoEntryPrice);
                        }
                    }
                }
                Log.Information("[STATE] Successfully loaded {Count} positions from local memory.", state.Count);
            }
        }
        catch (Exception ex)
        {
            Log.Error("[STATE] Failed to load bot_state.json: {Error}", ex.Message);
        }
    }
}

