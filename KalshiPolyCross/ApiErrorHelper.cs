using System.Net;

namespace KalshiPolyCross;

/// <summary>
/// Converts HTTP and network exceptions into actionable error messages,
/// with specific hints for Polymarket geo-blocks and Kalshi auth failures.
/// </summary>
internal static class ApiErrorHelper
{
    internal static string ClassifyPoly(Exception ex)
    {
        if (ex is HttpRequestException hre)
        {
            return hre.StatusCode switch
            {
                HttpStatusCode.Forbidden
                    => $"HTTP 403 — Polymarket geo-blocked or Cloudflare challenge; verify proxy tunnel is up",
                HttpStatusCode.TooManyRequests
                    => $"HTTP 429 — Polymarket rate-limited; backing off",
                HttpStatusCode.Unauthorized
                    => $"HTTP 401 — Polymarket auth failure; check POLY_API_KEY / POLY_API_SECRET",
                HttpStatusCode.ServiceUnavailable
                    => $"HTTP 503 — Polymarket temporarily unavailable",
                _ => $"HTTP {(int?)hre.StatusCode} — {ex.Message}"
            };
        }
        return ClassifyNetwork(ex, "Polymarket", "proxy tunnel");
    }

    internal static string ClassifyKalshi(Exception ex)
    {
        if (ex is HttpRequestException hre)
        {
            return hre.StatusCode switch
            {
                HttpStatusCode.Unauthorized
                    => $"HTTP 401 — Kalshi auth failure; check KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH",
                HttpStatusCode.Forbidden
                    => $"HTTP 403 — Kalshi access denied; key may lack trading permissions",
                HttpStatusCode.TooManyRequests
                    => $"HTTP 429 — Kalshi rate-limited; slow down",
                HttpStatusCode.ServiceUnavailable
                    => $"HTTP 503 — Kalshi temporarily unavailable",
                _ => $"HTTP {(int?)hre.StatusCode} — {ex.Message}"
            };
        }
        return ClassifyNetwork(ex, "Kalshi", "network");
    }

    private static string ClassifyNetwork(Exception ex, string platform, string hint)
    {
        string msg = ex.Message;
        if (msg.Contains("407"))
            return $"{platform} proxy auth failed (407) — check proxy credentials";
        if (msg.Contains("Connection refused") || msg.Contains("ECONNREFUSED") ||
            msg.Contains("No connection could be made"))
            return $"{platform} connection refused — is the {hint} running?";
        if (msg.Contains("No such host") || msg.Contains("NameResolutionFailure") ||
            msg.Contains("nodename nor servname"))
            return $"{platform} DNS failure — check {hint} / network config";
        if (msg.Contains("timed out") || msg.Contains("timeout", StringComparison.OrdinalIgnoreCase) ||
            msg.Contains("TimedOut"))
            return $"{platform} request timed out — {hint} may be slow or unreachable";
        if (msg.Contains("SSL") || msg.Contains("TLS") || msg.Contains("certificate"))
            return $"{platform} TLS/SSL error — {ex.Message}";
        return ex.Message;
    }
}
