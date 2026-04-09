namespace PredictionBacktester.Engine.LiveExecution;

public class KalshiApiConfig
{
    public string ApiKeyId { get; set; } = "";        // UUID: a952bcbe-ec3b-...
    public string PrivateKeyPath { get; set; } = "";  // Path to .key PEM file

    // Demo: https://demo-api.kalshi.co/trade-api/v2
    // Prod: https://api.elections.kalshi.com/trade-api/v2
    public string BaseRestUrl { get; set; } = "https://demo-api.kalshi.co/trade-api/v2";

    // Demo: wss://demo-api.kalshi.co/trade-api/ws/v2
    // Prod: wss://api.elections.kalshi.com/trade-api/ws/v2
    public string BaseWsUrl { get; set; } = "wss://demo-api.kalshi.co/trade-api/ws/v2";

    public static KalshiApiConfig FromEnvironment() => new()
    {
        ApiKeyId       = Environment.GetEnvironmentVariable("KALSHI_API_KEY_ID")       ?? "",
        PrivateKeyPath = Environment.GetEnvironmentVariable("KALSHI_PRIVATE_KEY_PATH") ?? "",
    };
}
