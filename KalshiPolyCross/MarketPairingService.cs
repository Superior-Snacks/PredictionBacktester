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
                    polyMarket.Question,
                    polyMarket.YesToken,
                    polyMarket.NoToken,
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
                    PolyYesToken = s.YesToken,
                    PolyNoToken = s.NoToken
                });
            }
        }
        Console.WriteLine($"[PAIRING SERVICE] Coarse filtering complete: {candidates.Count} potential pairs found.");
        return candidates;
    }

    /// <summary>
    /// Pass 2: Use Gemini API to confirm semantic equivalence of candidate pairs.
    /// Sends candidates in batches of <see cref="BatchSize"/> to minimize API calls.
    /// </summary>
    private async Task<List<CandidatePair>> SemanticMatch(List<CandidatePair> candidates)
    {
        const int BatchSize = 15;
        var confirmed = new List<CandidatePair>();
        int totalBatches = (candidates.Count + BatchSize - 1) / BatchSize;
        Console.WriteLine($"[PAIRING SERVICE] Semantic matching: {candidates.Count} candidates in {totalBatches} batch(es) of up to {BatchSize}...");

        for (int i = 0; i < candidates.Count; i += BatchSize)
        {
            var batch = candidates.Skip(i).Take(BatchSize).ToList();
            int batchNum = i / BatchSize + 1;
            Console.Write($"  [Batch {batchNum}/{totalBatches}] Evaluating {batch.Count} pairs... ");

            var matchedIndices = await EvaluateBatch(batch);

            foreach (int idx in matchedIndices)
            {
                confirmed.Add(batch[idx]);
                Console.WriteLine($"\n    ✅ MATCH: K: \"{batch[idx].KalshiTitle}\" ↔ P: \"{batch[idx].PolyQuestion}\"");
            }

            int misses = batch.Count - matchedIndices.Count;
            Console.WriteLine($"  → {matchedIndices.Count} matched, {misses} rejected.");

            // Rate limit: free tier allows 15 requests/min → wait 4s between batches.
            if (i + BatchSize < candidates.Count)
                await Task.Delay(4000);
        }
        return confirmed;
    }

    /// <summary>
    /// Sends a batch of candidate pairs to Gemini in a single prompt and returns the
    /// 0-based indices of pairs that are confirmed matches.
    /// </summary>
    private async Task<List<int>> EvaluateBatch(List<CandidatePair> batch)
    {
        var sb = new StringBuilder();
        sb.AppendLine("You are evaluating prediction market titles from two platforms (Kalshi and Polymarket).");
        sb.AppendLine("For each numbered pair below, determine if both titles refer to the EXACT same real-world event and question — same outcome, same time period, same subject.");
        sb.AppendLine("Reply ONLY with a JSON array of the 0-based indices of TRUE matches. Example: [0,2,5]. If none match, reply []. Do not include any other text.");
        sb.AppendLine();

        for (int i = 0; i < batch.Count; i++)
            sb.AppendLine($"{i}. Kalshi: \"{batch[i].KalshiTitle}\" | Polymarket: \"{batch[i].PolyQuestion}\"");

        var payload = new
        {
            contents = new[] { new { parts = new[] { new { text = sb.ToString() } } } },
            generationConfig = new
            {
                temperature = 0.0,
                maxOutputTokens = 100,
                responseMimeType = "application/json"
            }
        };

        string url = $"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={_geminiApiKey}";
        var content = new StringContent(JsonSerializer.Serialize(payload), Encoding.UTF8, "application/json");

        try
        {
            var response = await _httpClient.PostAsync(url, content);
            response.EnsureSuccessStatusCode();

            string jsonResponse = await response.Content.ReadAsStringAsync();
            using var doc = JsonDocument.Parse(jsonResponse);

            var root = doc.RootElement;
            if (!root.TryGetProperty("candidates", out var geminiCandidates) || geminiCandidates.GetArrayLength() == 0)
                return [];

            if (!geminiCandidates[0].TryGetProperty("content", out var contentEl) ||
                !contentEl.TryGetProperty("parts", out var parts) || parts.GetArrayLength() == 0)
                return [];

            string text = parts[0].GetProperty("text").GetString()?.Trim() ?? "[]";

            // Parse the returned JSON int array, clamping to valid batch indices
            using var arrDoc = JsonDocument.Parse(text);
            if (arrDoc.RootElement.ValueKind != JsonValueKind.Array) return [];

            return arrDoc.RootElement
                .EnumerateArray()
                .Where(el => el.ValueKind == JsonValueKind.Number && el.TryGetInt32(out _))
                .Select(el => el.GetInt32())
                .Where(idx => idx >= 0 && idx < batch.Count)
                .Distinct()
                .ToList();
        }
        catch (Exception ex)
        {
            Console.WriteLine($"\n[PAIRING SERVICE] Error calling Gemini API: {ex.Message}");
            return [];
        }
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