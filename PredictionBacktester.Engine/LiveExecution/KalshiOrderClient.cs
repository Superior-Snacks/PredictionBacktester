using System.Net.Http.Headers;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;

namespace PredictionBacktester.Engine.LiveExecution;

/// <summary>
/// Thin REST client for the Kalshi API.
/// Handles RSA-PSS request signing and provides methods used by the scanner and paper trader.
/// Does NOT place real orders — that is reserved for a future KalshiLiveBroker.
/// </summary>
public class KalshiOrderClient : IDisposable
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

    // ──────────────────────────────────────────────────────────────────────────
    //  Public API methods
    // ──────────────────────────────────────────────────────────────────────────

    /// <summary>Returns available balance in cents (integer).</summary>
    public async Task<long> GetBalanceCentsAsync()
    {
        using var doc = await GetAsync("/portfolio/balance");
        return doc.RootElement.GetProperty("balance").GetInt64();
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
