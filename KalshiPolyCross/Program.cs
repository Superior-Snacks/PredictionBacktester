using System.Collections.Concurrent;
using System.Net.WebSockets;
using System.Text;
using System.Text.Json;
using System.Text.RegularExpressions;
using KalshiPolyCross;
using PredictionBacktester.Engine;
using PredictionBacktester.Engine.LiveExecution;

// ══════════════════════════════════════════════════════════════════════════════
//  CONFIGURATION
// ══════════════════════════════════════════════════════════════════════════════
const string? KALSHI_CATEGORY_FILTER = "Sports"; // Set to null or empty to include all categories
const string? POLY_CATEGORY_FILTER   = "Sports"; // Set to null or empty to include all categories

const decimal ARB_THRESHOLD         = 0.995m;
const decimal DEPTH_FLOOR           = 1m;
const decimal MIN_BOOK_PRICE        = 0.03m;
const int     KALSHI_BATCH_SIZE     = 100;
const int     POLY_BATCH_SIZE       = 200;
const int     POLY_PING_INTERVAL_MS = 9_000;
const int     NEAR_MISS_INTERVAL_MS = 60_000;
const string  POLY_GAMMA_URL        = "https://gamma-api.polymarket.com";
const string  POLY_WS_URL           = "wss://ws-subscriptions-clob.polymarket.com/ws/market";

// Minimum number of key words from a Kalshi title that must appear in a
// Polymarket question for the pair to be treated as a candidate match.
// Raise this to reduce false positives; lower it to catch more candidates.
const int MIN_MATCH_WORDS = 2;

// ══════════════════════════════════════════════════════════════════════════════
//  STARTUP
// ══════════════════════════════════════════════════════════════════════════════
Console.WriteLine("═══════════════════════════════════════════════════════════");
Console.WriteLine("  KALSHI ↔ POLYMARKET CROSS-PLATFORM ARB TELEMETRY");
Console.WriteLine("═══════════════════════════════════════════════════════════");

// ── Kalshi auth ───────────────────────────────────────────────────────────────
var kalshiConfig = KalshiApiConfig.FromEnvironment();
if (string.IsNullOrEmpty(kalshiConfig.ApiKeyId) || string.IsNullOrEmpty(kalshiConfig.PrivateKeyPath))
{
    Console.WriteLine("[ERROR] Set KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH environment variables.");
    return;
}

using var orderClient = new KalshiOrderClient(kalshiConfig);
try
{
    long bal = await orderClient.GetBalanceCentsAsync();
    Console.WriteLine($"[KALSHI AUTH OK] Balance: ${bal / 100.0:0.00}");
}
catch (Exception ex)
{
    Console.WriteLine($"[KALSHI AUTH FAIL] {ex.Message}");
    return;
}

// ── Load optional manual pairs (explicit verified matches) ────────────────────
// cross_pairs.json is optional. If present, each entry is a verified pair:
//   { "kalshi_ticker": "KXFOO", "poly_yes_token": "abc...", "poly_no_token": "def...", "label": "..." }
// These are merged with auto-discovered pairs and always included regardless of score.
var manualPairs = new List<CrossPair>();
string manualPath = Path.Combine(AppContext.BaseDirectory, "cross_pairs.json");
if (!File.Exists(manualPath)) manualPath = "cross_pairs.json";
if (File.Exists(manualPath))
{
    try
    {
        using var manDoc = JsonDocument.Parse(File.ReadAllText(manualPath));
        foreach (var el in manDoc.RootElement.EnumerateArray())
        {
            string kTicker  = el.TryGetProperty("kalshi_ticker",  out var kt) ? (kt.GetString()  ?? "") : "";
            string yesToken = el.TryGetProperty("poly_yes_token", out var yt) ? (yt.GetString()  ?? "") : "";
            string noToken  = el.TryGetProperty("poly_no_token",  out var nt) ? (nt.GetString()  ?? "") : "";
            string label    = el.TryGetProperty("label",          out var lb) ? (lb.GetString()  ?? "") : kTicker;
            if (!string.IsNullOrEmpty(kTicker) && !string.IsNullOrEmpty(yesToken) && !string.IsNullOrEmpty(noToken))
            {
                string pairId = $"MANUAL_{kTicker}__{yesToken[..Math.Min(8, yesToken.Length)]}";
                manualPairs.Add(new CrossPair(pairId, label, kTicker, yesToken, noToken));
            }
        }
        Console.WriteLine($"[CONFIG] {manualPairs.Count} manual pair(s) loaded from cross_pairs.json");
    }
    catch (Exception ex)
    {
        Console.WriteLine($"[CONFIG WARN] Could not parse cross_pairs.json: {ex.Message}");
    }
}

// ── Scan Kalshi binary markets ────────────────────────────────────────────────
// Simple direct scan: fetch all open markets from the REST API.
// No events endpoint, no blocklist, no volume cap — we want every binary market.
Console.WriteLine("\n[KALSHI SCANNER] Fetching all open markets...");
var kalshiTickers    = new List<string>();
var kalshiTokenNames = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase); // ticker → yes_sub_title, ticker_NO → no_sub_title
var kalshiTitles     = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase); // ticker → full market title
var kalshiCategories = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase); // ticker → category

{
    using var scanHttp = new HttpClient();
    string baseRest = kalshiConfig.BaseRestUrl.TrimEnd('/');   // e.g. https://api.elections.kalshi.com/trade-api/v2
    string cursor = "";

    while (true)
    {
        string relPath = cursor == ""
            ? "/markets?status=open&limit=1000"
            : $"/markets?status=open&limit=1000&cursor={Uri.EscapeDataString(cursor)}";

        var (key, ts, sig) = orderClient.CreateAuthHeaders("GET", relPath);
        using var req = new HttpRequestMessage(HttpMethod.Get, baseRest + relPath);
        req.Headers.Add("KALSHI-ACCESS-KEY", key);
        req.Headers.Add("KALSHI-ACCESS-TIMESTAMP", ts);
        req.Headers.Add("KALSHI-ACCESS-SIGNATURE", sig);
        req.Headers.Accept.Add(new System.Net.Http.Headers.MediaTypeWithQualityHeaderValue("application/json"));

        using var resp = await scanHttp.SendAsync(req);
        resp.EnsureSuccessStatusCode();
        using var scanDoc = JsonDocument.Parse(await resp.Content.ReadAsStreamAsync());
        var root = scanDoc.RootElement;

        if (root.TryGetProperty("markets", out var marketsEl))
        {
            foreach (var m in marketsEl.EnumerateArray())
            {
                string ticker   = m.TryGetProperty("ticker",        out var tEl)  ? (tEl.GetString()  ?? "") : "";
                string title    = m.TryGetProperty("title",         out var tiEl) ? (tiEl.GetString() ?? "") : "";
                string yesSub   = m.TryGetProperty("yes_sub_title", out var ysEl) ? (ysEl.GetString() ?? "") : "";
                string noSub    = m.TryGetProperty("no_sub_title",  out var nsEl) ? (nsEl.GetString() ?? "") : "";
                string category = m.TryGetProperty("category",      out var cEl)  ? (cEl.GetString()  ?? "") : "";
                if (string.IsNullOrEmpty(ticker)) continue;
                kalshiTickers.Add(ticker);
                if (!string.IsNullOrEmpty(title))    kalshiTitles[ticker]             = title;
                if (!string.IsNullOrEmpty(yesSub))   kalshiTokenNames[ticker]         = yesSub;
                if (!string.IsNullOrEmpty(noSub))    kalshiTokenNames[ticker + "_NO"] = noSub;
                if (!string.IsNullOrEmpty(category)) kalshiCategories[ticker]         = category;
            }
        }

        cursor = root.TryGetProperty("cursor", out var curEl) ? (curEl.GetString() ?? "") : "";
        if (string.IsNullOrEmpty(cursor)) break;
        await Task.Delay(150);
    }
}
int kalshiTotal = kalshiTickers.Count;
if (!string.IsNullOrEmpty(KALSHI_CATEGORY_FILTER))
{
    kalshiTickers = kalshiTickers
        .Where(t => kalshiCategories.GetValueOrDefault(t, "").Equals(KALSHI_CATEGORY_FILTER, StringComparison.OrdinalIgnoreCase))
        .ToList();
    Console.WriteLine($"[KALSHI SCANNER] {kalshiTotal} open markets fetched → {kalshiTickers.Count} markets filtered by category '{KALSHI_CATEGORY_FILTER}'");
}
else
{
    Console.WriteLine($"[KALSHI SCANNER] {kalshiTotal} open markets fetched (no category filter)");
}

// ── Fetch Polymarket active markets ───────────────────────────────────────────
Console.WriteLine("[POLY SCANNER] Fetching active Polymarket markets...");

// List of (question, yesTokenId, noTokenId)
List<(string Question, string YesToken, string NoToken)> polyMarkets = [];
using var httpClient = new HttpClient();
httpClient.DefaultRequestHeaders.Add("User-Agent", "KalshiPolyCross/1.0");

try
{
    int offset = 0;
    const int pageSize = 500;
    while (true)
    {
        string url = $"{POLY_GAMMA_URL}/markets?active=true&closed=false&limit={pageSize}&offset={offset}";
        string json = await httpClient.GetStringAsync(url);
        using var doc = JsonDocument.Parse(json);
        var arr = doc.RootElement;
        if (arr.ValueKind != JsonValueKind.Array) break;
        int count = 0;
        foreach (var mkt in arr.EnumerateArray())
        {
            count++;
            string question = mkt.TryGetProperty("question", out var qEl) ? (qEl.GetString() ?? "") : "";

            // Parse clobTokenIds — can be JSON array or JSON-encoded string
            List<string> tokens = new();
            if (mkt.TryGetProperty("clobTokenIds", out var tokEl))
            {
                if (tokEl.ValueKind == JsonValueKind.Array)
                    tokens = tokEl.EnumerateArray().Select(x => x.GetString() ?? "").Where(s => s.Length > 0).ToList();
                else if (tokEl.ValueKind == JsonValueKind.String)
                {
                    try { tokens = JsonSerializer.Deserialize<List<string>>(tokEl.GetString()!) ?? new(); }
                    catch { }
                }
            }

            // Category filter: keep only markets matching the configured category
            bool includeMarket = string.IsNullOrEmpty(POLY_CATEGORY_FILTER);
            if (!includeMarket && POLY_CATEGORY_FILTER != null)
            {
                if (mkt.TryGetProperty("tags", out var tagsEl) && tagsEl.ValueKind == JsonValueKind.Array)
                    includeMarket = tagsEl.EnumerateArray().Any(t =>
                        (t.TryGetProperty("slug",  out var sl) && (sl.GetString()  ?? "").Contains(POLY_CATEGORY_FILTER, StringComparison.OrdinalIgnoreCase)) ||
                        (t.TryGetProperty("label", out var ll) && (ll.GetString()  ?? "").Contains(POLY_CATEGORY_FILTER, StringComparison.OrdinalIgnoreCase)));
                // Also accept if a top-level category field matches
                if (!includeMarket && mkt.TryGetProperty("category", out var pcEl))
                    includeMarket = (pcEl.GetString() ?? "").Contains(POLY_CATEGORY_FILTER, StringComparison.OrdinalIgnoreCase);
            }

            if (includeMarket && tokens.Count >= 2 && !string.IsNullOrEmpty(question))
                polyMarkets.Add((question, tokens[0], tokens[1]));
        }
        if (count < pageSize) break;
        offset += pageSize;
        await Task.Delay(200);
    }
    if (!string.IsNullOrEmpty(POLY_CATEGORY_FILTER))
    {
        Console.WriteLine($"[POLY SCANNER] {polyMarkets.Count} active markets fetched (filtered by category '{POLY_CATEGORY_FILTER}')");
    }
    else
    {
        Console.WriteLine($"[POLY SCANNER] {polyMarkets.Count} active markets fetched (no category filter)");
    }
}
catch (Exception ex)
{
    Console.WriteLine($"[POLY SCANNER ERROR] {ex.Message}");
}

// ── Auto-match: Pass 1 — direct title similarity ─────────────────────────────
// Compare each Kalshi market's full title (question text) word-for-word against
// every Polymarket question. This catches markets titled identically or near-
// identically across platforms before falling back to YES/NO subtitle keywords.
//
// Match rule: ≥80% of the Kalshi title's words appear in the Polymarket question
// (after stripping stop words). This is stricter than keyword matching and will
// produce very high-confidence pairs.
Console.WriteLine($"\n[MATCHING] Pass 1 — direct title similarity ({kalshiTickers.Count} Kalshi × {polyMarkets.Count} Poly)...\n");

var autoPairs   = new List<CrossPair>();
var perfectSave = new List<(string Ticker, string Label, string YesToken, string NoToken)>(); // 100% unambiguous matches → auto-save
var alreadyPaired = new HashSet<string>(manualPairs.Select(p => p.KalshiTicker), StringComparer.OrdinalIgnoreCase);
int pass1Count = 0;

foreach (var ticker in kalshiTickers)
{
    if (alreadyPaired.Contains(ticker)) continue;
    string kTitle = kalshiTitles.GetValueOrDefault(ticker, "");
    if (string.IsNullOrWhiteSpace(kTitle)) continue;

    var kWords = TitleToKeyWords(kTitle);
    if (kWords.Count < MIN_MATCH_WORDS) continue;

    int minRequired = (int)Math.Ceiling(kWords.Count * 0.8);

    var scored = polyMarkets
        .Select(m => (Market: m, Score: kWords.Count(w => m.Question.Contains(w, StringComparison.OrdinalIgnoreCase))))
        .Where(x => x.Score >= minRequired)
        .OrderByDescending(x => x.Score)
        .ToList();

    if (scored.Count == 0) continue;

    // A "perfect" match: every Kalshi keyword found in the Poly question, and only
    // one Poly market qualifies (unambiguous). These are auto-saved to cross_pairs.json.
    var perfect = scored.Where(x => x.Score == kWords.Count).ToList();
    bool autosave = perfect.Count == 1;

    Console.WriteLine($"  [TITLE MATCH{(autosave ? " ★" : "")}] K:{ticker}");
    Console.WriteLine($"    Kalshi: \"{kTitle}\"");
    foreach (var (market, score) in scored)
    {
        string pairId = $"TITLE_{ticker}__{market.YesToken[..Math.Min(8, market.YesToken.Length)]}";
        string label  = kTitle;
        autoPairs.Add(new CrossPair(pairId, label, ticker, market.YesToken, market.NoToken));
        alreadyPaired.Add(ticker);
        string saveTag = (autosave && score == kWords.Count) ? " [AUTO-SAVE]" : "";
        Console.WriteLine($"    [{score}/{kWords.Count}]{saveTag} \"{market.Question}\"");
        Console.WriteLine($"          YES={market.YesToken[..Math.Min(16, market.YesToken.Length)]}...");
        pass1Count++;
    }
    if (autosave)
        perfectSave.Add((ticker, kTitle, perfect[0].Market.YesToken, perfect[0].Market.NoToken));
    Console.WriteLine();
}
Console.WriteLine($"[MATCHING] Pass 1 complete: {pass1Count} candidate pair(s) from direct title match\n");

// ── Auto-match: Pass 2 — YES/NO subtitle keywords ────────────────────────────
// For tickers not matched in pass 1, combine the YES and NO outcome subtitles
// and score Polymarket questions by keyword overlap. Useful when the Kalshi full
// title uses different phrasing but the outcome labels (team names, candidate
// names) are the same words that appear in the Polymarket question.
Console.WriteLine($"[MATCHING] Pass 2 — YES/NO subtitle keywords (remaining tickers)...\n");
int pass2Count = 0;

foreach (var ticker in kalshiTickers)
{
    if (alreadyPaired.Contains(ticker)) continue;

    string yesTitle = kalshiTokenNames.GetValueOrDefault(ticker, "");
    string noTitle  = kalshiTokenNames.GetValueOrDefault(ticker + "_NO", "");

    // If YES and NO titles are identical (or one contains the other), the outcome
    // names don't describe the event — just the team/candidate. We can't distinguish
    // "OKC wins championship" vs "OKC advances to conference finals" etc.
    string yesNorm = yesTitle.Trim().ToLowerInvariant();
    string noNorm  = noTitle.Trim().ToLowerInvariant();
    if (yesNorm == noNorm || yesNorm.Contains(noNorm) || noNorm.Contains(yesNorm))
        continue;  // identical subtitles — skip silently (too ambiguous)

    string combined = $"{yesTitle} {noTitle}";
    var keywords = TitleToKeyWords(combined);
    if (keywords.Count < MIN_MATCH_WORDS) continue;

    int minRequired = Math.Max(MIN_MATCH_WORDS, (int)Math.Ceiling(keywords.Count * 0.8));

    var candidates = polyMarkets
        .Select(m => (Market: m, Score: keywords.Count(kw => m.Question.Contains(kw, StringComparison.OrdinalIgnoreCase))))
        .Where(x => x.Score >= minRequired)
        .OrderByDescending(x => x.Score)
        .ToList();

    if (candidates.Count == 0) continue;

    Console.WriteLine($"  [KEYWORD MATCH] K:{ticker}");
    Console.WriteLine($"    YES: \"{yesTitle}\"  NO: \"{noTitle}\"");
    Console.WriteLine($"    Keywords: [{string.Join(", ", keywords)}]");

    foreach (var (market, score) in candidates)
    {
        string pairId = $"AUTO_{ticker}__{market.YesToken[..Math.Min(8, market.YesToken.Length)]}";
        string label  = $"{yesTitle} | K:{ticker}";
        autoPairs.Add(new CrossPair(pairId, label, ticker, market.YesToken, market.NoToken));
        Console.WriteLine($"    [{score}/{keywords.Count} need≥{minRequired}] \"{market.Question}\"");
        Console.WriteLine($"          YES={market.YesToken[..Math.Min(16, market.YesToken.Length)]}...");
        pass2Count++;
    }
    Console.WriteLine();
}

// Merge: manual pairs take precedence; auto pairs skip tickers already in manual list
var pairs = new List<CrossPair>(manualPairs);
foreach (var ap in autoPairs)
    if (!alreadyPaired.Contains(ap.KalshiTicker))
        pairs.Add(ap);

Console.WriteLine($"[MATCHING] Pass 2 complete: {pass2Count} candidate pair(s) from subtitle keywords");
Console.WriteLine($"[MATCHING] Total: {pass1Count} title + {pass2Count} keyword + {manualPairs.Count} manual = {pairs.Count} pairs");
if (pairs.Count == 0)
    Console.WriteLine("[WARN] No pairs found. Add entries to cross_pairs.json or wait for more Kalshi/Poly market overlap.");

// ── Auto-save perfect matches to cross_pairs.json ─────────────────────────────
// Only saves matches where every Kalshi title keyword was found in exactly one
// Polymarket question — unambiguous enough to skip manual review.
if (perfectSave.Count > 0)
{
    try
    {
        // Load existing file to avoid duplicates
        var existingJson = File.Exists(manualPath) ? File.ReadAllText(manualPath).Trim() : "[]";
        using var existDoc = JsonDocument.Parse(existingJson);
        var existingTickers = existDoc.RootElement.EnumerateArray()
            .Select(el => el.TryGetProperty("kalshi_ticker", out var kt) ? (kt.GetString() ?? "") : "")
            .ToHashSet(StringComparer.OrdinalIgnoreCase);

        var newEntries = perfectSave.Where(p => !existingTickers.Contains(p.Ticker)).ToList();
        if (newEntries.Count > 0)
        {
            // Append new entries to the existing array
            var allEntries = existDoc.RootElement.EnumerateArray()
                .Select(el => el.GetRawText())
                .ToList();
            foreach (var (ticker, label, yesToken, noToken) in newEntries)
                allEntries.Add(JsonSerializer.Serialize(new
                {
                    kalshi_ticker  = ticker,
                    poly_yes_token = yesToken,
                    poly_no_token  = noToken,
                    label          = label
                }));

            File.WriteAllText(manualPath, "[\n  " + string.Join(",\n  ", allEntries) + "\n]");
            Console.WriteLine($"[AUTO-SAVE] {newEntries.Count} perfect match(es) written to cross_pairs.json");
            foreach (var (ticker, label, _, _) in newEntries)
                Console.WriteLine($"  + {ticker}: \"{label}\"");
        }
        else
        {
            Console.WriteLine($"[AUTO-SAVE] {perfectSave.Count} perfect match(es) already in cross_pairs.json — no update needed");
        }
    }
    catch (Exception ex)
    {
        Console.WriteLine($"[AUTO-SAVE WARN] Could not update cross_pairs.json: {ex.Message}");
    }
}

// ── Build shared order books ──────────────────────────────────────────────────
var books = new ConcurrentDictionary<string, LocalOrderBook>(StringComparer.Ordinal);

// Kalshi size maps (required by ApplySnapshot / ApplyDelta)
var yesSizes = new ConcurrentDictionary<string, Dictionary<decimal, decimal>>();
var noSizes  = new ConcurrentDictionary<string, Dictionary<decimal, decimal>>();

// Collect unique Kalshi tickers and Poly tokens from matched pairs
var kalshiSubscribeTickers = pairs.Select(p => p.KalshiTicker).Distinct().ToList();
var polySubscribeTokens    = pairs.SelectMany(p => new[] { p.PolyYesTokenId, p.PolyNoTokenId }).Distinct().ToList();

foreach (var ticker in kalshiSubscribeTickers)
{
    books[$"K:{ticker}"]    = new LocalOrderBook($"K:{ticker}");
    books[$"K:{ticker}_NO"] = new LocalOrderBook($"K:{ticker}_NO");
    yesSizes[ticker] = new Dictionary<decimal, decimal>();
    noSizes[ticker]  = new Dictionary<decimal, decimal>();
}
foreach (var token in polySubscribeTokens)
    books[$"P:{token}"] = new LocalOrderBook($"P:{token}");

// ── Telemetry strategy ────────────────────────────────────────────────────────
var telemetry = new CrossPlatformArbTelemetryStrategy(pairs, books, ARB_THRESHOLD, DEPTH_FLOOR);

Console.WriteLine($"\n[BOOKS] {books.Count} order books created");
Console.WriteLine($"  Kalshi tickers : {kalshiSubscribeTickers.Count}");
Console.WriteLine($"  Poly tokens    : {polySubscribeTokens.Count}");

var cts = new CancellationTokenSource();
Console.CancelKeyPress += (_, e) => { e.Cancel = true; cts.Cancel(); };

// ══════════════════════════════════════════════════════════════════════════════
//  NEAR-MISS REPORT TASK
// ══════════════════════════════════════════════════════════════════════════════
_ = Task.Run(async () =>
{
    while (!cts.Token.IsCancellationRequested)
    {
        await Task.Delay(NEAR_MISS_INTERVAL_MS, cts.Token).ContinueWith(_ => { });
        if (cts.Token.IsCancellationRequested) break;

        int kalshiReady = books.Count(kv => kv.Key.StartsWith("K:") && kv.Value.HasReceivedDelta);
        int polyReady   = books.Count(kv => kv.Key.StartsWith("P:") && kv.Value.HasReceivedDelta);
        int kalshiTotal = books.Count(kv => kv.Key.StartsWith("K:"));
        int polyTotal   = books.Count(kv => kv.Key.StartsWith("P:"));

        Console.WriteLine($"\n[TELEMETRY] --- TOP {Math.Min(10, pairs.Count)} CLOSEST TO CROSS-PLATFORM ARB ---");
        Console.WriteLine($"  Kalshi books: {kalshiReady}/{kalshiTotal} | Poly books: {polyReady}/{polyTotal} | Pairs: {telemetry.TotalPairs} | Open arbs: {telemetry.OpenArbs}");

        foreach (var (cost, label, pairId, arbType, depth) in telemetry.GetNearMissSnapshot().Take(10))
        {
            decimal diff = cost - 1.00m;
            string tag = cost < 1.00m ? "ARB!" : $"+${diff:0.0000} away";
            Console.WriteLine($"  ${cost:0.0000} ({tag}) {arbType} | depth={depth:0.0} | {label}");
        }

        if (!telemetry.GetNearMissSnapshot().Any())
            Console.WriteLine("  (no books priced yet — waiting for WS data)");
    }
});

// ══════════════════════════════════════════════════════════════════════════════
//  KALSHI WEBSOCKET TASK
// ══════════════════════════════════════════════════════════════════════════════
var kalshiWsTask = Task.Run(async () =>
{
    while (!cts.Token.IsCancellationRequested)
    {
        try
        {
            using var ws = new ClientWebSocket();
            var (key, ts, sig) = orderClient.CreateAuthHeaders("GET", "/trade-api/ws/v2");
            ws.Options.SetRequestHeader("KALSHI-ACCESS-KEY", key);
            ws.Options.SetRequestHeader("KALSHI-ACCESS-TIMESTAMP", ts);
            ws.Options.SetRequestHeader("KALSHI-ACCESS-SIGNATURE", sig);

            await ws.ConnectAsync(new Uri(kalshiConfig.BaseWsUrl), cts.Token);
            Console.WriteLine($"[KALSHI WS] Connected to {kalshiConfig.BaseWsUrl}");

            int msgId = 1;
            for (int i = 0; i < kalshiSubscribeTickers.Count; i += KALSHI_BATCH_SIZE)
            {
                var batch = kalshiSubscribeTickers.Skip(i).Take(KALSHI_BATCH_SIZE);
                string tickerArray = string.Join(",", batch.Select(t => $"\"{t}\""));
                string subMsg = $"{{\"id\":{msgId++},\"cmd\":\"subscribe\",\"params\":{{\"channels\":[\"orderbook_delta\"],\"market_tickers\":[{tickerArray}]}}}}";
                await ws.SendAsync(Encoding.UTF8.GetBytes(subMsg), WebSocketMessageType.Text, true, cts.Token);
                await Task.Delay(100, cts.Token);
            }
            Console.WriteLine($"[KALSHI WS] Subscribed to {kalshiSubscribeTickers.Count} tickers");

            // Clear books on reconnect
            foreach (var ticker in kalshiSubscribeTickers)
            {
                books[$"K:{ticker}"].ClearBook();
                books[$"K:{ticker}_NO"].ClearBook();
                yesSizes[ticker].Clear();
                noSizes[ticker].Clear();
            }
            telemetry.OnReconnect();

            var buf = new byte[65536];
            using var ms = new MemoryStream();

            while (!cts.Token.IsCancellationRequested && ws.State == WebSocketState.Open)
            {
                ms.SetLength(0);
                WebSocketReceiveResult result;
                do
                {
                    result = await ws.ReceiveAsync(new ArraySegment<byte>(buf), cts.Token);
                    if (result.MessageType == WebSocketMessageType.Close)
                    {
                        await ws.CloseAsync(WebSocketCloseStatus.NormalClosure, "", CancellationToken.None);
                        goto kalshiReconnect;
                    }
                    ms.Write(buf, 0, result.Count);
                } while (!result.EndOfMessage);

                if (ms.Length == 0) continue;
                string message = Encoding.UTF8.GetString(ms.GetBuffer(), 0, (int)ms.Length);
                if (message is "heartbeat" or "PONG" or "pong") continue;

                ProcessKalshiMessage(message, books, yesSizes, noSizes, telemetry);
            }
            kalshiReconnect:;
        }
        catch (OperationCanceledException) { break; }
        catch (Exception ex)
        {
            Console.WriteLine($"[KALSHI WS ERROR] {ex.Message} — reconnecting in 5s...");
        }
        if (!cts.Token.IsCancellationRequested)
            await Task.Delay(5_000, cts.Token).ContinueWith(_ => { });
    }
});

// ══════════════════════════════════════════════════════════════════════════════
//  POLYMARKET WEBSOCKET TASK
// ══════════════════════════════════════════════════════════════════════════════
var polyWsTask = Task.Run(async () =>
{
    while (!cts.Token.IsCancellationRequested)
    {
        try
        {
            using var ws = new ClientWebSocket();
            await ws.ConnectAsync(new Uri(POLY_WS_URL), cts.Token);
            Console.WriteLine($"[POLY WS] Connected to {POLY_WS_URL}");

            // Ping task (Polymarket drops connection without PING every ~10s)
            var pingSrc = CancellationTokenSource.CreateLinkedTokenSource(cts.Token);
            _ = Task.Run(async () =>
            {
                var pingBytes = Encoding.UTF8.GetBytes("PING");
                while (!pingSrc.Token.IsCancellationRequested && ws.State == WebSocketState.Open)
                {
                    try
                    {
                        await Task.Delay(POLY_PING_INTERVAL_MS, pingSrc.Token);
                        await ws.SendAsync(new ArraySegment<byte>(pingBytes), WebSocketMessageType.Text, true, pingSrc.Token);
                    }
                    catch { break; }
                }
            });

            // Subscribe to all YES and NO tokens in batches
            bool isFirst = true;
            for (int i = 0; i < polySubscribeTokens.Count; i += POLY_BATCH_SIZE)
            {
                var batch = polySubscribeTokens.Skip(i).Take(POLY_BATCH_SIZE);
                string assetList = string.Join("\",\"", batch);
                string subMsg = isFirst
                    ? $"{{\"assets_ids\":[\"{assetList}\"],\"type\":\"market\"}}"
                    : $"{{\"assets_ids\":[\"{assetList}\"],\"operation\":\"subscribe\"}}";
                isFirst = false;
                await ws.SendAsync(Encoding.UTF8.GetBytes(subMsg), WebSocketMessageType.Text, true, cts.Token);
                await Task.Delay(100, cts.Token);
            }
            Console.WriteLine($"[POLY WS] Subscribed to {polySubscribeTokens.Count} tokens");

            var buf = new byte[65536];
            using var ms = new MemoryStream();

            while (!cts.Token.IsCancellationRequested && ws.State == WebSocketState.Open)
            {
                ms.SetLength(0);
                WebSocketReceiveResult result;
                do
                {
                    result = await ws.ReceiveAsync(new ArraySegment<byte>(buf), cts.Token);
                    if (result.MessageType == WebSocketMessageType.Close)
                    {
                        await ws.CloseAsync(WebSocketCloseStatus.NormalClosure, "", CancellationToken.None);
                        goto polyReconnect;
                    }
                    ms.Write(buf, 0, result.Count);
                } while (!result.EndOfMessage);

                if (ms.Length == 0) continue;
                string message = Encoding.UTF8.GetString(ms.GetBuffer(), 0, (int)ms.Length);
                if (message is "PONG" or "pong") continue;

                ProcessPolyMessage(message, books, telemetry);
            }
            polyReconnect:;
            pingSrc.Cancel();
        }
        catch (OperationCanceledException) { break; }
        catch (Exception ex)
        {
            Console.WriteLine($"[POLY WS ERROR] {ex.Message} — reconnecting in 5s...");
        }
        if (!cts.Token.IsCancellationRequested)
            await Task.Delay(5_000, cts.Token).ContinueWith(_ => { });
    }
});

await Task.WhenAll(kalshiWsTask, polyWsTask);
Console.WriteLine("\n[SHUTDOWN] Cross-platform arb telemetry stopped.");

// ══════════════════════════════════════════════════════════════════════════════
//  KALSHI MESSAGE PROCESSING
// ══════════════════════════════════════════════════════════════════════════════
static void ProcessKalshiMessage(
    string message,
    ConcurrentDictionary<string, LocalOrderBook> books,
    ConcurrentDictionary<string, Dictionary<decimal, decimal>> yesSizes,
    ConcurrentDictionary<string, Dictionary<decimal, decimal>> noSizes,
    CrossPlatformArbTelemetryStrategy telemetry)
{
    try
    {
        using var doc = JsonDocument.Parse(message);
        var root = doc.RootElement;

        if (!root.TryGetProperty("type", out var typeEl)) return;
        string msgType = typeEl.GetString() ?? "";
        if (!root.TryGetProperty("msg", out var msgEl)) return;
        if (!msgEl.TryGetProperty("market_ticker", out var tickerEl)) return;
        string ticker = tickerEl.GetString() ?? "";

        if (!books.TryGetValue($"K:{ticker}", out var yesBook)) return;
        books.TryGetValue($"K:{ticker}_NO", out var noBook);

        if (!yesSizes.TryGetValue(ticker, out var ySizeMap) ||
            !noSizes.TryGetValue(ticker,  out var nSizeMap)) return;

        if (msgType == "orderbook_snapshot")
        {
            ApplyKalshiSnapshot(yesBook, noBook, msgEl, ySizeMap, nSizeMap);
            telemetry.OnBookUpdate($"K:{ticker}");
            if (noBook != null) telemetry.OnBookUpdate($"K:{ticker}_NO");
        }
        else if (msgType == "orderbook_delta")
        {
            bool noChanged = ApplyKalshiDelta(yesBook, noBook, msgEl, ySizeMap, nSizeMap);
            telemetry.OnBookUpdate($"K:{ticker}");
            if (noBook != null && noChanged) telemetry.OnBookUpdate($"K:{ticker}_NO");
        }
    }
    catch (JsonException) { }
}

static bool ApplyKalshiDelta(
    LocalOrderBook yesBook, LocalOrderBook? noBook,
    JsonElement msg,
    Dictionary<decimal, decimal> yesSizeMap, Dictionary<decimal, decimal> noSizeMap)
{
    if (!msg.TryGetProperty("price_dollars", out var priceEl)) return false;
    if (!msg.TryGetProperty("delta_fp",      out var deltaEl)) return false;
    if (!msg.TryGetProperty("side",          out var sideEl))  return false;

    if (!decimal.TryParse(priceEl.GetString(), System.Globalization.NumberStyles.Any,
            System.Globalization.CultureInfo.InvariantCulture, out decimal price)) return false;
    if (!decimal.TryParse(deltaEl.GetString(), System.Globalization.NumberStyles.Any,
            System.Globalization.CultureInfo.InvariantCulture, out decimal delta)) return false;
    string side = sideEl.GetString() ?? "";

    if (price < MIN_BOOK_PRICE || price > (1m - MIN_BOOK_PRICE)) return false;

    if (side == "yes")
    {
        decimal newSize = yesSizeMap.GetValueOrDefault(price, 0m) + delta;
        decimal impliedNoAsk = Math.Round(1m - price, 4);
        if (newSize <= 0) { yesSizeMap.Remove(price); yesBook.UpdatePriceLevel("BUY", price, 0m); noBook?.UpdatePriceLevel("SELL", impliedNoAsk, 0m); }
        else              { yesSizeMap[price] = newSize; yesBook.UpdatePriceLevel("BUY", price, newSize); noBook?.UpdatePriceLevel("SELL", impliedNoAsk, newSize); }
        yesBook.MarkDeltaReceived();
        return false;
    }
    if (side == "no")
    {
        decimal newSize = noSizeMap.GetValueOrDefault(price, 0m) + delta;
        decimal impliedYesAsk = Math.Round(1m - price, 4);
        if (newSize <= 0) { noSizeMap.Remove(price); noBook?.UpdatePriceLevel("BUY", price, 0m); yesBook.UpdatePriceLevel("SELL", impliedYesAsk, 0m); }
        else              { noSizeMap[price] = newSize; noBook?.UpdatePriceLevel("BUY", price, newSize); yesBook.UpdatePriceLevel("SELL", impliedYesAsk, newSize); }
        noBook?.MarkDeltaReceived();
        yesBook.MarkDeltaReceived();
        return true;
    }
    return false;
}

static void ApplyKalshiSnapshot(
    LocalOrderBook yesBook, LocalOrderBook? noBook,
    JsonElement msg,
    Dictionary<decimal, decimal> yesSizeMap, Dictionary<decimal, decimal> noSizeMap)
{
    yesBook.ClearBook();
    noBook?.ClearBook();
    yesSizeMap.Clear();
    noSizeMap.Clear();

    if (msg.TryGetProperty("yes_dollars_fp", out var yesEl) && yesEl.ValueKind == JsonValueKind.Array)
        foreach (var level in yesEl.EnumerateArray())
            if (TryParseLevel(level, out decimal price, out decimal size))
            {
                yesSizeMap[price] = size;
                yesBook.UpdatePriceLevel("BUY", price, size);
                noBook?.UpdatePriceLevel("SELL", Math.Round(1m - price, 4), size);
            }

    if (msg.TryGetProperty("no_dollars_fp", out var noEl) && noEl.ValueKind == JsonValueKind.Array)
        foreach (var level in noEl.EnumerateArray())
            if (TryParseLevel(level, out decimal noPrice, out decimal size))
            {
                noSizeMap[noPrice] = size;
                noBook?.UpdatePriceLevel("BUY", noPrice, size);
                yesBook.UpdatePriceLevel("SELL", Math.Round(1m - noPrice, 4), size);
            }
}

static bool TryParseLevel(JsonElement level, out decimal price, out decimal size)
{
    price = 0; size = 0;
    if (level.ValueKind != JsonValueKind.Array) return false;
    var arr = level.EnumerateArray().ToArray();
    if (arr.Length < 2) return false;
    if (!decimal.TryParse(arr[0].GetString(), System.Globalization.NumberStyles.Any,
            System.Globalization.CultureInfo.InvariantCulture, out price)) return false;
    if (!decimal.TryParse(arr[1].GetString(), System.Globalization.NumberStyles.Any,
            System.Globalization.CultureInfo.InvariantCulture, out size)) return false;
    return price >= MIN_BOOK_PRICE && price <= (1m - MIN_BOOK_PRICE);
}

// ══════════════════════════════════════════════════════════════════════════════
//  POLYMARKET MESSAGE PROCESSING
// ══════════════════════════════════════════════════════════════════════════════
static void ProcessPolyMessage(
    string message,
    ConcurrentDictionary<string, LocalOrderBook> books,
    CrossPlatformArbTelemetryStrategy telemetry)
{
    try
    {
        using var doc = JsonDocument.Parse(message);
        var root = doc.RootElement;
        if (!root.TryGetProperty("event_type", out var etEl)) return;
        string eventType = etEl.GetString() ?? "";

        if (eventType == "book" && root.TryGetProperty("asset_id", out var idEl))
        {
            string assetId = idEl.GetString() ?? "";
            string bookKey = $"P:{assetId}";
            if (!books.TryGetValue(bookKey, out var book)) return;
            if (root.TryGetProperty("bids", out var bidsEl) && root.TryGetProperty("asks", out var asksEl))
                book.ProcessBookUpdate(bidsEl, asksEl);
            // Do NOT call telemetry here: snapshot alone is not live data
        }
        else if (eventType == "price_change" && root.TryGetProperty("price_changes", out var changesEl))
        {
            foreach (var change in changesEl.EnumerateArray())
            {
                if (!change.TryGetProperty("asset_id", out var assetIdEl)) continue;
                string assetId = assetIdEl.GetString() ?? "";
                string bookKey = $"P:{assetId}";
                if (!books.TryGetValue(bookKey, out var book)) continue;

                if (!decimal.TryParse(change.GetProperty("price").GetString(),
                        System.Globalization.NumberStyles.Any,
                        System.Globalization.CultureInfo.InvariantCulture, out decimal price)) continue;
                if (!decimal.TryParse(change.GetProperty("size").GetString(),
                        System.Globalization.NumberStyles.Any,
                        System.Globalization.CultureInfo.InvariantCulture, out decimal size)) continue;
                string side = change.GetProperty("side").GetString() ?? "";

                book.UpdatePriceLevel(side, price, size);
                book.MarkDeltaReceived();
                telemetry.OnBookUpdate(bookKey);
            }
        }
    }
    catch (JsonException) { }
}

// ══════════════════════════════════════════════════════════════════════════════
//  AUTO-MATCH HELPERS
// ══════════════════════════════════════════════════════════════════════════════

// Extract meaningful words from a market title.
// Combines YES and NO titles so both team/candidate names are captured.
static List<string> TitleToKeyWords(string combinedTitle)
{
    var stopWords = new HashSet<string>(StringComparer.OrdinalIgnoreCase)
    {
        // Common English stop words
        "the","a","an","and","or","of","to","in","at","on","for","by","from",
        "with","will","who","when","what","how","does","did","has","have",
        "was","were","are","is","that","this","be","do","not","but","if",
        "as","it","its","yes","no","get","gets","got","more","than","most",
        "any","all","into","out","over","under","up","down","per","via",
        // Domain-generic suffixes that appear in many different team/org names
        // and would cause false positives if used as match keywords
        "gaming","esports","sports","team","gg","club","fc"
    };
    return Regex.Split(combinedTitle.ToLowerInvariant(), @"[^a-z0-9]+")
               .Where(w => w.Length >= 3 && !stopWords.Contains(w))
               .Distinct()
               .ToList();
}
