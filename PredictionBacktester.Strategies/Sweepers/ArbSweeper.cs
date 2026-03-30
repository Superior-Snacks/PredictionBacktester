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
            HttpClient clobHttpClient)
        {
            _sharedInstances = sharedInstances;
            _orderBooks = orderBooks;
            _arbEvents = arbEvents;
            _tokenNames = tokenNames;
            _clobHttpClient = clobHttpClient;
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
            // 1. Near-miss report from telemetry strategy
            if (_sharedInstances.TryGetValue("Fast_Merge_Arb_Telemetry", out var telemetryInstance)
                && telemetryInstance is FastMergeArbTelemetryStrategy telemetry)
            {
                telemetry.PrintNearMissReport();
            }

            // 2. Message lag report
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

            // 3. Book audit (async, non-blocking)
            _ = Task.Run(async () =>
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
            });

            await Task.CompletedTask;
        }
    }
}
