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

// ── Load regex rules ──────────────────────────────────────────────────────────
string rulesPath = Path.Combine(AppContext.BaseDirectory, "cross_pairs.json");
if (!File.Exists(rulesPath)) rulesPath = "cross_pairs.json";
if (!File.Exists(rulesPath))
{
    Console.WriteLine("[ERROR] cross_pairs.json not found. Place it alongside the executable.");
    return;
}

List<(string Name, Regex KalshiRe, Regex PolyRe)> rules;
try
{
    using var ruleDoc = JsonDocument.Parse(File.ReadAllText(rulesPath));
    rules = ruleDoc.RootElement.EnumerateArray().Select(el => (
        Name:     el.GetProperty("name").GetString() ?? "",
        KalshiRe: new Regex(el.GetProperty("kalshi_regex").GetString()     ?? "^$", RegexOptions.IgnoreCase),
        PolyRe:   new Regex(el.GetProperty("poly_title_regex").GetString()  ?? "^$", RegexOptions.IgnoreCase)
    )).ToList();
    Console.WriteLine($"[CONFIG] Loaded {rules.Count} matching rules from cross_pairs.json");
}
catch (Exception ex)
{
    Console.WriteLine($"[ERROR] Failed to parse cross_pairs.json: {ex.Message}");
    return;
}

// ── Scan Kalshi binary tickers ────────────────────────────────────────────────
Console.WriteLine("\n[KALSHI SCANNER] Fetching open markets...");
var kalshiScanner = new KalshiMarketScanner(orderClient, minVolume24h: 0m);
var kalshiScan = await kalshiScanner.ScanAllAsync();

// Collect all binary tickers (strip BIN_ prefix to get the actual ticker)
var kalshiTickers = kalshiScan.BinaryMarkets.Keys
    .Select(k => k.StartsWith("BIN_", StringComparison.Ordinal) ? k[4..] : k)
    .Distinct()
    .ToList();
Console.WriteLine($"[KALSHI SCANNER] {kalshiTickers.Count} binary tickers available for matching");

// ── Fetch Polymarket active markets ───────────────────────────────────────────
Console.WriteLine("[POLY SCANNER] Fetching active Polymarket markets...");

// List of (question, yesTokenId, noTokenId)
var polyMarkets = new List<(string Question, string YesToken, string NoToken)>();
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
            if (tokens.Count >= 2 && !string.IsNullOrEmpty(question))
                polyMarkets.Add((question, tokens[0], tokens[1]));
        }
        if (count < pageSize) break;
        offset += pageSize;
        await Task.Delay(200);
    }
    Console.WriteLine($"[POLY SCANNER] {polyMarkets.Count} active markets fetched");
}
catch (Exception ex)
{
    Console.WriteLine($"[POLY SCANNER ERROR] {ex.Message}");
}

// ── Apply regex rules to build matched pairs ──────────────────────────────────
Console.WriteLine("\n[MATCHING] Applying regex rules...");
var pairs = new List<CrossPair>();

foreach (var (name, kalshiRe, polyRe) in rules)
{
    var matchedKalshi = kalshiTickers.Where(t => kalshiRe.IsMatch(t)).ToList();
    var matchedPoly   = polyMarkets.Where(m => polyRe.IsMatch(m.Question)).ToList();

    if (matchedKalshi.Count == 0 || matchedPoly.Count == 0)
    {
        Console.WriteLine($"  [NO MATCH] {name}  (Kalshi:{matchedKalshi.Count} / Poly:{matchedPoly.Count})");
        continue;
    }

    foreach (var kTicker in matchedKalshi)
    foreach (var (question, yesToken, noToken) in matchedPoly)
    {
        string pairId = $"CROSS_{kTicker}__{yesToken[..Math.Min(8, yesToken.Length)]}";
        string label  = $"{name} | K:{kTicker}";
        pairs.Add(new CrossPair(pairId, label, kTicker, yesToken, noToken));
        Console.WriteLine($"  [MATCH] {label}");
        Console.WriteLine($"          Poly: \"{question}\"");
        Console.WriteLine($"          YES={yesToken[..Math.Min(16, yesToken.Length)]}... NO={noToken[..Math.Min(16, noToken.Length)]}...");
    }
}

if (pairs.Count == 0)
{
    Console.WriteLine("\n[WARN] No pairs matched. Edit cross_pairs.json and restart.");
    Console.WriteLine("       The bot will continue but no arbs can be detected.");
}
else
{
    Console.WriteLine($"\n[MATCHING] {pairs.Count} pair(s) ready for monitoring");
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
