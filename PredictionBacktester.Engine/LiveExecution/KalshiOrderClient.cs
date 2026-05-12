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
        resp.EnsureSuccessStatusCode();

        var stream = await resp.Content.ReadAsStreamAsync();
        return await JsonDocument.ParseAsync(stream);
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
        var stream = await resp.Content.ReadAsStreamAsync();
        if (!resp.IsSuccessStatusCode)
        {
            using var sr = new StreamReader(stream);
            string err = await sr.ReadToEndAsync();
            throw new HttpRequestException(
                $"Kalshi POST {relPath} {(int)resp.StatusCode}: {err[..Math.Min(400, err.Length)]}");
        }
        return await JsonDocument.ParseAsync(stream);
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
    /// Returns empty list on any API failure.
    /// </summary>
    public async Task<List<(string Ticker, int Position)>> GetPositionsAsync()
    {
        try
        {
            using var doc = await GetAsync("/portfolio/positions");
            var result = new List<(string, int)>();
            if (!doc.RootElement.TryGetProperty("market_positions", out var arr)) return result;
            foreach (var el in arr.EnumerateArray())
            {
                string ticker = el.TryGetProperty("ticker",   out var t) ? t.GetString() ?? "" : "";
                int    pos    = el.TryGetProperty("position", out var p) ? p.GetInt32()         : 0;
                if (!string.IsNullOrEmpty(ticker) && pos != 0)
                    result.Add((ticker, pos));
            }
            return result;
        }
        catch (Exception ex)
        {
            Console.WriteLine($"[KALSHI] GetPositionsAsync failed: {ex.Message}");
            return new List<(string, int)>();
        }
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
        decimal fill    = order.TryGetProperty("fill_count_fp", out var fc) ? fc.GetDecimal()       : 0m;

        return (orderId, status, fill);
    }

    /// <summary>Polls GET /portfolio/orders/{orderId} once and returns (status, fill_count_fp).</summary>
    public async Task<(string Status, decimal FillCount)> PollOrderAsync(string orderId)
    {
        using var doc = await GetAsync($"/portfolio/orders/{orderId}");
        var order = doc.RootElement.TryGetProperty("order", out var o) ? o : doc.RootElement;

        string  status = order.TryGetProperty("status",        out var st) ? (st.GetString() ?? "") : "";
        decimal fill   = order.TryGetProperty("fill_count_fp", out var fc) ? fc.GetDecimal()       : 0m;

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
