using PredictionBacktester.Engine.LiveExecution;

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

        Console.WriteLine("┌─ Kalshi +EV taker bot — M0 (OBSERVATION ONLY) ─────────────────────────────");
        Console.WriteLine("│  Pinnacle de-vigged = fair value.  Kalshi WS detects, Kalshi REST values.");
        Console.WriteLine("│  No order API is wired in this build. Nothing here can place a trade.");
        Console.WriteLine("└────────────────────────────────────────────────────────────────────────────");

        // ── Credentials + pairs ───────────────────────────────────────────────────────────────────────
        var config = KalshiApiConfig.FromEnvironment();      // also loads the solution-root .env
        if (string.IsNullOrWhiteSpace(config.ApiKeyId) || string.IsNullOrWhiteSpace(config.PrivateKeyPath))
        {
            Console.WriteLine("[FATAL] KALSHI_API_KEY_ID / KALSHI_PRIVATE_KEY_PATH not set.");
            return 2;
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

        // M1: grade what has already been logged. REST only — no WebSocket is opened, so this is safe to
        // run while another bot holds the account's single socket.
        if (args.Contains("--resolve"))
        {
            using var rk = new KalshiOrderClient(config);
            return await ResolveAsync(rk, ArgValue(args, "--resolve-glob"), !args.Contains("--all-obs"));
        }

        // Stop before ANY connection. Kalshi allows one WebSocket per account, and a second one connects
        // happily and receives nothing — so "just checking the pair file" must never be able to silently
        // blind a bot that is already running.
        if (args.Contains("--check"))
        {
            Console.WriteLine($"[CHECK] {pairs.Count} pair(s), "
                            + $"{pairs.Select(p => p.KalshiTicker).Distinct().Count()} ticker(s), "
                            + $"{pairs.SelectMany(p => new[] { p.YesToken, p.NoToken }).Distinct().Count()} "
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
        var tokens  = pairs.SelectMany(p => new[] { p.YesToken, p.NoToken })
                           .Distinct(StringComparer.Ordinal).ToList();

        using var kalshi    = new KalshiOrderClient(config);
        using var telemetry = new EvTelemetry();
        using var snapshots = new OracleSnapshotLog();
        var oracle = new PinnacleOracle(sidecar, tokens);
        var feed   = new KalshiBookFeed(kalshi, config, tickers);
        var eval   = new EvEvaluator(pairs, oracle, feed, kalshi, telemetry, cfg) { Verbose = verbose };

        kalshi.RateLimitRetryLogger = i =>
        {
            Interlocked.Increment(ref eval.Stats.RateLimited);
            if (verbose) Console.WriteLine($"[429] {i.Method} {i.Path} backing off {i.DelaySeconds:0.##}s "
                                         + $"(attempt {i.Attempt}/{i.MaxAttempts})");
        };

        Console.WriteLine($"[TELEMETRY] {telemetry.Path}");
        Console.WriteLine($"[SNAPSHOT ] {snapshots.Path}  (every {EvConfig.Env("EV_SNAPSHOT_MIN", 5):0} min, "
                        + "oracle only — this is what M1 grades soonest)");

        using var cts = new CancellationTokenSource();
        Console.CancelKeyPress += (_, e) => { e.Cancel = true; cts.Cancel(); };

        var feedTask   = feed.RunAsync(cts.Token);
        var oracleTask = oracle.RunAsync(cts.Token);
        var evalTask   = eval.RunAsync(cts.Token);

        // Both triggers feed the same queue. Kalshi ticking is one source of signals; Pinnacle moving is
        // the other, and a bot woken only by Kalshi would never see the second kind at all.
        feed.OnBookChanged += eval.Nudge;
        oracle.OnPolled    += eval.SweepAll;

        await RefreshBankrollAsync(kalshi, eval, cfg, announce: true);
        var bankrollTask = BankrollLoopAsync(kalshi, eval, cfg, cts.Token);
        var snapTask     = SnapshotLoopAsync(snapshots, oracle, feed, pairs, cfg, cts.Token);

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

        if (bookAudit)
        {
            await WaitWarmAsync(feed, oracle, cts.Token, TimeSpan.FromSeconds(45));
            await BookAuditAsync(feed, kalshi, oracle, pairs, ArgInt(args, "--book-audit") ?? 10);
            cts.Cancel();
            await SafeAll(feedTask, oracleTask, evalTask, bankrollTask, snapTask, settleTask, reloadTask);
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
            PrintStatus(eval, feed, oracle, telemetry, pairs.Count);
            await resolver.ResolveAsync(tickers, CancellationToken.None);   // bank before we exit
            cts.Cancel();
            await SafeAll(feedTask, oracleTask, evalTask, bankrollTask, snapTask, settleTask, reloadTask);
            return 0;
        }

        var statusTask = StatusLoopAsync(eval, feed, oracle, telemetry, pairs.Count, cts.Token);
        await SafeAll(feedTask, oracleTask, evalTask, bankrollTask, snapTask, settleTask, reloadTask, statusTask);
        PrintStatus(eval, feed, oracle, telemetry, pairs.Count);
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

                int newPairs = eval.UpsertPairs(fresh);
                var newTickers = fresh.Select(p => p.KalshiTicker)
                                      .Where(t => !everSeen.Contains(t)).Distinct(StringComparer.Ordinal).ToList();
                feed.EnqueueSubscribe(newTickers);
                int newTokens = oracle.AddTokens(fresh.SelectMany(p => new[] { p.YesToken, p.NoToken }));

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

                if (newPairs > 0 || newTokens > 0)
                    Console.WriteLine($"[PAIRS] reloaded: +{newPairs} market(s), +{newTokens} Pinnacle "
                                    + $"selection(s) — now {eval.PairCount} watched, {seenCount} ever seen "
                                    + "(finished markets are kept until their result is banked).");
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
            var yes = oracle.Get(p.YesToken);
            var no  = oracle.Get(p.NoToken);
            if (yes is null || no is null || !yes.Open || !no.Open) continue;
            if (!oracle.Fresh(yes) || !oracle.Fresh(no)) continue;
            try
            {
                log.Write(p, yes, no, oracle.AgeMs(yes), feed.Top(p.KalshiTicker).YesAsk, cfg.DeVigMethod);
                n++;
            }
            catch (Exception ex) { Console.WriteLine($"[SNAPSHOT] {p.KalshiTicker}: {ex.Message}"); }
        }
        return n;
    }

    // ── Bankroll ──────────────────────────────────────────────────────────────────────────────────────
    /// <summary>Sizing needs a bankroll even though M0 buys nothing: the Contracts column is what M1 will
    /// use to weight realised results, so a run with no bankroll logs correct EV and meaningless sizes.</summary>
    private static async Task RefreshBankrollAsync(KalshiOrderClient k, EvEvaluator eval, EvConfig cfg, bool announce)
    {
        try
        {
            long cents = await k.GetBalanceCentsAsync();
            eval.BankrollUsd = cents / 100.0;
            if (announce) Console.WriteLine($"[BANKROLL] ${eval.BankrollUsd:0.00} (live Kalshi balance)");
        }
        catch (Exception ex)
        {
            eval.BankrollUsd = cfg.BankrollFallback;
            if (announce)
                Console.WriteLine($"[BANKROLL] balance read failed ({ex.GetType().Name}) — using "
                                + $"EV_BANKROLL_USD=${eval.BankrollUsd:0.00}. EV is unaffected; only the "
                                + "size columns are.");
        }
    }

    private static async Task BankrollLoopAsync(KalshiOrderClient k, EvEvaluator eval, EvConfig cfg, CancellationToken ct)
    {
        while (!ct.IsCancellationRequested)
        {
            try { await Task.Delay(TimeSpan.FromSeconds(60), ct); } catch (OperationCanceledException) { break; }
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
                                              EvTelemetry t, int pairCount, CancellationToken ct)
    {
        while (!ct.IsCancellationRequested)
        {
            try { await Task.Delay(TimeSpan.FromSeconds(30), ct); } catch (OperationCanceledException) { break; }
            PrintStatus(e, f, o, t, pairCount);
        }
    }

    private static void PrintStatus(EvEvaluator e, KalshiBookFeed f, PinnacleOracle o, EvTelemetry t, int pairCount)
    {
        var s = e.Stats;
        Console.WriteLine(
            $"[{DateTime.UtcNow:HH:mm:ss}] pairs {pairCount} | ws {(f.IsConnected ? "up" : "DOWN")} msgs {f.MessageCount} "
          + $"| oracle {(o.IsConnected ? "up" : "DOWN")} quotes {o.QuoteCount} stale {o.StaleCount} "
          + $"{(o.SessionReady ? "" : "SESSION-DOWN ")}"
          + $"| screened {s.Screened} (noquote {s.NoQuote} stale {s.StaleOracle} susp {s.Suspended} "
          + $"below {s.BelowPrescreen} cooldown {s.Cooldown}) "
          + $"| rest {s.RestCalls} fail {s.RestFailed} 429 {s.RateLimited} "
          + $"| SIGNALS {s.Signals} rejected-at-rest {s.RejectedByRest} floored {s.FlooredToZero} "
          + $"| rows {t.RowsWritten} | bankroll ${e.BankrollUsd:0.00}");
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
              --book-audit [N]     dump the local WS ask ladder against REST for N markets and exit
              --pairs <path>       cross_pairs.json to read (default: HardVenArb's, or EV_PAIRS_FILE)
              --sidecar <url>      odds sidecar base URL (default: HARDVEN_SIDECAR_URL, or localhost:8787)
              --verbose            log candidates that REST rejected, and 429 back-offs

            Environment: EV_MIN, EV_PRESCREEN_SLACK, EV_MIN_PRICE, EV_MAX_PRICE, EV_DEVIG (proportional|shin),
            EV_FEE_RATE, EV_RECHECK_COOLDOWN_MS, EV_REST_CONCURRENCY, EV_MAX_TRADE_FRACTION, EV_BANKROLL_USD,
            EV_ORACLE_POLL_MS, EV_ORACLE_MAX_AGE_MS, EV_SNAPSHOT_MIN, EV_PAIRS_FILE, HARDVEN_SIDECAR_URL.
            """);
    }
}
