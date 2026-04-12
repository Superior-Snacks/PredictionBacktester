using System.Text.Json;
using PredictionBacktester.Engine.LiveExecution;

namespace PredictionBacktester.Engine;

/// <summary>
/// Two types of arb available on Kalshi:
///
///   BINARY  — single market: YES ask + NO ask &lt; $1.00
///             Buy YES on the market AND buy NO on the same market.
///             One side always resolves to $1.00.
///             Represented as a 2-leg event: [ticker, ticker_NO]
///
///   CATEGORICAL — multi-leg event: sum of YES asks across all legs &lt; $1.00
///             Buy YES on every market in the group.
///             Exactly one pays $1.00 at resolution.
///             Represented as an N-leg event: [ticker1, ticker2, ...]
///
/// The "_NO" suffix is a virtual ID: it tells Program.cs to build a separate
/// LocalOrderBook populated from the NO side of that market's WebSocket feed.
/// </summary>
public class KalshiScanResult
{
    /// <summary>eventId → [ticker1, ticker2, ...] — buy YES on each leg.</summary>
    public Dictionary<string, List<string>> CategoricalEvents { get; init; } = new();

    /// <summary>
    /// "BIN_{ticker}" → [ticker, ticker_NO] — buy YES on ticker, buy NO on ticker_NO.
    /// Legs share the same WebSocket stream; Program.cs splits them into two books.
    /// </summary>
    public Dictionary<string, List<string>> BinaryMarkets { get; init; } = new();

    /// <summary>All virtual IDs (including _NO variants) → display name.</summary>
    public Dictionary<string, string> TokenNames { get; init; } = new();
}

public class KalshiMarketScanner
{
    private readonly KalshiOrderClient _client;

    // Minimum 24h volume in contracts to include a market in binary scan.
    // Avoids subscribing to thousands of illiquid markets with stale books.
    private readonly decimal _minVolume24h;

    // Dynamic blocklist loaded from event_blocklist.json at construction time.
    // The Python analyzer writes series prefixes here after detecting all-NO
    // resolutions so new non-exhaustive market types are blocked automatically
    // without requiring a code change or rebuild.
    private readonly HashSet<string> _dynamicBlocklist;

    /// <summary>Default path the scanner looks for the auto-generated blocklist.</summary>
    public const string BlocklistPath = "event_blocklist.json";

    /// <summary>Backward-compat: all token names from the most recent scan.</summary>
    public Dictionary<string, string> TokenNames { get; private set; } = new();

    public KalshiMarketScanner(KalshiOrderClient client, decimal minVolume24h = 10m,
        string? blocklistPath = null)
    {
        _client = client;
        _minVolume24h = minVolume24h;
        _dynamicBlocklist = LoadBlocklist(blocklistPath ?? BlocklistPath);
    }

    /// <summary>
    /// Loads the JSON blocklist file produced by the Python analyzer.
    /// Returns an empty set (not an error) when the file does not yet exist.
    /// </summary>
    private static HashSet<string> LoadBlocklist(string path)
    {
        if (!File.Exists(path)) return new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        try
        {
            string json = File.ReadAllText(path);
            using var doc = System.Text.Json.JsonDocument.Parse(json);
            var root = doc.RootElement;
            // Accept either a JSON array of strings or {"blocked": [...]}
            var arr = root.ValueKind == System.Text.Json.JsonValueKind.Array
                ? root
                : root.GetProperty("blocked");
            var set = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
            foreach (var el in arr.EnumerateArray())
            {
                string? s = el.GetString();
                if (!string.IsNullOrWhiteSpace(s)) set.Add(s);
            }
            Console.WriteLine($"[KALSHI SCANNER] Dynamic blocklist loaded: {set.Count} series from {path}");
            return set;
        }
        catch (Exception ex)
        {
            Console.WriteLine($"[KALSHI SCANNER] Warning: could not parse {path}: {ex.Message}");
            return new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        }
    }

    // ──────────────────────────────────────────────────────────────────────────
    //  Main entry point
    // ──────────────────────────────────────────────────────────────────────────

    /// <summary>
    /// Scans all open Kalshi events and returns both categorical and binary arb candidates.
    /// </summary>
    public async Task<KalshiScanResult> ScanAllAsync()
    {
        Console.WriteLine("\n[KALSHI SCANNER] Fetching open events with nested markets...");
        List<JsonElement> events = await _client.GetOpenEventsWithMarketsAsync();
        Console.WriteLine($"[KALSHI SCANNER] {events.Count} events received.");

        var categorical = new Dictionary<string, List<string>>();
        var binary      = new Dictionary<string, List<string>>();
        var names       = new Dictionary<string, string>();

        int skippedScalar        = 0;
        int skippedNonExhaustive = 0;
        int catEligible          = 0;
        int binEligible          = 0;

        foreach (var ev in events)
        {
            string eventTicker = GetString(ev, "event_ticker");
            if (string.IsNullOrEmpty(eventTicker)) continue;

            // Skip event types that are structurally non-exhaustive: all legs can
            // simultaneously resolve NO when the underlying event falls outside every
            // defined bucket, making the apparent "arb" a zero-EV spread trade.
            //   VICROUND — UFC/boxing Victory Round: legs cover specific rounds only;
            //              if the fight goes to decision no leg pays $1.
            //   MLBHR    — MLB Home Run by inning: if no HR is hit the entire game
            //              every inning leg resolves NO.
            if (IsNonExhaustiveEvent(eventTicker)) { skippedNonExhaustive++; continue; }

            if (!ev.TryGetProperty("markets", out var marketsEl) ||
                marketsEl.ValueKind != JsonValueKind.Array)
                continue;

            var activeYesTickers  = new List<string>(); // legs for categorical
            decimal catMaxVol24h  = 0m;                 // highest leg volume — event is live if any leg traded
            decimal catRestAskSum = 0m;                 // sum of REST yes_ask prices across all active legs
            int     catPricedLegs = 0;                  // legs with a non-zero REST ask price
            // Count legs that use cumulative/spread language:
            //   0 → clean categorical
            //   1 → acceptable: a single "X or more" final catch-all bucket
            //   2+ → non-exclusive: multiple legs can resolve YES simultaneously
            int cumulativeLegCount = 0;
            // Count legs that describe "X happened in a specific period" (round/inning/set/period).
            // These are "WHEN did X happen" markets (e.g. "wins in Round 2", "HR in Inning 4").
            // If 2+ legs have this structure with no covering "it never happened" leg, every leg
            // can resolve NO simultaneously — the market is structurally non-exhaustive.
            int episodeLegCount = 0;
            int totalMarketsInEvent = 0;
            var skippedStatuses = new List<string>();

            foreach (var mkt in marketsEl.EnumerateArray())
            {
                totalMarketsInEvent++;
                // Skip non-active and scalar (e.g. temperature range) markets
                string mktStatus = GetString(mkt, "status");
                if (!string.Equals(mktStatus, "active", StringComparison.OrdinalIgnoreCase))
                {
                    skippedStatuses.Add(mktStatus);
                    continue;
                }

                string marketType = GetString(mkt, "market_type");
                if (string.Equals(marketType, "scalar", StringComparison.OrdinalIgnoreCase))
                {
                    skippedScalar++;
                    continue;
                }

                string ticker = GetString(mkt, "ticker");
                if (string.IsNullOrEmpty(ticker)) continue;

                // Build display names from yes_sub_title / no_sub_title / title
                string yesTitle = GetString(mkt, "yes_sub_title");
                string noTitle  = GetString(mkt, "no_sub_title");
                string baseTitle = GetString(mkt, "title");
                if (string.IsNullOrEmpty(baseTitle))
                    baseTitle = GetString(mkt, "question");

                string displayYes = string.IsNullOrEmpty(yesTitle) ? baseTitle + " [YES]" : yesTitle;
                string displayNo  = string.IsNullOrEmpty(noTitle)  ? baseTitle + " [NO]"  : noTitle;

                names[ticker]         = displayYes;
                names[ticker + "_NO"] = displayNo;

                // Count legs that use cumulative or half-line-spread language.
                // "wins by 2 or more" and "wins by 3 or more" can both resolve YES → non-exclusive.
                // "wins by 1.5" / "wins by 2.5" (.5 lines) are implicit cumulative thresholds —
                // multiple of these can resolve YES (or all resolve NO), so they're also non-exclusive.
                // One cumulative leg is fine: it's the final catch-all bucket in a proper N-way split.
                if (IsCumulativeTitle(yesTitle) || IsCumulativeTitle(noTitle) || IsCumulativeTitle(baseTitle)
                    || IsHalfLineSpread(yesTitle) || IsHalfLineSpread(noTitle) || IsHalfLineSpread(baseTitle))
                    cumulativeLegCount++;

                // Count legs that describe "X happened IN a specific period/round/inning".
                // Two or more such legs means this is a "WHEN did X occur?" market — inherently
                // non-exhaustive unless a catch-all "it never happened" leg exists.
                // We don't try to detect the catch-all; the safe default is to reject.
                if (IsEpisodeLegTitle(yesTitle) || IsEpisodeLegTitle(noTitle) || IsEpisodeLegTitle(baseTitle))
                    episodeLegCount++;

                decimal vol24h = ParseFp(mkt, "volume_24h_fp");
                catMaxVol24h = Math.Max(catMaxVol24h, vol24h);

                // Accumulate REST ask prices for mutual-exclusivity check.
                // For a true categorical market exactly one leg resolves YES,
                // so sum of YES asks ≈ $1.00. Independent markets (e.g. "will
                // player X hit a HR?") where multiple legs can resolve YES
                // simultaneously will have sum >> $1.00.
                // Use NO bid as the more reliable implied ask when available.
                decimal yesAsk = ParseDollars(mkt, "yes_ask_dollars");
                decimal noBid  = ParseDollars(mkt, "no_bid_dollars");
                decimal impliedAsk = noBid > 0.01m ? Math.Round(1m - noBid, 4) : yesAsk;
                if (impliedAsk > 0.01m && impliedAsk < 1.0m)
                {
                    catRestAskSum += impliedAsk;
                    catPricedLegs++;
                }

                // ── BINARY ARB candidate check ──────────────────────────────
                // Pre-screen using the snapshot ask prices from the REST response.
                // This is a hint only — WebSocket books are the ground truth.
                if (vol24h >= _minVolume24h)
                {
                    decimal noAsk = ParseDollars(mkt, "no_ask_dollars");

                    // Include if:
                    //   a) Sum is already < $0.995 (immediate arb candidate), OR
                    //   b) Both sides have reasonable prices (market is liquid)
                    bool hasAskPrices = yesAsk > 0.01m && noAsk > 0.01m;
                    bool preScreenArb = hasAskPrices && (yesAsk + noAsk < 1.05m); // generous threshold

                    if (preScreenArb)
                    {
                        string binEventId = "BIN_" + ticker;
                        binary[binEventId] = new List<string> { ticker, ticker + "_NO" };
                        binEligible++;
                    }
                }

                // Collect for categorical check
                activeYesTickers.Add(ticker);
            }

            // ── CATEGORICAL ARB candidate check ─────────────────────────────
            // Require at least one leg to have traded in the last 24h.
            // Events where every leg has volume=0 are resolved/stale — only
            // resting phantom orders remain, which look like arbs but aren't.
            int dropped = totalMarketsInEvent - activeYesTickers.Count;
            bool hasPartiallyResolved = skippedStatuses.Any(s =>
                string.Equals(s, "finalized", StringComparison.OrdinalIgnoreCase) ||
                string.Equals(s, "inactive", StringComparison.OrdinalIgnoreCase));

            // Mutual-exclusivity guard: for a true categorical market the sum of
            // YES asks across all legs must sit near $1.00. Skip events where:
            //   • sum > 1.50 → multiple legs can resolve YES simultaneously
            //                  (independent bets — "will player X hit a HR?")
            //   • sum < 0.82 → >18% probability unaccounted for; likely a hidden
            //                  outcome exists (e.g. "no HR hit", "fight to decision").
            //                  Floor raised from 0.40 — a true exhaustive categorical
            //                  should price close to $1.00 once markets are liquid.
            // Only apply when prices are available for all (or all-but-one) legs.
            bool pricesAvailable = catPricedLegs >= activeYesTickers.Count - 1 && catPricedLegs >= 2;
            bool sumIsReasonable = !pricesAvailable
                || (catRestAskSum >= 0.82m && catRestAskSum <= 1.50m);

            if (activeYesTickers.Count >= 3 && catMaxVol24h >= _minVolume24h
                && !hasPartiallyResolved && sumIsReasonable
                && cumulativeLegCount < 2 && episodeLegCount < 2)
            {
                categorical[eventTicker] = activeYesTickers;
                catEligible++;
            }
        }

        Console.WriteLine($"[KALSHI SCANNER] Categorical: {catEligible} events | " +
                          $"Binary: {binEligible} markets | " +
                          $"Skipped: {skippedScalar} scalar, " +
                          $"{skippedNonExhaustive} blocklisted (VICROUND/MLBHR/SPREAD/TOTAL), " +
                          $"filtered by price-sum/cumulative/episode guards.");

        TokenNames = names;

        return new KalshiScanResult
        {
            CategoricalEvents = categorical,
            BinaryMarkets     = binary,
            TokenNames        = names,
        };
    }

    /// <summary>
    /// Backward-compatible method. Returns only categorical events (3+ legs).
    /// Prefer ScanAllAsync() for new code.
    /// </summary>
    public async Task<Dictionary<string, List<string>>> GetArbitrageEventsAsync()
    {
        var result = await ScanAllAsync();
        return result.CategoricalEvents;
    }

    // ──────────────────────────────────────────────────────────────────────────
    //  Helpers
    // ──────────────────────────────────────────────────────────────────────────

    /// <summary>
    /// Returns true when the event ticker matches a known structurally non-exhaustive
    /// market type where all legs can simultaneously resolve NO.
    /// Checks both the hardcoded list and the dynamic blocklist loaded from
    /// event_blocklist.json (written by the Python analyzer after detecting losses).
    ///
    ///   VICROUND — UFC/boxing Victory Round: legs cover specific finish rounds;
    ///              a decision win leaves every leg at $0.
    ///   MLBHR    — MLB Home Run by inning: if no HR is hit every inning leg is $0.
    /// </summary>
    private bool IsNonExhaustiveEvent(string eventTicker)
    {
        string upper = eventTicker.ToUpperInvariant();
        // Hardcoded known-bad series prefixes
        if (upper.Contains("VICROUND") || upper.Contains("MLBHR"))
            return true;
        // Dynamic blocklist: series prefixes detected by the analyzer
        // (any prefix whose events have produced an all-NO resolution)
        foreach (string blocked in _dynamicBlocklist)
            if (eventTicker.StartsWith(blocked, StringComparison.OrdinalIgnoreCase))
                return true;
        return false;
    }

    /// <summary>
    /// Returns true if the title describes "X happened IN a specific period/round/inning/set".
    /// Two or more such legs in an event indicates a "WHEN did X occur?" structure that is
    /// non-exhaustive: if X never occurs (e.g. fight goes to decision, no HR is hit) every
    /// leg resolves NO simultaneously.  Examples that match:
    ///   "wins in Round 2"  •  "HR in Inning 4"  •  "scored in the 3rd period"  •  "Set 1"
    /// </summary>
    private static bool IsEpisodeLegTitle(string title)
    {
        if (string.IsNullOrEmpty(title)) return false;
        string lower = title.ToLowerInvariant();
        // "round N", "inning N", "period N", "set N", or ordinal variants ("2nd round", "3rd inning")
        return System.Text.RegularExpressions.Regex.IsMatch(lower,
            @"\b(round|inning|period|set)\s*\d+|\b\d+\s*(st|nd|rd|th)\s+(round|inning|period|set)\b");
    }

    /// <summary>
    /// Returns true if the title contains a decimal spread line (e.g. "1.5", "2.5").
    /// Half-goal/point lines are implicit cumulative thresholds — "wins by 1.5" means
    /// "wins by 2 or more" and multiple such legs in an event can all resolve YES (or
    /// all resolve NO if the margin falls outside every listed threshold).
    /// </summary>
    private static bool IsHalfLineSpread(string title)
    {
        if (string.IsNullOrEmpty(title)) return false;
        return System.Text.RegularExpressions.Regex.IsMatch(title, @"\b\d+\.5\b");
    }

    /// <summary>
    /// Returns true if the title contains cumulative/nested language that signals
    /// non-mutual-exclusivity.  "wins by 2 or more" and "wins by 3 or more" can
    /// both resolve YES in the same event — they are NOT exclusive outcomes.
    /// </summary>
    private static bool IsCumulativeTitle(string title)
    {
        if (string.IsNullOrEmpty(title)) return false;
        string lower = title.ToLowerInvariant();
        return lower.Contains("or more")
            || lower.Contains("or higher")
            || lower.Contains("or greater")
            || lower.Contains("at least")
            || lower.Contains("and above")
            || lower.Contains("or above");
    }

    private static string GetString(JsonElement el, string key)
        => el.TryGetProperty(key, out var v) ? v.GetString() ?? "" : "";

    /// <summary>Parse a FixedPointDollars string field (e.g. "0.5400") → decimal.</summary>
    private static decimal ParseDollars(JsonElement el, string key)
    {
        if (!el.TryGetProperty(key, out var v)) return 0m;
        string? s = v.ValueKind == JsonValueKind.String ? v.GetString() : v.ToString();
        return decimal.TryParse(s, System.Globalization.NumberStyles.Any,
            System.Globalization.CultureInfo.InvariantCulture, out decimal d) ? d : 0m;
    }

    /// <summary>Parse a FixedPointCount string field (e.g. "150.00") → decimal.</summary>
    private static decimal ParseFp(JsonElement el, string key)
        => ParseDollars(el, key); // same format — reuse
}
