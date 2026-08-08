using System.Net.Http;
using System.Net.Http.Headers;
using System.Text;
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
    private readonly string _sidecarBase;             // control plane lives in the sidecar (lifecycle owns it)
    private readonly HttpClient _ctlHttp = new() { Timeout = TimeSpan.FromSeconds(30) };
    private string _lastId = "";
    private bool _warnedAuth;
    private int _emptyContent;        // consecutive human messages with unreadable text (missing intent)
    private bool _warnedIntent;

    public bool Enabled => !string.IsNullOrWhiteSpace(_token) && !string.IsNullOrWhiteSpace(_channelId);

    public DiscordCommandListener(string? botToken, string? channelId, Func<string, Task> reply,
                                  Func<Task<string>> onStatus, Func<Task> onShutdown, int pollSec = 10,
                                  string sidecarBaseUrl = "")
    {
        _token     = string.IsNullOrWhiteSpace(botToken)  ? null : botToken.Trim();
        _channelId = string.IsNullOrWhiteSpace(channelId) ? null : channelId.Trim();
        _reply     = reply;
        _onStatus  = onStatus;
        _onShutdown = onShutdown;
        _pollSec   = pollSec > 0 ? pollSec : 10;
        _sidecarBase = (sidecarBaseUrl ?? "").TrimEnd('/');
        _http      = new HttpClient { Timeout = TimeSpan.FromSeconds(15) };
        if (_token != null)
            _http.DefaultRequestHeaders.Authorization = new AuthenticationHeaderValue("Bot", _token);
    }

    public async Task RunAsync(CancellationToken ct)
    {
        if (!Enabled) return;
        Console.WriteLine("[DISCORD CMD] command listener ON — send 'commands' in the channel for the menu.");
        _lastId = await GetLatestIdAsync(ct);   // baseline: ignore history; only react to messages sent from now on
        // Post the menu once on startup so the operator never has to remember a command to discover commands.
        // DISCORD_POST_HELP_ON_START=0 to suppress (e.g. if the supervisor restarts often).
        if (Environment.GetEnvironmentVariable("DISCORD_POST_HELP_ON_START") != "0")
            await SafeReply(HelpText);
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
            // MESSAGE CONTENT INTENT check: without it Discord returns messages with EMPTY content, so the
            // listener silently ignores every command and looks "connected but broken". Catch that once.
            if (content.Length == 0)
            {
                if (++_emptyContent == 3 && !_warnedIntent)
                {
                    _warnedIntent = true;
                    Console.WriteLine("[DISCORD CMD] read 3 human messages with EMPTY content — the bot almost " +
                                      "certainly lacks the MESSAGE CONTENT INTENT (Discord Developer Portal → " +
                                      "your app → Bot → Privileged Gateway Intents → Message Content Intent). " +
                                      "Commands cannot work until that is enabled.");
                    await SafeReply("⚠️ I can see messages but not their text — enable the **Message Content " +
                                    "Intent** for this bot in the Discord Developer Portal, then restart me.");
                }
                continue;
            }
            _emptyContent = 0;
            await HandleAsync(content);
        }
        if (!string.IsNullOrEmpty(newest)) _lastId = newest;
    }

    private async Task HandleAsync(string raw)
    {
        // "pause low funds" → verb "pause", args "low funds". Session/schedule verbs are FORWARDED to the
        // sidecar, which owns the lifecycle (browser open/close, windows, pins) and persists operator state.
        string[] parts = raw.Split(' ', 2, StringSplitOptions.RemoveEmptyEntries);
        string cmd = parts.Length > 0 ? parts[0] : "";
        string args = parts.Length > 1 ? parts[1].Trim() : "";
        switch (cmd)
        {
            case "status":
                Console.WriteLine("[DISCORD CMD] 'status' requested");
                try { await _reply(await _onStatus()); }
                catch (Exception ex) { await SafeReply($"status failed: {ex.Message}"); }
                break;
            case "close":
            case "end":
            case "kill":
                Console.WriteLine($"[DISCORD CMD] '{cmd}' — graceful shutdown requested");
                await SafeReply("🛑 shutdown requested — stopping the bot **and the sidecar** " +
                                "(supervisor will NOT restart).");
                // Tear the SIDECAR down too. This used to run only under the `--stop-sidecar` CLI flag, so a
                // Discord `close` stopped the C# bot and left the sidecar running its own lifecycle — still
                // opening/closing Pinnacle and holding a logged-in browser session. Observed 2026-08-08: the
                // bot stopped at 08:32 and sidecar window alerts kept arriving until 15:17. "Stop the bot
                // entirely" has to mean the venue session too; an unattended logged-in browser is the exact
                // exposure the schedule exists to avoid. Sidecar first — it refuses while a bet is in flight.
                try
                {
                    using var sc = new HttpClient { Timeout = TimeSpan.FromSeconds(15) };
                    using var sr = await sc.PostAsync($"{_sidecarBase.TrimEnd('/')}/shutdown", null);
                    Console.WriteLine($"[DISCORD CMD] sidecar /shutdown → {(int)sr.StatusCode}");
                    if (!sr.IsSuccessStatusCode)
                        await SafeReply($"⚠️ sidecar shutdown returned HTTP {(int)sr.StatusCode} — " +
                                        "it may still be running; check the browser is closed.");
                }
                catch (Exception ex)
                {
                    Console.WriteLine($"[DISCORD CMD] sidecar shutdown error: {ex.Message}");
                    await SafeReply($"⚠️ could not reach the sidecar to stop it ({ex.GetType().Name}) — " +
                                    "the bot is stopping, but CLOSE THE BROWSER MANUALLY.");
                }
                try { await _onShutdown(); }
                catch (Exception ex) { Console.WriteLine($"[DISCORD CMD] shutdown hook error: {ex.Message}"); }
                break;

            // ── session control (sidecar) ─────────────────────────────────────
            case "pause":
                await CtlAsync("/control/pause", new { reason = args.Length > 0 ? args : "discord" },
                               "⏸️ pausing — closing the site");
                break;
            case "resume":
            case "start":
                await CtlAsync("/control/resume", new { reason = "discord" }, "▶️ resuming — back on schedule");
                break;
            case "force":
            case "open":
                await CtlAsync("/control/force_open",
                               new { minutes = ParseNum(args, 60), reason = "discord force" },
                               $"🔵 forcing open for {ParseNum(args, 60):0} min");
                break;

            // Hands-off banking window. Opens the site in the bot's OWN Chrome profile (same account that
            // places the bets) and freezes tab switching, organic activity and page reloads so a deposit isn't
            // interrupted. Works THROUGH a balance halt on purpose: the halt closes the browser, and a closed
            // browser is exactly what you need open to top up. Auto-reverts to the halt — `resume` clears it.
            case "banking":
            case "deposit":
            case "bank":
                await CtlAsync("/control/banking",
                               new { minutes = ParseNum(args, 30), reason = "discord banking" },
                               $"🏦 banking window for {ParseNum(args, 30):0} min — site opening, automation frozen. " +
                               "Send `resume` when the funds have landed.");
                break;

            // ── schedule + pins (sidecar) ─────────────────────────────────────
            case "schedule":
            case "sched":
                await ScheduleAsync(args);
                break;
            case "pin":
                await CtlAsync("/control/pins", new { pins = args }, $"📌 pins set to: {(args.Length > 0 ? args : "(none)")}");
                break;
            case "unpin":
                await CtlAsync("/control/pins", new { pins = "" }, "📌 all pins cleared");
                break;
            case "toggle":
            case "set":
                await ToggleAsync(args);
                break;
            case "commands":
            case "cmds":
            case "help":
            case "?":
                await SafeReply(HelpText);
                break;
        }
    }

    /// <summary>The operator's menu. Posted once at startup (so it's always in recent history) and on
    /// `commands`/`help`. Kept under Discord's 2000-char cap.</summary>
    public const string HelpText =
        "**HardVen commands** — send any of these in this channel\n" +
        "`commands` · `status` — bot + session state\n" +
        "**Session**\n" +
        "`pause [reason]` — close the site & stay dark *(survives a restart)*\n" +
        "`resume` — back on schedule (also clears a low-balance halt)\n" +
        "`force [minutes]` — open NOW outside the schedule (default 60, auto-reverts)\n" +
        "`banking [minutes]` — open the site & FREEZE all automation so you can deposit/withdraw " +
        "(default 30; works through a low-balance halt, then reverts to it — `resume` when funds land)\n" +
        "**Schedule**\n" +
        "`schedule` — show the current plan\n" +
        "`schedule <key>=<val> …` — e.g. `schedule lead_min=20 max_blocks=3`\n" +
        "  · keys: `lead_min trail_min min_gap_min min_games max_blocks session_hours jitter_min horizon_hours paired_only today_only`\n" +
        "`pin 09:00-12:00[,20:00-23:00]` — always-on hours (local) · `unpin` — clear them\n" +
        "**Flags**\n" +
        "`toggle` — list flags · `toggle <FLAG> <0|1>` — e.g. `toggle HARDVEN_BET_ENABLE 0`\n" +
        "  · ⚠️ flags the C# bot reads at startup reply `needs-restart` — they are saved, not live\n" +
        "**Stop**\n" +
        "`close` / `end` / `kill` — stop the bot entirely (supervisor will NOT restart it)";

    private static double ParseNum(string s, double dflt)
        => double.TryParse(s.Split(' ')[0], System.Globalization.NumberStyles.Any,
                           System.Globalization.CultureInfo.InvariantCulture, out var v) && v > 0 ? v : dflt;

    /// <summary>POST a control verb to the sidecar and report the resulting state back to the channel.</summary>
    private async Task CtlAsync(string path, object payload, string ack)
    {
        if (string.IsNullOrEmpty(_sidecarBase))
        {
            await SafeReply("⚠️ no sidecar URL configured — session control unavailable.");
            return;
        }
        Console.WriteLine($"[DISCORD CMD] -> sidecar {path}");
        try
        {
            using var body = new StringContent(JsonSerializer.Serialize(payload), Encoding.UTF8, "application/json");
            using var resp = await _ctlHttp.PostAsync(_sidecarBase + path, body);
            string txt = await resp.Content.ReadAsStringAsync();
            if (!resp.IsSuccessStatusCode)
            {
                await SafeReply($"⚠️ {path} failed (HTTP {(int)resp.StatusCode}): {Trim(txt, 300)}");
                return;
            }
            await SafeReply($"{ack}\n{Summarize(txt)}");
        }
        catch (Exception ex)
        {
            await SafeReply($"⚠️ {path} error: {ex.GetType().Name}: {ex.Message}");
        }
    }

    /// <summary>`schedule` with no args shows the plan; with `k=v` pairs it edits and replans.</summary>
    private async Task ScheduleAsync(string args)
    {
        if (string.IsNullOrWhiteSpace(args))
        {
            if (string.IsNullOrEmpty(_sidecarBase)) { await SafeReply("⚠️ no sidecar URL configured."); return; }
            try
            {
                using var resp = await _ctlHttp.GetAsync(_sidecarBase + "/debug/schedule");
                await SafeReply(resp.IsSuccessStatusCode
                    ? FormatSchedule(await resp.Content.ReadAsStringAsync())
                    : $"⚠️ schedule unavailable (HTTP {(int)resp.StatusCode})");
            }
            catch (Exception ex) { await SafeReply($"⚠️ schedule error: {ex.Message}"); }
            return;
        }
        var kv = new Dictionary<string, object>();
        foreach (var tok in args.Split(new[] { ' ', ',' }, StringSplitOptions.RemoveEmptyEntries))
        {
            var p = tok.Split('=', 2);
            if (p.Length == 2 && p[0].Length > 0) kv[p[0].Trim()] = p[1].Trim();
        }
        if (kv.Count == 0) { await SafeReply("nothing to change — try `schedule lead_min=20 max_blocks=3`"); return; }
        await CtlAsync("/control/schedule", kv, $"🗓️ schedule updated: {string.Join(", ", kv.Select(x => $"{x.Key}={x.Value}"))}");
    }

    private async Task ToggleAsync(string args)
    {
        var p = args.Split(new[] { ' ', '=' }, 2, StringSplitOptions.RemoveEmptyEntries);
        if (p.Length == 0) { await CtlAsync("/control/toggle", new { }, "🎛️ toggles"); return; }
        string key = p[0].Trim().ToUpperInvariant();
        string val = p.Length > 1 ? p[1].Trim() : "";
        await CtlAsync("/control/toggle", new { key, value = val }, $"🎛️ {key} → {val}");
    }

    /// <summary>Pull the few fields worth echoing out of a control response (state, override, next change).</summary>
    private static string Summarize(string json)
    {
        try
        {
            using var doc = JsonDocument.Parse(json);
            var r = doc.RootElement;
            if (r.ValueKind != JsonValueKind.Object) return "";
            var bits = new List<string>();
            if (r.TryGetProperty("state", out var st)) bits.Add($"state=**{st.GetString()}**");
            if (r.TryGetProperty("override_reason", out var rs) && !string.IsNullOrEmpty(rs.GetString()))
                bits.Add($"reason={rs.GetString()}");
            if (r.TryGetProperty("windows", out var w)) bits.Add($"windows={w}");
            if (r.TryGetProperty("next_change_secs", out var n) && n.ValueKind == JsonValueKind.Number)
                bits.Add($"next change in {n.GetDouble() / 60:0}m");
            if (r.TryGetProperty("applied", out var ap) && ap.ValueKind == JsonValueKind.Object)
                bits.Add($"applied={ap}");
            if (r.TryGetProperty("detail", out var d)) bits.Add(d.GetString() ?? "");
            if (r.TryGetProperty("effect", out var ef)) bits.Add($"effect={ef.GetString()}");
            return bits.Count > 0 ? string.Join(" · ", bits) : Trim(json, 400);
        }
        catch { return Trim(json, 400); }
    }

    private static string FormatSchedule(string json)
    {
        try
        {
            using var doc = JsonDocument.Parse(json);
            var r = doc.RootElement;
            var sb = new StringBuilder();
            sb.Append($"🗓️ **state={r.GetProperty("state").GetString()}**");
            if (r.TryGetProperty("override", out var ov) && ov.ValueKind == JsonValueKind.String)
                sb.Append($" (override: {ov.GetString()})");
            sb.Append('\n');
            if (r.TryGetProperty("windows_detail", out var wd) && wd.ValueKind == JsonValueKind.Array)
                foreach (var w in wd.EnumerateArray().Take(6))
                {
                    string pin = w.TryGetProperty("pinned", out var pn) && pn.ValueKind == JsonValueKind.True ? " 📌" : "";
                    string mark = w.GetProperty("state").GetString() == "NOW" ? " ⬅ now" : "";
                    sb.Append($"• {w.GetProperty("open_local").GetString()} → {w.GetProperty("close_local").GetString()} " +
                              $"({w.GetProperty("games")} games){pin}{mark}\n");
                }
            if (r.TryGetProperty("left_behind", out var lb) && lb.ValueKind == JsonValueKind.Array && lb.GetArrayLength() > 0)
                sb.Append($"• left behind: {lb.GetArrayLength()} game(s)\n");
            return Trim(sb.ToString(), 1800);
        }
        catch { return Trim(json, 800); }
    }

    private static string Trim(string s, int n) => s.Length <= n ? s : s[..n] + "…";

    private async Task SafeReply(string msg) { try { await _reply(msg); } catch { } }

    // Discord snowflake ids are monotonically increasing numeric strings → compare by length then ordinal.
    private static int CompareSnowflake(string a, string b)
    {
        if (a.Length != b.Length) return a.Length - b.Length;
        return string.CompareOrdinal(a, b);
    }
}
