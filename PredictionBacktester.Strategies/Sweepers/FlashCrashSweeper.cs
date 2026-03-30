using System;
using System.Collections.Generic;
using System.Net.Http;
using System.Text.Json;
using System.Threading.Tasks;
using PredictionBacktester.Data.ApiClients;

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

        public FlashCrashSweeper(
            PolymarketClient apiClient,
            HashSet<string> subscribedTokens,
            Dictionary<string, string> tokenNames,
            Dictionary<string, decimal> tokenMinSizes,
            Dictionary<string, int> tokenFeeRates,
            HttpClient clobHttpClient,
            Func<List<string>, Task> subscribeCallback)
        {
            _apiClient = apiClient;
            _subscribedTokens = subscribedTokens;
            _tokenNames = tokenNames;
            _tokenMinSizes = tokenMinSizes;
            _tokenFeeRates = tokenFeeRates;
            _clobHttpClient = clobHttpClient;
            _subscribeCallback = subscribeCallback;
        }

        public async Task RunSweepAsync()
        {
            try
            {
                List<string> newTokens = await DiscoverNewMarketsAsync(_apiClient);
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

        private async Task<List<string>> DiscoverNewMarketsAsync(PolymarketClient apiClient)
        {
            var newlyDiscovered = new List<string>();
            int limit = 100;

            for (int offset = 0; ; offset += limit)
            {
                var events = await apiClient.GetActiveEventsAsync(limit, offset);
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
