namespace HardVenArb;

/// <summary>
/// STUB live client for the HardVen betting-site venue — the open slot to fill in.
///
/// Deliberate stub depth (scaffold): the <b>read-only</b> methods return benign defaults so the bot
/// BOOTS cleanly (CrossArbExecutor.InitializeBalancesAsync queries fees/tick/balance per token at
/// startup). The <b>mutating</b> methods (place order, poll order, refresh allowance) throw
/// <see cref="NotImplementedException"/> — so the bot runs the Kalshi side and idles, and the moment
/// an arb actually tries to place a HardVen order it fails loudly. Until this client (and
/// <see cref="HardVenWebsocketFeed"/>) are implemented, no HardVen book is fed, so no cross-arb window
/// can open and the throw paths stay unreached.
///
/// TODO: implement against the real betting-site API (auth, order placement, balances, fees).
/// </summary>
public sealed class HardVenOrderClient : IHardVenOrderExecutor
{
    private readonly HardVenApiConfig _config;

    public HardVenOrderClient(HardVenApiConfig config) => _config = config;

    // ── Mutating: not implemented (throw if an arb ever reaches order placement) ──
    public Task<string> SubmitOrderAsync(
        string tokenId, decimal price, decimal size, int side,
        bool negRisk = false, string tickSize = "0.01", int feeRateBps = 0) =>
        throw new NotImplementedException("TODO: implement HardVenOrderClient.SubmitOrderAsync against the betting-site API.");

    public Task<string> GetOrderAsync(string orderId) =>
        throw new NotImplementedException("TODO: implement HardVenOrderClient.GetOrderAsync.");

    public Task UpdateBalanceAllowanceAsync(string tokenId) =>
        throw new NotImplementedException("TODO: implement HardVenOrderClient.UpdateBalanceAllowanceAsync.");

    // ── Read-only: benign defaults so the executor can boot (no live HardVen data yet) ──
    public Task<decimal> GetTokenBalanceAsync(string tokenId) => Task.FromResult(0m);

    public Task<decimal> GetUsdcBalanceAsync() => Task.FromResult(0m);

    public Task<int> GetTakerFeeAsync(string tokenId) => Task.FromResult(0);

    public Task<(decimal R, double E)> GetFeeParamsAsync(string tokenId) => Task.FromResult((0.03m, 1.0));

    public Task<string> GetTickSizeAsync(string tokenId) => Task.FromResult("0.01");
}
