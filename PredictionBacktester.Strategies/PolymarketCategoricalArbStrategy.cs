using System;
using System.Collections.Concurrent;
using System.Collections.Generic;
using PredictionBacktester.Engine;

namespace PredictionBacktester.Strategies
{
    public class PolymarketCategoricalArbStrategy : ILiveStrategy
    {
        public string StrategyName { get; }

        private readonly Dictionary<string, string> _tokenToEventMap = new();
        private readonly Dictionary<string, List<string>> _eventTokens = new();
        private readonly ConcurrentDictionary<string, LocalOrderBook> _books = new();

        // True = Paper mode (lock after first buy per event)
        // False = Production mode (re-enter whenever profitable)
        public bool LockEventAfterBuy { get; set; } = true;
        private readonly ConcurrentDictionary<string, bool> _lockedEvents = new();

        private readonly decimal _maxInvestmentPerTrade;
        private readonly decimal _slippageCents;

        // Fee parameters — defaults to Politics/Finance/Tech (feeRate=0.04, exponent=1)
        private readonly double _feeRate;
        private readonly double _feeExponent;

        public PolymarketCategoricalArbStrategy(
            Dictionary<string, List<string>> configuredEvents,
            string name = "Categorical_Merge_Arb",
            decimal maxInvestmentPerTrade = 50.00m,
            decimal slippageCents = 0.02m,
            double feeRate = 0.04,
            double feeExponent = 1.0)
        {
            StrategyName = name;
            _maxInvestmentPerTrade = maxInvestmentPerTrade;
            _slippageCents = slippageCents;
            _feeRate = feeRate;
            _feeExponent = feeExponent;

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

        /// <summary>
        /// Polymarket taker fee per share in USDC.
        /// Formula: fee = price * feeRate * (price * (1 - price))^exponent
        /// </summary>
        private decimal CalculateFeePerShare(decimal price)
        {
            double p = (double)price;
            double fee = p * _feeRate * Math.Pow(p * (1.0 - p), _feeExponent);
            return Math.Round((decimal)fee, 4);
        }

        private void EvaluateArbitrage(string eventId, GlobalSimulatedBroker broker)
        {
            if (LockEventAfterBuy && _lockedEvents.ContainsKey(eventId))
                return;

            var yesTokenIds = _eventTokens[eventId];

            // Walk each leg's book to get accurate VWAP and available depth
            decimal totalCostPerSet = 0m;
            decimal totalFeePerSet = 0m;
            decimal bottleneckShares = decimal.MaxValue;

            foreach (var token in yesTokenIds)
            {
                if (!_books.TryGetValue(token, out var book)) return;

                decimal bestAsk = book.GetBestAskPrice();
                if (bestAsk >= 1.00m || bestAsk <= 0.01m) return;

                // Walk the full ask side to find real available depth at reasonable prices
                decimal maxPrice = Math.Min(bestAsk + _slippageCents, 0.99m);
                var walkResult = book.WalkAsks(maxPrice, _maxInvestmentPerTrade, 1.0m);

                if (walkResult.TotalShares <= 0) return;

                // Use VWAP (not bestAsk) for accurate cost estimation
                totalCostPerSet += walkResult.Vwap;
                totalFeePerSet += CalculateFeePerShare(walkResult.Vwap);
                bottleneckShares = Math.Min(bottleneckShares, walkResult.TotalShares);
            }

            decimal totalNetCostPerSet = totalCostPerSet + totalFeePerSet;

            // Only execute if total cost per complete set (including fees) leaves profit
            // 0.005 buffer for rounding/dust
            if (totalNetCostPerSet >= 0.995m)
                return;

            decimal profitPerSet = 1.00m - totalNetCostPerSet;

            // Size: how many complete sets can we afford?
            decimal maxAffordableSets = _maxInvestmentPerTrade / totalNetCostPerSet;
            decimal setsToBuy = Math.Min(bottleneckShares, maxAffordableSets);
            setsToBuy = Math.Floor(setsToBuy * 100m) / 100m;

            if (setsToBuy <= 0.01m)
                return;

            // Cash check: ensure we can afford ALL legs
            decimal totalDollarsNeeded = setsToBuy * totalCostPerSet; // gross cost only — fees deducted by broker
            if (totalDollarsNeeded > broker.CashBalance)
            {
                setsToBuy = Math.Floor(broker.CashBalance / totalCostPerSet * 100m) / 100m;
                if (setsToBuy <= 0.01m) return;
            }

            Console.WriteLine($"\n[ARB DETECTED] Event: {eventId}");
            Console.WriteLine($"-> Cost/set: ${totalCostPerSet:0.0000} + fees ${totalFeePerSet:0.0000} = ${totalNetCostPerSet:0.0000} | Profit/set: ${profitPerSet:0.0000} | Sets: {setsToBuy}");

            if (LockEventAfterBuy)
                _lockedEvents.TryAdd(eventId, true);

            ExecuteMultiLegArbitrage(eventId, yesTokenIds, setsToBuy, broker);
        }

        private void ExecuteMultiLegArbitrage(string eventId, List<string> yesTokenIds, decimal setsToBuy, GlobalSimulatedBroker broker)
        {
            foreach (var token in yesTokenIds)
            {
                if (!_books.TryGetValue(token, out var book)) continue;

                decimal bestAsk = book.GetBestAskPrice();
                // Target price with slippage so the order doesn't get rejected on tiny price moves
                decimal targetPrice = Math.Min(bestAsk + _slippageCents, 0.99m);
                // Gross dollar amount for this leg — broker handles fees internally
                decimal dollarsForLeg = setsToBuy * bestAsk;

                broker.SubmitBuyOrder(token, targetPrice, dollarsForLeg, book);
            }

            Console.WriteLine($"[ARB FIRED] {yesTokenIds.Count}-leg buy dispatched for {eventId}");
        }
    }
}
