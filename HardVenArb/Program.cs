using System.Globalization;
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
//  --stop-after D  wall-clock cap on the run: "2h", "90m", "3600s", or a bare number = minutes. Pair with
//                  --try N for an unattended wait: ends on the Nth arb OR the clock, whichever is first.
//  --stop-sidecar  on a clean exit, POST /shutdown to the sidecar so the managed browser + Pinnacle session
//                  close too (refused while a bet is in flight). For unattended runs.
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
//    I   inject a FAVOURITE-on-Kalshi test arb (dry-run only; fires the real gates on a pre-live pair, sim fills)
//    O   inject an UNDERDOG-on-Kalshi test arb  (dry-run only; should hit the favourite-gate skip when it's ON)
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
// ── Self-check: the SAME-SIDE guard (name-based hedge verification) ───────────────────────────────────
// Numbers cannot tell a hedge from a doubled bet at a coin flip; names can. This is the check that would
// have stopped 2026-08-12's Dias trade, so it gets a test rather than a hope.
if (args.Contains("--side-check"))
{
    int bad = 0;
    void C(string label, bool ok) { Console.WriteLine($"  {(ok ? "PASS" : "FAIL")}  {label}"); if (!ok) bad++; }

    // THE REAL FAILURE: Kalshi YES = Sabrina Dias, book leg also Sabrina Dias.
    C("blocks the real Dias same-side bet",
      !CrossArbExecutor.SideNamesOppose("Sabrina Dias (Sets)", "Sabrina Dias", "K_YES_P_NO", out var w1));
    Console.WriteLine($"        reason: {w1}");

    // The CORRECT version of that same trade must still go through.
    C("allows the correct opposite leg",
      CrossArbExecutor.SideNamesOppose("Gabriela Kawano Cho (Sets)", "Sabrina Dias", "K_YES_P_NO", out _));

    // K_NO_P_YES is the mirror: holding Kalshi NO, the book leg SHOULD be the Kalshi outcome.
    C("K_NO_P_YES allows the matching leg",
      CrossArbExecutor.SideNamesOppose("Sabrina Dias (Sets)", "Sabrina Dias", "K_NO_P_YES", out _));
    C("K_NO_P_YES blocks the opposite leg",
      !CrossArbExecutor.SideNamesOppose("Gabriela Kawano Cho", "Sabrina Dias", "K_NO_P_YES", out _));

    // NEAR-EVEN GAMES MUST STILL TRADE — the whole point of doing this by name, not by price.
    C("even-money game still allowed (surname-only label)",
      CrossArbExecutor.SideNamesOppose("Kobori (Sets)", "Lola Giza", "K_YES_P_NO", out _));
    C("even-money game blocked when it IS the same player",
      !CrossArbExecutor.SideNamesOppose("Giza (Sets)", "Lola Giza", "K_YES_P_NO", out _));

    // Missing label = books with no slip name must not be bricked, unless asked to fail closed.
    C("missing label passes by default", CrossArbExecutor.SideNamesOppose("", "Sabrina Dias", "K_YES_P_NO", out _));
    Environment.SetEnvironmentVariable("HARDVEN_REQUIRE_SIDE_NAME", "1");
    C("missing label fails closed with HARDVEN_REQUIRE_SIDE_NAME=1",
      !CrossArbExecutor.SideNamesOppose("", "Sabrina Dias", "K_YES_P_NO", out _));
    Environment.SetEnvironmentVariable("HARDVEN_REQUIRE_SIDE_NAME", null);

    Console.WriteLine(bad == 0 ? "ALL PASS" : $"FAILURES: {bad}");
    Environment.ExitCode = bad == 0 ? 0 : 1;
    return;
}

// ── Self-check: the sampled slip verifier's GATE (pre-live only, rate-limited, one at a time) ─────────
// This gate decides how often the bot navigates and clicks at the venue, so a bug here is an
// anti-detection problem, not a wrong number in a file. Exercised directly rather than inferred from a run.
if (args.Contains("--slip-verify-check"))
{
    // Set BEFORE the type is first touched: the interval/enabled flags are static readonly from env.
    Environment.SetEnvironmentVariable("HARDVEN_SLIP_VERIFY", "1");
    Environment.SetEnvironmentVariable("HARDVEN_SLIP_VERIFY_INTERVAL_SEC", "2");   // keep the check quick
    int failures = 0;
    void Check(string label, bool ok)
    {
        Console.WriteLine($"  {(ok ? "PASS" : "FAIL")}  {label}");
        if (!ok) failures++;
    }

    var probePairs = new List<CrossPair> { new("P1", "A vs B", "KX-T", "hvYes", "hvNo") };
    var probe = new CrossPlatformArbTelemetryStrategy(
        probePairs, new System.Collections.Concurrent.ConcurrentDictionary<string, LocalOrderBook>());

    Console.WriteLine("\n[1] disabled until a quoter is wired");
    Check("no verifier -> no sampling", !probe.TrySlipVerifySlot(openedInPlay: false));

    probe.SetSlipVerifier((_, _) => Task.FromResult((0.5m, "")));

    Console.WriteLine("\n[2] pre-live and in-play hold SEPARATE budgets");
    Check("pre-live arb takes its own slot", probe.TrySlipVerifySlot(openedInPlay: false));
    probe.ReleaseSlipVerifySlot();
    // The point of separate budgets: an in-play sample can never consume the slot a pre-live arb would
    // have used — which is what "pre-live has priority" has to mean when you cannot see what is coming.
    Check("in-play has its own, untouched by the above", probe.TrySlipVerifySlot(openedInPlay: true));
    probe.ReleaseSlipVerifySlot();
    Check("pre-live now throttled, its budget is spent", !probe.TrySlipVerifySlot(openedInPlay: false));
    Check("in-play also throttled (its longer budget)", !probe.TrySlipVerifySlot(openedInPlay: true));
    // Re-claim so [3] exercises the in-flight lock from a held state, as it did before.
    System.Threading.Thread.Sleep(CrossPlatformArbTelemetryStrategy.SlipVerifyIntervalMsForTest + 250);
    Check("pre-live recovers once its interval elapses", probe.TrySlipVerifySlot(openedInPlay: false));

    Console.WriteLine("\n[3] one at a time (the rover is a single tab)");
    Check("second arb is refused while one is in flight", !probe.TrySlipVerifySlot(openedInPlay: false));
    probe.ReleaseSlipVerifySlot();

    Console.WriteLine("\n[4] rate limit holds after the slot is released");
    Check("still refused inside the interval", !probe.TrySlipVerifySlot(openedInPlay: false));
    int waitMs = CrossPlatformArbTelemetryStrategy.SlipVerifyIntervalMsForTest;
    Console.WriteLine($"      waiting {waitMs}ms for the interval to elapse...");
    await Task.Delay(waitMs + 250);
    Check("allowed once the interval has passed", probe.TrySlipVerifySlot(openedInPlay: false));
    probe.ReleaseSlipVerifySlot();

    Console.WriteLine("\n[5] concurrent opens cannot both click");
    await Task.Delay(waitMs + 250);
    int granted = 0;
    await Task.WhenAll(Enumerable.Range(0, 32).Select(_ => Task.Run(() =>
    {
        if (probe.TrySlipVerifySlot(openedInPlay: false)) Interlocked.Increment(ref granted);
    })));
    Check($"exactly one of 32 simultaneous arbs was sampled (got {granted})", granted == 1);

    Console.WriteLine(failures == 0 ? "\nALL PASS" : $"\nFAILURES: {failures}");
    // Exit code via ExitCode, not `return n`: a returned value would make this file's entry point
    // int-returning and every other bare `return;` in it a compile error.
    Environment.ExitCode = failures == 0 ? 0 : 1;
    return;
}

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

// --try-success (or HARDVEN_TRY_SUCCESS=1): spend the --try budget on POSITIONS OPENED rather than attempts.
// By default any execution that reaches the firing stage counts — including a clean MISS (both legs 0) and a
// fully-reversed Case A, which leave the bot holding nothing. At a real net floor most attempts end that way,
// so `--try 1` would routinely stop the run before it ever got an arb on. With this, misses are free.
bool trySuccessOnly = args.Contains("--try-success")
                   || Environment.GetEnvironmentVariable("HARDVEN_TRY_SUCCESS") == "1";
if (trySuccessOnly && tryN is null)
    Console.WriteLine("[WARN] --try-success has no effect without --try N; ignoring.");

// --stop-after <dur>: hard wall-clock cap on the run ("2h", "90m", "3600s", or a bare number = minutes).
// For unattended waits on a thin slate: pair with `--try 1` so the bot ends either when an arb fires OR when
// the clock runs out, whichever comes first.
TimeSpan? stopAfter = null;
int stopIdx = Array.IndexOf(args, "--stop-after");
if (stopIdx >= 0 && stopIdx + 1 < args.Length)
{
    string raw = args[stopIdx + 1].Trim().ToLowerInvariant();
    string num = raw.TrimEnd('h', 'm', 's');
    if (double.TryParse(num, NumberStyles.Any, CultureInfo.InvariantCulture, out double qty) && qty > 0)
        stopAfter = raw.EndsWith("h") ? TimeSpan.FromHours(qty)
                  : raw.EndsWith("s") ? TimeSpan.FromSeconds(qty)
                  : TimeSpan.FromMinutes(qty);          // "m" or bare number
    if (stopAfter is null)
        Console.WriteLine($"[WARN] --stop-after '{args[stopIdx + 1]}' not understood (use 2h / 90m / 3600s) — ignored.");
}

// --stop-sidecar: on a clean exit, POST /shutdown to the sidecar so the managed browser + Pinnacle session
// close too. For unattended runs that would otherwise leave a logged-in browser open for hours.
bool stopSidecar = args.Contains("--stop-sidecar");

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
// ── env-driven limits ─────────────────────────────────────────────────────────────────────────────────
// EVERY risk/size limit in this file reads an env var. They used to be `const` — recompile-only — which is
// how a $40 reserve buffer ended up standing in front of a $12.88 account with no runtime way to change it:
// the bot skipped every arb as "balance-limited to 0 contracts" and the run read as "the market offered
// nothing". A limit you cannot change without a rebuild is a limit that will eventually be wrong silently.
//
// Convention: a value <= 0 in the environment is treated as UNSET and falls back, EXCEPT where zero is a
// meaningful setting (a zero buffer, a zero cooldown) — those pass allowZero: true. That keeps a typo'd or
// empty env var from silently disabling a safety limit, while still letting you deliberately set one to 0.
static decimal EnvDec(string name, decimal fallback, bool allowZero = false)
{
    string? raw = Environment.GetEnvironmentVariable(name);
    if (decimal.TryParse(raw, NumberStyles.Any, CultureInfo.InvariantCulture, out var v)
        && (v > 0m || (allowZero && v == 0m)))
        return v;
    return fallback;
}

static int EnvInt(string name, int fallback, bool allowZero = false)
{
    string? raw = Environment.GetEnvironmentVariable(name);
    if (int.TryParse(raw, NumberStyles.Any, CultureInfo.InvariantCulture, out var v)
        && (v > 0 || (allowZero && v == 0)))
        return v;
    return fallback;
}

// Tri-state on purpose: unset keeps the built-in default, so a bool limit can be turned BOTH ways from the
// environment. "!= null && == 1" would make it impossible to switch a default-on tripwire off.
static bool EnvBool(string name, bool fallback)
{
    string? raw = (Environment.GetEnvironmentVariable(name) ?? "").Trim();
    if (raw == "1" || raw.Equals("true", StringComparison.OrdinalIgnoreCase)) return true;
    if (raw == "0" || raw.Equals("false", StringComparison.OrdinalIgnoreCase)) return false;
    return fallback;
}

decimal ARB_THRESHOLD       = EnvDec("HARDVEN_ARB_THRESHOLD", 0.995m);
decimal DEPTH_FLOOR         = EnvDec("HARDVEN_DEPTH_FLOOR", 1m, allowZero: true);
decimal MIN_BOOK_PRICE      = EnvDec("HARDVEN_MIN_BOOK_PRICE", 0.03m, allowZero: true);
int     KALSHI_BATCH_SIZE   = EnvInt("HARDVEN_KALSHI_BATCH_SIZE", 100);
int     HARDVEN_BATCH_SIZE  = EnvInt("HARDVEN_BATCH_SIZE", 200);
// /odds is now an instant cache read (the sidecar refreshes the book in the background), so the bot can
// poll fast and cheaply. Default 3s; override with HARDVEN_POLL_MS. NOTE: actual quote freshness is set by
// the sidecar's BOOKMAKER_REFRESH_SEC — this only controls how often the bot pulls the latest cached book.
int           HARDVEN_PING_INTERVAL_MS =
    int.TryParse(Environment.GetEnvironmentVariable("HARDVEN_POLL_MS"), out var _hvPollMs) && _hvPollMs > 0
        ? _hvPollMs : 3_000;
int NEAR_MISS_INTERVAL_MS   = EnvInt("HARDVEN_NEAR_MISS_INTERVAL_MS", 60_000);
int STATUS_DASH_INTERVAL_MS = EnvInt("HARDVEN_STATUS_INTERVAL_MS", 30_000);
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

// BOT TAG — which venue this process is. Two bots share one Discord channel, so without it every
// alert is anonymous and, worse, every COMMAND is obeyed by BOTH: one "pause" pauses Pinnacle and
// BetInAsia together, and "status" returns two interleaved replies with no way to tell them apart.
// Defaults to HARDVEN_OUTPUT_TAG so the value that already separates the journals also names the bot.
// Empty = legacy single-bot behaviour (no prefix required on commands).
string BOT_TAG = (Environment.GetEnvironmentVariable("HARDVEN_BOT_TAG")
                  ?? Environment.GetEnvironmentVariable("HARDVEN_OUTPUT_TAG") ?? "").Trim().ToLowerInvariant();
string BOT_NAME = BOT_TAG.Length > 0 ? BOT_TAG.ToUpperInvariant() : "HardVenArb";

// Discord webhook alerter (halts, naked-position failures, low cash). .env is loaded above, so the
// URL is in the process env now; an unset/empty URL leaves it a disabled no-op.
var discord = new DiscordNotifier(Environment.GetEnvironmentVariable("DISCORD_WEBHOOK_URL"), BOT_NAME);
if (discord.Enabled)
    Console.WriteLine($"[DISCORD] webhook alerts enabled — posting as [{BOT_NAME}]"
                      + (BOT_TAG.Length > 0 ? $"; commands must be addressed '{BOT_TAG} <cmd>'"
                                            : " (no bot tag set — this bot answers UNADDRESSED commands, "
                                              + "which is ambiguous if another bot shares this channel)"));
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
// HARDVEN_PAIRS_FILE selects the pairs file, so a second venue can run beside Pinnacle without
// touching its pairing. Token formats are venue-specific ("221310:1633549397:home" on Pinnacle vs
// "tennis:338:2026-08-09,...:tennis_match,all:p1" on BetInAsia) and a pairs file is read by exactly
// one book, so sharing one file would mean each pairer silently destroying the other's work.
// Filename only — the dev/published directory resolution below still applies.
string pairsFileName = (Environment.GetEnvironmentVariable("HARDVEN_PAIRS_FILE") ?? "cross_pairs.json").Trim();
string outputDirFile = Path.Combine(AppContext.BaseDirectory, pairsFileName);
string sourceDir     = Path.GetFullPath(Path.Combine(AppContext.BaseDirectory, "../../.."));
string sourceDirFile = Path.Combine(sourceDir, pairsFileName);
bool   isDevBuild    = Directory.GetFiles(sourceDir, "*.csproj").Length > 0;
// Dev build → the auto-pairer writes cross_pairs.json to the SOURCE dir (HardVenArb/). Point the reload path
// there even when the file doesn't exist YET (the first auto-pair run creates it), so the hot-reload actually
// finds it — otherwise a fresh setup with HARDVEN_AUTO_PAIR would freeze onto a CWD path and never load pairs.
string manualPath = isDevBuild ? sourceDirFile
                               : (File.Exists(outputDirFile) ? outputDirFile : pairsFileName);
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
                // The Kalshi YES side's NAME — the executor cross-checks the book leg against it.
                string kOutcome = el.TryGetProperty("kalshi_outcome", out var kOut) ? (kOut.GetString() ?? "") : "";
                manualPairs.Add(new CrossPair(pairId, label, kTicker, yesToken, noToken, eventId, settlementDate, isNegRisk, hardvenMinSize, threeWay, kOutcome));
            }
        }
        Console.WriteLine($"[CONFIG] {manualPairs.Count} manual pair(s) loaded from {pairsFileName}"
                          + (excludedCount > 0 ? $" ({excludedCount} skipped by --exclude)" : ""));
    }
    catch (Exception ex)
    {
        Console.WriteLine($"[CONFIG WARN] Could not parse {pairsFileName}: {ex.Message}");
    }
}

// Also load derivative_pairs.json (spread/total LINES from pair_derivatives.py) — SAME schema, kept in a
// separate file so the moneyline pairs stay clean. Merged into manualPairs here so the run prices the lines
// too (the sidecar resolves the {lid}:{mid}:{type}:{points}:{side} tokens). Hot-reloaded alongside
// cross_pairs.json (below), so a daily auto-pair refreshes the derivative lines live too.
// A SECOND VENUE MUST NOT INHERIT THIS FILE. Tokens are venue-specific, so when HARDVEN_PAIRS_FILE points
// somewhere other than the default, the matching derivative file is this venue's too — loading Pinnacle's
// would feed `{lid}:{mid}:{type}:{points}:{side}` tokens to a book that cannot resolve them. Observed
// 2026-08-11: the BetInAsia run loaded 31 Pinnacle tennis lines and logged "selection no longer quoted"
// for every one, forever. Default it to the sibling of whatever pairs file is in use, and let
// HARDVEN_DERIV_PAIRS_FILE override; "" (or a missing file) simply loads no derivatives.
string derivFileName = (Environment.GetEnvironmentVariable("HARDVEN_DERIV_PAIRS_FILE") ?? "").Trim();
if (derivFileName.Length == 0)
    derivFileName = pairsFileName.Equals("cross_pairs.json", StringComparison.OrdinalIgnoreCase)
        ? "derivative_pairs.json"
        // cross_pairs_bia.json -> derivative_pairs_bia.json
        : pairsFileName.Replace("cross_pairs", "derivative_pairs", StringComparison.OrdinalIgnoreCase);
string derivSrc  = Path.Combine(sourceDir, derivFileName);
string derivOut  = Path.Combine(AppContext.BaseDirectory, derivFileName);
string derivPath = isDevBuild ? derivSrc
                              : (File.Exists(derivOut) ? derivOut : derivFileName);
if (!File.Exists(derivPath))
    Console.WriteLine($"[CONFIG] no derivative pairs ({derivFileName} not found) — moneyline only.");
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
                string kOutcomeD = el.TryGetProperty("kalshi_outcome", out var kOutD) ? (kOutD.GetString() ?? "") : "";
                manualPairs.Add(new CrossPair(pairId, label, kTicker, yesToken, noToken, eventId, settlementDate, isNegRisk, hardvenMinSize, false, kOutcomeD));
            }
        }
        Console.WriteLine($"[CONFIG] {manualPairs.Count - before} derivative pair(s) loaded from {derivFileName}"
                          + (derivExcluded > 0 ? $" ({derivExcluded} skipped by --exclude)" : ""));
    }
    catch (Exception ex)
    {
        Console.WriteLine($"[CONFIG WARN] Could not parse {derivFileName}: {ex.Message}");
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
// VERIFY-ON-DETECTION: when a window opens on a screening-only HardVen leg (sidecar tag wv=false, i.e. an
// httpx re-seed of an untabbed tail league), ask the sidecar to promote that league to a LIVE WS tab so the
// arb gets confirmed on real-time prices before it's trusted. Fire-and-forget POST; deduped per league in the
// strategy. No-op unless the sidecar is in reader mode (only then does any price carry wv=false).
var verifyHttp = new HttpClient { Timeout = TimeSpan.FromSeconds(8) };
Action<string> requestHardVenVerify = lid => _ = Task.Run(async () =>
{
    try
    {
        using var resp = await verifyHttp.PostAsync($"{HARDVEN_SIDECAR_URL}/verify?lid={Uri.EscapeDataString(lid)}", null);
        var body = await resp.Content.ReadAsStringAsync();
        Console.WriteLine($"[VERIFY] league {lid}: {body}");
    }
    catch (Exception ex) { DebugLog.Feed($"verify POST failed for {lid}: {ex.GetType().Name}: {ex.Message}"); }
});
var telemetry = new CrossPlatformArbTelemetryStrategy(pairs, state.Books, ARB_THRESHOLD, DEPTH_FLOOR, requestHardVenVerify);

// ── REST verifier — confirms arb windows via independent REST calls ───────────
var restVerifier = new CrossArbRestVerifier(orderClient, telemetry, hardvenProxy, HARDVEN_SIDECAR_URL);
telemetry.OnArbOpened += restVerifier.OnArbOpened;
// Sampled betslip measurement (HARDVEN_SLIP_VERIFY=1). Independent of the executor: it runs in ANY mode,
// including telemetry-only, because its whole point is measuring what the board price is worth before
// trusting it with money. Wired here because the verifier needs the strategy to exist first.
telemetry.SetSlipVerifier(restVerifier.SlipQuoteAsync, () => restVerifier.LastSlipVia);

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
    // HardVen balance/bets route through the sidecar (it owns the Pinnacle session). FX: the account is EUR,
    // Kalshi USD — convert the wallet balance the same way the feed converts depth (HARDVEN_FX_TO_USD).
    // FX: seed from the env, then keep it LIVE from the sidecar's /fx. The env used to be the only source and
    // drifted 6.9% unnoticed (1.08 vs a real 1.1542 on 2026-08-06), which over-stakes the book leg and turns
    // a hedged arb into a directional position. FxRate rejects out-of-band updates, so a bad fetch is inert.
    FxRate.SeedFromEnvironment();
    decimal hardvenFxToUsd = FxRate.Current;
    _ = Task.Run(async () =>
    {
        var fxHttp = new HttpClient { Timeout = TimeSpan.FromSeconds(15) };
        int fxSec = int.TryParse(Environment.GetEnvironmentVariable("HARDVEN_FX_POLL_SEC"), out var fp) && fp > 0
                    ? fp : 900;
        bool warned = false;
        while (!cts.Token.IsCancellationRequested)
        {
            try
            {
                using var r = await fxHttp.GetAsync($"{HARDVEN_SIDECAR_URL.TrimEnd('/')}/fx", cts.Token);
                if (r.IsSuccessStatusCode)
                {
                    using var doc = JsonDocument.Parse(await r.Content.ReadAsStringAsync(cts.Token));
                    if (doc.RootElement.TryGetProperty("rate", out var rt) && rt.TryGetDecimal(out var live))
                    {
                        if (FxRate.TryUpdate(live, "sidecar", out string why))
                        {
                            if (!warned && Math.Abs(live - hardvenFxToUsd) / hardvenFxToUsd > 0.02m)
                            {
                                warned = true;
                                Console.WriteLine($"[FX] live {live:0.0000} vs env {hardvenFxToUsd:0.0000} " +
                                    $"({(live / hardvenFxToUsd - 1m):+0.0%;-0.0%}) — using LIVE. Update " +
                                    "HARDVEN_FX_TO_USD so a sidecar outage falls back to something current.");
                                _ = discord.AlertAsync($"💱 FX drift: env {hardvenFxToUsd:0.0000} → live " +
                                    $"{live:0.0000} ({(live / hardvenFxToUsd - 1m):+0.0%;-0.0%}). Stakes now " +
                                    "sized on the live rate.");
                            }
                        }
                        else Console.WriteLine($"[FX] rejected sidecar rate: {why}");
                    }
                }
            }
            catch { /* best-effort: the seeded rate stands */ }
            try { await Task.Delay(fxSec * 1000, cts.Token); }
            catch (OperationCanceledException) { break; }
        }
    });
    // previewOnly on EVERY dry-run: the sidecar then refuses to place even when HARDVEN_BET_ENABLE=1 is armed
    // in the environment for live trading. Without this, --dry-run + HARDVEN_LIVE_BET_PATH=1 reached the real
    // placement path (observed 2026-08-04 — a real bet was attempted and only missed because the odds moved).
    var hardvenOrderClient = new HardVenOrderClient(hardvenConfig, HARDVEN_SIDECAR_URL, hardvenFxToUsd,
                                                    previewOnly: isDryRun);
    // Max combined dollar cost per arb entry. HARD CAP on position size, so it must be raised before the bot
    // can take the €100+ bets the bankroll plan calls for — at $30 a €100 stake is simply unreachable and the
    // ladder would silently size down to the smallest rung. Env-driven (was a recompile-only const).
    decimal MAX_BET_USD = EnvDec("HARDVEN_MAX_BET_USD", 30m);
    // Per-platform cash reserve, held back from every sizing decision.
    //   HARDVEN_BALANCE_BUFFER_USD  — absolute $, wins when set. Use this on a SMALL account: the pct form
    //                                 is a fraction of maxBet and has no idea what your balance actually is,
    //                                 so raising maxBet quietly raises the reserve too.
    //   HARDVEN_BALANCE_BUFFER_PCT  — fraction of maxBet (the historic behaviour, default 0.20).
    // Both accept 0 to disable the reserve entirely.
    decimal BALANCE_BUFFER_PCT = EnvDec("HARDVEN_BALANCE_BUFFER_PCT", 0.20m, allowZero: true);
    decimal BALANCE_BUFFER_USD = EnvDec("HARDVEN_BALANCE_BUFFER_USD", -1m, allowZero: true);
    bool    bufferIsAbsolute   = BALANCE_BUFFER_USD >= 0m;
    // The executor takes a PERCENTAGE and multiplies by maxBet, so an absolute buffer is expressed back as
    // the equivalent fraction. Done here rather than by changing the executor's contract so there is exactly
    // one definition of the reserve, and it stays visible on the startup banner.
    if (bufferIsAbsolute)
        BALANCE_BUFFER_PCT = MAX_BET_USD > 0m ? BALANCE_BUFFER_USD / MAX_BET_USD : 0m;
    decimal EXECUTION_THRESHOLD = EnvDec("HARDVEN_EXEC_THRESHOLD", 0.995m);
    // Minimum net to attempt execution (the thin-margin slippage buffer). Default 0.985 = require ~1.5¢/set. On
    // stable PRE-LIVE lines that settle within ~2 days, at price-taker size (no market impact), you can run it
    // thin: HARDVEN_EXEC_NET_FLOOR closer to EXECUTION_THRESHOLD captures down to any positive edge after fees.
    // Set it >= EXECUTION_THRESHOLD to disable the extra floor entirely (execute any detected arb). Kept above
    // MIN_PLAUSIBLE_NET or the executable band [MIN_PLAUSIBLE_NET, floor] collapses and nothing fires.
    decimal EXEC_NET_FLOOR  = EnvDec("HARDVEN_EXEC_NET_FLOOR", 0.985m);
    // Reject arbs cheaper than this: a >10% "edge" signals a mispriced/mismatched pair (JOR), not a real arb.
    decimal MIN_PLAUSIBLE_NET = EnvDec("HARDVEN_MIN_PLAUSIBLE_NET", 0.90m);
    // Re-entry cooldown per pair AND per HardVen leg (the sibling guard shares it). Was hardcoded at 120 with no
    // way to change it — which made the dry-run failure-scenario suite painful to drive, since each injected arb
    // then sat out two minutes. Injected (testMode) arbs now bypass it entirely, so this is for tuning real
    // re-entry pace: lower = re-enter a recurring edge sooner, higher = fewer bites at the same line.
    int PAIR_COOLDOWN_SEC = EnvInt("HARDVEN_PAIR_COOLDOWN_SEC", 120, allowZero: true);
    // HARDVEN_ALLOW_REENTRY: take MULTIPLE positions on the same pair. WAS forced off under --live so a
    // stale .env could never stack real positions by accident; now it follows the env in EVERY mode by
    // explicit request, because one-position-per-pair caps how many fires a verification night can produce
    // and proving the execution path needs fires.
    // Know what it switches off: the pair cooldown, the leg/sibling cooldown AND the one-position-per-pair
    // lock — so HARDVEN_PAIR_COOLDOWN_SEC becomes inert and a recurring edge can be re-taken immediately.
    // Simultaneous sibling fires are still blocked by the in-flight lock, and MAX_EXPOSURE_USD /
    // MAX_DAY_LOSS_USD / the per-trade tripwire still bound the total damage.
    bool ALLOW_REENTRY = Environment.GetEnvironmentVariable("HARDVEN_ALLOW_REENTRY") == "1";
    if (ALLOW_REENTRY)
    {
        Console.ForegroundColor = ConsoleColor.Yellow;
        Console.WriteLine((isDryRun ? "[TEST MODE]" : "[LIVE — REAL MONEY]")
                        + " HARDVEN_ALLOW_REENTRY=1 — pair cooldown, leg/sibling cooldown AND the "
                        + "one-position-per-pair lock are all OFF. A recurring edge re-enters immediately"
                        + (isDryRun ? "." : ", so ONE mispaired market can be hit repeatedly. Exposure is "
                                            + "bounded only by MAX_EXPOSURE_USD / MAX_DAY_LOSS_USD."));
        Console.ResetColor();
    }
    decimal LOW_BALANCE_ALERT_USD = EnvDec("HARDVEN_LOW_BALANCE_ALERT_USD", 15m, allowZero: true);  // Discord-alert below this

    // Recovery / halt policy. Ops rule: only halt on the daily-loss tripwire, a manual stop, or a network
    // error — never on a naked leg. A naked/partial leg is hedged if still ≤ break-even, else swept out;
    // if it truly can't flatten (venue paused) the pair is orphaned and the bot keeps running.
    decimal HEDGE_MAX_NET       = EnvDec("HARDVEN_HEDGE_MAX_NET", 1.0m);        // complete a hedge only if net ≤ this (1.0 = break-even)
    int     REVERSE_FLOOR_CENTS = EnvInt("HARDVEN_REVERSE_FLOOR_CENTS", 1);     // reverse sweeps the book down to this price to guarantee a fill
    int     REVERSE_MAX_ATTEMPTS= EnvInt("HARDVEN_REVERSE_MAX_ATTEMPTS", 4);    // sweep attempts before orphaning the remainder
    decimal TRADE_MAX_LOSS_MULT = EnvDec("HARDVEN_TRADE_MAX_LOSS_MULT", 3.0m);  // halt if one (hedged) fill lands >Nx worse than its edge
    bool    PER_TRADE_TRIPWIRE  = EnvBool("HARDVEN_PER_TRADE_TRIPWIRE", true);  // enable the per-trade tripwire above

    // In dry-run, probe real credentials before swapping in simulated clients.
    // This surfaces auth/connectivity issues without risking any orders.
    if (isDryRun)
    {
        try
        {
            long kBal = await orderClient.GetBalanceCentsAsync();
            decimal hvBal = await hardvenOrderClient.GetUsdcBalanceAsync();   // via sidecar /balance (0 if sidecar down)
            string hvNote = hvBal > 0m ? $"HardVen=${hvBal:0.00} (FX→USD)" : "HardVen=$0.00 (sidecar down or no wallet)";
            Console.WriteLine($"[CRED CHECK] Kalshi=${kBal / 100m:0.00} — credentials OK; {hvNote}");
        }
        catch (Exception ex)
        {
            Console.WriteLine($"[ERROR] Credential check failed: {ex.Message}");
            Console.WriteLine("[INFO] Fix credentials or use --telemetry for credential-free mode.");
            return;
        }
    }

    // ── BETTING-CONTRACT PREFLIGHT ────────────────────────────────────────────────────────────────────
    // The sidecar keeps its OWN hard stake cap (HARDVEN_MAX_STAKE) in a separate process, deliberately not the
    // same knob as the ladder's HARDVEN_STAKE_MAX: an independent ceiling is what catches a units/FX/depth bug
    // in THIS process before it becomes a real bet. Defence in depth only works if the two agree, though —
    // a sidecar cap BELOW the ladder's rung rejects the book leg AFTER the Kalshi leg has already filled, so
    // every arb becomes a naked leg + recovery. That is silent today (a rejected bet looks like any other miss)
    // and the two env names are one transposition apart, so verify it up front and refuse to arm on a mismatch.
    // Read the dress-rehearsal flag directly: `liveBetPath` is declared below, and the preflight has to run
    // before the execution clients are built.
    if (!isDryRun || Environment.GetEnvironmentVariable("HARDVEN_LIVE_BET_PATH") == "1")
    {
        try
        {
            using var hc = new HttpClient { Timeout = TimeSpan.FromSeconds(10) };
            using var hr = await hc.GetAsync($"{HARDVEN_SIDECAR_URL.TrimEnd('/')}/health");
            using var hd = JsonDocument.Parse(await hr.Content.ReadAsStringAsync());
            if (hd.RootElement.TryGetProperty("betting", out var bet))
            {
                decimal ladderCap = StakeLadder.MaxStakeAccount;   // 0 = uncapped
                bool betEnabled = bet.TryGetProperty("bet_enabled", out var be) && be.GetBoolean();
                decimal? sidecarCap = bet.TryGetProperty("max_stake", out var ms)
                                      && ms.ValueKind == JsonValueKind.Number ? ms.GetDecimal() : null;
                // With HARDVEN_STAKE_MAX set, Snap() only ever reduces, so the rung is <= that cap and the
                // comparison is exact. UNCAPPED, the stake is bounded only by depth and --max-bet, which we
                // cannot pin down here — so warn that the sidecar becomes the real (and silent) binding limit
                // rather than pretending MinRung is the worst case.
                decimal willSend = ladderCap;      // 0 = unbounded → unverifiable
                if (willSend > 0m && sidecarCap is decimal cap && cap < willSend)
                {
                    Console.ForegroundColor = ConsoleColor.Red;
                    Console.WriteLine(
                        $"[PREFLIGHT FAIL] Sidecar HARDVEN_MAX_STAKE={cap:0.00} is BELOW the stake this bot will " +
                        $"send ({willSend:0.00}). Every book leg would be REJECTED at the slip — after the Kalshi " +
                        $"leg has already filled — leaving a naked leg on every arb. Raise HARDVEN_MAX_STAKE to " +
                        $"at least {willSend:0.00} (and restart the sidecar), or lower HARDVEN_STAKE_MAX.");
                    Console.ResetColor();
                    return;
                }
                if (!betEnabled)
                    Console.WriteLine("[PREFLIGHT] NOTE: sidecar HARDVEN_BET_ENABLE=0 — the book leg will PREVIEW " +
                                      "only (no real bet). Under --live the Kalshi leg is still REAL.");
                if (willSend <= 0m)
                    Console.WriteLine($"[PREFLIGHT WARN] HARDVEN_STAKE_MAX is unset, so the ladder is uncapped and " +
                                      $"the sidecar's {sidecarCap?.ToString("0.00") ?? "?"} cap silently becomes the " +
                                      $"binding limit — any larger rung is rejected AFTER the Kalshi leg fills. " +
                                      $"Set HARDVEN_STAKE_MAX to make this checkable.");
                else
                    Console.WriteLine($"[PREFLIGHT OK] betting contract agrees — sidecar cap " +
                                      $"{sidecarCap?.ToString("0.00") ?? "none"} ≥ stake sent {willSend:0.00}, " +
                                      $"bet_enabled={betEnabled}.");
            }
            else
            {
                Console.WriteLine("[PREFLIGHT] sidecar /health has no `betting` block (older sidecar?) — " +
                                  "cannot verify HARDVEN_MAX_STAKE ≥ HARDVEN_STAKE_MAX. Check it by hand.");
            }
        }
        catch (Exception ex)
        {
            Console.ForegroundColor = ConsoleColor.Red;
            Console.WriteLine($"[PREFLIGHT FAIL] could not reach the sidecar to verify the betting contract: {ex.Message}");
            Console.WriteLine("[PREFLIGHT] refusing to arm — a live run needs the sidecar up anyway.");
            Console.ResetColor();
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
    // HardVen leg: the PAPER sim in dry-run (simulates Pinnacle fills — no browser, no real bet), the live
    // sidecar client in --live.
    //
    // HARDVEN_LIVE_BET_PATH=1 is the DRESS REHEARSAL: in dry-run, use the REAL sidecar client so the whole
    // placement chain executes for real — contracts→stake conversion, POST /bet, reply parsing, recovery on
    // the result — while Kalshi stays simulated. No money can move: Kalshi is a sim, and the sidecar refuses
    // to place unless HARDVEN_BET_ENABLE=1 with an implemented _place_via_ui(). It replies accepted=false,
    // so the bot sees a failed HardVen leg and runs the (simulated) Kalshi recovery — which is precisely the
    // path to rehearse. Ignored outside dry-run.
    bool liveBetPath = Environment.GetEnvironmentVariable("HARDVEN_LIVE_BET_PATH") == "1";
    // HARDVEN_DRYRUN_UI=1 = PROPER dry-run simulation: the simulated HardVen client first drives the REAL Pinnacle
    // UI verify-only (locate the game, click the moneyline, verify the popover, enter the stake — nothing placed)
    // and THEN simulates the fill. So a dry run exercises the true browser placement steps end-to-end, no money,
    // while fills/scenarios stay simulated. Needs the logged-in sidecar. Ignored under LIVE_BET_PATH / non-dry-run.
    bool dryRunUi = Environment.GetEnvironmentVariable("HARDVEN_DRYRUN_UI") == "1";
    IHardVenOrderExecutor hardvenExec = isDryRun && !liveBetPath
        ? new SimulatedHardVenClient(fillProfile!, dryRunUi ? hardvenOrderClient : null)
        : hardvenOrderClient;
    if (isDryRun && liveBetPath)
    {
        Console.ForegroundColor = ConsoleColor.Yellow;
        Console.WriteLine("[DRESS REHEARSAL] HARDVEN_LIVE_BET_PATH=1 — HardVen leg routes to the REAL sidecar " +
                          "/bet (Kalshi stays simulated). Placement is still blocked by the sidecar's own gates.");
        Console.ResetColor();
    }
    else if (isDryRun && dryRunUi)
    {
        Console.ForegroundColor = ConsoleColor.Cyan;
        Console.WriteLine("[DRYRUN UI] HARDVEN_DRYRUN_UI=1 — each HardVen BUY drives the REAL Pinnacle UI verify-only " +
                          "(locate + click moneyline + verify), then the fill is simulated. Nothing is placed; needs the logged-in sidecar.");
        Console.ResetColor();
    }
    // Total combined open-exposure cap. Default $1,000 = full deployment of the $500/platform capital;
    // the per-platform balance/buffer checks still gate each side so neither venue overdraws.
    decimal maxExposureUsd = EnvDec("HARDVEN_MAX_EXPOSURE_USD", 1000m);
    // Per-day cumulative-loss tripwire — a HARD halt requiring a manual reset, so it was the single most
    // consequential number in this file to have had no env at all.
    decimal maxDayLossUsd  = EnvDec("HARDVEN_MAX_DAY_LOSS_USD", 20m);
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
        pairCooldownSeconds: PAIR_COOLDOWN_SEC,
        fillTimeoutMs:       EnvInt("HARDVEN_FILL_TIMEOUT_MS", 5000),
        maxDayLossUsd:       maxDayLossUsd,
        dryRun:              isDryRun,
        minBuy:              minBuy,
        singleEntry:         singleEntry,
        allowReentry:        ALLOW_REENTRY,
        logErrors:           logErrors,
        tryN:                tryN,
        trySuccessOnly:      trySuccessOnly,
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
        executionWindowWeeks: execWindowWeeks,
        hardvenFxToUsd:       hardvenFxToUsd,
        hardvenCurrency:      Environment.GetEnvironmentVariable("HARDVEN_CURRENCY") ?? "EUR");
    telemetry.OnArbOpened  += executor.OnArbOpened;
    telemetry.BookUpdated  += executor.OnBookUpdate;  // event-driven early exit checks
    await executor.InitializeBalancesAsync();
    if (isLive && pairs.Count > 0)
        await executor.ReconcileOnStartupAsync(pairs);
    string execLabel  = isDryRun ? $"DRY RUN [{scenarioName}] — no real orders" : "LIVE";
    string minBuyTag  = minBuy ? "  MIN-BUY=1" : $"  maxBet=${MAX_BET_USD:0.00}";
    // The try budget is on this line because "why did the bot stop?" is the question it answers, and the
    // attempts-vs-positions distinction decides whether a miss ends the run.
    string tryTag = tryN is null ? "unlimited"
                  : $"{tryN} {(trySuccessOnly ? "POSITION(S) — misses are free" : "attempt(s) — a miss counts")}";
    // Show the reserve in DOLLARS, not just as a percentage. The percentage is a fraction of maxBet, so
    // "buffer=20%" tells you nothing about whether it fits inside your balance — which is the only thing
    // that decides if a bet is possible.
    decimal effectiveBufferUsd = MAX_BET_USD * BALANCE_BUFFER_PCT;
    string  bufferTag = bufferIsAbsolute
        ? $"buffer=${effectiveBufferUsd:0.00} (absolute)"
        : $"buffer={BALANCE_BUFFER_PCT:P0} of maxBet = ${effectiveBufferUsd:0.00}";
    Console.WriteLine($"[EXECUTOR] {execLabel} |{minBuyTag} {bufferTag} maxExposure=${maxExposureUsd:0.00} threshold={EXECUTION_THRESHOLD:0.000} cooldown={PAIR_COOLDOWN_SEC}s dayLoss=${maxDayLossUsd:0.00} try={tryTag}");

    // ── the reserve-vs-balance check ──────────────────────────────────────────────────────────────────
    // Sizing is floor((balance - buffer) / price) per platform, so a reserve at or above the balance makes
    // EVERY arb size to zero contracts and skip. Nothing else in the log says so: the bot connects, prices
    // flow, near-misses print, and the day reads as "no arbs were available". Observed live 2026-08-10 with
    // a $40 reserve (20% of a $200 maxBet) in front of a $12.88 account.
    foreach (var (venue, bal) in new[] { ("Kalshi", executor.KalshiBalanceUsd),
                                         ("HardVen", executor.HardVenBalanceUsd) })
    {
        if (effectiveBufferUsd < bal) continue;
        Console.ForegroundColor = ConsoleColor.Red;
        Console.WriteLine(
            $"[LIMITS] *** {venue} CANNOT TRADE: reserve buffer ${effectiveBufferUsd:0.00} >= balance ${bal:0.00}. " +
            $"Every arb will size to 0 contracts and skip as 'balance-limited'. ***");
        Console.WriteLine(
            $"[LIMITS]     Fix by funding {venue}, or lower the reserve: HARDVEN_BALANCE_BUFFER_USD=<abs $> " +
            $"(0 disables it) or HARDVEN_BALANCE_BUFFER_PCT=<fraction of maxBet>, or lower " +
            $"HARDVEN_MAX_BET_USD (currently ${MAX_BET_USD:0.00}).");
        Console.ResetColor();
        await discord.AlertAsync($"⚠️ **{BOT_NAME}** cannot trade on {venue}: reserve buffer " +
                                 $"${effectiveBufferUsd:0.00} ≥ balance ${bal:0.00}. Every arb sizes to 0 contracts.");
    }
    // Show the EXECUTION GATES explicitly — "why did/didn't it fire" is otherwise invisible until a skip line
    // appears, and the favourite gate's start state (env HARDVEN_FAVORITE_KALSHI_SPORTS, live-toggled by H) is
    // easy to misremember mid-session.
    Console.WriteLine($"[EXECUTOR] gates: favoriteOnKalshi={(executor.FavoriteGateOn ? "ON" : "OFF")} " +
                      $"(min {Environment.GetEnvironmentVariable("HARDVEN_FAVORITE_MIN") ?? "0.5"}, scope " +
                      $"{executor.FavoriteScope}, H toggles) | " +
                      $"preLiveOnly={(Environment.GetEnvironmentVariable("HARDVEN_PRELIVE_ONLY") != "0" ? "ON" : "OFF")} | " +
                      $"requireWsVerified={(Environment.GetEnvironmentVariable("HARDVEN_REQUIRE_WS_VERIFIED") != "0" ? "ON" : "OFF")} | " +
                      $"moneylineOnly={(Environment.GetEnvironmentVariable("HARDVEN_MONEYLINE_ONLY") != "0" ? "ON" : "OFF")} | " +
                      $"execNetFloor={EXEC_NET_FLOOR:0.000}");
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
    (executor != null ? "  H=FavHedge" : "") +
    (isDryRun ? "  I=InjectFavArb  O=InjectDogArb  U=InjectMismatch  K=SimReconnect  E=InjectErrors  X=DropHardVenBook" : "") +
    (isDebug  ? "  │  G=Discovery  T=Trades  W=Balance  F=Feed  R=Books" : ""));
_ = Task.Run(() =>
{
    try
    {
        _ = Console.KeyAvailable;   // probe once: throws NOW if stdin isn't a TTY, so the warning shows at startup
        while (!cts.Token.IsCancellationRequested)
        {
            if (!Console.KeyAvailable) { Thread.Sleep(50); continue; }
            var info = Console.ReadKey(intercept: true);
            var key  = info.Key;
            switch (key)
            {
                case ConsoleKey.N: DebugLog.NearMissEnabled   = !DebugLog.NearMissEnabled;   break;
                case ConsoleKey.A: DebugLog.StatusDashEnabled = !DebugLog.StatusDashEnabled; break;
                case ConsoleKey.H when executor != null:
                {
                    bool on = executor.ToggleFavoriteGate();
                    Console.WriteLine(on
                        ? "[KEYS] Favorite-on-Kalshi hedge ON — skipping the underdog-on-Kalshi direction (tennis retirement-void protection)"
                        : "[KEYS] Favorite-on-Kalshi hedge OFF — taking both directions by best edge (no void hedge)");
                    break;
                }
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
                case ConsoleKey.I when isDryRun:   // inject a FAVOURITE-on-Kalshi test arb
                case ConsoleKey.O when isDryRun:   // inject an UNDERDOG-on-Kalshi test arb
                {
                    // DRY-RUN gate tester: fire a synthetic arb through InjectTestArb, which runs the REAL executor
                    // path with the DATA-QUALITY gates bypassed (pre-live / WS-verify / stale — meaningless for a
                    // fabricated price) so it reaches the gates under test (favourite / ladder / recovery). Fills
                    // are simulated (no money). kAsk chooses the side: >0.5 favourite (proceeds), <0.5 underdog (skip).
                    if (executor == null) { Console.WriteLine("[KEYS] Executor not active — start --dry-run first"); break; }
                    bool favorite = key == ConsoleKey.I;
                    // Need all 4 books subscribed (for sizing/depth). PREFER a pre-live pair (its Pinnacle moneyline
                    // is live, so a DRYRUN_UI verify can locate it), but fall back to any subscribed pair since
                    // testMode bypasses the pre-live gate anyway.
                    var inj = pairs.FirstOrDefault(p => !p.ThreeWay
                            && state.Books.ContainsKey($"K:{p.KalshiTicker}") && state.Books.ContainsKey($"K:{p.KalshiTicker}_NO")
                            && state.Books.TryGetValue($"H:{p.HardVenYesTokenId}", out var y) && !y.IsLive
                            && state.Books.TryGetValue($"H:{p.HardVenNoTokenId}",  out var n) && !n.IsLive)
                        ?? pairs.FirstOrDefault(p => !p.ThreeWay
                            && state.Books.ContainsKey($"K:{p.KalshiTicker}") && state.Books.ContainsKey($"K:{p.KalshiTicker}_NO")
                            && state.Books.ContainsKey($"H:{p.HardVenYesTokenId}") && state.Books.ContainsKey($"H:{p.HardVenNoTokenId}"));
                    if (inj == null)
                    {
                        Console.WriteLine("[KEYS] inject: no pair with all 4 books subscribed yet — wait a moment for books to load, then retry");
                        break;
                    }
                    decimal kAsk = favorite ? 0.55m : 0.45m;   // Kalshi YES ask: >0.5 favourite, <0.5 underdog
                    // net = kAsk + pAsk + Kalshi fee(~0.017) must sit in the EXECUTABLE band [0.90, 0.985]:
                    // favourite 0.55+0.40+0.017=0.967, underdog 0.45+0.50+0.017=0.967 — both clear the thin-margin
                    // floor (0.985) and the too-good floor (0.90), so they actually reach sizing/ladder/fill.
                    decimal pAsk = favorite ? 0.40m : 0.50m;
                    Console.WriteLine($"[KEYS] INJECT {(favorite ? "FAVOURITE" : "UNDERDOG")}-on-Kalshi test arb -> {inj.Label} " +
                                      $"(kAsk={kAsk:0.00} pAsk={pAsk:0.00}) | K_YES_P_NO");
                    executor.InjectTestArb(inj.PairId, "K_YES_P_NO", kAsk, pAsk);
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
        Console.ForegroundColor = ConsoleColor.Yellow;
        Console.WriteLine("[KEYS] DISABLED — stdin is not an interactive terminal (redirected / piped / no TTY). " +
                          "The I/O/H/U/K/E/X keypress toggles will NOT fire — that is why an inject does nothing. " +
                          "Run the bot DIRECTLY in a terminal (not via a pipe, `>` redirect, `< nul`, nohup, or a " +
                          "run-config that detaches stdin) to use them.");
        Console.ResetColor();
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

// Uptime baseline = the whole RUN's start (the supervisor writes .run_started), so the daily 6am bot recycle
// doesn't reset "up 0d0h" — which read like a crash. A FRESH file (<5m old) = a genuine start; older = a
// recycle/crash-restart mid-run. Falls back to now if the file is missing (e.g. launched without the supervisor).
DateTime runStartedAt = DateTime.UtcNow;
bool isFreshStart = true;
try
{
    string rsPath = Path.Combine(sourceDir, ".run_started");
    if (File.Exists(rsPath) &&
        DateTime.TryParse(File.ReadAllText(rsPath).Trim(), null,
            System.Globalization.DateTimeStyles.RoundtripKind, out var rs))
    {
        runStartedAt  = rs.ToUniversalTime();
        isFreshStart  = (DateTime.UtcNow - runStartedAt).TotalMinutes < 5;
    }
}
catch { }

// HONEST book count: "LIVE" = received a delta, not resolved/halted (IsDead), and updated within the fresh
// window — i.e. currently providing pricing. The old "HasReceivedDelta" count only ever LATCHED up (never
// decremented), so it read a misleading constant (e.g. HardVen stuck at 772) even when most books were stale
// overnight. This reflects real coverage right now. `HARDVEN_BOOK_FRESH_SEC` (default 120) sets the window.
double bookFreshSec = double.TryParse(Environment.GetEnvironmentVariable("HARDVEN_BOOK_FRESH_SEC"), out var bfs) && bfs > 0
    ? bfs : 120;
int LiveBooks(string prefix)
{
    var now = DateTime.UtcNow;
    return state.Books.Count(kv => kv.Key.StartsWith(prefix) && kv.Value.HasReceivedDelta
        && !kv.Value.IsDead && (now - kv.Value.LastDeltaAt).TotalSeconds <= bookFreshSec);
}
int TotalBooks(string prefix) => state.Books.Count(kv => kv.Key.StartsWith(prefix));

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
    // Fresh run → "started"; a mid-run recycle/restart → a quieter "reloaded" note so the morning recycle doesn't
    // read like a crash (and the uptime below is the RUN's, not this process's).
    _ = discord.AlertAsync(isFreshStart
        ? $"🟢 {modeLabel} started — {pairs.Count} pair(s), sidecar {HARDVEN_SIDECAR_URL}. Heartbeat every {heartbeatMin}m."
        : $"🔄 {modeLabel} reloaded (daily recycle / restart) — {pairs.Count} pair(s), run uptime unbroken.");
    _ = Task.Run(async () =>
    {
        // Debounced down/up tracking per signal. everUp gates out the startup warm-up (nothing is "lost" until
        // it was up at least once); downSince + downGraceSec suppress the brief re-capture gap on every scheduled
        // reopen; alerted latches so a stuck signal fires 🔴 exactly once (and its 🟢 recovery once).
        var downSince = new Dictionary<string, DateTime?> { ["session"] = null, ["hardven"] = null, ["kalshi"] = null };
        var everUp    = new Dictionary<string, bool>      { ["session"] = false, ["hardven"] = false, ["kalshi"] = false };
        var alerted   = new Dictionary<string, bool>      { ["session"] = false, ["hardven"] = false, ["kalshi"] = false };
        var lastHeartbeat = DateTime.UtcNow;
        // Edge-detect the session coming up, to refresh balances immediately (see below). Seeded from the
        // CURRENT state so a bot started while already logged in doesn't fire a redundant refresh.
        bool sessionWasReady = hardvenFeed.SessionReady;

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

                // BALANCE ON SESSION-OPEN. While the browser is dark /balance answers null (unreadable, not
                // zero) and the C# client maps that to $0 — so `pAffordable` is 0 and EVERY arb skips for
                // insufficient funds. The periodic refresh is on a 5-MINUTE timer, so a window that opens at
                // 05:00 could spend its first minutes unable to trade — and the Asian morning slate front-loads
                // arbs at open. Refresh the moment the session actually comes up instead of waiting for the tick.
                if (hardvenFeed.SessionReady && !sessionWasReady && executor != null)
                {
                    Console.WriteLine("[BALANCE] session came up — refreshing balances now (not waiting for the 5m tick)");
                    _ = executor.RefreshBalancesNowAsync();
                }
                sessionWasReady = hardvenFeed.SessionReady;

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
                    int kLive = LiveBooks("K:"), pLive = LiveBooks("H:");
                    int kTotal = TotalBooks("K:"), pTotal = TotalBooks("H:");
                    // How many HardVen books are IN-PLAY (IsLive) right now — if this stays 0 while games are live,
                    // the paired tokens aren't following games into in-play (why in-play arbs never log).
                    int pInplay = state.Books.Count(kv => kv.Key.StartsWith("H:") && kv.Value.IsLive);
                    var up = nowDt - runStartedAt;
                    string sessTag = darkNow ? "dark (scheduled)" : hardvenFeed.SessionReady ? "ready" : "DOWN";
                    _ = discord.AlertAsync(
                        $"💓 up {up.Days}d{up.Hours}h{up.Minutes}m │ session {sessTag} │ " +
                        $"live books K={kLive}/{kTotal} H={pLive}/{pTotal} (H in-play={pInplay}) │ WS K={(kOk ? "ok" : "down")} H={(hvOk ? "ok" : "down")} │ " +
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
                StandardOutputEncoding = System.Text.Encoding.UTF8,   // else the em-dash/arrow return as cp1252 mojibake (ÔÇö/ÔåÆ)
                StandardErrorEncoding = System.Text.Encoding.UTF8,
            };
            psi.Environment["PYTHONUTF8"] = "1";                      // force Python to emit UTF-8 on the pipe
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
        int kLive = LiveBooks("K:"), pLive = LiveBooks("H:");
        int kTotal = TotalBooks("K:"), pTotal = TotalBooks("H:");
        string sess = hardvenFeed.ScheduledDark ? "dark (scheduled)"
                    : hardvenFeed.SessionReady ? "ready" : "DOWN";
        var up = DateTime.UtcNow - runStartedAt;
        string live = $"📊 **status** — session {sess} | live books K={kLive}/{kTotal} H={pLive}/{pTotal} | " +
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
        onShutdown: ShutdownHookAsync,
        sidecarBaseUrl: HARDVEN_SIDECAR_URL,   // session/schedule verbs go to the sidecar's control plane
        botTag: BOT_TAG);                      // only answer commands addressed to this venue
    if (cmdListener.Enabled)
    {
        Console.WriteLine("[DISCORD CMD] remote commands enabled: status / help / pause / resume / force / " +
                          "schedule / pin / unpin / toggle / close");
        _ = Task.Run(() => cmdListener.RunAsync(cts.Token));
    }

    // BALANCE PUSH: the sidecar holds no Kalshi credentials, so the guard (which halts the schedule when
    // EITHER leg runs dry) needs our Kalshi cash pushed to it. Best-effort; a failed push leaves the figure
    // STALE on the sidecar, which is explicitly treated as "unknown" — never as zero — so a dead push path
    // can't halt a healthy bot.
    _ = Task.Run(async () =>
    {
        var http = new HttpClient { Timeout = TimeSpan.FromSeconds(10) };
        int periodSec = int.TryParse(Environment.GetEnvironmentVariable("HARDVEN_BALANCE_PUSH_SEC"), out var ps)
                        && ps > 0 ? ps : 300;
        while (!cts.Token.IsCancellationRequested)
        {
            try
            {
                decimal bal = (await orderClient.GetBalanceCentsAsync()) / 100m;
                if (!string.IsNullOrEmpty(HARDVEN_SIDECAR_URL))
                    using (await http.PostAsync(
                        $"{HARDVEN_SIDECAR_URL.TrimEnd('/')}/control/balance?kalshi_usd={bal.ToString(System.Globalization.CultureInfo.InvariantCulture)}",
                        null)) { }
            }
            catch { /* best-effort: never disturb trading */ }
            try { await Task.Delay(periodSec * 1000, cts.Token); }
            catch (OperationCanceledException) { break; }
        }
    });

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

                // A scheduled dark window CLOSES the browser, so the HardVen feed going quiet is the plan
                // working, not a venue disconnect. Without this the watchdog cried CONNECTION HALT to Discord
                // every dark stretch (3x on the 2026-08-06 soak), which would mask a real overnight outage.
                // Kalshi is still expected up while dark, so it keeps its full watchdog.
                bool darkNow = hardvenFeed.ScheduledDark;
                bool kOk = kalshiFeed.IsConnected;
                bool pOk = hardvenFeed.IsConnected || darkNow;

                // ── WS connect/disconnect transitions ──────────────────────────
                if (!kOk && lastKOk) Console.WriteLine("[WATCHDOG] Kalshi disconnected — halting new trades");
                if (!pOk && lastPOk) Console.WriteLine("[WATCHDOG] HardVen disconnected — halting new trades");
                if ( kOk && !lastKOk) Console.WriteLine("[WATCHDOG] Kalshi reconnected — resuming trades");
                if ( pOk && !lastPOk) Console.WriteLine($"[WATCHDOG] HardVen {(darkNow ? "dark (scheduled) — not a disconnect" : "reconnected — resuming trades")}");
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

                // `!darkNow` is load-bearing: pOk is forced true while dark (above), so without it this would
                // REST-ping a deliberately-closed venue every 60s and halt on the failure — the same false
                // alarm by another route.
                if (pOk && !darkNow && pSilS >= WS_SILENCE_THRESHOLD_S
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
                    $"K=${executor.KalshiBalanceUsd:0.00}  P={CrossArbExecutor.BalStr(executor.HardVenBalanceUsd)}  │  " +
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
            // Share-tolerant read: FileShare.Delete lets the pairers' atomic os.replace(temp → file) rename the
            // file WHILE we hold it open (we keep reading the complete old file). Combined with the pairers now
            // writing atomically, the hot-reload can never see a half-written file — fixes the mid-write NRE.
            string reloadJson;
            using (var fs = new FileStream(reloadPath, FileMode.Open, FileAccess.Read,
                                           FileShare.ReadWrite | FileShare.Delete))
            using (var sr = new StreamReader(fs))
                reloadJson = sr.ReadToEnd();
            if (string.IsNullOrWhiteSpace(reloadJson))
            {
                DebugLog.Write($"Hot-reload: {reloadPath} empty (mid-write?), skipping this cycle");
                continue;
            }
            using var doc = JsonDocument.Parse(reloadJson);
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

            // Newly paired tokens have no fee params / tick size yet. Without this they keep defaults for
            // the rest of the session, so the executor prices them differently from the detector that
            // found the arb. Fire-and-forget: it is rate-limited (~1.5s/token) and must not block reload.
            if (executor != null && validPList.Count > 0)
            {
                var ex2 = executor;
                _ = Task.Run(async () =>
                {
                    try { await ex2.PrefetchFeeRatesForNewPairsAsync(); }
                    catch (Exception e) { Console.WriteLine($"[FEE PREFETCH] re-pair prefetch failed: {e.Message}"); }
                });
            }
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

// --stop-after: wall-clock watchdog. Cancels the same cts a double-Ctrl+C would, so the normal shutdown
// sequence (telemetry flush → executor flush → optional sidecar stop) runs unchanged.
if (stopAfter is TimeSpan limit)
{
    Console.WriteLine($"[STOP-AFTER] run ends in {limit.TotalMinutes:0} min ({DateTime.Now.Add(limit):HH:mm}) " +
                      (tryN is int n ? $"or after {n} executed arb(s), whichever comes first." : "unless stopped sooner."));
    _ = Task.Run(async () =>
    {
        try { await Task.Delay(limit, cts.Token); }
        catch (OperationCanceledException) { return; }   // stopped earlier (--try hit / Ctrl+C) — nothing to do
        Console.WriteLine($"\n[STOP-AFTER] {limit.TotalMinutes:0} min elapsed — shutting down.");
        cts.Cancel();
    });
}

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
// --stop-sidecar: tear down the sidecar too (closes the managed browser + Pinnacle session). Done LAST, after
// telemetry/executor have flushed, so nothing is mid-write. The sidecar refuses (409) while a bet is in flight.
if (stopSidecar)
{
    string url = $"{HARDVEN_SIDECAR_URL.TrimEnd('/')}/shutdown";
    try
    {
        Console.WriteLine($"[SHUTDOWN] --stop-sidecar: POST {url}");
        using var stopHttp = new HttpClient { Timeout = TimeSpan.FromSeconds(10) };
        using var resp = await stopHttp.PostAsync(url, null);
        if (resp.IsSuccessStatusCode)
            Console.WriteLine("[SHUTDOWN] sidecar stop requested — browser + Pinnacle session closing.");
        else if ((int)resp.StatusCode == 404)
            Console.WriteLine("[SHUTDOWN] sidecar has NO /shutdown endpoint (HTTP 404) — it is running an OLD build. "
                            + "RESTART the sidecar to pick it up; stop this one by hand.");
        else if ((int)resp.StatusCode == 409)
            Console.WriteLine("[SHUTDOWN] sidecar refused: a bet is in flight (HTTP 409) — left running on purpose. "
                            + "Check the position, then stop it by hand.");
        else
            Console.WriteLine($"[SHUTDOWN] sidecar refused to stop (HTTP {(int)resp.StatusCode}) — stop it by hand.");
    }
    catch (Exception ex)
    {
        Console.WriteLine($"[SHUTDOWN] could not reach the sidecar at {url} ({ex.GetType().Name}) — stop it by hand.");
    }
}
else
    DebugLog.Write("--stop-sidecar not set — leaving the sidecar (and its browser) running.");
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
