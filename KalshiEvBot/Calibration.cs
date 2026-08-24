using System.Globalization;

namespace KalshiEvBot;

/// <summary>One prediction, stripped to what grading needs. Sourced from either telemetry or snapshot rows.</summary>
public sealed record Obs(
    string Ticker, string Side, DateTime At, double PProp, double PShin, double PUsed,
    double RestAsk, double Cost, double Ev, int Contracts, bool InPlay, double OracleAgeMs,
    bool IsSignal, bool? Won, int WsVerified,   // WsVerified: 1 yes, 0 no, -1 the row predates the column
    string Regime, string Decision);            // who moved first, and what the bot did about it

/// <summary>
/// Grades logged predictions against Kalshi settlement.
///
/// <para><b>This measures the MODEL, not the money.</b> A signal at p≈0.28 carries a payoff standard
/// deviation near 0.45 against a ~0.02 edge, and contracts inside one signal share an outcome, so detecting
/// that edge in realised P&amp;L at 2σ needs on the order of a thousand settled signals. Asking instead
/// whether <c>P_true</c> is CALIBRATED pools every logged row — signal or not — and a few hundred
/// settlements then resolve the 2–4 point bias that would eat the whole edge. The P&amp;L section is
/// reported last and should be read as colour, not as evidence.</para>
///
/// <para><b>One observation per (ticker, side) by default.</b> The re-check cooldown logs the same live
/// opportunity every 15s — one market produced six rows in five minutes — and those rows share a single
/// outcome. Counting them as six independent trials would shrink every confidence interval by more than
/// a factor of two and manufacture significance out of a repeat.</para>
/// </summary>
public static class Calibration
{
    /// <summary>Tickers whose Pinnacle token pointed at the OPPONENT — their P_true is the COMPLEMENT of
    /// the truth, so every EV from them is a phantom of roughly |1-2p| while the venues actually agree.
    ///
    /// <para>Read from <c>ev_misoriented.json</c> rather than compiled in, so a refined diagnosis is an edit
    /// instead of a rebuild — one listed entry is ambiguous by construction (a flip near p=0.5 is nearly
    /// undetectable) and must stay reversible.</para>
    ///
    /// <para><b>The rows are EXCLUDED here, never deleted from the telemetry.</b> Those CSVs are the
    /// append-only record this milestone is made of; destroying evidence to clean a report is how a wrong
    /// call becomes permanent.</para></summary>
    public static HashSet<string> MisorientedTickers()
    {
        var outp = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        try
        {
            string path = Path.Combine(Directory.GetCurrentDirectory(), "ev_misoriented.json");
            if (!File.Exists(path)) return outp;
            using var doc = System.Text.Json.JsonDocument.Parse(File.ReadAllText(path));
            if (doc.RootElement.TryGetProperty("tickers", out var t)
                && t.ValueKind == System.Text.Json.JsonValueKind.Object)
                foreach (var m in t.EnumerateObject()) outp.Add(m.Name);
        }
        catch (Exception ex) { Console.WriteLine($"[CAL] ev_misoriented.json unreadable: {ex.Message}"); }
        return outp;
    }

    /// <summary>How many rows the last FromTelemetry call dropped as mis-oriented, for the coverage line.</summary>
    public static int LastMisorientedDropped { get; private set; }

    /// <summary>Rows dropped because the WS book and the REST valuation disagreed by more than this many
    /// CENTS. Mirrors the live bot's EV_MAX_WS_REST_GAP so the report grades the rule the bot now runs.</summary>
    public static double MaxSourceGapCents => Env("EV_MAX_WS_REST_GAP", 0.03) * 100.0;
    public static int LastSourceGapDropped { get; private set; }

    /// <summary>In-play rows dropped for a quote older than the live gate — a feed that was already dying.</summary>
    public static double MaxInPlayAgeMs => Env("EV_ORACLE_MAX_AGE_INPLAY_MS", 1000);
    public static int LastStaleAgeDropped { get; private set; }

    private static double Env(string k, double d)
        => double.TryParse(Environment.GetEnvironmentVariable(k), System.Globalization.NumberStyles.Any,
                           CultureInfo.InvariantCulture, out var v) ? v : d;

    /// <summary>Turns raw telemetry rows into gradeable observations.</summary>
    public static List<Obs> FromTelemetry(IEnumerable<Dictionary<string, string>> rows,
                                          IReadOnlyDictionary<string, SettlementRecord> settled)
    {
        var outp = new List<Obs>();
        var misoriented = MisorientedTickers();
        int droppedMis = 0, droppedGap = 0, droppedAge = 0;
        foreach (var r in rows)
        {
            string ticker = Csv.Str(r, "Ticker"), side = Csv.Str(r, "Side");
            if (ticker.Length == 0 || side.Length == 0) continue;
            if (misoriented.Contains(ticker)) { droppedMis++; continue; }
            // THE TWO KALSHI SOURCES DISAGREED, so one of them was stale and nothing can say which. Rows
            // written BEFORE EV_MAX_WS_REST_GAP existed carry Decision=SIGNAL even though the live bot
            // would now suppress them, and grading them would score a rule the bot has abandoned — the
            // same reasoning that already excludes pre-WS-verified rows in section 5.
            //
            // It matters more here than a stale quote usually would, because the gap MANUFACTURES the
            // edge: with a 2c prescreen slack, a row whose WS ask reads 8c high only survives when the
            // REST-based EV is ~+7c. Measured 2026-08-24: 7 of 11 signals in one burst sat past 3c while
            // the all-day base rate was 0.62%.
            double srcGap = Math.Abs(Csv.Num(r, "WsRestGapCents"));
            if (srcGap > MaxSourceGapCents) { droppedGap++; continue; }
            // A QUOTE THAT WAS AGEING is a feed dying, not a slow tick. The sidecar stamps ts=now only WHILE
            // CONNECTED and serves the stored ts once the session drops, so age climbs with wall-clock the
            // moment the feed goes — and a row can clear the gate on the last poll before it closes.
            // Observed 2026-08-24: the final signal before a session drop sat at 4,855ms against a 5,000ms
            // gate, while p99 across all 312 signals was 546ms. Mirrors the live
            // EV_ORACLE_MAX_AGE_INPLAY_MS so the report grades the rule the bot now runs.
            double ageMs = Csv.Num(r, "OracleAgeMs");
            if (Csv.Str(r, "InPlay") == "1" && ageMs > MaxInPlayAgeMs) { droppedAge++; continue; }
            DateTime.TryParse(Csv.Str(r, "Timestamp"), CultureInfo.InvariantCulture,
                              DateTimeStyles.RoundtripKind, out var at);
            bool? won = settled.TryGetValue(ticker, out var s) ? s.WonFor(side) : null;
            outp.Add(new Obs(
                ticker, side, at,
                Csv.Num(r, "PTrueProp"), Csv.Num(r, "PTrueShin"), Csv.Num(r, "PTrueUsed"),
                Csv.Num(r, "KalshiRestAsk"), Csv.Num(r, "CostPerContract"), Csv.Num(r, "Ev"),
                Csv.Int(r, "Contracts"), Csv.Str(r, "InPlay") == "1", Csv.Num(r, "OracleAgeMs"),
                Csv.Str(r, "Decision") == "SIGNAL", won,
                Csv.Str(r, "OracleWsVerified") is "1" ? 1 : Csv.Str(r, "OracleWsVerified") is "0" ? 0 : -1,
                Csv.Str(r, "MoveRegime"), Csv.Str(r, "Decision")));
        }
        LastMisorientedDropped = droppedMis;
        LastSourceGapDropped = droppedGap;
        LastStaleAgeDropped = droppedAge;
        return outp;
    }

    /// <summary>One observation per (ticker, side): the FIRST row we would actually have ACTED on — the
    /// earliest <c>SIGNAL</c> — falling back to the earliest row of any kind when the market never signalled.
    ///
    /// <para><b>First, not last</b>, either way: a later row is closer to the outcome and grading on it
    /// flatters the model for a reason that has nothing to do with the oracle. Preferring the first SIGNAL
    /// keeps that property — it is the first ACTIONABLE moment, not the one nearest the answer.</para>
    ///
    /// <para><b>Why not simply the first row seen.</b> That was the original rule, and it quietly made two
    /// sections unable to answer their own questions. The first row of a market has NO history by
    /// definition, so its regime is always <c>FIRST_LOOK</c> — §4b reported `first_look n=62` and could
    /// never see the 41 settled <c>PINNACLE_LED</c> rows sitting in the same data. And because the first row
    /// is usually a <c>REJECTED_REST</c> screening pass, §2/§3 were calibrating every market we GLANCED at
    /// rather than the ones we would have traded. Both still worth measuring — hence the signals-only line
    /// in §3 — but they are different questions and were being reported as one.</para></summary>
    public static List<Obs> Dedupe(IEnumerable<Obs> obs)
        => obs.GroupBy(o => (o.Ticker, o.Side))
              .Select(g => g.Where(o => o.IsSignal).OrderBy(o => o.At).FirstOrDefault()
                        ?? g.OrderBy(o => o.At).First())
              .ToList();

    /// <summary>Coarse sport from the Kalshi series. A heuristic, and deliberately a visible one: an
    /// unrecognised series reads "other" rather than being silently folded into soccer.</summary>
    public static string Sport(string ticker)
    {
        string t = ticker.ToUpperInvariant();
        if (t.Contains("ATP") || t.Contains("WTA") || t.Contains("ITF")) return "tennis";
        if (t.Contains("MLB") || t.Contains("KBO") || t.Contains("NPB") || t.Contains("LMB")) return "baseball";
        if (t.Contains("GAME")) return "soccer";
        return "other";
    }

    // ── Statistics ────────────────────────────────────────────────────────────────────────────────────
    /// <summary>Mean squared error of a probability forecast. Lower is better, and it is the one number
    /// that compares two de-vig methods without arguing about thresholds.</summary>
    public static double Brier(IEnumerable<(double P, bool Won)> v)
    {
        var l = v.ToList();
        return l.Count == 0 ? double.NaN : l.Average(x => Math.Pow(x.P - (x.Won ? 1.0 : 0.0), 2));
    }

    /// <summary>Standard error of a realised proportion, Agresti-Coull adjusted (add two successes and two
    /// failures). The naive sqrt(p(1-p)/n) returns EXACTLY ZERO whenever a bucket is all wins or all losses,
    /// so a 2-for-2 split printed "+/- 0.000" — perfect confidence from two observations. This never
    /// collapses and is better behaved at the small n every early bucket has.</summary>
    private static double Se(int wins, int n)
    {
        double pa = (wins + 1.0) / (n + 2.0);
        return Math.Sqrt(pa * (1 - pa) / (n + 2.0));
    }

    private static string Bar(double predicted, double realised, int width = 22)
    {
        int a = (int)Math.Round(Math.Clamp(predicted, 0, 1) * width);
        int b = (int)Math.Round(Math.Clamp(realised, 0, 1) * width);
        var c = new char[width];
        for (int i = 0; i < width; i++) c[i] = '·';
        if (a < width) c[Math.Max(0, a)] = 'p';
        if (b < width) c[Math.Max(0, b)] = c[Math.Max(0, b)] == 'p' ? '#' : 'r';
        return new string(c);
    }

    // ── The report ────────────────────────────────────────────────────────────────────────────────────
    public static void Report(List<Obs> all, IReadOnlyDictionary<string, SettlementRecord> settled, bool dedupe = true)
    {
        Console.WriteLine();
        Console.WriteLine("══ CALIBRATION REPORT ══════════════════════════════════════════════════════");

        var obs = dedupe ? Dedupe(all) : all;
        var graded = obs.Where(o => o.Won.HasValue).ToList();

        // ── 1. Coverage ───────────────────────────────────────────────────────────────────────────────
        // Count only the markets THIS dataset actually contains. Counting the whole store reported "32
        // market(s) still active" under 25 observations across 17 markets — three numbers that cannot all
        // be about the same thing, because the store also holds markets from earlier sessions whose rows
        // are not in these files.
        var mine = all.Select(o => o.Ticker).Distinct(StringComparer.Ordinal).ToHashSet(StringComparer.Ordinal);
        int distinctTickers = mine.Count;
        int active = settled.Where(kv => mine.Contains(kv.Key)).Count(kv => !kv.Value.Terminal);
        int lost   = settled.Where(kv => mine.Contains(kv.Key)).Count(kv => kv.Value.IsGone);
        Console.WriteLine($"\n1. COVERAGE");
        Console.WriteLine($"   {all.Count} logged row(s) → {obs.Count} independent observation(s) "
                        + $"({(dedupe ? "one per ticker+side" : "NOT deduped")}) across {distinctTickers} market(s)");
        if (LastMisorientedDropped > 0)
        {
            // Stated, not silent. A quietly shorter dataset is indistinguishable from a quieter day, and the
            // whole reason these rows are excluded is that they looked plausible row by row.
            Console.ForegroundColor = ConsoleColor.DarkYellow;
            Console.WriteLine($"   EXCLUDED {LastMisorientedDropped} row(s) from {MisorientedTickers().Count} "
                            + "MIS-ORIENTED ticker(s) (ev_misoriented.json) — their Pinnacle token named the "
                            + "OPPONENT, so their EV is a phantom. Still present in the telemetry.");
            Console.ResetColor();
        }
        if (LastSourceGapDropped > 0)
        {
            Console.ForegroundColor = ConsoleColor.DarkYellow;
            Console.WriteLine($"   EXCLUDED {LastSourceGapDropped} row(s) where the Kalshi WS book and REST "
                            + $"disagreed by >{MaxSourceGapCents:0.#}c — one source was stale, and the gap "
                            + "manufactures apparent edge through the prescreen slack.");
            Console.ResetColor();
        }
        if (LastStaleAgeDropped > 0)
        {
            Console.ForegroundColor = ConsoleColor.DarkYellow;
            Console.WriteLine($"   EXCLUDED {LastStaleAgeDropped} in-play row(s) whose oracle quote was older "
                            + $"than {MaxInPlayAgeMs:0}ms — an ageing quote is a feed dying, not a slow tick.");
            Console.ResetColor();
        }
        Console.WriteLine($"   settled: {graded.Count}   awaiting settlement: {obs.Count - graded.Count} "
                        + $"({active} market(s) still active)");
        if (lost > 0)
        {
            // Not a rounding error in the sample — it is data that can never be recovered, so it is stated
            // outright rather than absorbed into "awaiting settlement" where it would look like patience.
            Console.ForegroundColor = ConsoleColor.Red;
            Console.WriteLine($"   LOST: {lost} market(s) are GONE from Kalshi with no result ever banked — "
                            + "those observations are unrecoverable.");
            Console.WriteLine("   Keep the bot running (it banks settlements every EV_SETTLE_POLL_MIN minutes) "
                            + "rather than relying on --resolve after the fact.");
            Console.ResetColor();
        }
        if (graded.Count == 0)
        {
            Console.WriteLine("\n   Nothing has settled yet — no calibration is possible. This is normal early:");
            Console.WriteLine("   a match that is postponed sits 'active' against a fallback close_time two");
            Console.WriteLine("   weeks out, while one that is played finalizes within the hour.");
            return;
        }

        // ── 2. Calibration by decile ──────────────────────────────────────────────────────────────────
        Console.WriteLine($"\n2. CALIBRATION BY P_true DECILE  (proportional de-vig, {graded.Count} settled)");
        Console.WriteLine("   bucket        n   predicted   realised    diff   p=predicted r=realised");
        foreach (var g in graded.GroupBy(o => Math.Min(9, (int)(o.PProp * 10))).OrderBy(g => g.Key))
        {
            var l = g.ToList();
            double pred = l.Average(o => o.PProp), real = l.Count(o => o.Won!.Value) / (double)l.Count;
            Console.WriteLine($"   {g.Key / 10.0:0.0}-{(g.Key + 1) / 10.0:0.0}  {l.Count,5}   "
                            + $"{pred,9:0.000}   {real,8:0.000}  {real - pred,+7:+0.000;-0.000}   {Bar(pred, real)}");
        }

        // ── 3. Pooled bias — the number that decides it ───────────────────────────────────────────────
        Console.WriteLine($"\n3. POOLED BIAS");
        foreach (var (name, sel) in new (string, Func<Obs, double>)[]
                 { ("proportional", o => o.PProp), ("Shin", o => o.PShin) })
        {
            double pred = graded.Average(sel);
            double real = graded.Count(o => o.Won!.Value) / (double)graded.Count;
            double se   = Se(graded.Count(o => o.Won!.Value), graded.Count);
            double z    = se > 0 ? (real - pred) / se : 0;
            double brier = Brier(graded.Select(o => (sel(o), o.Won!.Value)));
            Console.WriteLine($"   {name,-13} predicted {pred:0.0000}   realised {real:0.0000}   "
                            + $"diff {real - pred:+0.0000;-0.0000} ± {se:0.0000} (z={z:+0.0;-0.0})   "
                            + $"Brier {brier:0.0000}");
        }
        Console.WriteLine("   Lower Brier = better-calibrated forecast. A |z| under about 2 means the sample");
        Console.WriteLine("   cannot yet distinguish this bias from chance — collect more before acting on it.");

        // ORACLE ACCURACY AND STRATEGY ACCURACY ARE DIFFERENT QUESTIONS. The pooled line above grades every
        // market we looked at, which is the right test of whether the de-vig produces an honest probability
        // — and it is answerable from ANY sport, including ones we have stopped trading. This line grades
        // only the subset we would have BOUGHT. A de-vig can be well calibrated in general while the rows
        // that clear an EV threshold are exactly the ones where it is wrong, and only the second line sees
        // that. Reported separately rather than blended, because blending them answers neither.
        var sigOnly = graded.Where(o => o.IsSignal).ToList();
        if (sigOnly.Count > 0)
        {
            int smk = sigOnly.Select(o => o.Ticker).Distinct(StringComparer.Ordinal).Count();
            double sp = sigOnly.Average(o => o.PProp), sr = sigOnly.Count(o => o.Won!.Value) / (double)sigOnly.Count;
            Console.WriteLine($"   SIGNALS ONLY  n={sigOnly.Count} across {smk} market(s)  predicted {sp:0.0000}  "
                            + $"realised {sr:0.0000}  diff {sr - sp:+0.0000;-0.0000} +/- "
                            + $"{Se(sigOnly.Count(o => o.Won!.Value), sigOnly.Count):0.0000}");
            if (smk < 5)
                Console.WriteLine($"   ^ {smk} market(s) is not a sample. This line is a placeholder until the count grows.");
        }
        else Console.WriteLine("   SIGNALS ONLY  (none settled yet — the strategy itself is still ungraded)");

        // ── 4. Splits: the in-play / oracle-lag question ──────────────────────────────────────────────
        Console.WriteLine($"\n4. SPLITS  (does the edge survive where it was found?)");
        // EVERY LINE CARRIES ITS DISTINCT-MARKET COUNT. A mean over observations that mostly come from ONE
        // market is a mean over one outcome, and it reads exactly like a result: measured 2026-08-22, a
        // guard-grading line showed "+3.50c, 11 up / 1 down" that was a single WTA match sampled twelve
        // times, and a 125-signal drift figure that was three football matches. Both were briefly believed.
        // Printing `mk=` next to `n=` makes that visible at the point of reading rather than three analyses
        // later, and `n >> mk` earns an explicit warning rather than a footnote.
        void Split(string label, IEnumerable<Obs> sub)
        {
            var l = sub.ToList();
            if (l.Count == 0) { Console.WriteLine($"   {label,-22} (none)"); return; }
            int mk = l.Select(o => o.Ticker).Distinct(StringComparer.Ordinal).Count();
            double pred = l.Average(o => o.PProp), real = l.Count(o => o.Won!.Value) / (double)l.Count;
            string warn = mk == 1 && l.Count > 2 ? "  <- ONE MARKET"
                        : l.Count >= 4 * Math.Max(mk, 1) ? "  <- few markets"
                        : "";
            Console.WriteLine($"   {label,-22} n={l.Count,4} mk={mk,3}  predicted {pred:0.000}  realised {real:0.000}  "
                            + $"diff {real - pred:+0.000;-0.000} +/- {Se(l.Count(o => o.Won!.Value), l.Count):0.000}{warn}");
        }
        var inPlay = graded.Where(o => o.InPlay).ToList();
        var pre    = graded.Where(o => !o.InPlay).ToList();
        Split("in-play", inPlay);
        Split("pre-match", pre);

        // ORACLE AGE, WITHIN IN-PLAY ONLY. Split across the whole sample it is not a second test at all:
        // in-play quotes are pushed constantly and pre-match ones sit quiet, so "age" and "in-play" are the
        // same variable wearing two names — the first run reported byte-identical numbers for both pairs.
        // A lag effect can only show up as a gradient among rows that are otherwise alike.
        Split("  in-play, oracle <1s", inPlay.Where(o => o.OracleAgeMs >= 0 && o.OracleAgeMs < 1000));
        Split("  in-play, oracle >=1s", inPlay.Where(o => o.OracleAgeMs >= 1000));

        double Corr(Func<Obs, double> a, Func<Obs, double> b)
        {
            if (graded.Count < 3) return double.NaN;
            double ma = graded.Average(a), mb = graded.Average(b);
            double sab = graded.Sum(o => (a(o) - ma) * (b(o) - mb));
            double sa = Math.Sqrt(graded.Sum(o => Math.Pow(a(o) - ma, 2)));
            double sb = Math.Sqrt(graded.Sum(o => Math.Pow(b(o) - mb, 2)));
            return sa * sb > 0 ? sab / (sa * sb) : double.NaN;
        }
        double conf = Corr(o => o.InPlay ? 1 : 0, o => Math.Min(o.OracleAgeMs, 10_000));
        if (double.IsFinite(conf))
            Console.WriteLine($"   in-play vs oracle-age correlation: {conf:+0.00;-0.00}"
                            + (Math.Abs(conf) > 0.7
                                ? "  — CONFOUNDED. These two splits are measuring one variable; the lag "
                                + "question cannot be answered until the sample contains quiet in-play rows "
                                + "or busy pre-match ones."
                                : "  — separable enough to read the two splits independently."));
        Console.WriteLine("   If in-play calibrates WORSE than pre-match, the in-play signals are oracle lag:");
        Console.WriteLine("   we are seeing Pinnacle a second late while Kalshi has already repriced.");

        // ── 4b. WHO LED, and WHAT THE GUARDS COST ─────────────────────────────────────────────────────
        // The strategy's whole claim is that Pinnacle leads and Kalshi follows. If that is true, PINNACLE_LED
        // rows should calibrate better than STANDING ones, and KALSHI_LED should be actively bad — we
        // suppressed it on one demonstrated example, and this is where that call gets tested rather than
        // assumed.
        Console.WriteLine("\n4b. WHICH SIDE MOVED FIRST");
        foreach (var g in graded.Where(o => o.Regime.Length > 0)
                                .GroupBy(o => o.Regime).OrderByDescending(g => g.Count()))
            Split(g.Key.ToLowerInvariant(), g);
        if (!graded.Any(o => o.Regime.Length > 0))
            Console.WriteLine("   (no rows carry MoveRegime yet — it was added 2026-08-22)");
        Console.WriteLine("   PINNACLE_LED is the thesis. If STANDING calibrates just as well, the edge is not");
        Console.WriteLine("   about speed at all. If KALSHI_LED calibrates BADLY, suppressing it was right.");

        // Every guard writes its own Decision, so each one can be graded on what it REMOVED. Five filters
        // were added in a single evening against single observed failures; this is the only thing that can
        // say whether they cut noise or cut edge.
        Console.WriteLine("\n4c. WHAT EACH GUARD SUPPRESSED  (did it remove noise, or remove edge?)");
        foreach (var g in graded.Where(o => o.Decision.Length > 0 && o.Decision != "REJECTED_REST")
                                .GroupBy(o => o.Decision).OrderByDescending(g => g.Count()))
            Split(g.Key.ToLowerInvariant(), g);
        Console.WriteLine("   A suppressed class that calibrates WELL is edge we threw away — loosen that guard.");

        // ── 4d. BY SPORT ──────────────────────────────────────────────────────────────────────────────
        // SPLIT, NOT FILTER. It is tempting to delete a sport once it has been ruled out for trading —
        // soccer was, on 44,246 rows showing zero oracle-led moves. But that verdict was about CAPTURABILITY
        // (Pinnacle suspends on goals, so we are structurally last), not about whether the de-vigged number
        // was CORRECT. Those are independent, and §2/§3 test the second one. On 2026-08-22 soccer held 96 of
        // the 104 settled outcomes: dropping it would have left the de-vig validated by eight observations.
        // So a retired sport still pays rent as evidence about the ORACLE, and the split is what keeps it
        // from contaminating conclusions about the STRATEGY.
        Console.WriteLine();
        Console.WriteLine("4d. BY SPORT  (a retired sport still grades the DE-VIG, just not the strategy)");
        foreach (var g in graded.GroupBy(o => Sport(o.Ticker)).OrderByDescending(g => g.Count()))
            Split(g.Key, g);
        var sigBySport = graded.Where(o => o.IsSignal).GroupBy(o => Sport(o.Ticker)).ToList();
        if (sigBySport.Count > 0)
        {
            Console.WriteLine("   signals only:");
            foreach (var g in sigBySport.OrderByDescending(g => g.Count())) Split("  " + g.Key, g);
        }
        Console.WriteLine("   Capturability is per-sport; de-vig accuracy should NOT be. If one sport");
        Console.WriteLine("   calibrates far worse than another, that is the MODEL failing, not the venue.");

        // ── 5. Signals — colour only ──────────────────────────────────────────────────────────────────
        var sigs = graded.Where(o => o.IsSignal).ToList();
        // RETIRED SPORTS ARE EXCLUDED FROM THE P&L LIST, and ONLY from here.
        //
        // Soccer is closed for this strategy on evidence (44,246 rows, zero oracle-led moves; goal
        // suspensions mean we are structurally last), so its nine signals - all taken BEFORE the price band,
        // de-vig agreement and kinetic filter existed - describe a configuration that no longer runs.
        // Listing them beside live tennis rows makes the section harder to read and its total misleading.
        //
        // They are NOT dropped from the DATA and NOT from any other section. Sections 2/3 calibrate the
        // ORACLE, which is sport-agnostic, and soccer supplies most of the settled volume there (520 of 661
        // on 2026-08-24) - deleting it would leave the de-vig validated by a handful of observations. The
        // excluded rows are summarised in one line below, so the record is filtered rather than quietly
        // shortened. Set EV_REPORT_RETIRED_SPORTS= (empty) to restore them.
        var retired = (Environment.GetEnvironmentVariable("EV_REPORT_RETIRED_SPORTS") ?? "soccer")
                      .Split(',', StringSplitOptions.RemoveEmptyEntries)
                      .Select(x => x.Trim().ToLowerInvariant())
                      .Where(x => x.Length > 0).ToHashSet();
        var droppedSigs = sigs.Where(o => retired.Contains(Sport(o.Ticker))).ToList();
        if (droppedSigs.Count > 0) sigs = sigs.Where(o => !retired.Contains(Sport(o.Ticker))).ToList();
        Console.WriteLine($"\n5. SIGNALS ONLY — {sigs.Count} settled  (colour, NOT evidence: see the header)");
        if (droppedSigs.Count > 0)
        {
            double dq = droppedSigs.Sum(o => o.Ev * Math.Max(1, o.Contracts));
            double dr = droppedSigs.Sum(o => ((o.Won!.Value ? 1.0 : 0.0) - o.Cost) * Math.Max(1, o.Contracts));
            Console.WriteLine($"   (excluding {droppedSigs.Count} settled signal(s) from retired sport(s) "
                            + $"[{string.Join(",", retired)}]: {droppedSigs.Count(o => o.Won!.Value)} won, "
                            + $"quoted ${dq:0.00}, realised ${dr:+0.00;-0.00} - still graded in sections 2-4d)");
        }
        if (sigs.Count == 0) { Console.WriteLine("   none settled yet."); return; }
        double quoted = sigs.Sum(o => o.Ev * Math.Max(1, o.Contracts));
        double realis = sigs.Sum(o => ((o.Won!.Value ? 1.0 : 0.0) - o.Cost) * Math.Max(1, o.Contracts));
        int won = sigs.Count(o => o.Won!.Value);
        Console.WriteLine($"   won {won}/{sigs.Count}   quoted EV ${quoted:0.00}   realised ${realis:+0.00;-0.00}");
        int preGate = sigs.Count(o => o.WsVerified < 0);
        if (preGate > 0)
        {
            // Rows written before OracleWsVerified existed cannot be told apart from verified ones by the
            // Decision column alone, yet they were logged under a rule that no longer applies — a
            // screening-only quote could be labelled SIGNAL then and would be SIGNAL_UNVERIFIED now.
            // Counting them silently would grade a rule the bot has since abandoned.
            Console.ForegroundColor = ConsoleColor.DarkYellow;
            Console.WriteLine($"   NOTE: {preGate} of these predate the WS-verified gate (blank column) and "
                            + "were logged under the older, looser rule. Marked '?' below.");
            Console.ResetColor();
        }
        foreach (var o in sigs.OrderBy(o => o.At))
            Console.WriteLine($"     {o.At:MM-dd HH:mm}  {o.Ticker,-42} {o.Side,-3} "
                            + $"ask {o.RestAsk:0.00}  ev {o.Ev * 100:+0.0;-0.0}c  x{o.Contracts,-3} "
                            + $"{(o.WsVerified < 0 ? "?" : o.WsVerified == 1 ? "wv" : "  ")} "
                            + $"→ {(o.Won!.Value ? "WON " : "lost")} "
                            + $"{((o.Won!.Value ? 1.0 : 0.0) - o.Cost) * Math.Max(1, o.Contracts),+7:+0.00;-0.00}");
        Console.WriteLine($"\n   With {sigs.Count} settled signal(s), this line is noise. It becomes evidence in");
        Console.WriteLine( "   the hundreds; section 3 gets there far sooner.");
    }
}
