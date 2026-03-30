using System;
using System.Collections.Generic;
using System.Linq;
using System.Net.Http;
using System.Text.Json;
using System.Threading.Tasks;
using PredictionBacktester.Engine;

namespace PredictionBacktester.Strategies.Sweepers
{
    public class ArbSweeper : IStrategySweeper
    {
        private readonly Dictionary<string, ILiveStrategy> _sharedInstances;
        private readonly Dictionary<string, LocalOrderBook> _orderBooks;
        private readonly Dictionary<string, List<string>> _arbEvents;
        private readonly Dictionary<string, string> _tokenNames;
        private readonly HttpClient _clobHttpClient;
        private readonly Dictionary<string, GlobalSimulatedBroker> _brokers;
        private readonly Dictionary<string, decimal> _startingCapitals;
        private readonly HashSet<string> _droppedStrategies;
        private readonly HashSet<string> _subscribedTokens;
        private readonly Func<List<string>, Task> _subscribeCallback;

        // --- LAG TRACKING (owned by sweeper, fed from WS loop) ---
        private long _lagSampleCount = 0;
        private double _lagSumMs = 0;
        private double _lagMaxMs = 0;
        private readonly object _lagLock = new();

        public ArbSweeper(
            Dictionary<string, ILiveStrategy> sharedInstances,
            Dictionary<string, LocalOrderBook> orderBooks,
            Dictionary<string, List<string>> arbEvents,
            Dictionary<string, string> tokenNames,
            HttpClient clobHttpClient,
            Dictionary<string, GlobalSimulatedBroker> brokers,
            Dictionary<string, decimal> startingCapitals,
            HashSet<string> droppedStrategies,
            HashSet<string> subscribedTokens,
            Func<List<string>, Task> subscribeCallback)
        {
            _sharedInstances = sharedInstances;
            _orderBooks = orderBooks;
            _arbEvents = arbEvents;
            _tokenNames = tokenNames;
            _clobHttpClient = clobHttpClient;
            _brokers = brokers;
            _startingCapitals = startingCapitals;
            _droppedStrategies = droppedStrategies;
            _subscribedTokens = subscribedTokens;
            _subscribeCallback = subscribeCallback;
        }

        public void RecordLag(double lagMs)
        {
            lock (_lagLock)
            {
                _lagSampleCount++;
                _lagSumMs += lagMs;
                if (lagMs > _lagMaxMs) _lagMaxMs = lagMs;
            }
        }

        public async Task RunSweepAsync()
        {
            // 1. Arb P&L Dashboard (realized profit only — arbs are guaranteed at settlement)
            PrintArbDashboard();

            // 2. Near-miss report from telemetry strategy
            if (_sharedInstances.TryGetValue("Fast_Merge_Arb_Telemetry", out var telemetryInstance)
                && telemetryInstance is FastMergeArbTelemetryStrategy telemetry)
            {
                telemetry.PrintNearMissReport();
            }

            // 3. Message lag report
            lock (_lagLock)
            {
                if (_lagSampleCount > 0)
                {
                    double avgLag = _lagSumMs / _lagSampleCount;
                    var color = _lagMaxMs > 2000 ? ConsoleColor.Red : _lagMaxMs > 500 ? ConsoleColor.Yellow : ConsoleColor.Green;
                    Console.ForegroundColor = color;
                    Console.WriteLine($"[LAG] Avg: {avgLag:0}ms | Max: {_lagMaxMs:0}ms | Samples: {_lagSampleCount:N0}");
                    Console.ResetColor();
                    _lagSampleCount = 0;
                    _lagSumMs = 0;
                    _lagMaxMs = 0;
                }
            }

            // 4. Book audit (async, non-blocking)
            _ = RunBookAuditAsync();

            // 5. Discover new arb events and subscribe
            await DiscoverNewArbEventsAsync();
        }

        private void PrintArbDashboard()
        {
            int maxNameLen = _brokers.Keys.Where(n => !_droppedStrategies.Contains(n)).Select(n => n.Length).DefaultIfEmpty(20).Max();

            Console.WriteLine("\n================= ARB PERFORMANCE OVERVIEW =================");
            foreach (var kvp in _brokers)
            {
                if (_droppedStrategies.Contains(kvp.Key)) continue;
                string name = kvp.Key;
                var broker = kvp.Value;
                decimal startCap = _startingCapitals.GetValueOrDefault(name, 5000m);
                decimal realizedPnl = broker.CashBalance - startCap;

                lock (GlobalSimulatedBroker.ConsoleLock)
                {
                    Console.ForegroundColor = realizedPnl >= 0 ? ConsoleColor.Green : ConsoleColor.Red;
                    Console.WriteLine($"[{name.PadRight(maxNameLen)}] Cash: ${broker.CashBalance:0.00} | Realized P&L: ${realizedPnl:0.00} | Buys: {broker.TotalActions} Fills: {broker.TotalTradesExecuted} (W:{broker.WinningTrades} L:{broker.LosingTrades}) Rej: {broker.RejectedOrders}");
                    Console.ResetColor();
                }
            }
            Console.WriteLine($"  Events monitored: {_arbEvents.Count} | Tokens subscribed: {_subscribedTokens.Count} | Books loaded: {_orderBooks.Count}");
            Console.WriteLine("=============================================================\n");
        }

        private async Task DiscoverNewArbEventsAsync()
        {
            try
            {
                var scanner = new PolymarketMarketScanner();
                var allEvents = await scanner.GetTopLiquidEventsAsync();

                int newEvents = 0;
                var newTokens = new List<string>();

                foreach (var evt in allEvents)
                {
                    string eventId = evt.Key;
                    List<string> tokenIds = evt.Value;

                    if (_arbEvents.ContainsKey(eventId)) continue;

                    // Register with tracking state
                    _arbEvents[eventId] = tokenIds;
                    newEvents++;

                    // Register with shared strategy instances
                    foreach (var instance in _sharedInstances.Values)
                    {
                        if (instance is FastMergeArbTelemetryStrategy telemetry)
                            telemetry.RegisterEvent(eventId, tokenIds);
                        if (instance is PolymarketCategoricalArbStrategy execution)
                            execution.RegisterEvent(eventId, tokenIds);
                    }

                    // Track new tokens for WS subscription
                    foreach (var token in tokenIds)
                    {
                        if (_subscribedTokens.Add(token))
                        {
                            newTokens.Add(token);
                            if (scanner.TokenNames.TryGetValue(token, out var name))
                                _tokenNames.TryAdd(token, name);
                        }
                    }
                }

                if (newEvents > 0)
                {
                    Console.ForegroundColor = ConsoleColor.Green;
                    Console.WriteLine($"[ARB DISCOVERY] Found {newEvents} new 3+ leg event(s) ({newTokens.Count} new tokens). Subscribing...");
                    Console.ResetColor();

                    if (newTokens.Count > 0)
                        await _subscribeCallback(newTokens);
                }
            }
            catch (Exception ex)
            {
                Console.WriteLine($"[ARB DISCOVERY ERROR] {ex.Message}");
            }
        }

        private async Task RunBookAuditAsync()
        {
            try
            {
                var auditTokens = _orderBooks.Keys
                    .Where(k => _arbEvents.Values.Any(v => v.Contains(k)))
                    .OrderBy(_ => Random.Shared.Next())
                    .Take(5)
                    .ToList();

                if (auditTokens.Count == 0) return;

                int matches = 0, mismatches = 0;
                foreach (var token in auditTokens)
                {
                    try
                    {
                        string resp = await _clobHttpClient.GetStringAsync($"book?token_id={token}");
                        using var bookDoc = JsonDocument.Parse(resp);
                        var bookRoot = bookDoc.RootElement;

                        decimal restBestAsk = 1.00m;
                        if (bookRoot.TryGetProperty("asks", out var asksArr) && asksArr.GetArrayLength() > 0)
                        {
                            restBestAsk = asksArr.EnumerateArray()
                                .Select(a => decimal.Parse(a.GetProperty("price").GetString() ?? "1"))
                                .Min();
                        }

                        decimal localBestAsk = _orderBooks[token].GetBestAskPrice();

                        if (Math.Abs(restBestAsk - localBestAsk) < 0.002m)
                            matches++;
                        else
                        {
                            mismatches++;
                            string name = _tokenNames.GetValueOrDefault(token, token[..Math.Min(8, token.Length)] + "...");
                            Console.ForegroundColor = ConsoleColor.Red;
                            Console.WriteLine($"[AUDIT MISMATCH] {name}: Local=${localBestAsk:0.000} REST=${restBestAsk:0.000} (diff=${Math.Abs(restBestAsk - localBestAsk):0.000})");
                            Console.ResetColor();
                        }

                        await Task.Delay(200);
                    }
                    catch { /* skip failed fetches */ }
                }

                Console.ForegroundColor = mismatches > 0 ? ConsoleColor.Yellow : ConsoleColor.Green;
                Console.WriteLine($"[AUDIT] Book check: {matches}/{matches + mismatches} match ({mismatches} stale)");
                Console.ResetColor();
            }
            catch (Exception ex)
            {
                Console.WriteLine($"[AUDIT ERROR] {ex.Message}");
            }
        }
    }
}
