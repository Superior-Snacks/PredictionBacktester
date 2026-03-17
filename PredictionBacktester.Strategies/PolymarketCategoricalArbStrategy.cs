using System;
using System.Collections.Concurrent;
using System.Collections.Generic;
using PredictionBacktester.Engine;

namespace PredictionBacktester.Strategies
{
    public class PolymarketCategoricalArbStrategy : ILiveStrategy
    {
        // 1. Fixing the Interface Mismatch
        public string StrategyName => "Categorical_Merge_Arb";

        private readonly Dictionary<string, string> _tokenToMarketMap = new();
        private readonly Dictionary<string, List<string>> _marketTokens = new();

        // 2. State Machine: Cache the real-time OrderBooks so we can cross-reference them
        private readonly ConcurrentDictionary<string, LocalOrderBook> _books = new();

        private readonly decimal _arbThreshold = 0.98m; // If sum of Asks < $0.98, we execute!
        private readonly decimal _maxInvestmentPerTrade = 50.00m; // Safety cap on dollars used

        public PolymarketCategoricalArbStrategy(Dictionary<string, List<string>> configuredMarkets)
        {
            foreach (var market in configuredMarkets)
            {
                string marketId = market.Key;
                List<string> tokenIds = market.Value;

                _marketTokens[marketId] = tokenIds;

                foreach (var token in tokenIds)
                {
                    _tokenToMarketMap[token] = marketId;
                }
            }
        }

        // 3. Implementing the exact Interface signature from your codebase
        public void OnBookUpdate(LocalOrderBook book, GlobalSimulatedBroker broker)
        {
            string asset = book.AssetId;

            // Is this token part of an Arb market we are tracking?
            if (!_tokenToMarketMap.TryGetValue(asset, out var marketId))
                return;

            // Cache the live order book reference
            _books[asset] = book;

            // Evaluate the Arb for the entire market group
            EvaluateArbitrage(marketId, broker);
        }

        private void EvaluateArbitrage(string marketId, GlobalSimulatedBroker broker)
        {
            var tokenIds = _marketTokens[marketId];
            
            decimal totalCost = 0m;
            decimal bottleneckShares = decimal.MaxValue;

            // Step 1: Sum up the best asks and find the weakest volume link
            foreach (var token in tokenIds)
            {
                if (!_books.TryGetValue(token, out var book))
                    return; // We haven't received data for all legs yet. Skip.

                decimal bestAsk = book.GetBestAskPrice();
                
                // Using the specific volume tracking from your GlobalSimulatedBroker
                decimal availableSize = broker.GetAvailableAskSize(book, token);

                if (bestAsk >= 1.00m || availableSize <= 0)
                    return; // Leg is empty or fully priced

                totalCost += bestAsk;
                bottleneckShares = Math.Min(bottleneckShares, availableSize);
            }

            // Step 2: Check if it's an actual arbitrage!
            if (totalCost >= _arbThreshold)
                return;

            // Step 3: Size the trade based on the Bottleneck and your Capital limits
            // We calculate how many full "Sets" of arb we can afford
            decimal maxAffordableSets = _maxInvestmentPerTrade / totalCost;
            
            // Limit our actual shares to whichever is smaller: the available volume, or our wallet
            decimal safeSharesToBuy = Math.Min(bottleneckShares, maxAffordableSets);
            safeSharesToBuy = Math.Floor(safeSharesToBuy * 100) / 100; // Round down to avoid decimal dust

            if (safeSharesToBuy <= 0.01m)
                return;

            // Cash check: ensure we can afford ALL legs, not just some
            decimal totalDollarsNeeded = safeSharesToBuy * totalCost;
            if (totalDollarsNeeded > broker.CashBalance)
            {
                safeSharesToBuy = Math.Floor(broker.CashBalance / totalCost * 100) / 100;
                if (safeSharesToBuy <= 0.01m) return;
            }

            Console.WriteLine($"\n[ARB DETECTED] Market: {marketId} | Spread: ${totalCost:0.00}");
            Console.WriteLine($"-> Bottleneck Volume: {bottleneckShares} shares. Executing {safeSharesToBuy} shares per leg.");

            // Step 4: Fire the orders!
            ExecuteMultiLegArbitrage(tokenIds, safeSharesToBuy, broker);
        }

        private void ExecuteMultiLegArbitrage(List<string> tokenIds, decimal safeSharesToBuy, GlobalSimulatedBroker broker)
        {
            foreach (var token in tokenIds)
            {
                if (_books.TryGetValue(token, out var book))
                {
                    decimal bestAsk = book.GetBestAskPrice();
                    decimal requiredDollars = bestAsk * safeSharesToBuy;

                    // SubmitBuyOrder handles liquidity consumption internally
                    broker.SubmitBuyOrder(token, bestAsk, requiredDollars, book);
                }
            }

            Console.WriteLine($"[ARB FIRED] Multi-leg FAK orders successfully dispatched.");
        }
    }
}