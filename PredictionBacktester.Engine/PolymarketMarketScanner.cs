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
        // FIX 1: Static HttpClient prevents socket exhaustion/memory leaks
        private static readonly HttpClient _httpClient = CreateConfiguredClient();
        
        private static HttpClient CreateConfiguredClient()
        {
            var client = new HttpClient();
            client.BaseAddress = new Uri("https://gamma-api.polymarket.com/");
            client.DefaultRequestHeaders.Add("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) QuantBot/1.0");
            return client;
        }

        public async Task<Dictionary<string, List<string>>> GetTopLiquidMarketsAsync(int targetMarketCount = 500)
        {
            Console.WriteLine($"\n[SCANNER] Initializing Gamma API Auto-Discovery...");
            Console.WriteLine($"[SCANNER] Target: Top {targetMarketCount} highest-volume active markets.");
            
            var arbConfig = new Dictionary<string, List<string>>();
            int offset = 0;
            int limit = 100; // API max is 100 per page

            while (arbConfig.Count < targetMarketCount)
            {
                string url = $"markets?active=true&closed=false&order=volume_24hr&ascending=false&limit={limit}&offset={offset}";
                
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

                foreach (var mkt in root.EnumerateArray())
                {
                    if (arbConfig.Count >= targetMarketCount) break;

                    // FIX 2: Use the exact Market ID to prevent duplicate key collisions
                    string marketId = mkt.GetProperty("id").GetString() ?? Guid.NewGuid().ToString();
                    string question = mkt.GetProperty("question").GetString() ?? "Unknown";
                    
                    List<string> tokenIds = new List<string>();
                    if (mkt.TryGetProperty("clobTokenIds", out var tokensEl))
                    {
                        if (tokensEl.ValueKind == JsonValueKind.String)
                        {
                            tokenIds = JsonSerializer.Deserialize<List<string>>(tokensEl.GetString()) ?? new List<string>();
                        }
                        else if (tokensEl.ValueKind == JsonValueKind.Array)
                        {
                            // FIX 3: Clean null/empty string handling
                            tokenIds = tokensEl.EnumerateArray()
                                              .Select(x => x.GetString())
                                              .Where(x => !string.IsNullOrEmpty(x))
                                              .ToList()!;
                        }
                    }

                    // We still want Binary (YES/NO) markets for Flash Crash Merge Arbs!
                    if (tokenIds.Count >= 2)
                    {
                        // Clean up the display name for the console logs
                        string safeName = question.Length > 30 ? question.Substring(0, 30).Trim() + "..." : question;
                        string displayKey = $"[{marketId}] {safeName}";
                        
                        // Because we use the actual Polymarket ID, collisions are mathematically impossible
                        if (!arbConfig.ContainsKey(displayKey))
                        {
                            arbConfig[displayKey] = tokenIds;
                        }
                    }
                }

                offset += limit;
                await Task.Delay(200); 
            }

            Console.WriteLine($"[SCANNER] Successfully locked in {arbConfig.Count} highly liquid markets.");
            return arbConfig;
        }
        
        // --- SCAVENGER BOT EXTENSION ---
        // We add this method now so the Scavenger bot can use it next week
        public async Task<Dictionary<string, List<string>>> GetLongTailMarketsAsync(int offset, int limit)
        {
            // Similar logic, but it allows the Scavenger to "walk" deep into the dead markets
            // We will flesh this out fully when we build the Scavenger loop.
            return new Dictionary<string, List<string>>(); 
        }
    }
}