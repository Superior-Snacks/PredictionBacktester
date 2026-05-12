namespace KalshiPolyCross;

/// <summary>
/// Preset SimulatedFillProfile configurations for dry-run scenario testing.
/// Pass <paramref name="seed"/> for deterministic replay; omit for random fills.
/// </summary>
public static class FailureScenarios
{
    /// <summary>No failures, default latency. Baseline for comparison.</summary>
    public static SimulatedFillProfile HappyPath(int? seed = null) => new(seed);

    /// <summary>20% Kalshi leg failure, 300ms Kalshi latency. Tests unhedged-recovery on entry leg miss.</summary>
    public static SimulatedFillProfile FlakyKalshi(int? seed = null) => new(seed)
    {
        FillLatencyMsKalshi = 300,
        KalshiLegFailRate   = 0.20,
    };

    /// <summary>20% Poly leg failure. Tests partial-fill recovery when hedge leg misses.</summary>
    public static SimulatedFillProfile FlakyPoly(int? seed = null) => new(seed)
    {
        PolyLegFailRate = 0.20,
    };

    /// <summary>1¢ Kalshi slippage, 2% Poly slippage. Tests per-trade and daily P&amp;L tripwires.</summary>
    public static SimulatedFillProfile ChronicSlippage(int? seed = null) => new(seed)
    {
        KalshiSlippageCents = 1m,
        PolySlippagePct     = 0.02m,
    };

    /// <summary>40% partial fill rate on both venues. Saturates RecoverUnhedgedAsync.</summary>
    public static SimulatedFillProfile PartialFillSwamp(int? seed = null) => new(seed)
    {
        PartialFillRate = 0.40,
    };

    /// <summary>10% leg failures + 15% partials on both venues, elevated latency.</summary>
    public static SimulatedFillProfile BothVenuesFlaky(int? seed = null) => new(seed)
    {
        FillLatencyMsKalshi = 200,
        FillLatencyMsPoly   = 160,
        KalshiLegFailRate   = 0.10,
        PolyLegFailRate     = 0.10,
        PartialFillRate     = 0.15,
    };

    /// <summary>2s Kalshi / 1.5s Poly latency, no failures. Tests timing logic under delay.</summary>
    public static SimulatedFillProfile LatencyStorm(int? seed = null) => new(seed)
    {
        FillLatencyMsKalshi = 2000,
        FillLatencyMsPoly   = 1500,
    };

    // ── Stage 4: targeted critical-fix scenarios ──────────────────────────────

    /// <summary>
    /// Poly always fails, Kalshi always fills. Every trade leaves an unhedged Kalshi position.
    /// Triggers dust absorption (CleanupDustUsd=$0.25) when contracts × legAsk &lt; $0.25,
    /// otherwise exercises RecoverUnhedgedAsync. Run against cheap markets (ask &lt; $0.25/contract)
    /// for reliable dust coverage.
    /// </summary>
    public static SimulatedFillProfile DustUnhedged(int? seed = null) => new(seed)
    {
        PolyLegFailRate = 1.0,
    };

    /// <summary>
    /// Kalshi entry always fails, Poly hedge fills. Every trade leaves an unhedged Poly position,
    /// driving RecoverUnhedgedAsync Case B (hedge on Kalshi). The Kalshi hedge uses
    /// Math.Ceiling(ask × 100) + slippage for priceCents. Run with a Kalshi book ask at a
    /// half-cent (e.g. $0.475) to verify ceiling gives 48¢, not 47¢.
    /// </summary>
    public static SimulatedFillProfile HalfCentBoundary(int? seed = null) => new(seed)
    {
        KalshiLegFailRate = 1.0,
    };

    /// <summary>
    /// Pre-injects 6 consecutive Kalshi REST failures at startup. CheckMaintenanceThresholdAsync
    /// fires at 5 consecutive errors, setting _connectionHalted=true and journaling VENUE_MAINTENANCE.
    /// Verify: (1) halt fires after the 5th error, (2) successful calls auto-clear _connectionHalted.
    /// Press E at runtime to inject additional error batches.
    /// </summary>
    public static SimulatedFillProfile MaintenanceThreshold(int? seed = null) => new(seed)
    {
        KalshiErrorsOnStartup = 6,
    };

    /// <summary>
    /// 40% Kalshi leg failures; 25% of those are misreported as phantom 1-contract fills.
    /// The phantom fills are NOT tracked in venue positions, guaranteeing a mismatch on the
    /// next ReconcileTradeAsync call → executor halts. Use with SimulatedVenuePositionClient
    /// for additional runtime-injected mismatches via the M key.
    /// </summary>
    public static SimulatedFillProfile CancelRace(int? seed = null) => new(seed)
    {
        KalshiLegFailRate = 0.40,
        CancelRaceRate    = 0.25,
    };

    /// <summary>
    /// Looks up a profile by case-insensitive name. Used by the --scenario CLI flag.
    /// Throws <see cref="ArgumentException"/> for unknown names.
    /// </summary>
    public static SimulatedFillProfile FromName(string name, int? seed = null) =>
        name.ToLowerInvariant() switch
        {
            "happypath"           => HappyPath(seed),
            "flakykalshi"         => FlakyKalshi(seed),
            "flakypoly"           => FlakyPoly(seed),
            "chronicslippage"     => ChronicSlippage(seed),
            "partialfillswamp"    => PartialFillSwamp(seed),
            "bothvenuesflaky"     => BothVenuesFlaky(seed),
            "latencystorm"        => LatencyStorm(seed),
            "dustunhedged"        => DustUnhedged(seed),
            "halfcentboundary"    => HalfCentBoundary(seed),
            "maintenancethreshold"=> MaintenanceThreshold(seed),
            "cancelrace"          => CancelRace(seed),
            _ => throw new ArgumentException(
                $"Unknown scenario '{name}'. Valid: HappyPath, FlakyKalshi, FlakyPoly, " +
                $"ChronicSlippage, PartialFillSwamp, BothVenuesFlaky, LatencyStorm, " +
                $"DustUnhedged, HalfCentBoundary, MaintenanceThreshold, CancelRace")
        };
}
