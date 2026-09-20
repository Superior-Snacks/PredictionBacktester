using System.Globalization;
using System.Text.RegularExpressions;

namespace KalshiEvBot;

/// <summary>
/// One row per exception the bot swallows on a venue-facing path, with what the venue actually said.
///
/// <para><b>Why this file exists.</b> On 2026-09-17 every order between 07:02 and 08:54 failed — 66 in a row —
/// and the only record was <c>Status=error:HttpRequestException</c> in the order log. The full message
/// (<c>Kalshi POST /portfolio/orders 5xx: {...}</c>) had gone to the console and was lost. The cause turned
/// out to be Kalshi's documented weekly maintenance (Thursdays 03:00–05:00 ET = 07:00–09:00 local), which
/// could have been read off the response body in seconds. So: the type, the HTTP status, the venue's own
/// error code, and the first 400 characters of the message, written to disk at the moment it happens.</para>
/// </summary>
public sealed class ErrorLog : IDisposable
{
    public static readonly string[] Columns =
    {
        "At", "Where", "Ticker", "Side", "Kind", "HttpStatus", "VenueCode", "Message",
    };

    private readonly RollingCsv _csv;

    public ErrorLog(string? directory = null, string prefix = "EvErrors")
        => _csv = new RollingCsv(directory ?? Directory.GetCurrentDirectory(), prefix, Columns);

    public string Path => _csv.Path;
    public long RowsWritten => _csv.RowsWritten;

    /// <summary>The order log's Status string for a failed attempt: <c>error:503 exchange_maintenance</c>
    /// rather than <c>error:HttpRequestException</c> whenever the venue answered — so section 7 groups
    /// failures by what the venue said, and a closed exchange reads as one.</summary>
    public static string Tag(Exception ex)
    {
        if (ex is HttpRequestException h && h.StatusCode is { } sc)
        {
            string code = VenueCode(ex.Message);
            return $"error:{(int)sc}" + (code.Length > 0 ? " " + code : "");
        }
        return "error:" + ex.GetType().Name;
    }

    /// <summary>Kalshi's <c>"code"</c> field from an error body embedded in the message, if any. Kept to a
    /// short token so it is safe as a CSV cell and a grouping key.</summary>
    public static string VenueCode(string message)
    {
        var m = Regex.Match(message ?? "", "\"code\"\\s*:\\s*\"([A-Za-z0-9_.-]{1,48})\"");
        return m.Success ? m.Groups[1].Value : "";
    }

    public void Write(string where, string ticker, string side, Exception ex)
    {
        int status = ex is HttpRequestException h && h.StatusCode is { } sc ? (int)sc : 0;
        string msg = (ex.Message ?? "").Replace("\r", " ").Replace("\n", " ");
        if (msg.Length > 400) msg = msg[..400];
        _csv.WriteRow(new[]
        {
            DateTime.UtcNow.ToString("o", CultureInfo.InvariantCulture), RollingCsv.Q(where), RollingCsv.Q(ticker),
            RollingCsv.Q(side), RollingCsv.Q(ex.GetType().Name), status > 0 ? status.ToString(CultureInfo.InvariantCulture) : "",
            RollingCsv.Q(VenueCode(ex.Message ?? "")), RollingCsv.Q(msg),
        });
    }

    public void Dispose() => _csv.Dispose();
}
