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
//  dotnet run --project KalshiPolyCross -- --dry-run --try 5    # execute exactly 5 arbs then exit cleanly
//  dotnet run --project KalshiPolyCross -- --live --min-buy     # live: always 1 contract, ignore maxBet sizing
//  dotnet run --project KalshiPolyCross -- --dry-run --seed 42  # reproducible dry-run (same fills every time)
//  dotnet run --project KalshiPolyCross -- --dry-run --scenario FlakyKalshi  # named failure profile
//
//  Exactly one mode flag is required. --debug, --try N, --min-buy, --seed N, and --scenario are optional.
//  --try N         execute exactly N complete arbs then shut down; works with --dry-run or --live.
//  --min-buy       cap every arb to exactly 1 contract regardless of maxBet (useful for initial live shakedown).
//  --seed N        seed the dry-run fill RNG for reproducible results; omit for non-deterministic simulation.
//  --scenario Name named failure profile for dry-run (default: HappyPath).
//                  Valid: HappyPath, FlakyKalshi, FlakyPoly, ChronicSlippage,
//                         PartialFillSwamp, BothVenuesFlaky, LatencyStorm
//
//  Runtime key toggles (all modes):
//    N   toggle near-miss top-10 report   (on by default)
//    S   toggle status dashboard          (on by default; live/dry-run only)
//    M   inject +1 position mismatch      (dry-run only; fires on next ReconcileTradeAsync → halt)
//    C   simulate WS reconnect            (dry-run only; closes arb windows, resumes after 500ms)
//    E   inject 6 Kalshi REST errors      (dry-run only; triggers VENUE_MAINTENANCE halt at 5+)
//    X   drop first pair's Poly YES book  (dry-run only; simulates book-missing during recovery)
//
//  --debug additional key toggles:
//    D   toggle Discovery logs  — arb window detection events
//    T   toggle Trades logs     — order execution events
//    B   toggle Balance logs    — balance fetch / refresh events
//    F   toggle Feed logs       — WebSocket connect / message events
//    R   toggle Books logs      — REST book-refresh events
//    H   print current toggle status
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
//    POLY_SOCKS_PROXY           (optional) SOCKS5 proxy for Polymarket REST — socks5://host:port
//                               Balance fetches + order execution route through this proxy.
//                               WebSocket feed connects directly (no proxy).
//                               Omit if running from an unrestricted IP (e.g. US cloud server).
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

// --try N: execute exactly N arbs then shut down cleanly (dry-run or live)
int? tryN = null;
int tryIdx = Array.IndexOf(args, "--try");
if (tryIdx >= 0 && tryIdx + 1 < args.Length && int.TryParse(args[tryIdx + 1], out int parsedN) && parsedN > 0)
    tryN = parsedN;

// --min-buy: cap every arb to 1 contract regardless of maxBet sizing
bool minBuy = args.Contains("--min-buy");

// --seed N: seed the dry-run fill RNG for reproducible simulated outcomes
int? fillSeed = null;
int seedIdx = Array.IndexOf(args, "--seed");
if (seedIdx >= 0 && seedIdx + 1 < args.Length && int.TryParse(args[seedIdx + 1], out int parsedSeed))
    fillSeed = parsedSeed;

// --scenario <name>: pick a named failure profile for dry-run (default: HappyPath)
string scenarioName = "HappyPath";
int scenIdx = Array.IndexOf(args, "--scenario");
if (scenIdx >= 0 && scenIdx + 1 < args.Length)
    scenarioName = args[scenIdx + 1];
if (scenIdx >= 0 && !isDryRun)
    Console.WriteLine("[WARN] --scenario is only meaningful with --dry-run; ignored in this mode.");

var cts = new CancellationTokenSource();

// ══════════════════════════════════════════════════════════════════════════════
//  CONFIGURATION
// ══════════════════════════════════════════════════════════════════════════════
const decimal ARB_THRESHOLD         = 0.995m;
const decimal DEPTH_FLOOR           = 1m;
const decimal MIN_BOOK_PRICE        = 0.03m;
const int     KALSHI_BATCH_SIZE     = 100;
const int     POLY_BATCH_SIZE       = 200;
const int     POLY_PING_INTERVAL_MS = 9_000;
const int     NEAR_MISS_INTERVAL_MS  = 60_000;
const int     STATUS_DASH_INTERVAL_MS = 30_000;
const string  POLY_WS_URL            = "wss://ws-subscriptions-clob.polymarket.com/ws/market";

// ══════════════════════════════════════════════════════════════════════════════
//  STARTUP
// ══════════════════════════════════════════════════════════════════════════════
string modeLabel = isLive ? "LIVE EXECUTION" : isDryRun ? "DRY RUN" : "TELEMETRY";
string debugTag  = isDebug ? " +DEBUG" : "";
Console.WriteLine("═══════════════════════════════════════════════════════════");
Console.WriteLine($"  KALSHI ↔ POLYMARKET CROSS-PLATFORM ARB  [{modeLabel}{debugTag}]");
Console.WriteLine("═══════════════════════════════════════════════════════════");

// ── Kalshi auth ───────────────────────────────────────────────────────────────
var kalshiConfig = KalshiApiConfig.FromEnvironment(); // also loads .env into the process environment
// Read proxy after .env is loaded — it's set by LoadDotEnv() inside FromEnvironment().
string polyProxy = (Environment.GetEnvironmentVariable("POLY_SOCKS_PROXY") ?? "").Trim();
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
var restVerifier = new CrossArbRestVerifier(orderClient, telemetry, polyProxy);
telemetry.OnArbOpened += restVerifier.OnArbOpened;

// ── Executor — live order placement on WS-detected arb windows ────────────────
CrossArbExecutor?            executor    = null;
// Concrete dry-run refs kept outside the if-block so key handlers (M/C/E/X) can reach them.
SimulatedKalshiClient?       simKalshi   = null;
SimulatedVenuePositionClient? venueClient = null;
SimulatedPolymarketClient?    simPoly     = null;

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
    const decimal MAX_BET_USD        = 10m;    // max combined dollar cost per arb entry
    const decimal BALANCE_BUFFER_PCT = 0.20m;  // per-platform reserve (fraction of maxBet)

    // In dry-run, probe real credentials before swapping in simulated clients.
    // This surfaces auth/connectivity issues without risking any orders.
    if (isDryRun)
    {
        try
        {
            long    kBal = await orderClient.GetBalanceCentsAsync();
            decimal pBal = await polyOrderClient.GetUsdcBalanceAsync();
            Console.WriteLine($"[CRED CHECK] Kalshi=${kBal / 100m:0.00} Poly=${pBal:0.00} — credentials OK");
        }
        catch (Exception ex)
        {
            Console.WriteLine($"[ERROR] Credential check failed: {ex.Message}");
            Console.WriteLine("[INFO] Fix credentials or use --telemetry for credential-free mode.");
            return;
        }
    }

    // Order execution clients — simulated in dry-run, real in live.
    SimulatedFillProfile? fillProfile = null;
    if (isDryRun)
    {
        try   { fillProfile = FailureScenarios.FromName(scenarioName, fillSeed); }
        catch (ArgumentException ex) { Console.WriteLine($"[ERROR] {ex.Message}"); return; }
        simKalshi   = new SimulatedKalshiClient(fillProfile);
        venueClient = new SimulatedVenuePositionClient(simKalshi);
        simPoly     = new SimulatedPolymarketClient(fillProfile, state.Books);
    }
    PredictionBacktester.Engine.LiveExecution.IKalshiOrderExecutor    kalshiExec =
        isDryRun ? venueClient! : orderClient;
    PredictionBacktester.Engine.LiveExecution.IPolymarketOrderExecutor polyExec   =
        isDryRun ? simPoly!     : polyOrderClient;
    // Dry-run mirrors the full simulated $1,000 balance; live caps concurrent risk tightly.
    decimal       maxExposureUsd     = isDryRun ? 1000m : 50m;
    executor = new CrossArbExecutor(
        kalshi:              kalshiExec,
        poly:                polyExec,
        telemetry:           telemetry,
        books:               state.Books,
        maxBetUsd:           MAX_BET_USD,
        balanceBufferPct:    BALANCE_BUFFER_PCT,
        maxExposureUsd:      maxExposureUsd,
        executionThreshold:  0.990m,
        pairCooldownSeconds: 120,
        fillTimeoutMs:       5000,
        maxDayLossUsd:       20m,
        dryRun:              isDryRun,
        minBuy:              minBuy,
        tryN:                tryN,
        outerCts:            cts);
    telemetry.OnArbOpened += executor.OnArbOpened;
    await executor.InitializeBalancesAsync();
    if (isLive && pairs.Count > 0)
        await executor.ReconcileOnStartupAsync(pairs);
    string execLabel  = isDryRun ? $"DRY RUN [{scenarioName}] — no real orders" : "LIVE";
    string minBuyTag  = minBuy ? "  MIN-BUY=1" : $"  maxBet=${MAX_BET_USD:0.00}";
    Console.WriteLine($"[EXECUTOR] {execLabel} |{minBuyTag} buffer={BALANCE_BUFFER_PCT:P0} maxExposure=${maxExposureUsd:0.00} threshold=0.990 cooldown=120s");
}
else // --telemetry
{
    Console.WriteLine("[EXECUTOR] Telemetry-only mode — no orders will be placed.");
}

// ── Proxy IP verification: confirm proxy routes to a different egress ─────────
// Runs in --debug mode (any mode) and always in --live.
if (isDebug || isLive)
    await CheckPolyProxyAsync(polyProxy, isLive);

Console.WriteLine($"\n[BOOKS] {state.Books.Count} order books created");
Console.WriteLine($"  Kalshi tickers : {kalshiSubscribeTickers.Count}");
Console.WriteLine($"  Poly tokens    : {polySubscribeTokens.Count}");

var knownKalshiTickers = new HashSet<string>(kalshiSubscribeTickers, StringComparer.Ordinal);
var knownPolyTokens    = new HashSet<string>(polySubscribeTokens,    StringComparer.Ordinal);
var knownPairIds       = new HashSet<string>(pairs.Select(p => p.PairId), StringComparer.Ordinal);

Console.CancelKeyPress += (_, e) => { e.Cancel = true; cts.Cancel(); };

// ── Key toggles ────────────────────────────────────────────────────────────
// N/S always active. Debug keys (D/T/B/F/R/H) only meaningful with --debug.
// Silently no-ops if stdin is not a TTY (e.g. screen/tmux without PTY).
Console.WriteLine("[KEYS] N=NearMiss  S=StatusDash" +
    (isDryRun ? "  M=InjectMismatch  C=SimReconnect  E=InjectErrors  X=DropPolyBook" : "") +
    (isDebug  ? "  │  D=Discovery  T=Trades  B=Balance  F=Feed  R=Books  H=DebugStatus" : ""));
_ = Task.Run(() =>
{
    try
    {
        while (!cts.Token.IsCancellationRequested)
        {
            if (!Console.KeyAvailable) { Thread.Sleep(50); continue; }
            var key = Console.ReadKey(intercept: true).Key;
            switch (key)
            {
                case ConsoleKey.N: DebugLog.NearMissEnabled   = !DebugLog.NearMissEnabled;   break;
                case ConsoleKey.S: DebugLog.StatusDashEnabled = !DebugLog.StatusDashEnabled; break;
                case ConsoleKey.D when isDebug: DebugLog.DiscoveryEnabled = !DebugLog.DiscoveryEnabled; break;
                case ConsoleKey.T when isDebug: DebugLog.TradesEnabled    = !DebugLog.TradesEnabled;    break;
                case ConsoleKey.B when isDebug: DebugLog.BalanceEnabled   = !DebugLog.BalanceEnabled;   break;
                case ConsoleKey.F when isDebug: DebugLog.FeedEnabled      = !DebugLog.FeedEnabled;      break;
                case ConsoleKey.R when isDebug: DebugLog.BooksEnabled     = !DebugLog.BooksEnabled;     break;
                case ConsoleKey.M when isDryRun:
                {
                    var firstP = pairs.FirstOrDefault();
                    if (firstP != null && venueClient != null)
                    {
                        venueClient.InjectMismatch(firstP.KalshiTicker, +1);
                        simPoly?.InjectTokenBalanceMismatch(firstP.PolyYesTokenId, +1m);
                        Console.WriteLine($"[KEYS] Mismatch queued for {firstP.Label} — fires on next ReconcileTradeAsync");
                    }
                    else Console.WriteLine("[KEYS] No pairs loaded or venueClient inactive");
                    break;
                }
                case ConsoleKey.C when isDryRun:
                {
                    // Simulate a WS reconnect event: halt, close open telemetry windows, then resume.
                    executor?.HaltForConnectionLoss();
                    telemetry.OnKalshiReconnect();
                    telemetry.OnPolyReconnect();
                    Console.WriteLine("[KEYS] Simulated reconnect — telemetry windows closed, resuming in 500ms");
                    _ = Task.Run(async () =>
                    {
                        await Task.Delay(500);
                        executor?.ResumeFromConnectionLoss();
                        Console.WriteLine("[KEYS] Connection halt cleared — trading resumed");
                    });
                    break;
                }
                case ConsoleKey.E when isDryRun:
                {
                    simKalshi?.InjectMaintenanceErrors(6);
                    Console.WriteLine("[KEYS] Injected 6 Kalshi REST errors — VENUE_MAINTENANCE fires after 5 consecutive");
                    break;
                }
                case ConsoleKey.X when isDryRun:
                {
                    var firstP = pairs.FirstOrDefault();
                    if (firstP != null && state.Books.TryRemove($"P:{firstP.PolyYesTokenId}", out _))
                        Console.WriteLine($"[KEYS] Removed Poly YES book for {firstP.Label} — recovery will see missing book");
                    else
                        Console.WriteLine("[KEYS] No pair loaded or Poly YES book not found in state.Books");
                    break;
                }
            }
            if (key is ConsoleKey.N or ConsoleKey.S)
                Console.WriteLine($"[KEYS] {DebugLog.DisplayStatusLine()}");
            else if (isDebug && key is ConsoleKey.D or ConsoleKey.T or ConsoleKey.B or ConsoleKey.F or ConsoleKey.R or ConsoleKey.H)
                Console.WriteLine($"[DEBUG] {DebugLog.DebugStatusLine()}");
        }
    }
    catch (InvalidOperationException)
    {
        DebugLog.Write("Key toggle listener unavailable — stdin is not a TTY");
    }
});

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

            if (DebugLog.NearMissEnabled)
            {
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
//  CONNECTION WATCHDOG  (live / dry-run only)
// ══════════════════════════════════════════════════════════════════════════════
if (executor != null)
{
    _ = Task.Run(async () =>
    {
        const int WATCHDOG_INTERVAL_MS   = 5_000;
        const int WS_SILENCE_THRESHOLD_S = 60;   // REST-ping when WS connected but silent this long
        bool     lastKOk     = true, lastPOk = true;
        DateTime lastKPingAt = DateTime.MinValue;
        DateTime lastPPingAt = DateTime.MinValue;
        try
        {
            while (!cts.Token.IsCancellationRequested)
            {
                await Task.Delay(WATCHDOG_INTERVAL_MS, cts.Token).ContinueWith(_ => { });
                if (cts.Token.IsCancellationRequested) break;

                bool kOk = kalshiFeed.IsConnected;
                bool pOk = polyFeed.IsConnected;

                // ── WS connect/disconnect transitions ──────────────────────────
                if (!kOk && lastKOk) Console.WriteLine("[WATCHDOG] Kalshi disconnected — halting new trades");
                if (!pOk && lastPOk) Console.WriteLine("[WATCHDOG] Polymarket disconnected — halting new trades");
                if ( kOk && !lastKOk) Console.WriteLine("[WATCHDOG] Kalshi reconnected — resuming trades");
                if ( pOk && !lastPOk) Console.WriteLine("[WATCHDOG] Polymarket reconnected — resuming trades");
                lastKOk = kOk;
                lastPOk = pOk;
                if (!kOk || !pOk) executor.HaltForConnectionLoss();
                else              executor.ResumeFromConnectionLoss();

                // ── Silence detection: WS connected but no messages for 60s ───
                // Distinguish "venue is quiet" (REST succeeds) from "we're cut off" (REST fails).
                var nowDt    = DateTime.UtcNow;
                double kSilS = (nowDt - kalshiFeed.LastMessageAt).TotalSeconds;
                double pSilS = (nowDt - polyFeed  .LastMessageAt).TotalSeconds;

                if (kOk && kSilS >= WS_SILENCE_THRESHOLD_S
                        && (nowDt - lastKPingAt).TotalSeconds >= WS_SILENCE_THRESHOLD_S)
                {
                    lastKPingAt = nowDt;
                    bool kRestOk = await executor.PingKalshiAsync();
                    if (kRestOk)
                        Console.WriteLine($"[WATCHDOG] Kalshi WS silent {kSilS:0}s — REST OK (venue quiet, no arb activity)");
                    else
                    {
                        Console.ForegroundColor = ConsoleColor.Red;
                        Console.WriteLine($"[WATCHDOG ALERT] Kalshi WS silent {kSilS:0}s — REST unreachable. " +
                                          "Possible network cut-off — halting until reconnect.");
                        Console.ResetColor();
                        executor.HaltForConnectionLoss();
                    }
                }

                if (pOk && pSilS >= WS_SILENCE_THRESHOLD_S
                        && (nowDt - lastPPingAt).TotalSeconds >= WS_SILENCE_THRESHOLD_S)
                {
                    lastPPingAt = nowDt;
                    bool pRestOk = await executor.PingPolyAsync();
                    if (pRestOk)
                        Console.WriteLine($"[WATCHDOG] Poly WS silent {pSilS:0}s — REST OK (venue quiet, no arb activity)");
                    else
                    {
                        Console.ForegroundColor = ConsoleColor.Red;
                        Console.WriteLine($"[WATCHDOG ALERT] Poly WS silent {pSilS:0}s — REST unreachable. " +
                                          "Possible network cut-off — halting until reconnect.");
                        Console.ResetColor();
                        executor.HaltForConnectionLoss();
                    }
                }
            }
        }
        catch (Exception ex) { Console.WriteLine($"[WATCHDOG ERROR] {ex.Message}"); }
    });
}

// ══════════════════════════════════════════════════════════════════════════════
//  STATUS DASHBOARD  (live / dry-run only)
// ══════════════════════════════════════════════════════════════════════════════
if (executor != null)
{
    _ = Task.Run(async () =>
    {
        try
        {
            while (!cts.Token.IsCancellationRequested)
            {
                await Task.Delay(STATUS_DASH_INTERVAL_MS, cts.Token).ContinueWith(_ => { });
                if (cts.Token.IsCancellationRequested || !DebugLog.StatusDashEnabled) continue;

                int kReady = state.Books.Count(kv => kv.Key.StartsWith("K:") && kv.Value.HasReceivedDelta);
                int pReady = state.Books.Count(kv => kv.Key.StartsWith("P:") && kv.Value.HasReceivedDelta);
                int kTotal = state.Books.Count(kv => kv.Key.StartsWith("K:"));
                int pTotal = state.Books.Count(kv => kv.Key.StartsWith("P:"));

                decimal proj = executor.TotalProjectedProfit;
                string projStr = (proj >= 0 ? "+" : "") + $"${proj:0.00}";
                string haltTag = executor.IsHalted           ? "  [HALTED — manual reset required]"
                               : executor.IsConnectionHalted ? "  [CONN HALT — waiting for reconnect]"
                               : "";
                string tryTag  = executor.TriesRemaining >= 0 ? $"  triesLeft={executor.TriesRemaining}" : "";
                Console.WriteLine(
                    $"[STATUS {DateTime.UtcNow:HH:mm:ss}] " +
                    $"K=${executor.KalshiBalanceUsd:0.00}  P=${executor.PolyBalanceUsd:0.00}  │  " +
                    $"invested=${executor.TotalInvested:0.00}  proj={projStr}  │  " +
                    $"exposure=${executor.TotalExposure:0.00}/${executor.MaxExposureUsd:0.00}  │  " +
                    $"open={executor.OpenPositionCount}  filled={executor.TotalExecuted}  earlyExit={executor.EarlyExitsCompleted}  │  " +
                    $"books K={kReady}/{kTotal} P={pReady}/{pTotal}" +
                    $"  WS K={kalshiFeed.IsConnected} P={polyFeed.IsConnected}" +
                    $"  dayLoss=${executor.DayLossUsd:0.00}/${executor.MaxDayLossUsd:0.00}" +
                    $"  cleanup=${executor.TotalCleanupCostUsd:0.00}" +
                    $"{tryTag}{haltTag}");
            }
        }
        catch (Exception ex)
        {
            Console.WriteLine($"[STATUS DASH ERROR] {ex.Message}");
        }
    });
}

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
var bookRefresher = new BookRefresherService(state.Books, orderClient, polyProxy);
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

static async Task CheckPolyProxyAsync(string socksProxy, bool isLive)
{
    const string ipUrl = "https://api.ipify.org?format=text";

    string localIp = "?";
    try
    {
        using var direct = new HttpClient { Timeout = TimeSpan.FromSeconds(5) };
        localIp = (await direct.GetStringAsync(ipUrl)).Trim();
    }
    catch (Exception ex)
    {
        Console.WriteLine($"[PROXY CHECK] Local IP lookup failed: {ex.Message}");
    }

    if (string.IsNullOrEmpty(socksProxy))
    {
        string liveWarn = isLive ? " — WARN: --live mode without proxy; Polymarket may geo-block" : "";
        Console.WriteLine($"[PROXY CHECK] No POLY_SOCKS_PROXY — Polymarket REST calls will use local IP ({localIp}){liveWarn}");
        return;
    }

    string proxyIp = "?";
    try
    {
        var handler = new HttpClientHandler
        {
            Proxy    = new System.Net.WebProxy(socksProxy),
            UseProxy = true
        };
        using var proxied = new HttpClient(handler) { Timeout = TimeSpan.FromSeconds(8) };
        proxyIp = (await proxied.GetStringAsync(ipUrl)).Trim();
    }
    catch (Exception ex)
    {
        Console.WriteLine($"[PROXY CHECK FAIL] Proxy {socksProxy} unreachable: {ApiErrorHelper.ClassifyPoly(ex)}");
        if (isLive)
            Console.WriteLine("[PROXY CHECK WARN] --live mode — Polymarket REST calls may fall back to local IP or fail");
        return;
    }

    if (proxyIp != localIp && proxyIp != "?")
        Console.WriteLine($"[PROXY CHECK OK] localIP={localIp} → proxyIP={proxyIp} — different egress confirmed ✓");
    else
        Console.WriteLine($"[PROXY CHECK WARN] localIP={localIp} proxyIP={proxyIp} — same IP! Proxy may not be tunneling traffic");
}

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
        RpcUrl        = (Environment.GetEnvironmentVariable("POLY_RPC_URL")       ?? "https://polygon-rpc.com").Trim(),
        SocksProxy    = (Environment.GetEnvironmentVariable("POLY_SOCKS_PROXY")   ?? "").Trim(),
    };
}
