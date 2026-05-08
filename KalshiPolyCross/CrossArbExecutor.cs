using System.Collections.Concurrent;
using System.Text;
using System.Text.Json;
using System.Text.RegularExpressions;
using System.Threading.Channels;
using PredictionBacktester.Engine;
using PredictionBacktester.Engine.LiveExecution;

namespace KalshiPolyCross;

public record ArbPosition(
    string   PairId,
    string   ArbType,
    decimal  KalshiContracts,
    decimal  PolyShares,
    decimal  KalshiEntryPrice,
    decimal  PolyEntryPrice,
    DateTime EntryTime
);

/// <summary>
/// Fires simultaneous IOC/FAK orders on both legs when CrossPlatformArbTelemetryStrategy
/// detects a cross-platform arb window. Kalshi leg uses IOC via PlaceOrderAsync;
/// Polymarket leg uses FAK via PolymarketOrderClient (POLY_GNOSIS_SAFE signing).
/// </summary>
public class CrossArbExecutor
{
    private readonly KalshiOrderClient _kalshi;
    private readonly PolymarketOrderClient _poly;
    private readonly CrossPlatformArbTelemetryStrategy _telemetry;
    private readonly ConcurrentDictionary<string, LocalOrderBook> _books;

    // ── Configuration ─────────────────────────────────────────────────────────
    private readonly decimal _maxBetUsd;           // max combined dollar cost per arb entry
    private readonly decimal _balanceBufferPct;    // fraction of maxBetUsd kept as per-platform reserve
    private readonly decimal _maxExposureUsd;
    private readonly decimal _executionThreshold;
    private readonly int     _pairCooldownSeconds;
    private readonly int     _fillTimeoutMs;
    private readonly bool    _dryRun;

    // ── Runtime state ─────────────────────────────────────────────────────────
    private readonly ConcurrentDictionary<string, long>        _cooldownUntil = new();
    private readonly ConcurrentDictionary<string, ArbPosition> _openPositions = new();
    private          decimal _totalExposure        = 0m;
    private          decimal _totalInvested        = 0m;
    private          decimal _totalProjectedProfit = 0m;
    private readonly object  _exposureLock         = new();
    private readonly CancellationTokenSource _cts  = new();
    private          int     _totalExecuted        = 0;

    // ── Balance tracking ──────────────────────────────────────────────────────
    // Live: fetched from APIs at startup, refreshed after each execution.
    // Dry-run: seeded at $1,000 per side, decremented on each simulated entry.
    private          decimal _kalshiBalanceUsd;
    private          decimal _polyBalanceUsd;
    private readonly object  _balanceLock = new();

    // ── Fee model (must mirror CrossPlatformArbTelemetryStrategy) ────────────
    // Poly: fee = p × feeRate × (p×(1-p))^1 — Politics/Finance/Tech, March 30 2026+
    private static decimal KalshiFee(decimal p) => 0.07m * p * (1m - p);
    private static decimal PolyFee(decimal p)   => p * 0.04m * p * (1m - p);

    // ── CSV ───────────────────────────────────────────────────────────────────
    private readonly Channel<string> _csvChannel =
        Channel.CreateUnbounded<string>(new UnboundedChannelOptions { SingleReader = true });
    private readonly string _csvPath;
    private bool _headerWritten;

    public int     OpenPositionCount    => _openPositions.Count(kv => kv.Value != null);
    public decimal MaxExposureUsd       => _maxExposureUsd;
    public int     TotalExecuted        => Volatile.Read(ref _totalExecuted);
    public decimal KalshiBalanceUsd     { get { lock (_balanceLock)  return _kalshiBalanceUsd;        } }
    public decimal PolyBalanceUsd       { get { lock (_balanceLock)  return _polyBalanceUsd;          } }
    public decimal TotalExposure        { get { lock (_exposureLock) return _totalExposure;           } }
    public decimal TotalInvested        { get { lock (_exposureLock) return _totalInvested;           } }
    public decimal TotalProjectedProfit { get { lock (_exposureLock) return _totalProjectedProfit;    } }

    public CrossArbExecutor(
        KalshiOrderClient               kalshi,
        PolymarketOrderClient           poly,
        CrossPlatformArbTelemetryStrategy telemetry,
        ConcurrentDictionary<string, LocalOrderBook> books,
        decimal maxBetUsd           = 10m,
        decimal balanceBufferPct    = 0.20m,
        decimal maxExposureUsd      = 10m,
        decimal executionThreshold  = 0.990m,
        int     pairCooldownSeconds = 120,
        int     fillTimeoutMs       = 5000,
        bool    dryRun              = false)
    {
        _kalshi              = kalshi;
        _poly                = poly;
        _telemetry           = telemetry;
        _books               = books;
        _maxBetUsd           = maxBetUsd;
        _balanceBufferPct    = balanceBufferPct;
        _maxExposureUsd      = maxExposureUsd;
        _executionThreshold  = executionThreshold;
        _pairCooldownSeconds = pairCooldownSeconds;
        _fillTimeoutMs       = fillTimeoutMs;
        _dryRun              = dryRun;
        _csvPath             = $"CrossArbExecution_{DateTime.UtcNow:yyyyMMdd_HHmmss}.csv";
        _ = Task.Run(RunCsvWriterAsync);
    }

    /// <summary>
    /// Fetches real balances from both platforms (live) or seeds simulated $1,000 (dry-run).
    /// Call once after construction, before the WS feeds start.
    /// </summary>
    public async Task InitializeBalancesAsync()
    {
        if (_dryRun)
        {
            // Probe real balances so credential/connectivity issues surface in dry-run.
            // Simulation still starts at $1,000 regardless of actual balance.
            await RefreshBalancesAsync(initial: true);
            lock (_balanceLock) { _kalshiBalanceUsd = 1000m; _polyBalanceUsd = 1000m; }
            Console.WriteLine("[BALANCE INIT] Dry-run: simulation seeded at $1,000.00 on each platform");
            return;
        }
        await RefreshBalancesAsync(initial: true);
        _ = Task.Run(PeriodicBalanceRefreshLoop);
    }

    private async Task PeriodicBalanceRefreshLoop()
    {
        while (!_cts.Token.IsCancellationRequested)
        {
            try
            {
                await Task.Delay(TimeSpan.FromMinutes(5), _cts.Token);
                await RefreshBalancesAsync();
            }
            catch (TaskCanceledException) { break; }
            catch (Exception ex)
            {
                Console.WriteLine($"[BALANCE WARN] Periodic refresh loop error: {ex.Message}");
                DebugLog.Balance($"PeriodicBalanceRefreshLoop exception: {ex}");
            }
        }
    }

    private async Task RefreshBalancesAsync(bool initial = false)
    {
        try
        {
            long    kCents    = await _kalshi.GetBalanceCentsAsync();
            decimal newKalshi = kCents / 100m;
            decimal newPoly   = await _poly.GetUsdcBalanceAsync();
            lock (_balanceLock) { _kalshiBalanceUsd = newKalshi; _polyBalanceUsd = newPoly; }
            string tag = initial ? "[BALANCE INIT]" : "[BALANCE]";
            Console.WriteLine($"{tag} Kalshi=${newKalshi:0.00} Poly=${newPoly:0.00}");
            DebugLog.Balance($"RefreshBalancesAsync: K=${newKalshi:0.00} P=${newPoly:0.00} initial={initial}");
        }
        catch (Exception ex)
        {
            string balErr = ex is HttpRequestException ? ApiErrorHelper.ClassifyKalshi(ex) : ex.Message;
            Console.WriteLine($"[BALANCE WARN] Failed to refresh balance: {balErr}");
            DebugLog.Balance($"RefreshBalancesAsync exception: {ex}");
        }
    }

    /// <summary>Wire to telemetry.OnArbOpened — fires on every new WS-detected arb window.</summary>
    public void OnArbOpened(string pairId, decimal netCost, string arbType, decimal depth)
    {
        DebugLog.Trades($"OnArbOpened: {pairId} {arbType} net={netCost:0.0000} depth={depth:0.0}");
        _ = Task.Run(async () =>
        {
            try { await ExecuteAsync(pairId, arbType); }
            catch (Exception ex)
            {
                Console.WriteLine($"[EXEC ERROR] {pairId}: {ex.Message}");
                DebugLog.Trades($"ExecuteAsync unhandled exception for {pairId}: {ex}");
            }
        });
    }

    // ── Core execution ────────────────────────────────────────────────────────

    private async Task ExecuteAsync(string pairId, string arbType)
    {
        long now = DateTimeOffset.UtcNow.ToUnixTimeSeconds();

        // Guard: cooldown or open position on this pair
        if (_cooldownUntil.TryGetValue(pairId, out long cd) && now < cd)
        {
            DebugLog.Trades($"ExecuteAsync {pairId}: skipped — cooldown active for {cd - now}s more");
            return;
        }
        if (_openPositions.ContainsKey(pairId))
        {
            DebugLog.Trades($"ExecuteAsync {pairId}: skipped — open position already tracked");
            return;
        }

        var pair = _telemetry.GetPair(pairId);
        if (pair == null)
        {
            DebugLog.Trades($"ExecuteAsync {pairId}: pair not found in telemetry");
            return;
        }

        // Re-read live book prices at execution time
        if (!_books.TryGetValue($"K:{pair.KalshiTicker}",    out var kYes))
        { DebugLog.Trades($"ExecuteAsync {pair.Label}: missing book K:{pair.KalshiTicker}"); return; }
        if (!_books.TryGetValue($"K:{pair.KalshiTicker}_NO", out var kNo))
        { DebugLog.Trades($"ExecuteAsync {pair.Label}: missing book K:{pair.KalshiTicker}_NO"); return; }
        if (!_books.TryGetValue($"P:{pair.PolyYesTokenId}",  out var pYes))
        { DebugLog.Trades($"ExecuteAsync {pair.Label}: missing book P:yes"); return; }
        if (!_books.TryGetValue($"P:{pair.PolyNoTokenId}",   out var pNo))
        { DebugLog.Trades($"ExecuteAsync {pair.Label}: missing book P:no"); return; }

        decimal kLegAsk, pLegAsk;
        string  kalshiSide, polyToken;

        if (arbType == "K_YES_P_NO")
        {
            kLegAsk    = kYes.GetBestAskPrice();
            pLegAsk    = pNo.GetBestAskPrice();
            kalshiSide = "yes";
            polyToken  = pair.PolyNoTokenId;
        }
        else // K_NO_P_YES
        {
            kLegAsk    = kNo.GetBestAskPrice();
            pLegAsk    = pYes.GetBestAskPrice();
            kalshiSide = "no";
            polyToken  = pair.PolyYesTokenId;
        }

        // Re-validate arb still holds at execution time
        decimal netNow = kLegAsk + pLegAsk + KalshiFee(kLegAsk) + PolyFee(pLegAsk);
        DebugLog.Trades($"ExecuteAsync {pair.Label}: live check — kLeg={kLegAsk:0.0000} pLeg={pLegAsk:0.0000} net={netNow:0.0000} threshold={_executionThreshold:0.000}");
        if (netNow >= _executionThreshold)
        {
            Console.WriteLine($"[EXEC SKIP] {pair.Label} | net=${netNow:0.0000} >= threshold {_executionThreshold:0.000}");
            return;
        }

        if (kLegAsk <= 0.02m || pLegAsk <= 0.02m)
        {
            DebugLog.Trades($"ExecuteAsync {pair.Label}: price below 2¢ floor — kLeg={kLegAsk:0.0000} pLeg={pLegAsk:0.0000}");
            return;
        }

        int     kPriceCents   = (int)Math.Round(kLegAsk * 100);
        decimal pricePerSet   = kLegAsk + pLegAsk;
        int     contracts     = (int)Math.Floor(_maxBetUsd / pricePerSet);
        if (contracts < 1)
        {
            Console.WriteLine($"[EXEC SKIP] {pair.Label} | pricePerSet=${pricePerSet:0.0000} > maxBet=${_maxBetUsd:0.00}");
            return;
        }
        decimal polyShares    = contracts;
        decimal kalshiCost    = kLegAsk * contracts;
        decimal polyCost      = pLegAsk * contracts;
        decimal estimatedCost = kalshiCost + polyCost;
        decimal minBuffer     = _maxBetUsd * _balanceBufferPct;

        // Balance check — scale contracts down to what's affordable if needed.
        // All sizing and speculative reservation happen inside a single lock to prevent
        // concurrent executions from double-spending the same balance.
        int     idealContracts = contracts;
        decimal kBalSnap, pBalSnap;
        lock (_balanceLock)
        {
            kBalSnap = _kalshiBalanceUsd;
            pBalSnap = _polyBalanceUsd;

            // Max contracts each side can fund while preserving the buffer reserve.
            int kAffordable = kLegAsk > 0 ? (int)Math.Floor((kBalSnap - minBuffer) / kLegAsk) : 0;
            int pAffordable = pLegAsk > 0 ? (int)Math.Floor((pBalSnap - minBuffer) / pLegAsk) : 0;
            contracts = Math.Min(contracts, Math.Min(kAffordable, pAffordable));

            if (contracts >= 1)
            {
                // Recompute costs at the (possibly reduced) contract count and reserve.
                polyShares    = contracts;
                kalshiCost    = kLegAsk * contracts;
                polyCost      = pLegAsk * contracts;
                estimatedCost = kalshiCost + polyCost;
                _kalshiBalanceUsd -= kalshiCost;
                _polyBalanceUsd   -= polyCost;
            }
        }
        if (contracts < 1)
        {
            Console.WriteLine(
                $"[EXEC SKIP] {pair.Label} | Balance too low for 1 contract (${pricePerSet:0.0000}/set) " +
                $"K=${kBalSnap:0.00} P=${pBalSnap:0.00} buffer={_balanceBufferPct:P0}(${minBuffer:0.00})");
            DebugLog.Balance($"ExecuteAsync {pair.Label}: balance too low — K=${kBalSnap:0.00} P=${pBalSnap:0.00}");
            return;
        }
        if (contracts < idealContracts)
        {
            Console.WriteLine(
                $"[EXEC SCALE] {pair.Label} | {idealContracts}→{contracts} contracts (balance limited) " +
                $"K=${kBalSnap:0.00} P=${pBalSnap:0.00}");
            DebugLog.Balance($"ExecuteAsync {pair.Label}: scaled {idealContracts}→{contracts} contracts");
        }

        if (_dryRun)
        {
            // Speculative deduction above acts as the simulated spend; log and exit.
            decimal kBalAfter, pBalAfter;
            lock (_balanceLock) { kBalAfter = _kalshiBalanceUsd; pBalAfter = _polyBalanceUsd; }
            Console.ForegroundColor = ConsoleColor.Yellow;
            Console.WriteLine(
                $"[DRY RUN] {pair.Label} | {arbType} | K-{kalshiSide}={kLegAsk:0.0000} " +
                $"P={pLegAsk:0.0000} net=${netNow:0.0000} | {contracts} contracts est.cost=${estimatedCost:0.00} " +
                $"| balanceAfter K=${kBalAfter:0.00} P=${pBalAfter:0.00}");
            Console.ResetColor();
            EnqueueCsvRow(pair, arbType, DateTime.UtcNow, kPriceCents, kLegAsk, pLegAsk,
                          0m, 0m, 0m, netNow, "DRY_RUN");
            return;
        }

        // Exposure check (live only, thread-safe)
        bool exposureOk;
        lock (_exposureLock)
        {
            exposureOk = _totalExposure + estimatedCost <= _maxExposureUsd;
            if (exposureOk) _totalExposure += estimatedCost;
        }
        if (!exposureOk)
        {
            Console.WriteLine(
                $"[EXEC SKIP] {pair.Label} | Exposure limit " +
                $"${_totalExposure:0.00}+${estimatedCost:0.00} > ${_maxExposureUsd:0.00}");
            // Restore speculative balance reservation
            lock (_balanceLock) { _kalshiBalanceUsd += kalshiCost; _polyBalanceUsd += polyCost; }
            return;
        }

        // Set cooldown before firing to block concurrent execution on the same pair
        _cooldownUntil[pairId] = now + _pairCooldownSeconds;

        Console.ForegroundColor = ConsoleColor.Cyan;
        Console.WriteLine(
            $"[EXEC] {pair.Label} | {arbType} | K-{kalshiSide}={kLegAsk:0.0000} " +
            $"P={pLegAsk:0.0000} net=${netNow:0.0000} | {contracts} contracts @ ${estimatedCost:0.00}");
        Console.ResetColor();

        var t0 = DateTime.UtcNow;

        // Fire both legs simultaneously
        var kalshiTask = PlaceKalshiLegAsync(pair.KalshiTicker, kalshiSide, kPriceCents, contracts);
        var polyTask   = PlacePolyLegAsync(polyToken, pLegAsk, polyShares);
        await Task.WhenAll(kalshiTask, polyTask);

        var (kOrderId, kStatus, kFilled) = kalshiTask.Result;
        var (pFilled, pActualPrice)       = polyTask.Result;

        // Balanced quantity: the portion of each leg that is fully hedged.
        // Any excess on one side is an unhedged delta requiring recovery.
        decimal balancedQty  = Math.Min(kFilled, pFilled);
        decimal kUnhedged    = kFilled - balancedQty;  // excess Kalshi contracts
        decimal pUnhedged    = pFilled - balancedQty;  // excess Poly shares
        bool    neitherFilled = kFilled == 0 && pFilled == 0;

        if (balancedQty > 0)
        {
            decimal actualCost      = kLegAsk * balancedQty + pActualPrice * balancedQty;
            // Actual net cost per set using confirmed fill prices + fees.
            // kLegAsk is the IOC limit price (fills at or below this). pActualPrice is Poly's
            // reported average fill price and may exceed pLegAsk if the book was walked.
            decimal actualNetPerSet = kLegAsk + pActualPrice + KalshiFee(kLegAsk) + PolyFee(pActualPrice);
            bool    arbProfitable   = actualNetPerSet < 1.0m;
            decimal actualProfit    = balancedQty * (1.0m - actualNetPerSet);

            _openPositions[pairId] = new ArbPosition(
                pairId, arbType, balancedQty, balancedQty, kLegAsk, pActualPrice, t0);
            Interlocked.Increment(ref _totalExecuted);
            lock (_exposureLock) { _totalInvested += actualCost; _totalProjectedProfit += actualProfit; }

            if (arbProfitable)
            {
                Console.ForegroundColor = ConsoleColor.Green;
                Console.WriteLine(
                    $"[EXEC OK] {pair.Label} | K={balancedQty}@{kPriceCents}¢ | " +
                    $"P={balancedQty:0.00}sh@${pActualPrice:0.0000} | cost=${actualCost:0.00} " +
                    $"actualNet={actualNetPerSet:0.0000} projProfit=${actualProfit:0.00}");
            }
            else
            {
                Console.ForegroundColor = ConsoleColor.Yellow;
                Console.WriteLine(
                    $"[EXEC SLIPPAGE] {pair.Label} | K={balancedQty}@{kPriceCents}¢ | " +
                    $"P={balancedQty:0.00}sh@${pActualPrice:0.0000} | cost=${actualCost:0.00} " +
                    $"actualNet={actualNetPerSet:0.0000} detectedNet={netNow:0.0000} — arb eaten by slippage");
            }
            Console.ResetColor();
        }

        if (kUnhedged > 0 || pUnhedged > 0)
        {
            // One side filled more than the other — unhedged delta requires hedge-or-reverse.
            // TODO: implement hedge-or-reverse recovery (see todo.md)
            Console.ForegroundColor = ConsoleColor.Red;
            Console.WriteLine(
                $"[EXEC UNHEDGED] {pair.Label} | " +
                $"kFilled={kFilled} pFilled={pFilled:0.00} balanced={balancedQty} " +
                $"kExcess={kUnhedged} pExcess={pUnhedged:0.00} — hedge-or-reverse not yet implemented");
            Console.ResetColor();
        }
        else if (neitherFilled)
        {
            Console.WriteLine($"[EXEC MISS] {pair.Label} | Neither leg filled. k-status={kStatus}");
        }

        // Release exposure only when nothing filled at all.
        // Unhedged delta keeps exposure tracked until recovery resolves it.
        if (neitherFilled)
            lock (_exposureLock) { _totalExposure -= estimatedCost; }

        if (!neitherFilled)
        {
            // Re-fetch real balances after any execution — replaces speculative reservation with actuals.
            await RefreshBalancesAsync();
        }
        else
        {
            // Nothing filled — restore speculative balance reservation in full.
            lock (_balanceLock) { _kalshiBalanceUsd += kalshiCost; _polyBalanceUsd += polyCost; }
        }

        EnqueueCsvRow(pair, arbType, t0, kPriceCents, kLegAsk, pLegAsk,
                      kFilled, pFilled, pActualPrice, netNow, kStatus);
    }

    // ── Kalshi IOC leg ────────────────────────────────────────────────────────

    private async Task<(string OrderId, string Status, decimal FillCount)> PlaceKalshiLegAsync(
        string ticker, string side, int priceCents, int count)
    {
        DebugLog.Trades($"PlaceKalshiLegAsync: {ticker} {side} {priceCents}¢ × {count}");
        try
        {
            var (orderId, status, fillImm) = await _kalshi.PlaceOrderAsync(ticker, side, priceCents, count);
            DebugLog.Trades($"PlaceKalshiLegAsync: placed orderId={orderId} status={status} fillImm={fillImm}");

            if (status == "executed" || fillImm >= count)
                return (orderId, status, fillImm);

            if (string.IsNullOrEmpty(orderId))
            {
                DebugLog.Trades($"PlaceKalshiLegAsync: empty orderId with status={status} — not polling");
                return ("", status, 0m);
            }

            // Poll until resolved or fill timeout
            using var cts = new CancellationTokenSource(_fillTimeoutMs);
            int polls = 0;
            while (!cts.Token.IsCancellationRequested)
            {
                await Task.Delay(50).ConfigureAwait(false);
                var (pollStatus, pollFill) = await _kalshi.PollOrderAsync(orderId);
                polls++;
                DebugLog.Trades($"PlaceKalshiLegAsync: poll #{polls} orderId={orderId} status={pollStatus} fill={pollFill}");
                if (pollStatus == "executed" || pollStatus == "canceled")
                    return (orderId, pollStatus, pollFill);
            }

            DebugLog.Trades($"PlaceKalshiLegAsync: timeout after {polls} polls, orderId={orderId} fillImm={fillImm}");
            return (orderId, "timeout", fillImm);
        }
        catch (Exception ex)
        {
            Console.WriteLine($"[KALSHI LEG ERROR] {ticker}: {ApiErrorHelper.ClassifyKalshi(ex)}");
            DebugLog.Trades($"PlaceKalshiLegAsync exception for {ticker}: {ex}");
            return ("", "error", 0m);
        }
    }

    // ── Polymarket FAK leg ────────────────────────────────────────────────────
    // Routes through POLY_PROXY_ADDRESS (Gnosis Safe) via PolymarketOrderClient —
    // identical EIP-712 POLY_GNOSIS_SAFE signing as PredictionLiveProduction.

    // Mirrors TryExtractFillFromResponse in PolymarketLiveBroker.
    // BUY:  takingAmount = shares received, makingAmount = USDC spent
    // SELL: takingAmount = USDC received,  makingAmount = shares sold
    private static (decimal Shares, decimal Dollars) ExtractPolyFill(JsonElement root, bool isSell)
    {
        decimal takingVal = 0m, makingVal = 0m;
        if (root.TryGetProperty("takingAmount", out var ta) || root.TryGetProperty("taking_amount", out ta))
        {
            string? v = ta.ValueKind == JsonValueKind.String ? ta.GetString() : ta.ToString();
            if (v != null && !decimal.TryParse(v, out takingVal)) takingVal = 0m;
        }
        if (root.TryGetProperty("makingAmount", out var ma) || root.TryGetProperty("making_amount", out ma))
        {
            string? v = ma.ValueKind == JsonValueKind.String ? ma.GetString() : ma.ToString();
            if (v != null && !decimal.TryParse(v, out makingVal)) makingVal = 0m;
        }
        return isSell ? (makingVal, takingVal) : (takingVal, makingVal);
    }

    private async Task<(decimal FilledShares, decimal AvgPrice)> PlacePolyLegAsync(
        string tokenId, decimal price, decimal shares)
    {
        string tokenShort = tokenId[..Math.Min(12, tokenId.Length)];
        DebugLog.Trades($"PlacePolyLegAsync: token={tokenShort}... price={price:0.0000} shares={shares}");
        try
        {
            decimal limitPrice = Math.Min(0.99m, price);
            DebugLog.Trades($"PlacePolyLegAsync: limitPrice={limitPrice:0.0000} (evaluated arb price)");

            string result = "";
            int feeRate = 0;
            for (int attempt = 0; attempt < 2; attempt++)
            {
                try
                {
                    DebugLog.Trades($"PlacePolyLegAsync: attempt {attempt + 1} feeRateBps={feeRate}");
                    result = await _poly.SubmitOrderAsync(
                        tokenId, limitPrice, shares, side: 0 /*BUY*/,
                        negRisk: false, feeRateBps: feeRate);
                    break;
                }
                catch (Exception ex) when (
                    ex.Message.Contains("invalid fee rate") &&
                    ex.Message.Contains("taker fee:"))
                {
                    var m = Regex.Match(ex.Message, @"taker fee:\s*(\d+)");
                    if (m.Success && int.TryParse(m.Groups[1].Value, out int fee))
                    {
                        DebugLog.Trades($"PlacePolyLegAsync: fee autocorrect — retrying with feeRateBps={fee}");
                        feeRate = fee;
                    }
                    else
                        throw;
                }
            }

            if (string.IsNullOrEmpty(result))
            {
                DebugLog.Trades($"PlacePolyLegAsync: empty result from SubmitOrderAsync");
                return (0m, 0m);
            }

            using var doc = JsonDocument.Parse(result);
            var root = doc.RootElement;

            if (!root.TryGetProperty("success", out var sv) || !sv.GetBoolean())
            {
                DebugLog.Trades($"PlacePolyLegAsync: success=false in response — {result[..Math.Min(200, result.Length)]}");
                return (0m, 0m);
            }

            string orderId = root.TryGetProperty("orderID", out var oidEl) ? oidEl.GetString() ?? "" : "";
            string respStatus = root.TryGetProperty("status", out var stEl) ? stEl.GetString() ?? "" : "";
            DebugLog.Trades($"PlacePolyLegAsync: orderID={orderId} status={respStatus}");

            // BUY: takingAmount = shares received, makingAmount = USDC spent
            (decimal filledShares, decimal spentUsdc) = ExtractPolyFill(root, isSell: false);
            DebugLog.Trades($"PlacePolyLegAsync: response fill — shares={filledShares} spent={spentUsdc}");

            // REST poll fallback — production uses this when the POST response doesn't carry fill
            // amounts (e.g. status=delayed). FAK should fill immediately, but poll once to be safe.
            if (filledShares <= 0 && !string.IsNullOrEmpty(orderId))
            {
                DebugLog.Trades($"PlacePolyLegAsync: no fill in response — polling orderID={orderId}");
                try
                {
                    string pollResult = await _poly.GetOrderAsync(orderId);
                    using var pollDoc = JsonDocument.Parse(pollResult);
                    var pollRoot = pollDoc.RootElement;
                    JsonElement orderData = pollRoot.ValueKind == JsonValueKind.Array && pollRoot.GetArrayLength() > 0
                        ? pollRoot[0] : pollRoot;

                    string pollStatus = orderData.TryGetProperty("status", out var ps) ? ps.GetString() ?? "" : "";
                    DebugLog.Trades($"PlacePolyLegAsync: poll status={pollStatus}");

                    if (pollStatus == "matched" || pollStatus == "live")
                    {
                        // Poll response uses size_matched / taker_amount_matched (different field names)
                        if (orderData.TryGetProperty("size_matched", out var smEl) &&
                            decimal.TryParse(smEl.ToString(), out decimal sm) && sm > 0)
                            filledShares = sm;
                        else if (orderData.TryGetProperty("taker_amount_matched", out var takerEl) &&
                            decimal.TryParse(takerEl.ToString(), out sm) && sm > 0)
                            filledShares = sm;

                        if (orderData.TryGetProperty("maker_amount_matched", out var makerEl) &&
                            decimal.TryParse(makerEl.ToString(), out decimal md) && md > 0)
                            spentUsdc = md;

                        DebugLog.Trades($"PlacePolyLegAsync: poll fill — shares={filledShares} spent={spentUsdc}");
                    }
                }
                catch (Exception pollEx)
                {
                    DebugLog.Trades($"PlacePolyLegAsync: poll failed for orderID={orderId}: {pollEx.Message}");
                }
            }

            if (filledShares <= 0)
            {
                DebugLog.Trades($"PlacePolyLegAsync: filledShares=0 after response+poll — FAK killed or no liquidity");
                return (0m, 0m);
            }

            decimal avgPrice = spentUsdc > 0 ? spentUsdc / filledShares : price;
            DebugLog.Trades($"PlacePolyLegAsync: filled={filledShares} avgPrice={avgPrice:0.0000}");
            return (filledShares, avgPrice);
        }
        catch (Exception ex)
        {
            Console.WriteLine($"[POLY LEG ERROR] {tokenShort}...: {ApiErrorHelper.ClassifyPoly(ex)}");
            DebugLog.Trades($"PlacePolyLegAsync exception for {tokenShort}: {ex}");
            return (0m, 0m);
        }
    }

    // ── CSV ───────────────────────────────────────────────────────────────────

    private void EnqueueCsvRow(CrossPair pair, string arbType, DateTime t,
        int kPriceCents, decimal kLegAsk, decimal pLegAsk,
        decimal kFilled, decimal pFilled, decimal pPrice, decimal netCost, string kStatus)
    {
        if (!_headerWritten)
        {
            _headerWritten = true;
            _csvChannel.Writer.TryWrite(
                "Time,PairId,Label,ArbType,KalshiTicker,PolyToken," +
                "KPriceCents,KLegAsk,PLegAsk,NetCost," +
                "KFilled,PFilled,PAvgPrice,KStatus,DryRun");
        }
        string row = string.Join(",",
            t.ToString("yyyy-MM-dd HH:mm:ss.fff"),
            Quote(pair.PairId),
            Quote(pair.Label),
            arbType,
            pair.KalshiTicker,
            Quote(arbType == "K_YES_P_NO" ? pair.PolyNoTokenId : pair.PolyYesTokenId),
            kPriceCents,
            kLegAsk.ToString("0.0000"),
            pLegAsk.ToString("0.0000"),
            netCost.ToString("0.0000"),
            kFilled.ToString("0.00"),
            pFilled.ToString("0.00"),
            pPrice.ToString("0.0000"),
            kStatus,
            _dryRun ? "1" : "0"
        );
        _csvChannel.Writer.TryWrite(row);
    }

    private static string Quote(string s) => $"\"{s.Replace("\"", "\"\"")}\"";

    private async Task RunCsvWriterAsync()
    {
        try
        {
            using var sw = new StreamWriter(_csvPath, append: false, Encoding.UTF8) { AutoFlush = false };
            await foreach (var line in _csvChannel.Reader.ReadAllAsync())
            {
                await sw.WriteLineAsync(line);
                await sw.FlushAsync();
            }
        }
        catch (Exception ex) { Console.WriteLine($"[EXEC CSV ERROR] {ex.Message}"); }
    }

    public async Task ShutdownAsync()
    {
        _cts.Cancel();
        _cts.Dispose();
        _csvChannel.Writer.TryComplete();
        // Allow the writer task to drain
        await Task.Delay(200);
    }
}
