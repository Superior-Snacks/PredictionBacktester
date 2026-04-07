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

        /// <summary>Token ID → market question name, populated during scan.</summary>
        public Dictionary<string, string> TokenNames { get; } = new();

        /// <summary>
        /// Scans Gamma API for active categorical arb events (negRisk=true, 3+ legs).
        /// Only negRisk events have mutually exclusive outcomes where exactly one pays $1.00.
        /// Filters out: sports prop bundles, timeline events, augmented neg-risk traps.
        /// </summary>
        public async Task<Dictionary<string, List<string>>> GetTopLiquidEventsAsync(int targetEventCount = 0)
        {
            string targetLabel = targetEventCount > 0 ? $"Top {targetEventCount}" : "ALL";
            Console.WriteLine($"\n[SCANNER] Initializing Gamma API Auto-Discovery...");
            Console.WriteLine($"[SCANNER] Target: {targetLabel} active negRisk 3+ leg events.");

            // Sports events have a mandatory 3s matching delay — not viable for arb execution
            var sportsKeywords = new HashSet<string>(StringComparer.OrdinalIgnoreCase)
            {
                "soccer", "football", "basketball", "baseball", "hockey", "tennis",
                "mma", "ufc", "esports", "cricket", "rugby", "golf", "volleyball",
                "boxing", "cycling", "racing", "motorsport", "swimming", "athletics",
                "nba", "nfl", "nhl", "mlb", "nba", "ncaa", "epl", "champions-league",
                "la-liga", "bundesliga", "serie-a", "ligue-1", "sports"
            };

            var arbConfig = new Dictionary<string, List<string>>();
            int skippedNotNegRisk = 0;
            int skippedSports = 0;
            int skippedAugmented = 0;
            int skippedTooFewLegs = 0;
            int offset = 0;
            int limit = 100;

            while (targetEventCount <= 0 || arbConfig.Count < targetEventCount)
            {
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
                    if (targetEventCount > 0 && arbConfig.Count >= targetEventCount) break;

                    string eventId = evt.GetProperty("id").GetString() ?? Guid.NewGuid().ToString();

                    // Only negRisk events have mutually exclusive outcomes (elections, 3-way sports, Fed decisions).
                    // Non-negRisk events are prop bundles (NBA spreads+O/U+props) or timelines ("by March?/April?").
                    bool isNegRisk = evt.TryGetProperty("negRisk", out var nrEl) && nrEl.ValueKind == JsonValueKind.True;
                    if (!isNegRisk)
                    {
                        skippedNotNegRisk++;
                        continue;
                    }

                    // Skip sports events — Polymarket imposes a 3s matching delay on sports orders
                    // making arb execution unviable (prices move before fills land).
                    bool isSports = false;
                    if (evt.TryGetProperty("tags", out var tagsEl) && tagsEl.ValueKind == JsonValueKind.Array)
                    {
                        foreach (var tag in tagsEl.EnumerateArray())
                        {
                            string? slug  = tag.TryGetProperty("slug",  out var s) ? s.GetString() : null;
                            string? label = tag.TryGetProperty("label", out var l) ? l.GetString() : null;
                            if ((slug  != null && sportsKeywords.Any(k => slug .Contains(k, StringComparison.OrdinalIgnoreCase))) ||
                                (label != null && sportsKeywords.Any(k => label.Contains(k, StringComparison.OrdinalIgnoreCase))))
                            {
                                isSports = true;
                                break;
                            }
                        }
                    }
                    if (isSports)
                    {
                        skippedSports++;
                        continue;
                    }

                    if (!evt.TryGetProperty("markets", out var marketsEl) || marketsEl.ValueKind != JsonValueKind.Array)
                        continue;

                    if (marketsEl.GetArrayLength() < 3)
                    {
                        skippedTooFewLegs++;
                        continue;
                    }

                    List<string> yesTokenIds = new List<string>();
                    bool isAugmented = false;

                    foreach (var mkt in marketsEl.EnumerateArray())
                    {
                        // Skip augmented negative risk markets (placeholder traps)
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
                                tokens = JsonSerializer.Deserialize<List<string?>>(tokensEl.GetString()!) ?? new();
                            }
                            else
                            {
                                continue;
                            }

                            if (tokens.Count > 0 && !string.IsNullOrEmpty(tokens[0]))
                            {
                                string yesToken = tokens[0]!;
                                yesTokenIds.Add(yesToken);

                                if (mkt.TryGetProperty("question", out var qEl))
                                    TokenNames.TryAdd(yesToken, qEl.GetString() ?? "");
                            }
                        }
                    }

                    if (isAugmented)
                    {
                        skippedAugmented++;
                        continue;
                    }

                    if (yesTokenIds.Count < 3)
                    {
                        skippedTooFewLegs++;
                        continue;
                    }

                    if (!arbConfig.ContainsKey(eventId))
                    {
                        arbConfig[eventId] = yesTokenIds;
                    }
                }

                offset += limit;
                await Task.Delay(200);
            }

            Console.WriteLine($"[SCANNER] Found {arbConfig.Count} valid categorical arb events.");
            Console.WriteLine($"[SCANNER] Skipped: {skippedNotNegRisk} non-negRisk, {skippedSports} sports (3s delay), {skippedAugmented} augmented traps, {skippedTooFewLegs} < 3 legs.");
            return arbConfig;
        }
    }
}
