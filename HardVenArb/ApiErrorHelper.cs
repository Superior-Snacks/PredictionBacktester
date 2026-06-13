using System.Net;

namespace HardVenArb;

/// <summary>
/// Converts HTTP and network exceptions into actionable error messages,
/// with specific hints for HardVen geo-blocks and Kalshi auth failures.
/// </summary>
internal static class ApiErrorHelper
{
    internal static string ClassifyHardVen(Exception ex)
    {
        if (ex is HttpRequestException hre)
        {
            return hre.StatusCode switch
            {
                HttpStatusCode.Forbidden
                    => $"HTTP 403 — HardVen geo-blocked or Cloudflare challenge; verify proxy tunnel is up",
                HttpStatusCode.TooManyRequests
                    => $"HTTP 429 — HardVen rate-limited; backing off",
                HttpStatusCode.Unauthorized
                    => $"HTTP 401 — HardVen auth failure; check HARDVEN_API_KEY / HARDVEN_API_SECRET",
                HttpStatusCode.ServiceUnavailable
                    => $"HTTP 503 — HardVen temporarily unavailable",
                _ => $"HTTP {(int?)hre.StatusCode} — {ex.Message}"
            };
        }
        return ClassifyNetwork(ex, "HardVen", "proxy tunnel");
    }

    /// <summary>
    /// True when a HardVen order error is an <em>order-level rejection</em> (the venue is up and
    /// simply refused this order — FAK found no match, bad params, insufficient balance, market closed)
    /// rather than a venue-health failure (5xx, timeout, connection refused, 429). Order rejections must
    /// NOT count toward the venue-maintenance/outage tripwire: a fast market that FAK-kills several
    /// orders in a row is normal trading, not HardVen going down. Keys on the HTTP status only —
    /// a 400/404 from the order endpoint is always an order rejection, never an outage; infra failures
    /// surface as 5xx / connection / timeout, which return false here and still count.
    /// </summary>
    internal static bool IsHardVenOrderRejection(Exception ex)
    {
        if (ex is HttpRequestException hre)
            return hre.StatusCode is HttpStatusCode.BadRequest or HttpStatusCode.NotFound;

        // HardVenOrderClient throws a plain Exception embedding the RestSharp status token, e.g.
        // "[HardVen API Error] BadRequest: {\"error\":\"no orders found to match with FAK order...\"}".
        // Match the embedded status (BadRequest/NotFound) — distinctive tokens that never appear in the
        // 5xx names (InternalServerError/ServiceUnavailable/BadGateway/GatewayTimeout).
        string m = ex.Message;
        if (m.Contains("[HardVen API Error]", StringComparison.Ordinal))
            return m.Contains("BadRequest", StringComparison.Ordinal)
                || m.Contains("NotFound", StringComparison.Ordinal);
        return false;
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
