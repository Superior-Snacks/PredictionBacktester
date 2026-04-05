using System;
using System.Collections.Concurrent;
using System.Collections.Generic;
using System.Diagnostics;
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

        // Execution safety parameters
        private readonly long _requiredSustainMs;
        private readonly decimal _minProfitPerSet;
        private readonly decimal _depthFloorShares;

        // Sustain timer state: tracks when each event's arb was first detected
        private readonly ConcurrentDictionary<string, long> _arbFirstSeenMs = new();

        // Early exit state: tracks cost basis per event for sell-back evaluation
        private readonly ConcurrentDictionary<string, decimal> _eventCostBasis = new();
        private readonly ConcurrentDictionary<string, decimal> _eventSetsBought = new();

        public PolymarketCategoricalArbStrategy(
            Dictionary<string, List<string>> configuredEvents,
            string name = "Categorical_Merge_Arb",
            decimal maxInvestmentPerTrade = 50.00m,
            decimal slippageCents = 0.02m,
            double feeRate = 0.04,
            double feeExponent = 1.0,
            long requiredSustainMs = 500,
            decimal minProfitPerSet = 0.02m,
            decimal depthFloorShares = 5m)
        {
            StrategyName = name;
            _maxInvestmentPerTrade = maxInvestmentPerTrade;
            _slippageCents = slippageCents;
            _feeRate = feeRate;
            _feeExponent = feeExponent;
            _requiredSustainMs = requiredSustainMs;
            _minProfitPerSet = minProfitPerSet;
            _depthFloorShares = depthFloorShares;

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

            // If we hold a position on this event, check for early exit
            if (_lockedEvents.ContainsKey(eventId))
            {
                EvaluateSellBack(eventId, broker);
                return;
            }

            EvaluateArbitrage(eventId, broker);
        }

        /// <summary>
        /// Clears stale book state after a WebSocket reconnect.
        /// Does NOT clear _lockedEvents — those represent real executed trades.
        /// </summary>
        public void OnReconnect()
        {
            _books.Clear();
            _arbFirstSeenMs.Clear();
        }

        /// <summary>
        /// Registers a new event discovered at runtime. Thread-safe.
        /// </summary>
        public void RegisterEvent(string eventId, List<string> tokenIds)
        {
            lock (_eventTokens)
            {
                if (_eventTokens.ContainsKey(eventId)) return;
                _eventTokens[eventId] = tokenIds;
                foreach (var token in tokenIds)
                    _tokenToEventMap[token] = eventId;
            }
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
            long nowMs = Stopwatch.GetTimestamp() / (Stopwatch.Frequency / 1000);

            // Walk each leg's book to get accurate VWAP and available depth
            decimal totalCostPerSet = 0m;
            decimal totalFeePerSet = 0m;
            decimal bottleneckShares = decimal.MaxValue;
            bool allLegsHaveBooks = true;

            foreach (var token in yesTokenIds)
            {
                if (!_books.TryGetValue(token, out var book)) { allLegsHaveBooks = false; break; }

                decimal bestAsk = book.GetBestAskPrice();
                if (bestAsk >= 1.00m) { allLegsHaveBooks = false; break; }

                // Walk the full ask side to find real available depth at reasonable prices
                decimal maxPrice = Math.Min(bestAsk + _slippageCents, 0.99m);
                var walkResult = book.WalkAsks(maxPrice, _maxInvestmentPerTrade, 1.0m);

                if (walkResult.TotalShares <= 0) { allLegsHaveBooks = false; break; }

                // Use VWAP (not bestAsk) for accurate cost estimation
                totalCostPerSet += walkResult.Vwap;
                totalFeePerSet += CalculateFeePerShare(walkResult.Vwap);
                bottleneckShares = Math.Min(bottleneckShares, walkResult.TotalShares);
            }

            if (!allLegsHaveBooks)
            {
                // Arb condition no longer holds — reset sustain timer
                _arbFirstSeenMs.TryRemove(eventId, out _);
                return;
            }

            decimal totalNetCostPerSet = totalCostPerSet + totalFeePerSet;

            // Only execute if total cost per complete set (including fees) leaves profit
            if (totalNetCostPerSet >= 0.995m)
            {
                _arbFirstSeenMs.TryRemove(eventId, out _);
                return;
            }

            decimal profitPerSet = 1.00m - totalNetCostPerSet;

            // SAFETY 1: Minimum profit threshold — skip thin arbs
            if (profitPerSet < _minProfitPerSet)
            {
                _arbFirstSeenMs.TryRemove(eventId, out _);
                return;
            }

            // SAFETY 2: Depth floor — every leg must have enough shares
            if (bottleneckShares < _depthFloorShares)
            {
                _arbFirstSeenMs.TryRemove(eventId, out _);
                return;
            }

            // SAFETY 3: Sustain timer — arb must persist for N ms before entry
            if (!_arbFirstSeenMs.TryGetValue(eventId, out long firstSeenMs))
            {
                _arbFirstSeenMs[eventId] = nowMs;
                return; // First sighting — wait for next update
            }

            long sustainedMs = nowMs - firstSeenMs;
            if (sustainedMs < _requiredSustainMs)
                return; // Still waiting

            // Arb has sustained — clear timer so it doesn't re-trigger
            _arbFirstSeenMs.TryRemove(eventId, out _);

            // Size: how many complete sets can we afford?
            decimal maxAffordableSets = _maxInvestmentPerTrade / totalNetCostPerSet;
            decimal setsToBuy = Math.Min(bottleneckShares, maxAffordableSets);
            setsToBuy = Math.Floor(setsToBuy * 100m) / 100m;

            if (setsToBuy <= 0.01m)
                return;

            // Cash check: ensure we can afford ALL legs
            decimal totalDollarsNeeded = setsToBuy * totalCostPerSet;
            if (totalDollarsNeeded > broker.CashBalance)
            {
                setsToBuy = Math.Floor(broker.CashBalance / totalCostPerSet * 100m) / 100m;
                if (setsToBuy <= 0.01m) return;
            }

            Console.WriteLine($"\n[ARB DETECTED] Event: {eventId} (sustained {sustainedMs}ms)");
            Console.WriteLine($"-> Cost/set: ${totalCostPerSet:0.0000} + fees ${totalFeePerSet:0.0000} = ${totalNetCostPerSet:0.0000} | Profit/set: ${profitPerSet:0.0000} | Sets: {setsToBuy} | Depth: {bottleneckShares:0.00}");

            if (LockEventAfterBuy)
                _lockedEvents.TryAdd(eventId, true);

            ExecuteMultiLegArbitrage(eventId, yesTokenIds, setsToBuy, broker);
        }

        private void ExecuteMultiLegArbitrage(string eventId, List<string> yesTokenIds, decimal setsToBuy, GlobalSimulatedBroker broker)
        {
            decimal totalSpent = 0m;

            foreach (var token in yesTokenIds)
            {
                if (!_books.TryGetValue(token, out var book)) continue;

                decimal bestAsk = book.GetBestAskPrice();
                decimal targetPrice = Math.Min(bestAsk + _slippageCents, 0.99m);
                decimal dollarsForLeg = setsToBuy * bestAsk;

                totalSpent += dollarsForLeg + (setsToBuy * CalculateFeePerShare(bestAsk));
                broker.SubmitBuyOrder(token, targetPrice, dollarsForLeg, book);
            }

            // Record cost basis for sell-back evaluation
            _eventCostBasis.AddOrUpdate(eventId, totalSpent, (_, old) => old + totalSpent);
            _eventSetsBought.AddOrUpdate(eventId, setsToBuy, (_, old) => old + setsToBuy);

            Console.WriteLine($"[ARB FIRED] {yesTokenIds.Count}-leg buy dispatched for {eventId} (cost: ${totalSpent:0.00})");
        }

        private void EvaluateSellBack(string eventId, GlobalSimulatedBroker broker)
        {
            if (!_eventCostBasis.TryGetValue(eventId, out decimal costBasis)) return;
            if (!_eventSetsBought.TryGetValue(eventId, out decimal setsHeld)) return;
            if (setsHeld <= 0) return;

            var yesTokenIds = _eventTokens[eventId];

            // Check we still hold positions on all legs
            decimal minShares = decimal.MaxValue;
            foreach (var token in yesTokenIds)
            {
                decimal shares = broker.GetPositionShares(token);
                if (shares <= 0) return; // Missing a leg — can't sell complete sets
                minShares = Math.Min(minShares, shares);
            }

            // Walk bids on all legs to see what we'd get for selling minShares complete sets
            decimal totalProceeds = 0m;
            decimal totalSellFees = 0m;

            foreach (var token in yesTokenIds)
            {
                if (!_books.TryGetValue(token, out var book)) return;

                decimal bestBid = book.GetBestBidPrice();
                if (bestBid <= 0.01m) return; // No real bid

                var walkResult = book.WalkBids(bestBid - _slippageCents, minShares, 1.0m);
                if (walkResult.TotalShares <= 0) return;

                totalProceeds += walkResult.Vwap * Math.Min(walkResult.TotalShares, minShares);
                totalSellFees += CalculateFeePerShare(walkResult.Vwap) * Math.Min(walkResult.TotalShares, minShares);
            }

            decimal netProceeds = totalProceeds - totalSellFees;

            // Only sell back if profit per set meets the minimum threshold
            decimal profit = netProceeds - costBasis;
            if (setsHeld > 0 && profit / setsHeld < _minProfitPerSet)
                return;

            Console.WriteLine($"\n[ARB SELL-BACK] Event: {eventId}");
            Console.WriteLine($"-> Cost basis: ${costBasis:0.00} | Net proceeds: ${netProceeds:0.00} | Profit: ${profit:0.00}");

            // Sell all legs
            foreach (var token in yesTokenIds)
            {
                if (!_books.TryGetValue(token, out var book)) continue;
                decimal bestBid = book.GetBestBidPrice();
                broker.SubmitSellAllOrder(token, bestBid, book);
            }

            // Clear state — event is no longer locked, can re-enter
            _lockedEvents.TryRemove(eventId, out _);
            _eventCostBasis.TryRemove(eventId, out _);
            _eventSetsBought.TryRemove(eventId, out _);

            Console.WriteLine($"[ARB SELL-BACK COMPLETE] {yesTokenIds.Count} legs sold for {eventId}");
        }
    }
}
