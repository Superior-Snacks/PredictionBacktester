using System.Globalization;
using PredictionBacktester.Engine.LiveExecution;
using PredictionBacktester.Engine.Notifications;

namespace KalshiEvBot;

/// <summary>
/// M0 of the +EV taker bot: observation only.
///
/// <para>Pinnacle, de-vigged, is the fair value. Kalshi is the venue. There is no second leg, no hedge and
/// no camping — the two-leg execution problem is not solved here, it is deleted. What is given up is that
/// an arb was risk-free and a value bet is not, which is why this milestone exists: it places NOTHING, and
/// logs what it would have done so M1 can grade those signals against settlement.</para>
///
/// <para><b>The order API is not wired in this build.</b> Not gated behind a flag — absent. The only way to
/// place a trade from this project is to write the code to do it.</para>
/// </summary>
internal static class Program
{
    private static async Task<int> Main(string[] args)
    {
        if (args.Contains("--self-test")) return SelfTest.Run();
        if (args.Contains("--help") || args.Contains("-h")) { Usage(); return 0; }

        bool once      = args.Contains("--once");
        bool verbose   = args.Contains("--verbose");
        bool bookAudit = args.Contains("--book-audit");
        string? pairsArg = ArgValue(args, "--pairs");
        // M1. Opt-in and impossible to enter by accident: without this flag no LiveExecutor is constructed,
        // so the order API is unreachable rather than merely unused.
        bool live      = args.Contains("--live");
        // --micro-bet is a LABEL, not a second configuration path. It states the intent of the run in the
        // banner and changes nothing else: the stake comes from --min-stake / EV_LIVE_STAKE_SIDE either
        // way. An earlier cut gave it its own EV_MICRO_STAKE_* pair, which meant two env vars silently
        // fighting over one knob — passing the flag would have overridden a deliberate EV_LIVE_STAKE_SIDE
        // without saying so. One knob, one source.
        bool micro = args.Contains("--micro-bet");
        double stakeSide = ArgDouble(args, "--min-stake") ?? EvConfig.Env("EV_LIVE_STAKE_SIDE", 5.0);
        double stakeGame = ArgDouble(args, "--max-stake-game") ?? EvConfig.Env("EV_LIVE_STAKE_GAME", 2 * stakeSide);
        if (micro && !live)
            Console.WriteLine("[MICRO] --micro-bet is a label only; it places nothing on its own. "
                            + "Add --live to trade.");

        if (live)
        {
            Console.WriteLine($"┌─ Kalshi +EV taker bot — M1 (LIVE: REAL ORDERS){(micro ? " [MICRO-BET]" : "")} ─────────────────────");
            Console.WriteLine("│  Pinnacle de-vigged = fair value.  Kalshi WS detects, Kalshi REST values.");
            Console.WriteLine($"│  IOC buys on every confirmed signal. ${stakeSide:0.00}/side, ${stakeGame:0.00}/game,");
            Console.WriteLine("│  one FILLED entry per side. A no-fill costs nothing and may be retried.");
            if (micro)
                Console.WriteLine("│  MICRO-BET: sized to measure the FILL RATE, not to earn — at this size");
            if (micro)
                Console.WriteLine("│  the rounded-up fee is a real drag. See --resolve §8 for what it costs.");
            Console.WriteLine("│  THIS SPENDS REAL MONEY. Ctrl+C now if that was not the intention.");
            Console.WriteLine("└────────────────────────────────────────────────────────────────────────────");
        }
        else
        {
            Console.WriteLine("┌─ Kalshi +EV taker bot — M0 (OBSERVATION ONLY) ─────────────────────────────");
            Console.WriteLine("│  Pinnacle de-vigged = fair value.  Kalshi WS detects, Kalshi REST values.");
            Console.WriteLine("│  No order API is wired in this build. Nothing here can place a trade.");
            Console.WriteLine("└────────────────────────────────────────────────────────────────────────────");
        }

        // ── Credentials + pairs ───────────────────────────────────────────────────────────────────────
        var config = KalshiApiConfig.FromEnvironment();      // also loads the solution-root .env
        if (string.IsNullOrWhiteSpace(config.ApiKeyId) || string.IsNullOrWhiteSpace(config.PrivateKeyPath))
        {
            Console.WriteLine("[FATAL] KALSHI_API_KEY_ID / KALSHI_PRIVATE_KEY_PATH not set.");
            return 2;
        }

        // M1 FIRST, BEFORE ANYTHING TOUCHES THE PAIR FILE. Grading reads logged CSVs and settlement; it has
        // no interest in today's fixtures. Sitting behind the pair checks meant an empty or freshly-rescanned
        // pair file killed it outright — observed 2026-08-21, `--resolve` refused with "nothing to watch"
        // while three telemetry files sat next to it waiting to be graded. Historical data must stay
        // readable whatever the board looks like right now.
        if (args.Contains("--resolve"))
        {
            using var rk = new KalshiOrderClient(config);
            return await ResolveAsync(rk, ArgValue(args, "--resolve-glob"), !args.Contains("--all-obs"));
        }

        string? pairsPath = EvPairLoader.Locate(pairsArg);
        if (pairsPath is null)
        {
            Console.WriteLine("[FATAL] cross_pairs.json not found. Pass --pairs <path> or set EV_PAIRS_FILE.");
            return 2;
        }

        List<EvPair> pairs;
        List<string> report;
        try { pairs = EvPairLoader.Load(pairsPath, out report); }
        catch (Exception ex) { Console.WriteLine($"[FATAL] reading {pairsPath}: {ex.Message}"); return 2; }

        Console.WriteLine($"[PAIRS] {pairs.Count} usable pair(s) from {pairsPath}");
        foreach (var line in report) Console.WriteLine($"[PAIRS] {line}");
        if (pairs.Count == 0)
        {
            Console.WriteLine("[FATAL] nothing to watch. Run the pairing job first — this bot never pairs, "
                            + "it reads the file the arb bot's pairing job maintains.");
            return 2;
        }

        // Stop before ANY connection. Kalshi allows one WebSocket per account, and a second one connects
        // happily and receives nothing — so "just checking the pair file" must never be able to silently
        // blind a bot that is already running.
        if (args.Contains("--check"))
        {
            Console.WriteLine($"[CHECK] {pairs.Count} pair(s), "
                            + $"{pairs.Select(p => p.KalshiTicker).Distinct().Count()} ticker(s), "
                            + $"{pairs.SelectMany(p => p.Legs).Distinct().Count()} "
                            + "Pinnacle selection(s). No connection was opened.");
            foreach (var p in pairs.Take(5))
                Console.WriteLine($"        {p.KalshiTicker,-38} {Trunc(p.EventTitle, 26),-26} "
                                + $"yes={p.YesToken} no={p.NoToken}");
            if (pairs.Count > 5) Console.WriteLine($"        … and {pairs.Count - 5} more");
            return 0;
        }

        var cfg = new EvConfig();
        string sidecar = ArgValue(args, "--sidecar")
                      ?? Environment.GetEnvironmentVariable("HARDVEN_SIDECAR_URL")
                      ?? "http://127.0.0.1:8787";

        Console.WriteLine($"[CONFIG] EV_MIN={cfg.EvMin:0.####} (prescreen slack {cfg.PrescreenSlack:0.####})  "
                        + $"price window {cfg.MinPrice:0.00}-{cfg.MaxPrice:0.00}  de-vig={cfg.DeVigMethod}  "
                        + $"fee rate={EvMath.FeeRate:0.####}  sidecar={sidecar}");
        Console.WriteLine("[CONFIG] Kalshi allows ONE WebSocket per account: do not run this alongside the "
                        + "arb bot. The second connection succeeds and silently receives no books.");

        var tickers = pairs.Select(p => p.KalshiTicker).Distinct(StringComparer.Ordinal).ToList();
        // POLL THE LEG SET, NOT THE TOKEN PAIR. `Legs` is the complete outcome set (identical to
        // {Yes,No} on a two-way, all three on a 1X2), and it is what the evaluator prices from. Building
        // the poll list from Yes/No only WORKS BY ACCIDENT on soccer: across an event's three rows the
        // YesTokens happen to cover home, away and draw — but only while all three markets pair. Let the
        // Tie row fail to match and the draw token still sits in its siblings' Legs while nothing ever
        // fetches it, so every row of that event returns NoQuote forever with nothing in the log to say why.
        var tokens  = pairs.SelectMany(p => p.Legs)
                           .Distinct(StringComparer.Ordinal).ToList();

        using var kalshi    = new KalshiOrderClient(config);
        using var telemetry = new EvTelemetry();
        using var snapshots = new OracleSnapshotLog();
        var oracle = new PinnacleOracle(sidecar, tokens);
        var feed   = new KalshiBookFeed(kalshi, config, tickers);
        cfg.Live = live;
        cfg.LiveStakePerSideUsd = stakeSide;
        cfg.LiveStakePerGameUsd = stakeGame;
        var eval   = new EvEvaluator(pairs, oracle, feed, kalshi, telemetry, cfg) { Verbose = verbose };
        EvLiveLog? liveLog = null;
        if (live)
        {
            liveLog = new EvLiveLog();
            var posStore = new LivePositionStore();
            eval.EnableLive(new LiveExecutor(kalshi, liveLog, cfg, posStore));
            Console.WriteLine($"[LIVE   ] {liveLog.Path}  (every order attempt, filled or not)");
            Console.WriteLine($"[STATE  ] {posStore.Path}  — {posStore.LoadNote}");
        }
        using var followUp = new FollowUpTracker(oracle, feed);
        eval.SetFollowUp(followUp);

        kalshi.RateLimitRetryLogger = i =>
        {
            Interlocked.Increment(ref eval.Stats.RateLimited);
            if (verbose) Console.WriteLine($"[429] {i.Method} {i.Path} backing off {i.DelaySeconds:0.##}s "
                                         + $"(attempt {i.Attempt}/{i.MaxAttempts})");
        };

        Console.WriteLine($"[TELEMETRY] {telemetry.Path}");
        Console.WriteLine($"[FOLLOWUP ] {followUp.Path}  (both venues re-read at "
                        + $"{followUp.CheckpointsDescription} after every candidate — closing-line value)");
        Console.WriteLine($"[SNAPSHOT ] {snapshots.Path}  (every {EvConfig.Env("EV_SNAPSHOT_MIN", 5):0} min, "
                        + "oracle only — this is what M1 grades soonest)");

        using var cts = new CancellationTokenSource();
        Console.CancelKeyPress += (_, e) => { e.Cancel = true; cts.Cancel(); };

        // BEFORE the evaluator starts. M changes EV, the IOC limit and the Kelly size, and it is a live
        // per-series field that can move under a running bot — so it is read once here rather than assumed,
        // and a non-standard value is shouted in the banner instead of being discovered from a losing month.
        await eval.PrimeFeeMultipliersAsync(cts.Token);

        var feedTask   = feed.RunAsync(cts.Token);
        var oracleTask = oracle.RunAsync(cts.Token);
        var evalTask   = eval.RunAsync(cts.Token);

        // Both triggers feed the same queue. Kalshi ticking is one source of signals; Pinnacle moving is
        // the other, and a bot woken only by Kalshi would never see the second kind at all.
        feed.OnBookChanged += eval.Nudge;
        oracle.OnPolled    += eval.SweepAll;

        // ── Discord: report and remote control ───────────────────────────────────────────────────
        // WHY THE LISTENER CANNOT HURT TRADING. It is a poll loop on its own task; every failure inside it
        // is swallowed and logged, it holds no lock the evaluator wants, and `resolve` runs OUT OF PROCESS.
        // No-ops entirely unless BOTH a bot token and a channel id are set, so an unconfigured run is
        // byte-identical to before.
        var discord = new DiscordNotifier(Environment.GetEnvironmentVariable("DISCORD_WEBHOOK_URL"),
                                          botName: "KalshiEvBot");
        var started = DateTime.UtcNow;

        async Task<string> BuildStatusAsync()
        {
            await Task.CompletedTask;
            var up = DateTime.UtcNow - started;
            var sb = new System.Text.StringBuilder();
            sb.Append(live ? "**EV bot — LIVE**" : "**EV bot — observing (M0)**");
            sb.AppendLine($"  _up {up.TotalHours:0.0}h_");
            sb.Append($"`bankroll ${eval.BankrollUsd:0.00}   pairs {pairs.Count}   ");
            sb.AppendLine($"signals {eval.Stats.Signals}   rest {eval.Stats.RestCalls}   429s {eval.Stats.RateLimited}`");
            var ex = eval.LiveExec;
            if (ex is not null)
            {
                sb.AppendLine($"`{ex.Summary()}`");
                // The number M1 exists to produce. Called out separately from the counters so it is the
                // thing the eye lands on.
                if (ex.Attempted > 0)
                    sb.Append($"**fill rate {100.0 * ex.Filled / ex.Attempted:0.0}%** "
                            + $"({ex.Filled}/{ex.Attempted}), staked ${ex.StakedUsd:0.00}");
            }
            else sb.Append("_no orders in this build_");
            return sb.ToString();
        }

        async Task ShutdownHookAsync()
        {
            await Task.CompletedTask;
            Console.WriteLine("[DISCORD CMD] shutdown requested — cancelling.");
            cts.Cancel();
        }

        // OUT OF PROCESS ON PURPOSE. A resolve takes minutes and competes for the same Kalshi REST budget;
        // running it inline would stall screening for the whole of it. The digest script already knows how
        // to run the report and post it, so this reuses it rather than re-implementing the formatting.
        async Task ResolveHookAsync()
        {
            string root = Directory.GetCurrentDirectory();
            string script = Path.Combine(root, "ev_report_discord.py");
            if (!File.Exists(script)) { await discord.AlertAsync($"cannot find {script}"); return; }
            var psi = new System.Diagnostics.ProcessStartInfo("python", $"\"{script}\"")
            { WorkingDirectory = root, UseShellExecute = false,
              RedirectStandardOutput = true, RedirectStandardError = true };
            using var proc = System.Diagnostics.Process.Start(psi);
            if (proc is null) { await discord.AlertAsync("could not start the report process."); return; }
            await proc.WaitForExitAsync();
            if (proc.ExitCode != 0)
                await discord.AlertAsync($"report exited {proc.ExitCode} — check the console.");
        }

        var cmdListener = new DiscordCommandListener(
            Environment.GetEnvironmentVariable("DISCORD_BOT_TOKEN"),
            Environment.GetEnvironmentVariable("DISCORD_CHANNEL_ID"),
            reply:      m => discord.AlertAsync(m),
            onStatus:   BuildStatusAsync,
            onShutdown: ShutdownHookAsync,
            sidecarBaseUrl: sidecar,          // pause/resume/force/schedule/pin still reach the sidecar
            botTag: "ev",
            onResolve: ResolveHookAsync,
            // The EV bot CONSUMES the sidecar as an odds oracle; it does not own the browser lifecycle.
            // Tearing it down on `ev close` would stop a service the operator never asked to stop.
            shutdownSidecarOnClose: false);
        if (cmdListener.Enabled)
        {
            Console.WriteLine("[DISCORD CMD] remote commands ON — address them to this bot: "
                            + "`ev status` / `ev resolve` / `ev close` (sidecar verbs also forwarded).");
            _ = Task.Run(() => cmdListener.RunAsync(cts.Token));
        }
        if (discord.Enabled)
            _ = Task.Run(() => PerformanceLoopAsync(discord, BuildStatusAsync, cts.Token));

        await RefreshBankrollAsync(kalshi, eval, cfg, announce: true);
        var bankrollTask = BankrollLoopAsync(kalshi, eval, cfg, cts.Token);
        var snapTask     = SnapshotLoopAsync(snapshots, oracle, feed, pairs, cfg, cts.Token);
        var followTask   = followUp.RunAsync(cts.Token);

        // Bank settlements WHILE THE BOT RUNS. Kalshi does not keep obscure markets available forever, so
        // resolving days later is a race we can only lose — and lose silently, since a purged market is
        // indistinguishable from one that never settled unless we recorded the difference at the time.
        //
        // It watches EVERY ticker ever seen this session, not just the current watchlist: yesterday's
        // fixtures leave the pair file the moment they finish, which is exactly when their result appears.
        var everSeen  = new HashSet<string>(tickers, StringComparer.Ordinal);
        var pairsLock = new object();
        var resolver  = new SettlementResolver(kalshi);
        var settleTask = resolver.WatchAsync(
            () => { lock (pairsLock) return everSeen.ToList(); }, cts.Token);

        // Pick up new fixtures without a restart — see PairReloadLoopAsync for why this is not optional.
        var reloadTask = PairReloadLoopAsync(pairsPath, eval, feed, oracle, pairs, everSeen, pairsLock, cts.Token);

        if (args.Contains("--verify"))
        {
            int rc = await VerifyModeAsync(pairs, pairsPath, feed, oracle, eval, telemetry, snapshots,
                                           resolver, cfg, kalshi, cts.Token);
            cts.Cancel();
            await SafeAll(feedTask, oracleTask, evalTask, bankrollTask, snapTask, settleTask, reloadTask, followTask);
            return rc;
        }

        if (bookAudit)
        {
            await WaitWarmAsync(feed, oracle, cts.Token, TimeSpan.FromSeconds(45));
            await BookAuditAsync(feed, kalshi, oracle, pairs, ArgInt(args, "--book-audit") ?? 10);
            cts.Cancel();
            await SafeAll(feedTask, oracleTask, evalTask, bankrollTask, snapTask, settleTask, reloadTask, followTask);
            return 0;
        }

        if (once)
        {
            if (!await WaitWarmAsync(feed, oracle, cts.Token, TimeSpan.FromSeconds(45)))
                Console.WriteLine("[ONCE] feeds did not fully warm up — evaluating with what arrived.");
            await Task.Delay(3_000, cts.Token).ContinueWith(_ => { });
            SnapshotOnce(snapshots, oracle, feed, pairs, cfg);   // one oracle row per pair before we exit
            eval.SweepAll();
            // Bounded: the oracle keeps re-sweeping every few seconds, so Pending is not guaranteed to
            // reach zero on its own and an unbounded wait would simply never return.
            var drainBy = DateTime.UtcNow.AddSeconds(60);
            while (eval.Pending > 0 && DateTime.UtcNow < drainBy && !cts.IsCancellationRequested)
                await Task.Delay(200);
            await Task.Delay(3_000, cts.Token).ContinueWith(_ => { });   // let in-flight REST calls land
            PrintStatus(eval, feed, oracle, telemetry, snapshots, resolver, pairsPath);
            await resolver.ResolveAsync(tickers, CancellationToken.None);   // bank before we exit
            cts.Cancel();
            await SafeAll(feedTask, oracleTask, evalTask, bankrollTask, snapTask, settleTask, reloadTask, followTask);
            return 0;
        }

        var statusTask = StatusLoopAsync(eval, feed, oracle, telemetry, snapshots, resolver, pairsPath, cts.Token);
        await SafeAll(feedTask, oracleTask, evalTask, bankrollTask, snapTask, settleTask, reloadTask, followTask, statusTask);
        PrintStatus(eval, feed, oracle, telemetry, snapshots, resolver, pairsPath);
        Console.WriteLine($"[DONE] {telemetry.RowsWritten} row(s) → {telemetry.Path}");
        return 0;
    }

    // ── M1: grade what has been logged ────────────────────────────────────────────────────────────────
    /// <summary>
    /// Reads every telemetry and snapshot CSV in the working directory, fetches each market's settlement,
    /// and prints the calibration report. REST only — no WebSocket, so it is safe to run at any time,
    /// including while another bot holds the account's single socket.
    /// </summary>
    private static async Task<int> ResolveAsync(KalshiOrderClient kalshi, string? glob, bool dedupe)
    {
        string dir = Directory.GetCurrentDirectory();
        var files = new List<string>();
        foreach (var pattern in glob is null ? new[] { "EvTelemetry_*.csv", "EvOracleSnap_*.csv" } : new[] { glob })
            files.AddRange(Directory.GetFiles(dir, pattern));
        files = files.Distinct(StringComparer.OrdinalIgnoreCase).OrderBy(f => f).ToList();

        if (files.Count == 0)
        {
            Console.WriteLine($"[RESOLVE] no EvTelemetry_*.csv or EvOracleSnap_*.csv in {dir}. "
                            + "Run the bot first, or pass --resolve-glob <pattern>.");
            return 1;
        }

        var rows = new List<Dictionary<string, string>>();
        foreach (var f in files)
        {
            var r = Csv.Read(f);
            Console.WriteLine($"[RESOLVE] {System.IO.Path.GetFileName(f),-34} {r.Count,6} row(s)");
            rows.AddRange(r);
        }
        if (rows.Count == 0) { Console.WriteLine("[RESOLVE] nothing logged yet."); return 1; }

        var tickers = rows.Select(r => Csv.Str(r, "Ticker")).Where(t => t.Length > 0)
                          .Distinct(StringComparer.Ordinal).ToList();
        Console.WriteLine($"[RESOLVE] fetching settlement for {tickers.Count} market(s)…");

        var resolver = new SettlementResolver(kalshi);
        int alreadyKnown = resolver.Known.Values.Count(r => r.Terminal);
        var settled  = await resolver.ResolveAsync(tickers);
        Console.WriteLine($"[RESOLVE] {resolver.Fetched} fetched, {alreadyKnown} already on record, "
                        + $"{resolver.Failed} failed; {settled.Values.Count(s => s.IsFinal)} final, "
                        + $"{settled.Values.Count(s => s.IsGone)} gone from the venue.");
        Console.WriteLine($"[RESOLVE] permanent record: {resolver.Store.Path}");

        Calibration.Report(Calibration.FromTelemetry(rows, settled), settled, dedupe);
        return 0;
    }

    // ── --verify: exercise every subsystem and print a checklist ──────────────────────────────────────
    private sealed record Chk(string Status, string Name, string Detail);

    /// <summary>
    /// Runs each subsystem once and reports PASS / WARN / FAIL per item.
    ///
    /// <para><b>Why this exists.</b> Most of what this bot does is silent between events — snapshots every
    /// five minutes, settlements every ten, a pair reload only when the pairing job writes. A few minutes of
    /// console output therefore cannot distinguish "working" from "never ran", which is the same
    /// quiet-versus-broken confusion that has bitten this project repeatedly. This forces each one to act
    /// now and says plainly what happened.</para>
    ///
    /// <para>WARN, not FAIL, is used wherever the cause is legitimately external — no matches in play, the
    /// Pinnacle session down, nothing settled yet. Those are conditions to read, not defects.</para>
    /// </summary>
    private static async Task<int> VerifyModeAsync(
        List<EvPair> pairs, string pairsPath, KalshiBookFeed feed, PinnacleOracle oracle, EvEvaluator eval,
        EvTelemetry telemetry, OracleSnapshotLog snapshots, SettlementResolver resolver, EvConfig cfg,
        KalshiOrderClient rk, CancellationToken ct)
    {
        var checks = new List<Chk>();
        void Pass(string n, string d) => checks.Add(new Chk("PASS", n, d));
        void Warn(string n, string d) => checks.Add(new Chk("WARN", n, d));
        void Fail(string n, string d) => checks.Add(new Chk("FAIL", n, d));

        // A check that throws must become a FAIL LINE, not a stack trace that abandons the remaining
        // checks. The first live run died at check 8 and reported nothing at all about 9, 10 or 11 —
        // which is the opposite of what a diagnostic is for.
        async Task Step(string name, Func<Task> body)
        {
            try { await body(); }
            catch (Exception ex) { Fail(name, $"{ex.GetType().Name}: {ex.Message}"); }
        }

        Console.WriteLine("\n══ VERIFY — exercising every subsystem once ════════════════════════════════");

        // 1. Pair file
        Pass("pair file loaded", $"{pairs.Count} pair(s) from {System.IO.Path.GetFileName(pairsPath)}");

        // 2-3. Kalshi WS
        Console.WriteLine("[verify] waiting for the Kalshi snapshot burst…");
        await WaitWarmAsync(feed, oracle, ct, TimeSpan.FromSeconds(45));
        await Task.Delay(4000, ct).ContinueWith(_ => { });
        // CONNECTED IS NOT HEALTHY. A socket that subscribed and then went silent — a failed resubscribe,
        // a venue that quietly stopped publishing — reports connected forever. Silence is the real test.
        double quiet = EvConfig.Env("EV_WS_SILENT_SEC", 120);
        if (!feed.IsConnected || feed.MessageCount == 0)
            Fail("Kalshi WebSocket", $"connected={feed.IsConnected}, messages={feed.MessageCount}. "
                                   + "If another bot holds the account's single socket, this one gets nothing.");
        else if (feed.SilenceSec > quiet)
            Warn("Kalshi WebSocket", $"connected and subscribed to {feed.TickerCount}, but SILENT for "
                                   + $"{feed.SilenceSec:0}s. Connected is not the same as receiving.");
        else
            Pass("Kalshi WebSocket", $"connected, {feed.MessageCount} message(s), {feed.TickerCount} subscribed, "
                                   + $"last frame {feed.SilenceSec:0}s ago");

        int withBooks = pairs.Select(p => p.KalshiTicker).Distinct(StringComparer.Ordinal)
                             .Count(t => feed.Top(t).HasSnapshot);
        if (withBooks > 0) Pass("Kalshi books", $"{withBooks}/{pairs.Count} market(s) have a book");
        else Fail("Kalshi books", "no market received a snapshot — the subscribe was accepted but silent");

        // 4-6. Oracle
        if (!oracle.IsConnected)
            Fail("Pinnacle oracle", $"sidecar not answering. Is it running?");
        else if (!oracle.SessionReady)
            Warn("Pinnacle oracle", "sidecar up but the Pinnacle session is DOWN — no fair value until it re-logs in");
        else
            Pass("Pinnacle oracle", $"sidecar up, session ready, polling {oracle.TokenCount} selection(s)");

        // The ODDS SOCKET, as distinct from the sidecar's HTTP being up. The sidecar answers /odds perfectly
        // well from a cache whose feed died ten minutes ago, so "oracle up" says nothing about whether new
        // prices are still arriving.
        var fh = oracle.Feed;
        if (!fh.Known)
            Warn("Pinnacle odds feed", "the venue publishes no feed health — cannot tell a quiet market from "
                                     + "a dead socket. Expected only on an adapter that predates feed_health().");
        else if (!fh.Alive || !fh.Connected)
            Fail("Pinnacle odds feed", $"{fh}");
        else if (double.IsFinite(fh.LastFrameAge) && fh.LastFrameAge > EvConfig.Env("EV_FEED_SILENT_SEC", 300))
            Warn("Pinnacle odds feed", $"{fh} — connected but nothing has arrived in a while.");
        else if (fh.Subscribed == 0 && fh.ActiveLeagues > 0)
            Warn("Pinnacle odds feed", $"{fh} — {fh.ActiveLeagues} league(s) active but NONE subscribed yet; "
                                     + "the reconciler subscribes one per tick, so this clears on its own.");
        else
            Pass("Pinnacle odds feed", $"{fh}");

        int quoted = oracle.QuoteCount, stale = oracle.StaleCount, fresh = quoted - stale;
        double freshFrac = quoted > 0 ? (double)fresh / quoted : 0;
        if (quoted == 0)
            Fail("oracle quotes", "zero selections quoted — the pair file's matches may all have finished");
        else if (fresh == 0)
            Warn("oracle quotes", $"{quoted} quoted but ALL stale (see EV_ORACLE_MAX_AGE_MS). "
                                + "Nothing can be valued while this holds.");
        else if (freshFrac < 0.25)
            // A PASS here is a lie by omission. 858 quoted / 57 fresh reported PASS while the bot was
            // working from 7% of its watchlist — the Pinnacle reader can only tab a handful of leagues, so
            // pairing far more markets than it covers does not widen coverage, it just adds selections whose
            // prices are frozen or screening-only. That is a supply problem the checklist must not hide.
            Warn("oracle quotes", $"{quoted} quoted but only {fresh} FRESH ({freshFrac:0%}) — the reader "
                                + $"covers a fraction of {oracle.TokenCount} selection(s). The bot is working "
                                + "from that fraction; the rest are frozen or screening-only. Narrow the "
                                + "pairing to leagues the reader actually tabs, or expect this yield.");
        else
            Pass("oracle quotes", $"{quoted} quoted, {fresh} fresh ({freshFrac:0%}), {stale} stale");

        // 7. A full evaluation pass
        await Step("evaluation sweep", async () =>
        {
            long rowsBefore = telemetry.RowsWritten;
            var s0 = (eval.Stats.Screened, eval.Stats.RestCalls, eval.Stats.Signals);
            eval.ClearCooldowns();   // else the live loop's recent pass throttles this one to zero REST calls
            eval.SweepAll();
            var by = DateTime.UtcNow.AddSeconds(45);
            while (eval.Pending > 0 && DateTime.UtcNow < by && !ct.IsCancellationRequested) await Task.Delay(200);
            await Task.Delay(4000, ct).ContinueWith(_ => { });

            long screened = eval.Stats.Screened - s0.Screened;
            long rest     = eval.Stats.RestCalls - s0.RestCalls;
            long sigs     = eval.Stats.Signals - s0.Signals;
            if (screened == 0)
                Fail("evaluation sweep", "nothing was screened — the evaluator has no pairs or no books");
            else if (rest == 0)
                Warn("evaluation sweep", $"{screened} screened, 0 REST calls. Normal when nothing is near the "
                                       + "threshold; it means the free pre-screen rejected everything.");
            else
                Pass("evaluation sweep", $"{screened} screened → {rest} REST valuation(s) → {sigs} signal(s), "
                                       + $"{telemetry.RowsWritten - rowsBefore} row(s) written");
        });

        // 8. Telemetry file, arity checked against the file itself
        await Step("signal telemetry", () =>
        {
            checks.Add(FileArityCheck("signal telemetry", telemetry.Path, EvTelemetry.Columns.Length));
            return Task.CompletedTask;
        });

        // 9. Snapshot — force one now rather than waiting for the timer
        await Step("oracle snapshot", () =>
        {
            long snapBefore = snapshots.RowsWritten;
            int wrote = SnapshotOnce(snapshots, oracle, feed, pairs, cfg);
            if (wrote == 0)
                Warn("oracle snapshot", "0 rows — no pair currently has two fresh, open Pinnacle quotes");
            else
                Pass("oracle snapshot", $"{wrote} pair(s) recorded ({snapshots.RowsWritten - snapBefore} row(s))");
            checks.Add(FileArityCheck("snapshot file", snapshots.Path, OracleSnapshotLog.Columns.Length));
            return Task.CompletedTask;
        });

        // 10. Settlement banking — force a poll now
        await Step("settlement store", async () =>
        {
            Console.WriteLine("[verify] polling settlements…");
            var tick = pairs.Select(p => p.KalshiTicker).Distinct(StringComparer.Ordinal).ToList();
            await resolver.ResolveAsync(tick, ct);
            int fin = resolver.Known.Values.Count(r => r.IsFinal);
            int gone = resolver.Known.Values.Count(r => r.IsGone);
            if (!File.Exists(resolver.Store.Path))
                Fail("settlement store", "no ev_settlements.jsonl was written");
            else
                Pass("settlement store", $"{fin} final, {gone} gone, "
                                       + $"{resolver.Known.Count - fin - gone} still active → "
                                       + System.IO.Path.GetFileName(resolver.Store.Path));
        });

        // 11. Pair hot-reload, exercised end to end against a COPY — the live file is never touched
        // ── The order path's two live-resolved inputs, checked WITHOUT placing anything ───────────────
        // Kalshi shards markets and requires collateral on the shard the market lives on. Both were
        // discovered the hard way on 2026-08-28 (404 market_not_found, then 404 user_not_found), and both
        // fail at ORDER time — i.e. on a real signal, with money on the line and the opportunity gone by
        // the time it is read. Everything below is a GET, so this can run before every session.
        try
        {
            var probe = pairs.FirstOrDefault();
            if (probe is not null)
            {
                int shard = await rk.ExchangeIndexForAsync(probe.KalshiTicker);
                double mult = await rk.FeeMultiplierForAsync(probe.KalshiTicker);
                Pass("exchange shard resolves", $"{probe.KalshiTicker} -> shard {shard} (read from the market, "
                                              + "not assumed; a hardcoded 0 is what broke orders on 08-27)");
                long cents = await rk.GetBalanceCentsAsync();
                Pass("fee multiplier resolves", $"{probe.KalshiTicker.Split('-')[0]} M={mult:0.##}"
                                              + (Math.Abs(mult - 1.0) < 1e-9 ? " (standard)" : "  NON-STANDARD"));
                var bad = rk.UnknownFeeTypes;
                if (bad.Count > 0) Fail("fee shape known", "unimplemented fee_type on " + string.Join(", ", bad));
                else Pass("fee shape known", "quadratic — the form our arithmetic implements");
                // Collateral is PER SHARD: a funded account on shard 0 still cannot trade shard 3.
                double onShard = await rk.ShardBalanceAsync(shard);
                if (onShard <= 0)
                    Fail("collateral on the trading shard",
                         $"$0.00 on shard {shard} — every order will 404 user_not_found. "
                       + "Transfer via /portfolio/intra_exchange_instance_transfer.");
                else if (onShard < cfg.LiveStakePerGameUsd)
                    Warn("collateral on the trading shard",
                         $"${onShard:0.00} on shard {shard} — below one game's cap (${cfg.LiveStakePerGameUsd:0.00}).");
                else
                    Pass("collateral on the trading shard",
                         $"${onShard:0.00} on shard {shard} (total account ${cents / 100.0:0.00})");
            }
        }
        catch (Exception ex) { Fail("order-path preflight", $"{ex.GetType().Name}: {ex.Message}"); }

        checks.Add(await VerifyReloadAsync(pairsPath, ct));

        // ── Report ────────────────────────────────────────────────────────────────────────────────────
        Console.WriteLine("\n── RESULTS ─────────────────────────────────────────────────────────────────");
        foreach (var c in checks)
        {
            Console.ForegroundColor = c.Status == "PASS" ? ConsoleColor.Green
                                    : c.Status == "WARN" ? ConsoleColor.DarkYellow : ConsoleColor.Red;
            Console.Write($"  {c.Status}  ");
            Console.ResetColor();
            Console.WriteLine($"{c.Name,-20} {c.Detail}");
        }
        int fails = checks.Count(c => c.Status == "FAIL"), warns = checks.Count(c => c.Status == "WARN");
        Console.WriteLine($"\n  {checks.Count - fails - warns} passed, {warns} warning(s), {fails} failure(s)");
        Console.WriteLine("\n  Not exercised here (covered by --self-test): midnight file rolling, row-arity");
        Console.WriteLine("  rejection, de-vig and Kelly arithmetic. Run --self-test for those — it needs no venue.");
        return fails == 0 ? 0 : 1;
    }

    /// <summary>Reads the file back and compares its last row's field count to the schema. Checks the
    /// artefact rather than the code path that wrote it, which is the only way to catch a drift that the
    /// in-process guard would have to be broken to miss.</summary>
    private static Chk FileArityCheck(string name, string path, int expected)
    {
        if (!File.Exists(path)) return new Chk("FAIL", name, $"file not created: {path}");
        var rows = Csv.Read(path);
        string f = System.IO.Path.GetFileName(path);
        if (rows.Count == 0) return new Chk("WARN", name, $"{f} exists but has no rows yet");
        int cols = rows[^1].Count;
        return cols == expected
            ? new Chk("PASS", name, $"{f}, {rows.Count} row(s), {cols} columns — arity matches")
            : new Chk("FAIL", name, $"{f} last row has {cols} fields, schema says {expected}");
    }

    /// <summary>
    /// Proves the hot-reload path actually fires, using a temporary COPY of the pair file.
    ///
    /// <para>The live file is never written to — the pairing job owns it. A copy is trimmed by one market,
    /// a reload loop is pointed at it, the full set is restored, and the evaluator is checked for the
    /// market coming back. Without this the reload can only be verified by waiting hours for the pairing
    /// job and hoping, which is exactly the kind of "assume it works" this bot keeps getting caught by.</para>
    /// </summary>
    private static async Task<Chk> VerifyReloadAsync(string pairsPath, CancellationToken ct)
    {
        string tmp = System.IO.Path.Combine(System.IO.Path.GetTempPath(),
                                            "evbot_reload_" + Guid.NewGuid().ToString("N") + ".json");
        try
        {
            string json = File.ReadAllText(pairsPath);
            using var doc = System.Text.Json.JsonDocument.Parse(json);
            var all = doc.RootElement.EnumerateArray().Select(e => e.Clone()).ToList();

            // Hold back a row the LOADER WILL KEEP. Most rows in this file are unpaired and get skipped, so
            // trimming an arbitrary one changes nothing and the check reports a meaningless "no change".
            static bool Paired(System.Text.Json.JsonElement e)
                => e.TryGetProperty("hardven_yes_token", out var y) && !string.IsNullOrWhiteSpace(y.GetString())
                && e.TryGetProperty("hardven_no_token",  out var n) && !string.IsNullOrWhiteSpace(n.GetString());

            int drop = all.FindLastIndex(Paired);
            if (drop < 0 || all.Count(Paired) < 2)
                return new Chk("WARN", "pair hot-reload", "fewer than two paired rows — nothing to exercise with");

            File.WriteAllText(tmp, System.Text.Json.JsonSerializer.Serialize(
                all.Where((_, i) => i != drop)));
            var start = EvPairLoader.Load(tmp, out _);

            var probeEval = new EvEvaluator(start, new PinnacleOracle("", Array.Empty<string>()),
                                            new KalshiBookFeed(null!, new KalshiApiConfig(), Array.Empty<string>()),
                                            null!, null!, new EvConfig());
            int before = probeEval.PairCount;

            // Restore the full set — this is the write the loop is meant to notice.
            await Task.Delay(1100, ct);          // ensure a distinct mtime on coarse filesystem clocks
            File.WriteAllText(tmp, System.Text.Json.JsonSerializer.Serialize(all));

            var full = EvPairLoader.Load(tmp, out _);
            int added = probeEval.UpsertPairs(full);
            int after = probeEval.PairCount;

            return after > before || added > 0
                ? new Chk("PASS", "pair hot-reload", $"reload picked up {added} market(s) ({before} → {after}); "
                                                   + "the live file was not touched")
                : new Chk("WARN", "pair hot-reload", $"no change detected ({before} → {after}) — the trimmed "
                                                   + "market may have been unpaired and skipped both times");
        }
        catch (Exception ex)
        {
            return new Chk("FAIL", "pair hot-reload", $"{ex.GetType().Name}: {ex.Message}");
        }
        finally { try { File.Delete(tmp); } catch { } }
    }

    // ── Keeping the watchlist alive ───────────────────────────────────────────────────────────────────
    /// <summary>
    /// Re-reads <c>cross_pairs.json</c> whenever the pairing job rewrites it, and wires the new markets
    /// into the feed, the oracle and the evaluator.
    ///
    /// <para><b>Without this the bot is useful for one day.</b> The watchlist was fixed at startup, so by
    /// day two every match on it has finished and none of the day's new fixtures are being watched — a
    /// fortnight's run would return a day's data and look, from the console, exactly like a healthy one.</para>
    ///
    /// <para>Markets are only ever ADDED here, never removed. A finished market costs one dead subscription
    /// and nothing else, whereas dropping it would take it out of the settlement watcher's list before its
    /// result was banked — and Kalshi does not keep obscure markets around to be asked again later.</para>
    /// </summary>
    private static async Task PairReloadLoopAsync(string path, EvEvaluator eval, KalshiBookFeed feed,
                                                  PinnacleOracle oracle, List<EvPair> livePairs,
                                                  HashSet<string> everSeen, object pairsLock,
                                                  CancellationToken ct)
    {
        var every = TimeSpan.FromSeconds(Math.Max(30, EvConfig.Env("EV_PAIR_RELOAD_SEC", 120)));
        DateTime lastWrite = SafeWriteTime(path);
        Console.WriteLine($"[PAIRS] watching {System.IO.Path.GetFileName(path)} for updates every "
                        + $"{every.TotalSeconds:0}s — new fixtures are picked up without a restart.");

        while (!ct.IsCancellationRequested)
        {
            try { await Task.Delay(every, ct); } catch (OperationCanceledException) { break; }
            try
            {
                var w = SafeWriteTime(path);
                if (w <= lastWrite) continue;
                lastWrite = w;

                // The pairing job writes this file; a read landing mid-write yields a truncated document.
                // Treat that as "try again next tick" rather than as a reason to stop watching.
                List<EvPair> fresh;
                try { fresh = EvPairLoader.Load(path, out _); }
                catch (Exception ex)
                {
                    Console.WriteLine($"[PAIRS] reload skipped (file mid-write?): {ex.GetType().Name}");
                    lastWrite = DateTime.MinValue;      // force a retry next tick
                    continue;
                }
                if (fresh.Count == 0) continue;

                // REPLACE, don't accumulate. See ReplacePairs / SetTokens: the live watchlist is whatever the
                // pairing job currently says, and yesterday's finished fixtures must leave it or a fortnight's
                // daily re-pairs compound into thousands of dead markets swept every three seconds.
                var (newPairs, dropped) = eval.ReplacePairs(fresh);
                var newTickers = fresh.Select(p => p.KalshiTicker)
                                      .Where(t => !everSeen.Contains(t)).Distinct(StringComparer.Ordinal).ToList();
                feed.EnqueueSubscribe(newTickers);
                var (newTokens, goneTokens) = oracle.SetTokens(fresh.SelectMany(p => p.Legs));

                // One lock per resource, each taken consistently everywhere it is touched: `pairsLock`
                // guards everSeen (shared with the settlement watcher), the list instance guards itself
                // (shared with the snapshot loop, which locks the same instance).
                int seenCount;
                lock (pairsLock)
                {
                    foreach (var t in newTickers) everSeen.Add(t);
                    seenCount = everSeen.Count;
                }
                lock (livePairs)
                {
                    // The snapshot loop holds this instance, so replace the CONTENTS rather than the list.
                    var byTicker = livePairs.ToDictionary(p => p.KalshiTicker, StringComparer.Ordinal);
                    foreach (var p in fresh) byTicker[p.KalshiTicker] = p;
                    livePairs.Clear();
                    livePairs.AddRange(byTicker.Values);
                }

                if (newPairs > 0 || newTokens > 0 || dropped > 0 || goneTokens > 0)
                    Console.WriteLine($"[PAIRS] reloaded: +{newPairs}/-{dropped} market(s), "
                                    + $"+{newTokens}/-{goneTokens} Pinnacle selection(s) — now "
                                    + $"{eval.PairCount} watched, {seenCount} ever seen (dropped markets "
                                    + "still have their settlements banked).");
            }
            catch (OperationCanceledException) { break; }
            catch (Exception ex) { Console.WriteLine($"[PAIRS] reload error: {ex.GetType().Name}: {ex.Message}"); }
        }
    }

    private static DateTime SafeWriteTime(string path)
    {
        try { return File.Exists(path) ? File.GetLastWriteTimeUtc(path) : DateTime.MinValue; }
        catch { return DateTime.MinValue; }
    }

    // ── Oracle snapshots ──────────────────────────────────────────────────────────────────────────────
    /// <summary>
    /// Writes one oracle row per pair on a timer. This is the cheap half of M1: it needs no Kalshi REST
    /// call and no +EV window, so the sharp-book question accumulates evidence at the rate matches are
    /// PLAYED rather than at the rate signals happen.
    /// </summary>
    private static async Task SnapshotLoopAsync(OracleSnapshotLog log, PinnacleOracle oracle,
                                                KalshiBookFeed feed, List<EvPair> pairs, EvConfig cfg,
                                                CancellationToken ct)
    {
        var every = TimeSpan.FromMinutes(Math.Max(0.5, EvConfig.Env("EV_SNAPSHOT_MIN", 5)));
        // A short first delay so the opening snapshot lands on warm books rather than an empty cache.
        try { await Task.Delay(TimeSpan.FromSeconds(20), ct); } catch (OperationCanceledException) { return; }

        while (!ct.IsCancellationRequested)
        {
            int written = SnapshotOnce(log, oracle, feed, pairs, cfg);
            if (written > 0) Console.WriteLine($"[SNAPSHOT] {written} pair(s) recorded ({log.RowsWritten} total)");
            try { await Task.Delay(every, ct); } catch (OperationCanceledException) { break; }
        }
    }

    /// <summary>One pass over every pair. Skips anything the oracle cannot currently price — a snapshot of
    /// a stale or suspended quote is not an observation, it is a guess that would be graded as one.</summary>
    private static int SnapshotOnce(OracleSnapshotLog log, PinnacleOracle oracle, KalshiBookFeed feed,
                                    List<EvPair> pairs, EvConfig cfg)
    {
        int n = 0;
        List<EvPair> snapshot;
        lock (pairs) snapshot = pairs.ToList();   // the reload loop mutates this list in place
        foreach (var p in snapshot)
        {
            if (!p.LegsUsable) continue;
            // Every leg, not just the two named on the row: a 1X2 with a missing draw price has no valid S,
            // so its home and away legs are unusable too.
            var quotes = new List<OracleQuote>(p.Legs.Count);
            foreach (var tok in p.Legs)
            {
                var q = oracle.Get(tok);
                if (q is null || !q.Open || !oracle.Fresh(q)) { quotes.Clear(); break; }
                quotes.Add(q);
            }
            if (quotes.Count != p.Legs.Count) continue;
            try
            {
                log.Write(p, quotes, oracle.AgeMs(quotes[p.YesLegIndex]),
                          feed.Top(p.KalshiTicker).YesAsk, cfg.DeVigMethod);
                n++;
            }
            catch (Exception ex) { Console.WriteLine($"[SNAPSHOT] {p.KalshiTicker}: {ex.Message}"); }
        }
        return n;
    }

    /// <summary>Posts the live performance to Discord on a timer, so a session can be watched from away
    /// from the machine without asking.
    ///
    /// <para><b>Quiet by default when nothing is happening.</b> An unattended bot that posts an identical
    /// line every half hour trains the operator to ignore the channel, which is exactly when the one line
    /// that mattered gets missed. This posts only when the status TEXT has changed since the last post, and
    /// forces one through every EV_DISCORD_HEARTBEAT_MIN regardless so silence still means "alive".</para></summary>
    private static async Task PerformanceLoopAsync(DiscordNotifier discord, Func<Task<string>> status,
                                                   CancellationToken ct)
    {
        int everyMin  = (int)EvConfig.Env("EV_DISCORD_REPORT_MIN", 30);
        int beatMin   = (int)EvConfig.Env("EV_DISCORD_HEARTBEAT_MIN", 240);
        if (everyMin <= 0) return;
        string last = "";
        var lastPost = DateTime.UtcNow;
        while (!ct.IsCancellationRequested)
        {
            try { await Task.Delay(TimeSpan.FromMinutes(everyMin), ct); }
            catch (OperationCanceledException) { break; }
            try
            {
                string now = await status();
                bool stale = (DateTime.UtcNow - lastPost).TotalMinutes >= beatMin;
                if (now == last && !stale) continue;
                await discord.AlertAsync(now);
                last = now; lastPost = DateTime.UtcNow;
            }
            catch (Exception ex)
            {
                // Never let the reporter take the bot down: it is a convenience, not a dependency.
                Console.WriteLine($"[DISCORD] performance post failed: {ex.GetType().Name}: {ex.Message}");
            }
        }
    }

    // ── Bankroll ──────────────────────────────────────────────────────────────────────────────────────
    /// <summary>Sizing needs a bankroll even though M0 buys nothing: the Contracts column is what M1 will
    /// use to weight realised results, so a run with no bankroll logs correct EV and meaningless sizes.</summary>
    /// <summary>The local date the standing snapshot was taken for. One decision per day, then held.</summary>
    private static DateTime _bankrollDay = DateTime.MinValue;

    /// <summary>Cash plus the liquidation value of open positions — the base Kelly should actually size
    /// against. A held contract is capital COMMITTED, not capital lost, and sizing on cash alone would
    /// shrink the stake merely because a position is open rather than because the account got smaller.
    ///
    /// <para>Positions are valued at the BID, which is what we could really get out at, and the bid is
    /// derived from the OPPOSITE ask (<c>yes_bid = 1 − no_ask</c>) — the identity the book feed already
    /// relies on. That reuses the proven <see cref="EvEvaluator.AskDollars"/> reader rather than assuming a
    /// bid field exists in a payload we have never inspected. A side quoting no ask at all is valued at
    /// zero rather than guessed at.</para></summary>
    private static async Task<(double Cash, double Held, int Positions, string Note)>
        ReadEquityAsync(KalshiOrderClient k)
    {
        double cash = (await k.GetBalanceCentsAsync()) / 100.0;
        double held = 0; int n = 0; string note = "";
        try
        {
            foreach (var (ticker, qty) in await k.GetPositionsAsync())
            {
                if (qty == 0) continue;
                using var doc = await k.GetMarketAsync(ticker);
                var mkt = doc.RootElement.TryGetProperty("market", out var m) ? m : doc.RootElement;
                decimal opp = EvEvaluator.AskDollars(mkt, yes: qty < 0);   // YES held -> read no_ask, and vice versa
                if (opp <= 0m || opp >= 1m) continue;
                held += Math.Abs(qty) * (double)(1m - opp);
                n++;
            }
        }
        catch (Exception ex) { note = $"  (positions unread: {ex.GetType().Name} — cash only)"; }
        return (cash, held, n, note);
    }

    /// <summary>
    /// Decide the bankroll for TODAY, once, and hold it.
    ///
    /// <para><b>Why daily and not live.</b> Kelly sizes off the bankroll, so a balance re-read every minute
    /// makes the Contracts column — and every size-weighted figure built on it — drift under the dataset as
    /// fills and settlements land. The same signal logged twice in one day would carry different sizes for
    /// a reason that has nothing to do with the edge. One snapshot per local calendar day keeps every size
    /// within a day directly comparable, while still letting the account compound from day to day. EV never
    /// depends on this; only the size columns do.</para>
    ///
    /// <para><b>An explicit pin outranks the snapshot.</b> <c>EV_BANKROLL_USD</c> is authoritative and the
    /// balance is not read at all — that is what keeps M0 and M1 telemetry on one basis across the
    /// boundary.</para>
    /// </summary>
    private static async Task RefreshBankrollAsync(KalshiOrderClient k, EvEvaluator eval, EvConfig cfg,
                                                   bool announce, bool force = false)
    {
        if (cfg.BankrollFallback > 0)
        {
            eval.BankrollUsd = cfg.BankrollFallback;
            _bankrollDay = DateTime.Now.Date;
            if (announce)
                Console.WriteLine($"[BANKROLL] ${eval.BankrollUsd:0.00} PINNED (EV_BANKROLL_USD) - the live "
                                + "balance is not read, so sizing stays comparable as --live moves it.");
            return;
        }
        if (!force && _bankrollDay == DateTime.Now.Date) return;    // already decided for today
        bool firstOfDay = _bankrollDay != DateTime.Now.Date;
        try
        {
            var (cash, held, npos, note) = await ReadEquityAsync(k);
            eval.BankrollUsd = cash + held;
            _bankrollDay = DateTime.Now.Date;
            if (announce || firstOfDay)
                Console.WriteLine($"[BANKROLL] ${eval.BankrollUsd:0.00} for {_bankrollDay:ddd dd MMM} "
                                + $"= ${cash:0.00} cash + ${held:0.00} in {npos} open position(s).{note}"
                                + "  Fixed for the day; set EV_BANKROLL_USD to pin it outright.");
        }
        catch (Exception ex)
        {
            // DO NOT half-update. Leaving the previous day's figure standing is strictly better than sizing
            // off a zero, and the next attempt will retry because _bankrollDay was never advanced.
            if (eval.BankrollUsd <= 0) eval.BankrollUsd = cfg.BankrollFallback;
            if (announce || firstOfDay)
                Console.WriteLine($"[BANKROLL] balance read failed ({ex.GetType().Name}) — holding "
                                + $"${eval.BankrollUsd:0.00}. EV is unaffected; only the size columns are.");
        }
    }

    /// <summary>Wakes every few minutes but only ACTS on a local date rollover — the day check inside
    /// <see cref="RefreshBankrollAsync"/> makes every other tick a no-op, so this costs one balance read
    /// per day rather than one per minute.</summary>
    private static async Task BankrollLoopAsync(KalshiOrderClient k, EvEvaluator eval, EvConfig cfg, CancellationToken ct)
    {
        while (!ct.IsCancellationRequested)
        {
            try { await Task.Delay(TimeSpan.FromMinutes(5), ct); } catch (OperationCanceledException) { break; }
            await RefreshBankrollAsync(k, eval, cfg, announce: false);
        }
    }

    // ── The §4 decisive test ──────────────────────────────────────────────────────────────────────────
    /// <summary>
    /// Dumps the local WS ask ladder beside <c>GET /markets/{ticker}</c> for the same market at the same
    /// instant. This is the one measurement that can tell a stale book from a wrong one, and it needs the
    /// bot running — which is why it lives here rather than in a script.
    ///
    /// <para>The suspicion it exists to test: the WS ask reads a median 4c below REST, and 389 of 400
    /// measured windows opened on a book aged 0ms, so the error arrives on the tick rather than decaying
    /// into it. If the ladders here disagree on a freshly-updated market, the bug is in our book building.
    /// If they agree, the +4c came from somewhere else and §4 needs rewriting.</para>
    /// </summary>
    private static async Task BookAuditAsync(KalshiBookFeed feed, KalshiOrderClient kalshi,
                                             PinnacleOracle oracle, List<EvPair> pairs, int count)
    {
        Console.WriteLine("\n══ BOOK AUDIT — local WS ladder vs REST, same instant ══");
        Console.WriteLine("PRICE is checked against /markets, DEPTH against /markets/{ticker}/orderbook.");
        Console.WriteLine($"{"Ticker",-34} {"side",-4} {"wsAsk",8} {"restAsk",8} {"gap(c)",7} {"age(ms)",8}");

        // WHICH MARKETS TO SAMPLE. Taking them in file order sampled whatever the pairing job happened to
        // write first — which is yesterday's finished matches, i.e. dead books, exactly the ones that cannot
        // exhibit a snapshot/delta race. Order by how recently the book moved instead, so the busiest markets
        // are measured first. That is where the suspected bug would live, and it needs no timing by hand.
        var sample = pairs.Select(p => p.KalshiTicker).Distinct(StringComparer.Ordinal)
                          .Where(t => feed.Top(t).HasSnapshot)
                          .OrderBy(t => feed.Top(t).AgeMs)
                          .Take(Math.Max(1, count)).ToList();
        if (sample.Count == 0) { Console.WriteLine("(no market has a snapshot yet)"); return; }

        // Pinnacle's in-play flag, so each row says which regime it came from. A gap that only appears on a
        // fast in-play book is a completely different finding from one that appears everywhere.
        var inPlayOf = pairs.GroupBy(p => p.KalshiTicker)
                            .ToDictionary(g => g.Key,
                                          g => oracle.Get(g.First().YesToken)?.Live ?? false,
                                          StringComparer.Ordinal);
        int liveCount = sample.Count(t => inPlayOf.TryGetValue(t, out bool b) && b);
        Console.WriteLine($"Sampling {sample.Count} market(s), busiest book first — {liveCount} in-play, "
                        + $"{sample.Count - liveCount} pre-match.");
        if (liveCount == 0)
            Console.WriteLine("NOTE: nothing in-play right now. A quiet-book result does not close out the "
                            + "fast-book question — re-run while matches are actually being played.");

        var gaps = new List<double>();
        var gapsInPlay = new List<double>();
        var sizeRatios = new List<double>();
        foreach (var t in sample)
        {
            bool inPlay = inPlayOf.TryGetValue(t, out bool ip) && ip;
            var top = feed.Top(t);
            decimal ry, rn;
            List<(decimal Price, decimal Size)> restYesLadder, restNoLadder;
            try
            {
                using var doc = await kalshi.GetMarketAsync(t);
                var mkt = doc.RootElement.TryGetProperty("market", out var m) ? m : doc.RootElement;
                ry = EvEvaluator.AskDollars(mkt, yes: true);
                rn = EvEvaluator.AskDollars(mkt, yes: false);

                using var obDoc = await kalshi.GetMarketOrderBookAsync(t);
                var obRoot = obDoc.RootElement;
                var ob = obRoot.TryGetProperty("orderbook_fp", out var ofp) ? ofp
                       : obRoot.TryGetProperty("orderbook",    out var o)   ? o
                       : obRoot;
                restYesLadder = RestAskLadder(ob, yesSide: true);
                restNoLadder  = RestAskLadder(ob, yesSide: false);
            }
            catch (Exception ex) { Console.WriteLine($"{t,-34} REST failed: {ex.GetType().Name}"); continue; }

            foreach (var (side, ws, rest, restLadder) in new[]
                     { ("YES", top.YesAsk, ry, restYesLadder), ("NO", top.NoAsk, rn, restNoLadder) })
            {
                if (ws >= 1m || rest <= 0m) continue;
                double gap = (double)(rest - ws) * 100.0;
                gaps.Add(gap);
                if (inPlay) gapsInPlay.Add(gap);
                var wsLadder = feed.AskLadder(t, side == "YES");
                Console.WriteLine($"{Trunc(t, 34),-34} {side,-4} {ws,8:0.0000} {rest,8:0.0000} "
                                + $"{gap,7:+0.0;-0.0} {top.AgeMs,8:0} {(inPlay ? "LIVE" : "pre")}");
                Console.WriteLine($"       ws   {Ladder(wsLadder)}");
                Console.WriteLine($"       rest {Ladder(restLadder)}");

                // DEPTH, which the price comparison says nothing about. If these disagree by orders of
                // magnitude the two sources are on different size scales, and the Contracts column in the
                // telemetry is denominated in a unit nobody has checked.
                if (wsLadder.Count > 0 && restLadder.Count > 0 && restLadder[0].Size > 0m
                    && wsLadder[0].Price == restLadder[0].Price)
                    sizeRatios.Add((double)(wsLadder[0].Size / restLadder[0].Size));
            }
            await Task.Delay(150);   // polite spacing; this is a diagnostic, not a hot path
        }

        if (gaps.Count == 0) { Console.WriteLine("\n(nothing comparable)"); return; }
        gaps.Sort();
        static double Pct(List<double> v, double q) => v[Math.Min(v.Count - 1, (int)(v.Count * q))];
        int worse = gaps.Count(g => g > 0);
        Console.WriteLine($"\nPRICE — {gaps.Count} comparison(s):  p10 {Pct(gaps, .10):+0.0;-0.0}c   "
                        + $"median {Pct(gaps, .50):+0.0;-0.0}c   p90 {Pct(gaps, .90):+0.0;-0.0}c    "
                        + $"REST worse for us in {worse}/{gaps.Count} ({100.0 * worse / gaps.Count:0}%)");
        Console.WriteLine("A positive gap means the WS ask is optimistic — the price we would have screened "
                        + "on is cheaper than the one actually offered.");

        // The regime split is the point of the whole exercise: the original +4c came from an in-play-heavy
        // sample, so a clean pre-match result answers a question nobody asked.
        if (gapsInPlay.Count > 0)
        {
            gapsInPlay.Sort();
            Console.WriteLine($"PRICE, IN-PLAY ONLY — {gapsInPlay.Count} comparison(s):  "
                            + $"median {Pct(gapsInPlay, .50):+0.0;-0.0}c   "
                            + $"worse for us in {gapsInPlay.Count(g => g > 0)}/{gapsInPlay.Count}");
        }
        else
        {
            Console.WriteLine("PRICE, IN-PLAY ONLY — no in-play markets in this sample, so the fast-book "
                            + "case is still UNTESTED. This result covers quiet books only.");
        }

        if (sizeRatios.Count > 0)
        {
            sizeRatios.Sort();
            double med = Pct(sizeRatios, .50);
            Console.WriteLine($"\nDEPTH — {sizeRatios.Count} top-of-book size(s), ws/rest ratio:  "
                            + $"min {sizeRatios[0]:0.####}   median {med:0.####}   max {sizeRatios[^1]:0.####}");
            Console.WriteLine(Math.Abs(med - 1.0) < 0.01
                ? "Ratio ~1: both sources agree on size, so depth is whatever /orderbook reports."
                : "Ratio NOT ~1: the two sources are on DIFFERENT SIZE SCALES. Every Contracts figure in the "
                + "telemetry is then in an unverified unit — resolve before M2 sizes against it.");
        }
    }

    private static string Ladder(List<(decimal Price, decimal Size)> l)
        => l.Count == 0 ? "(empty)" : string.Join("  ", l.Select(x => $"{x.Price:0.00}x{x.Size:0.##}"));

    /// <summary>
    /// Ask ladder from a REST <c>/orderbook</c> payload. The endpoint reports BIDS on both sides, so an ask
    /// is the complement of the opposite side's bid — the same transform the WS feed applies, which is
    /// precisely what makes the two comparable.
    ///
    /// <para>Shape verified against the live API 2026-08-21, because it is not what the rest of this repo
    /// assumes: the wrapper is <c>orderbook_fp</c> (not <c>orderbook</c>), the side keys are
    /// <c>yes_dollars</c> / <c>yes</c> (not <c>yes</c> alone), and prices are DOLLAR strings
    /// (<c>"0.4300"</c>), not cent integers. Parsing it as cents yielded an empty ladder on every market and
    /// the depth check silently did nothing. Both namings are accepted so an API revision degrades to a
    /// visible mismatch rather than to silence.</para>
    /// </summary>
    private static List<(decimal Price, decimal Size)> RestAskLadder(System.Text.Json.JsonElement ob,
                                                                     bool yesSide, int depth = 3)
    {
        var levels = new List<(decimal Price, decimal Size)>();
        System.Text.Json.JsonElement arr = default;
        foreach (var key in yesSide ? new[] { "no_dollars", "no" } : new[] { "yes_dollars", "yes" })
            if (ob.TryGetProperty(key, out arr) && arr.ValueKind == System.Text.Json.JsonValueKind.Array) break;
        if (arr.ValueKind != System.Text.Json.JsonValueKind.Array) return levels;

        foreach (var lvl in arr.EnumerateArray())
        {
            var it = lvl.EnumerateArray().ToArray();
            if (it.Length < 2) continue;
            if (!TryNum(it[0], out decimal price) || !TryNum(it[1], out decimal size) || size <= 0m) continue;
            // Dollars if it carries a fraction or is <= 1; cents otherwise. A bid is a probability, so
            // "0.43" can only be dollars and "43" can only be cents — there is no ambiguous case.
            if (price > 1m) price /= 100m;
            levels.Add((Math.Round(1m - price, 4), size));
        }
        return levels.OrderBy(x => x.Price).Take(depth).ToList();   // cheapest ask first
    }

    private static bool TryNum(System.Text.Json.JsonElement el, out decimal v)
    {
        if (el.ValueKind == System.Text.Json.JsonValueKind.Number) { v = el.GetDecimal(); return true; }
        return decimal.TryParse(el.GetString(), System.Globalization.NumberStyles.Any,
                                System.Globalization.CultureInfo.InvariantCulture, out v);
    }

    // ── Plumbing ──────────────────────────────────────────────────────────────────────────────────────
    private static async Task<bool> WaitWarmAsync(KalshiBookFeed feed, PinnacleOracle oracle,
                                                  CancellationToken ct, TimeSpan limit)
    {
        Console.WriteLine("[WARMUP] waiting for the Kalshi snapshot burst and the first oracle poll…");
        var until = DateTime.UtcNow + limit;
        while (DateTime.UtcNow < until && !ct.IsCancellationRequested)
        {
            if (feed.IsConnected && feed.MessageCount > 0 && oracle.IsConnected && oracle.QuoteCount > 0)
                return true;
            await Task.Delay(500);
        }
        return false;
    }

    private static async Task StatusLoopAsync(EvEvaluator e, KalshiBookFeed f, PinnacleOracle o,
                                              EvTelemetry t, OracleSnapshotLog sn, SettlementResolver r,
                                              string pairsPath, CancellationToken ct)
    {
        while (!ct.IsCancellationRequested)
        {
            try { await Task.Delay(TimeSpan.FromSeconds(30), ct); } catch (OperationCanceledException) { break; }
            PrintStatus(e, f, o, t, sn, r, pairsPath);
        }
    }

    /// <summary>
    /// Two lines: the evaluation pipeline, then the long-run subsystems.
    ///
    /// <para>The second line exists so a pasted log can be read for health. Snapshots, settlement banking
    /// and the pair reload are all silent between events, so counters are the only way to tell a working
    /// quiet run from a broken one — including <c>pairfile</c>, which reports how long since the pairing
    /// job last wrote. A reload that never fires is expected if that age keeps growing, and a defect if it
    /// does not.</para>
    /// </summary>
    private static void PrintStatus(EvEvaluator e, KalshiBookFeed f, PinnacleOracle o, EvTelemetry t,
                                    OracleSnapshotLog sn, SettlementResolver r, string pairsPath)
    {
        var s = e.Stats;
        Console.WriteLine(
            $"[{DateTime.UtcNow:HH:mm:ss}] pairs {e.PairCount} | ws {(f.IsConnected ? "up" : "DOWN")} msgs {f.MessageCount} (silent {f.SilenceSec:0}s) seqgap {f.SeqGaps} resync {f.Resyncs} "
          + $"| oracle {(o.IsConnected ? "up" : "DOWN")} quotes {o.QuoteCount} stale {o.StaleCount} "
          + $"{(o.SessionReady ? "" : "SESSION-DOWN ")}"
          + $"| screened {s.Screened} (noquote {s.NoQuote} stale {s.StaleOracle} susp {s.Suspended} "
          + $"below {s.BelowPrescreen} cooldown {s.Cooldown} incomplete-book {s.IncompleteBook} unverified {s.ScreeningOnly} implausible {s.Implausible} prematch {s.PreMatch} out-of-band {s.OutOfBand} devig-split {s.DeVigSplit} source-gap {s.SourceGap} not-rising {s.NotRising} no-kinetic-hist {s.NoKineticHistory} kalshi-led {s.KalshiLed} pinnacle-led {s.PinnacleLed} venue-vanished {s.VenueVanished} venue-refused {s.VenueRefused}) "
          + $"| rest {s.RestCalls} fail {s.RestFailed} 429 {s.RateLimited} "
          + $"| SIGNALS {s.Signals} rejected-at-rest {s.RejectedByRest} floored {s.FlooredToZero} "
          + $"| rows {t.RowsWritten} | bankroll ${e.BankrollUsd:0.00}");

        var age = DateTime.UtcNow - SafeWriteTime(pairsPath);
        int fin = r.Known.Values.Count(x => x.IsFinal), gone = r.Known.Values.Count(x => x.IsGone);
        Console.WriteLine(
            $"           snapshots {sn.RowsWritten} → {System.IO.Path.GetFileName(sn.Path)} "
          + $"| settled {fin} final, {gone} gone, {r.Known.Count - fin - gone} pending "
          + $"| pairfile written {(age.TotalDays > 365 ? "never" : $"{age.TotalMinutes:0}m ago")} "
          + $"| telemetry → {System.IO.Path.GetFileName(t.Path)}");
        // The venue's own socket report. Quote age cannot distinguish a quiet market from a dead feed;
        // this can, and on an unattended run it is the difference between "slow night" and "broken".
        if (o.Feed.Known) Console.WriteLine($"           pinnacle feed: {o.Feed}");
    }

    private static async Task SafeAll(params Task[] tasks)
    {
        try { await Task.WhenAll(tasks); }
        catch (OperationCanceledException) { }
        catch (Exception ex) { Console.WriteLine($"[SHUTDOWN] {ex.GetType().Name}: {ex.Message}"); }
    }

    private static string Trunc(string s, int n) => s.Length <= n ? s : s[..n];

    private static string? ArgValue(string[] a, string flag)
    {
        int i = Array.IndexOf(a, flag);
        return i >= 0 && i + 1 < a.Length && !a[i + 1].StartsWith("--") ? a[i + 1] : null;
    }

    private static int? ArgInt(string[] a, string flag)
        => int.TryParse(ArgValue(a, flag), out int v) ? v : null;

    // InvariantCulture on purpose: "--min-stake 2.5" must mean two and a half dollars on a machine whose
    // locale uses a comma decimal separator, not twenty-five.
    private static double? ArgDouble(string[] a, string flag)
        => double.TryParse(ArgValue(a, flag), NumberStyles.Any, CultureInfo.InvariantCulture, out double v)
           ? v : null;

    private static void Usage()
    {
        Console.WriteLine("""
            KalshiEvBot — +EV taker bot, M0 (observation only; places nothing).

              --self-test          run the offline arithmetic checks and exit (no venue, no network)
              --check              load + validate the pair file and exit; opens NO connection
              --resolve            grade every logged row against Kalshi settlement (REST only, no WS)
              --resolve-glob <p>   restrict --resolve to CSVs matching this pattern
              --all-obs            --resolve without deduping to one observation per ticker+side
              --once               warm up, evaluate every pair once, print the tally, exit
              --verify             exercise every subsystem once and print a PASS/WARN/FAIL checklist
              --book-audit [N]     dump the local WS ask ladder against REST for N markets and exit
              --pairs <path>       cross_pairs.json to read (default: HardVenArb's, or EV_PAIRS_FILE)
              --sidecar <url>      odds sidecar base URL (default: HARDVEN_SIDECAR_URL, or localhost:8787)
              --verbose            log candidates that REST rejected, and 429 back-offs

            M1 — PLACES REAL ORDERS. Nothing below trades without --live.
              --live               IOC-buy every confirmed signal. Real money.
              --micro-bet          label only: marks the run as a fill-rate test. Changes no sizing.
              --min-stake <$>      per side (default 5); a no-fill is free and may be retried
              --max-stake-game <$> per game (default 2x the side stake)

            Environment: EV_MIN, EV_PRESCREEN_SLACK, EV_MIN_PRICE, EV_MAX_PRICE, EV_DEVIG (proportional|shin),
            EV_FEE_RATE, EV_RECHECK_COOLDOWN_MS, EV_REST_CONCURRENCY, EV_MAX_TRADE_FRACTION, EV_BANKROLL_USD,
            EV_ORACLE_POLL_MS, EV_ORACLE_MAX_AGE_MS, EV_SNAPSHOT_MIN, EV_PAIRS_FILE, HARDVEN_SIDECAR_URL,
            EV_LIVE_STAKE_SIDE, EV_LIVE_STAKE_GAME, EV_LIVE_RETRY_COOLDOWN_SEC.

            BANKROLL: EV_BANKROLL_USD pins it outright and the balance is never read. Unset, the bot takes
            ONE snapshot per local calendar day — cash plus the bid-value of open positions — and holds it,
            so Kelly sizes stay comparable within a day while the account still compounds across days.
            """);
    }
}
