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
    string NoToken,           // the other side of the same matchup
    string YesName,
    string NoName);

public static class EvPairLoader
{
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
        int noTokens = 0;

        foreach (var el in raw.RootElement.EnumerateArray())
        {
            string S(string k) => el.TryGetProperty(k, out var v) && v.ValueKind == JsonValueKind.String
                                ? (v.GetString() ?? "") : "";
            string ticker = S("kalshi_ticker");
            string yes    = S("hardven_yes_token");
            string no     = S("hardven_no_token");
            if (string.IsNullOrWhiteSpace(ticker)) continue;
            if (string.IsNullOrWhiteSpace(yes) || string.IsNullOrWhiteSpace(no)) { noTokens++; continue; }

            all.Add(new EvPair(ticker, S("event_id"), S("event_title"), S("kalshi_outcome"), S("label"),
                               S("settlement_date"), yes, no, S("hardven_yes_name"), S("hardven_no_name")));
        }
        if (noTokens > 0)
            report.Add($"{noTokens} row(s) skipped: unpaired (no Pinnacle token). A one-sided row cannot be "
                     + "de-vigged — there is no two-way sum to remove the margin from.");

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
        foreach (var g in all.Where(p => !string.IsNullOrEmpty(p.EventId))
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
