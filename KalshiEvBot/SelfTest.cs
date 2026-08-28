using System.Globalization;
using PredictionBacktester.Engine.LiveExecution;

namespace KalshiEvBot;

/// <summary>
/// The whole of the bot's arithmetic, checked with no venue, no network and no clock.
///
/// <para>M0 produces exactly one artefact — a CSV — and every conclusion drawn from it months later rests
/// on this arithmetic being right at the moment the row was written. There is no fill to arbitrate a bad
/// de-vig the way a filled arb arbitrates a bad price, so these assertions are the only thing standing
/// between a sign error and a fortnight of confidently wrong data.</para>
///
/// <para>Run with <c>--self-test</c>. Exit code 0 = all passed.</para>
/// </summary>
public static class SelfTest
{
    private static int _pass, _fail;

    private static void Check(bool ok, string name, string detail = "")
    {
        if (ok) { _pass++; Console.WriteLine($"  PASS  {name}"); }
        else    { _fail++; Console.ForegroundColor = ConsoleColor.Red;
                  Console.WriteLine($"  FAIL  {name}  {detail}"); Console.ResetColor(); }
    }

    private static void Near(double a, double b, double tol, string name)
        => Check(Math.Abs(a - b) <= tol, name, $"got {a:0.########}, want {b:0.########} (tol {tol})");

    public static int Run()
    {
        Console.WriteLine("KalshiEvBot self-test — pure arithmetic, no venue.\n");

        // ── De-vig ────────────────────────────────────────────────────────────────────────────────────
        Console.WriteLine("De-vig");
        {
            // 1.90 / 2.10 -> S = 0.52632 + 0.47619 = 1.00251... use a realistic Pinnacle two-way instead.
            double oA = 1.869, oB = 2.030;
            var p = DeVig.Proportional(oA, oB);
            var s = DeVig.Shin(oA, oB);
            double pOther = DeVig.Proportional(oB, oA).PTrue;
            double sOther = DeVig.Shin(oB, oA).PTrue;
            Near(p.PTrue + pOther, 1.0, 1e-9, "proportional: the two legs sum to 1");
            Near(s.PTrue + sOther, 1.0, 1e-6, "shin: the two legs sum to 1");
            Near(p.Overround, 1.0 / oA + 1.0 / oB - 1.0, 1e-12, "overround V = S - 1");
            Check(s.ShinZ > 0, "shin: z is positive when the book carries margin", $"z={s.ShinZ}");
            Check(Math.Abs(s.PTrue - p.PTrue) > 1e-6,
                  "shin and proportional actually differ (else logging both is pointless)",
                  $"prop={p.PTrue:0.######} shin={s.PTrue:0.######}");

            // A vig-free book has nothing to attribute: Shin must collapse onto the raw probabilities.
            var fair = DeVig.Shin(2.0, 2.0);
            Near(fair.PTrue, 0.5, 1e-9, "shin degenerates to proportional at zero vig");
            Near(fair.ShinZ, 0.0, 1e-9, "shin: z = 0 at zero vig");

            Check(!DeVig.Quotable(1.0, 2.0), "a one-sided/nonsense book is not quotable");
        }

        // ── Three-way de-vig (soccer 1X2) ─────────────────────────────────────────────────────────────
        Console.WriteLine("\nThree-way de-vig");
        {
            // A realistic La Liga 1X2. Kalshi listed this fixture at 0.42 / 0.24 / 0.35.
            double oH = 2.30, oD = 3.40, oA = 3.10;
            var p = DeVig.ProportionalN(new[] { oH, oD, oA });
            var s = DeVig.ShinN(new[] { oH, oD, oA });
            Near(p.PTrue.Sum(), 1.0, 1e-9, "proportional: three legs sum to 1");
            Near(s.PTrue.Sum(), 1.0, 1e-6, "shin: three legs sum to 1");
            Check(p.Overround > 0, "overround is positive on a real 1X2", $"{p.Overround:0.####}");
            Check(s.ShinZ > 0, "shin z is positive with margin to attribute", $"{s.ShinZ:0.#####}");

            // THE MAPPING THAT MATTERS: Kalshi NO on the home team pays on draw OR away, and taking the
            // complement of the home leg must equal exactly that sum. Getting this wrong is the single
            // most dangerous 3-way bug — every number stays plausible.
            Near(1.0 - p.PTrue[0], p.PTrue[1] + p.PTrue[2], 1e-12,
                 "P(home NO) == P(draw) + P(away) — the complement rule holds for 3 legs");
            Near(1.0 - p.PTrue[1], p.PTrue[0] + p.PTrue[2], 1e-12, "…and for the draw leg");

            // A missing leg must invalidate the WHOLE book, not just that leg.
            Check(!DeVig.Quotable(new[] { oH, 0.0, oA }), "a 1X2 with a missing draw price is NOT quotable");
            Check(!DeVig.ProportionalN(new[] { oH, 0.0, oA }).Ok, "…and yields no probabilities at all");

            // THE DANGEROUS CASE: a 1X2 silently presented as a two-way. It is arithmetically detectable,
            // because a book that sums BELOW 1 is not generous — it is incomplete.
            var truncated = DeVig.ProportionalN(new[] { oH, oA });   // draw quietly dropped
            Check(truncated.Overround < 0,
                  "dropping the draw makes the book sum BELOW 1 — the signature of a missing leg",
                  $"S={truncated.Overround + 1:0.###}");
            Check(truncated.PTrue[0] > p.PTrue[0] + 0.10,
                  "…and it inflates P(home) far above the truth, in the direction that makes us bet",
                  $"truncated={truncated.PTrue[0]:0.###} true={p.PTrue[0]:0.###}");
            Check(DeVig.ProportionalN(new[] { oH, oD, oA }).Overround > 0,
                  "a COMPLETE book always sums above 1 — no bookmaker offers a negative margin");

            // The two-way path must still agree with the n-way one — they are the same code now, and a
            // divergence here would mean tennis and soccer had drifted apart.
            var two = DeVig.Proportional(1.869, 2.030);
            var twoN = DeVig.ProportionalN(new[] { 1.869, 2.030 });
            Near(two.PTrue, twoN.PTrue[0], 1e-15, "the 2-way helper and the n-way form agree exactly");
            var twoS = DeVig.Shin(1.869, 2.030);
            var twoSN = DeVig.ShinN(new[] { 1.869, 2.030 });
            Near(twoS.PTrue, twoSN.PTrue[0], 1e-15, "…and likewise for Shin");

            // Heavy favourite: exactly where proportional de-vig is least trustworthy, so the two methods
            // must visibly disagree rather than silently coincide.
            var hp = DeVig.ProportionalN(new[] { 1.02, 26.0, 41.0 });
            var hs = DeVig.ShinN(new[] { 1.02, 26.0, 41.0 });
            Check(Math.Abs(hp.PTrue[0] - hs.PTrue[0]) > 1e-6,
                  "on a 0.98 favourite the two de-vigs differ — the reason both are logged",
                  $"prop={hp.PTrue[0]:0.#####} shin={hs.PTrue[0]:0.#####}");
        }

        // ── Fees ──────────────────────────────────────────────────────────────────────────────────────
        Console.WriteLine("\nFees");
        {
            Near(EvMath.FeePerContract(0.50), 0.0175, 1e-12, "fee peaks at the money: 1.75c at p=0.50");
            Near(EvMath.FeePerContract(0.05), 0.00332500, 1e-9, "fee at the wing: 0.33c at p=0.05");
            Check(EvMath.FeePerContract(0.50) > EvMath.FeePerContract(0.20),
                  "the 0.20-0.80 window pays MORE fee than the wings, not less");
            Near(EvMath.FeePerContract(0.30), EvMath.FeePerContract(0.70), 1e-12, "fee arc is symmetric");
            // MEASURED against a real fill (2026-08-28, order 01a0480d): 5 contracts at 0.54 were charged
            // average_fee_paid 0.0174 and the balance moved exactly $0.0870. So the venue ceils the
            // PER-CONTRACT fee to $0.0001 and multiplies; it does NOT ceil the order total to the cent.
            Near(EvMath.OrderFee(0.54, 5), 0.0870, 1e-9,
                 "the observed fill: 5 @ 0.54 costs $0.0870, not the $0.09 a cent-ceiling would give");
            Near(EvMath.OrderFee(0.50, 1), 0.0175, 1e-12, "one contract at 0.50 pays the marginal 1.75c");
            Near(EvMath.OrderFee(0.50, 100), 1.75, 1e-12, "100 contracts at 0.50 is charged $1.75");
            Check(EvMath.OrderFee(0.54, 5) >= EvMath.FeePerContract(0.54) * 5,
                  "the ceiling can only ever round the fee UP, never down");
            // THE PUBLISHED RULE IS A CEILING ON THE ORDER TOTAL, not per contract: "rounds up such that
            // the fee + positionCost is rounded to a centicent" (fee schedule, 7 Jul 2026). The two agree
            // on the observed fill above and DIVERGE here, so this is the case that pins the right one:
            //   total   = 0.07*7*0.33*0.67 = 0.1083390 -> ceil to $0.0001 = 0.1084
            //   per-ctr = ceil(0.0154770)  = 0.0155 x 7                   = 0.1085
            Near(EvMath.OrderFee(0.33, 7), 0.1084, 1e-9,
                 "ceiling applies to the ORDER TOTAL (0.1084), not per contract (0.1085)");
            // M is per series and read live from the venue; it scales the fee linearly.
            Near(EvMath.OrderFee(0.50, 10, 2.0), 2 * EvMath.OrderFee(0.50, 10, 1.0), 1e-12,
                 "a series multiplier of 2 doubles the fee");
            Near(EvMath.OrderFee(0.50, 10, 0.0), 0.0, 1e-12, "a multiplier of 0 means the series is free");
            // ...and it must reach EV and the limit too, or a doubled fee would be invisible where it counts.
            Check(EvMath.Ev(0.60, 0.55, 2.0) < EvMath.Ev(0.60, 0.55, 1.0),
                  "M reaches EV: a costlier series is worth less");
            Check(EvMath.BreakEvenLimit(0.60, 0.01, 2.0) < EvMath.BreakEvenLimit(0.60, 0.01, 1.0),
                  "M reaches the IOC limit: a costlier series must be bought cheaper");
        }

        // ── EV and the break-even limit ───────────────────────────────────────────────────────────────
        Console.WriteLine("\nEV and the IOC limit");
        {
            Near(EvMath.Ev(0.60, 0.55), 0.60 - 0.55 - EvMath.FeePerContract(0.55), 1e-12,
                 "EV is pTrue minus price minus fee");

            // The limit is the whole safety argument: fill at it and the trade is +EV by construction.
            foreach (var (pTrue, evMin) in new[] { (0.60, 0.01), (0.35, 0.02), (0.80, 0.0), (0.50, 0.035) })
            {
                double lim = EvMath.BreakEvenLimit(pTrue, evMin);
                Near(EvMath.Ev(pTrue, lim), evMin, 1e-9,
                     $"EV at the limit price equals EV_MIN exactly (pTrue={pTrue}, min={evMin})");
            }
            Check(EvMath.BreakEvenLimit(0.60, 0.01) < 0.60,
                  "the limit sits below pTrue — the fee has to come out of the edge");
            Near(EvMath.BreakEvenLimit(0.20, 0.50), 0.0, 1e-12, "an unreachable threshold yields no price");
        }

        // ── The pivot thesis, as an executable assertion ──────────────────────────────────────────────
        Console.WriteLine("\nArb is a strict subset of +EV");
        {
            // An arb needs the Kalshi ask to beat Pinnacle's VIGGED price; a value bet only needs it to
            // beat the FAIR price. Since S > 1, pAsk/S < pAsk, so every arb is also +EV and +EV is looser.
            double oMine = 1.869, oOther = 2.030;
            double pAsk  = 1.0 / oOther;
            double s     = 1.0 / oMine + 1.0 / oOther;
            double pTrue = (1.0 / oMine) / s;

            // A price that is an arb by a whisker...
            double kArb = 1.0 - pAsk - EvMath.FeePerContract(0.5) - 0.001;
            Check(kArb + pAsk + EvMath.FeePerContract(kArb) < 1.0, "test setup: this price really is an arb");
            Check(EvMath.Ev(pTrue, kArb) > 0, "every arb is also +EV", $"ev={EvMath.Ev(pTrue, kArb):0.#####}");

            // ...and a price that is +EV but NOT an arb. It must exist, or the pivot buys nothing.
            double kEv = kArb + 0.01;
            Check(kEv + pAsk + EvMath.FeePerContract(kEv) >= 1.0, "test setup: this price is NOT an arb");
            Check(EvMath.Ev(pTrue, kEv) > 0, "+EV admits prices an arb screen rejects",
                  $"ev={EvMath.Ev(pTrue, kEv):0.#####}");
        }

        // ── Sizing ────────────────────────────────────────────────────────────────────────────────────
        Console.WriteLine("\nSizing");
        {
            Near(EvMath.Alpha(0.0345), 0.35 * (1 - 0.0345 / 0.08), 1e-12,
                 "alpha at the measured Pinnacle vig is about fifth-Kelly");
            Check(EvMath.Alpha(0.0345) is > 0.19 and < 0.21, "…which is ~0.20", $"{EvMath.Alpha(0.0345):0.####}");
            Near(EvMath.Alpha(0.20), 0.10, 1e-12, "alpha floors at 0.10 on a wide vig");
            Near(EvMath.Alpha(-0.05), 0.35, 1e-12, "alpha CAPS at 0.35 — a crossed book cannot up-size us");
            Near(EvMath.Beta(0.05), 1.0, 1e-12, "beta is 1 below 10% exposure");
            Near(EvMath.Beta(0.20), 0.5, 1e-12, "beta halves at 20% exposure");
            Near(EvMath.Beta(0.35), 0.0, 1e-12, "beta is 0 past 30% exposure");

            // Kelly must use the FEE-INCLUSIVE cost in the denominator too; using the bare quote there
            // over-sizes every bet slightly, and Kelly compounds.
            double f = EvMath.FullKelly(0.60, 0.55);
            double cost = 0.55 + EvMath.FeePerContract(0.55);
            Near(f, (0.60 - cost) / (1 - cost), 1e-12, "Kelly denominator is 1 - fee-inclusive cost");
            Near(EvMath.FullKelly(0.40, 0.55), 0.0, 1e-12, "a -EV price sizes to zero, never to a reverse bet");

            var big = EvMath.Size(0.90, 0.50, 0.0345, 10_000, 0);
            Near(big.Fraction, 0.03, 1e-12, "the 3% single-trade cap binds on a huge edge");

            // The floor() that silently eats a valid signal at a small bankroll must announce itself.
            var tiny = EvMath.Size(0.62, 0.55, 0.0345, 20, 0);
            Check(tiny.Contracts == 0 && tiny.FlooredToZero,
                  "a signal rounded to zero contracts is FLAGGED, not silently skipped");
            var ok = EvMath.Size(0.62, 0.55, 0.0345, 5_000, 0);
            Check(ok.Contracts > 0 && !ok.FlooredToZero, "a normal bankroll sizes to whole contracts",
                  $"{ok.Contracts}");
        }

        // ── Telemetry arity ───────────────────────────────────────────────────────────────────────────
        Console.WriteLine("\nTelemetry");
        {
            string dir = Path.Combine(Path.GetTempPath(), "kalshievbot_selftest_" + Guid.NewGuid().ToString("N"));
            try
            {
                using (var t = new EvTelemetry(dir))
                {
                    var sz = EvMath.Size(0.62, 0.55, 0.0345, 5_000, 0);
                    // Writes through the real path: an arity drift throws here rather than corrupting a
                    // month of rows that still parse and still look plausible.
                    t.Write(new EvSignal(DateTime.UtcNow, "T", "E", "YES", "O", "A vs B", "2026-08-21", true,
                                         1.869, 2.030, 1.0072, 0.0072, 0.001, 0.52, 0.521, 0.52, "proportional",
                                         120, 500, 0.51m, 0.55m, 0, 40,
                                         EvMath.FeePerContract(0.55), EvMath.CostPerContract(0.55),
                                         -0.04, -0.04, -0.04, 0.0, 0.6, sz, 5000, 1.0, 27.5, true, "SIGNAL",
                                         2, "1.869;2.030", true, 250, 137.5, "PINNACLE_LED", "ok",
                                         1.25, true));
                }
                var lines = File.ReadAllLines(Directory.GetFiles(dir, "*.csv")[0]);
                Check(lines.Length == 2, "header + one row written", $"{lines.Length} line(s)");
                int head = lines[0].Split(',').Length, row = lines[1].Split(',').Length;
                Check(head == EvTelemetry.Columns.Length, "header column count matches the schema", $"{head}");
                Check(row == head, "row arity matches the header", $"row={row} header={head}");
            }
            finally { try { Directory.Delete(dir, true); } catch { } }
        }

        // ── Rolling CSV ───────────────────────────────────────────────────────────────────────────────
        Console.WriteLine("\nRolling CSV writer");
        {
            string dir = Path.Combine(Path.GetTempPath(), "kalshievbot_csv_" + Guid.NewGuid().ToString("N"));
            try
            {
                string[] cols = { "A", "B", "C" };
                string first;
                using (var c = new RollingCsv(dir, "T", cols))
                {
                    first = c.Path;
                    c.WriteRow(new[] { "1", "2", "3" });
                    bool threw = false;
                    try { c.WriteRow(new[] { "1", "2" }); } catch (InvalidOperationException) { threw = true; }
                    Check(threw, "a short row THROWS rather than writing a silently-shifted line");
                    Check(c.RowsWritten == 1, "the rejected row was not counted", $"{c.RowsWritten}");
                }
                // Re-opening the same day must APPEND, not restate the header.
                using (var c = new RollingCsv(dir, "T", cols)) { c.WriteRow(new[] { "4", "5", "6" }); }
                var lines = File.ReadAllLines(first);
                Check(lines.Length == 3, "re-opening the same day appends", string.Join(" | ", lines));
                Check(lines.Count(l => l == "A,B,C") == 1, "the header is written exactly once");

                // A different column set must never interleave into the same file.
                using (var c = new RollingCsv(dir, "T", new[] { "A", "B", "C", "D" })) { c.WriteRow(new[] { "1", "2", "3", "4" }); }
                Check(Directory.GetFiles(dir, "T_*.csv").Length == 2,
                      "a changed schema rolls to a NEW file instead of mixing shapes",
                      string.Join(", ", Directory.GetFiles(dir, "T_*.csv").Select(Path.GetFileName)));
                Check(File.ReadAllLines(first).Length == 3, "…and the original file is untouched");
            }
            finally { try { Directory.Delete(dir, true); } catch { } }
        }

        // ── Follow-up tracker ─────────────────────────────────────────────────────────────────────────
        Console.WriteLine("\nFollow-up (closing line value)");
        {
            string dir = Path.Combine(Path.GetTempPath(), "kalshievbot_fu_" + Guid.NewGuid().ToString("N"));
            string? prevEnv = Environment.GetEnvironmentVariable("EV_FOLLOWUP_SEC");
            try
            {
                Environment.SetEnvironmentVariable("EV_FOLLOWUP_SEC", "1");
                // No venue objects behind these, so every checkpoint takes the UNREADABLE path — which is
                // the one that only fires in production when a match ends, and therefore the one least
                // likely to be noticed if its column count drifted.
                // Disposed before reading: the writer holds the file, and a plain File.ReadAllLines is not
                // share-tolerant — the same trap that crashed --verify until Csv.Read was taught to open
                // with FileShare.ReadWrite.
                using (var tr = new FollowUpTracker(
                           new PinnacleOracle("", Array.Empty<string>()),
                           new KalshiBookFeed(null!, new KalshiApiConfig(), Array.Empty<string>()), dir))
                using (var cts = new CancellationTokenSource())
                {
                    var run = tr.RunAsync(cts.Token);
                    tr.Schedule(new FollowUp(DateTime.UtcNow, "NOSUCH-TICKER", "YES",
                                             new[] { "1:2:home", "1:2:away" }, 0, "SIGNAL", "STANDING",
                                             0.50, 0.55, 0.02, "proportional"));
                    Thread.Sleep(2600);
                    cts.Cancel();
                    try { run.Wait(2000); } catch { }
                }

                var files = Directory.GetFiles(dir, "EvFollowUp_*.csv");
                Check(files.Length == 1, "a follow-up file is written", $"{files.Length} file(s)");
                var lines = File.ReadAllLines(files[0]);
                Check(lines.Length >= 2, "the unreadable checkpoint still writes a row rather than vanishing",
                      $"{lines.Length} line(s)");
                int head = lines[0].Split(',').Length;
                Check(head == FollowUpTracker.Columns.Length, "header matches the schema", $"{head}");
                Check(lines[1].Split(',').Length == head, "the unreadable row has full arity",
                      $"row={lines[1].Split(',').Length} header={head}");
                Check(lines[1].Contains("gone"), "…and says WHY it could not be read", lines[1]);
            }
            finally
            {
                Environment.SetEnvironmentVariable("EV_FOLLOWUP_SEC", prevEnv);
                try { Directory.Delete(dir, true); } catch { }
            }
        }

        // ── Pair guards ───────────────────────────────────────────────────────────────────────────────
        Console.WriteLine("\nPair guards");
        {
            Check(EvPairLoader.SidesAgree("Pierluigi Basile", "Comino vs Basile", "215171:1634341888:away", out _),
                  "a correctly-paired row passes");
            Check(!EvPairLoader.SidesAgree("Lorenzo Comino", "Comino vs Basile", "215171:1634341888:away", out var w),
                  "an INVERTED row is caught", w);
            Check(EvPairLoader.SidesAgree("Anyone", "no separator here", "1:2:home", out _),
                  "an unparsable title is not judged (a guard that fires on noise is noise)");
            Check(EvPairLoader.SidesAgree("Anyone", "A vs B", "1:2:over", out _),
                  "a derivative token is out of scope for this guard");
        }

        // ── Three-way pair loading ────────────────────────────────────────────────────────────────────
        Console.WriteLine("\nThree-way pair loading");
        {
            string dir = Path.Combine(Path.GetTempPath(), "kalshievbot_pairs_" + Guid.NewGuid().ToString("N"));
            Directory.CreateDirectory(dir);
            try
            {
                string Row(string tk, string yes, string no, bool tw, string? legs) =>
                    "{\"kalshi_ticker\":\"" + tk + "\",\"event_id\":\"EV1\",\"event_title\":\"Arsenal vs Coventry\","
                  + "\"kalshi_outcome\":\"Arsenal\",\"settlement_date\":\"2026-08-21\","
                  + "\"hardven_yes_token\":\"" + yes + "\",\"hardven_no_token\":\"" + no + "\""
                  + (tw ? ",\"three_way\":true" : "")
                  + (legs is null ? "" : ",\"hardven_legs\":" + legs) + "}";

                // Complete 3-way: three rows, one matchup, distinct legs, identical leg sets.
                string legs = "[\"9:77:home\",\"9:77:away\",\"9:77:draw\"]";
                string good = Path.Combine(dir, "good.json");
                File.WriteAllText(good, "[" + string.Join(",", new[]
                {
                    Row("K-ARS", "9:77:home", "9:77:away", true, legs),
                    Row("K-COV", "9:77:away", "9:77:home", true, legs),
                    Row("K-TIE", "9:77:draw", "9:77:home", true, legs),
                }) + "]");
                var okPairs = EvPairLoader.Load(good, out var rep1);
                Check(okPairs.Count == 3, "a complete 3-way event loads all three markets", $"{okPairs.Count}");
                Check(okPairs.All(p => p.ThreeWay && p.Legs.Count == 3 && p.LegsUsable),
                      "each row carries the full leg set and finds its own YES leg");
                Check(okPairs.Single(p => p.KalshiTicker == "K-TIE").YesLegIndex == 2,
                      "the Tie row points at the draw leg");

                // Missing legs must be DROPPED, never silently treated as a two-way.
                string bad = Path.Combine(dir, "bad.json");
                File.WriteAllText(bad, "[" + Row("K-ARS", "9:77:home", "9:77:away", true, null) + "]");
                var badPairs = EvPairLoader.Load(bad, out var rep2);
                Check(badPairs.Count == 0, "a 3-way row without hardven_legs is dropped, not downgraded",
                      $"{badPairs.Count}");
                Check(rep2.Any(r => r.Contains("three-way")), "…and the drop is reported, not silent");

                // Two markets resolving to the SAME Pinnacle leg is a contradiction: drop the whole event.
                string dup = Path.Combine(dir, "dup.json");
                File.WriteAllText(dup, "[" + string.Join(",", new[]
                {
                    Row("K-ARS", "9:77:home", "9:77:away", true, legs),
                    Row("K-COV", "9:77:home", "9:77:away", true, legs),   // same leg as its sibling
                    Row("K-TIE", "9:77:draw", "9:77:home", true, legs),
                }) + "]");
                var dupPairs = EvPairLoader.Load(dup, out var rep3);
                Check(dupPairs.Count == 0, "a 3-way event with two markets on one leg drops entirely",
                      $"{dupPairs.Count}");
                Check(rep3.Any(r => r.Contains("SAME Pinnacle leg")), "…for the stated reason");

                // A 2-way file must be unaffected by any of this.
                string two = Path.Combine(dir, "two.json");
                File.WriteAllText(two, "[" + Row("K-2W", "9:88:home", "9:88:away", false, null) + "]");
                var twoPairs = EvPairLoader.Load(two, out _);
                Check(twoPairs.Count == 1 && !twoPairs[0].ThreeWay && twoPairs[0].Legs.Count == 2,
                      "a two-way row still loads and synthesises its own leg pair");

                // A settlement-rule mismatch pairs perfectly on names, so only a blocklist can stop it.
                string blk = Path.Combine(dir, "blocked.json");
                File.WriteAllText(blk, "[" + Row("KXUCLADVANCE-26AUG25X", "9:99:home", "9:99:away", false, null) + "]");
                var blkPairs = EvPairLoader.Load(blk, out var rep4);
                Check(blkPairs.Count == 0, "KXUCLADVANCE is blocked (extra time / penalties vs Pinnacle's 90min)",
                      $"{blkPairs.Count}");
                Check(rep4.Any(r => r.Contains("SETTLEMENT-RULE")), "…and the reason is stated");
            }
            finally { try { Directory.Delete(dir, true); } catch { } }
        }

        // -- THE KINETIC FILTER --------------------------------------------------------------------
        // The guard's value rests entirely on TryRise refusing to answer when it cannot: a window it has no
        // samples for must NOT read as "flat", or a market we just started watching passes a filter that has
        // no evidence about it. That failure would be silent and look exactly like normal operation.
        Console.WriteLine();
        Console.WriteLine("-- kinetic filter (P_true motion) --");
        {
            var keep = TimeSpan.FromSeconds(60);
            var win  = TimeSpan.FromSeconds(5);
            var now  = new DateTime(2026, 8, 22, 12, 0, 0, DateTimeKind.Utc);

            var empty = new EvEvaluator.PTrueTrack();
            Check(!empty.TryRise(now, win, out _), "no history at all -> cannot answer (not 'flat')");

            var young = new EvEvaluator.PTrueTrack();
            young.Add(now.AddSeconds(-2), 0.40, keep);
            young.Add(now, 0.46, keep);
            Check(!young.TryRise(now, win, out _),
                  "history shorter than the window -> cannot answer, even though it rose 6c");

            var rising = new EvEvaluator.PTrueTrack();
            // Dense sampling, as production produces (~4/sec). A sparse fixture would trip the hole
            // check and test nothing about the rise itself.
            for (int i = 80; i >= 10; i--) rising.Add(now.AddMilliseconds(-i * 100), 0.40, keep);
            for (int i = 9;  i >= 0;  i--) rising.Add(now.AddMilliseconds(-i * 100), 0.46, keep);
            bool okR = rising.TryRise(now, win, out double riseR);
            Check(okR && Math.Abs(riseR - 0.06) < 1e-9, "spans the window -> +6c measured", $"{riseR:0.0000}");

            // The case the whole guard exists for: a gap opening because OUR price is falling.
            var falling = new EvEvaluator.PTrueTrack();
            for (int i = 80; i >= 10; i--) falling.Add(now.AddMilliseconds(-i * 100), 0.60, keep);
            for (int i = 9;  i >= 0;  i--) falling.Add(now.AddMilliseconds(-i * 100), 0.52, keep);
            bool okF = falling.TryRise(now, win, out double riseF);
            Check(okF && riseF < 0, "a DECLINING fair value reports negative rise -> suppressed", $"{riseF:0.0000}");

            // A static market must still carry a reference point, or the filter suppresses everything for
            // want of HISTORY rather than for want of MOTION -- a different thing, and logged differently.
            var flat = new EvEvaluator.PTrueTrack();
            for (int i = 12; i >= 0; i--) flat.Add(now.AddSeconds(-i), 0.33, keep);
            bool okS = flat.TryRise(now, win, out double riseS);
            Check(okS && Math.Abs(riseS) < 1e-9, "static market: answerable, and the answer is 'not rising'");

            // THE ORACLE-OUTAGE CASE. The sidecar cycles its browser on the lifecycle schedule while this
            // bot stays up, leaving a HOLE in the series. Measuring across it reports the oracle catching
            // up as though Pinnacle had moved — a false PINNACLE_LED, which is the exact shape the filter
            // exists to detect.
            var gapped = new EvEvaluator.PTrueTrack();
            gapped.Add(now.AddSeconds(-40), 0.40, keep);   // before the outage
            gapped.Add(now.AddSeconds(-8),  0.40, keep);   // ...32s hole...
            gapped.Add(now.AddSeconds(-1),  0.55, keep);   // oracle back, 15c higher
            // DEFAULT IS OFF, so the hole is tolerated: assert the shipped behaviour, not the opt-in one.
            // Holes longer than `keep` (~30s) are still refused, by pruning rather than by this check.
            bool holeChecked = Environment.GetEnvironmentVariable("EV_KINETIC_MAX_HOLE_SEC") is { } hv
                               && double.TryParse(hv, out var hvd) && hvd > 0;
            bool gotGapped = gapped.TryRise(now, win, out _);
            Check(holeChecked ? !gotGapped : gotGapped,
                  holeChecked ? "hole check ON -> refuses across an oracle outage"
                              : "hole check OFF by default -> a hole is tolerated (pruning covers real outages)");

            // A BUSY book must stay answerable. 5000 screening passes with a value changing every one of
            // them, across 20s: the 250ms floor keeps the buffer small, and small must not mean "shorter
            // than the window". This is the case a pure count cap silently broke.
            var busy = new EvEvaluator.PTrueTrack();
            for (int i = 0; i < 5000; i++) busy.Add(now.AddMilliseconds(i * 4), 0.5 + (i % 7) * 1e-4, keep);
            Check(busy.TryRise(now.AddMilliseconds(4 * 4999), win, out _),
                  "5000 rapid samples over 20s: still spans the 5s window");
        }

        // ── Live position persistence ─────────────────────────────────────────────────────────────────
        // THE POINT OF THIS CLASS IS A CASE THAT ONLY HAPPENS AFTER A CRASH, so it would otherwise ship
        // untested and its failure mode is silent: the caps quietly become per-process and the bot
        // re-enters markets it already bought. Exercised here against a real temp file.
        Console.WriteLine();
        Console.WriteLine("-- live position store (restart safety) --");
        {
            string dir = Path.Combine(Path.GetTempPath(), "evbot-selftest-" + Guid.NewGuid().ToString("N"));
            Directory.CreateDirectory(dir);
            try
            {
                string f = Path.Combine(dir, "pos.json");
                var now = DateTime.UtcNow.ToString("o", CultureInfo.InvariantCulture);

                var a = new LivePositionStore(f);
                Check(a.Load().Filled.Count == 0, "missing file loads empty rather than throwing");

                a.Save(new Dictionary<string, string> { ["KXATP-ABC|YES"] = now, ["KXATP-ABC|NO"] = now },
                       new Dictionary<string, decimal> { ["KXATP-ABC"] = 9.75m });

                var (fl, sp) = new LivePositionStore(f).Load();
                Check(fl.Count == 2 && fl.ContainsKey("KXATP-ABC|YES"), "filled sides survive a reload");
                Check(sp.TryGetValue("KXATP-ABC", out var v) && v == 9.75m,
                      "per-event spend survives a reload exactly", $"got {(sp.TryGetValue("KXATP-ABC", out var g) ? g : -1)}");

                // An entry past its TTL must not resurrect and block a market that settled long ago.
                string old = DateTime.UtcNow.AddDays(-99).ToString("o", CultureInfo.InvariantCulture);
                a.Save(new Dictionary<string, string> { ["OLD|YES"] = old, ["NEW|YES"] = now },
                       new Dictionary<string, decimal>());
                var pruned = new LivePositionStore(f).Load().Filled;
                Check(pruned.ContainsKey("NEW|YES") && !pruned.ContainsKey("OLD|YES"),
                      "expired entries are dropped, fresh ones kept");

                // A half-written file must degrade to empty, not take the bot down on startup.
                File.WriteAllText(f, "{ this is not json");
                var corrupt = new LivePositionStore(f);
                Check(corrupt.Load().Filled.Count == 0, "corrupt file loads empty without throwing");
                Check(corrupt.LoadNote.Contains("CORRUPT"), "and says so, rather than looking like a clean start");
            }
            finally { try { Directory.Delete(dir, true); } catch { } }
        }

        Console.WriteLine();
        Console.WriteLine($"{_pass} passed, {_fail} failed.");
        return _fail == 0 ? 0 : 1;
    }
}
