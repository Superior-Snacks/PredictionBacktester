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
static async Task<Dictionary<string, string>> FetchKalshiSeriesCategories(KalshiOrderClient orderClient, HttpClient httpClient)
{
    var seriesCategories = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
    string baseRest = orderClient.ApiConfig.BaseRestUrl.TrimEnd('/');
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
var kalshiTitles     = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase); // ticker → full market title

using var httpClient = new HttpClient();
httpClient.DefaultRequestHeaders.Add("User-Agent", "KalshiPolyCross/1.0");

var kalshiSeriesCategories = await FetchKalshiSeriesCategories(orderClient, httpClient);

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
                    if (string.IsNullOrEmpty(ticker)) continue;
                    kalshiTickers.Add(ticker);
                    if (!string.IsNullOrEmpty(title)) kalshiTitles[ticker] = title;
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
                        polyMarkets.Add((question, tokens[0], tokens[1]));
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
    await pairingService.FindAndSavePairs(kalshiTitles, polyMarkets, manualPath);
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

            // Clear books on reconnect, then notify telemetry (closes open windows)
            foreach (var ticker in kalshiSubscribeTickers)
            {
                books[$"K:{ticker}"].ClearBook();
                books[$"K:{ticker}_NO"].ClearBook();
                yesSizes[ticker].Clear();
                noSizes[ticker].Clear();
            }
            telemetry.OnKalshiReconnect();

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

            // Clear books on reconnect, then notify telemetry (closes open windows)
            foreach (var token in polySubscribeTokens)
                books[$"P:{token}"].ClearBook();
            telemetry.OnPolyReconnect();

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
