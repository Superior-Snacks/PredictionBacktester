namespace HardVenArb;

/// <summary>
/// Thread-safe debug logger with per-category runtime toggles.
/// Enabled once at startup from --debug flag; individual categories can be
/// toggled live via keyboard shortcuts (D/T/B/F/R — see Program.cs key handler).
/// </summary>
internal static class DebugLog
{
    internal static bool Enabled { get; set; }

    // ── Per-category debug toggles ────────────────────────────────────────────
    internal static bool DiscoveryEnabled { get; set; } = false; // D — arb detection (off by default; high volume)
    internal static bool TradesEnabled    { get; set; } = true;  // T — order execution events
    internal static bool BalanceEnabled   { get; set; } = true;  // B — balance fetch/refresh events
    internal static bool FeedEnabled      { get; set; } = false; // F — WebSocket feed events (off by default: very high volume)
    internal static bool BooksEnabled     { get; set; } = true;  // R — REST book-refresh events

    // ── Display toggles (all modes) ───────────────────────────────────────────
    internal static bool NearMissEnabled   { get; set; } = true; // N — top-10 near-miss report
    internal static bool StatusDashEnabled { get; set; } = true; // S — periodic status dashboard

    // ── Category writers ──────────────────────────────────────────────────────
    internal static void Write    (string msg) => Log("GEN",       true,             msg);
    internal static void Discovery(string msg) => Log("DISCOVERY", DiscoveryEnabled, msg);
    internal static void Trades   (string msg) => Log("TRADES",    TradesEnabled,    msg);
    internal static void Balance  (string msg) => Log("BALANCE",   BalanceEnabled,   msg);
    internal static void Feed     (string msg) => Log("FEED",      FeedEnabled,      msg);
    internal static void Books    (string msg) => Log("BOOKS",     BooksEnabled,     msg);

    private static void Log(string tag, bool categoryOn, string msg)
    {
        if (!Enabled || !categoryOn) return;
        Console.ForegroundColor = ConsoleColor.DarkGray;
        Console.WriteLine($"[DBG/{tag} {DateTime.UtcNow:HH:mm:ss.fff}] {msg}");
        Console.ResetColor();
    }

    internal static string DebugStatusLine() =>
        $"Discovery={F(DiscoveryEnabled)} Trades={F(TradesEnabled)} Balance={F(BalanceEnabled)} Feed={F(FeedEnabled)} Books={F(BooksEnabled)}";

    internal static string DisplayStatusLine() =>
        $"NearMiss={F(NearMissEnabled)} StatusDash={F(StatusDashEnabled)}";

    private static string F(bool v) => v ? "ON" : "off";
}
