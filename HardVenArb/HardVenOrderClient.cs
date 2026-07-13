using System.Globalization;
using System.Net.Http;
using System.Text.Json;

namespace HardVenArb;

/// <summary>
/// Live client for the HardVen betting-site venue. It is a THIN HTTP client to the local sidecar
/// (the same process that serves <c>GET /odds</c>) — the sidecar owns the Pinnacle session/auth, so all
/// balance + bet traffic routes through it (<c>GET /balance</c>, <c>POST /bet</c>, <c>GET /bets/{id}</c>),
/// never a second Pinnacle connection from C#.
///
/// State (2026-07): READ methods are live — <see cref="GetUsdcBalanceAsync"/> returns the real Pinnacle
/// wallet balance FX-converted to USD (Kalshi is USD; the Pinnacle account is EUR). Fee/tick reads return
/// Pinnacle-correct benign values (no per-contract taker fee — the vig is baked into the decimal odds).
/// The MUTATING methods (place/poll order, refresh) throw until the bet-placement task wires them to the
/// sidecar <c>/bet</c> endpoint (which itself gates real money behind the sidecar's execution enable flag).
/// </summary>
public sealed class HardVenOrderClient : IHardVenOrderExecutor
{
    private readonly HardVenApiConfig _config;
    private readonly string _sidecarBase;   // e.g. http://127.0.0.1:8787 — the odds/balance/bet sidecar
    private readonly decimal _fxToUsd;      // USD per account-unit (EUR≈1.08); 1.0 = USD book / no-op
    private readonly HttpClient _http = new() { Timeout = TimeSpan.FromSeconds(15) };

    public HardVenOrderClient(HardVenApiConfig config, string sidecarBaseUrl, decimal fxToUsd)
    {
        _config      = config;
        _sidecarBase = (sidecarBaseUrl ?? "").TrimEnd('/');
        _fxToUsd     = fxToUsd > 0m ? fxToUsd : 1.0m;
    }

    // ── Read-only: live via the sidecar ────────────────────────────────────────
    /// <summary>Real Pinnacle wallet cash, FX-converted to USD. 0 on any error (executor treats 0 as
    /// "can't fund" and low-cash-alerts / skips — never throws out of the balance refresh loop).</summary>
    public async Task<decimal> GetUsdcBalanceAsync()
    {
        if (string.IsNullOrWhiteSpace(_sidecarBase)) return 0m;
        try
        {
            using var resp = await _http.GetAsync($"{_sidecarBase}/balance");
            if (!resp.IsSuccessStatusCode) return 0m;
            using var doc = JsonDocument.Parse(await resp.Content.ReadAsStringAsync());
            if (!doc.RootElement.TryGetProperty("balance", out var b) || !b.TryGetDecimal(out var amt))
                return 0m;
            return Math.Round(amt * _fxToUsd, 2);   // account currency → USD-equivalent
        }
        catch
        {
            return 0m;
        }
    }

    /// <summary>Position on a specific selection = the open (unsettled) back bet on it. Not yet tracked via
    /// token id (the sidecar's /bets/open is the future source); 0 = start flat. TODO: wire to open bets when
    /// crash-recovery of a live HardVen leg is needed.</summary>
    public Task<decimal> GetTokenBalanceAsync(string tokenId) => Task.FromResult(0m);

    /// <summary>Pinnacle has no per-contract taker fee — the margin (vig) is inside the decimal odds, so the
    /// 1/odds cost the feed serves already accounts for it. 0 = no add-on fee.</summary>
    public Task<int> GetTakerFeeAsync(string tokenId) => Task.FromResult(0);

    /// <summary>No separate HardVen fee curve (vig is in the odds) → R=0 so the executor adds no phantom
    /// per-share fee to the HardVen leg.</summary>
    public Task<(decimal R, double E)> GetFeeParamsAsync(string tokenId) => Task.FromResult((0m, 1.0));

    /// <summary>Odds price granularity placeholder (a back bet takes the offered odds, not a limit tick).</summary>
    public Task<string> GetTickSizeAsync(string tokenId) => Task.FromResult("0.01");

    // ── Mutating: gated until the bet-placement task wires these to the sidecar /bet endpoint ──
    public Task<string> SubmitOrderAsync(
        string tokenId, decimal price, decimal size, int side,
        bool negRisk = false, string tickSize = "0.01", int feeRateBps = 0) =>
        throw new NotImplementedException("TODO(bet): route SubmitOrderAsync → sidecar POST /bet (real money — behind the sidecar execution enable gate).");

    public Task<string> GetOrderAsync(string orderId) =>
        throw new NotImplementedException("TODO(bet): route GetOrderAsync → sidecar GET /bets/{id}.");

    public Task UpdateBalanceAllowanceAsync(string tokenId) => Task.CompletedTask;   // no allowance concept on a sportsbook
}
