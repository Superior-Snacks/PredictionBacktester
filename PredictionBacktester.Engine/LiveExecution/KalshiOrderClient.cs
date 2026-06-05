using System.Globalization;
using System.Net.Http.Headers;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;

namespace PredictionBacktester.Engine.LiveExecution;

/// <summary>
/// REST client for the Kalshi API. Handles RSA-PSS request signing, market queries,
/// and live IOC order placement / fill polling.
/// </summary>
public class KalshiOrderClient : IKalshiOrderExecutor, IDisposable
{
    private readonly KalshiApiConfig _config;
    private readonly RSA _rsa;
    private readonly HttpClient _http;

    // Full REST path prefix used when computing signatures
    // Signing requires the complete path from root: /trade-api/v2/...
    private const string PathPrefix = "/trade-api/v2";

    /// <summary>
    /// Optional hook invoked with (relPath, responseBody) for every REST response.
    /// Set this in callers that need raw-response logging (e.g. KalshiPolyCross --debug).
    /// </summary>
    public Action<string, string>? RawResponseLogger { get; set; }

    public KalshiOrderClient(KalshiApiConfig config)
    {
        _config = config;
        _rsa = LoadPrivateKey(config.PrivateKeyPath);
        _http = new HttpClient { BaseAddress = new Uri(config.BaseRestUrl) };
    }

    // ──────────────────────────────────────────────────────────────────────────
    //  Auth
    // ──────────────────────────────────────────────────────────────────────────

    /// <summary>
    /// Creates the three Kalshi auth headers for a request.
    /// Message signed: {timestampMs}{METHOD}{/trade-api/v2/path-without-query}
    /// </summary>
    public (string key, string timestamp, string signature) CreateAuthHeaders(string method, string relPath)
    {
        string ts = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds().ToString();

        // Strip query parameters before signing.
        // If relPath already contains the full path (e.g. WS upgrade), use it as-is.
        string fullPath = relPath.StartsWith("/trade-api/") ? relPath : PathPrefix + relPath;
        string pathForSig = fullPath.Split('?')[0];

        byte[] msgBytes = Encoding.UTF8.GetBytes(ts + method + pathForSig);
        byte[] sigBytes = _rsa.SignData(msgBytes, HashAlgorithmName.SHA256, RSASignaturePadding.Pss);
        string sig = Convert.ToBase64String(sigBytes);

        return (_config.ApiKeyId, ts, sig);
    }

    // ──────────────────────────────────────────────────────────────────────────
    //  HTTP helpers
    // ──────────────────────────────────────────────────────────────────────────

    private async Task<JsonDocument> GetAsync(string relPath)
    {
        var (key, ts, sig) = CreateAuthHeaders("GET", relPath);

        using var req = new HttpRequestMessage(HttpMethod.Get, _config.BaseRestUrl.TrimEnd('/') + relPath);
        req.Headers.Add("KALSHI-ACCESS-KEY", key);
        req.Headers.Add("KALSHI-ACCESS-TIMESTAMP", ts);
        req.Headers.Add("KALSHI-ACCESS-SIGNATURE", sig);
        req.Headers.Accept.Add(new MediaTypeWithQualityHeaderValue("application/json"));

        using var resp = await _http.SendAsync(req);
        string body = await resp.Content.ReadAsStringAsync();
        RawResponseLogger?.Invoke(relPath, body);
        resp.EnsureSuccessStatusCode();
        return JsonDocument.Parse(body);
    }

    private async Task<JsonDocument> PostAsync(string relPath, object body)
    {
        string json = JsonSerializer.Serialize(body);
        var (key, ts, sig) = CreateAuthHeaders("POST", relPath);

        using var req = new HttpRequestMessage(HttpMethod.Post, _config.BaseRestUrl.TrimEnd('/') + relPath);
        req.Headers.Add("KALSHI-ACCESS-KEY",       key);
        req.Headers.Add("KALSHI-ACCESS-TIMESTAMP", ts);
        req.Headers.Add("KALSHI-ACCESS-SIGNATURE", sig);
        req.Content = new StringContent(json, Encoding.UTF8, "application/json");

        using var resp = await _http.SendAsync(req);
        string respBody = await resp.Content.ReadAsStringAsync();
        RawResponseLogger?.Invoke(relPath, respBody);
        if (!resp.IsSuccessStatusCode)
            throw new HttpRequestException(
                $"Kalshi POST {relPath} {(int)resp.StatusCode}: {respBody[..Math.Min(400, respBody.Length)]}");
        return JsonDocument.Parse(respBody);
    }

    // ──────────────────────────────────────────────────────────────────────────
    //  Public API methods
    // ──────────────────────────────────────────────────────────────────────────

    /// <summary>Returns the event JSON including nested markets.</summary>
    public async Task<JsonDocument> GetEventAsync(string eventTicker)
        => await GetAsync($"/events/{eventTicker}?with_nested_markets=true");

    /// <summary>Returns market metadata including top-of-book convenience price fields.</summary>
    public async Task<JsonDocument> GetMarketAsync(string ticker)
        => await GetAsync($"/markets/{ticker}");

    /// <summary>Returns the order book for a single market ticker.</summary>
    public async Task<JsonDocument> GetMarketOrderBookAsync(string ticker)
        => await GetAsync($"/markets/{ticker}/orderbook");

    /// <summary>Returns available balance in cents (integer).</summary>
    public async Task<long> GetBalanceCentsAsync()
    {
        using var doc = await GetAsync("/portfolio/balance");
        return doc.RootElement.GetProperty("balance").GetInt64();
    }

    /// <summary>
    /// Returns all open market positions. Positive position = net YES, negative = net NO.
    /// Paginates automatically. Throws on HTTP/parse errors — callers must distinguish
    /// failure from a genuinely empty account (empty list = no positions, exception = bad read).
    /// </summary>
    public async Task<List<(string Ticker, int Position)>> GetPositionsAsync()
    {
        var result = new List<(string, int)>();
        string cursor = "";
        while (true)
        {
            string path = cursor == ""
                ? "/portfolio/positions?limit=200"
                : $"/portfolio/positions?limit=200&cursor={Uri.EscapeDataString(cursor)}";

            using var doc = await GetAsync(path);   // throws on HTTP error — callers distinguish failure from empty
            var root = doc.RootElement;

            if (root.TryGetProperty("market_positions", out var arr) && arr.ValueKind == JsonValueKind.Array)
            {
                foreach (var el in arr.EnumerateArray())
                {
                    string ticker = el.TryGetProperty("ticker", out var t) ? t.GetString() ?? "" : "";
                    int pos = ReadIntFlexible(el, "position");
                    if (!string.IsNullOrEmpty(ticker) && pos != 0) result.Add((ticker, pos));
                }
            }

            if (root.TryGetProperty("cursor", out var cEl) && cEl.ValueKind == JsonValueKind.String &&
                !string.IsNullOrEmpty(cEl.GetString()))
                cursor = cEl.GetString()!;
            else break;

            await Task.Delay(200);
        }
        return result;
    }

    private static int ReadIntFlexible(JsonElement el, string prop)
    {
        if (!el.TryGetProperty(prop, out var v)) return 0;
        return v.ValueKind switch
        {
            JsonValueKind.Number => v.TryGetInt32(out var i) ? i : (int)Math.Round(v.GetDouble()),
            JsonValueKind.String =>
                int.TryParse(v.GetString(), NumberStyles.Any, CultureInfo.InvariantCulture, out var s) ? s :
                decimal.TryParse(v.GetString(), NumberStyles.Any, CultureInfo.InvariantCulture, out var d) ? (int)Math.Round(d) : 0,
            _ => 0
        };
    }

    /// <summary>
    /// Places an IOC order on Kalshi. Returns (orderId, status, fill_count_fp).
    /// side = "yes" | "no", action = "buy" | "sell".
    /// priceCents = price in cents (e.g. 65 for $0.65).
    /// clientOrderId tags the order for idempotency / self-trade prevention.
    /// </summary>
    public async Task<(string OrderId, string Status, decimal FillCount)> PlaceOrderAsync(
        string ticker, string side, int priceCents, int count,
        string action = "buy", string? clientOrderId = null)
    {
        string priceField = side == "yes" ? "yes_price" : "no_price";
        var body = new Dictionary<string, object>
        {
            ["ticker"]        = ticker,
            ["side"]          = side,
            ["action"]        = action,
            ["count"]         = count,
            [priceField]      = priceCents,
            ["time_in_force"] = "immediate_or_cancel",
        };
        if (!string.IsNullOrEmpty(clientOrderId))
            body["client_order_id"] = clientOrderId;

        using var doc = await PostAsync("/portfolio/orders", body);
        var order = doc.RootElement.TryGetProperty("order", out var o) ? o : doc.RootElement;

        string  orderId = order.TryGetProperty("order_id",      out var id) ? (id.GetString() ?? "") : "";
        string  status  = order.TryGetProperty("status",        out var st) ? (st.GetString() ?? "") : "";
        decimal fill    = order.TryGetProperty("fill_count_fp", out var fc)
            ? decimal.Parse(fc.GetString() ?? "0", CultureInfo.InvariantCulture) : 0m;

        return (orderId, status, fill);
    }

    /// <summary>Polls GET /portfolio/orders/{orderId} once and returns (status, fill_count_fp).</summary>
    public async Task<(string Status, decimal FillCount)> PollOrderAsync(string orderId)
    {
        using var doc = await GetAsync($"/portfolio/orders/{orderId}");
        var order = doc.RootElement.TryGetProperty("order", out var o) ? o : doc.RootElement;

        string  status = order.TryGetProperty("status",        out var st) ? (st.GetString() ?? "") : "";
        decimal fill   = order.TryGetProperty("fill_count_fp", out var fc)
            ? decimal.Parse(fc.GetString() ?? "0", CultureInfo.InvariantCulture) : 0m;

        return (status, fill);
    }

    /// <summary>
    /// Returns open events with their nested markets in one call.
    /// Paginates automatically until all events are fetched.
    /// </summary>
    public async Task<List<JsonElement>> GetOpenEventsWithMarketsAsync()
    {
        var results = new List<JsonElement>();
        string cursor = "";

        while (true)
        {
            string path = cursor == ""
                ? "/events?status=open&limit=200&with_nested_markets=true"
                : $"/events?status=open&limit=200&with_nested_markets=true&cursor={Uri.EscapeDataString(cursor)}";

            using var doc = await GetAsync(path);
            var root = doc.RootElement;

            if (root.TryGetProperty("events", out var eventsEl))
            {
                foreach (var ev in eventsEl.EnumerateArray())
                    results.Add(ev.Clone());
            }

            // Check for next page
            if (root.TryGetProperty("cursor", out var cursorEl) &&
                cursorEl.ValueKind == JsonValueKind.String &&
                !string.IsNullOrEmpty(cursorEl.GetString()))
            {
                cursor = cursorEl.GetString()!;
            }
            else
            {
                break;
            }

            await Task.Delay(200); // Polite rate limiting between pages
        }

        return results;
    }

    // ──────────────────────────────────────────────────────────────────────────
    //  Private helpers
    // ──────────────────────────────────────────────────────────────────────────

    private static RSA LoadPrivateKey(string keyPath)
    {
        if (string.IsNullOrEmpty(keyPath))
            throw new InvalidOperationException("KALSHI_PRIVATE_KEY_PATH is not set.");

        string pem = File.ReadAllText(keyPath);
        var rsa = RSA.Create();
        rsa.ImportFromPem(pem);
        return rsa;
    }

    public void Dispose()
    {
        _rsa.Dispose();
        _http.Dispose();
    }
}
