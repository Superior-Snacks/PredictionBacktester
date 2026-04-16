using System.Text;
using System.Text.Json.Nodes;
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

    // Similarity score threshold for a pair to be considered a candidate for the AI judge.
    private const double SimilarityThreshold = 0.75;
    private const int TopNCandidates = 5; // For each Kalshi market, send the top N most similar Poly markets to the AI judge.

    private class CandidatePair
    {
        public string KalshiTicker { get; set; } = "";
        public string KalshiTitle { get; set; } = "";
        public DateTime? KalshiCloseDate { get; set; }
        public string KalshiRules { get; set; } = "";
        public string PolyQuestion { get; set; } = "";
        public string PolyYesToken { get; set; } = "";
        public string PolyNoToken { get; set; } = "";
        public DateTime? PolyCloseDate { get; set; }
        public string PolyDescription { get; set; } = "";
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
        Dictionary<string, (string Title, DateTime? CloseDate, string Rules)> kalshiMarkets,
        List<(string Question, string YesToken, string NoToken, DateTime? EndDate, string Description)> polyMarkets,
        string outputPath)
    {
        Console.WriteLine("\n[PAIRING SERVICE] Starting market pairing process...");
        Console.WriteLine("[PAIRING SERVICE] Ctrl+C at any time — pairs found so far are saved after each batch.");

        // Step 1: Advanced filtering based on semantic embeddings.
        var candidates = await EmbeddingFilter(kalshiMarkets, polyMarkets);

        // Step 2: Final semantic match via AI Judge, saving after each batch so
        // Ctrl+C doesn't lose work already done.
        await SemanticMatchAndSave(candidates, outputPath);

        Console.WriteLine("[PAIRING SERVICE] Market pairing process complete.");
    }

    /// <summary>
    /// Pass 1: Use text embeddings to find the most semantically similar market pairs.
    /// This is far more accurate than keyword matching.
    /// </summary>
    private async Task<List<CandidatePair>> EmbeddingFilter(
        Dictionary<string, (string Title, DateTime? CloseDate, string Rules)> kalshiMarkets,
        List<(string Question, string YesToken, string NoToken, DateTime? EndDate, string Description)> polyMarkets)
    {
        var candidates = new List<CandidatePair>();
        Console.WriteLine("[PAIRING SERVICE] Embedding filter: Finding candidates by semantic similarity...");

        if (kalshiMarkets.Count == 0 || polyMarkets.Count == 0)
        {
            Console.WriteLine("[PAIRING SERVICE] No markets from one or both platforms to compare. Skipping embedding filter.");
            return candidates;
        }

        // 1. Batch-fetch embeddings for ALL titles upfront — two API bursts instead of N.
        //    Both use the same GetEmbeddingsAsync batching (100/request, 1s between batches)
        //    which keeps us well within the free-tier 100 RPM limit.
        var polyQuestions = polyMarkets.Select(p => p.Question).ToList();
        var kalshiTitles  = kalshiMarkets.Values.Select(v => v.Title).Distinct().ToList();

        var polyEmbeddings  = await GetEmbeddingsAsync(polyQuestions, "Polymarket Questions");
        var kalshiEmbeddings = await GetEmbeddingsAsync(kalshiTitles, "Kalshi Titles");

        int processedCount = 0;
        Console.WriteLine($"[PAIRING SERVICE] Comparing {kalshiMarkets.Count} Kalshi markets against {polyMarkets.Count} Polymarket markets...");

        // 2. Pure in-memory loop — no API calls inside here.
        foreach (var (kalshiTicker, kalshiData) in kalshiMarkets)
        {
            if (!kalshiEmbeddings.TryGetValue(kalshiData.Title, out var kalshiEmbedding))
            {
                processedCount++;
                continue;
            }

            // 3. Apply Coarse Date Filter to eliminate obviously incompatible markets.
            //    7-day window: Kalshi and Polymarket sometimes express the same deadline differently.
            var validPolyMarkets = polyMarkets.Where(p =>
            {
                if (kalshiData.CloseDate.HasValue && p.EndDate.HasValue)
                {
                    if (Math.Abs((kalshiData.CloseDate.Value - p.EndDate.Value).TotalDays) > 7) return false;
                }
                return true;
            }).ToList();

            // 4. Score the Kalshi title against pre-fetched Polymarket embeddings.
            var scored = validPolyMarkets
                .Select(polyMarket =>
                {
                    polyEmbeddings.TryGetValue(polyMarket.Question, out var polyEmbedding);
                    return (
                        polyMarket,
                        Score: polyEmbedding != null ? CosineSimilarity(kalshiEmbedding, polyEmbedding) : -1.0
                    );
                })
                .Where(x => x.Score > SimilarityThreshold)
                .OrderByDescending(x => x.Score)
                .Take(TopNCandidates)
                .ToList();

            foreach (var s in scored)
            {
                candidates.Add(new CandidatePair
                {
                    KalshiTicker = kalshiTicker,
                    KalshiTitle = kalshiData.Title,
                    KalshiCloseDate = kalshiData.CloseDate,
                    KalshiRules = kalshiData.Rules,
                    PolyQuestion = s.polyMarket.Question,
                    PolyYesToken = s.polyMarket.YesToken,
                    PolyNoToken = s.polyMarket.NoToken,
                    PolyCloseDate = s.polyMarket.EndDate,
                    PolyDescription = s.polyMarket.Description
                });
            }

            processedCount++;
            if (processedCount % 50 == 0)
            {
                Console.WriteLine($"  [Embedding Filter] Processed {processedCount}/{kalshiMarkets.Count} Kalshi markets, found {candidates.Count} candidates so far...");
            }
        }

        Console.WriteLine($"[PAIRING SERVICE] Embedding filter complete: {candidates.Count} potential pairs found.");
        return candidates;
    }

    /// <summary>
    /// Calls the Gemini `batchEmbedContents` API to get embeddings for a list of texts.
    /// Handles batching to respect the API's limit of 100 texts per request.
    /// </summary>
    private async Task<Dictionary<string, float[]>> GetEmbeddingsAsync(IEnumerable<string> texts, string description)
    {
        var embeddings = new Dictionary<string, float[]>();
        // Free tier counts each individual text as 1 request toward the 100 RPM quota.
        // Batch of 20 + 15s delay ≈ 80 texts/min — safely under the cap.
        const int ApiBatchSize = 20;
        var textList = texts.ToList();

        for (int i = 0; i < textList.Count; i += ApiBatchSize)
        {
            var batch = textList.Skip(i).Take(ApiBatchSize).ToList();
            Console.WriteLine($"[EMBEDDING] Getting embeddings for {batch.Count} texts ({description})...");

            var requests = batch.Select(text => new
            {
                model = "models/gemini-embedding-001",
                content = new { parts = new[] { new { text } } }
            }).ToList();

            var payload = new { requests };
            string url = $"https://generativelanguage.googleapis.com/v1beta/models/gemini-embedding-001:batchEmbedContents?key={_geminiApiKey.Trim()}";
            var content = new StringContent(JsonSerializer.Serialize(payload), Encoding.UTF8, "application/json");

            try
            {
                var response = await _httpClient.PostAsync(url, content);
                if (!response.IsSuccessStatusCode)
                {
                    string errDetails = await response.Content.ReadAsStringAsync();
                    Console.WriteLine($"\n[EMBEDDING] Error calling Gemini embedding API: {response.StatusCode} - {errDetails}");
                    continue; // Skip this batch
                }

                using var doc = JsonDocument.Parse(await response.Content.ReadAsStringAsync());
                if (doc.RootElement.TryGetProperty("embeddings", out var embeddingsArray))
                {
                    for (int j = 0; j < embeddingsArray.GetArrayLength(); j++)
                    {
                        var embeddingData = embeddingsArray[j];
                        if (embeddingData.TryGetProperty("value", out var values))
                        {
                            var vector = values.EnumerateArray().Select(v => v.GetSingle()).ToArray();
                            embeddings[batch[j]] = vector;
                        }
                    }
                }
            }
            catch (Exception ex)
            {
                Console.WriteLine($"\n[EMBEDDING] Exception during embedding API call: {ex.Message}");
            }

            if (i + ApiBatchSize < textList.Count)
                await Task.Delay(15000); // 15s between batches → ~80 texts/min under 100 RPM free tier
        }

        return embeddings;
    }

    /// <summary>
    /// Pass 2: Evaluate each batch with the AI Judge and immediately save
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
            if (matched.Any())
                await SavePairs(matched, outputPath);

            totalMatched += matched.Count;

            // Rate limit: free tier allows 5 requests/min → wait 13s between batches.
            if (i + BatchSize < candidates.Count)
                await Task.Delay(13000);
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
        sb.AppendLine("You are a strict financial compliance officer auditing arbitrage opportunities. You will be given one target Kalshi market and a list of candidate Polymarket markets.");
        sb.AppendLine("Your job is to find an EXACT MATCH. An exact match means the underlying oracle, the resolution metric, and the expiration date/time are identical.");
        sb.AppendLine("Pay extreme attention to 'Dead Heat' rules, overtime rules, and timezones.");
        sb.AppendLine("For each numbered pair below, determine if both titles refer to the EXACT same real-world event and question.");
        sb.AppendLine("Reply ONLY with a JSON array of the 0-based indices of TRUE matches. Example: [0,2,5]. If none match, reply []. Do not include any other text.");
        sb.AppendLine();

        for (int i = 0; i < batch.Count; i++)
        {
            var c = batch[i];
            sb.AppendLine($"[{i}]");
            sb.AppendLine($"KALSHI MARKET:");
            sb.AppendLine($"Title: {c.KalshiTitle}");
            sb.AppendLine($"Close Date: {(c.KalshiCloseDate.HasValue ? c.KalshiCloseDate.Value.ToString("O") : "Unknown")}");
            sb.AppendLine($"Resolution Rules: {c.KalshiRules}");
            sb.AppendLine($"POLYMARKET MARKET:");
            sb.AppendLine($"Title: {c.PolyQuestion}");
            sb.AppendLine($"Close Date: {(c.PolyCloseDate.HasValue ? c.PolyCloseDate.Value.ToString("O") : "Unknown")}");
            sb.AppendLine($"Description: {c.PolyDescription}");
            sb.AppendLine();
        }

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

        // .Trim() removes any invisible newlines from the .env file that would corrupt the URL
        string url = $"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-pro:generateContent?key={_geminiApiKey.Trim()}";
        var content = new StringContent(JsonSerializer.Serialize(payload), Encoding.UTF8, "application/json");

        try
        {
            var response = await _httpClient.PostAsync(url, content);
            if (!response.IsSuccessStatusCode)
            {
                string errDetails = await response.Content.ReadAsStringAsync();
                Console.WriteLine($"\n[PAIRING SERVICE] Error calling Gemini API: {response.StatusCode} - {errDetails}");
                return [];
            }

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

    /// <summary>
    /// Calculates the cosine similarity between two vectors.
    /// </summary>
    private static double CosineSimilarity(float[] vecA, float[] vecB)
    {
        if (vecA.Length != vecB.Length)
            throw new ArgumentException("Vectors must have the same dimension.");

        double dotProduct = 0.0;
        double normA = 0.0;
        double normB = 0.0;
        for (int i = 0; i < vecA.Length; i++)
        {
            dotProduct += vecA[i] * vecB[i];
            normA += vecA[i] * vecA[i];
            normB += vecB[i] * vecB[i];
        }
        double denom = Math.Sqrt(normA) * Math.Sqrt(normB);
        return denom == 0.0 ? 0.0 : dotProduct / denom;
    }
}