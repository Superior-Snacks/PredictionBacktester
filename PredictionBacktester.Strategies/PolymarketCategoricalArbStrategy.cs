using System;
using System.Collections.Concurrent;
using System.Collections.Generic;
using PredictionBacktester.Engine;

namespace PredictionBacktester.Strategies
{
    public class PolymarketCategoricalArbStrategy : ILiveStrategy
    {
        public string StrategyName => "Categorical_Merge_Arb";

        private readonly Dictionary<string, string> _tokenToEventMap = new();
        private readonly Dictionary<string, List<string>> _eventTokens = new();
        private readonly ConcurrentDictionary<string, LocalOrderBook> _books = new();

        // --- NEW: Toggleable Lock ---
        // True = Paper Bot mode (don't spam the same fake arb)
        // False = Production mode (arb it as many times as profitability allows)
        public bool LockEventAfterBuy { get; set; } = true;
        private readonly ConcurrentDictionary<string, bool> _lockedEvents = new();

        private readonly decimal _maxInvestmentPerTrade = 50.00m;

        public PolymarketCategoricalArbStrategy(Dictionary<string, List<string>> configuredEvents)
        {
            foreach (var evt in configuredEvents)
            {
                string eventId = evt.Key;
                List<string> yesTokenIds = evt.Value;

                _eventTokens[eventId] = yesTokenIds;

                foreach (var token in yesTokenIds)
                {
                    _tokenToEventMap[token] = eventId;
                }
            }
        }

        public void OnBookUpdate(LocalOrderBook book, GlobalSimulatedBroker broker)
        {
            string asset = book.AssetId;

            if (!_tokenToEventMap.TryGetValue(asset, out var eventId))
                return;

            _books[asset] = book;

            EvaluateArbitrage(eventId, broker);
        }

        // --- NEW: Dynamic Fee Polynomial Formula ---
        // Formula: fee = p * feeRate * (p * (1 - p))^exponent
        private decimal CalculateFeePerShare(decimal price)
        {
            double p = (double)price;
            
            // Using standard Political/Financial/Tech fee parameters as a safe baseline:
            // Rate: 0.04 (4%), Exponent: 1
            double feeRate = 0.04; 
            double exponent = 1.0;
            
            double fee = p * feeRate * Math.Pow(p * (1.0 - p), exponent);
            
            // Polymarket rounds fees to 4 decimals
            return Math.Round((decimal)fee, 4);
        }

        private void EvaluateArbitrage(string eventId, GlobalSimulatedBroker broker)
        {
            // Lock Check
            if (LockEventAfterBuy && _lockedEvents.ContainsKey(eventId))
                return;

            var yesTokenIds = _eventTokens[eventId];
            
            decimal totalGrossCost = 0m;
            decimal totalFeeCost = 0m;
            decimal bottleneckShares = decimal.MaxValue;

            foreach (var token in yesTokenIds)
            {
                if (!_books.TryGetValue(token, out var book)) return;

                decimal bestAsk = book.GetBestAskPrice();
                decimal availableSize = broker.GetAvailableAskSize(book, token);

                if (bestAsk >= 1.00m || availableSize <= 0) return;

                totalGrossCost += bestAsk;
                totalFeeCost += CalculateFeePerShare(bestAsk);
                bottleneckShares = Math.Min(bottleneckShares, availableSize);
            }

            decimal totalCostWithFees = totalGrossCost + totalFeeCost;

            // We execute if the total cost including ALL polynomial fees leaves a profit
            // Added a 0.5% (0.005) buffer to account for rounding/dust
            if (totalCostWithFees >= 0.995m)
                return;

            decimal profitPerShare = 1.00m - totalCostWithFees;

            // Size the trade based on the Bottleneck and Capital limits
            decimal maxAffordableSets = _maxInvestmentPerTrade / totalCostWithFees;
            decimal safeSharesToBuy = Math.Min(bottleneckShares, maxAffordableSets);
            safeSharesToBuy = Math.Floor(safeSharesToBuy * 100) / 100;

            if (safeSharesToBuy <= 0.01m)
                return;

            // Cash check
            decimal totalDollarsNeeded = safeSharesToBuy * totalCostWithFees;
            if (totalDollarsNeeded > broker.CashBalance)
            {
                safeSharesToBuy = Math.Floor(broker.CashBalance / totalCostWithFees * 100) / 100;
                if (safeSharesToBuy <= 0.01m) return;
            }

            Console.WriteLine($"\n[ARB DETECTED] Event: {eventId}");
            Console.WriteLine($"-> Gross Spread: ${totalGrossCost:0.0000} | Fees: ${totalFeeCost:0.0000} | Total Cost: ${totalCostWithFees:0.0000}");
            Console.WriteLine($"-> Net Profit per set: ${profitPerShare:0.0000} | Executing {safeSharesToBuy} shares per leg.");

            // Lock the event if toggle is enabled
            if (LockEventAfterBuy)
            {
                _lockedEvents.TryAdd(eventId, true);
            }

            // Fire the orders!
            ExecuteMultiLegArbitrage(yesTokenIds, safeSharesToBuy, broker);
        }

        private void ExecuteMultiLegArbitrage(List<string> yesTokenIds, decimal safeSharesToBuy, GlobalSimulatedBroker broker)
        {
            foreach (var token in yesTokenIds)
            {
                if (_books.TryGetValue(token, out var book))
                {
                    decimal bestAsk = book.GetBestAskPrice();
                    
                    // The investment amount per leg includes the ask price + fees
                    decimal requiredDollars = (bestAsk + CalculateFeePerShare(bestAsk)) * safeSharesToBuy;

                    broker.SubmitBuyOrder(token, bestAsk, requiredDollars, book);
                }
            }

            Console.WriteLine($"[ARB FIRED] Multi-leg YES tokens successfully dispatched.");
        }
    }
}