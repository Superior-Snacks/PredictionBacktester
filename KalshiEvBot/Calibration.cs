using System.Globalization;

namespace KalshiEvBot;

/// <summary>One prediction, stripped to what grading needs. Sourced from either telemetry or snapshot rows.</summary>
public sealed record Obs(
    string Ticker, string Side, DateTime At, double PProp, double PShin, double PUsed,
    double RestAsk, double Cost, double Ev, int Contracts, bool InPlay, double OracleAgeMs,
    bool IsSignal, bool? Won);

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
    /// <summary>Turns raw telemetry rows into gradeable observations.</summary>
    public static List<Obs> FromTelemetry(IEnumerable<Dictionary<string, string>> rows,
                                          IReadOnlyDictionary<string, SettlementRecord> settled)
    {
        var outp = new List<Obs>();
        foreach (var r in rows)
        {
            string ticker = Csv.Str(r, "Ticker"), side = Csv.Str(r, "Side");
            if (ticker.Length == 0 || side.Length == 0) continue;
            DateTime.TryParse(Csv.Str(r, "Timestamp"), CultureInfo.InvariantCulture,
                              DateTimeStyles.RoundtripKind, out var at);
            bool? won = settled.TryGetValue(ticker, out var s) ? s.WonFor(side) : null;
            outp.Add(new Obs(
                ticker, side, at,
                Csv.Num(r, "PTrueProp"), Csv.Num(r, "PTrueShin"), Csv.Num(r, "PTrueUsed"),
                Csv.Num(r, "KalshiRestAsk"), Csv.Num(r, "CostPerContract"), Csv.Num(r, "Ev"),
                Csv.Int(r, "Contracts"), Csv.Str(r, "InPlay") == "1", Csv.Num(r, "OracleAgeMs"),
                Csv.Str(r, "Decision") == "SIGNAL", won));
        }
        return outp;
    }

    /// <summary>First observation per (ticker, side). First, not last: a later one is closer to the outcome
    /// and grading on it flatters the model for a reason that has nothing to do with the oracle.</summary>
    public static List<Obs> Dedupe(IEnumerable<Obs> obs)
        => obs.GroupBy(o => (o.Ticker, o.Side))
              .Select(g => g.OrderBy(o => o.At).First())
              .ToList();

    // ── Statistics ────────────────────────────────────────────────────────────────────────────────────
    /// <summary>Mean squared error of a probability forecast. Lower is better, and it is the one number
    /// that compares two de-vig methods without arguing about thresholds.</summary>
    public static double Brier(IEnumerable<(double P, bool Won)> v)
    {
        var l = v.ToList();
        return l.Count == 0 ? double.NaN : l.Average(x => Math.Pow(x.P - (x.Won ? 1.0 : 0.0), 2));
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
        int distinctTickers = all.Select(o => o.Ticker).Distinct(StringComparer.Ordinal).Count();
        int active = settled.Values.Count(s => !s.Terminal);
        int lost   = settled.Values.Count(s => s.IsGone);
        Console.WriteLine($"\n1. COVERAGE");
        Console.WriteLine($"   {all.Count} logged row(s) → {obs.Count} independent observation(s) "
                        + $"({(dedupe ? "one per ticker+side" : "NOT deduped")}) across {distinctTickers} market(s)");
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
            double se   = Math.Sqrt(Math.Max(1e-12, real * (1 - real)) / graded.Count);
            double z    = se > 0 ? (real - pred) / se : 0;
            double brier = Brier(graded.Select(o => (sel(o), o.Won!.Value)));
            Console.WriteLine($"   {name,-13} predicted {pred:0.0000}   realised {real:0.0000}   "
                            + $"diff {real - pred:+0.0000;-0.0000} ± {se:0.0000} (z={z:+0.0;-0.0})   "
                            + $"Brier {brier:0.0000}");
        }
        Console.WriteLine("   Lower Brier = better-calibrated forecast. A |z| under about 2 means the sample");
        Console.WriteLine("   cannot yet distinguish this bias from chance — collect more before acting on it.");

        // ── 4. Splits: the in-play / oracle-lag question ──────────────────────────────────────────────
        Console.WriteLine($"\n4. SPLITS  (does the edge survive where it was found?)");
        void Split(string label, IEnumerable<Obs> sub)
        {
            var l = sub.ToList();
            if (l.Count == 0) { Console.WriteLine($"   {label,-22} (none)"); return; }
            double pred = l.Average(o => o.PProp), real = l.Count(o => o.Won!.Value) / (double)l.Count;
            double se = Math.Sqrt(Math.Max(1e-12, real * (1 - real)) / l.Count);
            Console.WriteLine($"   {label,-22} n={l.Count,4}  predicted {pred:0.000}  realised {real:0.000}  "
                            + $"diff {real - pred:+0.000;-0.000} ± {se:0.000}");
        }
        Split("in-play", graded.Where(o => o.InPlay));
        Split("pre-match", graded.Where(o => !o.InPlay));
        Split("oracle < 2s old", graded.Where(o => o.OracleAgeMs >= 0 && o.OracleAgeMs < 2000));
        Split("oracle >= 2s old", graded.Where(o => o.OracleAgeMs >= 2000));
        Console.WriteLine("   If in-play calibrates WORSE than pre-match, the in-play signals are oracle lag:");
        Console.WriteLine("   we are seeing Pinnacle a second late while Kalshi has already repriced.");

        // ── 5. Signals — colour only ──────────────────────────────────────────────────────────────────
        var sigs = graded.Where(o => o.IsSignal).ToList();
        Console.WriteLine($"\n5. SIGNALS ONLY — {sigs.Count} settled  (colour, NOT evidence: see the header)");
        if (sigs.Count == 0) { Console.WriteLine("   none settled yet."); return; }
        double quoted = sigs.Sum(o => o.Ev * Math.Max(1, o.Contracts));
        double realis = sigs.Sum(o => ((o.Won!.Value ? 1.0 : 0.0) - o.Cost) * Math.Max(1, o.Contracts));
        int won = sigs.Count(o => o.Won!.Value);
        Console.WriteLine($"   won {won}/{sigs.Count}   quoted EV ${quoted:0.00}   realised ${realis:+0.00;-0.00}");
        foreach (var o in sigs.OrderBy(o => o.At))
            Console.WriteLine($"     {o.At:MM-dd HH:mm}  {o.Ticker,-42} {o.Side,-3} "
                            + $"ask {o.RestAsk:0.00}  ev {o.Ev * 100:+0.0;-0.0}c  x{o.Contracts,-3} "
                            + $"→ {(o.Won!.Value ? "WON " : "lost")} "
                            + $"{((o.Won!.Value ? 1.0 : 0.0) - o.Cost) * Math.Max(1, o.Contracts),+7:+0.00;-0.00}");
        Console.WriteLine($"\n   With {sigs.Count} settled signal(s), this line is noise. It becomes evidence in");
        Console.WriteLine( "   the hundreds; section 3 gets there far sooner.");
    }
}
