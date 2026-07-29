using System.Collections.Concurrent;
using System.Globalization;
using System.Text.Json;

namespace HardVenArb;

/// <summary>
/// Dry-run / PAPER implementation of <see cref="IHardVenOrderExecutor"/>. Simulates Pinnacle bet placement
/// via <see cref="SimulatedFillProfile"/> (latency, partial fills, leg failures, slippage) WITHOUT opening
/// Pinnacle or placing a real bet. This is the missing half of paper mode: the scaffold dropped
/// SimulatedPolymarketClient and never replaced it, so <c>--dry-run</c> previously threw on the HardVen leg
/// (the live <see cref="HardVenOrderClient"/> throws until the UI bet-slip path is built).
///
/// <para><see cref="SubmitOrderAsync"/> returns the SAME response shape the live sidecar <c>/bet</c> path will
/// (success / orderID / status / takingAmount / makingAmount), so the executor's <c>ExtractHardVenFill</c>
/// parses it identically to live. Internal per-token position state keeps <see cref="GetTokenBalanceAsync"/>
/// consistent for the recovery / reconciliation paths.</para>
/// </summary>
public sealed class SimulatedHardVenClient : IHardVenOrderExecutor
{
    private readonly SimulatedFillProfile _profile;
    private readonly HardVenOrderClient? _uiVerifier;                                 // dry-run "proper sim": drive the REAL UI verify-only
    private readonly ConcurrentDictionary<string, decimal> _positions = new();       // tokenId -> net shares held
    private readonly ConcurrentDictionary<string, (string Status, decimal Shares)> _orders = new();

    /// <param name="uiVerifier">When set (HARDVEN_DRYRUN_UI=1), each BUY first drives the REAL Pinnacle UI
    /// verify-only (locate + click moneyline + verify popover + stake, nothing placed) before the fill is
    /// simulated — a proper end-to-end dry run. A verify FAILURE fails the leg (faithful: a real bet couldn't
    /// place), so the executor runs recovery. Null = pure numeric sim (no browser).</param>
    public SimulatedHardVenClient(SimulatedFillProfile profile, HardVenOrderClient? uiVerifier = null)
    {
        _profile    = profile;
        _uiVerifier = uiVerifier;
    }

    public async Task<string> SubmitOrderAsync(
        string tokenId, decimal price, decimal size, int side,
        bool negRisk = false, string tickSize = "0.01", int feeRateBps = 0)
    {
        bool    isSell    = side != 0;                                 // 0 = BUY; non-zero = SELL (recovery reversal)

        // DRY-RUN "proper simulation": for a BUY, actually drive the Pinnacle UI (locate + click the moneyline +
        // verify) with NOTHING placed, then simulate the fill. A verify failure = the real bet wouldn't place →
        // fail the leg so recovery runs (faithful). SELL is never driven (no-reverse model; sim handles it).
        if (!isSell && _uiVerifier != null)
        {
            var v = await _uiVerifier.VerifyPlacementAsync(tokenId, price, size);
            if (v.Verified)
                Console.WriteLine($"[DRYRUN UI] Pinnacle moneyline located + verified @ {v.Odds:0.000} — real UI drive OK, nothing placed; simulating fill");
            else
            {
                Console.WriteLine($"[DRYRUN UI] Pinnacle verify FAILED — {v.Reason} — failing the leg (a real bet would not place)");
                string failId = $"SIM_H_UIFAIL_{DateTimeOffset.UtcNow.ToUnixTimeMilliseconds()}";
                _orders[failId] = ("canceled", 0m);
                return JsonSerializer.Serialize(new
                {
                    success = false, orderID = failId, status = "canceled",
                    takingAmount = "0", makingAmount = "0", reason = v.Reason,
                });
            }
        }

        if (_profile.FillLatencyMsHardVen > 0)
            await Task.Delay(_profile.FillLatencyMsHardVen);

        decimal filled    = _profile.SimulateHardVenFill(Math.Abs(size));
        decimal fillPrice = _profile.GetHardVenFillPrice(price);
        decimal dollars   = Math.Round(filled * fillPrice, 4);

        if (filled > 0m)
            _positions.AddOrUpdate(tokenId, isSell ? -filled : filled, (_, old) => old + (isSell ? -filled : filled));

        string orderId = $"SIM_H_{DateTimeOffset.UtcNow.ToUnixTimeMilliseconds()}_{tokenId[..Math.Min(8, tokenId.Length)]}";
        string status  = filled > 0m ? "matched" : "canceled";
        _orders[orderId] = (status, filled);

        // BUY:  takingAmount = shares received, makingAmount = USDC spent.
        // SELL: makingAmount = shares sold,     takingAmount = USDC received (ExtractHardVenFill swaps for isSell).
        decimal taking = isSell ? dollars : filled;
        decimal making = isSell ? filled  : dollars;
        return JsonSerializer.Serialize(new
        {
            success      = filled > 0m,          // a 0-fill (leg-fail injection) = the order didn't execute
            orderID      = orderId,
            status,
            takingAmount = taking.ToString(CultureInfo.InvariantCulture),
            makingAmount = making.ToString(CultureInfo.InvariantCulture),
        });
    }

    public Task<string> GetOrderAsync(string orderId)
    {
        var (status, shares) = _orders.TryGetValue(orderId, out var r) ? r : ("matched", 0m);
        return Task.FromResult(JsonSerializer.Serialize(new
        {
            orderID      = orderId,
            status,
            size_matched = shares.ToString(CultureInfo.InvariantCulture),
        }));
    }

    /// <summary>Simulated open position on a leg (recovery / reconcile read this). Clamped ≥0 — a "sell"
    /// reversal can't drive it negative in the sim.</summary>
    public Task<decimal> GetTokenBalanceAsync(string tokenId) =>
        Task.FromResult(_positions.TryGetValue(tokenId, out var v) ? Math.Max(0m, v) : 0m);

    /// <summary>Benign cash balance. In dry-run the executor seeds $1,000 internally and does NOT re-poll this,
    /// so the value only surfaces credential/connectivity at init — a large constant is fine.</summary>
    public Task<decimal> GetUsdcBalanceAsync() => Task.FromResult(100_000m);

    public Task UpdateBalanceAllowanceAsync(string tokenId) => Task.CompletedTask;

    // Pinnacle's vig is baked into the decimal odds → no per-contract taker fee (matches the live client).
    public Task<int> GetTakerFeeAsync(string tokenId) => Task.FromResult(0);
    public Task<(decimal R, double E)> GetFeeParamsAsync(string tokenId) => Task.FromResult((0m, 1.0));
    public Task<string> GetTickSizeAsync(string tokenId) => Task.FromResult("0.01");
}
