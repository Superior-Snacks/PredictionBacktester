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

    // Sports categories have unpredictable matching delays — skip them
    private static readonly HashSet<string> SportsKeywords = new(StringComparer.OrdinalIgnoreCase)
    {
        "soccer", "football", "basketball", "baseball", "hockey", "tennis",
        "mma", "ufc", "esports", "cricket", "rugby", "golf", "volleyball",
        "boxing", "cycling", "racing", "motorsport", "swimming", "athletics",
        "nba", "nfl", "nhl", "mlb", "ncaa", "epl", "champions-league",
        "la-liga", "bundesliga", "serie-a", "ligue-1", "sports"
    };

    /// <summary>Backward-compat: all token names from the most recent scan.</summary>
    public Dictionary<string, string> TokenNames { get; private set; } = new();

    public KalshiMarketScanner(KalshiOrderClient client, decimal minVolume24h = 10m)
    {
        _client = client;
        _minVolume24h = minVolume24h;
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

        int skippedSports  = 0;
        int skippedScalar  = 0;
        int catEligible    = 0;
        int binEligible    = 0;

        foreach (var ev in events)
        {
            string eventTicker = GetString(ev, "event_ticker");
            if (string.IsNullOrEmpty(eventTicker)) continue;

            if (IsSportsEvent(ev)) { skippedSports++; continue; }

            if (!ev.TryGetProperty("markets", out var marketsEl) ||
                marketsEl.ValueKind != JsonValueKind.Array)
                continue;

            var activeYesTickers = new List<string>(); // legs for categorical

            foreach (var mkt in marketsEl.EnumerateArray())
            {
                // Skip non-active and scalar (e.g. temperature range) markets
                if (!string.Equals(GetString(mkt, "status"), "active", StringComparison.OrdinalIgnoreCase))
                    continue;

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

                // ── BINARY ARB candidate check ──────────────────────────────
                // Pre-screen using the snapshot ask prices from the REST response.
                // This is a hint only — WebSocket books are the ground truth.
                decimal vol24h = ParseFp(mkt, "volume_24h_fp");
                if (vol24h >= _minVolume24h)
                {
                    decimal yesAsk = ParseDollars(mkt, "yes_ask_dollars");
                    decimal noAsk  = ParseDollars(mkt, "no_ask_dollars");

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
            if (activeYesTickers.Count >= 3)
            {
                categorical[eventTicker] = activeYesTickers;
                catEligible++;
            }
        }

        Console.WriteLine($"[KALSHI SCANNER] Categorical: {catEligible} events | " +
                          $"Binary: {binEligible} markets | " +
                          $"Skipped: {skippedSports} sports, {skippedScalar} scalar.");

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

    private static bool IsSportsEvent(JsonElement ev)
    {
        string cat = GetString(ev, "category");
        if (!string.IsNullOrEmpty(cat) &&
            SportsKeywords.Any(k => cat.Contains(k, StringComparison.OrdinalIgnoreCase)))
            return true;

        if (ev.TryGetProperty("tags", out var tagsEl) && tagsEl.ValueKind == JsonValueKind.Array)
        {
            foreach (var tag in tagsEl.EnumerateArray())
            {
                string? slug  = tag.TryGetProperty("slug",  out var sEl) ? sEl.GetString() : null;
                string? label = tag.TryGetProperty("label", out var lEl) ? lEl.GetString() : null;

                if ((slug  != null && SportsKeywords.Any(k => slug.Contains(k,  StringComparison.OrdinalIgnoreCase))) ||
                    (label != null && SportsKeywords.Any(k => label.Contains(k, StringComparison.OrdinalIgnoreCase))))
                    return true;
            }
        }

        return false;
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
