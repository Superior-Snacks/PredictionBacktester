using System;
using System.Collections.Concurrent;
using System.Collections.Generic;
using System.Globalization;
using System.Linq;
using System.Net.Http;
using System.Text.Json;
using System.Threading.Tasks;
using PredictionBacktester.Data.ApiClients;
using PredictionBacktester.Engine;

namespace PredictionBacktester.Strategies.Sweepers
{
    public class FlashCrashSweeper : IStrategySweeper
    {
        private readonly PolymarketClient _apiClient;
        private readonly HashSet<string> _subscribedTokens;
        private readonly Dictionary<string, string> _tokenNames;
        private readonly Dictionary<string, decimal> _tokenMinSizes;
        private readonly Dictionary<string, int> _tokenFeeRates;
        private readonly HttpClient _clobHttpClient;
        private readonly Func<List<string>, Task> _subscribeCallback;
        private readonly Dictionary<string, GlobalSimulatedBroker> _brokers;
        private readonly Dictionary<string, decimal> _startingCapitals;
        private readonly HashSet<string> _droppedStrategies;
        private readonly ConcurrentDictionary<string, DateTime> _lastBookUpdate;
        private readonly HashSet<string> _forceSettled;

        public FlashCrashSweeper(
            PolymarketClient apiClient,
            HashSet<string> subscribedTokens,
            Dictionary<string, string> tokenNames,
            Dictionary<string, decimal> tokenMinSizes,
            Dictionary<string, int> tokenFeeRates,
            HttpClient clobHttpClient,
            Func<List<string>, Task> subscribeCallback,
            Dictionary<string, GlobalSimulatedBroker> brokers,
            Dictionary<string, decimal> startingCapitals,
            HashSet<string> droppedStrategies,
            ConcurrentDictionary<string, DateTime> lastBookUpdate,
            HashSet<string> forceSettled)
        {
            _apiClient = apiClient;
            _subscribedTokens = subscribedTokens;
            _tokenNames = tokenNames;
            _tokenMinSizes = tokenMinSizes;
            _tokenFeeRates = tokenFeeRates;
            _clobHttpClient = clobHttpClient;
            _subscribeCallback = subscribeCallback;
            _brokers = brokers;
            _startingCapitals = startingCapitals;
            _droppedStrategies = droppedStrategies;
            _lastBookUpdate = lastBookUpdate;
            _forceSettled = forceSettled;
        }

        public async Task RunSweepAsync()
        {
            // 1. Full performance dashboard
            PrintDashboard();

            // 2. Settlement sweep — resolve closed markets
            await RunSettlementSweepAsync();

            // 3. Staleness sweep — detect and resolve stale tokens
            await RunStalenessSweepAsync();

            // 4. Market discovery — find new >$50k binary markets
            await DiscoverAndSubscribeAsync();
        }

        private void PrintDashboard()
        {
            int maxNameLen = _brokers.Keys.Where(n => !_droppedStrategies.Contains(n)).Select(n => n.Length).DefaultIfEmpty(20).Max();

            Console.WriteLine("\n================= STRATEGY PERFORMANCE OVERVIEW =================");
            foreach (var kvp in _brokers)
            {
                if (_droppedStrategies.Contains(kvp.Key)) continue;
                string name = kvp.Key;
                var broker = kvp.Value;
                decimal startCap = _startingCapitals.GetValueOrDefault(name, 1000m);
                decimal totalEquity = broker.GetTotalPortfolioValue();
                decimal pnl = totalEquity - startCap;
                decimal mtmValue = totalEquity - broker.CashBalance;
                decimal realizedPnl = broker.CashBalance - startCap;

                lock (GlobalSimulatedBroker.ConsoleLock)
                {
                    Console.ForegroundColor = pnl >= 0 ? ConsoleColor.Green : ConsoleColor.Red;
                    Console.WriteLine($"[{name.PadRight(maxNameLen)}] Equity: ${totalEquity:0.00} | PnL: ${pnl:0.00} (Real: ${realizedPnl:0.00} + MTM: ${mtmValue:0.00}) | Actions: {broker.TotalActions} Exits: {broker.TotalTradesExecuted} (W:{broker.WinningTrades} L:{broker.LosingTrades}) Rej: {broker.RejectedOrders}");
                    Console.ResetColor();
                }
            }
            Console.WriteLine("=================================================================\n");
        }

        private async Task RunSettlementSweepAsync()
        {
            Console.WriteLine("\n[SYSTEM] Running background settlement sweep...");
            try
            {
                int sweepLimit = 100;
                for (int offset = 0; offset < 500; offset += sweepLimit)
                {
                    var events = await _apiClient.GetClosedEventsAsync(sweepLimit, offset);
                    if (events == null || events.Count == 0) break;

                    foreach (var ev in events)
                    {
                        if (ev.Markets == null) continue;
                        foreach (var market in ev.Markets)
                        {
                            if (market.IsClosed && market.ClobTokenIds != null && market.OutcomePrices != null)
                            {
                                for (int i = 0; i < market.ClobTokenIds.Length; i++)
                                {
                                    string tokenId = market.ClobTokenIds[i];
                                    foreach (var broker in _brokers.Values)
                                    {
                                        if (broker.GetPositionShares(tokenId) > 0 || broker.GetNoPositionShares(tokenId) > 0)
                                        {
                                            decimal finalPayoutPrice = 0.00m;
                                            if (i < market.OutcomePrices.Length && decimal.TryParse(market.OutcomePrices[i], out decimal price))
                                                finalPayoutPrice = price;

                                            broker.ResolveMarket(tokenId, finalPayoutPrice);
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
            }
            catch { /* Ignore sweep errors */ }
        }

        private async Task RunStalenessSweepAsync()
        {
            try
            {
                var staleThreshold = DateTime.UtcNow.AddHours(-1);
                var staleTokens = _lastBookUpdate
                    .Where(kvp => kvp.Value < staleThreshold && !_forceSettled.Contains(kvp.Key))
                    .Select(kvp => kvp.Key)
                    .ToList();

                int settled = 0;
                foreach (var tokenId in staleTokens)
                {
                    bool anyPosition = _brokers.Values.Any(b => b.GetPositionShares(tokenId) > 0 || b.GetNoPositionShares(tokenId) > 0);
                    if (!anyPosition)
                    {
                        _forceSettled.Add(tokenId);
                        continue;
                    }

                    var market = await _apiClient.GetMarketByTokenIdAsync(tokenId);
                    if (market == null) continue;

                    if (market.IsClosed && market.ClobTokenIds != null && market.OutcomePrices != null)
                    {
                        for (int i = 0; i < market.ClobTokenIds.Length; i++)
                        {
                            if (market.ClobTokenIds[i] == tokenId)
                            {
                                decimal payoutPrice = 0m;
                                if (i < market.OutcomePrices.Length)
                                    decimal.TryParse(market.OutcomePrices[i], NumberStyles.Any, CultureInfo.InvariantCulture, out payoutPrice);

                                foreach (var broker in _brokers.Values)
                                {
                                    if (broker.GetPositionShares(tokenId) > 0 || broker.GetNoPositionShares(tokenId) > 0)
                                        broker.ResolveMarket(tokenId, payoutPrice);
                                }

                                string name = _tokenNames.GetValueOrDefault(tokenId, tokenId[..Math.Min(8, tokenId.Length)] + "...");
                                lock (GlobalSimulatedBroker.ConsoleLock)
                                {
                                    Console.ForegroundColor = ConsoleColor.Yellow;
                                    Console.WriteLine($"[STALE SETTLE] {name} — no updates for 1h, API confirmed closed. Settled @ ${payoutPrice:0.00}");
                                    Console.ResetColor();
                                }
                                settled++;
                                break;
                            }
                        }
                    }
                    _forceSettled.Add(tokenId);

                    await Task.Delay(100);
                }

                if (settled > 0)
                    Console.WriteLine($"[STALE SETTLE] Force-settled {settled} stale market(s).");
            }
            catch { /* Ignore staleness sweep errors */ }
        }

        private async Task DiscoverAndSubscribeAsync()
        {
            try
            {
                List<string> newTokens = await DiscoverNewMarketsAsync();
                if (newTokens.Count > 0)
                {
                    Console.ForegroundColor = ConsoleColor.Green;
                    Console.WriteLine($"[DISCOVERY] Found {newTokens.Count} new market(s) crossing the $50k threshold! Subscribing...");
                    Console.ResetColor();

                    await _subscribeCallback(newTokens);
                }
            }
            catch (Exception ex)
            {
                Console.WriteLine($"[DISCOVERY ERROR] {ex.Message}");
            }
        }

        private async Task<List<string>> DiscoverNewMarketsAsync()
        {
            var newlyDiscovered = new List<string>();
            int limit = 100;

            for (int offset = 0; ; offset += limit)
            {
                var events = await _apiClient.GetActiveEventsAsync(limit, offset);
                if (events == null || events.Count == 0) break;

                foreach (var ev in events)
                {
                    if (ev.Markets == null) continue;
                    foreach (var market in ev.Markets)
                    {
                        if (market.StartDate.HasValue && market.StartDate.Value > DateTime.UtcNow) continue;
                        if (market.Volume < 50000m) continue;

                        if (market.ClobTokenIds != null && !market.IsClosed && market.ClobTokenIds.Length > 0)
                        {
                            string yesToken = market.ClobTokenIds[0];

                            if (_subscribedTokens.Add(yesToken))
                            {
                                _tokenNames.TryAdd(yesToken, market.Question);
                                _tokenMinSizes.TryAdd(yesToken, market.OrderMinSize > 0 ? market.OrderMinSize : 1.00m);
                                newlyDiscovered.Add(yesToken);

                                try
                                {
                                    var feeResp = await _clobHttpClient.GetStringAsync($"/fee-rate?token_id={yesToken}");
                                    using var doc = JsonDocument.Parse(feeResp);
                                    var root = doc.RootElement;
                                    if ((root.TryGetProperty("fee_rate_bps", out var feeEl) ||
                                         root.TryGetProperty("feeRateBps", out feeEl)) &&
                                        feeEl.TryGetInt32(out int bps) && bps > 0)
                                    {
                                        _tokenFeeRates[yesToken] = bps;
                                    }
                                }
                                catch { /* Non-critical — fees default to 0 if fetch fails */ }
                            }
                        }
                    }
                }
            }
            return newlyDiscovered;
        }
    }
}
