using System.Text.Json;
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

// ══════════════════════════════════════════════════════════════════════════════
//  STARTUP
// ══════════════════════════════════════════════════════════════════════════════
Console.WriteLine("═══════════════════════════════════════════════════════════");
Console.WriteLine("  KALSHI ↔ POLYMARKET CROSS-PLATFORM ARB TELEMETRY");
Console.WriteLine("═══════════════════════════════════════════════════════════");

// Check for pairing mode
bool isPairingMode = args.Contains("--pair");
if (isPairingMode)
{
    Console.ForegroundColor = ConsoleColor.Cyan;
    Console.WriteLine("\n[MODE] Running in AI Market Pairing mode. The bot will not start.");
    Console.ResetColor();
}

// ── Kalshi auth ───────────────────────────────────────────────────────────────
var kalshiConfig = KalshiApiConfig.FromEnvironment();
if (string.IsNullOrEmpty(kalshiConfig.ApiKeyId) || string.IsNullOrEmpty(kalshiConfig.PrivateKeyPath))
{
    Console.WriteLine("[ERROR] Set KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH environment variables.");
    return;
}

// ── Gemini auth (for pairing mode) ────────────────────────────────────────────
string? geminiApiKey = Environment.GetEnvironmentVariable("GEMINI_API_KEY");
if (isPairingMode && string.IsNullOrEmpty(geminiApiKey))
{
    Console.WriteLine("[ERROR] --pair mode requires GEMINI_API_KEY environment variable.");
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

/// <summary>
/// Fetches all series from the Kalshi API to build a map of series_ticker -> category.
/// This is the reliable way to get category info, as the market/event objects often have it deprecated.
/// </summary>
static async Task<Dictionary<string, string>> FetchKalshiSeriesCategories(KalshiOrderClient orderClient, HttpClient httpClient, KalshiApiConfig config)
{
    var seriesCategories = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
    string baseRest = config.BaseRestUrl.TrimEnd('/');
    string cursor = "";

    Console.WriteLine("[KALSHI SCANNER] Fetching series data for category mapping...");

    while (true)
    {
        string path = string.IsNullOrEmpty(cursor) ? "/series?limit=1000" : $"/series?limit=1000&cursor={Uri.EscapeDataString(cursor)}";
        var (key, ts, sig) = orderClient.CreateAuthHeaders("GET", path);
        using var req = new HttpRequestMessage(HttpMethod.Get, baseRest + path);
        req.Headers.Add("KALSHI-ACCESS-KEY", key);
        req.Headers.Add("KALSHI-ACCESS-TIMESTAMP", ts);
        req.Headers.Add("KALSHI-ACCESS-SIGNATURE", sig);

        using var resp = await httpClient.SendAsync(req);
        resp.EnsureSuccessStatusCode();
        using var doc = JsonDocument.Parse(await resp.Content.ReadAsStringAsync());

        if (doc.RootElement.TryGetProperty("series", out var seriesEl))
            foreach (var s in seriesEl.EnumerateArray())
                if (s.TryGetProperty("ticker", out var tEl) && s.TryGetProperty("category", out var cEl) && tEl.GetString() is { } t && cEl.GetString() is { } c)
                    seriesCategories[t] = c;

        cursor = doc.RootElement.TryGetProperty("cursor", out var curEl) ? curEl.GetString() ?? "" : "";
        if (string.IsNullOrEmpty(cursor)) break;
        await Task.Delay(150);
    }
    Console.WriteLine($"[KALSHI SCANNER] {seriesCategories.Count} series categories loaded.");
    return seriesCategories;
}

// ── Scan Kalshi binary markets ────────────────────────────────────────────────
// Simple direct scan: fetch all open markets from the REST API.
// No events endpoint, no blocklist, no volume cap — we want every binary market.
Console.WriteLine("\n[KALSHI SCANNER] Fetching all open markets...");
var kalshiTickers    = new List<string>();
var kalshiTokenNames = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase); // ticker → yes_sub_title, ticker_NO → no_sub_title
var kalshiMarkets    = new Dictionary<string, (string Title, DateTime? CloseDate, string Rules)>(StringComparer.OrdinalIgnoreCase); 

using var httpClient = new HttpClient();
httpClient.DefaultRequestHeaders.Add("User-Agent", "KalshiPolyCross/1.0");

var kalshiSeriesCategories = await FetchKalshiSeriesCategories(orderClient, httpClient, kalshiConfig);

string baseRest = kalshiConfig.BaseRestUrl.TrimEnd('/');
string cursor = "";
int kalshiTotal = 0;

while (true)
{
    string relPath = string.IsNullOrEmpty(cursor)
        ? "/events?status=open&with_nested_markets=true&limit=200"
        : $"/events?status=open&with_nested_markets=true&limit=200&cursor={Uri.EscapeDataString(cursor)}";

    var (key, ts, sig) = orderClient.CreateAuthHeaders("GET", relPath);
    using var req = new HttpRequestMessage(HttpMethod.Get, baseRest + relPath);
    req.Headers.Add("KALSHI-ACCESS-KEY", key);
    req.Headers.Add("KALSHI-ACCESS-TIMESTAMP", ts);
    req.Headers.Add("KALSHI-ACCESS-SIGNATURE", sig);

    using var resp = await httpClient.SendAsync(req);
    resp.EnsureSuccessStatusCode();
    using var scanDoc = JsonDocument.Parse(await resp.Content.ReadAsStreamAsync());
    var root = scanDoc.RootElement;

    if (root.TryGetProperty("events", out var eventsEl))
    {
        foreach (var ev in eventsEl.EnumerateArray())
        {
            kalshiTotal++;
            string seriesTicker = ev.TryGetProperty("series_ticker", out var stEl) ? stEl.GetString() ?? "" : "";
            string category = kalshiSeriesCategories.GetValueOrDefault(seriesTicker, "");

            if (!string.IsNullOrEmpty(KALSHI_CATEGORY_FILTER) && !category.Equals(KALSHI_CATEGORY_FILTER, StringComparison.OrdinalIgnoreCase))
                continue;

            if (ev.TryGetProperty("markets", out var marketsEl))
            {
                foreach (var m in marketsEl.EnumerateArray())
                {
                    string ticker = m.TryGetProperty("ticker", out var tEl) ? (tEl.GetString() ?? "") : "";
                    string title = m.TryGetProperty("title", out var tiEl) ? (tiEl.GetString() ?? "") : "";
                    string rules = m.TryGetProperty("rules_primary", out var rEl) ? (rEl.GetString() ?? "") : "";
                    
                    DateTime? closeDate = null;
                    if (m.TryGetProperty("expected_expiration_time", out var expEl) && expEl.ValueKind == JsonValueKind.String && DateTime.TryParse(expEl.GetString(), out var dt1)) closeDate = dt1;
                    else if (m.TryGetProperty("close_time", out var clEl) && clEl.ValueKind == JsonValueKind.String && DateTime.TryParse(clEl.GetString(), out var dt2)) closeDate = dt2;

                    if (string.IsNullOrEmpty(ticker)) continue;
                    kalshiTickers.Add(ticker);
                    if (!string.IsNullOrEmpty(title)) kalshiMarkets[ticker] = (title, closeDate, rules);
                }
            }
        }
    }

    cursor = root.TryGetProperty("cursor", out var curEl) ? (curEl.GetString() ?? "") : "";
    if (string.IsNullOrEmpty(cursor)) break;
    await Task.Delay(150);
}

if (!string.IsNullOrEmpty(KALSHI_CATEGORY_FILTER))
{
    Console.WriteLine($"[KALSHI SCANNER] {kalshiTotal} open events fetched → {kalshiTickers.Count} markets found in category '{KALSHI_CATEGORY_FILTER}'");
}
else
{
    Console.WriteLine($"[KALSHI SCANNER] {kalshiTotal} open markets fetched (no category filter)");
}

// ── Fetch Polymarket active markets ───────────────────────────────────────────
Console.WriteLine("[POLY SCANNER] Fetching active Polymarket markets...");

// List of (question, yesTokenId, noTokenId, endDate, description)
var polyMarkets = new List<(string Question, string YesToken, string NoToken, DateTime? EndDate, string Description)>();

try
{
    int offset = 0;
    const int pageSize = 500;
    while (true)
    {
        string url = $"{POLY_GAMMA_URL}/events?active=true&closed=false&limit={pageSize}&offset={offset}";
        string json = await httpClient.GetStringAsync(url);
        using var doc = JsonDocument.Parse(json);
        var arr = doc.RootElement;
        if (arr.ValueKind != JsonValueKind.Array) break;
        int count = 0;
        foreach (var ev in arr.EnumerateArray())
        {
            count++;

            // Category filter: keep only markets matching the configured category
            bool includeEvent = string.IsNullOrEmpty(POLY_CATEGORY_FILTER);
            if (!includeEvent && POLY_CATEGORY_FILTER != null)
            {
                if (ev.TryGetProperty("tags", out var tagsEl) && tagsEl.ValueKind == JsonValueKind.Array)
                    includeEvent = tagsEl.EnumerateArray().Any(t =>
                        (t.TryGetProperty("slug",  out var sl) && (sl.GetString()  ?? "").Contains(POLY_CATEGORY_FILTER, StringComparison.OrdinalIgnoreCase)) ||
                        (t.TryGetProperty("label", out var ll) && (ll.GetString()  ?? "").Contains(POLY_CATEGORY_FILTER, StringComparison.OrdinalIgnoreCase)));
                // Also accept if a top-level category field matches
                if (!includeEvent && ev.TryGetProperty("category", out var pcEl))
                    includeEvent = (pcEl.GetString() ?? "").Contains(POLY_CATEGORY_FILTER, StringComparison.OrdinalIgnoreCase);
            }

            string description = ev.TryGetProperty("description", out var descEl) ? (descEl.GetString() ?? "") : "";
            DateTime? endDate = null;
            if (ev.TryGetProperty("end_date", out var edEl) && edEl.ValueKind == JsonValueKind.String && DateTime.TryParse(edEl.GetString(), out var dt)) endDate = dt;

            if (includeEvent && ev.TryGetProperty("markets", out var marketsEl) && marketsEl.ValueKind == JsonValueKind.Array)
            {
                foreach (var mkt in marketsEl.EnumerateArray())
                {
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

                    if (tokens.Count >= 2 && !string.IsNullOrEmpty(question))
                        polyMarkets.Add((question, tokens[0], tokens[1], endDate, description));
                }
            }
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

// ── AI Market Pairing Mode ────────────────────────────────────────────────────
if (isPairingMode)
{
    // Apply the same category filter used for the live bot so pairing only produces
    // pairs the live bot will actually monitor.
    var pairingService = new MarketPairingService(geminiApiKey!);
    await pairingService.FindAndSavePairs(kalshiMarkets, polyMarkets, manualPath);
    Console.WriteLine("\n[PAIRING MODE] Process complete. Exiting.");
    return;
}

// ══════════════════════════════════════════════════════════════════════════════
//  BOT EXECUTION (Normal Mode)
// ══════════════════════════════════════════════════════════════════════════════

// In normal mode, we just use the pairs from the JSON file.
var pairs = new List<CrossPair>(manualPairs);

Console.WriteLine($"[MATCHING] {pairs.Count} pair(s) loaded from {manualPath}");
if (pairs.Count == 0)
{
    Console.WriteLine("[WARN] No pairs found. Add entries to cross_pairs.json or wait for more Kalshi/Poly market overlap.");
    Console.WriteLine("[INFO] To generate new pairs, run with the --pair argument.");
}

// ── Build shared order books ──────────────────────────────────────────────────
var kalshiSubscribeTickers = pairs.Select(p => p.KalshiTicker).Distinct().ToList();
var polySubscribeTokens    = pairs.SelectMany(p => new[] { p.PolyYesTokenId, p.PolyNoTokenId }).Distinct().ToList();

var state = new MarketStateTracker();
foreach (var ticker in kalshiSubscribeTickers) state.InitKalshiMarket(ticker);
foreach (var token  in polySubscribeTokens)    state.InitPolyToken(token);

// ── Telemetry strategy ────────────────────────────────────────────────────────
var telemetry = new CrossPlatformArbTelemetryStrategy(pairs, state.Books, ARB_THRESHOLD, DEPTH_FLOOR);

Console.WriteLine($"\n[BOOKS] {state.Books.Count} order books created");
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

        int kalshiReady = state.Books.Count(kv => kv.Key.StartsWith("K:") && kv.Value.HasReceivedDelta);
        int polyReady   = state.Books.Count(kv => kv.Key.StartsWith("P:") && kv.Value.HasReceivedDelta);
        int kalshiTotal = state.Books.Count(kv => kv.Key.StartsWith("K:"));
        int polyTotal   = state.Books.Count(kv => kv.Key.StartsWith("P:"));

        Console.WriteLine($"\n[TELEMETRY] --- TOP {Math.Min(10, pairs.Count)} CLOSEST TO CROSS-PLATFORM ARB ---");
        Console.WriteLine($"  Kalshi books: {kalshiReady}/{kalshiTotal} | Poly books: {polyReady}/{polyTotal} | Pairs: {telemetry.TotalPairs} | Open arbs: {telemetry.OpenArbs}");

        var snapshot = telemetry.GetNearMissSnapshot().Take(10).ToList();
        foreach (var (cost, label, pairId, arbType, depth, isLive) in snapshot)
        {
            decimal diff   = cost - 1.00m;
            string  tag    = cost < 1.00m ? "ARB!" : $"+${diff:0.0000} away";
            string  live   = isLive ? " *** LIVE ***" : "";
            Console.WriteLine($"  ${cost:0.0000} ({tag}) {arbType} | depth={depth:0.0} | {label}{live}");
        }

        if (snapshot.Count == 0)
            Console.WriteLine("  (no books priced yet — waiting for WS data)");
    }
});

// ══════════════════════════════════════════════════════════════════════════════
//  WEBSOCKET FEEDS
// ══════════════════════════════════════════════════════════════════════════════
var kalshiFeed = new KalshiWebsocketFeed(orderClient, kalshiConfig, kalshiSubscribeTickers,
                                         state, telemetry, KALSHI_BATCH_SIZE, MIN_BOOK_PRICE);
var polyFeed   = new PolymarketWebsocketFeed(POLY_WS_URL, polySubscribeTokens,
                                             state, telemetry, POLY_BATCH_SIZE, POLY_PING_INTERVAL_MS);

var kalshiWsTask = Task.Run(async () => 
{
    try { await kalshiFeed.RunAsync(cts.Token); }
    catch (Exception ex) { Console.WriteLine($"[FATAL] Kalshi feed crashed: {ex.Message}"); }
    finally { if (!cts.IsCancellationRequested) cts.Cancel(); }
});

var polyWsTask   = Task.Run(async () => 
{
    try { await polyFeed.RunAsync(cts.Token); }
    catch (Exception ex) { Console.WriteLine($"[FATAL] Poly feed crashed: {ex.Message}"); }
    finally { if (!cts.IsCancellationRequested) cts.Cancel(); }
});

await Task.WhenAll(kalshiWsTask, polyWsTask);
await telemetry.ShutdownAsync(); // flush and close CSV before exit
Console.WriteLine("\n[SHUTDOWN] Cross-platform arb telemetry stopped.");
