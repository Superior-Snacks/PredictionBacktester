using System.Text.Json;
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
            string kTicker  = el.TryGetProperty("kalshi_ticker",  out var kt)  ? (kt.GetString()  ?? "") : "";
            string yesToken = el.TryGetProperty("poly_yes_token", out var yt)  ? (yt.GetString()  ?? "") : "";
            string noToken  = el.TryGetProperty("poly_no_token",  out var nt)  ? (nt.GetString()  ?? "") : "";
            string label    = el.TryGetProperty("label",          out var lb)  ? (lb.GetString()  ?? "") : kTicker;
            string eventId  = el.TryGetProperty("event_id",       out var eid) ? (eid.GetString() ?? "") : "";
            DateOnly? settlementDate = null;
            if (el.TryGetProperty("settlement_date", out var sd) && DateOnly.TryParse(sd.GetString(), out var d))
                settlementDate = d;
            if (!string.IsNullOrEmpty(kTicker) && !string.IsNullOrEmpty(yesToken) && !string.IsNullOrEmpty(noToken))
            {
                string pairId = $"MANUAL_{kTicker}__{yesToken[..Math.Min(8, yesToken.Length)]}";
                manualPairs.Add(new CrossPair(pairId, label, kTicker, yesToken, noToken, eventId, settlementDate));
            }
        }
        Console.WriteLine($"[CONFIG] {manualPairs.Count} manual pair(s) loaded from cross_pairs.json");
    }
    catch (Exception ex)
    {
        Console.WriteLine($"[CONFIG WARN] Could not parse cross_pairs.json: {ex.Message}");
    }
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
    Console.WriteLine("[INFO] To generate new pairs, run: python KalshiPolyCross/pair_markets.py");
}

// ── Build shared order books ──────────────────────────────────────────────────
var kalshiSubscribeTickers = pairs.Select(p => p.KalshiTicker).Distinct().ToList();
var polySubscribeTokens    = pairs.SelectMany(p => new[] { p.PolyYesTokenId, p.PolyNoTokenId }).Distinct().ToList();

var state = new MarketStateTracker();
foreach (var ticker in kalshiSubscribeTickers) state.InitKalshiMarket(ticker);
foreach (var token  in polySubscribeTokens)    state.InitPolyToken(token);

// ── Telemetry strategy ────────────────────────────────────────────────────────
var telemetry = new CrossPlatformArbTelemetryStrategy(pairs, state.Books, ARB_THRESHOLD, DEPTH_FLOOR);

// ── REST verifier — confirms arb windows via independent REST calls ───────────
var restVerifier = new CrossArbRestVerifier(orderClient, telemetry);
telemetry.OnArbOpened += restVerifier.OnArbOpened;

Console.WriteLine($"\n[BOOKS] {state.Books.Count} order books created");
Console.WriteLine($"  Kalshi tickers : {kalshiSubscribeTickers.Count}");
Console.WriteLine($"  Poly tokens    : {polySubscribeTokens.Count}");

bool showBlended = !args.Contains("--no-blended");

// Track which tickers/tokens are already subscribed (for hot-reload dedup)
var knownKalshiTickers = new HashSet<string>(kalshiSubscribeTickers, StringComparer.Ordinal);
var knownPolyTokens    = new HashSet<string>(polySubscribeTokens,    StringComparer.Ordinal);
var knownPairIds       = new HashSet<string>(pairs.Select(p => p.PairId), StringComparer.Ordinal);

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

        if (showBlended)
        {
            var blendedSnapshot = telemetry.GetBlendedNearMissSnapshot().ToList();
            if (blendedSnapshot.Count > 0)
            {
                Console.WriteLine($"  --- BLENDED (pick cheapest YES per leg across both platforms) ---");
                foreach (var (cost, evId, choices, depth, isLive) in blendedSnapshot)
                {
                    decimal diff = cost - 1.00m;
                    string  tag  = cost < 1.00m ? "ARB!" : $"+${diff:0.0000} away";
                    string  live = isLive ? " *** LIVE ***" : "";
                    Console.WriteLine($"  ${cost:0.0000} ({tag}) BLENDED({choices}) | depth={depth:0.0} | {evId}{live}");
                }
            }
        }
    }
});

// ══════════════════════════════════════════════════════════════════════════════
//  WEBSOCKET FEEDS
// ══════════════════════════════════════════════════════════════════════════════
var kalshiFeed = new KalshiWebsocketFeed(orderClient, kalshiConfig, kalshiSubscribeTickers,
                                         state, telemetry, KALSHI_BATCH_SIZE, MIN_BOOK_PRICE);
var polyFeed   = new PolymarketWebsocketFeed(POLY_WS_URL, polySubscribeTokens,
                                             state, telemetry, POLY_BATCH_SIZE, POLY_PING_INTERVAL_MS);

// ══════════════════════════════════════════════════════════════════════════════
//  HOT-RELOAD: watch cross_pairs.json for new pairs every 30s
// ══════════════════════════════════════════════════════════════════════════════
_ = Task.Run(async () =>
{
    while (!cts.Token.IsCancellationRequested)
    {
        await Task.Delay(900_000, cts.Token).ContinueWith(_ => { });
        if (cts.Token.IsCancellationRequested) break;
        if (!File.Exists(manualPath)) continue;
        try
        {
            using var doc = JsonDocument.Parse(File.ReadAllText(manualPath));
            var newPairs    = new List<CrossPair>();
            var newKTickers = new List<string>();
            var newPTokens  = new List<string>();

            foreach (var el in doc.RootElement.EnumerateArray())
            {
                string kTicker  = el.TryGetProperty("kalshi_ticker",  out var kt)  ? (kt.GetString()  ?? "") : "";
                string yesToken = el.TryGetProperty("poly_yes_token", out var yt)  ? (yt.GetString()  ?? "") : "";
                string noToken  = el.TryGetProperty("poly_no_token",  out var nt)  ? (nt.GetString()  ?? "") : "";
                string label    = el.TryGetProperty("label",          out var lb)  ? (lb.GetString()  ?? "") : kTicker;
                string eventId  = el.TryGetProperty("event_id",       out var eid) ? (eid.GetString() ?? "") : "";
                DateOnly? settlementDate = null;
                if (el.TryGetProperty("settlement_date", out var sd2) && DateOnly.TryParse(sd2.GetString(), out var d2))
                    settlementDate = d2;
                if (string.IsNullOrEmpty(kTicker) || string.IsNullOrEmpty(yesToken) || string.IsNullOrEmpty(noToken)) continue;

                string pairId = $"MANUAL_{kTicker}__{yesToken[..Math.Min(8, yesToken.Length)]}";
                if (knownPairIds.Contains(pairId)) continue;
                knownPairIds.Add(pairId);

                newPairs.Add(new CrossPair(pairId, label, kTicker, yesToken, noToken, eventId, settlementDate));
                if (knownKalshiTickers.Add(kTicker)) newKTickers.Add(kTicker);
                if (knownPolyTokens.Add(yesToken))   newPTokens.Add(yesToken);
                if (knownPolyTokens.Add(noToken))    newPTokens.Add(noToken);
            }

            if (newPairs.Count == 0) continue;

            foreach (var t in newKTickers) state.InitKalshiMarket(t);
            foreach (var t in newPTokens)  state.InitPolyToken(t);
            telemetry.AddPairs(newPairs);
            if (newKTickers.Count > 0) kalshiFeed.EnqueueSubscribe(newKTickers);
            if (newPTokens.Count  > 0) polyFeed.EnqueueSubscribe(newPTokens);
        }
        catch (Exception ex)
        {
            Console.WriteLine($"[HOT-RELOAD] Error reading {manualPath}: {ex.Message}");
        }
    }
});

// ── Book refresher — keeps quiet books alive via periodic REST snapshots ──────
var bookRefresher = new BookRefresherService(state.Books, orderClient);
_ = Task.Run(async () =>
{
    try { await bookRefresher.RunAsync(cts.Token); }
    catch (Exception ex) { Console.WriteLine($"[BOOK REFRESH ERROR] {ex.Message}"); }
});

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
