using System;
using System.Collections.Generic;
using System.Linq;
using PredictionBacktester.Core.Entities.Database;
using PredictionBacktester.Engine;
using PredictionLiveTrader;

namespace PredictionBacktester.Strategies;

public class LiveFlashCrashSniperStrategy
{
    private readonly decimal _crashThreshold;
    private readonly long _timeWindowSeconds;
    private readonly decimal _reboundProfitMargin;
    private readonly decimal _stopLossMargin;
    private readonly decimal _riskPercentage;

    // We now store the history of the "Best Ask" (cheapest seller) instead of trades
    private readonly Queue<(long Timestamp, decimal Price)> _recentAsks;

    public LiveFlashCrashSniperStrategy(
        decimal crashThreshold = 0.15m,
        long timeWindowSeconds = 60,
        decimal reboundProfitMargin = 0.05m,
        decimal stopLossMargin = 0.15m,
        decimal riskPercentage = 0.05m)
    {
        _crashThreshold = crashThreshold;
        _timeWindowSeconds = timeWindowSeconds;
        _reboundProfitMargin = reboundProfitMargin;
        _stopLossMargin = stopLossMargin;
        _riskPercentage = riskPercentage;

        _recentAsks = new Queue<(long, decimal)>();
    }

    // Notice the signature change: We pass the Order Book instead of a Trade Tick!
    public void OnBookUpdate(LocalOrderBook book, PaperBroker broker)
    {
        long now = DateTimeOffset.UtcNow.ToUnixTimeSeconds();

        // Look at the shelves!
        decimal bestAsk = book.GetBestAskPrice();  // Cheapest price we can BUY for
        decimal bestBid = book.GetBestBidPrice();  // Highest price we can SELL for
        decimal availableSize = book.GetBestAskSize(); // How many shares the seller has

        // If the book is empty on the seller side, do nothing
        if (bestAsk >= 1.00m || availableSize <= 0) return;

        // --- 1. MAINTAIN THE TIME WINDOW ---
        _recentAsks.Enqueue((now, bestAsk));
        while (_recentAsks.Count > 0 && (now - _recentAsks.Peek().Timestamp) > _timeWindowSeconds)
        {
            _recentAsks.Dequeue();
        }

        if (_recentAsks.Count < 2) return;

        // Equity is calculated based on what we can ACTUALLY sell our shares for (the Bid)
        decimal currentEquity = broker.GetTotalPortfolioValue(bestBid);
        decimal dollarsToInvest = Math.Min(currentEquity * _riskPercentage, broker.CashBalance);

        // --- 2. MANAGE OPEN POSITIONS (Take Profit & Stop Loss) ---
        if (broker.PositionShares > 0)
        {
            // We have to sell to the highest BUYER (Best Bid)
            bool isTakeProfit = bestBid >= broker.AverageEntryPrice + _reboundProfitMargin;
            bool isStopLoss = bestBid <= broker.AverageEntryPrice - _stopLossMargin;

            if (isTakeProfit || isStopLoss)
            {
                // We sell our shares at the actual Bid price
                broker.SellAll(bestBid, broker.PositionShares);
            }
            return;
        }

        // --- 3. HUNT FOR NEW CRASHES ON THE BOOK ---
        decimal maxAskInWindow = _recentAsks.Max(x => x.Price);

        // If the cheapest seller suddenly drops their price by 15 cents...
        if (maxAskInWindow - bestAsk >= _crashThreshold && dollarsToInvest >= 1.00m)
        {
            // REALITY CHECK: We can only buy what is actually sitting on the shelf!
            decimal maxAffordableShares = dollarsToInvest / bestAsk;
            decimal sharesToBuy = Math.Min(maxAffordableShares, availableSize);
            decimal actualDollarsSpent = sharesToBuy * bestAsk;

            if (actualDollarsSpent >= 1.00m) // Ensure minimum order size
            {
                broker.Buy(bestAsk, actualDollarsSpent, sharesToBuy);
                _recentAsks.Clear();
            }
        }
    }
}