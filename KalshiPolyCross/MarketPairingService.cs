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
        Console.WriteLine("[PAIRING SERVICE] Ctrl+C at any time — pairs found so far are saved after each batch.");

        // Step 1: Coarse filtering based on keyword overlap.
        var candidates = CoarseFilter(kalshiTitles, polyMarkets);

        // Step 2 + 3: Semantic match batch-by-batch, saving after each batch so
        // Ctrl+C doesn't lose work already done.
        await SemanticMatchAndSave(candidates, outputPath);

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
    /// Pass 2+3 combined: evaluate each batch with Gemini and immediately save
    /// any matches to disk. This way Ctrl+C never loses already-confirmed pairs.
    /// </summary>
    private async Task SemanticMatchAndSave(List<CandidatePair> candidates, string outputPath)
    {
        const int BatchSize = 15;
        int totalBatches = (candidates.Count + BatchSize - 1) / BatchSize;
        int totalMatched = 0;
        Console.WriteLine($"[PAIRING SERVICE] Semantic matching: {candidates.Count} candidates in {totalBatches} batch(es) of up to {BatchSize}...");

        for (int i = 0; i < candidates.Count; i += BatchSize)
        {
            var batch = candidates.Skip(i).Take(BatchSize).ToList();
            int batchNum = i / BatchSize + 1;
            Console.Write($"  [Batch {batchNum}/{totalBatches}] Evaluating {batch.Count} pairs... ");

            var matchedIndices = await EvaluateBatch(batch);
            var matched = matchedIndices.Select(idx => batch[idx]).ToList();

            foreach (var m in matched)
                Console.WriteLine($"\n    ✅ MATCH: K: \"{m.KalshiTitle}\" ↔ P: \"{m.PolyQuestion}\"");

            Console.WriteLine($"  → {matched.Count} matched, {batch.Count - matched.Count} rejected.");

            // Save immediately — if the user Ctrl+C's after this line, these pairs are safe.
            if (matched.Count > 0)
                await SavePairs(matched, outputPath);

            totalMatched += matched.Count;

            // Rate limit: free tier allows 15 requests/min → wait 4s between batches.
            if (i + BatchSize < candidates.Count)
                await Task.Delay(4000);
        }

        Console.WriteLine($"[PAIRING SERVICE] Done — {totalMatched} total pair(s) confirmed across {totalBatches} batch(es).");
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

        // Load existing pairs and build a dedup key set to prevent running --pair twice
        // from accumulating duplicates that cause the bot to monitor the same market twice.
        var outputArray  = new System.Text.Json.Nodes.JsonArray();
        // Key = "kalshi_ticker|poly_yes_token" (case-insensitive) for dedup
        var existingKeys = new HashSet<string>(StringComparer.OrdinalIgnoreCase);

        if (System.IO.File.Exists(outputPath))
        {
            try
            {
                string json = await System.IO.File.ReadAllTextAsync(outputPath);
                using var doc = JsonDocument.Parse(json);
                foreach (var el in doc.RootElement.EnumerateArray())
                {
                    string kt = el.TryGetProperty("kalshi_ticker",  out var k) ? k.GetString() ?? "" : "";
                    string pt = el.TryGetProperty("poly_yes_token", out var p) ? p.GetString() ?? "" : "";
                    existingKeys.Add($"{kt}|{pt}");
                    outputArray.Add(System.Text.Json.Nodes.JsonNode.Parse(el.GetRawText()));
                }
            }
            catch (Exception ex)
            {
                Console.WriteLine($"[PAIRING SERVICE] Warning reading existing pairs: {ex.Message}");
            }
        }

        int added = 0, skipped = 0;
        foreach (var pair in newPairs)
        {
            if (!existingKeys.Add($"{pair.KalshiTicker}|{pair.PolyYesToken}"))
            {
                Console.WriteLine($"[PAIRING SERVICE] Skipping duplicate: {pair.KalshiTicker}");
                skipped++;
                continue;
            }
            outputArray.Add(new System.Text.Json.Nodes.JsonObject
            {
                ["kalshi_ticker"]  = pair.KalshiTicker,
                ["poly_yes_token"] = pair.PolyYesToken,
                ["poly_no_token"]  = pair.PolyNoToken,
                ["label"]          = pair.KalshiTitle
            });
            added++;
        }

        if (skipped > 0) Console.WriteLine($"[PAIRING SERVICE] Skipped {skipped} duplicate(s).");

        if (added == 0)
        {
            Console.WriteLine("[PAIRING SERVICE] No new unique pairs to save.");
            return;
        }

        // Atomic write
        string tempPath = outputPath + ".tmp";
        await System.IO.File.WriteAllTextAsync(tempPath,
            outputArray.ToJsonString(new JsonSerializerOptions { WriteIndented = true }));
        System.IO.File.Move(tempPath, outputPath, overwrite: true);

        Console.WriteLine($"[PAIRING SERVICE] Saved {added} new pair(s) to {outputPath}.");
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