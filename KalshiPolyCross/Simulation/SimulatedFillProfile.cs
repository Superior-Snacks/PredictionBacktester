namespace KalshiPolyCross;

/// <summary>
/// Controls how simulated fills behave in dry-run mode.
/// Inject into CrossArbExecutor so the dry-run path produces realistic
/// outcomes (latency, slippage, partial fills, leg failures) instead of
/// instantaneous perfect fills.
/// </summary>
public class SimulatedFillProfile
{
    // ── Latency ───────────────────────────────────────────────────────────────
    /// <summary>Simulated round-trip ms for a Kalshi IOC order.</summary>
    public int FillLatencyMsKalshi { get; init; } = 100;
    /// <summary>Simulated round-trip ms for a Polymarket FAK order.</summary>
    public int FillLatencyMsPoly   { get; init; } = 80;

    // ── Slippage ──────────────────────────────────────────────────────────────
    /// <summary>Cents above the limit price at which the simulated Kalshi fill occurs.</summary>
    public decimal KalshiSlippageCents { get; init; } = 0m;
    /// <summary>
    /// Fraction of the limit price added to the simulated Poly fill (0.01 = 1%).
    /// Poly slippage is modeled as percentage-of-price rather than fixed cents
    /// because Poly prices span a much wider range (0.02–0.98).
    /// </summary>
    public decimal PolySlippagePct { get; init; } = 0m;

    // ── Failure rates ─────────────────────────────────────────────────────────
    /// <summary>
    /// 0–1 probability that a leg fills partially (20–90% of requested qty).
    /// Exercises RecoverUnhedgedAsync and the hedge/reverse decision tree.
    /// </summary>
    public double PartialFillRate { get; init; } = 0.0;
    /// <summary>0–1 probability that a Kalshi IOC leg fills 0.</summary>
    public double KalshiLegFailRate { get; init; } = 0.0;
    /// <summary>0–1 probability that a Polymarket FAK leg fills 0.</summary>
    public double PolyLegFailRate   { get; init; } = 0.0;
    /// <summary>
    /// 0–1 probability that a genuinely-canceled Kalshi IOC is misreported as "executed"
    /// with a phantom 1-contract fill. The phantom fill is NOT written to venue positions,
    /// so the next ReconcileTradeAsync call detects a mismatch and halts.
    /// </summary>
    public double CancelRaceRate    { get; init; } = 0.0;

    // ── RNG ───────────────────────────────────────────────────────────────────
    private readonly Random _rng;
    private readonly object _rngLock = new();

    /// <param name="seed">
    /// Optional RNG seed. Same seed + same profile + same market events = identical
    /// simulated fills, enabling deterministic regression tests.
    /// </param>
    public SimulatedFillProfile(int? seed = null)
    {
        _rng = seed.HasValue ? new Random(seed.Value) : new Random();
    }

    // ── Price helpers ──────────────────────────────────────────────────────────

    /// <summary>
    /// Number of PlaceOrderAsync calls that should throw instead of simulating a fill.
    /// Applied in order: first N calls fail, then normal simulation resumes.
    /// Use to pre-seed the MaintenanceThreshold scenario at startup.
    /// </summary>
    public int KalshiErrorsOnStartup { get; init; } = 0;

    /// <summary>Returns true if this canceled order should be misreported as a phantom fill.</summary>
    public bool ShouldCancelRace()
    {
        if (CancelRaceRate <= 0.0) return false;
        lock (_rngLock) return _rng.NextDouble() < CancelRaceRate;
    }

    /// <summary>
    /// Returns the simulated Kalshi fill price in dollars:
    /// <c>limitPriceDollars + KalshiSlippageCents / 100</c>.
    /// </summary>
    public decimal GetKalshiFillPrice(decimal limitPriceDollars)
        => limitPriceDollars + KalshiSlippageCents / 100m;

    /// <summary>
    /// Returns the simulated Poly fill price:
    /// <c>limitPrice × (1 + PolySlippagePct)</c>.
    /// </summary>
    public decimal GetPolyFillPrice(decimal limitPrice)
        => limitPrice * (1m + PolySlippagePct);

    // ── Fill quantity helpers ──────────────────────────────────────────────────

    /// <summary>
    /// Simulates a Kalshi IOC fill for <paramref name="requested"/> contracts.
    /// Returns 0 on a leg failure, a partial quantity (min 1) on a partial fill,
    /// or <paramref name="requested"/> on a full fill.
    /// Kalshi requires whole contracts, so the result is always an integer.
    /// </summary>
    public int SimulateKalshiFill(int requested)
    {
        if (requested <= 0) return 0;
        lock (_rngLock)
        {
            if (_rng.NextDouble() < KalshiLegFailRate) return 0;
            if (_rng.NextDouble() < PartialFillRate)
            {
                // Fill 20–90% of the requested quantity, at least 1 contract.
                double pct = 0.20 + _rng.NextDouble() * 0.70;
                return Math.Max(1, (int)Math.Floor(pct * requested));
            }
            return requested;
        }
    }

    /// <summary>
    /// Simulates a Polymarket FAK fill for <paramref name="requested"/> shares.
    /// Returns 0m on a leg failure, a partial quantity on a partial fill,
    /// or <paramref name="requested"/> on a full fill.
    /// Poly supports fractional shares, so the result may be non-integer.
    /// </summary>
    public decimal SimulatePolyFill(decimal requested)
    {
        if (requested <= 0m) return 0m;
        lock (_rngLock)
        {
            if (_rng.NextDouble() < PolyLegFailRate) return 0m;
            if (_rng.NextDouble() < PartialFillRate)
            {
                double pct = 0.20 + _rng.NextDouble() * 0.70;
                return Math.Max(0.01m, Math.Round((decimal)pct * requested, 4));
            }
            return requested;
        }
    }
}
