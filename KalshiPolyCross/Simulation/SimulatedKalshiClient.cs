using System.Collections.Concurrent;
using PredictionBacktester.Engine.LiveExecution;

namespace KalshiPolyCross;

/// <summary>
/// Dry-run implementation of IKalshiOrderExecutor. Applies SimulatedFillProfile to
/// produce realistic fill outcomes (latency, partial fills, leg failures) without
/// sending any real orders. Maintains internal position state so GetPositionsAsync
/// returns values consistent with what was simulated.
/// </summary>
public class SimulatedKalshiClient : IKalshiOrderExecutor
{
    private readonly SimulatedFillProfile _profile;
    private readonly ConcurrentDictionary<string, int> _positions = new();
    private readonly ConcurrentDictionary<string, (string Status, decimal Fill)> _completedOrders = new();
    private int _pendingErrors;

    public SimulatedKalshiClient(SimulatedFillProfile profile)
    {
        _profile       = profile;
        _pendingErrors = profile.KalshiErrorsOnStartup;
    }

    /// <summary>
    /// Injects <paramref name="count"/> consecutive simulated REST failures.
    /// The next <paramref name="count"/> PlaceOrderAsync calls will throw, incrementing
    /// _kalshiConsecErrors in the executor until CheckMaintenanceThresholdAsync fires.
    /// Threshold in CrossArbExecutor is 5 consecutive errors.
    /// </summary>
    public void InjectMaintenanceErrors(int count)
    {
        Interlocked.Add(ref _pendingErrors, count);
        Console.ForegroundColor = ConsoleColor.Yellow;
        Console.WriteLine($"[SIM] Injected {count} Kalshi REST error(s) — threshold is 5 consecutive; VENUE_MAINTENANCE fires at 5+");
        Console.ResetColor();
    }

    public async Task<(string OrderId, string Status, decimal FillCount)> PlaceOrderAsync(
        string ticker, string side, int priceCents, int count,
        string action = "buy", string? clientOrderId = null)
    {
        // Maintenance error injection: throws to exercise CheckMaintenanceThresholdAsync in executor.
        if (_pendingErrors > 0 && Interlocked.Decrement(ref _pendingErrors) >= 0)
            throw new Exception("simulated Kalshi REST failure (maintenance injection)");

        if (_profile.FillLatencyMsKalshi > 0)
            await Task.Delay(_profile.FillLatencyMsKalshi);

        int filled = _profile.SimulateKalshiFill(Math.Abs(count));

        // Cancel-race: a missed IOC is misreported as "executed" with a phantom 1-contract
        // fill. Venue position (_positions) is NOT updated — executor records a fill the venue
        // doesn't have, guaranteeing a mismatch on the next ReconcileTradeAsync call.
        bool phantomFill = filled == 0 && _profile.ShouldCancelRace();
        if (phantomFill) filled = 1;

        string status = filled > 0 ? "executed" : "canceled";

        if (filled > 0 && !phantomFill)
        {
            bool isSell = string.Equals(action, "sell", StringComparison.OrdinalIgnoreCase);
            _positions.AddOrUpdate(ticker,
                isSell ? -filled : filled,
                (_, old) => old + (isSell ? -filled : filled));
        }

        string orderId = clientOrderId is { Length: > 0 }
            ? $"SIM_{clientOrderId}"
            : $"SIM_K_{DateTimeOffset.UtcNow.ToUnixTimeMilliseconds()}_{ticker}";

        _completedOrders[orderId] = (status, filled);
        return (orderId, status, filled);
    }

    public Task<(string Status, decimal FillCount)> PollOrderAsync(string orderId)
    {
        // Simulated orders resolve in PlaceOrderAsync — poll always returns the stored result.
        if (_completedOrders.TryGetValue(orderId, out var r))
            return Task.FromResult((r.Status, r.Fill));
        return Task.FromResult(("executed", 0m));
    }

    public Task<List<(string Ticker, int Position)>> GetPositionsAsync()
    {
        var result = _positions
            .Where(kv => kv.Value != 0)
            .Select(kv => (kv.Key, kv.Value))
            .ToList();
        return Task.FromResult(result);
    }

    // Executor overrides this to $1,000 immediately after calling it in dry-run mode.
    public Task<long> GetBalanceCentsAsync() => Task.FromResult(100_000_00L);
}
