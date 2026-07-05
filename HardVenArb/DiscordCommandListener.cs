using System.Net.Http;
using System.Net.Http.Headers;
using System.Text.Json;

namespace HardVenArb;

/// <summary>
/// Polls a Discord channel for operator COMMANDS (<c>status</c> / <c>close</c> / <c>end</c>) via a bot token, so
/// the unattended bot can be queried and stopped remotely from the same #alerts channel it posts to. Send-only
/// webhooks can't READ messages, hence the separate bot token + channel id.
///
/// Robustness (this runs inside a multi-day unattended bot): every failure is swallowed and logged — a Discord
/// hiccup, a bad token, or a rate-limit never disrupts trading/telemetry. No-op unless BOTH a bot token and a
/// channel id are configured. Only reacts to HUMAN messages posted AFTER startup (baseline = newest id at start),
/// and ignores bot/webhook authors (so it never reacts to its own posts). Requires the bot to have the channel's
/// View Channel + Read Message History and the MESSAGE CONTENT INTENT (else message text comes back empty).
/// </summary>
public sealed class DiscordCommandListener
{
    private readonly string? _token;
    private readonly string? _channelId;
    private readonly HttpClient _http;
    private readonly Func<string, Task> _reply;       // post a reply to the channel (reuses the webhook)
    private readonly Func<Task<string>> _onStatus;    // build the 'status' text
    private readonly Func<Task> _onShutdown;          // graceful stop (write sentinel + cancel)
    private readonly int _pollSec;
    private string _lastId = "";
    private bool _warnedAuth;

    public bool Enabled => !string.IsNullOrWhiteSpace(_token) && !string.IsNullOrWhiteSpace(_channelId);

    public DiscordCommandListener(string? botToken, string? channelId, Func<string, Task> reply,
                                  Func<Task<string>> onStatus, Func<Task> onShutdown, int pollSec = 10)
    {
        _token     = string.IsNullOrWhiteSpace(botToken)  ? null : botToken.Trim();
        _channelId = string.IsNullOrWhiteSpace(channelId) ? null : channelId.Trim();
        _reply     = reply;
        _onStatus  = onStatus;
        _onShutdown = onShutdown;
        _pollSec   = pollSec > 0 ? pollSec : 10;
        _http      = new HttpClient { Timeout = TimeSpan.FromSeconds(15) };
        if (_token != null)
            _http.DefaultRequestHeaders.Authorization = new AuthenticationHeaderValue("Bot", _token);
    }

    public async Task RunAsync(CancellationToken ct)
    {
        if (!Enabled) return;
        Console.WriteLine("[DISCORD CMD] command listener ON — send 'status' or 'close'/'end' in the channel.");
        _lastId = await GetLatestIdAsync(ct);   // baseline: ignore history; only react to messages sent from now on
        while (!ct.IsCancellationRequested)
        {
            try { await PollAsync(ct); }
            catch (OperationCanceledException) when (ct.IsCancellationRequested) { break; }
            catch (Exception ex) { Console.WriteLine($"[DISCORD CMD] poll error: {ex.GetType().Name}: {ex.Message}"); }
            try { await Task.Delay(_pollSec * 1000, ct); }
            catch (OperationCanceledException) { break; }
        }
    }

    private string ApiBase => $"https://discord.com/api/v10/channels/{_channelId}/messages";

    private async Task<string> GetLatestIdAsync(CancellationToken ct)
    {
        try
        {
            using var resp = await _http.GetAsync($"{ApiBase}?limit=1", ct);
            if (!resp.IsSuccessStatusCode) return "";
            using var doc = JsonDocument.Parse(await resp.Content.ReadAsStringAsync(ct));
            foreach (var m in doc.RootElement.EnumerateArray())
                return m.GetProperty("id").GetString() ?? "";
        }
        catch { }
        return "";
    }

    private async Task PollAsync(CancellationToken ct)
    {
        string url = string.IsNullOrEmpty(_lastId) ? $"{ApiBase}?limit=5" : $"{ApiBase}?after={_lastId}&limit=10";
        using var resp = await _http.GetAsync(url, ct);
        if (!resp.IsSuccessStatusCode)
        {
            if ((int)resp.StatusCode is 401 or 403 && !_warnedAuth)
            {
                _warnedAuth = true;
                Console.WriteLine($"[DISCORD CMD] {(int)resp.StatusCode} reading the channel — check DISCORD_BOT_TOKEN, " +
                                  "the bot's channel access, and that the MESSAGE CONTENT INTENT is enabled.");
            }
            return;
        }
        using var doc = JsonDocument.Parse(await resp.Content.ReadAsStringAsync(ct));
        // Discord returns newest-first; process oldest-first so commands run in order, and advance _lastId.
        var msgs = doc.RootElement.EnumerateArray().ToList();
        string newest = _lastId;
        for (int i = msgs.Count - 1; i >= 0; i--)
        {
            var m = msgs[i];
            string id = m.TryGetProperty("id", out var idEl) ? (idEl.GetString() ?? "") : "";
            if (id.Length == 0) continue;
            if (CompareSnowflake(id, newest) > 0) newest = id;
            // ignore bot/webhook authors (incl. our OWN posts) — only act on a human operator's messages
            if (m.TryGetProperty("author", out var au) && au.TryGetProperty("bot", out var b)
                && b.ValueKind == JsonValueKind.True)
                continue;
            string content = (m.TryGetProperty("content", out var c) ? c.GetString() ?? "" : "")
                .Trim().ToLowerInvariant();
            await HandleAsync(content);
        }
        if (!string.IsNullOrEmpty(newest)) _lastId = newest;
    }

    private async Task HandleAsync(string cmd)
    {
        switch (cmd)
        {
            case "status":
                Console.WriteLine("[DISCORD CMD] 'status' requested");
                try { await _reply(await _onStatus()); }
                catch (Exception ex) { await SafeReply($"status failed: {ex.Message}"); }
                break;
            case "close":
            case "end":
                Console.WriteLine($"[DISCORD CMD] '{cmd}' — graceful shutdown requested");
                await SafeReply("🛑 shutdown requested — stopping the bot gracefully (supervisor will NOT restart).");
                try { await _onShutdown(); }
                catch (Exception ex) { Console.WriteLine($"[DISCORD CMD] shutdown hook error: {ex.Message}"); }
                break;
        }
    }

    private async Task SafeReply(string msg) { try { await _reply(msg); } catch { } }

    // Discord snowflake ids are monotonically increasing numeric strings → compare by length then ordinal.
    private static int CompareSnowflake(string a, string b)
    {
        if (a.Length != b.Length) return a.Length - b.Length;
        return string.CompareOrdinal(a, b);
    }
}
