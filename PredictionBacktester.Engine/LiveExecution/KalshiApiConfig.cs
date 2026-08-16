namespace PredictionBacktester.Engine.LiveExecution;

public class KalshiApiConfig
{
    public string ApiKeyId { get; set; } = "";        // UUID: a952bcbe-ec3b-...
    public string PrivateKeyPath { get; set; } = "";  // Path to .key PEM file

    // Demo: https://demo-api.kalshi.co/trade-api/v2
    // Prod: https://api.elections.kalshi.com/trade-api/v2
    public string BaseRestUrl { get; set; } = "https://api.elections.kalshi.com/trade-api/v2";

    // Demo: wss://demo-api.kalshi.co/trade-api/ws/v2
    // Prod: wss://api.elections.kalshi.com/trade-api/ws/v2
    public string BaseWsUrl { get; set; } = "wss://api.elections.kalshi.com/trade-api/ws/v2";

    public static KalshiApiConfig FromEnvironment()
    {
        LoadDotEnv();
        return new()
        {
            ApiKeyId       = Environment.GetEnvironmentVariable("KALSHI_API_KEY_ID")       ?? "",
            PrivateKeyPath = Environment.GetEnvironmentVariable("KALSHI_PRIVATE_KEY_PATH") ?? "",
        };
    }

    /// <summary>
    /// Searches for a .env file and loads KEY=VALUE pairs into the process environment.
    /// Searches: executable dir → parent dir → user home dir → CWD.
    /// Handles 'export KEY=VALUE' and bare 'KEY=VALUE' syntax.
    /// Does not overwrite variables already set in the environment.
    /// </summary>
    /// <summary>Drop a trailing <c> # comment</c> from an UNQUOTED .env value.
    ///
    /// <para>Annotated .env lines are normal, and without this the comment becomes part of the value. Here that
    /// fails SILENTLY, which is the dangerous half: <c>HARDVEN_BOOK_FIRST=1  # press first</c> is not "1" any
    /// more, so the leg-ordering model quietly reverts, and <c>HARDVEN_BALANCE_BUFFER_USD=0  # no reserve</c>
    /// fails to parse and falls back to the percentage reserve — both of which change what the bot trades with
    /// no message anywhere (observed 2026-08-16).</para>
    ///
    /// <para>Whitespace-delimited, and quoted values are left alone, because '#' is legitimate inside a password
    /// or a URL fragment: <c>PASS=hunter#2</c> and <c>PASS="a # b"</c> both survive intact.</para></summary>
    private static string StripInlineComment(string v)
    {
        if (v.Length == 0 || v[0] == '"' || v[0] == '\'') return v;
        for (int i = 1; i < v.Length; i++)
            if (v[i] == '#' && char.IsWhiteSpace(v[i - 1]))
                return v[..i].TrimEnd();
        return v;
    }

    private static void LoadDotEnv()
    {
        var searchDirs = new[]
        {
            AppContext.BaseDirectory,
            Path.GetDirectoryName(AppContext.BaseDirectory.TrimEnd(Path.DirectorySeparatorChar)),
            Environment.GetFolderPath(Environment.SpecialFolder.UserProfile),
            Directory.GetCurrentDirectory(),
        };

        foreach (var dir in searchDirs)
        {
            if (string.IsNullOrEmpty(dir)) continue;
            var path = Path.Combine(dir, ".env");
            if (!File.Exists(path)) continue;

            foreach (var raw in File.ReadAllLines(path))
            {
                var line = raw.Trim();
                if (line.Length == 0 || line.StartsWith('#')) continue;
                if (line.StartsWith("export ", StringComparison.Ordinal))
                    line = line[7..].TrimStart();

                var eq = line.IndexOf('=');
                if (eq <= 0) continue;

                var key = line[..eq].Trim();
                var val = StripInlineComment(line[(eq + 1)..].Trim()).Trim('"').Trim('\'');

                if (!string.IsNullOrEmpty(key) &&
                    Environment.GetEnvironmentVariable(key) is null)
                {
                    Environment.SetEnvironmentVariable(key, val);
                }
            }
            return; // stop at first .env found
        }
    }
}
