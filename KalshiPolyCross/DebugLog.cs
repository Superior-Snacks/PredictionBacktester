namespace KalshiPolyCross;

internal static class DebugLog
{
    internal static bool Enabled { get; set; }

    internal static void Write(string msg)
    {
        if (!Enabled) return;
        Console.ForegroundColor = ConsoleColor.DarkGray;
        Console.WriteLine($"[DEBUG {DateTime.UtcNow:HH:mm:ss.fff}] {msg}");
        Console.ResetColor();
    }
}
