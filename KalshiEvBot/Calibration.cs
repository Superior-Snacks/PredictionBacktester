using System.Globalization;
using System.Text.Json;
using System.Text.RegularExpressions;

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


    /// <summary>Cross-checks every SIGNAL ticker's Kalshi outcome name against the Pinnacle selection name
    /// recorded in pair_ledger.jsonl. A NAME check: it catches a token naming a different player, and is
    /// blind to a venue catalog that is itself mislabelled. Silent when the ledger is absent.</summary>
    private static void OrientationCheck(List<Obs> all)
    {
        string? path = new[]
        {
            Environment.GetEnvironmentVariable("EV_PAIR_LEDGER"),
            Path.Combine(Directory.GetCurrentDirectory(), "pair_ledger.jsonl"),
            Path.Combine(Directory.GetCurrentDirectory(), "HardVenArb", "pair_ledger.jsonl"),
        }.FirstOrDefault(p => !string.IsNullOrWhiteSpace(p) && File.Exists(p));
        if (path is null) return;                       // no ledger -> nothing to say, so say nothing

        // FIRST entry per ticker: the pairing that was in force when it signalled, not a later rewrite.
        var led = new Dictionary<string, (string Outcome, string Stored)>(StringComparer.Ordinal);
        foreach (string line in File.ReadLines(path))
        {
            if (string.IsNullOrWhiteSpace(line)) continue;
            try
            {
                using var d = JsonDocument.Parse(line);
                var r = d.RootElement;
                string tk = r.TryGetProperty("ticker", out var t) ? (t.GetString() ?? "") : "";
                string oc = r.TryGetProperty("kalshi_outcome", out var o) ? (o.GetString() ?? "") : "";
                string sn = r.TryGetProperty("stored_name", out var n) ? (n.GetString() ?? "") : "";
                if (tk.Length > 0 && !led.ContainsKey(tk)) led[tk] = (oc, sn);
            }
            catch (JsonException) { }                   // a torn line is not a reason to skip the check
        }
        if (led.Count == 0) return;

        static HashSet<string> Tok(string s) =>
            Regex.Replace(s ?? "", "[^a-z ]", " ", RegexOptions.IgnoreCase).ToLowerInvariant()
                 .Split(' ', StringSplitOptions.RemoveEmptyEntries)
                 .Where(w => w.Length >= 3).ToHashSet(StringComparer.Ordinal);

        int ok = 0, bad = 0, unknown = 0;
        var flips = new List<string>();
        foreach (string tk in all.Where(o => o.IsSignal).Select(o => o.Ticker).Distinct(StringComparer.Ordinal))
        {
            if (!led.TryGetValue(tk, out var e) || e.Outcome.Length == 0 || e.Stored.Length == 0) { unknown++; continue; }
            if (Tok(e.Outcome).Overlaps(Tok(e.Stored))) ok++;
            else { bad++; flips.Add($"{tk}: Kalshi '{e.Outcome}' but the token names '{e.Stored}'"); }
        }
        if (ok + bad == 0) return;

        if (bad > 0) Console.ForegroundColor = ConsoleColor.Red;
        Console.WriteLine($"   PAIR ORIENTATION: {ok} signal ticker(s) name-verified, {bad} MIS-ORIENTED"
                        + (unknown > 0 ? $", {unknown} with no ledger entry" : ""));
        foreach (string f in flips) Console.WriteLine($"      *** {f}");
        if (bad > 0)
        {
            Console.WriteLine("      ^ add these to ev_misoriented.json - their EV is a phantom and every");
            Console.WriteLine("        number below is contaminated until they are excluded.");
        }
        Console.ResetColor();
    }


    /// <summary>Section 7 — did the orders we tried actually fill? Silent until --live has written a row.</summary>
    private static void LivePathReport(string dir)
    {
        var files = Directory.GetFiles(dir, "EvLive_*.csv").OrderBy(f => f).ToList();
        if (files.Count == 0) return;                    // M0: nothing to say

        var rows = new List<(string Ticker, string Side, double Limit, double RestPx, double Ev,
                             int Req, string Status, double Fill, double Avg, double Ms, double Slip)>();
        foreach (string f in files)
        {
            using var sr = new StreamReader(f);
            string? head = sr.ReadLine();
            if (head is null) continue;
            var col = head.Split(',').Select((h, i) => (h.Trim('"'), i)).ToDictionary(x => x.Item1, x => x.i);
            string? line;
            while ((line = sr.ReadLine()) is not null)
            {
                var p = Csv.SplitLine(line);
                double D(string k) => col.TryGetValue(k, out int i) && i < p.Count
                                      && double.TryParse(p[i], NumberStyles.Any, CultureInfo.InvariantCulture,
                                                         out double v) ? v : double.NaN;
                string S(string k) => col.TryGetValue(k, out int i) && i < p.Count ? p[i] : "";
                rows.Add((S("Ticker"), S("Side"), D("LimitPrice"), D("RestAsk"), D("EvCents"),
                          (int)(double.IsNaN(D("Requested")) ? 0 : D("Requested")), S("Status"),
                          D("FillCount"), D("AvgFillPrice"), D("LatencyMs"), D("SlippageCents")));
            }
        }
        if (rows.Count == 0) return;

        // ATTEMPTS ONLY. A "budget-exhausted" row is a decision not to try, not a failure to fill, and
        // counting it as a miss would understate the fill rate by exactly the amount the caps impose.
        var att = rows.Where(r => r.Req > 0).ToList();
        var got = att.Where(r => r.Fill > 0).ToList();
        Console.WriteLine();
        Console.WriteLine("7. LIVE PATH  (can we actually buy what we find?)");
        if (att.Count == 0)
        {
            Console.WriteLine($"   {rows.Count} row(s), none an actual attempt (all budget-capped).");
            return;
        }
        double fillRate = 100.0 * got.Count / att.Count;
        Console.WriteLine($"   attempts {att.Count}   FILLED {got.Count} ({fillRate:0.0}%)   "
                        + $"no-fill {att.Count - got.Count}   skipped-by-budget {rows.Count - att.Count}");

        if (got.Count > 0)
        {
            double contracts = got.Sum(r => r.Fill);
            double spend = got.Sum(r => r.Fill * (r.Avg > 0 ? r.Avg : r.Limit));
            var slips = got.Where(r => !double.IsNaN(r.Slip)).Select(r => r.Slip).OrderBy(x => x).ToList();
            Console.WriteLine($"   bought {contracts:0} contract(s) for ${spend:0.00}");
            if (slips.Count > 0)
                Console.WriteLine($"   slippage vs the screened ask: median {slips[slips.Count / 2]:+0.00;-0.00}c  "
                                + $"worst {slips[^1]:+0.00;-0.00}c   "
                                + $"({slips.Count(x => x <= 0)}/{slips.Count} at or better than screened)");
            // PARTIAL FILLS ARE THE QUIET FAILURE. Getting 3 of 12 contracts is not "a fill" for a strategy
            // whose whole question is whether the size is there; it is reported separately rather than
            // folded into the headline rate.
            int partial = got.Count(r => r.Fill < r.Req);
            if (partial > 0)
                Console.WriteLine($"   PARTIAL on {partial} of {got.Count} fills — the depth was not there for "
                                + "the full size.");
        }
        var lat = att.Where(r => !double.IsNaN(r.Ms)).Select(r => r.Ms).OrderBy(x => x).ToList();
        if (lat.Count > 0)
            Console.WriteLine($"   order round-trip: median {lat[lat.Count / 2]:0}ms  p90 {lat[(int)(0.9 * (lat.Count - 1))]:0}ms");
        var bad = att.Where(r => r.Status.StartsWith("error", StringComparison.OrdinalIgnoreCase)).ToList();
        if (bad.Count > 0)
            Console.WriteLine($"   {bad.Count} attempt(s) ERRORED at the venue: "
                            + string.Join(", ", bad.Select(b => b.Status).Distinct().Take(4)));
        Console.WriteLine("   A low fill rate is the finding, not a fault: it means the book we screen is not");
        Console.WriteLine("   the book we can trade, and the edge is smaller than the telemetry suggests.");
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

        // RETIRED SPORTS ARE EVIDENCE ABOUT THE ORACLE, NOT ABOUT THE STRATEGY - so the split has to be
        // real. Section 4d already argued exactly this, but only section 5 acted on it: 4, 4b and 4c POOLED
        // every sport, and soccer is roughly two thirds of the settled volume while losing essentially all
        // of it. That is not a small bias. Measured 2026-08-24: the pooled bias read -0.018, concealing
        // tennis at +0.062 against soccer at -0.232; and an in-play oracle-age gradient that looked like a
        // decisive staleness effect (0 wins in 7 above 1s) was seven in-play SOCCER rows - Pinnacle
        // suspending on goals - while tennis has never once produced a signal on a quote older than 1s.
        // An oracle-age guard was nearly shipped on the strength of that pooled number.
        // So: 2 and 3 keep every sport, because the de-vig is sport-agnostic and needs the volume;
        // 4/4b/4c/5 see only LIVE sports, because they answer strategy questions; 4d stays pooled,
        // because it is the section whose whole job is to show the split.
        var retired = (Environment.GetEnvironmentVariable("EV_REPORT_RETIRED_SPORTS") ?? "soccer")
                      .Split(',', StringSplitOptions.RemoveEmptyEntries)
                      .Select(x => x.Trim().ToLowerInvariant())
                      .Where(x => x.Length > 0).ToHashSet();
        var live = graded.Where(o => !retired.Contains(Sport(o.Ticker))).ToList();

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

        // ── ORIENTATION SANITY: are the pairs behind these numbers pointing at the right player? ──────
        // EVERY FIGURE BELOW ASSUMES THE PAIRING IS SOUND. A flipped pair - Pinnacle token naming the
        // OPPONENT - still produces a de-vig, still clears EV, and still books; it simply prices one player
        // against the other's probability, so its "edge" is a phantom of roughly |1-2p|. Ten such tickers
        // are already excluded by ev_misoriented.json, and they were found only because their settled
        // results were impossible. This runs the check up front instead, on every resolve, because a
        // calibration report built on flipped pairs is confidently wrong rather than obviously broken.
        //
        // FREE AND LOCAL: pair_ledger.jsonl already records `kalshi_outcome` and `stored_name` (the Pinnacle
        // selection our token pointed at) side by side, so this needs no API and no fixture mapping.
        // NAME TOKENS, NOT SUBSTRINGS: "Felipe Meligeni Alves" vs "Felipe Meligeni Rodrigues Alves" are the
        // same player and a substring test strips both - it did, once.
        OrientationCheck(all);
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
        // LIVE SPORTS ONLY, unlike the two lines above it. Those grade the DE-VIG, which is
        // sport-agnostic and needs soccer's volume to be worth reading. This one grades the STRATEGY,
        // and a retired sport has no vote in that. Left pooled it was actively misleading: on
        // 2026-08-25 seven soccer signals that went 0-for-7 against a predicted 0.220 dragged the
        // headline signal diff from +0.016 (tennis, section 4c) to -0.005, flipping its sign on 7 of
        // 78 rows — so the report's most prominent signal number disagreed with 4c and 4d for no
        // reason a reader could see.
        var sigOnly = live.Where(o => o.IsSignal).ToList();
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
        Console.WriteLine($"\n4. SPLITS  (does the edge survive where it was found?)"
                        + (live.Count != graded.Count
                           ? $"  [LIVE ONLY: {graded.Count - live.Count} row(s) from {string.Join(",", retired)} excluded]"
                           : ""));
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
        var inPlay = live.Where(o => o.InPlay).ToList();
        var pre    = live.Where(o => !o.InPlay).ToList();
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
            if (live.Count < 3) return double.NaN;
            double ma = live.Average(a), mb = live.Average(b);
            double sab = live.Sum(o => (a(o) - ma) * (b(o) - mb));
            double sa = Math.Sqrt(live.Sum(o => Math.Pow(a(o) - ma, 2)));
            double sb = Math.Sqrt(live.Sum(o => Math.Pow(b(o) - mb, 2)));
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
        foreach (var g in live.Where(o => o.Regime.Length > 0)
                                .GroupBy(o => o.Regime).OrderByDescending(g => g.Count()))
            Split(g.Key.ToLowerInvariant(), g);
        if (!live.Any(o => o.Regime.Length > 0))
            Console.WriteLine("   (no rows carry MoveRegime yet — it was added 2026-08-22)");
        Console.WriteLine("   PINNACLE_LED is the thesis. If STANDING calibrates just as well, the edge is not");
        Console.WriteLine("   about speed at all. If KALSHI_LED calibrates BADLY, suppressing it was right.");

        // Every guard writes its own Decision, so each one can be graded on what it REMOVED. Five filters
        // were added in a single evening against single observed failures; this is the only thing that can
        // say whether they cut noise or cut edge.
        Console.WriteLine("\n4c. WHAT EACH GUARD SUPPRESSED  (did it remove noise, or remove edge?)");
        foreach (var g in live.Where(o => o.Decision.Length > 0 && o.Decision != "REJECTED_REST")
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


        // ── 4e. GUARD AUDIT: would the blocked candidates have PAID? ──────────────────────────────────
        // SECTION 4c CANNOT ANSWER THIS, and the reason is structural rather than a tuning problem.
        // Everything above runs on DEDUPED observations — one row per (ticker, side), preferring the first
        // SIGNAL and otherwise the first row chronologically. But ~90% of ticker+sides open with a
        // REJECTED_REST row, so that label wins the tiebreak and every guard decision that fired later on
        // the same ticker is discarded. Measured 2026-08-25: NOT_RISING held 13,529 telemetry rows and
        // reached 4c with ZERO; OUT_OF_BAND 10,105 rows reached it with 10. A guard blocking a thousand
        // candidates a day was being graded on a sample of one.
        //
        // So this section deliberately does NOT dedupe the same way. For each guard it takes the
        // (ticker, side) pairs where that guard fired and NO signal ever fired — if a signal fired we took
        // the trade, so it was never suppressed — and grades the first such row against settlement.
        //
        // THE TEST IS `realised - cost`, NOT `realised - predicted`. A suppressed candidate asks one
        // question: had we bought it at the recorded cost, would the payout have exceeded it? Break-even is
        // the COST, never 0.500 — the whole point of buying below fair value is that a 50% win rate at 46c
        // still pays. Two honest caveats: these are COUNTERFACTUAL fills at the logged REST cost (fair for a
        // taker, but never observed), and one ticker can trip several guards, so the rows overlap and the
        // n column does not sum.
        Console.WriteLine();
        Console.WriteLine("4e. GUARD AUDIT  (would what each guard BLOCKED have paid? break-even = cost)");
        var tradedKeys = all.Where(o => o.IsSignal)
                            .Select(o => (o.Ticker, o.Side)).ToHashSet();
        var blocked = all.Where(o => o.Won.HasValue
                                  && o.Decision.Length > 0
                                  && o.Decision != "REJECTED_REST" && o.Decision != "SIGNAL"
                                  && !retired.Contains(Sport(o.Ticker))
                                  && !tradedKeys.Contains((o.Ticker, o.Side)))
                         .GroupBy(o => (o.Ticker, o.Side, o.Decision))
                         .Select(g => g.OrderBy(o => o.At).First())      // first firing per ticker+side+guard
                         .GroupBy(o => o.Decision)
                         .OrderByDescending(g => g.Count())
                         .ToList();
        if (blocked.Count == 0)
            Console.WriteLine("   (nothing blocked has settled yet)");
        foreach (var g in blocked)
        {
            var l = g.ToList();
            int n = l.Count;
            double cost = l.Average(o => o.Cost);
            double real = l.Count(o => o.Won!.Value) / (double)n;
            var pl = l.Select(o => (o.Won!.Value ? 1.0 : 0.0) - o.Cost).ToList();
            double mean = pl.Average();
            double sd = n > 1 ? Math.Sqrt(pl.Sum(x => Math.Pow(x - mean, 2)) / (n - 1)) : 0;
            double se = n > 0 && sd > 0 ? sd / Math.Sqrt(n) : 0;
            double t = se > 0 ? mean / se : 0;
            // A guard only EARNS its keep at 2 sigma; anything less is a coin-flip dressed as a policy.
            string verdict = t >= 2 ? "<- EDGE THROWN AWAY, loosen it"
                           : t <= -2 ? "<- guard was RIGHT"
                           : "inconclusive";
            Console.WriteLine($"   {g.Key.ToLowerInvariant(),-20} n={n,4}  cost {cost:0.000}  realised {real:0.000}"
                            + $"  edge/ctr {mean:+0.0000;-0.0000}  t={t:+0.00;-0.00}  {verdict}");
        }
        Console.WriteLine("   A guard that blocks PROFITABLE candidates is costing money AND slowing the");
        Console.WriteLine("   verdict, because every suppressed trade is a settled sample we never collect.");


        // ── 6. WHEN WILL WE KNOW? ─────────────────────────────────────────────────────────────────────
        // THE TWO QUESTIONS ARE ONE QUESTION. Per-contract P/L is (realised - predicted) + quoted EV, so
        // the calibration diff and the money question are the same number shifted by a constant. They
        // therefore resolve at the SAME sample size — there is no separate, later P/L milestone to wait
        // for. What makes P/L *look* slower is that it is usually quoted in dollars, where Kelly sizing
        // adds variance that carries no information about whether the edge is real.
        //
        // BREAK-EVEN IS THE COST, NEVER 0.500. A signal may under-realise its predicted probability by its
        // entire EV and still pay, because the price was below fair value to begin with. So the threshold
        // the diff must clear is -EV, not zero.
        void WhenWillWeKnow(List<Obs> sg)
        {
            Console.WriteLine();
            Console.WriteLine("6. WHEN WILL WE KNOW?  (what has to be true, and how much more data it needs)");
            if (sg.Count < 2) { Console.WriteLine("   (not enough settled signals yet)"); return; }
            // ESTIMATE FROM SINGLE-SIDED MARKETS ONLY, and quote the target in the same unit.
            // The first cut computed the required n from ALL signal rows using binomial variance, which
            // assumes independent draws — but a market that signalled on BOTH sides is forced to exactly
            // one win, so those rows are perfectly anti-correlated, not independent. Including them
            // understates the variance, understates the n required, and leaves the headline target counted
            // in ROWS while the progress line underneath counts MARKETS. Two different units, one of them
            // wrong. Everything below is therefore computed on single-sided markets alone.
            var byMkt = sg.GroupBy(o => o.Ticker, StringComparer.Ordinal).ToList();
            var one   = byMkt.Where(g => g.Count() == 1).Select(g => g.First()).ToList();
            int twoSidedMkts = byMkt.Count - one.Count;
            if (one.Count < 2)
            {
                Console.WriteLine($"   only {one.Count} single-sided market(s) so far — nothing to project from yet.");
                return;
            }
            var sgU = one;
            int n = sgU.Count;
            double pred = sgU.Average(o => o.PProp);
            double cost = sgU.Average(o => o.Cost);
            double real = sgU.Count(o => o.Won!.Value) / (double)n;
            double ev   = pred - cost;                       // quoted edge per contract
            double diff = real - pred;                       // calibration miss
            double se   = Math.Sqrt(Math.Max(real * (1 - real), 1e-6) / n);
            double edge = diff + ev;                         // = realised - cost = P/L per contract
            double sig  = se > 0 ? edge / se : 0;
            // n for the 2-sigma verdict: n > 4*var / edge^2. Quoted twice, because this figure scales as
            // 1/edge^2 and the edge estimate is itself noisy — the optimistic and conservative cases differ
            // by several times, and reporting only the first would badly understate the wait.
            double var0 = Math.Max(real * (1 - real), 1e-6);
            double nOpt = edge > 0 ? 4 * var0 / (edge * edge) : double.NaN;
            double nCon = ev   > 0 ? 4 * var0 / (ev * ev)     : double.NaN;
            Console.WriteLine($"   A. CALIBRATION DIFF  (realised - predicted, single-sided live-sport markets)");
            Console.WriteLine($"      break-even   diff > {-ev:+0.0000;-0.0000}   (may under-realise by the whole EV and still pay)");
            Console.WriteLine($"      now          n={n}  diff {diff:+0.0000;-0.0000} +/- {se:0.0000}"
                            + $"   -> {sig:+0.00;-0.00} sigma above break-even");
            Console.WriteLine($"   B. PER-CONTRACT P/L  (realised - cost; identical test, shifted by EV)");
            Console.WriteLine($"      break-even   > 0");
            Console.WriteLine($"      now          {edge:+0.0000;-0.0000} per contract"
                            + $"   = {(cost > 0 ? 100 * edge / cost : 0):+0.0;-0.0}% ROI on a {cost:0.000} mean cost");
            Console.WriteLine($"   VERDICT ARRIVES AT (2 sigma):");
            if (double.IsFinite(nOpt))
                Console.WriteLine($"      n ~= {nOpt,6:0}   if the CURRENT edge estimate ({edge:+0.0000;-0.0000}) is the true one");
            if (double.IsFinite(nCon))
                Console.WriteLine($"      n ~= {nCon,6:0}   if the true edge is only the quoted EV ({ev:+0.0000;-0.0000}), i.e. diff = 0");
            // ── C. ALWAYS-VALID BOUND ────────────────────────────────────────────────────────────────
            // A FIXED-n TEST IS ONLY VALID IF YOU LOOK ONCE, AT AN n YOU COMMITTED TO IN ADVANCE.
            // Re-running --resolve every day and stopping the moment it looks good is exactly the optional-
            // stopping error: given enough peeks, a coin will eventually clear 2 sigma. So the fixed-n
            // targets above are the honest answer to "how long", but they are NOT licence to watch the
            // number weekly and call it the day it crosses.
            //
            // Robbins' normal-mixture confidence sequence removes that problem: the bound below holds
            // SIMULTANEOUSLY for every n, so it may be checked after every single settlement and acted on
            // the moment it clears zero. X = won - cost lies in an interval of width 1, so Hoeffding's
            // lemma gives sub-Gaussian sigma = 1/2. `rho` tunes where the boundary is tightest; it is set
            // to the conservative target so the bound is sharpest in the region where a verdict is
            // plausible.
            //
            // IT IS WIDER THAN THE FIXED-n INTERVAL AT THE SAME n, ALWAYS — that width is the price of
            // looking whenever you like, and it is why this crosses later than the fixed-n date when the
            // edge is exactly as estimated. It pays off in the other branch: if the true edge is BIGGER
            // than estimated, this clears zero early and the run can stop honestly, years before a
            // pre-committed n would have allowed.
            double Radius(double m, double rho, double alpha = 0.05, double sigma = 0.5)
            {
                if (m < 1) return double.PositiveInfinity;
                double a = m * rho + 1.0;
                return Math.Sqrt(2.0 * sigma * sigma * a / (m * m * rho) * Math.Log(Math.Sqrt(a) / alpha));
            }
            double rhoTune = double.IsFinite(nCon) && nCon > 1 ? 1.0 / nCon : 1.0 / 1000.0;
            double rad = Radius(n, rhoTune);
            double lower = edge - rad;
            Console.WriteLine("   C. ALWAYS-VALID BOUND  (safe to re-check after EVERY settlement)");
            Console.WriteLine($"      now          95% lower bound on the edge = {lower:+0.0000;-0.0000}"
                            + (lower > 0 ? "   *** CLEARS ZERO - verdict reached ***" : "   (needs > 0)"));
            long cross = 0;
            for (long m = n; m <= 200000; m = m < 2000 ? m + 10 : m + 250)
                if (edge - Radius(m, rhoTune) > 0) { cross = m; break; }
            Console.WriteLine(cross > 0
                ? $"      crosses zero at n ~= {cross}  IF the current edge ({edge:+0.0000;-0.0000}) is the true one"
                : "      does not cross within 200k at the current edge estimate");
            Console.WriteLine("      Wider than the fixed-n interval by design: that width is what buys the right");
            Console.WriteLine("      to look every day. Watch THIS one week to week, not A or B.");

            // PROGRESS TRACKS THE ALWAYS-VALID TARGET, not the optimistic fixed-n one. Section C is the
            // number that actually licenses a decision under the daily re-checking this report gets, so
            // measuring progress against A's target would advertise a milestone that cannot be acted on.
            double target = cross > 0 ? cross : nOpt;
            double pct = double.IsFinite(target) && target > 0 ? 100.0 * n / target : 0;
            Console.WriteLine($"   PROGRESS: {n} of ~{(double.IsFinite(target) ? target : 0),0:0} single-sided market(s)"
                            + (pct > 0 ? $"  ({pct:0.#}%)" : "")
                            + "  [against the always-valid target]");
            Console.WriteLine($"      {sg.Count} signal row(s) span {byMkt.Count} market(s); {twoSidedMkts} signalled on BOTH");
            Console.WriteLine( "      sides and are EXCLUDED above — a both-sides market is forced to exactly one win,");
            Console.WriteLine( "      so it is deterministic and says nothing about edge.");
        }

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
        if (sigs.Count == 0) { Console.WriteLine("   none settled yet."); WhenWillWeKnow(sigOnly); return; }
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
        WhenWillWeKnow(sigOnly);
        LivePathReport(Directory.GetCurrentDirectory());
        StakeScaling(sigOnly, Directory.GetCurrentDirectory());
    }

    /// <summary>
    /// 8. What the micro stake costs in rounded-up fees, and what a larger one would have returned.
    ///
    /// <para><b>The gap this measures.</b> <see cref="EvMath.Ev"/> prices the MARGINAL fee
    /// (<c>rate*p*(1-p)</c>, no count), but Kalshi charges <see cref="EvMath.OrderFee"/> — the whole order
    /// rounded UP to the cent. Three contracts at 50c pay $0.06 where the EV assumed $0.0525. That 0.25c per
    /// contract is a quarter of a 1c edge, it is pure loss, and no telemetry column has ever carried it.
    /// It shrinks as the order grows, which is precisely why the micro stake is the worst case.</para>
    ///
    /// <para><b>Why this projection is exact and not a guess.</b> The fee is a deterministic function of
    /// price and count, so "what would we have paid at $50 a side" is computed, not estimated. The ONE thing
    /// it cannot know is whether the book would have filled the larger size — that is the question M1 is
    /// running to answer, and until it does, every row below the current stake is an upper bound.</para>
    /// </summary>
    private static void StakeScaling(List<Obs> sigs, string dir)
    {
        // Single-sided markets only, for the same reason section 6 uses them: a both-sides market is forced
        // to exactly one win and carries no information about edge, so including it would quietly average
        // a deterministic row into a P/L estimate.
        var one = sigs.GroupBy(o => o.Ticker, StringComparer.Ordinal)
                      .Where(g => g.Count() == 1).Select(g => g.First())
                      .Where(o => o.Won.HasValue && o.RestAsk > 0 && o.RestAsk < 1).ToList();
        Console.WriteLine();
        Console.WriteLine("8. STAKE SCALING  (what the rounded-up fee costs, and what a bigger stake returns)");
        if (one.Count < 2) { Console.WriteLine("   (not enough settled single-sided signals yet)"); return; }

        Console.WriteLine($"   Kalshi rounds the fee UP to the cent on the WHOLE order; EV prices the marginal");
        Console.WriteLine($"   fee. The difference is pure loss and it shrinks as the order grows. {one.Count} signal(s).");
        Console.WriteLine();
        Console.WriteLine("   stake/side   mean x   fee paid   fee priced   drag/ctr   net edge/ctr   if unrounded   per $100");
        double[] ladder = { 5, 10, 25, 50, 100, 250 };
        foreach (double stake in ladder)
        {
            double charged = 0, priced = 0, netPl = 0, rawPl = 0, staked = 0; long ctrs = 0; int tradable = 0;
            foreach (var o in one)
            {
                int count = (int)Math.Floor(stake / o.RestAsk);
                if (count < 1) continue;                      // the stake cannot buy one contract here
                tradable++; ctrs += count; staked += count * o.RestAsk;
                double fc = EvMath.OrderFee(o.RestAsk, count);
                double fa = EvMath.FeePerContract(o.RestAsk) * count;
                charged += fc; priced += fa;
                // o.Cost already nets the MARGINAL fee, so only the rounding EXCESS is added back here.
                double pl = count * ((o.Won!.Value ? 1.0 : 0.0) - o.Cost);
                rawPl += pl;                                  // what EV assumed it would return
                netPl += pl - (fc - fa);                       // what the venue's rounding actually leaves
            }
            if (tradable == 0 || ctrs == 0) continue;
            double dragC = (charged - priced) / ctrs * 100.0;
            Console.WriteLine($"   ${stake,-10:0}  {(double)ctrs / tradable,6:0.0}   ${charged,7:0.00}   ${priced,8:0.00}"
                            + $"   {dragC,7:0.000}c   {netPl / ctrs,+10:+0.0000;-0.0000}"
                            + $"   {rawPl / ctrs,+10:+0.0000;-0.0000}"
                            + $"   {(staked > 0 ? 100 * netPl / staked : 0),+8:+0.00;-0.00}");
        }
        Console.WriteLine();
        Console.WriteLine("   'if unrounded' is the SAME row without the ceiling — so the pair is apples to");
        Console.WriteLine("   apples and the gap between them is exactly what rounding costs. It is the only");
        Console.WriteLine("   honest read of the fee: everything else in the row moves with the stake too.");
        Console.WriteLine();
        // WHY THIS DOES NOT MATCH SECTION 6, stated rather than left as a puzzle. Section 6 weights every
        // market EQUALLY (one settled market, one vote) because it is answering "is the edge real?" — a
        // question about calibration, where a cheap market and a dear one are one observation each. This
        // table weights by CONTRACTS, because it is answering "what would we have earned?" — and a $5 stake
        // buys 25 contracts at 20c against 6 at 80c. The two differ whenever results correlate with price,
        // which at n=101 is as likely to be noise as signal.
        Console.WriteLine("   NOT COMPARABLE WITH SECTION 6, and the difference is not an error: section 6");
        Console.WriteLine("   weights each market equally (is the edge real?), this weights by contracts");
        Console.WriteLine("   (what would it have earned?). A $5 stake buys 25 contracts at 20c but 6 at 80c,");
        Console.WriteLine("   so the two agree only if results are independent of price. At n=101 the gap");
        Console.WriteLine("   between them is as likely to be noise as a real skew toward cheap signals.");
        Console.WriteLine();
        Console.WriteLine("   Every row above the current stake assumes the book would have filled the larger");
        Console.WriteLine("   size — exactly what M1 is measuring. Upper bounds until section 7 says otherwise.");

        // ── What we ACTUALLY paid, once live rows exist ────────────────────────────────────────────────
        var files = Directory.GetFiles(dir, "EvLive_*.csv");
        if (files.Length == 0) return;
        double obsCharged = 0, obsPriced = 0, obsVenue = 0, obsChargedMatched = 0;
        long obsCtrs = 0, obsCtrsMatched = 0;
        int fills = 0, feeSkips = 0, venueRows = 0;
        foreach (var f in files)
            foreach (var r in Csv.Read(f))
            {
                if (Csv.Str(r, "Status") == "fee-rounding-negative") { feeSkips++; continue; }
                double fill = Csv.Num(r, "FillCount");
                if (fill <= 0) continue;
                fills++; obsCtrs += (long)fill;
                obsCharged += Csv.Num(r, "FeeChargedUsd");
                obsPriced  += Csv.Num(r, "FeeAssumedUsd");
                // COMPARE LIKE WITH LIKE. The venue total was summed over rows that CARRY FeeVenueUsd while
                // the model total covered every fill, so rows predating that column made the model look
                // permanently too expensive: 40 fills of model against 38 of venue read as +0.086c/contract
                // of "error" that was really two missing rows. Keep a parallel model total over exactly the
                // rows the venue figure covers.
                double v = Csv.Num(r, "FeeVenueUsd");
                if (v > 0)
                {
                    obsVenue += v;
                    obsChargedMatched += Csv.Num(r, "FeeChargedUsd");
                    obsCtrsMatched += (long)fill;
                    venueRows++;
                }
            }
        if (obsCtrs > 0)
        {
            Console.WriteLine();
            // MODEL vs MODEL is not a check. FeeCharged and FeeAssumed are both OUR arithmetic, so their
            // agreement proves only that we are self-consistent — an earlier cut of this line announced
            // "0.000c of real, unmodelled drag" while its own total was $0.06 too high. The only honest
            // comparison is against what the venue actually took, which FeeVenueUsd now records.
            Console.WriteLine($"   OBSERVED on {fills} fill(s), {obsCtrs} contract(s): our ceiling model "
                            + $"${obsCharged:0.0000} vs our marginal model ${obsPriced:0.0000} "
                            + $"({(obsCharged - obsPriced) / obsCtrs * 100.0:0.000}c/contract of rounding)");
            if (venueRows > 0 && obsCtrsMatched > 0)
                Console.WriteLine($"   VENUE CHARGED ${obsVenue:0.0000} on {venueRows} fill(s) / {obsCtrsMatched} "
                                + $"contract(s); our model on the SAME rows ${obsChargedMatched:0.0000} — "
                                + $"{(obsChargedMatched - obsVenue) / obsCtrsMatched * 100.0:+0.000;-0.000}c/contract "
                                + "off reality. THIS is the number that would catch a fee change.");
            else
                Console.WriteLine("   (no FeeVenueUsd recorded yet — rows predating that column cannot be "
                                + "checked against the venue, only against ourselves.)");
        }
        if (feeSkips > 0)
            Console.WriteLine($"   {feeSkips} order(s) were REFUSED because rounding erased the edge outright — "
                            + "the stake is too small to trade at those prices.");
    }
}
