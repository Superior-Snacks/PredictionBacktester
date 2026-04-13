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
                var val = line[(eq + 1)..].Trim().Trim('"').Trim('\'');

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
