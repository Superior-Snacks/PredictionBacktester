using System.Text.Json;
using PredictionBacktester.Engine.LiveExecution;

namespace PredictionBacktester.Engine;

/// <summary>
/// Scans the Kalshi API for arb-eligible events: groups of 3+ mutually-exclusive
/// binary markets where exactly one will resolve YES, so buying YES on all legs
/// guarantees a $1.00 payout if total cost &lt; $1.00.
/// </summary>
public class KalshiMarketScanner
{
    private readonly KalshiOrderClient _client;

    /// <summary>Market ticker → human-readable title, populated during scan.</summary>
    public Dictionary<string, string> TokenNames { get; } = new();

    // Sports categories have unpredictable matching delays — skip them for arb
    private static readonly HashSet<string> SportsKeywords = new(StringComparer.OrdinalIgnoreCase)
    {
        "soccer", "football", "basketball", "baseball", "hockey", "tennis",
        "mma", "ufc", "esports", "cricket", "rugby", "golf", "volleyball",
        "boxing", "cycling", "racing", "motorsport", "swimming", "athletics",
        "nba", "nfl", "nhl", "mlb", "ncaa", "epl", "champions-league",
        "la-liga", "bundesliga", "serie-a", "ligue-1", "sports"
    };

    public KalshiMarketScanner(KalshiOrderClient client)
    {
        _client = client;
    }

    /// <summary>
    /// Returns a map of eventId → ordered list of market tickers eligible for categorical arb.
    /// Only events with 3+ active open markets are included.
    /// </summary>
    public async Task<Dictionary<string, List<string>>> GetArbitrageEventsAsync()
    {
        Console.WriteLine("\n[KALSHI SCANNER] Fetching open events with nested markets...");

        List<JsonElement> events = await _client.GetOpenEventsWithMarketsAsync();
        Console.WriteLine($"[KALSHI SCANNER] Received {events.Count} events from API.");

        var arbConfig = new Dictionary<string, List<string>>();
        int skippedSports = 0;
        int skippedTooFewLegs = 0;
        int skippedNoMarkets = 0;

        foreach (var ev in events)
        {
            string eventTicker = ev.TryGetProperty("event_ticker", out var etEl) ? etEl.GetString() ?? "" : "";
            if (string.IsNullOrEmpty(eventTicker)) continue;

            // Check for sports tags
            if (IsSportsEvent(ev))
            {
                skippedSports++;
                continue;
            }

            // Extract nested markets
            if (!ev.TryGetProperty("markets", out var marketsEl) ||
                marketsEl.ValueKind != JsonValueKind.Array)
            {
                skippedNoMarkets++;
                continue;
            }

            var activeTickers = new List<string>();

            foreach (var mkt in marketsEl.EnumerateArray())
            {
                // Only include active (open) markets
                string status = mkt.TryGetProperty("status", out var sEl) ? sEl.GetString() ?? "" : "";
                if (!string.Equals(status, "active", StringComparison.OrdinalIgnoreCase))
                    continue;

                string ticker = mkt.TryGetProperty("ticker", out var tEl) ? tEl.GetString() ?? "" : "";
                if (string.IsNullOrEmpty(ticker)) continue;

                activeTickers.Add(ticker);

                // Populate display name
                string title = "";
                if (mkt.TryGetProperty("title", out var titleEl))
                    title = titleEl.GetString() ?? "";
                else if (mkt.TryGetProperty("question", out var qEl))
                    title = qEl.GetString() ?? "";
                TokenNames[ticker] = title;
            }

            if (activeTickers.Count < 3)
            {
                skippedTooFewLegs++;
                continue;
            }

            arbConfig[eventTicker] = activeTickers;
        }

        Console.WriteLine($"[KALSHI SCANNER] Eligible events: {arbConfig.Count} | " +
                          $"Skipped: {skippedSports} sports, {skippedTooFewLegs} <3 legs, {skippedNoMarkets} no markets.");

        return arbConfig;
    }

    private static bool IsSportsEvent(JsonElement ev)
    {
        // Check category field
        if (ev.TryGetProperty("category", out var catEl))
        {
            string? cat = catEl.GetString();
            if (cat != null && SportsKeywords.Any(k =>
                cat.Contains(k, StringComparison.OrdinalIgnoreCase)))
                return true;
        }

        // Check tags array (slug + label)
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
}
