using System.Text.Json;
using KalshiPolyCross;
using PredictionBacktester.Engine;
using PredictionBacktester.Engine.LiveExecution;

// ══════════════════════════════════════════════════════════════════════════════
//  USAGE
//
//  dotnet run --project KalshiPolyCross -- --telemetry          # detect arbs, log CSV, no orders
//  dotnet run --project KalshiPolyCross -- --dry-run            # same as --live, log only, no real orders
//  dotnet run --project KalshiPolyCross -- --live               # full production: real orders on both legs
//  dotnet run --project KalshiPolyCross -- --telemetry --debug  # any mode can add --debug for verbose logs
//
//  Exactly one mode flag is required. --debug is optional and works with any mode.
//
//  Required env vars (Kalshi):
//    KALSHI_API_KEY_ID          Kalshi API key ID
//    KALSHI_PRIVATE_KEY_PATH    Path to RSA private key PEM file
//
//  Required env vars (Polymarket execution — omit for telemetry-only mode):
//    POLY_API_KEY               Polymarket CLOB API key
//    POLY_API_SECRET            Polymarket CLOB API secret
//    POLY_API_PASSPHRASE        Polymarket CLOB API passphrase
//    POLY_PRIVATE_KEY           EOA private key (hex, no 0x prefix)
//    POLY_PROXY_ADDRESS         Gnosis Safe proxy wallet address (POLY_GNOSIS_SAFE signer)
//    POLY_RPC_URL               (optional) Polygon RPC — defaults to https://polygon-rpc.com
//
//  cross_pairs.json: verified Kalshi↔Polymarket market pairs; auto-populated on scan,
//                    must be non-empty for arb detection to fire.
//
//  Output: CrossArbTelemetry_*.csv   — all detected arb windows (always)
//          CrossArbExecution_*.csv   — order execution results   (when executor active)
// ══════════════════════════════════════════════════════════════════════════════

// ══════════════════════════════════════════════════════════════════════════════
//  MODE SELECTION
// ══════════════════════════════════════════════════════════════════════════════
bool isLive      = args.Contains("--live");
bool isDryRun    = args.Contains("--dry-run");
bool isTelemetry = args.Contains("--telemetry");

int modeCount = (isLive ? 1 : 0) + (isDryRun ? 1 : 0) + (isTelemetry ? 1 : 0);
if (modeCount == 0)
{
    Console.WriteLine("Usage: KalshiPolyCross --telemetry | --dry-run | --live");
    Console.WriteLine("  --telemetry   detect arbs, log CSV, no orders (no POLY_* env vars needed)");
    Console.WriteLine("  --dry-run     same as --live but logs instead of placing real orders");
    Console.WriteLine("  --live        full production — real orders on both legs");
    return;
}
if (modeCount > 1)
{
    Console.WriteLine("[ERROR] Specify exactly one mode: --telemetry, --dry-run, or --live");
    return;
}

bool isDebug = args.Contains("--debug");
DebugLog.Enabled = isDebug;

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
string modeLabel = isLive ? "LIVE EXECUTION" : isDryRun ? "DRY RUN" : "TELEMETRY";
string debugTag  = isDebug ? " +DEBUG" : "";
Console.WriteLine("═══════════════════════════════════════════════════════════");
Console.WriteLine($"  KALSHI ↔ POLYMARKET CROSS-PLATFORM ARB  [{modeLabel}{debugTag}]");
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
// In dev builds AppContext.BaseDirectory = bin/Debug/net10.0/ — the output copy of
// cross_pairs.json is stale; pair_markets.py writes to the project source dir 3 levels up.
// Detect dev by looking for a .csproj file there; production published builds have none.
string outputDirFile = Path.Combine(AppContext.BaseDirectory, "cross_pairs.json");
string sourceDir     = Path.GetFullPath(Path.Combine(AppContext.BaseDirectory, "../../.."));
string sourceDirFile = Path.Combine(sourceDir, "cross_pairs.json");
bool   isDevBuild    = Directory.GetFiles(sourceDir, "*.csproj").Length > 0;
string manualPath    = isDevBuild && File.Exists(sourceDirFile) ? sourceDirFile : outputDirFile;
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

// ── Executor — live order placement on WS-detected arb windows ────────────────
CrossArbExecutor? executor = null;

if (isLive || isDryRun)
{
    var polyConfig = LoadPolymarketConfig();
    if (polyConfig == null)
    {
        Console.WriteLine($"[ERROR] --{(isLive ? "live" : "dry-run")} requires POLY_* env vars.");
        Console.WriteLine("  Required: POLY_API_KEY, POLY_API_SECRET, POLY_API_PASSPHRASE, POLY_PRIVATE_KEY, POLY_PROXY_ADDRESS");
        return;
    }
    var polyOrderClient = new PredictionBacktester.Engine.LiveExecution.PolymarketOrderClient(polyConfig);
    executor = new CrossArbExecutor(
        kalshi:              orderClient,
        poly:                polyOrderClient,
        telemetry:           telemetry,
        books:               state.Books,
        maxContracts:        1m,
        maxExposureUsd:      10m,
        executionThreshold:  0.990m,
        pairCooldownSeconds: 120,
        fillTimeoutMs:       5000,
        dryRun:              isDryRun);
    telemetry.OnArbOpened += executor.OnArbOpened;
    string execLabel = isDryRun ? "DRY RUN — no real orders" : "LIVE";
    Console.WriteLine($"[EXECUTOR] {execLabel} | maxContracts=1 maxExposure=$10 threshold=0.990 cooldown=120s");
}
else // --telemetry
{
    Console.WriteLine("[EXECUTOR] Telemetry-only mode — no orders will be placed.");
}

Console.WriteLine($"\n[BOOKS] {state.Books.Count} order books created");
Console.WriteLine($"  Kalshi tickers : {kalshiSubscribeTickers.Count}");
Console.WriteLine($"  Poly tokens    : {polySubscribeTokens.Count}");

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
    try
    {
        while (!cts.Token.IsCancellationRequested)
        {
            await Task.Delay(NEAR_MISS_INTERVAL_MS, cts.Token).ContinueWith(_ => { });
            if (cts.Token.IsCancellationRequested) break;

            int kalshiReady = state.Books.Count(kv => kv.Key.StartsWith("K:") && kv.Value.HasReceivedDelta);
            int polyReady   = state.Books.Count(kv => kv.Key.StartsWith("P:") && kv.Value.HasReceivedDelta);
            int kalshiTotal = state.Books.Count(kv => kv.Key.StartsWith("K:"));
            int polyTotal   = state.Books.Count(kv => kv.Key.StartsWith("P:"));

            DebugLog.Write($"Near-miss reporter: kalshi={kalshiReady}/{kalshiTotal} poly={polyReady}/{polyTotal} pairs={telemetry.TotalPairs} openArbs={telemetry.OpenArbs}");

            Console.WriteLine($"\n[TELEMETRY] --- TOP {Math.Min(10, pairs.Count)} CLOSEST TO CROSS-PLATFORM ARB ---");
            Console.WriteLine($"  Kalshi books: {kalshiReady}/{kalshiTotal} | Poly books: {polyReady}/{polyTotal} | Pairs: {telemetry.TotalPairs} | Open arbs: {telemetry.OpenArbs}");

            var snapshot = telemetry.GetNearMissSnapshot().Take(10).ToList();
            foreach (var (cost, label, pairId, arbType, depth, isLiveArb) in snapshot)
            {
                decimal diff = cost - 1.00m;
                string  tag  = cost < 1.00m ? "ARB!" : $"+${diff:0.0000} away";
                string  live = isLiveArb ? " *** LIVE ***" : "";
                Console.WriteLine($"  ${cost:0.0000} ({tag}) {arbType} | depth={depth:0.0} | {label}{live}");
            }

            if (snapshot.Count == 0)
                Console.WriteLine("  (no books priced yet — waiting for WS data)");
        }
    }
    catch (Exception ex)
    {
        Console.WriteLine($"[NEAR-MISS REPORTER ERROR] {ex.Message}");
        DebugLog.Write($"Near-miss reporter crashed: {ex}");
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
        if (!File.Exists(manualPath))
        {
            DebugLog.Write($"Hot-reload: {manualPath} not found, skipping");
            continue;
        }
        DebugLog.Write($"Hot-reload: reading {manualPath}");
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

            if (newPairs.Count == 0)
            {
                DebugLog.Write("Hot-reload: no new pairs found in file");
                continue;
            }
            DebugLog.Write($"Hot-reload: found {newPairs.Count} new pair(s) — K={newKTickers.Count} new tickers, P={newPTokens.Count} new tokens");

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
    catch (Exception ex)
    {
        Console.WriteLine($"[FATAL] Kalshi feed crashed: {ex.Message}");
        DebugLog.Write($"Kalshi feed exception: {ex}");
    }
    finally { if (!cts.IsCancellationRequested) cts.Cancel(); }
});

var polyWsTask = Task.Run(async () =>
{
    try { await polyFeed.RunAsync(cts.Token); }
    catch (Exception ex)
    {
        Console.WriteLine($"[FATAL] Poly feed crashed: {ex.Message}");
        DebugLog.Write($"Poly feed exception: {ex}");
    }
    finally { if (!cts.IsCancellationRequested) cts.Cancel(); }
});

await Task.WhenAll(kalshiWsTask, polyWsTask);

DebugLog.Write("WS feeds stopped — beginning shutdown sequence");
try { await telemetry.ShutdownAsync(); }
catch (Exception ex)
{
    Console.WriteLine($"[SHUTDOWN ERROR] Telemetry flush failed: {ex.Message}");
    DebugLog.Write($"telemetry.ShutdownAsync exception: {ex}");
}
if (executor != null)
{
    try { await executor.ShutdownAsync(); }
    catch (Exception ex)
    {
        Console.WriteLine($"[SHUTDOWN ERROR] Executor flush failed: {ex.Message}");
        DebugLog.Write($"executor.ShutdownAsync exception: {ex}");
    }
}
Console.WriteLine("\n[SHUTDOWN] Cross-platform arb bot stopped.");

static PredictionBacktester.Engine.LiveExecution.PolymarketApiConfig? LoadPolymarketConfig()
{
    string[] required = ["POLY_API_KEY", "POLY_API_SECRET", "POLY_API_PASSPHRASE",
                         "POLY_PRIVATE_KEY", "POLY_PROXY_ADDRESS"];
    if (required.Any(k => string.IsNullOrEmpty(Environment.GetEnvironmentVariable(k))))
        return null;
    return new PredictionBacktester.Engine.LiveExecution.PolymarketApiConfig
    {
        ApiKey        = Environment.GetEnvironmentVariable("POLY_API_KEY")!.Trim(),
        ApiSecret     = Environment.GetEnvironmentVariable("POLY_API_SECRET")!.Trim(),
        ApiPassphrase = Environment.GetEnvironmentVariable("POLY_API_PASSPHRASE")!.Trim(),
        PrivateKey    = Environment.GetEnvironmentVariable("POLY_PRIVATE_KEY")!.Trim(),
        ProxyAddress  = Environment.GetEnvironmentVariable("POLY_PROXY_ADDRESS")!.Trim(),
        RpcUrl        = (Environment.GetEnvironmentVariable("POLY_RPC_URL") ?? "https://polygon-rpc.com").Trim(),
    };
}
