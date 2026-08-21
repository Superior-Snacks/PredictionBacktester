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
                                         -0.04, -0.04, -0.04, 0.0, 0.6, sz, 5000, 1.0, 27.5, true, "SIGNAL"));
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

        Console.WriteLine($"\n{_pass} passed, {_fail} failed.");
        return _fail == 0 ? 0 : 1;
    }
}
