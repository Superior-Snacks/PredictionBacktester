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

    // Embedding model — change here if the model name changes.
    private const string EmbeddingModel = "gemini-embedding-002";

    // Embeddings are saved to disk after every batch so overnight runs survive
    // daily quota exhaustion and can resume the next day without re-fetching.
    private const string EmbeddingCachePath = "embeddings_cache.json";

    // AI judge models ranked by intelligence. The waterfall removes a model from the
    // front of the list the moment its daily quota (RPD) is exhausted, then retries
    // the same batch with the next model. RPM hits just wait 65s and retry same model.
    private readonly List<string> _judgeModels =
    [
        "gemini-3-flash",        // Best reasoning — try first
        "gemini-2.5-flash",      // Fallback 1
        "gemini-2.5-flash-lite", // Fallback 2
        "gemini-3.1-flash-lite"  // Fallback 3 — bulk processor
    ];

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
        public double Score { get; set; } // Cosine similarity — used to prioritise judge batches
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

        // Load already-confirmed pairs so subsequent runs skip Kalshi tickers that
        // were matched on a previous day — avoids wasting judge RPD budget on them.
        var alreadyPairedTickers = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        if (File.Exists(outputPath))
        {
            try
            {
                using var doc = JsonDocument.Parse(await File.ReadAllTextAsync(outputPath));
                foreach (var el in doc.RootElement.EnumerateArray())
                    if (el.TryGetProperty("kalshi_ticker", out var kt) && kt.GetString() is { } t)
                        alreadyPairedTickers.Add(t);
                if (alreadyPairedTickers.Count > 0)
                    Console.WriteLine($"[PAIRING SERVICE] Skipping {alreadyPairedTickers.Count} already-paired Kalshi ticker(s) from previous run(s).");
            }
            catch { /* fresh file or empty — proceed normally */ }
        }

        // Step 1: Advanced filtering based on semantic embeddings.
        var candidates = await EmbeddingFilter(kalshiMarkets, polyMarkets, alreadyPairedTickers);

        // Step 2: Sort candidates by cosine score descending so the judge evaluates
        // the highest-probability pairs first — if RPD runs out mid-run, the best
        // candidates will already have been evaluated.
        candidates.Sort((a, b) => b.Score.CompareTo(a.Score));

        // Step 3: Final semantic match via AI Judge, saving after each batch so
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
        List<(string Question, string YesToken, string NoToken, DateTime? EndDate, string Description)> polyMarkets,
        HashSet<string> alreadyPairedTickers)
    {
        var candidates = new List<CandidatePair>();
        Console.WriteLine("[PAIRING SERVICE] Embedding filter: Finding candidates by semantic similarity...");

        if (kalshiMarkets.Count == 0 || polyMarkets.Count == 0)
        {
            Console.WriteLine("[PAIRING SERVICE] No markets from one or both platforms to compare. Skipping embedding filter.");
            return candidates;
        }

        // 1. Load persisted embedding cache — lets overnight runs resume without
        //    re-spending daily quota on titles already embedded on a previous run.
        var cache = await LoadEmbeddingCacheAsync();
        int cacheHits = kalshiMarkets.Values.Count(v => cache.ContainsKey(v.Title))
                      + polyMarkets.Count(p => cache.ContainsKey(p.Question));
        Console.WriteLine($"[PAIRING SERVICE] Embedding cache: {cache.Count} entries loaded ({cacheHits} cover current markets).");

        // 2. Batch-fetch embeddings for ALL titles upfront — two API bursts instead of N.
        //    Already-cached texts are skipped; newly fetched ones are saved to disk after
        //    every batch so a daily quota cutoff doesn't lose work already done.
        var polyQuestions  = polyMarkets.Select(p => p.Question).ToList();
        var kalshiTitles   = kalshiMarkets.Values.Select(v => v.Title).Distinct().ToList();

        var polyEmbeddings   = await GetEmbeddingsAsync(polyQuestions, "Polymarket Questions", cache);
        var kalshiEmbeddings = await GetEmbeddingsAsync(kalshiTitles,  "Kalshi Titles",        cache);

        int processedCount = 0;
        Console.WriteLine($"[PAIRING SERVICE] Comparing {kalshiMarkets.Count} Kalshi markets against {polyMarkets.Count} Polymarket markets...");

        // 2. Pure in-memory loop — no API calls inside here.
        foreach (var (kalshiTicker, kalshiData) in kalshiMarkets)
        {
            if (alreadyPairedTickers.Contains(kalshiTicker)) continue;

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
                    PolyDescription = s.polyMarket.Description,
                    Score = s.Score
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
    /// Calls the Gemini batchEmbedContents API. Skips texts already in the shared
    /// cache, saves new results to disk after every batch, and stops gracefully on
    /// a daily quota (RPD) 429 so the run can resume tomorrow with the saved cache.
    /// </summary>
    private async Task<Dictionary<string, float[]>> GetEmbeddingsAsync(
        IEnumerable<string> texts, string description, Dictionary<string, float[]> cache)
    {
        var result   = new Dictionary<string, float[]>();
        var textList = texts.ToList();

        // Serve cached entries immediately.
        var uncached = new List<string>();
        foreach (var text in textList)
        {
            if (cache.TryGetValue(text, out var cached))
                result[text] = cached;
            else
                uncached.Add(text);
        }

        if (uncached.Count == 0)
        {
            Console.WriteLine($"[EMBEDDING] All {textList.Count} {description} embeddings served from cache.");
            return result;
        }

        Console.WriteLine($"[EMBEDDING] {result.Count} cached / {uncached.Count} to fetch ({description}).");

        // Free tier: 100 RPM, 1 000 RPD (each API call = 1 request regardless of batch size).
        // Maximise texts per call (100 = API limit) so the 1 000 RPD covers ~100 000 texts/day.
        // 1s delay between calls stays well under 100 RPM.
        const int ApiBatchSize = 100;
        string url = $"https://generativelanguage.googleapis.com/v1beta/models/{EmbeddingModel}:batchEmbedContents?key={_geminiApiKey.Trim()}";

        for (int i = 0; i < uncached.Count; i += ApiBatchSize)
        {
            var batch = uncached.Skip(i).Take(ApiBatchSize).ToList();
            int batchNum   = i / ApiBatchSize + 1;
            int totalBatches = (uncached.Count + ApiBatchSize - 1) / ApiBatchSize;
            Console.WriteLine($"[EMBEDDING] [{description}] Batch {batchNum}/{totalBatches} — {batch.Count} texts...");

            var requests = batch.Select(text => new
            {
                model   = $"models/{EmbeddingModel}",
                content = new { parts = new[] { new { text } } }
            }).ToList();

            var payload = new { requests };
            var content = new StringContent(JsonSerializer.Serialize(payload), Encoding.UTF8, "application/json");

            try
            {
                var response = await _httpClient.PostAsync(url, content);
                if (!response.IsSuccessStatusCode)
                {
                    string errDetails = await response.Content.ReadAsStringAsync();

                    if ((int)response.StatusCode == 429)
                    {
                        // Daily quota (RPD) exhausted — save cache and bail out gracefully.
                        // The next run will resume from where we left off.
                        if (errDetails.Contains("day", StringComparison.OrdinalIgnoreCase) ||
                            errDetails.Contains("PerDay", StringComparison.OrdinalIgnoreCase))
                        {
                            Console.WriteLine($"\n[EMBEDDING] Daily quota (RPD) exhausted after {result.Count} embeddings.");
                            Console.WriteLine("[EMBEDDING] Cache saved — re-run tomorrow to continue from this point.");
                            return result;
                        }

                        // Per-minute quota (RPM) — wait 65s and retry the same batch.
                        Console.WriteLine($"\n[EMBEDDING] Per-minute quota hit, waiting 65s before retry...");
                        await Task.Delay(65000);
                        i -= ApiBatchSize; // will be re-incremented by the for loop
                        continue;
                    }

                    Console.WriteLine($"\n[EMBEDDING] Error: {response.StatusCode} — {errDetails}");
                    continue;
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
                            result[batch[j]]  = vector;
                            cache[batch[j]]   = vector;
                        }
                    }
                }

                // Persist after every successful batch — quota cutoff mid-run loses nothing.
                await SaveEmbeddingCacheAsync(cache);
            }
            catch (Exception ex)
            {
                Console.WriteLine($"\n[EMBEDDING] Exception: {ex.Message}");
            }

            if (i + ApiBatchSize < uncached.Count)
                await Task.Delay(12000); // 12s between batches — stays under 30K TPM (covers ~58 tokens/title avg)
        }

        return result;
    }

    private static async Task<Dictionary<string, float[]>> LoadEmbeddingCacheAsync()
    {
        if (!File.Exists(EmbeddingCachePath)) return [];
        try
        {
            string json = await File.ReadAllTextAsync(EmbeddingCachePath);
            using var doc = JsonDocument.Parse(json);
            var cache = new Dictionary<string, float[]>();
            foreach (var prop in doc.RootElement.EnumerateObject())
            {
                var vector = prop.Value.EnumerateArray().Select(v => v.GetSingle()).ToArray();
                cache[prop.Name] = vector;
            }
            return cache;
        }
        catch (Exception ex)
        {
            Console.WriteLine($"[EMBEDDING] Warning: could not load cache ({ex.Message}). Starting fresh.");
            return [];
        }
    }

    private static async Task SaveEmbeddingCacheAsync(Dictionary<string, float[]> cache)
    {
        try
        {
            var node = new JsonObject();
            foreach (var (text, vector) in cache)
            {
                var arr = new JsonArray();
                foreach (var v in vector) arr.Add(v);
                node[text] = arr;
            }
            string tempPath = EmbeddingCachePath + ".tmp";
            await File.WriteAllTextAsync(tempPath, node.ToJsonString());
            File.Move(tempPath, EmbeddingCachePath, overwrite: true);
        }
        catch (Exception ex)
        {
            Console.WriteLine($"[EMBEDDING] Warning: could not save cache ({ex.Message}).");
        }
    }

    /// <summary>
    /// Pass 2: Evaluate each batch with the AI Judge and immediately save
    /// any matches to disk. This way Ctrl+C never loses already-confirmed pairs.
    /// </summary>
    private async Task SemanticMatchAndSave(List<CandidatePair> candidates, string outputPath)
    {
        const int BatchSize = 25; // Maximise pairs per request — top models have only 20 RPD
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

            // Proactive safety delay between batches. Conservative enough for the
            // most restrictive free-tier model (~5 RPM). The reactive 65s wait inside
            // EvaluateBatch handles any RPM hits that still slip through.
            if (i + BatchSize < candidates.Count)
                await Task.Delay(13000);
        }

        Console.WriteLine($"[PAIRING SERVICE] Done — {totalMatched} total pair(s) confirmed across {totalBatches} batch(es).");
    }

    /// <summary>
    /// Sends a batch of candidate pairs to the best available judge model and returns
    /// the 0-based indices of confirmed matches. Uses a waterfall: on RPD quota exhaustion
    /// the current model is dropped and the same batch is retried with the next one.
    /// RPM quota hits wait 65s and retry on the same model.
    /// </summary>
    private async Task<List<int>> EvaluateBatch(List<CandidatePair> batch)
    {
        // ── Structured step-by-step prompt ────────────────────────────────────────
        // Written so even a lite model can follow it reliably — no reading between lines.
        var sb = new StringBuilder();
        sb.AppendLine("You are a market matching engine. Identify EXACT matches between prediction markets.");
        sb.AppendLine("For each numbered pair apply these 4 checks IN ORDER. REJECT the pair the moment any check fails.");
        sb.AppendLine("If you are unsure about any check, REJECT.");
        sb.AppendLine();
        sb.AppendLine("STEP 1 — EXPIRY DATE: Do both markets close within 7 days of each other? If NO → REJECT.");
        sb.AppendLine("STEP 2 — SUBJECT: Are both markets about the exact same team, player, or named event? If NO → REJECT.");
        sb.AppendLine("STEP 3 — OUTCOME: Do both markets resolve YES/NO on the exact same question? If NO → REJECT.");
        sb.AppendLine("STEP 4 — EDGE CASES: Check for any difference in these resolution conditions. If any difference exists → REJECT.");
        sb.AppendLine("  - Overtime: does one market count overtime and the other not?");
        sb.AppendLine("  - Ties: does one market refund on a tie and the other not?");
        sb.AppendLine("  - Cancellation: do the markets handle postponements or cancellations differently?");
        sb.AppendLine();
        sb.AppendLine("A pair is a MATCH only if it passes ALL 4 steps.");
        sb.AppendLine("Reply ONLY with a JSON array of 0-based indices of MATCHING pairs. Examples: [0,2,5] or []");
        sb.AppendLine("Do not write anything else.");
        sb.AppendLine();

        for (int i = 0; i < batch.Count; i++)
        {
            var c = batch[i];
            sb.AppendLine($"[{i}]");
            sb.AppendLine($"KALSHI  | Title: {c.KalshiTitle}");
            sb.AppendLine($"        | Close: {(c.KalshiCloseDate.HasValue ? c.KalshiCloseDate.Value.ToString("O") : "Unknown")}");
            sb.AppendLine($"        | Rules: {c.KalshiRules}");
            sb.AppendLine($"POLY    | Title: {c.PolyQuestion}");
            sb.AppendLine($"        | Close: {(c.PolyCloseDate.HasValue ? c.PolyCloseDate.Value.ToString("O") : "Unknown")}");
            sb.AppendLine($"        | Desc:  {c.PolyDescription}");
            sb.AppendLine();
        }

        var payload = new
        {
            contents        = new[] { new { parts = new[] { new { text = sb.ToString() } } } },
            generationConfig = new { temperature = 0.0, maxOutputTokens = 200, responseMimeType = "application/json" }
        };
        string body = JsonSerializer.Serialize(payload);

        // ── Waterfall loop ─────────────────────────────────────────────────────────
        while (_judgeModels.Count > 0)
        {
            string model = _judgeModels[0];
            string url   = $"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={_geminiApiKey.Trim()}";
            var    httpContent = new StringContent(body, Encoding.UTF8, "application/json");

            try
            {
                var response = await _httpClient.PostAsync(url, httpContent);

                if (!response.IsSuccessStatusCode)
                {
                    string errDetails = await response.Content.ReadAsStringAsync();

                    if ((int)response.StatusCode == 429)
                    {
                        // Daily quota (RPD) → drop this model and fall through to the next.
                        if (errDetails.Contains("day", StringComparison.OrdinalIgnoreCase) ||
                            errDetails.Contains("PerDay", StringComparison.OrdinalIgnoreCase))
                        {
                            Console.WriteLine($"\n[JUDGE] {model} daily quota exhausted — falling back to next model.");
                            _judgeModels.RemoveAt(0);
                            continue; // retry same batch with next model
                        }

                        // Per-minute quota (RPM) → wait and retry on the same model.
                        Console.WriteLine($"\n[JUDGE] {model} per-minute quota hit — waiting 65s...");
                        await Task.Delay(65000);
                        continue;
                    }

                    // Model not found or not available — drop it and try the next one.
                    if ((int)response.StatusCode == 404)
                    {
                        Console.WriteLine($"\n[JUDGE] {model} not found (404) — dropping from waterfall.");
                        _judgeModels.RemoveAt(0);
                        continue;
                    }

                    Console.WriteLine($"\n[JUDGE] {model} error: {response.StatusCode} — {errDetails}");
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

                // Strip potential markdown code blocks (e.g. ```json ... ```)
                if (text.StartsWith("```", StringComparison.OrdinalIgnoreCase))
                {
                    int firstNewline = text.IndexOf('\n');
                    if (firstNewline != -1) text = text[(firstNewline + 1)..];
                    if (text.EndsWith("```")) text = text[..^3];
                    text = text.Trim();
                }

                using var arrDoc = JsonDocument.Parse(text);
                if (arrDoc.RootElement.ValueKind != JsonValueKind.Array) return [];

                return [..arrDoc.RootElement
                    .EnumerateArray()
                    .Where(el => el.ValueKind == JsonValueKind.Number && el.TryGetInt32(out _))
                    .Select(el => el.GetInt32())
                    .Where(idx => idx >= 0 && idx < batch.Count)
                    .Distinct()];
            }
            catch (Exception ex)
            {
                Console.WriteLine($"\n[JUDGE] {model} exception: {ex.Message}");
                return [];
            }
        }

        Console.WriteLine("\n[JUDGE] All models exhausted — skipping remaining batches.");
        return [];
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