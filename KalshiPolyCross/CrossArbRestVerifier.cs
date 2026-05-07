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
    {
        DebugLog.Trades($"CrossArbRestVerifier.OnArbOpened: {pairId} {arbType} — queuing REST check");
        _ = Task.Run(async () =>
        {
            try { await VerifyAsync(pairId, arbType); }
            catch (Exception ex)
            {
                Console.WriteLine($"[REST CHECK ERROR] {pairId}: {ApiErrorHelper.ClassifyPoly(ex)}");
                DebugLog.Trades($"VerifyAsync unhandled exception for {pairId}: {ex}");
            }
        });
    }

    private async Task VerifyAsync(string pairId, string arbType)
    {
        DebugLog.Trades($"VerifyAsync {pairId}: waiting for semaphore (current count unknown)");
        await _sem.WaitAsync();
        var sw = Stopwatch.StartNew();
        try
        {
            var pair = _telemetry.GetPair(pairId);
            if (pair == null)
            {
                DebugLog.Trades($"VerifyAsync {pairId}: pair not found in telemetry, skipping");
                return;
            }

            DebugLog.Trades($"VerifyAsync {pair.Label}: fetching Kalshi ask for {pair.KalshiTicker}");
            decimal kAsk = await GetKalshiAskAsync(pair.KalshiTicker, arbType);
            DebugLog.Trades($"VerifyAsync {pair.Label}: Kalshi ask={kAsk:0.0000}");

            string polyToken = arbType == "K_YES_P_NO" ? pair.PolyNoTokenId : pair.PolyYesTokenId;
            DebugLog.Trades($"VerifyAsync {pair.Label}: fetching Poly ask for token {polyToken[..Math.Min(8, polyToken.Length)]}...");
            decimal pAsk = await GetPolyAskAsync(polyToken);
            DebugLog.Trades($"VerifyAsync {pair.Label}: Poly ask={pAsk:0.0000}");

            sw.Stop();
            bool confirmed = kAsk > 0m && pAsk > 0m && (kAsk + pAsk) < 1.00m;
            DebugLog.Trades($"VerifyAsync {pair.Label}: sum={kAsk + pAsk:0.0000} confirmed={confirmed} in {sw.ElapsedMilliseconds}ms");

            _telemetry.UpdateRestVerification(pairId, confirmed, kAsk, pAsk, sw.ElapsedMilliseconds);

            if (!confirmed)
            {
                string verdict = kAsk < 0m || pAsk < 0m ? "FETCH_FAIL" : "NO_ARB";
                Console.WriteLine($"[REST CHECK] {pair.Label} | {arbType} | " +
                                  $"K={kAsk:0.0000} P={pAsk:0.0000} sum={(kAsk + pAsk):0.0000} | " +
                                  $"{verdict} in {sw.ElapsedMilliseconds}ms");
            }
        }
        catch (Exception ex)
        {
            Console.WriteLine($"[REST CHECK ERROR] {pairId}: {ApiErrorHelper.ClassifyPoly(ex)}");
            DebugLog.Trades($"VerifyAsync caught exception for {pairId}: {ex}");
        }
        finally
        {
            _sem.Release();
            DebugLog.Trades($"VerifyAsync {pairId}: semaphore released");
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
            {
                DebugLog.Trades($"GetKalshiAskAsync {ticker}: found {key}={p:0.0000}");
                return p;
            }
        }

        // Fallback: cents integer (e.g. yes_ask = 65 → 0.65)
        if (mkt.TryGetProperty(centsKey, out var centsEl) && centsEl.ValueKind == JsonValueKind.Number)
        {
            decimal cents = centsEl.GetDecimal();
            if (cents > 0m)
            {
                decimal result = Math.Round(cents / 100m, 4);
                DebugLog.Trades($"GetKalshiAskAsync {ticker}: fallback cents {centsKey}={cents} → {result:0.0000}");
                return result;
            }
        }

        DebugLog.Trades($"GetKalshiAskAsync {ticker}: no valid ask field found");
        return -1m;
    }

    // Polymarket CLOB REST book: asks sorted ascending by price.
    private async Task<decimal> GetPolyAskAsync(string tokenId)
    {
        string json = await _http.GetStringAsync(PolyBookUrl + tokenId);
        using var doc = JsonDocument.Parse(json);
        if (!doc.RootElement.TryGetProperty("asks", out var asks))
        {
            DebugLog.Trades($"GetPolyAskAsync {tokenId[..Math.Min(8, tokenId.Length)]}: no 'asks' field in response");
            return -1m;
        }

        decimal bestAsk = decimal.MaxValue;
        int count = 0;
        foreach (var ask in asks.EnumerateArray())
        {
            if (ask.TryGetProperty("price", out var priceEl) &&
                decimal.TryParse(priceEl.GetString(), NumberStyles.Any,
                    CultureInfo.InvariantCulture, out decimal price))
            { bestAsk = Math.Min(bestAsk, price); count++; }
        }

        if (bestAsk < decimal.MaxValue)
        {
            DebugLog.Trades($"GetPolyAskAsync {tokenId[..Math.Min(8, tokenId.Length)]}: bestAsk={bestAsk:0.0000} from {count} levels");
            return bestAsk;
        }

        DebugLog.Trades($"GetPolyAskAsync {tokenId[..Math.Min(8, tokenId.Length)]}: no parseable ask levels");
        return -1m;
    }
}
