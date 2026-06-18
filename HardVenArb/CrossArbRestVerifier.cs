using System.Collections.Concurrent;
using System.Diagnostics;
using System.Globalization;
using System.Text.Json;
using PredictionBacktester.Engine.LiveExecution;

namespace HardVenArb;

/// <summary>
/// Fires an async REST check against Kalshi + HardVen whenever a new arb window opens.
/// Calls UpdateRestVerification on the telemetry strategy with the confirmed price and delay.
/// </summary>
public class CrossArbRestVerifier
{
    private readonly KalshiOrderClient _kalshi;
    private readonly HttpClient _http;
    private readonly CrossPlatformArbTelemetryStrategy _telemetry;
    private readonly SemaphoreSlim _sem = new(2, 2); // max 2 concurrent REST checks

    /// <summary>Tick size per HardVen token ID, populated lazily from /book REST responses.</summary>
    public ConcurrentDictionary<string, string> HardVenTickSizes { get; } = new();

    // HardVen odds come from the local sidecar (the only source) — the verifier re-reads /odds to confirm
    // a window at arb-open. (Replaces a dead clob.hardven.com host left over from the Poly→HardVen rename.)
    private readonly string _sidecarBase;

    public CrossArbRestVerifier(KalshiOrderClient kalshi, CrossPlatformArbTelemetryStrategy telemetry,
                                string? socksProxy = null, string? sidecarBase = null)
    {
        _kalshi    = kalshi;
        _telemetry = telemetry;
        _sidecarBase = (sidecarBase ?? "http://127.0.0.1:8787").TrimEnd('/');
        if (!string.IsNullOrEmpty(socksProxy))
        {
            var handler = new HttpClientHandler
            {
                Proxy    = new System.Net.WebProxy(socksProxy),
                UseProxy = true
            };
            _http = new HttpClient(handler) { Timeout = TimeSpan.FromSeconds(5) };
        }
        else
        {
            _http = new HttpClient { Timeout = TimeSpan.FromSeconds(5) };
        }
    }

    /// <summary>Subscribe to telemetry.OnArbOpened and call this method.</summary>
    public void OnArbOpened(string pairId, decimal netCost, string arbType, decimal depth, decimal kLegAsk, decimal pLegAsk)
    {
        DebugLog.Trades($"CrossArbRestVerifier.OnArbOpened: {pairId} {arbType} — queuing REST check");
        _ = Task.Run(async () =>
        {
            try { await VerifyAsync(pairId, arbType); }
            catch (Exception ex)
            {
                Console.WriteLine($"[REST CHECK ERROR] {pairId}: {ApiErrorHelper.ClassifyHardVen(ex)}");
                DebugLog.Trades($"VerifyAsync unhandled exception for {pairId}: {ex}");
            }
        });
    }

    /// <summary>
    /// Fetches live ask prices for both legs directly. Used by the executor as a
    /// stale-book gate before firing orders when venue time-skew is large.
    /// Returns (-1,-1) if either fetch fails.
    /// </summary>
    public async Task<(decimal KAsk, decimal PAsk)> GetCurrentAsksAsync(CrossPair pair, string arbType)
    {
        string hardvenToken = arbType == "K_YES_P_NO" ? pair.HardVenNoTokenId : pair.HardVenYesTokenId;
        decimal kAsk = await GetKalshiAskAsync(pair.KalshiTicker, arbType);
        decimal pAsk = await GetHardVenAskAsync(hardvenToken);
        return (kAsk, pAsk);
    }

    /// <summary>
    /// Fetches live bid prices for both held legs. Used by early-exit monitoring when
    /// WS books are stale. Returns (-1,-1) if either fetch fails.
    /// K_YES_P_NO: we hold K YES + P NO → fetch yes_bid on Kalshi, bids on HardVen NO token.
    /// K_NO_P_YES: we hold K NO  + P YES → fetch no_bid  on Kalshi, bids on HardVen YES token.
    /// </summary>
    public async Task<(decimal KBid, decimal PBid)> GetCurrentBidsAsync(CrossPair pair, string arbType)
    {
        string hardvenToken = arbType == "K_YES_P_NO" ? pair.HardVenNoTokenId : pair.HardVenYesTokenId;
        decimal kBid = await GetKalshiBidAsync(pair.KalshiTicker, arbType);
        decimal pBid = await GetHardVenBidAsync(hardvenToken);
        return (kBid, pBid);
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

            string hardvenToken = arbType == "K_YES_P_NO" ? pair.HardVenNoTokenId : pair.HardVenYesTokenId;
            DebugLog.Trades($"VerifyAsync {pair.Label}: fetching HardVen ask for token {hardvenToken[..Math.Min(8, hardvenToken.Length)]}...");
            decimal pAsk = await GetHardVenAskAsync(hardvenToken);
            DebugLog.Trades($"VerifyAsync {pair.Label}: HardVen ask={pAsk:0.0000}");

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
            Console.WriteLine($"[REST CHECK ERROR] {pairId}: {ApiErrorHelper.ClassifyHardVen(ex)}");
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

    /// <summary>
    /// Re-reads one HardVen selection from the sidecar /odds → (implied_price, status).
    /// implied_price is the per-contract cost (= 1/decimal_odds = the "ask"); status is "open"/"suspended".
    /// (-1, "") on any error or missing selection.
    /// </summary>
    private async Task<(decimal price, string status)> GetHardVenSelectionAsync(string tokenId)
    {
        string url = $"{_sidecarBase}/odds?selections={Uri.EscapeDataString(tokenId)}";
        string json = await _http.GetStringAsync(url);
        using var doc = JsonDocument.Parse(json);
        if (!doc.RootElement.TryGetProperty("selections", out var sels) ||
            !sels.TryGetProperty(tokenId, out var sel))
            return (-1m, "");
        string status = sel.TryGetProperty("status", out var st) ? (st.GetString() ?? "") : "";
        decimal price = sel.TryGetProperty("implied_price", out var ip) && ip.TryGetDecimal(out var p) ? p : -1m;
        return (price, status);
    }

    /// <summary>
    /// Whether a HardVen token is currently tradeable (sidecar reports status "open"). False on error.
    /// </summary>
    public async Task<bool> CheckHardVenTokenAsync(string tokenId)
    {
        try { return (await GetHardVenSelectionAsync(tokenId)).status == "open"; }
        catch { return false; }
    }

    // Same structure as GetKalshiAskAsync but reads bid fields.
    // K_YES_P_NO: hold YES → sell YES → yes_bid; K_NO_P_YES: hold NO → sell NO → no_bid.
    private async Task<decimal> GetKalshiBidAsync(string ticker, string arbType)
    {
        using var doc = await _kalshi.GetMarketAsync(ticker);
        var mkt = doc.RootElement.TryGetProperty("market", out var m) ? m : doc.RootElement;

        bool sellYes = arbType == "K_YES_P_NO";
        string[] dollarKeys = sellYes
            ? ["yes_bid_dollars", "yes_bid_price"]
            : ["no_bid_dollars",  "no_bid_price"];
        string centsKey = sellYes ? "yes_bid" : "no_bid";

        foreach (var key in dollarKeys)
        {
            if (!mkt.TryGetProperty(key, out var el)) continue;
            string? s = el.ValueKind == JsonValueKind.String ? el.GetString() : el.GetRawText();
            if (decimal.TryParse(s, NumberStyles.Any, CultureInfo.InvariantCulture, out decimal p) && p > 0m)
            {
                DebugLog.Trades($"GetKalshiBidAsync {ticker}: found {key}={p:0.0000}");
                return p;
            }
        }

        if (mkt.TryGetProperty(centsKey, out var centsEl) && centsEl.ValueKind == JsonValueKind.Number)
        {
            decimal cents = centsEl.GetDecimal();
            if (cents > 0m)
            {
                decimal result = Math.Round(cents / 100m, 4);
                DebugLog.Trades($"GetKalshiBidAsync {ticker}: fallback cents {centsKey}={cents} → {result:0.0000}");
                return result;
            }
        }

        DebugLog.Trades($"GetKalshiBidAsync {ticker}: no valid bid field found");
        return -1m;
    }

    // HardVen is a BACK-ONLY sportsbook — there is no lay/sell side, so no bid to confirm. Return -1
    // ("no bid"); the executor treats a HardVen leg as non-reversible (it can't sell into a bid).
    private Task<decimal> GetHardVenBidAsync(string tokenId) => Task.FromResult(-1m);

    // HardVen "ask" = the sidecar's per-contract implied price (1/decimal_odds) when the market is open.
    private async Task<decimal> GetHardVenAskAsync(string tokenId)
    {
        var (price, status) = await GetHardVenSelectionAsync(tokenId);
        if (status == "open" && price > 0m)
        {
            DebugLog.Trades($"GetHardVenAskAsync {tokenId}: ask={price:0.0000} (sidecar)");
            return price;
        }
        DebugLog.Trades($"GetHardVenAskAsync {tokenId}: not open / no price (status={status})");
        return -1m;
    }
}
