using System.Text;
using System.Text.Json;
using System.Text.RegularExpressions;

namespace KalshiPolyCross;

/// <summary>
/// Service to find matching markets between Kalshi and Polymarket using a hybrid
/// keyword filter and Gemini-powered semantic analysis.
/// </summary>
public class MarketPairingService
{
    private readonly string _geminiApiKey;
    private readonly HttpClient _httpClient;
    private const int MinMatchWords = 2;

    private class CandidatePair
    {
        public string KalshiTicker { get; set; } = "";
        public string KalshiTitle { get; set; } = "";
        public string PolyQuestion { get; set; } = "";
        public string PolyYesToken { get; set; } = "";
        public string PolyNoToken { get; set; } = "";
    }

    public MarketPairingService(string geminiApiKey)
    {
        _geminiApiKey = geminiApiKey;
        _httpClient = new HttpClient();
    }

    /// <summary>
    /// Main entry point. Orchestrates the entire matching process.
    /// </summary>
    public async Task FindAndSavePairs(
        Dictionary<string, string> kalshiTitles,
        List<(string Question, string YesToken, string NoToken)> polyMarkets,
        string outputPath)
    {
        Console.WriteLine("\n[PAIRING SERVICE] Starting market pairing process...");

        // Step 1: Coarse filtering based on keyword overlap.
        var candidates = CoarseFilter(kalshiTitles, polyMarkets);

        // Step 2: Semantic matching using the Gemini API.
        var confirmedPairs = await SemanticMatch(candidates);

        // Step 3: Persist the new pairs.
        await SavePairs(confirmedPairs, outputPath);

        Console.WriteLine("[PAIRING SERVICE] Market pairing process complete.");
    }

    /// <summary>
    /// Pass 1: Fast keyword-based filtering to find potential matches.
    /// </summary>
    private List<CandidatePair> CoarseFilter(
        Dictionary<string, string> kalshiTitles,
        List<(string Question, string YesToken, string NoToken)> polyMarkets)
    {
        var candidates = new List<CandidatePair>();
        Console.WriteLine("[PAIRING SERVICE] Coarse filtering: Finding candidates by keyword overlap...");

        foreach (var (kalshiTicker, kalshiTitle) in kalshiTitles)
        {
            var kalshiKeywords = TitleToKeyWords(kalshiTitle);
            if (kalshiKeywords.Count < MinMatchWords) continue;

            var scored = polyMarkets
                .Select(polyMarket => (
                    Question: polyMarket.Question,
                    Score: kalshiKeywords.Count(kw => polyMarket.Question.Contains(kw, StringComparison.OrdinalIgnoreCase))
                ))
                .Where(x => x.Score >= MinMatchWords)
                .OrderByDescending(x => x.Score)
                .ToList();

            foreach (var s in scored)
            {
                candidates.Add(new CandidatePair
                {
                    KalshiTicker = kalshiTicker,
                    KalshiTitle = kalshiTitle,
                    PolyQuestion = s.Question,
                    PolyYesToken = s.polyMarket.YesToken,
                    PolyNoToken = s.polyMarket.NoToken
                });
            }
        }
        Console.WriteLine($"[PAIRING SERVICE] Coarse filtering complete: {candidates.Count} potential pairs found.");
        return candidates;
    }

    /// <summary>
    /// Pass 2: Use Gemini API to confirm semantic equivalence of candidate pairs.
    /// </summary>
    private async Task<List<CandidatePair>> SemanticMatch(List<CandidatePair> candidates)
    {
        var confirmed = new List<CandidatePair>();
        Console.WriteLine("[PAIRING SERVICE] Semantic matching: Using Gemini API to confirm pairs...");

        for (int i = 0; i < candidates.Count; i++)
        {
            var candidate = candidates[i];
            Console.Write($"  [{i + 1}/{candidates.Count}] Evaluating K: \"{candidate.KalshiTitle}\" vs P: \"{candidate.PolyQuestion}\"... ");

            bool isMatch = await IsSameMarket(candidate.KalshiTitle, candidate.PolyQuestion);

            if (isMatch)
            {
                confirmed.Add(candidate);
                Console.WriteLine("✅ MATCH!");
            }
            else
            {
                Console.WriteLine("❌ No.");
            }

            // IMPORTANT: Rate limit to stay within the free tier (15 requests per minute).
            await Task.Delay(4000);
        }
        return confirmed;
    }

    private async Task<bool> IsSameMarket(string kalshiTitle, string polyQuestion)
    {
        string prompt = $"Are these two prediction market titles asking the exact same question about the exact same real-world event? Answer ONLY with 'YES' or 'NO'. Do not provide any other text.\n\nMarket 1: {kalshiTitle}\nMarket 2: {polyQuestion}";

        var payload = new
        {
            contents = new[] { new { parts = new[] { new { text = prompt } } } },
            generationConfig = new { temperature = 0.0, maxOutputTokens = 5 }
        };

        var content = new StringContent(JsonSerializer.Serialize(payload), Encoding.UTF8, "application/json");
        string url = $"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={_geminiApiKey}";

        try
        {
            var response = await _httpClient.PostAsync(url, content);
            response.EnsureSuccessStatusCode();

            string jsonResponse = await response.Content.ReadAsStringAsync();
            using var doc = JsonDocument.Parse(jsonResponse);
            
            var root = doc.RootElement;
            if (root.TryGetProperty("candidates", out var candidates) && candidates.GetArrayLength() > 0)
            {
                if (candidates[0].TryGetProperty("content", out var contentElement) &&
                    contentElement.TryGetProperty("parts", out var parts) && parts.GetArrayLength() > 0)
                {
                    var text = parts[0].GetProperty("text").GetString()?.Trim().ToUpperInvariant() ?? "";
                    return text.StartsWith("YES");
                }
            }
        }
        catch (Exception ex)
        {
            Console.WriteLine($"\n[PAIRING SERVICE] Error calling Gemini API: {ex.Message}");
        }

        return false;
    }

    private async Task SavePairs(List<CandidatePair> newPairs, string outputPath)
    {
        if (newPairs.Count == 0)
        {
            Console.WriteLine("[PAIRING SERVICE] No new pairs to save.");
            return;
        }

        Console.WriteLine($"[PAIRING SERVICE] Saving {newPairs.Count} new pairs to {outputPath}...");
        
        var existingPairs = new List<object>();
        if (System.IO.File.Exists(outputPath))
        {
            try
            {
                string json = await System.IO.File.ReadAllTextAsync(outputPath);
                existingPairs = JsonSerializer.Deserialize<List<object>>(json) ?? new List<object>();
            }
            catch (Exception ex)
            {
                Console.WriteLine($"[PAIRING SERVICE] Warning reading existing pairs: {ex.Message}");
            }
        }

        // Append the new pairs, mapping them to the format expected by Program.cs
        foreach (var pair in newPairs)
        {
            existingPairs.Add(new
            {
                kalshi_ticker = pair.KalshiTicker,
                poly_yes_token = pair.PolyYesToken,
                poly_no_token = pair.PolyNoToken,
                label = pair.KalshiTitle
            });
        }

        // Atomic file write using a temporary file
        string tempPath = outputPath + ".tmp";
        string outJson = JsonSerializer.Serialize(existingPairs, new JsonSerializerOptions { WriteIndented = true });
        await System.IO.File.WriteAllTextAsync(tempPath, outJson);
        System.IO.File.Move(tempPath, outputPath, overwrite: true);
        
        Console.WriteLine($"[PAIRING SERVICE] Successfully updated {outputPath}.");
    }

    private static List<string> TitleToKeyWords(string title)
    {
        // This can be the same helper function from Program.cs
        var stopWords = new HashSet<string>(StringComparer.OrdinalIgnoreCase) { "the", "a", "an", "will", "who", "what", "when", "by", "for", "in", "is", "at", "on", "or", "and", "to", "of" };
        return Regex.Split(title.ToLowerInvariant(), @"[^a-z0-9]+")
                   .Where(w => w.Length >= 3 && !stopWords.Contains(w))
                   .Distinct()
                   .ToList();
    }
}