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
        private readonly HttpClient _httpClient;
        
        public PolymarketMarketScanner()
        {
            _httpClient = new HttpClient();
            _httpClient.BaseAddress = new Uri("https://gamma-api.polymarket.com/");
            
            // Polymarket can block default HTTP clients. We disguise it as a standard web browser.
            _httpClient.DefaultRequestHeaders.Add("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64)");
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
                // Fetch active, open markets, sorted descending by 24h volume
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

                // If the array is empty, we have scraped the entire active exchange
                if (root.ValueKind != JsonValueKind.Array || root.GetArrayLength() == 0)
                    break; 

                foreach (var mkt in root.EnumerateArray())
                {
                    if (arbConfig.Count >= targetMarketCount) break;

                    string question = mkt.GetProperty("question").GetString() ?? "Unknown Market";
                    
                    // Parse Token IDs (Polymarket Gamma API sometimes returns this as a stringified JSON array)
                    List<string> tokenIds = new List<string>();
                    if (mkt.TryGetProperty("clobTokenIds", out var tokensEl))
                    {
                        if (tokensEl.ValueKind == JsonValueKind.String)
                        {
                            tokenIds = JsonSerializer.Deserialize<List<string>>(tokensEl.GetString()) ?? new List<string>();
                        }
                        else if (tokensEl.ValueKind == JsonValueKind.Array)
                        {
                            tokenIds = tokensEl.EnumerateArray().Select(x => x.GetString()).ToList();
                        }
                    }

                    // We need at least 2 tokens (YES/NO or A/B/C) to form an Arbitrage group
                    if (tokenIds.Count >= 2 && !tokenIds.Contains(null))
                    {
                        // Clean the question for a safe, readable dictionary key
                        string safeKey = new string(question.Where(c => char.IsLetterOrDigit(c) || c == ' ').ToArray());
                        
                        // Truncate long titles so your console logs don't get messy
                        if (safeKey.Length > 40) safeKey = safeKey.Substring(0, 40).Trim() + "...";
                        
                        string marketKey = $"Rank_{arbConfig.Count + 1}_{safeKey.Replace(" ", "_")}";
                        
                        // Prevent duplicates
                        if (!arbConfig.ContainsKey(marketKey))
                        {
                            arbConfig[marketKey] = tokenIds;
                        }
                    }
                }

                offset += limit;
                
                // Polite rate limiting. The Gamma API limit is 300 req / 10s. This delay keeps us incredibly safe.
                await Task.Delay(200); 
            }

            Console.WriteLine($"[SCANNER] Successfully locked in {arbConfig.Count} highly liquid markets.");
            return arbConfig;
        }
    }
}