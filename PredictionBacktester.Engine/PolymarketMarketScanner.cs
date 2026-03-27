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
                string url = $"markets?active=true&closed=false&order=volume24hr&ascending=false&limit={limit}&offset={offset}";
                
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

                    string marketId = mkt.GetProperty("conditionId").GetString() ?? Guid.NewGuid().ToString();
                    string question = mkt.GetProperty("question").GetString() ?? "Unknown";
                    
                    // --- NEW: Parse Negative Risk Flags ---
                    bool isNegRisk = mkt.TryGetProperty("negRisk", out var nrEl) && nrEl.ValueKind == JsonValueKind.True;
                    bool isAugmented = mkt.TryGetProperty("negRiskAugmented", out var augEl) && augEl.ValueKind == JsonValueKind.True;

                    // Safety: Skip augmented markets entirely so we don't accidentally arb "Placeholder" outcomes
                    if (isAugmented) continue; 

                    List<string> tokenIds = new List<string>();
                    if (mkt.TryGetProperty("clobTokenIds", out var tokensEl) && tokensEl.ValueKind == JsonValueKind.Array)
                    {
                        tokenIds = tokensEl.EnumerateArray()
                                        .Select(x => x.GetString())
                                        .Where(x => !string.IsNullOrEmpty(x))
                                        .ToList()!;
                    }

                    // --- NEW: Target ONLY 3+ leg markets ---
                    if (tokenIds.Count >= 3)
                    {
                        // Append [NegRisk] tag if applicable so the broker/strategy knows how to route it
                        string tag = isNegRisk ? "[NegRisk] " : "";
                        string safeName = question.Length > 30 ? question.Substring(0, 30).Trim() + "..." : question;
                        string displayKey = $"{tag}[{marketId}] {safeName}";
                        
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