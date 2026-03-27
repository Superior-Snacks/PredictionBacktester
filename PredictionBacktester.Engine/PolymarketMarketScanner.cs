using System;
using System.Collections.Generic;
using System.Linq;
using System.Net.Http;
using System.Text.Json;
using System.Threading.Tasks;

namespace PredictionBacktester.Engine
{
    public class PolymarketMarketScanner
    {
        private static readonly HttpClient _httpClient = CreateConfiguredClient();
        
        private static HttpClient CreateConfiguredClient()
        {
            var client = new HttpClient();
            client.BaseAddress = new Uri("https://gamma-api.polymarket.com/");
            client.DefaultRequestHeaders.Add("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) QuantBot/1.0");
            return client;
        }

        public async Task<Dictionary<string, List<string>>> GetTopLiquidEventsAsync(int targetEventCount = 500)
        {
            Console.WriteLine($"\n[SCANNER] Initializing Gamma API Auto-Discovery for Events...");
            Console.WriteLine($"[SCANNER] Target: Top {targetEventCount} highest-volume active events.");
            
            var arbConfig = new Dictionary<string, List<string>>();
            int offset = 0;
            int limit = 100; // API max is 100 per page

            while (arbConfig.Count < targetEventCount)
            {
                // Fetch /events sorted by volume
                string url = $"events?active=true&closed=false&order=volume24hr&ascending=false&limit={limit}&offset={offset}";
                
                var response = await _httpClient.GetAsync(url);
                if (!response.IsSuccessStatusCode)
                {
                    Console.WriteLine($"[SCANNER ERROR] Gamma API returned {response.StatusCode}");
                    break;
                }

                string json = await response.Content.ReadAsStringAsync();
                using var doc = JsonDocument.Parse(json);
                var root = doc.RootElement;

                if (root.ValueKind != JsonValueKind.Array || root.GetArrayLength() == 0)
                    break; 

                foreach (var evt in root.EnumerateArray())
                {
                    if (arbConfig.Count >= targetEventCount) break;

                    string eventId = evt.GetProperty("id").GetString() ?? Guid.NewGuid().ToString();
                    
                    // We need the child markets
                    if (!evt.TryGetProperty("markets", out var marketsEl) || marketsEl.ValueKind != JsonValueKind.Array)
                        continue;

                    // Filter out standard binary events (must have 3+ child markets)
                    if (marketsEl.GetArrayLength() < 3)
                        continue;

                    List<string> yesTokenIds = new List<string>();
                    bool isAugmented = false;

                    foreach (var mkt in marketsEl.EnumerateArray())
                    {
                        // Skip if the event has augmented negative risk (placeholder trap)
                        if (mkt.TryGetProperty("negRiskAugmented", out var augEl) && augEl.ValueKind == JsonValueKind.True)
                        {
                            isAugmented = true;
                            break;
                        }

                        // Collect the YES token (Index 0) from each child market
                        // clobTokenIds can be a JSON array OR a JSON-encoded string — handle both
                        if (mkt.TryGetProperty("clobTokenIds", out var tokensEl))
                        {
                            List<string?> tokens;
                            if (tokensEl.ValueKind == JsonValueKind.Array)
                            {
                                tokens = tokensEl.EnumerateArray().Select(x => x.GetString()).ToList();
                            }
                            else if (tokensEl.ValueKind == JsonValueKind.String)
                            {
                                // API returns clobTokenIds as a serialized JSON string: "[\"token1\",\"token2\"]"
                                tokens = JsonSerializer.Deserialize<List<string?>>(tokensEl.GetString()!) ?? new();
                            }
                            else
                            {
                                continue;
                            }

                            if (tokens.Count > 0 && !string.IsNullOrEmpty(tokens[0]))
                            {
                                yesTokenIds.Add(tokens[0]!);
                            }
                        }
                    }

                    if (isAugmented || yesTokenIds.Count < 3) 
                        continue;

                    if (!arbConfig.ContainsKey(eventId))
                    {
                        arbConfig[eventId] = yesTokenIds;
                    }
                }

                offset += limit;
                await Task.Delay(200); 
            }

            Console.WriteLine($"[SCANNER] Successfully locked in {arbConfig.Count} highly liquid 3+ leg events.");
            return arbConfig;
        }
    }
}