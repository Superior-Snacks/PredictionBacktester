using System;
using System.Linq;
using PredictionBacktester.Engine;

namespace PredictionBacktester.Strategies;

public class OrderBookImbalanceStrategy : ILiveStrategy
{
    public string StrategyName { get; }

    private readonly decimal _imbalanceRatioThreshold;
    private readonly int _depthLevelsToScan;
    private readonly decimal _takeProfitMargin;
    private readonly decimal _stopLossMargin;
    private readonly decimal _riskPercentage;

    public OrderBookImbalanceStrategy(
        string strategyName = "Imbalance_Bot",
        decimal imbalanceRatioThreshold = 5.0m, // Bid volume must be 5x Ask volume
        int depthLevelsToScan = 3,              // Look at the top 3 price levels
        decimal takeProfitMargin = 0.02m,       // Dump for a quick 2-cent scalp
        decimal stopLossMargin = 0.02m,         // Cut losses quickly if the wall breaks
        decimal riskPercentage = 0.05m)
    {
        StrategyName = strategyName;
        _imbalanceRatioThreshold = imbalanceRatioThreshold;
        _depthLevelsToScan = depthLevelsToScan;
        _takeProfitMargin = takeProfitMargin;
        _stopLossMargin = stopLossMargin;
        _riskPercentage = riskPercentage;
    }

    public void OnBookUpdate(LocalOrderBook book, GlobalSimulatedBroker broker)
    {
        string assetId = book.AssetId;
        decimal bestAsk = book.GetBestAskPrice();
        decimal bestBid = book.GetBestBidPrice();
        decimal availableAskSize = book.GetBestAskSize();
        decimal availableBidSize = book.GetBestBidSize();

        // Always keep the broker updated on the latest portfolio valuations
        broker.UpdateLastKnownPrice(assetId, bestAsk);

        if (bestAsk >= 1.00m || bestAsk <= 0.00m || availableAskSize <= 0 || availableBidSize <= 0) return;

        // THE FIX: The Maximum Spread Filter
        // If the spread is wider than 5 cents, the market makers have pulled liquidity. DO NOT TRADE!
        if (bestAsk - bestBid > 0.05m) return;

        decimal positionShares = broker.GetPositionShares(assetId);

        // --- 1. MANAGE OPEN POSITIONS (The Scalp) ---
        if (positionShares > 0)
        {
            decimal avgEntry = broker.GetAverageEntryPrice(assetId);

            // Did we make our 2 cents? Or did the buy wall collapse? Dump it!
            if (bestBid >= avgEntry + _takeProfitMargin || bestBid <= avgEntry - _stopLossMargin)
            {
                broker.SellAll(assetId, bestBid, availableBidSize);
            }
            return;
        }

        // --- 2. CALCULATE THE WALLS (The Setup) ---
        // Sum up the total volume of the top N highest buyers
        decimal bidVolume = book.Bids.Keys
                                .OrderByDescending(p => p)
                                .Take(_depthLevelsToScan)
                                .Sum(p => book.Bids[p]);

        // Sum up the total volume of the top N lowest sellers
        decimal askVolume = book.Asks.Keys
                                .OrderBy(p => p)
                                .Take(_depthLevelsToScan)
                                .Sum(p => book.Asks[p]);

        if (askVolume <= 0 || bidVolume <= 0) return;

        // How many times heavier are the buyers than the sellers?
        decimal imbalanceRatio = bidVolume / askVolume;

        // --- 3. EXECUTE THE SNIPE (The Trigger) ---
        if (imbalanceRatio >= _imbalanceRatioThreshold)
        {
            decimal currentEquity = broker.GetTotalPortfolioValue();
            decimal dollarsToInvest = Math.Min(currentEquity * _riskPercentage, broker.CashBalance);

            if (dollarsToInvest >= 1.00m)
            {
                decimal maxAffordableShares = dollarsToInvest / bestAsk;
                // Only buy up to the available size on the top Ask shelf
                decimal sharesToBuy = Math.Min(maxAffordableShares, availableAskSize);
                decimal actualDollarsSpent = sharesToBuy * bestAsk;

                if (actualDollarsSpent >= 1.00m)
                {
                    broker.Buy(assetId, bestAsk, actualDollarsSpent, sharesToBuy);
                }
            }
        }
    }
}