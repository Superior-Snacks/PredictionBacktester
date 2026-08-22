using System.Text.Json;

namespace KalshiEvBot;

/// <summary>
/// One Kalshi binary market joined to the two selections of the matching Pinnacle two-way.
///
/// <para>BOTH Pinnacle tokens are required, not just the one that corresponds to Kalshi YES. The arb bot
/// only ever needed the opposite leg it was going to hedge into; a value bet needs the SUM of both legs,
/// because the vig is only visible across the pair. A row with one token is unusable here even though it
/// was tradeable there.</para>
/// </summary>
public sealed record EvPair(
    string KalshiTicker,
    string EventId,
    string EventTitle,
    string KalshiOutcome,
    string Label,
    string SettlementDate,
    string YesToken,          // Pinnacle selection that pays when Kalshi YES resolves YES
    string NoToken,           // 2-way: the true complement. 3-way: merely ANOTHER leg — see below.
    string YesName,
    string NoName,
    bool ThreeWay,
    IReadOnlyList<string> Legs)   // every mutually-exclusive outcome of the matchup, YesToken among them
{
    /// <summary>
    /// <b>The trap this type exists to close.</b> On a two-way, <c>NoToken</c> is the complement of
    /// <c>YesToken</c> and <c>P(no) = 1 - P(yes)</c> follows. On a soccer 1X2 it is not: "not Arsenal" is
    /// Coventry <i>plus</i> the draw, while <c>NoToken</c> points at Coventry alone. Using it as a
    /// complement there yields a confidently wrong <c>P_true</c> that nothing downstream can detect,
    /// because every individual number still looks plausible.
    ///
    /// <para>So nothing in the evaluator reads <c>NoToken</c> for pricing. It works from <see cref="Legs"/>,
    /// which is the complete outcome set for both shapes, and takes the complement as
    /// <c>1 - P(YesToken)</c> — correct for any number of legs.</para>
    /// </summary>
    public int YesLegIndex => Legs is null ? -1 : Legs.ToList().IndexOf(YesToken);

    /// <summary>A pair is only usable if its YES leg is actually in the leg set — otherwise there is no
    /// probability to read out and the row is a data fault, not a trade.</summary>
    public bool LegsUsable => Legs is { Count: >= 2 } && YesLegIndex >= 0;
}

public static class EvPairLoader
{
    /// <summary>
    /// Kalshi series whose SETTLEMENT RULE does not match the Pinnacle market we pair against, whatever the
    /// names say. These are dropped outright, because nothing downstream can detect the mismatch: the teams
    /// are right, the matchup is right, the prices look sane, and the bet is simply resolved by a different
    /// rule than the one it was priced under.
    ///
    /// <para><c>KXUCLADVANCE</c> is the live example. It is structurally a clean two-way ("X advances past
    /// Y") so it would pair without complaint, but "advances" includes extra time and penalties while
    /// Pinnacle's 1X2 is explicitly <i>90 minutes plus stoppage, not extra time or penalties</i> — the
    /// Kalshi rules text says so outright. Every knockout tie level after 90 minutes would be mispriced,
    /// and only on the ties that go long, which is exactly when it costs most.</para>
    ///
    /// <para>Pairing these correctly needs Pinnacle's separate to-advance market, not the moneyline.</para>
    /// </summary>
    public static readonly HashSet<string> BlockedSeries = new(StringComparer.OrdinalIgnoreCase)
    {
        "KXUCLADVANCE",
    };

    private static string SeriesOf(string ticker) => (ticker ?? "").Split('-')[0];

    /// <summary>Finds cross_pairs.json without being told. Order matters: an explicit env var wins, then
    /// the arb bot's copy (which the pairing job rewrites and re-points, so it is the live one), then the
    /// working directory. Never bundles its own copy — a second file would drift from the pairing job
    /// silently and the bot would trade yesterday's matchup ids.</summary>
    public static string? Locate(string? explicitPath)
    {
        var candidates = new List<string?>
        {
            explicitPath,
            Environment.GetEnvironmentVariable("EV_PAIRS_FILE"),
            Path.Combine(Directory.GetCurrentDirectory(), "HardVenArb", "cross_pairs.json"),
            Path.Combine(Directory.GetCurrentDirectory(), "cross_pairs.json"),
            Path.Combine(AppContext.BaseDirectory, "..", "..", "..", "..", "HardVenArb", "cross_pairs.json"),
        };
        foreach (var c in candidates)
            if (!string.IsNullOrWhiteSpace(c) && File.Exists(c)) return Path.GetFullPath(c);
        return null;
    }

    /// <summary>Reads the pair file and applies both side-consistency guards. Returns the survivors;
    /// <paramref name="report"/> receives one line per rejection so the count is never a mystery.</summary>
    public static List<EvPair> Load(string path, out List<string> report)
    {
        report = new List<string>();
        var raw = JsonDocument.Parse(File.ReadAllText(path));
        var all = new List<EvPair>();
        int noTokens = 0, badLegs = 0, threeWayCount = 0, blocked = 0, unvalidated = 0;

        foreach (var el in raw.RootElement.EnumerateArray())
        {
            string S(string k) => el.TryGetProperty(k, out var v) && v.ValueKind == JsonValueKind.String
                                ? (v.GetString() ?? "") : "";
            string ticker = S("kalshi_ticker");
            string yes    = S("hardven_yes_token");
            string no     = S("hardven_no_token");
            if (string.IsNullOrWhiteSpace(ticker)) continue;
            if (BlockedSeries.Contains(SeriesOf(ticker))) { blocked++; continue; }
            if (string.IsNullOrWhiteSpace(yes) || string.IsNullOrWhiteSpace(no)) { noTokens++; continue; }

            bool threeWay = el.TryGetProperty("three_way", out var tw) && tw.ValueKind == JsonValueKind.True;

            // The leg set: every mutually-exclusive outcome of the matchup. Two-way rows synthesise it from
            // the pair, which is exactly right there. Three-way rows MUST carry it explicitly — the two
            // tokens on the row are two of three outcomes, and there is no way to infer the third.
            List<string> legs;
            if (threeWay)
            {
                legs = el.TryGetProperty("hardven_legs", out var lg) && lg.ValueKind == JsonValueKind.Array
                     ? lg.EnumerateArray().Where(x => x.ValueKind == JsonValueKind.String)
                         .Select(x => x.GetString() ?? "").Where(x => x.Length > 0).Distinct(StringComparer.Ordinal).ToList()
                     : new List<string>();

                // NO SILENT FALLBACK. Treating an incomplete three-way as a two-way would divide by the
                // wrong S and produce a plausible-looking P_true that is simply wrong — the exact failure
                // mode nothing downstream can catch. Skip the row and say so.
                if (legs.Count < 3 || !legs.Contains(yes, StringComparer.Ordinal)) { badLegs++; continue; }

                // A three-way the pairing could not PRICE-CHECK is unverified, not merely unlucky. Swapping
                // the two team legs leaves both markets on different sides of the right matchup, so the
                // structural checks all pass and only a comparison against real prices can catch it. When
                // that comparison did not happen there is no defence left, and the failure is silent and
                // enormous: 2026-08-22 produced 30 such events reading up to 42c of phantom edge, with
                // Manchester City valued at 23% against Kalshi's 65%.
                if (el.TryGetProperty("price_unvalidated", out var pu) && pu.ValueKind == JsonValueKind.True)
                { unvalidated++; continue; }
                threeWayCount++;
            }
            else legs = new List<string> { yes, no };

            all.Add(new EvPair(ticker, S("event_id"), S("event_title"), S("kalshi_outcome"), S("label"),
                               S("settlement_date"), yes, no, S("hardven_yes_name"), S("hardven_no_name"),
                               threeWay, legs));
        }
        if (noTokens > 0)
            report.Add($"{noTokens} row(s) skipped: unpaired (no Pinnacle token). A one-sided row cannot be "
                     + "de-vigged — there is no two-way sum to remove the margin from.");
        if (badLegs > 0)
            report.Add($"{badLegs} three-way row(s) skipped: `hardven_legs` missing, short, or not containing "
                     + "the YES token. A 1X2 needs all three prices to de-vig; there is no safe two-way "
                     + "fallback, so these are dropped rather than mispriced. Re-run the pairing job.");
        if (blocked > 0)
            report.Add($"{blocked} row(s) skipped: series blocked for a SETTLEMENT-RULE mismatch "
                     + $"({string.Join(", ", BlockedSeries)}). These pair cleanly on names but resolve "
                     + "under a different rule than the Pinnacle price we would value them with.");
        if (unvalidated > 0)
            report.Add($"{unvalidated} three-way row(s) skipped: the pairing could not PRICE-CHECK them "
                     + "(`price_unvalidated`). A swapped pair of team legs passes every structural check, "
                     + "so without that comparison there is no defence — re-run the pairing once the "
                     + "Pinnacle feed is warm and these become usable.");
        if (threeWayCount > 0)
            report.Add($"{threeWayCount} three-way row(s) loaded (soccer 1X2 and similar).");

        // ── Guard 1: title order vs token designation (advisory) ──────────────────────────────────────
        foreach (var p in all)
            if (!SidesAgree(p.KalshiOutcome, p.EventTitle, p.YesToken, out string why))
                report.Add($"[?] {p.KalshiTicker}: {why} — advisory only; the event check below is the one that drops.");

        // ── Guard 2: the two markets of one event must name the SAME matchup on OPPOSITE sides ────────
        // Assumption-free, and therefore the one allowed to drop rows: opposite outcomes of one fixture
        // cannot both be ':home', and cannot live on two different matchup ids. When they contradict,
        // nothing in the file says which row is wrong, so BOTH go — trading the survivor is a coin flip
        // wearing an arb's clothes. This is the check that caught two legs booked on one outcome.
        var broken = new HashSet<string>(StringComparer.Ordinal);

        // ── 3-WAY arm. Without this the check keys on Count()==2 and silently skips every soccer event —
        // leaving the rows with the MOST ways to be wrong as the only ones with no cross-check at all.
        // Three outcomes of one fixture must name one matchup, hold three DISTINCT yes legs, and agree on
        // what the leg set even is.
        foreach (var g in all.Where(p => p.ThreeWay && !string.IsNullOrEmpty(p.EventId))
                             .GroupBy(p => p.EventId))
        {
            var rows = g.ToList();
            var mids = rows.Select(p => p.YesToken.Split(':') is { Length: 3 } s ? s[1] : "").Distinct().ToList();
            string why =
                  mids.Count != 1 || mids[0].Length == 0
                      ? $"the markets name {mids.Count} different Pinnacle matchups"
                : rows.Select(p => p.YesToken).Distinct(StringComparer.Ordinal).Count() != rows.Count
                      ? "two markets resolved to the SAME Pinnacle leg — three outcomes cannot share one"
                : rows.Select(p => string.Join("|", p.Legs.OrderBy(x => x, StringComparer.Ordinal)))
                      .Distinct(StringComparer.Ordinal).Count() != 1
                      ? "the markets disagree about the matchup's leg set"
                : "";
            if (why.Length == 0) continue;
            broken.Add(g.Key);
            report.Add($"[DROP] all {rows.Count} market(s) of {g.Key}: {why}.");
        }

        foreach (var g in all.Where(p => !p.ThreeWay && !string.IsNullOrEmpty(p.EventId))
                             .GroupBy(p => p.EventId).Where(g => g.Count() == 2))
        {
            var seg = g.Select(p => p.YesToken.Split(':')).ToList();
            if (seg.Any(x => x.Length != 3)) continue;                       // derivative/malformed — not this check
            string why = seg[0][1] != seg[1][1]
                ? $"the two markets name DIFFERENT Pinnacle matchups ({seg[0][1]} vs {seg[1][1]})"
                : seg[0][2] == seg[1][2]
                    ? $"both markets buy the ':{seg[0][2]}' side — opposite outcomes cannot share one"
                    : "";
            if (why.Length == 0) continue;
            broken.Add(g.Key);
            report.Add($"[DROP] both markets of {g.Key}: {why}.");
        }

        return all.Where(p => !broken.Contains(p.EventId)).ToList();
    }

    /// <summary>
    /// Does the Kalshi outcome name the side this Pinnacle token actually buys, per the title's
    /// "{home} vs {away}" ordering? Advisory: the title is Kalshi's, so its ordering is an assumption
    /// about Pinnacle's, and a disagreement is a reason to look rather than a reason to drop.
    /// Returns true whenever it cannot judge — a guard that fires on unparsable input is noise.
    /// </summary>
    public static bool SidesAgree(string kalshiOutcome, string eventTitle, string yesToken, out string why)
    {
        why = "";
        string desig = (yesToken ?? "").Split(':') is { Length: 3 } tp ? tp[2] : "";
        if (desig is not ("home" or "away")) return true;               // derivative or malformed
        string title = eventTitle ?? "";
        int at = -1, seplen = 0;
        foreach (var sp in new[] { " vs ", " v ", " - " })
        {
            at = title.IndexOf(sp, StringComparison.OrdinalIgnoreCase);
            if (at > 0) { seplen = sp.Length; break; }
        }
        if (at <= 0) return true;
        string home = title[..at].Trim(), away = title[(at + seplen)..].Trim();
        if (home.Length == 0 || away.Length == 0 || string.IsNullOrWhiteSpace(kalshiOutcome)) return true;

        static HashSet<string> Words(string s) => new(
            new string((s ?? "").ToLowerInvariant().Select(c => char.IsLetter(c) || c == ' ' ? c : ' ').ToArray())
                .Split(' ', StringSplitOptions.RemoveEmptyEntries), StringComparer.Ordinal);

        var outcome = Words(kalshiOutcome);
        var mine    = Words(desig == "away" ? away : home);
        var other   = Words(desig == "away" ? home : away);
        if (outcome.Overlaps(mine) || !outcome.Overlaps(other)) return true;
        why = $"kalshi_outcome '{kalshiOutcome}' is the '{(desig == "away" ? home : away)}' side of "
            + $"'{title}', but hardven_yes_token is ':{desig}' which buys '{(desig == "away" ? away : home)}'";
        return false;
    }
}
