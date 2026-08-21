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

        if (bookAudit)
        {
            await WaitWarmAsync(feed, oracle, cts.Token, TimeSpan.FromSeconds(45));
            await BookAuditAsync(feed, kalshi, tickers, ArgInt(args, "--book-audit") ?? 10);
            cts.Cancel();
            await SafeAll(feedTask, oracleTask, evalTask, bankrollTask);
            return 0;
        }

        if (once)
        {
            if (!await WaitWarmAsync(feed, oracle, cts.Token, TimeSpan.FromSeconds(45)))
                Console.WriteLine("[ONCE] feeds did not fully warm up — evaluating with what arrived.");
            await Task.Delay(3_000, cts.Token).ContinueWith(_ => { });
            eval.SweepAll();
            // Bounded: the oracle keeps re-sweeping every few seconds, so Pending is not guaranteed to
            // reach zero on its own and an unbounded wait would simply never return.
            var drainBy = DateTime.UtcNow.AddSeconds(60);
            while (eval.Pending > 0 && DateTime.UtcNow < drainBy && !cts.IsCancellationRequested)
                await Task.Delay(200);
            await Task.Delay(3_000, cts.Token).ContinueWith(_ => { });   // let in-flight REST calls land
            PrintStatus(eval, feed, oracle, telemetry, pairs.Count);
            cts.Cancel();
            await SafeAll(feedTask, oracleTask, evalTask, bankrollTask);
            return 0;
        }

        var statusTask = StatusLoopAsync(eval, feed, oracle, telemetry, pairs.Count, cts.Token);
        await SafeAll(feedTask, oracleTask, evalTask, bankrollTask, statusTask);
        PrintStatus(eval, feed, oracle, telemetry, pairs.Count);
        Console.WriteLine($"[DONE] {telemetry.RowsWritten} row(s) → {telemetry.Path}");
        return 0;
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
                                             List<string> tickers, int count)
    {
        Console.WriteLine("\n══ BOOK AUDIT — local WS ladder vs REST, same instant ══");
        Console.WriteLine($"{"Ticker",-34} {"side",-4} {"wsAsk",8} {"restAsk",8} {"gap(c)",7} {"age(ms)",8}  ws top-3");

        var live = tickers.Where(t => feed.Top(t).HasSnapshot).Take(Math.Max(1, count)).ToList();
        if (live.Count == 0) { Console.WriteLine("(no market has a snapshot yet)"); return; }

        var gaps = new List<double>();
        foreach (var t in live)
        {
            var top = feed.Top(t);
            decimal ry, rn;
            try
            {
                using var doc = await kalshi.GetMarketAsync(t);
                var mkt = doc.RootElement.TryGetProperty("market", out var m) ? m : doc.RootElement;
                ry = EvEvaluator.AskDollars(mkt, yes: true);
                rn = EvEvaluator.AskDollars(mkt, yes: false);
            }
            catch (Exception ex) { Console.WriteLine($"{t,-34} REST failed: {ex.GetType().Name}"); continue; }

            foreach (var (side, ws, rest) in new[] { ("YES", top.YesAsk, ry), ("NO", top.NoAsk, rn) })
            {
                if (ws >= 1m || rest <= 0m) continue;
                double gap = (double)(rest - ws) * 100.0;
                gaps.Add(gap);
                string ladder = string.Join(" ", feed.AskLadder(t, side == "YES")
                                                     .Select(l => $"{l.Price:0.00}x{l.Size:0}"));
                Console.WriteLine($"{Trunc(t, 34),-34} {side,-4} {ws,8:0.0000} {rest,8:0.0000} "
                                + $"{gap,7:+0.0;-0.0} {top.AgeMs,8:0}  {ladder}");
            }
            await Task.Delay(120);   // polite spacing; this is a diagnostic, not a hot path
        }

        if (gaps.Count == 0) { Console.WriteLine("\n(nothing comparable)"); return; }
        gaps.Sort();
        double Pct(double q) => gaps[Math.Min(gaps.Count - 1, (int)(gaps.Count * q))];
        int worse = gaps.Count(g => g > 0);
        Console.WriteLine($"\n{gaps.Count} comparison(s):  p10 {Pct(.10):+0.0;-0.0}c   median {Pct(.50):+0.0;-0.0}c   "
                        + $"p90 {Pct(.90):+0.0;-0.0}c    REST worse for us in {worse}/{gaps.Count} "
                        + $"({100.0 * worse / gaps.Count:0}%)");
        Console.WriteLine("A positive gap means the WS ask is optimistic — the price we would have screened "
                        + "on is cheaper than the one actually offered.");
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
              --once               warm up, evaluate every pair once, print the tally, exit
              --book-audit [N]     dump the local WS ask ladder against REST for N markets and exit
              --pairs <path>       cross_pairs.json to read (default: HardVenArb's, or EV_PAIRS_FILE)
              --sidecar <url>      odds sidecar base URL (default: HARDVEN_SIDECAR_URL, or localhost:8787)
              --verbose            log candidates that REST rejected, and 429 back-offs

            Environment: EV_MIN, EV_PRESCREEN_SLACK, EV_MIN_PRICE, EV_MAX_PRICE, EV_DEVIG (proportional|shin),
            EV_FEE_RATE, EV_RECHECK_COOLDOWN_MS, EV_REST_CONCURRENCY, EV_MAX_TRADE_FRACTION, EV_BANKROLL_USD,
            EV_ORACLE_POLL_MS, EV_ORACLE_MAX_AGE_MS, EV_PAIRS_FILE, HARDVEN_SIDECAR_URL.
            """);
    }
}
