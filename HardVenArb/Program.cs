using System.Text.Json;
using HardVenArb;
using PredictionBacktester.Engine;
using PredictionBacktester.Engine.LiveExecution;

// ══════════════════════════════════════════════════════════════════════════════
//  USAGE
//
//  dotnet run --project HardVenArb -- --telemetry          # detect arbs, log CSV, no orders
//  dotnet run --project HardVenArb -- --dry-run            # same as --live, log only, no real orders
//  dotnet run --project HardVenArb -- --live               # full production: real orders on both legs
//  dotnet run --project HardVenArb -- --telemetry --debug  # any mode can add --debug for verbose logs
//  dotnet run --project HardVenArb -- --dry-run --try 5    # execute exactly 5 arbs then exit cleanly
//  dotnet run --project HardVenArb -- --live --min-buy     # live: always 1 contract, ignore maxBet sizing
//  dotnet run --project HardVenArb -- --dry-run --seed 42  # reproducible dry-run (same fills every time)
//  dotnet run --project HardVenArb -- --dry-run --scenario FlakyKalshi  # named failure profile
//  dotnet run --project HardVenArb -- --live --single-entry  # one open position per pair; re-entry allowed after close
//  dotnet run --project HardVenArb -- --live --log           # append failed-execution output to error_log.txt
//
//  Exactly one mode flag is required. All others are optional.
//  --try N         execute exactly N complete arbs then shut down; works with --dry-run or --live.
//  --min-buy       cap every arb to exactly 1 contract regardless of maxBet (useful for initial live shakedown).
//  --single-entry  one open position per pair at a time; re-entry allowed once the position closes (exit or settlement).
//  --wN            rolling execution window: only execute arbs whose Kalshi close date is within N weeks of today
//                  (e.g. --w2). Re-checked live each attempt, so far-out pairs become eligible as they approach.
//  --log           capture full console output for failed executions and append to error_log.txt.
//  --seed N        seed the dry-run fill RNG for reproducible results; omit for non-deterministic simulation.
//  --scenario Name named failure profile for dry-run (default: HappyPath).
//                  Valid: HappyPath, FlakyKalshi, FlakyHardVen, ChronicSlippage,
//                         PartialFillSwamp, BothVenuesFlaky, LatencyStorm
//
//  Runtime key toggles (bare keypresses — works in tmux, SSH, screen):
//    N   toggle near-miss top-10 report   (on by default)
//    A   toggle status dashboard          (on by default; live/dry-run only)
//    U   inject +1 position mismatch      (dry-run only; fires on next ReconcileTradeAsync → halt)
//    K   simulate WS reconnect            (dry-run only; closes arb windows, resumes after 500ms)
//    E   inject 6 Kalshi REST errors      (dry-run only; triggers VENUE_MAINTENANCE halt at 5+)
//    X   drop first pair's HardVen YES book  (dry-run only; simulates book-missing during recovery)
//
//  --debug additional key toggles:
//    G   toggle Discovery logs  — arb window detection events
//    T   toggle Trades logs     — order execution events
//    W   toggle Balance logs    — balance fetch / refresh events
//    F   toggle Feed logs       — WebSocket connect / message events
//    R   toggle Books logs      — REST book-refresh events
//
//  Required env vars (Kalshi):
//    KALSHI_API_KEY_ID          Kalshi API key ID
//    KALSHI_PRIVATE_KEY_PATH    Path to RSA private key PEM file
//
//  Required env vars (HardVen execution — omit for telemetry-only mode):
//    HARDVEN_API_KEY               HardVen CLOB API key
//    HARDVEN_API_SECRET            HardVen CLOB API secret
//    HARDVEN_API_PASSPHRASE        HardVen CLOB API passphrase
//    HARDVEN_PRIVATE_KEY           EOA private key (hex, no 0x prefix)
//    HARDVEN_PROXY_ADDRESS         Gnosis Safe proxy wallet address (HARDVEN_GNOSIS_SAFE signer)
//    HARDVEN_RPC_URL               (optional) HardVengon RPC — defaults to https://hardvengon-rpc.com
//    HARDVEN_SOCKS_PROXY           (optional) SOCKS5 proxy for HardVen REST — socks5://host:port
//                               Balance fetches + order execution route through this proxy.
//                               WebSocket feed connects directly (no proxy).
//                               Omit if running from an unrestricted IP (e.g. US cloud server).
//
//  Optional env vars (runtime / telemetry):
//    HARDVEN_SIDECAR_URL           HardVen odds sidecar base URL (default http://127.0.0.1:8787)
//    HARDVEN_FX_TO_USD             USD per HardVen book-unit for EUR→USD size (default 1.0; ~1.08 for the EUR account)
//    HARDVEN_HEDGE_MONITOR_SECS    seconds to sample the post-open Kalshi unwind price for the hedge tape (default 30; 0 = off)
//    HARDVEN_KEEP_AWAKE            1 = suppress system sleep while running (default 1, Windows-only); 0 to disable
//
//  cross_pairs.json: verified Kalshi↔HardVen market pairs; auto-populated on scan,
//                    must be non-empty for arb detection to fire.
//                    (Sidecar-side pairing + the unattended feed supervisor / keep-awake are documented in STARTUP.md.)
//
//  Output: CrossArbTelemetry_*.csv    — all detected arb windows (always)
//          CrossArbHedgeMonitor_*.csv — post-open Kalshi unwind trajectory for the failed-leg hedge model (analyze_cross_arb.py §6)
//          CrossArbExecution_*.csv    — order execution results (when executor active)
// ══════════════════════════════════════════════════════════════════════════════

// ══════════════════════════════════════════════════════════════════════════════
//  MODE SELECTION
// ══════════════════════════════════════════════════════════════════════════════
// ── Quick diagnostic: fetch + print raw positions response then exit ──────────
if (args.Contains("--positions-check"))
{
    var cfg = KalshiApiConfig.FromEnvironment();
    using var client = new KalshiOrderClient(cfg);
    client.RawResponseLogger = (path, body) =>
    {
        Console.WriteLine($"\n=== RAW {path} (total {body.Length} chars) ===");
        // Print first 2000 chars to show event_positions header
        Console.WriteLine(body.Length > 2000 ? body[..2000] + "\n[…trimmed…]" : body);
        // Specifically locate and print market_positions
        int mpIdx = body.IndexOf("\"market_positions\"", StringComparison.Ordinal);
        if (mpIdx < 0)
            Console.WriteLine("\n*** market_positions key NOT FOUND in response ***");
        else
        {
            int end = Math.Min(mpIdx + 3000, body.Length);
            Console.WriteLine($"\n--- market_positions (at char {mpIdx}) ---");
            Console.WriteLine(body[mpIdx..end] + (end < body.Length ? "…" : ""));
        }
        Console.WriteLine("=== END ===");
    };
    var sw = System.Diagnostics.Stopwatch.StartNew();
    try
    {
        var positions = await client.GetPositionsAsync();
        Console.WriteLine($"\nParsed {positions.Count} position(s) in {sw.ElapsedMilliseconds}ms");
        foreach (var (t, p) in positions) Console.WriteLine($"  {t} = {p}");
    }
    catch (Exception ex) { Console.WriteLine($"\nTHREW: {ex.GetType().Name}: {ex.Message}"); }
    return;
}

bool isLive      = args.Contains("--live");
bool isDryRun    = args.Contains("--dry-run");
bool isTelemetry = args.Contains("--telemetry");

int modeCount = (isLive ? 1 : 0) + (isDryRun ? 1 : 0) + (isTelemetry ? 1 : 0);
if (modeCount == 0)
{
    Console.WriteLine("Usage: HardVenArb --telemetry | --dry-run | --live");
    Console.WriteLine("  --telemetry   detect arbs, log CSV, no orders (no HARDVEN_* env vars needed)");
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

// --single-entry: one open position per pair at a time (re-entry allowed after close)
bool singleEntry = args.Contains("--single-entry");

// --log: capture all console output for failed executions and append to error_log.txt
bool logErrors = args.Contains("--log");

// --exclude tennis,cricket,...  : skip pairs whose Kalshi ticker matches these sports/series (cleaner
// telemetry). Accepts friendly sport names (via the alias map) or any raw ticker substring (e.g. KXATP).
var excludeSubs = new List<string>();
int excludeIdx = Array.IndexOf(args, "--exclude");
if (excludeIdx >= 0 && excludeIdx + 1 < args.Length)
{
    var sportAliases = new Dictionary<string, string[]>(StringComparer.OrdinalIgnoreCase)
    {
        ["tennis"]     = new[] { "ATPMATCH", "WTAMATCH", "ITFMATCH", "ITFWMATCH", "ATPCHALLENGERMATCH", "WTACHALLENGERMATCH" },
        ["baseball"]   = new[] { "MLBGAME", "KBOGAME", "NPBGAME" },
        ["basketball"] = new[] { "NBAGAME", "WNBAGAME", "ACBGAME", "BBLGAME", "BSLGAME", "NCAABBGAME", "BIG3GAME" },
        ["cricket"]    = new[] { "T20MATCH", "WT20MATCH", "ODIMATCH", "TESTMATCH", "COUNTYCHAMPMATCH" },
        ["soccer"]     = new[] { "WCGAME", "USLGAME", "USLCUPGAME", "LALIGA2GAME", "CHLLDPGAME", "BOLPDIVGAME" },
        ["football"]   = new[] { "NFLGAME", "NCAAFGAME", "CFLGAME" },
        ["afl"]        = new[] { "AFLGAME" },
        ["boxing"]     = new[] { "BOXING" },
        ["ufc"]        = new[] { "UFCFIGHT" },
        ["mma"]        = new[] { "UFCFIGHT" },
    };
    foreach (var term in args[excludeIdx + 1].Split(',', StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries))
        if (sportAliases.TryGetValue(term, out var subs)) excludeSubs.AddRange(subs);
        else excludeSubs.Add(term.ToUpperInvariant());
}
bool IsExcludedTicker(string ticker) =>
    excludeSubs.Count > 0 && excludeSubs.Any(((ticker ?? "").ToUpperInvariant()).Contains);
if (excludeSubs.Count > 0)
    Console.WriteLine($"[CONFIG] --exclude active — skipping tickers containing: {string.Join(", ", excludeSubs)}");

// --wN: rolling execution window — only execute arbs whose Kalshi close date is within N weeks of today.
// Evaluated live in the executor (not a startup filter), so the window rolls forward each day and far-out
// pairs become eligible as they approach. 0 = no window (all dates eligible).
int execWindowWeeks = 0;
foreach (var a in args)
    if (a.Length > 3 && a.StartsWith("--w", StringComparison.OrdinalIgnoreCase) && int.TryParse(a.AsSpan(3), out int wkN) && wkN > 0)
    { execWindowWeeks = wkN; break; }
if (execWindowWeeks > 0)
    Console.WriteLine($"[WINDOW] Execution limited to arbs settling within {execWindowWeeks} week(s) (rolling, by Kalshi close date).");

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
const int     HARDVEN_BATCH_SIZE       = 200;
// /odds is now an instant cache read (the sidecar refreshes the book in the background), so the bot can
// poll fast and cheaply. Default 3s; override with HARDVEN_POLL_MS. NOTE: actual quote freshness is set by
// the sidecar's BOOKMAKER_REFRESH_SEC — this only controls how often the bot pulls the latest cached book.
int           HARDVEN_PING_INTERVAL_MS =
    int.TryParse(Environment.GetEnvironmentVariable("HARDVEN_POLL_MS"), out var _hvPollMs) && _hvPollMs > 0
        ? _hvPollMs : 3_000;
const int     NEAR_MISS_INTERVAL_MS  = 60_000;
const int     STATUS_DASH_INTERVAL_MS = 30_000;
// HARDVEN_SIDECAR_URL is read after .env is loaded (see below, next to the proxy read) — reading it here
// would predate LoadDotEnv() and silently ignore any value in .env.

// ══════════════════════════════════════════════════════════════════════════════
//  STARTUP
// ══════════════════════════════════════════════════════════════════════════════
string modeLabel = isLive ? "LIVE EXECUTION" : isDryRun ? "DRY RUN" : "TELEMETRY";
string debugTag  = isDebug ? " +DEBUG" : "";
Console.WriteLine("═══════════════════════════════════════════════════════════");
Console.WriteLine($"  KALSHI ↔ HARDVEN CROSS-PLATFORM ARB  [{modeLabel}{debugTag}]");
Console.WriteLine("═══════════════════════════════════════════════════════════");

// ── Kalshi auth ───────────────────────────────────────────────────────────────
var kalshiConfig = KalshiApiConfig.FromEnvironment(); // also loads .env into the process environment
// Read proxy after .env is loaded — it's set by LoadDotEnv() inside FromEnvironment().
string hardvenProxy = (Environment.GetEnvironmentVariable("HARDVEN_SOCKS_PROXY") ?? "").Trim();
// Same: read the sidecar URL after .env loads so a .env override is honoured on the server.
string HARDVEN_SIDECAR_URL = (Environment.GetEnvironmentVariable("HARDVEN_SIDECAR_URL") ?? "http://127.0.0.1:8787").Trim();

// Discord webhook alerter (halts, naked-position failures, low cash). .env is loaded above, so the
// URL is in the process env now; an unset/empty URL leaves it a disabled no-op.
var discord = new DiscordNotifier(Environment.GetEnvironmentVariable("DISCORD_WEBHOOK_URL"));
if (discord.Enabled) Console.WriteLine("[DISCORD] webhook alerts enabled");
if (string.IsNullOrEmpty(kalshiConfig.ApiKeyId) || string.IsNullOrEmpty(kalshiConfig.PrivateKeyPath))
{
    Console.WriteLine("[ERROR] Set KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH environment variables.");
    return;
}

using var orderClient = new KalshiOrderClient(kalshiConfig);
if (isDebug)
    orderClient.RawResponseLogger = (path, body) => DebugLog.Books($"[KALSHI REST] {path}\n{body}");
else
    orderClient.RawResponseLogger = (path, body) =>
    {
        if (path.Contains("/portfolio/positions"))
            Console.WriteLine($"[KALSHI RAW {DateTime.UtcNow:HH:mm:ss}] {path}\n{body[..Math.Min(1200, body.Length)]}");
    };
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
//   { "kalshi_ticker": "KXFOO", "hardven_yes_token": "abc...", "hardven_no_token": "def...", "label": "..." }
// These are merged with auto-discovered pairs and always included regardless of score.
var manualPairs = new List<CrossPair>();
// In dev builds AppContext.BaseDirectory = bin/Debug/net10.0/ — the output copy of
// cross_pairs.json is stale; pair_markets.py writes to the project source dir 3 levels up.
// Detect dev by looking for a .csproj file there; production published builds have none.
string outputDirFile = Path.Combine(AppContext.BaseDirectory, "cross_pairs.json");
string sourceDir     = Path.GetFullPath(Path.Combine(AppContext.BaseDirectory, "../../.."));
string sourceDirFile = Path.Combine(sourceDir, "cross_pairs.json");
bool   isDevBuild    = Directory.GetFiles(sourceDir, "*.csproj").Length > 0;
// Dev build → the auto-pairer writes cross_pairs.json to the SOURCE dir (HardVenArb/). Point the reload path
// there even when the file doesn't exist YET (the first auto-pair run creates it), so the hot-reload actually
// finds it — otherwise a fresh setup with HARDVEN_AUTO_PAIR would freeze onto a CWD path and never load pairs.
string manualPath = isDevBuild ? sourceDirFile
                               : (File.Exists(outputDirFile) ? outputDirFile : "cross_pairs.json");
if (File.Exists(manualPath))
{
    try
    {
        using var manDoc = JsonDocument.Parse(File.ReadAllText(manualPath));
        int excludedCount = 0;
        foreach (var el in manDoc.RootElement.EnumerateArray())
        {
            string kTicker  = el.TryGetProperty("kalshi_ticker",  out var kt)  ? (kt.GetString()  ?? "") : "";
            if (IsExcludedTicker(kTicker)) { excludedCount++; continue; }
            string yesToken = el.TryGetProperty("hardven_yes_token", out var yt)  ? (yt.GetString()  ?? "") : "";
            string noToken  = el.TryGetProperty("hardven_no_token",  out var nt)  ? (nt.GetString()  ?? "") : "";
            string label    = el.TryGetProperty("label",          out var lb)  ? (lb.GetString()  ?? "") : kTicker;
            string eventId  = el.TryGetProperty("event_id",       out var eid) ? (eid.GetString() ?? "") : "";
            DateOnly? settlementDate = null;
            if (el.TryGetProperty("settlement_date", out var sd) && DateOnly.TryParse(sd.GetString(), out var d))
                settlementDate = d;
            bool isNegRisk = el.TryGetProperty("is_neg_risk", out var nr) && nr.ValueKind == JsonValueKind.True;
            decimal hardvenMinSize = el.TryGetProperty("hardven_min_size", out var ms) && ms.TryGetDecimal(out decimal msv) && msv > 0 ? msv : 1.0m;
            bool threeWay = el.TryGetProperty("three_way", out var tw) && tw.ValueKind == JsonValueKind.True;
            if (!string.IsNullOrEmpty(kTicker) && !string.IsNullOrEmpty(yesToken) && !string.IsNullOrEmpty(noToken))
            {
                string pairId = $"MANUAL_{kTicker}__{yesToken[..Math.Min(8, yesToken.Length)]}";
                manualPairs.Add(new CrossPair(pairId, label, kTicker, yesToken, noToken, eventId, settlementDate, isNegRisk, hardvenMinSize, threeWay));
            }
        }
        Console.WriteLine($"[CONFIG] {manualPairs.Count} manual pair(s) loaded from cross_pairs.json"
                          + (excludedCount > 0 ? $" ({excludedCount} skipped by --exclude)" : ""));
    }
    catch (Exception ex)
    {
        Console.WriteLine($"[CONFIG WARN] Could not parse cross_pairs.json: {ex.Message}");
    }
}

// Also load derivative_pairs.json (spread/total LINES from pair_derivatives.py) — SAME schema, kept in a
// separate file so the moneyline pairs stay clean. Merged into manualPairs here so the run prices the lines
// too (the sidecar resolves the {lid}:{mid}:{type}:{points}:{side} tokens). Hot-reloaded alongside
// cross_pairs.json (below), so a daily auto-pair refreshes the derivative lines live too.
string derivSrc  = Path.Combine(sourceDir, "derivative_pairs.json");
string derivOut  = Path.Combine(AppContext.BaseDirectory, "derivative_pairs.json");
string derivPath = isDevBuild ? derivSrc
                              : (File.Exists(derivOut) ? derivOut : "derivative_pairs.json");
if (File.Exists(derivPath))
{
    try
    {
        using var derivDoc = JsonDocument.Parse(File.ReadAllText(derivPath));
        int before = manualPairs.Count, derivExcluded = 0;
        foreach (var el in derivDoc.RootElement.EnumerateArray())
        {
            string kTicker  = el.TryGetProperty("kalshi_ticker",  out var kt)  ? (kt.GetString()  ?? "") : "";
            if (IsExcludedTicker(kTicker)) { derivExcluded++; continue; }
            string yesToken = el.TryGetProperty("hardven_yes_token", out var yt)  ? (yt.GetString()  ?? "") : "";
            string noToken  = el.TryGetProperty("hardven_no_token",  out var nt)  ? (nt.GetString()  ?? "") : "";
            string label    = el.TryGetProperty("label",          out var lb)  ? (lb.GetString()  ?? "") : kTicker;
            string eventId  = el.TryGetProperty("event_id",       out var eid) ? (eid.GetString() ?? "") : "";
            DateOnly? settlementDate = null;
            if (el.TryGetProperty("settlement_date", out var sd3) && DateOnly.TryParse(sd3.GetString(), out var d3))
                settlementDate = d3;
            bool isNegRisk = el.TryGetProperty("is_neg_risk", out var nr) && nr.ValueKind == JsonValueKind.True;
            decimal hardvenMinSize = el.TryGetProperty("hardven_min_size", out var ms) && ms.TryGetDecimal(out decimal msv) && msv > 0 ? msv : 1.0m;
            if (!string.IsNullOrEmpty(kTicker) && !string.IsNullOrEmpty(yesToken) && !string.IsNullOrEmpty(noToken))
            {
                // pairId includes the YES token prefix → unique per line (many lines share one kTicker prefix).
                string pairId = $"MANUAL_{kTicker}__{yesToken[..Math.Min(8, yesToken.Length)]}";
                manualPairs.Add(new CrossPair(pairId, label, kTicker, yesToken, noToken, eventId, settlementDate, isNegRisk, hardvenMinSize, false));
            }
        }
        Console.WriteLine($"[CONFIG] {manualPairs.Count - before} derivative pair(s) loaded from derivative_pairs.json"
                          + (derivExcluded > 0 ? $" ({derivExcluded} skipped by --exclude)" : ""));
    }
    catch (Exception ex)
    {
        Console.WriteLine($"[CONFIG WARN] Could not parse derivative_pairs.json: {ex.Message}");
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
    Console.WriteLine("[WARN] No pairs found. Add entries to cross_pairs.json or wait for more Kalshi/HardVen market overlap.");
    Console.WriteLine("[INFO] To generate new pairs, run: python HardVenArb/pair_markets.py");
}

// ── Build shared order books ──────────────────────────────────────────────────
var kalshiSubscribeTickers = pairs.Select(p => p.KalshiTicker).Distinct().ToList();
var hardvenSubscribeTokens    = pairs.SelectMany(p => new[] { p.HardVenYesTokenId, p.HardVenNoTokenId }).Distinct().ToList();

var state = new MarketStateTracker();
foreach (var ticker in kalshiSubscribeTickers) state.InitKalshiMarket(ticker);
foreach (var token  in hardvenSubscribeTokens)    state.InitHardVenToken(token);

// ── Telemetry strategy ────────────────────────────────────────────────────────
var telemetry = new CrossPlatformArbTelemetryStrategy(pairs, state.Books, ARB_THRESHOLD, DEPTH_FLOOR);

// ── REST verifier — confirms arb windows via independent REST calls ───────────
var restVerifier = new CrossArbRestVerifier(orderClient, telemetry, hardvenProxy, HARDVEN_SIDECAR_URL);
telemetry.OnArbOpened += restVerifier.OnArbOpened;

// ── Executor — live order placement on WS-detected arb windows ────────────────
CrossArbExecutor?            executor    = null;
// Concrete dry-run refs kept outside the if-block so key handlers (M/C/E/X) can reach them.
SimulatedKalshiClient?       simKalshi   = null;
SimulatedVenuePositionClient? venueClient = null;

if (isLive || isDryRun)
{
    // HardVen venue is a stub (scaffold). It constructs without creds; read-only calls return benign
    // defaults so the bot boots, and order placement throws NotImplementedException until implemented.
    var hardvenConfig = HardVenApiConfig.FromEnvironment();
    if (!hardvenConfig.IsConfigured)
        Console.WriteLine("[WARN] HARDVEN_* not set — HardVen is a stub venue (no live orders). " +
                          "Implement HardVenOrderClient + HardVenWebsocketFeed to enable execution.");
    var hardvenOrderClient = new HardVenOrderClient(hardvenConfig);
    const decimal MAX_BET_USD          = 30m;    // max combined dollar cost per arb entry
    const decimal BALANCE_BUFFER_PCT   = 0.20m;  // per-platform reserve (fraction of maxBet)
    const decimal EXECUTION_THRESHOLD  = 0.995m; // net-cost ceiling for arb detection
    const decimal EXEC_NET_FLOOR       = 0.985m; // minimum net to attempt execution (1.5¢/set profit floor); Kalshi always gets ask+1¢ limit regardless
    const decimal MIN_PLAUSIBLE_NET    = 0.90m;  // reject arbs cheaper than this: a >10% "edge" signals a mispriced/mismatched pair (JOR), not a real arb
    const decimal LOW_BALANCE_ALERT_USD = 15m;   // Discord-alert when either venue's cash drops below this

    // Recovery / halt policy. Ops rule: only halt on the daily-loss tripwire, a manual stop, or a network
    // error — never on a naked leg. A naked/partial leg is hedged if still ≤ break-even, else swept out;
    // if it truly can't flatten (venue paused) the pair is orphaned and the bot keeps running.
    const decimal HEDGE_MAX_NET        = 1.0m;   // complete a hedge only if net ≤ this (1.0 = break-even; raise to tolerate worse hedges when capital is ample)
    const int     REVERSE_FLOOR_CENTS  = 1;      // relentless reverse sweeps the book down to this price to guarantee a fill
    const int     REVERSE_MAX_ATTEMPTS = 4;      // sweep attempts before orphaning the remainder
    const decimal TRADE_MAX_LOSS_MULT  = 3.0m;   // per-trade tripwire: halt if a single (hedged) fill lands >Nx worse than its edge
    const bool    PER_TRADE_TRIPWIRE   = true;   // enable the per-trade tripwire above

    // In dry-run, probe real credentials before swapping in simulated clients.
    // This surfaces auth/connectivity issues without risking any orders.
    if (isDryRun)
    {
        try
        {
            long kBal = await orderClient.GetBalanceCentsAsync();
            Console.WriteLine($"[CRED CHECK] Kalshi=${kBal / 100m:0.00} — credentials OK (HardVen=stub)");
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
    }
    PredictionBacktester.Engine.LiveExecution.IKalshiOrderExecutor kalshiExec =
        isDryRun ? venueClient! : orderClient;
    // Stub venue: same client in all modes (no HardVen sim in this scaffold).
    IHardVenOrderExecutor hardvenExec = hardvenOrderClient;
    // Total combined open-exposure cap. $1,000 = full deployment of the $500/platform capital;
    // the per-platform balance/buffer checks still gate each side so neither venue overdraws.
    decimal       maxExposureUsd     = 1000m;
    executor = new CrossArbExecutor(
        kalshi:              kalshiExec,
        hardven:                hardvenExec,
        telemetry:           telemetry,
        books:               state.Books,
        maxBetUsd:           MAX_BET_USD,
        balanceBufferPct:    BALANCE_BUFFER_PCT,
        maxExposureUsd:      maxExposureUsd,
        executionThreshold:  EXECUTION_THRESHOLD,
        execNetFloor:        EXEC_NET_FLOOR,
        pairCooldownSeconds: 120,
        fillTimeoutMs:       5000,
        maxDayLossUsd:       20m,
        dryRun:              isDryRun,
        minBuy:              minBuy,
        singleEntry:         singleEntry,
        logErrors:           logErrors,
        tryN:                tryN,
        outerCts:            cts,
        hardvenTickSizes:       restVerifier.HardVenTickSizes,
        restVerifier:        restVerifier,
        hedgeMaxNet:         HEDGE_MAX_NET,
        reverseFloorCents:   REVERSE_FLOOR_CENTS,
        reverseMaxAttempts:  REVERSE_MAX_ATTEMPTS,
        tradeMaxLossMult:    TRADE_MAX_LOSS_MULT,
        perTradeTripwire:    PER_TRADE_TRIPWIRE,
        minPlausibleNet:     MIN_PLAUSIBLE_NET,
        discord:             discord,
        lowBalanceAlertUsd:  LOW_BALANCE_ALERT_USD,
        executionWindowWeeks: execWindowWeeks);
    telemetry.OnArbOpened  += executor.OnArbOpened;
    telemetry.BookUpdated  += executor.OnBookUpdate;  // event-driven early exit checks
    await executor.InitializeBalancesAsync();
    if (isLive && pairs.Count > 0)
        await executor.ReconcileOnStartupAsync(pairs);
    string execLabel  = isDryRun ? $"DRY RUN [{scenarioName}] — no real orders" : "LIVE";
    string minBuyTag  = minBuy ? "  MIN-BUY=1" : $"  maxBet=${MAX_BET_USD:0.00}";
    Console.WriteLine($"[EXECUTOR] {execLabel} |{minBuyTag} buffer={BALANCE_BUFFER_PCT:P0} maxExposure=${maxExposureUsd:0.00} threshold={EXECUTION_THRESHOLD:0.000} cooldown=120s");
}
else // --telemetry
{
    Console.WriteLine("[EXECUTOR] Telemetry-only mode — no orders will be placed.");
}

// ── Proxy IP verification: confirm proxy routes to a different egress ─────────
// Runs in --debug mode (any mode) and always in --live.
if (isDebug || isLive)
    await CheckHardVenProxyAsync(hardvenProxy, isLive);

Console.WriteLine($"\n[BOOKS] {state.Books.Count} order books created");
Console.WriteLine($"  Kalshi tickers : {kalshiSubscribeTickers.Count}");
Console.WriteLine($"  HardVen tokens    : {hardvenSubscribeTokens.Count}");

var knownKalshiTickers = new HashSet<string>(kalshiSubscribeTickers, StringComparer.Ordinal);
var knownHardVenTokens    = new HashSet<string>(hardvenSubscribeTokens,    StringComparer.Ordinal);
var knownPairIds       = new HashSet<string>(pairs.Select(p => p.PairId), StringComparer.Ordinal);

// Survive a STRAY Ctrl+C/Break (the terminal/host can deliver one we didn't type, which was killing
// unattended runs). A single signal is logged + IGNORED; a deliberate quit needs a SECOND within 3s.
int ctrlCCount = 0;
DateTime lastCtrlC = DateTime.MinValue;
Console.CancelKeyPress += (_, e) =>
{
    e.Cancel = true;   // always prevent the default abrupt termination
    var now = DateTime.UtcNow;
    if ((now - lastCtrlC).TotalSeconds > 3) ctrlCCount = 0;
    lastCtrlC = now;
    if (++ctrlCCount >= 2)
    {
        Console.WriteLine($"[SIGNAL] Second Ctrl+C/Break ({e.SpecialKey}) within 3s — shutting down.");
        cts.Cancel();
    }
    else
    {
        Console.WriteLine($"[SIGNAL] Ctrl+C/Break received ({e.SpecialKey}) — IGNORED. Press again within 3s to quit. " +
                          "(If you didn't press it, the terminal/host delivered a stray signal — now harmless.)");
    }
};

// ── Key toggles ────────────────────────────────────────────────────────────
// Bare keypresses (no Ctrl) — works in tmux, SSH, and screen sessions.
Console.WriteLine("[KEYS] N=NearMiss  A=StatusDash" +
    (isDryRun ? "  U=InjectMismatch  K=SimReconnect  E=InjectErrors  X=DropHardVenBook" : "") +
    (isDebug  ? "  │  G=Discovery  T=Trades  W=Balance  F=Feed  R=Books" : ""));
_ = Task.Run(() =>
{
    try
    {
        while (!cts.Token.IsCancellationRequested)
        {
            if (!Console.KeyAvailable) { Thread.Sleep(50); continue; }
            var info = Console.ReadKey(intercept: true);
            var key  = info.Key;
            switch (key)
            {
                case ConsoleKey.N: DebugLog.NearMissEnabled   = !DebugLog.NearMissEnabled;   break;
                case ConsoleKey.A: DebugLog.StatusDashEnabled = !DebugLog.StatusDashEnabled; break;
                case ConsoleKey.G when isDebug: DebugLog.DiscoveryEnabled = !DebugLog.DiscoveryEnabled; break;
                case ConsoleKey.T when isDebug: DebugLog.TradesEnabled    = !DebugLog.TradesEnabled;    break;
                case ConsoleKey.W when isDebug: DebugLog.BalanceEnabled   = !DebugLog.BalanceEnabled;   break;
                case ConsoleKey.F when isDebug: DebugLog.FeedEnabled      = !DebugLog.FeedEnabled;      break;
                case ConsoleKey.R when isDebug: DebugLog.BooksEnabled     = !DebugLog.BooksEnabled;     break;
                case ConsoleKey.U when isDryRun:
                {
                    if (executor != null)
                        executor.QueueMismatchOnNextTrade();
                    else
                        Console.WriteLine("[KEYS] Executor not active — start --dry-run first");
                    break;
                }
                case ConsoleKey.K when isDryRun:
                {
                    // Simulate a WS reconnect event: halt, close open telemetry windows, then resume.
                    executor?.HaltForConnectionLoss();
                    telemetry.OnKalshiReconnect();
                    telemetry.OnHardVenReconnect();
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
                    if (firstP != null && state.Books.TryRemove($"H:{firstP.HardVenYesTokenId}", out _))
                        Console.WriteLine($"[KEYS] Removed HardVen YES book for {firstP.Label} — recovery will see missing book");
                    else
                        Console.WriteLine("[KEYS] No pair loaded or HardVen YES book not found in state.Books");
                    break;
                }
            }
            if (key is ConsoleKey.N or ConsoleKey.A)
                Console.WriteLine($"[KEYS] {DebugLog.DisplayStatusLine()}");
            else if (isDebug && key is ConsoleKey.G or ConsoleKey.T or ConsoleKey.W or ConsoleKey.F or ConsoleKey.R or ConsoleKey.P)
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
            int hardvenReady   = state.Books.Count(kv => kv.Key.StartsWith("H:") && kv.Value.HasReceivedDelta);
            int kalshiTotal = state.Books.Count(kv => kv.Key.StartsWith("K:"));
            int hardvenTotal   = state.Books.Count(kv => kv.Key.StartsWith("H:"));

            DebugLog.Write($"Near-miss reporter: kalshi={kalshiReady}/{kalshiTotal} hardven={hardvenReady}/{hardvenTotal} pairs={telemetry.TotalPairs} openArbs={telemetry.OpenArbs}");

            if (DebugLog.NearMissEnabled)
            {
                Console.WriteLine($"\n[TELEMETRY] --- TOP {Math.Min(10, pairs.Count)} CLOSEST TO CROSS-PLATFORM ARB ---");
                Console.WriteLine($"  Kalshi books: {kalshiReady}/{kalshiTotal} | HardVen books: {hardvenReady}/{hardvenTotal} | Pairs: {telemetry.TotalPairs} | Open arbs: {telemetry.OpenArbs}");

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
var hardvenFeed   = new HardVenWebsocketFeed(HARDVEN_SIDECAR_URL, hardvenSubscribeTokens,
                                             state, telemetry, HARDVEN_BATCH_SIZE, HARDVEN_PING_INTERVAL_MS);

// ══════════════════════════════════════════════════════════════════════════════
//  DISCORD HEARTBEAT + HEALTH ALERTS  (ALL modes — the unattended "is it up?" net)
// ══════════════════════════════════════════════════════════════════════════════
// Runs in EVERY mode, telemetry included (the executor watchdog above is dry/live-only and console-only).
// Posts a startup ping, a periodic heartbeat, and EDGE-triggered alerts for: HardVen session logout
// (SessionReady flip — the Pinnacle login dropping), HardVen feed down, Kalshi feed down (+ recovery each).
// This is the operator's remote proof-of-life for a multi-day unattended telemetry run. No-op without a webhook.
if (discord.Enabled)
{
    int heartbeatMin = int.TryParse(Environment.GetEnvironmentVariable("DISCORD_HEARTBEAT_MIN"), out var hm) && hm > 0
        ? hm : 30;
    // A down signal must persist this long before we cry 🔴 — so we skip startup warm-up (pre-first-login) and
    // the brief re-capture gap after every scheduled reopen, and alert ONLY when something is genuinely stuck
    // (e.g. auto-login couldn't recover the session). Default 90s.
    double downGraceSec = double.TryParse(Environment.GetEnvironmentVariable("DISCORD_DOWN_GRACE_SEC"), out var gs) && gs > 0
        ? gs : 90;
    long arbsLogged = 0;
    telemetry.OnArbOpened += (_, _, _, _, _, _) => Interlocked.Increment(ref arbsLogged);
    var startedAt = DateTime.UtcNow;
    _ = discord.AlertAsync($"🟢 {modeLabel} started — {pairs.Count} pair(s), sidecar {HARDVEN_SIDECAR_URL}. " +
                           $"Heartbeat every {heartbeatMin}m.");
    _ = Task.Run(async () =>
    {
        // Debounced down/up tracking per signal. everUp gates out the startup warm-up (nothing is "lost" until
        // it was up at least once); downSince + downGraceSec suppress the brief re-capture gap on every scheduled
        // reopen; alerted latches so a stuck signal fires 🔴 exactly once (and its 🟢 recovery once).
        var downSince = new Dictionary<string, DateTime?> { ["session"] = null, ["hardven"] = null, ["kalshi"] = null };
        var everUp    = new Dictionary<string, bool>      { ["session"] = false, ["hardven"] = false, ["kalshi"] = false };
        var alerted   = new Dictionary<string, bool>      { ["session"] = false, ["hardven"] = false, ["kalshi"] = false };
        var lastHeartbeat = DateTime.UtcNow;

        void Track(string key, bool up, DateTime now, string downMsg, string upMsg, string? establishedMsg)
        {
            if (up)
            {
                if (!everUp[key]) { everUp[key] = true; if (establishedMsg != null) _ = discord.AlertAsync(establishedMsg); }
                if (alerted[key]) { alerted[key] = false; _ = discord.AlertAsync(upMsg); }   // recovered after a 🔴
                downSince[key] = null;
            }
            else if (everUp[key])                          // ignore down-time before the first-ever success
            {
                downSince[key] ??= now;
                if (!alerted[key] && (now - downSince[key]!.Value).TotalSeconds >= downGraceSec)
                {
                    alerted[key] = true;                   // fire the 🔴 once; 🟢 recovery clears it
                    _ = discord.AlertAsync(downMsg);
                }
            }
        }

        try
        {
            while (!cts.Token.IsCancellationRequested)
            {
                await Task.Delay(15_000, cts.Token).ContinueWith(_ => { });
                if (cts.Token.IsCancellationRequested) break;
                var nowDt = DateTime.UtcNow;

                bool darkNow   = hardvenFeed.ScheduledDark;  // lifecycle dark window — planned close, NOT a logout
                bool sessionOk = hardvenFeed.SessionReady || darkNow;   // treat a scheduled dark as "not a problem"
                bool hvOk      = hardvenFeed.IsConnected;     // sidecar serving odds
                bool kOk       = kalshiFeed.IsConnected;

                // Alert only on a STUCK problem: startup warm-up + scheduled-reopen re-capture gaps are absorbed
                // by everUp + the grace window, so an auto-login that recovers within grace stays silent.
                Track("session", sessionOk, nowDt,
                    $"🔴 HardVen session down >{downGraceSec:0}s — Pinnacle logged out and auto-login hasn't recovered it. May need a manual login.",
                    "🟢 HardVen session recovered — login re-captured, books flowing.",
                    "🟢 HardVen session established — login captured.");
                Track("hardven", hvOk, nowDt,
                    $"🔴 HardVen feed down >{downGraceSec:0}s — sidecar unreachable or not serving odds.",
                    "🟢 HardVen feed back up.", null);
                Track("kalshi", kOk, nowDt,
                    $"🔴 Kalshi feed down >{downGraceSec:0}s.",
                    "🟢 Kalshi feed back up.", null);

                // ── periodic heartbeat ──
                if ((nowDt - lastHeartbeat).TotalMinutes >= heartbeatMin)
                {
                    lastHeartbeat = nowDt;
                    int kReady = state.Books.Count(kv => kv.Key.StartsWith("K:") && kv.Value.HasReceivedDelta);
                    int pReady = state.Books.Count(kv => kv.Key.StartsWith("H:") && kv.Value.HasReceivedDelta);
                    int kTotal = state.Books.Count(kv => kv.Key.StartsWith("K:"));
                    int pTotal = state.Books.Count(kv => kv.Key.StartsWith("H:"));
                    var up = nowDt - startedAt;
                    string sessTag = darkNow ? "dark (scheduled)" : hardvenFeed.SessionReady ? "ready" : "DOWN";
                    _ = discord.AlertAsync(
                        $"💓 up {up.Days}d{up.Hours}h{up.Minutes}m │ session {sessTag} │ " +
                        $"books K={kReady}/{kTotal} H={pReady}/{pTotal} │ WS K={(kOk ? "ok" : "down")} H={(hvOk ? "ok" : "down")} │ " +
                        $"openArbs={telemetry.OpenArbs} arbsLogged={Interlocked.Read(ref arbsLogged)}");
                }
            }
        }
        catch (Exception ex) { Console.WriteLine($"[DISCORD HEARTBEAT ERROR] {ex.Message}"); }
    });
}

// ══════════════════════════════════════════════════════════════════════════════
//  DISCORD COMMAND LISTENER  (remote 'status' / 'close'|'end' from the #alerts channel)
// ══════════════════════════════════════════════════════════════════════════════
// Query or stop the bot from your phone in the same channel it posts to. Needs a BOT token + channel id
// (webhooks are send-only). No-op without them; every action is best-effort so it never disrupts the run.
{
    var cmdStartedAt = DateTime.UtcNow;

    async Task<string> RunAnalyzerSummaryAsync()
    {
        try
        {
            string py = Environment.GetEnvironmentVariable("HARDVEN_PYTHON") ?? "python";
            var psi = new System.Diagnostics.ProcessStartInfo
            {
                FileName = py,
                WorkingDirectory = Directory.GetCurrentDirectory(),   // repo root: CSVs + analyze_cross_arb.py live here
                RedirectStandardOutput = true, RedirectStandardError = true,
                UseShellExecute = false, CreateNoWindow = true,
            };
            psi.ArgumentList.Add("analyze_cross_arb.py");
            psi.ArgumentList.Add("--summary");
            using var proc = System.Diagnostics.Process.Start(psi);
            if (proc == null) return "";
            var outTask = proc.StandardOutput.ReadToEndAsync();
            var errTask = proc.StandardError.ReadToEndAsync();
            using var toCts = new CancellationTokenSource(TimeSpan.FromSeconds(30));
            try { await proc.WaitForExitAsync(toCts.Token); }
            catch (OperationCanceledException) { try { proc.Kill(true); } catch { } return "(analyzer timed out)"; }
            string outp = (await outTask).Trim();
            if (outp.Length == 0) outp = (await errTask).Trim();
            return outp;
        }
        catch (Exception ex) { return $"(analyzer error: {ex.Message})"; }
    }

    async Task<string> BuildStatusAsync()
    {
        int kReady = state.Books.Count(kv => kv.Key.StartsWith("K:") && kv.Value.HasReceivedDelta);
        int pReady = state.Books.Count(kv => kv.Key.StartsWith("H:") && kv.Value.HasReceivedDelta);
        int kTotal = state.Books.Count(kv => kv.Key.StartsWith("K:"));
        int pTotal = state.Books.Count(kv => kv.Key.StartsWith("H:"));
        string sess = hardvenFeed.ScheduledDark ? "dark (scheduled)"
                    : hardvenFeed.SessionReady ? "ready" : "DOWN";
        var up = DateTime.UtcNow - cmdStartedAt;
        string live = $"📊 **status** — session {sess} | books K={kReady}/{kTotal} H={pReady}/{pTotal} | " +
                      $"WS K={(kalshiFeed.IsConnected ? "ok" : "down")} H={(hardvenFeed.IsConnected ? "ok" : "down")} | " +
                      $"openArbs={telemetry.OpenArbs} pairs={telemetry.TotalPairs} | up {up.Days}d{up.Hours}h{up.Minutes}m";
        string analysis = await RunAnalyzerSummaryAsync();
        string combined = string.IsNullOrWhiteSpace(analysis)
            ? live + "\n(no telemetry logged yet)"
            : live + "\n```\n" + analysis + "\n```";
        return combined.Length > 1900 ? combined[..1900] + "…" : combined;
    }

    async Task ShutdownHookAsync()
    {
        // Sentinel tells the supervisor this was a DELIBERATE stop (don't restart). CSV is flushed by the normal
        // shutdown path after cts cancels the feeds.
        try { await File.WriteAllTextAsync(Path.Combine(sourceDir, ".stop_requested"), DateTime.UtcNow.ToString("o")); }
        catch (Exception ex) { Console.WriteLine($"[DISCORD CMD] could not write stop sentinel: {ex.Message}"); }
        cts.Cancel();
    }

    var cmdListener = new DiscordCommandListener(
        Environment.GetEnvironmentVariable("DISCORD_BOT_TOKEN"),
        Environment.GetEnvironmentVariable("DISCORD_CHANNEL_ID"),
        reply:      msg => discord.AlertAsync(msg),
        onStatus:   BuildStatusAsync,
        onShutdown: ShutdownHookAsync);
    if (cmdListener.Enabled)
    {
        Console.WriteLine("[DISCORD CMD] remote commands enabled: status / close / end");
        _ = Task.Run(() => cmdListener.RunAsync(cts.Token));
    }

    // AUTO-STATUS after each session block (WEBHOOK-ONLY — no bot token needed). When a scheduled window closes
    // (ScheduledDark flips false→true) AND we actually collected during it, post the digest — a per-block summary
    // with zero interaction. Best-effort; never disrupts the run.
    if (discord.Enabled)
    {
        Console.WriteLine("[DISCORD] auto-status ON — a summary posts after each session block.");
        _ = Task.Run(async () =>
        {
            bool wasDark = hardvenFeed.ScheduledDark, sawReady = false;
            while (!cts.Token.IsCancellationRequested)
            {
                try { await Task.Delay(15_000, cts.Token).ContinueWith(_ => { }); } catch { break; }
                if (cts.Token.IsCancellationRequested) break;
                bool dark = hardvenFeed.ScheduledDark;
                if (!dark && hardvenFeed.SessionReady) sawReady = true;          // live during this open block
                if (dark && !wasDark && sawReady)                                // block just ended (had a session)
                {
                    sawReady = false;                                            // arm for the next block
                    try
                    {
                        await Task.Delay(5_000, cts.Token).ContinueWith(_ => { });   // let the block's last rows flush
                        _ = discord.AlertAsync("🗓️ **session block ended** —\n" + await BuildStatusAsync());
                    }
                    catch (Exception ex) { Console.WriteLine($"[BLOCK STATUS ERROR] {ex.Message}"); }
                }
                wasDark = dark;
            }
        });
    }
}

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
                bool pOk = hardvenFeed.IsConnected;

                // ── WS connect/disconnect transitions ──────────────────────────
                if (!kOk && lastKOk) Console.WriteLine("[WATCHDOG] Kalshi disconnected — halting new trades");
                if (!pOk && lastPOk) Console.WriteLine("[WATCHDOG] HardVen disconnected — halting new trades");
                if ( kOk && !lastKOk) Console.WriteLine("[WATCHDOG] Kalshi reconnected — resuming trades");
                if ( pOk && !lastPOk) Console.WriteLine("[WATCHDOG] HardVen reconnected — resuming trades");
                lastKOk = kOk;
                lastPOk = pOk;
                if (!kOk || !pOk) executor.HaltForConnectionLoss();
                else              executor.ResumeFromConnectionLoss();

                // ── Silence detection: WS connected but no messages for 60s ───
                // Distinguish "venue is quiet" (REST succeeds) from "we're cut off" (REST fails).
                var nowDt    = DateTime.UtcNow;
                double kSilS = (nowDt - kalshiFeed.LastMessageAt).TotalSeconds;
                double pSilS = (nowDt - hardvenFeed  .LastMessageAt).TotalSeconds;

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
                    bool pRestOk = await executor.PingHardVenAsync();
                    if (pRestOk)
                        Console.WriteLine($"[WATCHDOG] HardVen WS silent {pSilS:0}s — REST OK (venue quiet, no arb activity)");
                    else
                    {
                        Console.ForegroundColor = ConsoleColor.Red;
                        Console.WriteLine($"[WATCHDOG ALERT] HardVen WS silent {pSilS:0}s — REST unreachable. " +
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
                int pReady = state.Books.Count(kv => kv.Key.StartsWith("H:") && kv.Value.HasReceivedDelta);
                int kTotal = state.Books.Count(kv => kv.Key.StartsWith("K:"));
                int pTotal = state.Books.Count(kv => kv.Key.StartsWith("H:"));

                decimal proj = executor.TotalProjectedProfit;
                string projStr = (proj >= 0 ? "+" : "") + $"${proj:0.00}";
                string haltTag = executor.IsHalted           ? "  [HALTED — manual reset required]"
                               : executor.IsConnectionHalted ? "  [CONN HALT — waiting for reconnect]"
                               : "";
                string tryTag  = executor.TriesRemaining >= 0 ? $"  triesLeft={executor.TriesRemaining}" : "";
                Console.WriteLine(
                    $"[STATUS {DateTime.UtcNow:HH:mm:ss}] " +
                    $"K=${executor.KalshiBalanceUsd:0.00}  P=${executor.HardVenBalanceUsd:0.00}  │  " +
                    $"invested=${executor.TotalInvested:0.00}  proj={projStr}  │  " +
                    $"exposure=${executor.TotalExposure:0.00}/${executor.MaxExposureUsd:0.00}  │  " +
                    $"open={executor.OpenPositionCount}  filled={executor.TotalExecuted}  earlyExit={executor.EarlyExitsCompleted}  │  " +
                    $"books K={kReady}/{kTotal} P={pReady}/{pTotal}" +
                    $"  WS K={kalshiFeed.IsConnected} P={hardvenFeed.IsConnected}" +
                    $"  dayLoss=${executor.DayLossUsd:0.00}/${executor.MaxDayLossUsd:0.00}" +
                    $"  cleanup=${executor.TotalCleanupCostUsd:0.00}" +
                    $"{tryTag}{haltTag}");

                foreach (var p in executor.GetOpenPositionStatus())
                {
                    string pnlStr  = p.CanMonitorExit
                        ? (p.UnrealizedPnl >= 0 ? $"+${p.UnrealizedPnl:0.00}" : $"-${Math.Abs(p.UnrealizedPnl):0.00}")
                        : "n/a";
                    string bidStr  = p.CanMonitorExit
                        ? $"bid {p.KBid:0.000}+{p.PBid:0.000}"
                        : "bid n/a";
                    string monTag  = p.CanMonitorExit ? "" : "  [NO BID DATA — exit monitoring unavailable]";
                    Console.WriteLine(
                        $"  ├ {p.Label[..Math.Min(45, p.Label.Length)].PadRight(45)} │ {p.ArbType,-12} │ " +
                        $"K={p.KContracts:0}@{p.KEntry:0.000} P={p.PShares:0.##}@{p.PEntry:0.000} │ " +
                        $"{bidStr} │ pnl {pnlStr}{monTag}");
                }
            }
        }
        catch (Exception ex)
        {
            Console.WriteLine($"[STATUS DASH ERROR] {ex.Message}");
        }
    });
}

// ══════════════════════════════════════════════════════════════════════════════
//  HOT-RELOAD: re-read cross_pairs.json + derivative_pairs.json every 15 min for new pairs — so a daily
//  auto-pair (pairing_scheduler in the sidecar) is picked up live without a restart.
// ══════════════════════════════════════════════════════════════════════════════
_ = Task.Run(async () =>
{
    // Start FREQUENT then back off to 15 min: the startup auto-pair finishes ~1-2 min in, so we want to load its
    // fresh cross_pairs.json quickly (90s → 3m → 6m → … → 15m), not wait a full 15 min for the first pass.
    int reloadDelayMs = 90_000;
    while (!cts.Token.IsCancellationRequested)
    {
        await Task.Delay(reloadDelayMs, cts.Token).ContinueWith(_ => { });
        reloadDelayMs = Math.Min(reloadDelayMs * 2, 900_000);
        if (cts.Token.IsCancellationRequested) break;
        // Reload BOTH pair files: cross_pairs.json (moneyline) and derivative_pairs.json (spread/total). Same
        // schema + same knownPairIds dedup → only pairs NEW since the last read are added (daily re-pair).
        foreach (var reloadPath in new[] { manualPath, derivPath })
        {
        if (!File.Exists(reloadPath))
        {
            DebugLog.Write($"Hot-reload: {reloadPath} not found, skipping");
            continue;
        }
        DebugLog.Write($"Hot-reload: reading {reloadPath}");
        try
        {
            using var doc = JsonDocument.Parse(File.ReadAllText(reloadPath));
            var newPairs    = new List<CrossPair>();
            var newKTickers = new List<string>();
            var newPTokens  = new List<string>();

            foreach (var el in doc.RootElement.EnumerateArray())
            {
                string kTicker  = el.TryGetProperty("kalshi_ticker",  out var kt)  ? (kt.GetString()  ?? "") : "";
                if (IsExcludedTicker(kTicker)) continue;
                string yesToken = el.TryGetProperty("hardven_yes_token", out var yt)  ? (yt.GetString()  ?? "") : "";
                string noToken  = el.TryGetProperty("hardven_no_token",  out var nt)  ? (nt.GetString()  ?? "") : "";
                string label    = el.TryGetProperty("label",          out var lb)  ? (lb.GetString()  ?? "") : kTicker;
                string eventId  = el.TryGetProperty("event_id",       out var eid) ? (eid.GetString() ?? "") : "";
                DateOnly? settlementDate = null;
                if (el.TryGetProperty("settlement_date", out var sd2) && DateOnly.TryParse(sd2.GetString(), out var d2))
                    settlementDate = d2;
                if (string.IsNullOrEmpty(kTicker) || string.IsNullOrEmpty(yesToken) || string.IsNullOrEmpty(noToken)) continue;

                bool isNegRiskHot = el.TryGetProperty("is_neg_risk", out var nrHot) && nrHot.ValueKind == JsonValueKind.True;
                decimal hardvenMinSizeHot = el.TryGetProperty("hardven_min_size", out var msHot) && msHot.TryGetDecimal(out decimal msvHot) && msvHot > 0 ? msvHot : 1.0m;
                bool threeWayHot = el.TryGetProperty("three_way", out var twHot) && twHot.ValueKind == JsonValueKind.True;
                string pairId = $"MANUAL_{kTicker}__{yesToken[..Math.Min(8, yesToken.Length)]}";
                if (knownPairIds.Contains(pairId)) continue;
                knownPairIds.Add(pairId);

                newPairs.Add(new CrossPair(pairId, label, kTicker, yesToken, noToken, eventId, settlementDate, isNegRiskHot, hardvenMinSizeHot, threeWayHot));
                if (knownKalshiTickers.Add(kTicker)) newKTickers.Add(kTicker);
                if (knownHardVenTokens.Add(yesToken))   newPTokens.Add(yesToken);
                if (knownHardVenTokens.Add(noToken))    newPTokens.Add(noToken);
            }

            if (newPairs.Count == 0)
            {
                DebugLog.Write("Hot-reload: no new pairs found in file");
                continue;
            }
            DebugLog.Write($"Hot-reload: found {newPairs.Count} new pair(s) — K={newKTickers.Count} new tickers, P={newPTokens.Count} new tokens");

            // Validate each new pair (Kalshi open + HardVen tokens active) before subscribing.
            // Runs in this background Task.Run so the main bot is not interrupted.
            var validPairs = isDryRun
                ? newPairs
                : await executor!.ValidatePairsAtStartupAsync(newPairs);

            if (validPairs.Count == 0)
            {
                Console.WriteLine("[HOT-RELOAD] All new pair(s) failed validation — nothing added");
                continue;
            }

            var validKTickers = validPairs.Select(p => p.KalshiTicker).ToHashSet(StringComparer.OrdinalIgnoreCase);
            var validKList    = newKTickers.Where(t => validKTickers.Contains(t)).ToList();
            var validPTokens  = validPairs.SelectMany(p => new[] { p.HardVenYesTokenId, p.HardVenNoTokenId }).ToHashSet();
            var validPList    = newPTokens.Where(t => validPTokens.Contains(t)).ToList();

            foreach (var t in validKList)  state.InitKalshiMarket(t);
            foreach (var t in validPList)  state.InitHardVenToken(t);
            telemetry.AddPairs(validPairs);
            if (validKList.Count > 0) kalshiFeed.EnqueueSubscribe(validKList);
            if (validPList.Count > 0) hardvenFeed.EnqueueSubscribe(validPList);
        }
        catch (Exception ex)
        {
            Console.WriteLine($"[HOT-RELOAD] Error reading {reloadPath}: {ex.Message}");
        }
        }   // foreach reloadPath (cross_pairs.json + derivative_pairs.json)
    }
});

// ── Book refresher — keeps quiet books alive via periodic REST snapshots ──────
var bookRefresher = new BookRefresherService(state.Books, orderClient);
_ = Task.Run(async () =>
{
    try { await bookRefresher.RunAsync(cts.Token); }
    catch (Exception ex) { Console.WriteLine($"[BOOK REFRESH ERROR] {ex.Message}"); }
});

// Keep the machine awake for unattended day-long runs (laptop residential deploy). Windows-only; no-op
// elsewhere; HARDVEN_KEEP_AWAKE=0 disables. Released after the feeds stop.
bool keepAwakeOn = (Environment.GetEnvironmentVariable("HARDVEN_KEEP_AWAKE") ?? "1") != "0";
var keepAwakeTask = keepAwakeOn ? KeepAwake.RunAsync(cts.Token) : Task.CompletedTask;

// Feed SUPERVISORS: a feed that returns or throws while we are NOT shutting down is RESTARTED (capped backoff)
// instead of cancelling the whole bot. Survives WS drops, machine sleep/wake, and sidecar blips across a
// day-long run. Only a deliberate cts.Cancel() (double Ctrl+C) ends the run now.
var kalshiWsTask  = Task.Run(() => SuperviseFeedAsync("Kalshi",  kalshiFeed.RunAsync,  cts.Token));
var hardvenWsTask = Task.Run(() => SuperviseFeedAsync("HardVen", hardvenFeed.RunAsync, cts.Token));

await Task.WhenAll(kalshiWsTask, hardvenWsTask);
try { await keepAwakeTask; } catch { /* releases sleep-suppression in its own finally */ }

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

static async Task CheckHardVenProxyAsync(string socksProxy, bool isLive)
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
        string liveWarn = isLive ? " — WARN: --live mode without proxy; HardVen may geo-block" : "";
        Console.WriteLine($"[PROXY CHECK] No HARDVEN_SOCKS_PROXY — HardVen REST calls will use local IP ({localIp}){liveWarn}");
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
        Console.WriteLine($"[PROXY CHECK FAIL] Proxy {socksProxy} unreachable: {ApiErrorHelper.ClassifyHardVen(ex)}");
        if (isLive)
            Console.WriteLine("[PROXY CHECK WARN] --live mode — HardVen REST calls may fall back to local IP or fail");
        return;
    }

    if (proxyIp != localIp && proxyIp != "?")
        Console.WriteLine($"[PROXY CHECK OK] localIP={localIp} → proxyIP={proxyIp} — different egress confirmed ✓");
    else
        Console.WriteLine($"[PROXY CHECK WARN] localIP={localIp} proxyIP={proxyIp} — same IP! Proxy may not be tunneling traffic");
}

// Restart-loop wrapper for a WS/poll feed: run it, and if it returns or throws while we are NOT shutting down,
// restart it with capped exponential backoff (a healthy long run resets the backoff). A feed that returns
// IMMEDIATELY and cleanly a few times = disabled/misconfigured (e.g. HardVen with no sidecar URL) → stop
// supervising just that feed (the bot keeps running the other side). Only cancellation of `token` ends it.
static async Task SuperviseFeedAsync(string name, Func<CancellationToken, Task> runAsync, CancellationToken token)
{
    const int minBackoffSec = 2, maxBackoffSec = 30, giveUpAfterFastCleanExits = 5;
    int backoffSec = minBackoffSec, restarts = 0, fastCleanExits = 0;
    while (!token.IsCancellationRequested)
    {
        var started = DateTime.UtcNow;
        bool crashed = false;
        try
        {
            await runAsync(token);
        }
        catch (OperationCanceledException) when (token.IsCancellationRequested)
        {
            break;   // graceful shutdown
        }
        catch (Exception ex)
        {
            crashed = true;
            Console.WriteLine($"[SUPERVISOR] {name} feed crashed: {ex.GetType().Name}: {ex.Message}");
            DebugLog.Write($"{name} feed exception (before restart #{restarts + 1}): {ex}");
        }
        if (token.IsCancellationRequested) break;

        var ranFor = DateTime.UtcNow - started;
        if (!crashed && ranFor < TimeSpan.FromSeconds(3))
        {
            if (++fastCleanExits >= giveUpAfterFastCleanExits)
            {
                Console.WriteLine($"[SUPERVISOR] {name} feed returned immediately {fastCleanExits}x (disabled/misconfigured) " +
                                  "— stopping its supervisor; the bot keeps running.");
                return;
            }
        }
        else
        {
            fastCleanExits = 0;
        }

        // reset the backoff if the feed had been running healthily (a long, stable session that just dropped)
        backoffSec = ranFor > TimeSpan.FromMinutes(2) ? minBackoffSec : Math.Min(maxBackoffSec, backoffSec * 2);
        restarts++;
        Console.WriteLine($"[SUPERVISOR] {name} feed stopped after {ranFor.TotalSeconds:0}s — restarting (#{restarts}) in {backoffSec}s.");
        try { await Task.Delay(TimeSpan.FromSeconds(backoffSec), token); }
        catch (OperationCanceledException) { break; }
    }
    DebugLog.Write($"{name} feed supervisor exited after {restarts} restart(s).");
}

// LoadHardVenConfig() removed — HardVen creds load via HardVenApiConfig.FromEnvironment() (project-local).

// Suppress system sleep while the bot runs (unattended day-long runs on the laptop). Windows-only via
// SetThreadExecutionState; a periodic ES_SYSTEM_REQUIRED poke resets the idle timer (thread-independent, so it
// survives the task-pool thread that runs the loop). No-op off Windows — servers don't sleep.
static class KeepAwake
{
    [System.Runtime.InteropServices.DllImport("kernel32.dll")]
    private static extern uint SetThreadExecutionState(uint esFlags);
    private const uint ES_CONTINUOUS = 0x80000000, ES_SYSTEM_REQUIRED = 0x00000001;

    public static Task RunAsync(CancellationToken token)
    {
        if (!OperatingSystem.IsWindows()) return Task.CompletedTask;
        Console.WriteLine("[KEEP-AWAKE] Suppressing system sleep while the bot runs (HARDVEN_KEEP_AWAKE=0 to disable).");
        return Task.Run(async () =>
        {
            try
            {
                while (!token.IsCancellationRequested)
                {
                    if (OperatingSystem.IsWindows()) SetThreadExecutionState(ES_SYSTEM_REQUIRED);  // poke idle timer
                    await Task.Delay(TimeSpan.FromSeconds(30), token);
                }
            }
            catch (OperationCanceledException) { }
            finally
            {
                if (OperatingSystem.IsWindows()) SetThreadExecutionState(ES_CONTINUOUS);   // release the request
            }
        }, token);
    }
}
