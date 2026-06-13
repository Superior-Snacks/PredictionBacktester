namespace HardVenArb;

/// <summary>
/// Credentials/endpoint for the HardVen betting-site venue. Loaded from environment (the shared
/// Kalshi <c>LoadDotEnv()</c> already pulls the solution-root <c>.env</c> into the process, so adding
/// the <c>HARDVEN_*</c> keys there is sufficient). Fields are intentionally minimal placeholders —
/// extend (auth scheme, per-site rotation, etc.) when the real venue integration is built.
/// </summary>
public sealed class HardVenApiConfig
{
    public string ApiKey     { get; set; } = "";
    public string ApiSecret  { get; set; } = "";
    public string BaseUrl    { get; set; } = "";   // REST base for the betting site
    public string SocksProxy { get; set; } = "";   // optional SOCKS5 tunnel (e.g. "socks5://host:port")

    /// <summary>True once a base URL + key are present. Used to warn at startup when the venue is unconfigured.</summary>
    public bool IsConfigured => !string.IsNullOrWhiteSpace(BaseUrl) && !string.IsNullOrWhiteSpace(ApiKey);

    public static HardVenApiConfig FromEnvironment() => new()
    {
        ApiKey     = Environment.GetEnvironmentVariable("HARDVEN_API_KEY")     ?? "",
        ApiSecret  = Environment.GetEnvironmentVariable("HARDVEN_API_SECRET")  ?? "",
        BaseUrl    = Environment.GetEnvironmentVariable("HARDVEN_BASE_URL")    ?? "",
        SocksProxy = Environment.GetEnvironmentVariable("HARDVEN_SOCKS_PROXY") ?? "",
    };
}
