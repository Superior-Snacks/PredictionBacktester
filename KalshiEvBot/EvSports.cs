namespace KalshiEvBot;

/// <summary>
/// Which sport a Kalshi market belongs to, and which sports may place LIVE orders.
///
/// <para><b>Why a whitelist.</b> The bot trades whatever the pairing job writes into cross_pairs.json, and it
/// hot-reloads that file. So the day the pairer is widened to baseball or American football, a running bot
/// would start buying them within minutes, on an edge nobody has measured. <c>EV_LIVE_SPORTS</c> names the
/// sports allowed to trade (default: tennis); every other moneyline is watched on the SHADOW pipeline — the
/// same screening, its own files, never an order. A new sport is observe-only until someone names it here.
/// (Added 2026-10-08, before any non-tennis pair could reach the file.)</para>
/// </summary>
public static class EvSports
{
    // Explicit for the team-sport series, because "...GAME" alone reads as soccer: under that fallback every
    // NFL market would have been filed under the RETIRED sport and hidden from the strategy sections.
    private static readonly Dictionary<string, string> BySeries = new(StringComparer.OrdinalIgnoreCase)
    {
        ["KXNFLGAME"] = "football", ["KXNCAAFGAME"] = "football", ["KXCFLGAME"] = "football",
        ["KXNBAGAME"] = "basketball", ["KXNCAABGAME"] = "basketball", ["KXWNBAGAME"] = "basketball",
        ["KXEUROLEAGUEGAME"] = "basketball",
        ["KXNHLGAME"] = "hockey",
    };

    /// <summary>Coarse sport from the Kalshi SERIES (the ticker up to its first '-'). Matching on the series
    /// rather than the whole ticker keeps a team or player code from deciding the sport. An unrecognised
    /// series reads "other", never silently "soccer".</summary>
    public static string Of(string? ticker)
    {
        string series = (ticker ?? "").Split('-')[0].ToUpperInvariant();
        if (BySeries.TryGetValue(series, out var s)) return s;
        if (series.Contains("ATP") || series.Contains("WTA") || series.Contains("ITF")) return "tennis";
        if (series.Contains("MLB") || series.Contains("KBO") || series.Contains("NPB") || series.Contains("LMB")) return "baseball";
        if (series.Contains("NFL") || series.Contains("NCAAF")) return "football";   // their spread/total series
        if (series.Contains("GAME")) return "soccer";
        return "other";
    }

    /// <summary>EV_LIVE_SPORTS: comma-separated sport names. Unset or blank = tennis only.</summary>
    public static HashSet<string> ParseLive(string? csv)
    {
        var set = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        foreach (var s in (csv ?? "").Split(',', StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries))
            set.Add(s.ToLowerInvariant());
        if (set.Count == 0) set.Add("tennis");
        return set;
    }

    /// <summary>Which pipeline each pair belongs to: derivatives to their own; moneylines of a LIVE sport to
    /// the main one; every other moneyline to the shadow one. ONE function for startup and reload, so the two
    /// can never disagree about where a market goes.</summary>
    public static (List<EvPair> Ml, List<EvPair> Shadow, List<EvPair> Deriv) SplitPipelines(
        IEnumerable<EvPair> pairs, EvConfig cfg)
    {
        var ml = new List<EvPair>(); var sh = new List<EvPair>(); var dv = new List<EvPair>();
        foreach (var p in pairs)
            (p.IsDerivative ? dv : cfg.IsLiveSport(p.KalshiTicker) ? ml : sh).Add(p);
        return (ml, sh, dv);
    }
}
