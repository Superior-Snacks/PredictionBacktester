namespace KalshiPolyCross;

/// <summary>
/// Thread-safe debug logger with per-category runtime toggles.
/// Enabled once at startup from --debug flag; individual categories can be
/// toggled live via keyboard shortcuts (D/T/B/F/R — see Program.cs key handler).
/// </summary>
internal static class DebugLog
{
    internal static bool Enabled { get; set; }

    // ── Per-category toggles (all on when --debug is first activated) ─────────
    internal static bool DiscoveryEnabled { get; set; } = true; // D — arb detection events
    internal static bool TradesEnabled    { get; set; } = true; // T — order execution events
    internal static bool BalanceEnabled   { get; set; } = true; // B — balance fetch/refresh events
    internal static bool FeedEnabled      { get; set; } = true; // F — WebSocket feed events
    internal static bool BooksEnabled     { get; set; } = true; // R — REST book-refresh events

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

    internal static string StatusLine() =>
        $"Discovery={F(DiscoveryEnabled)} Trades={F(TradesEnabled)} Balance={F(BalanceEnabled)} Feed={F(FeedEnabled)} Books={F(BooksEnabled)}";

    private static string F(bool v) => v ? "ON" : "off";
}
