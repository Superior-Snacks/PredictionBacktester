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

    // Uses /markets/{ticker} convenience price fields (yes_ask_dollars / no_ask_dollars).
    // K_YES_P_NO: we buy YES on Kalshi → want yes_ask
    // K_NO_P_YES: we buy NO  on Kalshi → want no_ask
    private async Task<decimal> GetKalshiAskAsync(string ticker, string arbType)
    {
        using var doc = await _kalshi.GetMarketAsync(ticker);
        var mkt = doc.RootElement.TryGetProperty("market", out var m) ? m : doc.RootElement;

        // Dollar-string fields are preferred; fall back to cents-integer fields
        bool buyYes = arbType == "K_YES_P_NO";
        string[] dollarKeys = buyYes
            ? ["yes_ask_dollars", "yes_ask_price"]
            : ["no_ask_dollars",  "no_ask_price"];
        string centsKey = buyYes ? "yes_ask" : "no_ask";

        foreach (var key in dollarKeys)
        {
            if (!mkt.TryGetProperty(key, out var el)) continue;
            string? s = el.ValueKind == JsonValueKind.String ? el.GetString() : el.GetRawText();
            if (decimal.TryParse(s, NumberStyles.Any, CultureInfo.InvariantCulture, out decimal p) && p > 0m)
                return p;
        }

        // Fallback: cents integer (e.g. yes_ask = 65 → 0.65)
        if (mkt.TryGetProperty(centsKey, out var centsEl) && centsEl.ValueKind == JsonValueKind.Number)
        {
            decimal cents = centsEl.GetDecimal();
            if (cents > 0m) return Math.Round(cents / 100m, 4);
        }

        return -1m;
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
