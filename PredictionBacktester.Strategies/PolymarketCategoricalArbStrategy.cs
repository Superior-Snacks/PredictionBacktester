using System;
using System.Collections.Concurrent;
using System.Collections.Generic;
using System.Diagnostics;
using System.Linq;
using PredictionBacktester.Engine;

namespace PredictionBacktester.Strategies
{
    public class PolymarketCategoricalArbStrategy : ILiveStrategy
    {
        public string StrategyName { get; }

        private readonly Dictionary<string, string> _tokenToEventMap = new();
        private readonly Dictionary<string, List<string>> _eventTokens = new();
        private readonly ConcurrentDictionary<string, LocalOrderBook> _books = new();

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

        // Fast-path marker: events where we've dispatched buys (avoids querying all leg positions every tick)
        private readonly ConcurrentDictionary<string, bool> _eventsWithPositions = new();

        // Guards against new buys while sell-back is in progress
        private readonly ConcurrentDictionary<string, bool> _sellInProgress = new();

        // Running P&L tracking
        private decimal _totalRealizedPnL = 0m;
        private int _totalCompletedSellBacks = 0;
        private readonly object _pnlLock = new();

        public decimal TotalRealizedPnL => _totalRealizedPnL;
        public int CompletedSellBacks => _totalCompletedSellBacks;

        public PolymarketCategoricalArbStrategy(
            Dictionary<string, List<string>> configuredEvents,
            string name = "Categorical_Merge_Arb",
            decimal maxInvestmentPerTrade = 50.00m,
            decimal slippageCents = 0.02m,
            double feeRate = 0.04,
            double feeExponent = 1.0,
            long requiredSustainMs = 0,
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

            // If we hold positions on this event, evaluate sell-back
            if (_eventsWithPositions.ContainsKey(eventId))
                EvaluateSellBack(eventId, broker);

            // Evaluate re-entry (or first entry) unless a sell is in progress
            if (!_sellInProgress.ContainsKey(eventId))
                EvaluateArbitrage(eventId, broker);
        }

        /// <summary>
        /// Clears stale book state after a WebSocket reconnect.
        /// Does NOT clear _eventsWithPositions — those represent real executed trades.
        /// </summary>
        public void OnReconnect()
        {
            _books.Clear();
            _arbFirstSeenMs.Clear();
            _sellInProgress.Clear();
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

                // Subtract shares already held — don't double-buy depth we already consumed
                decimal alreadyHeld = broker.GetPositionShares(token);
                decimal netAvailable = walkResult.TotalShares - alreadyHeld;
                if (netAvailable <= 0) { allLegsHaveBooks = false; break; }

                // Use VWAP (not bestAsk) for accurate cost estimation
                totalCostPerSet += walkResult.Vwap;
                totalFeePerSet += CalculateFeePerShare(walkResult.Vwap);
                bottleneckShares = Math.Min(bottleneckShares, netAvailable);
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

            ExecuteMultiLegArbitrage(eventId, yesTokenIds, setsToBuy, broker);
        }

        private void ExecuteMultiLegArbitrage(string eventId, List<string> yesTokenIds, decimal setsToBuy, GlobalSimulatedBroker broker)
        {
            decimal totalSpent = 0m;

            // Sort by thinnest ask depth first — if a thin leg's FOK rejects, we know sooner
            var sortedTokens = yesTokenIds
                .OrderBy(t => _books.TryGetValue(t, out var b) ? b.GetBestAskSize() : decimal.MaxValue)
                .ToList();

            foreach (var token in sortedTokens)
            {
                if (!_books.TryGetValue(token, out var book)) continue;

                decimal bestAsk = book.GetBestAskPrice();
                decimal targetPrice = Math.Min(bestAsk + _slippageCents, 0.99m);
                decimal dollarsForLeg = setsToBuy * bestAsk;

                totalSpent += dollarsForLeg + (setsToBuy * CalculateFeePerShare(bestAsk));
                broker.SubmitBuyOrder(token, targetPrice, dollarsForLeg, book);
            }

            // Mark that we have positions on this event — cost basis derived from broker state
            _eventsWithPositions[eventId] = true;

            Console.WriteLine($"[ARB FIRED] {yesTokenIds.Count}-leg buy dispatched for {eventId} (cost: ${totalSpent:0.00})");
        }

        private void EvaluateSellBack(string eventId, GlobalSimulatedBroker broker)
        {
            var yesTokenIds = _eventTokens[eventId];

            // Derive position and cost basis from broker state (single source of truth)
            decimal minShares = decimal.MaxValue;
            decimal totalCostBasis = 0m;

            foreach (var token in yesTokenIds)
            {
                decimal shares = broker.GetPositionShares(token);
                if (shares <= 0) { minShares = 0; break; }
                minShares = Math.Min(minShares, shares);
                totalCostBasis += shares * broker.GetAverageEntryPrice(token);
            }

            if (minShares <= 0)
            {
                // Not holding complete sets — check if we hold anything at all
                bool anyShares = yesTokenIds.Any(t => broker.GetPositionShares(t) > 0);
                if (!anyShares)
                    _eventsWithPositions.TryRemove(eventId, out _);
                return;
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
            decimal profit = netProceeds - totalCostBasis;
            if (profit / minShares < _minProfitPerSet)
                return;

            // Block new buys while selling
            _sellInProgress[eventId] = true;

            Console.WriteLine($"\n[ARB SELL-BACK] Event: {eventId}");
            Console.WriteLine($"-> Cost basis: ${totalCostBasis:0.00} | Net proceeds: ${netProceeds:0.00} | Profit: ${profit:0.00}");

            // Sell all legs
            foreach (var token in yesTokenIds)
            {
                if (!_books.TryGetValue(token, out var book)) continue;
                decimal bestBid = book.GetBestBidPrice();
                broker.SubmitSellAllOrder(token, bestBid, book);
            }

            // Track realized P&L
            lock (_pnlLock)
            {
                _totalRealizedPnL += profit;
                _totalCompletedSellBacks++;
            }

            // Clear state
            _eventsWithPositions.TryRemove(eventId, out _);
            _sellInProgress.TryRemove(eventId, out _);

            Console.WriteLine($"[ARB SELL-BACK COMPLETE] {yesTokenIds.Count} legs sold for {eventId} | P&L: ${profit:0.00} | Running Total: ${_totalRealizedPnL:0.00} ({_totalCompletedSellBacks} trades)");
        }
    }
}
