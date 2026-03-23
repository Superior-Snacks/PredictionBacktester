using System;
using System.Collections.Generic;
using PredictionBacktester.Engine;

namespace PredictionLiveTrader;

/// <summary>
/// PaperBroker wrapper that simulates latency using replay timestamps instead of Task.Delay.
/// Orders are queued and executed when the replay clock advances past the latency window.
/// </summary>
public class ReplayBroker : PaperBroker
{
    /// <summary>Current replay timestamp in milliseconds (set by ReplayRunner each tick).</summary>
    public long ReplayTimeMs { get; set; }

    /// <summary>Simulated latency in milliseconds.</summary>
    public int ReplayLatencyMs { get; set; }

    private readonly struct DeferredOrder
    {
        public readonly long ExecuteAtMs;
        public readonly string AssetId;
        public readonly decimal TargetPrice;
        public readonly decimal Dollars; // > 0 for buy, 0 for sell-all
        public readonly bool IsSell;

        public DeferredOrder(long executeAtMs, string assetId, decimal targetPrice, decimal dollars, bool isSell)
        {
            ExecuteAtMs = executeAtMs;
            AssetId = assetId;
            TargetPrice = targetPrice;
            Dollars = dollars;
            IsSell = isSell;
        }
    }

    private readonly List<DeferredOrder> _deferredOrders = new();
    private readonly HashSet<string> _deferredAssets = new();

    public ReplayBroker(string strategyName, decimal initialCapital, Dictionary<string, string> tokenNames,
        Dictionary<string, decimal> tokenMinSizes, decimal maxBetSize, int replayLatencyMs)
        : base(strategyName, initialCapital, tokenNames, tokenMinSizes, maxBetSize)
    {
        ReplayLatencyMs = replayLatencyMs;
        LatencyMs = 0; // Disable the base class Task.Delay path — we handle latency ourselves
    }

    public override void SubmitBuyOrder(string assetId, decimal targetPrice, decimal dollarsToInvest, LocalOrderBook book)
    {
        if (ReplayLatencyMs <= 0)
        {
            // No latency — execute immediately through PaperBroker (which applies production constraints)
            base.SubmitBuyOrder(assetId, targetPrice, dollarsToInvest, book);
            return;
        }

        // Don't queue multiple orders for the same asset
        if (!_deferredAssets.Add(assetId)) return;

        _deferredOrders.Add(new DeferredOrder(
            ReplayTimeMs + ReplayLatencyMs,
            assetId, targetPrice, dollarsToInvest, isSell: false));
    }

    public override void SubmitSellAllOrder(string assetId, decimal targetPrice, LocalOrderBook book)
    {
        if (ReplayLatencyMs <= 0)
        {
            base.SubmitSellAllOrder(assetId, targetPrice, book);
            return;
        }

        if (!_deferredAssets.Add(assetId)) return;

        _deferredOrders.Add(new DeferredOrder(
            ReplayTimeMs + ReplayLatencyMs,
            assetId, targetPrice, 0m, isSell: true));
    }

    /// <summary>
    /// Called by ReplayRunner before processing each tick.
    /// Executes any deferred orders whose latency window has expired.
    /// </summary>
    public void DrainDeferredOrders(Dictionary<string, LocalOrderBook> orderBooks)
    {
        if (_deferredOrders.Count == 0) return;

        for (int i = _deferredOrders.Count - 1; i >= 0; i--)
        {
            var order = _deferredOrders[i];
            if (ReplayTimeMs < order.ExecuteAtMs) continue;

            // Time's up — execute against the CURRENT book state (price may have moved)
            _deferredOrders.RemoveAt(i);
            _deferredAssets.Remove(order.AssetId);

            if (!orderBooks.TryGetValue(order.AssetId, out var book)) continue;

            if (order.IsSell)
            {
                decimal currentBid = book.GetBestBidPrice();
                if (currentBid <= 0.01m || currentBid >= 0.99m) continue;

                if (currentBid >= order.TargetPrice)
                {
                    // Price still good — execute via base (PaperBroker constraints apply)
                    base.SubmitSellAllOrder(order.AssetId, currentBid, book);
                }
                else
                {
                    // Price moved against us — latency reject
                    Interlocked.Increment(ref _rejectedOrders);
                }
            }
            else
            {
                decimal currentAsk = book.GetBestAskPrice();
                if (currentAsk >= 0.99m || currentAsk <= 0.01m) continue;

                if (currentAsk <= order.TargetPrice)
                {
                    base.SubmitBuyOrder(order.AssetId, currentAsk, order.Dollars, book);
                }
                else
                {
                    Interlocked.Increment(ref _rejectedOrders);
                }
            }
        }
    }
}
