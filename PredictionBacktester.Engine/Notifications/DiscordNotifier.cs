using System.Net.Http;
using System.Text;
using System.Text.Json;

namespace HardVenArb;

/// <summary>
/// Fire-and-forget Discord webhook alerter. No-ops when no URL is configured. Every send is
/// best-effort — exceptions are swallowed (and logged to console) so alerting can never disrupt
/// trading, the same principle as the order-retry hooks. Callers use fire-and-forget
/// (<c>_ = AlertAsync(...)</c>); a single shared instance is safe for concurrent use.
/// </summary>
public sealed class DiscordNotifier
{
    private readonly string? _url;     // null when disabled (no webhook configured)
    private readonly string  _prefix;  // identifies this bot when several share one webhook channel
    private readonly HttpClient _http;

    /// <summary>True when a webhook URL is configured and alerts will actually be sent.</summary>
    public bool Enabled => _url is not null;

    public DiscordNotifier(string? webhookUrl, string botName = "HardVenArb")
    {
        _url    = string.IsNullOrWhiteSpace(webhookUrl) ? null : webhookUrl.Trim();
        _prefix = $"**[{botName}]** ";
        _http   = new HttpClient { Timeout = TimeSpan.FromSeconds(10) };
    }

    /// <summary>Post a single message to the webhook. Best-effort: never throws.</summary>
    public async Task AlertAsync(string message)
    {
        if (_url is null) return;
        try
        {
            string content = _prefix + message;
            if (content.Length > 1900) content = content[..1900] + "…";   // Discord hard-caps content at 2000
            string payload = JsonSerializer.Serialize(new { content });
            using var body = new StringContent(payload, Encoding.UTF8, "application/json");
            using var resp = await _http.PostAsync(_url, body);
            // Best-effort: a non-2xx (e.g. 429 rate-limit) is dropped, not retried — alerts must not back up.
        }
        catch (Exception ex)
        {
            Console.WriteLine($"[DISCORD] alert failed: {ex.Message}");
        }
    }
}
