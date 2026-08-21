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
            // Kalshi rounds the order fee UP to the cent, which only bites at the sizes M2 will trade.
            Near(EvMath.OrderFee(0.50, 1), 0.02, 1e-12, "one contract at 0.50 is charged 2c, not 1.75c");
            Near(EvMath.OrderFee(0.50, 100), 1.75, 1e-12, "100 contracts at 0.50 is charged $1.75");
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
                                         2, "1.869;2.030"));
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

        Console.WriteLine($"\n{_pass} passed, {_fail} failed.");
        return _fail == 0 ? 0 : 1;
    }
}
