using System.Diagnostics;
using System.Globalization;
using System.Text.Json;
using PredictionBacktester.Engine.LiveExecution;

namespace KalshiPolyCross;

/// <summary>
/// Fires an async REST check against Kalshi + Polymarket whenever a new arb window opens.
/// Calls UpdateRestVerification on the telemetry strategy with the confirmed price and delay.
/// </summary>
public class CrossArbRestVerifier
{
    private readonly KalshiOrderClient _kalshi;
    private readonly HttpClient _http;
    private readonly CrossPlatformArbTelemetryStrategy _telemetry;
    private readonly SemaphoreSlim _sem = new(2, 2); // max 2 concurrent REST checks

    private const string PolyBookUrl = "https://clob.polymarket.com/book?token_id=";

    public CrossArbRestVerifier(KalshiOrderClient kalshi, CrossPlatformArbTelemetryStrategy telemetry)
    {
        _kalshi   = kalshi;
        _telemetry = telemetry;
        _http     = new HttpClient { Timeout = TimeSpan.FromSeconds(5) };
    }

    /// <summary>Subscribe to telemetry.OnArbOpened and call this method.</summary>
    public void OnArbOpened(string pairId, decimal netCost, string arbType, decimal depth)
        => _ = Task.Run(() => VerifyAsync(pairId, arbType));

    private async Task VerifyAsync(string pairId, string arbType)
    {
        await _sem.WaitAsync();
        var sw = Stopwatch.StartNew();
        try
        {
            var pair = _telemetry.GetPair(pairId);
            if (pair == null) return;

            // Kalshi leg: YES ask = 1 - best NO bid; NO ask = 1 - best YES bid
            decimal kAsk = await GetKalshiAskAsync(pair.KalshiTicker, arbType);

            // Polymarket leg
            string polyToken = arbType == "K_YES_P_NO" ? pair.PolyNoTokenId : pair.PolyYesTokenId;
            decimal pAsk = await GetPolyAskAsync(polyToken);

            sw.Stop();
            bool confirmed = kAsk > 0m && pAsk > 0m && (kAsk + pAsk) < 1.00m;

            _telemetry.UpdateRestVerification(pairId, confirmed, kAsk, pAsk, sw.ElapsedMilliseconds);

            string verdict = confirmed ? "CONFIRMED" : (kAsk < 0m || pAsk < 0m ? "FETCH_FAIL" : "NO_ARB");
            Console.WriteLine($"[REST CHECK] {pair.Label} | {arbType} | " +
                              $"K={kAsk:0.0000} P={pAsk:0.0000} sum={(kAsk + pAsk):0.0000} | " +
                              $"{verdict} in {sw.ElapsedMilliseconds}ms");
        }
        catch (Exception ex)
        {
            Console.WriteLine($"[REST CHECK ERROR] {pairId}: {ex.Message}");
        }
        finally
        {
            _sem.Release();
        }
    }

    // Kalshi REST book: prices are in cents, stored as bid arrays.
    // YES ask  = 1 - best_no_bid   (K_YES_P_NO arb: we want to buy YES)
    // NO ask   = 1 - best_yes_bid  (K_NO_P_YES arb: we want to buy NO)
    private async Task<decimal> GetKalshiAskAsync(string ticker, string arbType)
    {
        using var doc = await _kalshi.GetMarketOrderBookAsync(ticker);
        var book = doc.RootElement.TryGetProperty("orderbook", out var ob) ? ob : doc.RootElement;

        // We look at the opposite side bids to derive the implied ask
        string bidSide = arbType == "K_YES_P_NO" ? "no" : "yes";
        if (!book.TryGetProperty(bidSide, out var arr)) return -1m;

        decimal bestBid = 0m;
        foreach (var lvl in arr.EnumerateArray())
        {
            var items = lvl.EnumerateArray().ToArray();
            if (items.Length < 2) continue;
            decimal priceCents = items[0].ValueKind == JsonValueKind.Number
                ? items[0].GetDecimal()
                : decimal.TryParse(items[0].GetString(), NumberStyles.Any,
                      CultureInfo.InvariantCulture, out decimal p) ? p : 0m;
            bestBid = Math.Max(bestBid, priceCents / 100m);
        }
        return bestBid > 0m ? Math.Round(1m - bestBid, 4) : -1m;
    }

    // Polymarket CLOB REST book: asks sorted ascending by price.
    private async Task<decimal> GetPolyAskAsync(string tokenId)
    {
        string json = await _http.GetStringAsync(PolyBookUrl + tokenId);
        using var doc = JsonDocument.Parse(json);
        if (!doc.RootElement.TryGetProperty("asks", out var asks)) return -1m;

        decimal bestAsk = decimal.MaxValue;
        foreach (var ask in asks.EnumerateArray())
        {
            if (ask.TryGetProperty("price", out var priceEl) &&
                decimal.TryParse(priceEl.GetString(), NumberStyles.Any,
                    CultureInfo.InvariantCulture, out decimal price))
                bestAsk = Math.Min(bestAsk, price);
        }
        return bestAsk < decimal.MaxValue ? bestAsk : -1m;
    }
}
