using PredictionBacktester.Engine;

namespace KalshiPaperTrader;

/// <summary>
/// Simulated broker for Kalshi paper trading.
/// Extends GlobalSimulatedBroker with Kalshi-specific constraints:
///   - Minimum order size: 1 contract
///   - No Ethereum signing or API calls — all fills are simulated against the live order book
/// Fee rate is 0 (set in strategy constructor) pending confirmation of Kalshi fee schedule.
/// </summary>
public class KalshiPaperBroker : GlobalSimulatedBroker
{
    public string StrategyName { get; }
    private readonly Dictionary<string, string> _tokenNames;

    public KalshiPaperBroker(string strategyName, decimal initialCapital, Dictionary<string, string> tokenNames)
        : base(initialCapital)
    {
        StrategyName = strategyName;
        _tokenNames = tokenNames;
        StrategyLabel = strategyName;
        AssetNameResolver = GetMarketName;
        SpreadPenalty = 0m; // Kalshi fees handled separately (currently 0)
    }

    /// <summary>Kalshi minimum order size is 1 contract.</summary>
    public override decimal GetMinSize(string assetId) => 1.0m;

    private string GetMarketName(string assetId)
    {
        if (_tokenNames.TryGetValue(assetId, out var name))
            return name.Length > 45 ? name[..42] + "..." : name;
        return assetId.Length > 12 ? assetId[..12] + "..." : assetId;
    }
}
