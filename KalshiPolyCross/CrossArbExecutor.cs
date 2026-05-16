using System.Collections.Concurrent;
using System.Text;
using System.Text.Json;
using System.Text.RegularExpressions;
using System.Threading.Channels;
using PredictionBacktester.Engine;
using PredictionBacktester.Engine.LiveExecution;

namespace KalshiPolyCross;

public record ReconciliationEntry(
    string PairId, string Label, string Status,
    decimal KalshiQty, decimal PolyQty, string Notes);

public record ArbPosition(
    string   PairId,
    string   ArbType,
    decimal  KalshiContracts,
    decimal  PolyShares,
    decimal  KalshiEntryPrice,
    decimal  PolyEntryPrice,
    DateTime EntryTime,
    string   ExecId           // trace ID — correlates all journal events for this trade
);

// Summary of what RecoverUnhedgedAsync did with the unhedged delta.
public record RecoveryResult(
    string  Outcome,        // HEDGE_COMPLETED | REVERSED_KALSHI | REVERSED_POLY | DUST_ABSORBED_KALSHI | DUST_ABSORBED_POLY | HALT
    decimal RecoveredQty,   // contracts/shares successfully hedged or reversed
    decimal LossUsd         // realized loss from the cleanup action
);

/// <summary>
/// Fires simultaneous IOC/FAK orders on both legs when CrossPlatformArbTelemetryStrategy
/// detects a cross-platform arb window. Kalshi leg uses IOC via PlaceOrderAsync;
/// Polymarket leg uses FAK via PolymarketOrderClient (POLY_GNOSIS_SAFE signing).
/// </summary>
public class CrossArbExecutor
{
    private readonly IKalshiOrderExecutor _kalshi;
    private readonly IPolymarketOrderExecutor _poly;
    private readonly CrossPlatformArbTelemetryStrategy _telemetry;
    private readonly ConcurrentDictionary<string, LocalOrderBook> _books;

    // ── Early exit tuning ─────────────────────────────────────────────────────
    // Triggered on every book update for pairs with an open position (event-driven),
    // with a 60 s fallback timer in case a book update is missed.
    // EarlyExitThreshold: fraction of expected settlement profit required to exit early.
    //   0   = never exit early (hold to settlement)
    //   0.5 = exit when unrealized PnL ≥ 50 % of expected profit   ← default
    //   1.0 = only exit when full settlement value is available on the bid
    private static decimal  EarlyExitThreshold         = 0.50m;
    private const  decimal  EarlyExitMinProfitUsd      = 0.05m;  // skip micro-exits below this
    private const  int      EarlyExitFallbackIntervalMs = 60_000; // fallback poll if a book update was missed

    // ── Configuration ─────────────────────────────────────────────────────────
    private readonly decimal _maxBetUsd;           // max combined dollar cost per arb entry
    private readonly decimal _balanceBufferPct;    // fraction of maxBetUsd kept as per-platform reserve
    private readonly decimal _maxExposureUsd;
    private readonly bool    _minBuy;              // --min-buy: cap every arb to exactly 1 contract
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
    private          int     _earlyExitsCompleted  = 0;
    private          int     _triesRemaining       = -1;  // -1 = unlimited
    private          int     _tryLimit             = -1;  // original N, for display
    private          CancellationTokenSource? _outerCts;
    private readonly ConcurrentDictionary<string, byte>    _inFlight          = new();
    private readonly ConcurrentDictionary<string, byte>    _earlyExitScheduled = new();
    private readonly ConcurrentDictionary<string, decimal> _perPairInvested = new();
    private readonly HashSet<string>                        _blocklist       = new(StringComparer.OrdinalIgnoreCase);
    private          int _kalshiConsecErrors = 0;
    private          int _polyConsecErrors   = 0;
    private const    int MaintenanceErrorThreshold = 5;
    private volatile bool   _halted               = false; // manual reset required (failed reversal / tripwire)
    private volatile bool   _connectionHalted     = false; // auto-clears when both venues reconnect
    private const    int    ReverseBufferCents           = 2;  // extra slippage tolerance for reversal orders
    private const    int    RecoveryHedgeSlippageCents   = 2;  // extra slippage tolerance for recovery hedge retries
    private const  decimal  TradeMaxLossMult       = 3.0m; // halt if actual loss > 3× expected edge
    private const  decimal  CleanupHedgeSkipUsd   = 1.00m; // skip hedge attempt if unhedged value < $1.00
    private const  decimal  CleanupDustUsd         = 0.25m; // absorb silently (no halt) if reversal fails and value < $0.25

    // ── Position scaling ──────────────────────────────────────────────────────
    // AllowScaleIn = false: one position per pair (hold to settlement/exit).
    // AllowScaleIn = true:  allow additional entries while a position is open,
    //                       up to MaxPerPairExposureUsd total across all fills.
    private const  bool     AllowScaleIn           = false;
    private const  decimal  MaxPerPairExposureUsd  = 200m;

    private          decimal  _dayLossUsd          = 0m;
    private          DateOnly _dayStart            = DateOnly.FromDateTime(DateTime.UtcNow);
    private readonly decimal  _maxDayLossUsd;
    private readonly object   _dayLossLock         = new();
    private          decimal  _totalCleanupCostUsd = 0m;
    private readonly object   _cleanupLock         = new();
    private          decimal  _dailyModeledFeesUsd = 0m;
    private          decimal  _dailyNetVarUsd       = 0m;
    private          int      _dailyTradeCount      = 0;
    private          DateOnly _feeTrackingDay       = DateOnly.FromDateTime(DateTime.UtcNow);
    private readonly object   _feeTrackingLock      = new();

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

    // ── Trade journal ─────────────────────────────────────────────────────────
    private readonly string        _journalPath = $"CrossArbJournal_{DateTime.UtcNow:yyyyMMdd}.jsonl";
    private readonly SemaphoreSlim _journalLock = new(1, 1);

    // ── CSV ───────────────────────────────────────────────────────────────────
    private readonly Channel<string> _csvChannel =
        Channel.CreateUnbounded<string>(new UnboundedChannelOptions { SingleReader = true });
    private readonly string _csvPath;
    private bool _headerWritten;
    private Task _csvWriterTask = Task.CompletedTask;

    public int     OpenPositionCount    => _openPositions.Count(kv => kv.Value != null);
    public decimal MaxExposureUsd       => _maxExposureUsd;
    public int     TotalExecuted        => Volatile.Read(ref _totalExecuted);
    public int     EarlyExitsCompleted  => Volatile.Read(ref _earlyExitsCompleted);
    public int     TriesRemaining       => Volatile.Read(ref _triesRemaining);  // -1 = unlimited
    public bool    IsHalted             => _halted;
    public bool    IsConnectionHalted   => _connectionHalted;
    public decimal DayLossUsd           { get { lock (_dayLossLock) return _dayLossUsd;    } }
    public decimal MaxDayLossUsd        => _maxDayLossUsd;
    public decimal TotalCleanupCostUsd  { get { lock (_cleanupLock) return _totalCleanupCostUsd; } }
    public decimal KalshiBalanceUsd     { get { lock (_balanceLock)  return _kalshiBalanceUsd;        } }
    public decimal PolyBalanceUsd       { get { lock (_balanceLock)  return _polyBalanceUsd;          } }
    public decimal TotalExposure        { get { lock (_exposureLock) return _totalExposure;           } }
    public decimal TotalInvested        { get { lock (_exposureLock) return _totalInvested;           } }
    public decimal TotalProjectedProfit { get { lock (_exposureLock) return _totalProjectedProfit;    } }

    public CrossArbExecutor(
        IKalshiOrderExecutor            kalshi,
        IPolymarketOrderExecutor        poly,
        CrossPlatformArbTelemetryStrategy telemetry,
        ConcurrentDictionary<string, LocalOrderBook> books,
        decimal maxBetUsd           = 10m,
        decimal balanceBufferPct    = 0.20m,
        decimal maxExposureUsd      = 10m,
        decimal executionThreshold  = 0.990m,
        int     pairCooldownSeconds = 120,
        int     fillTimeoutMs       = 5000,
        decimal maxDayLossUsd            = 20m,
        bool    dryRun                   = false,
        bool    minBuy                   = false,
        int?    tryN                = null,
        CancellationTokenSource? outerCts = null)
    {
        _kalshi              = kalshi;
        _poly                = poly;
        _telemetry           = telemetry;
        _books               = books;
        _maxBetUsd           = maxBetUsd;
        _balanceBufferPct    = balanceBufferPct;
        _maxExposureUsd      = maxExposureUsd;
        _minBuy              = minBuy;
        _executionThreshold  = executionThreshold;
        _pairCooldownSeconds = pairCooldownSeconds;
        _fillTimeoutMs       = fillTimeoutMs;
        _maxDayLossUsd       = maxDayLossUsd;
        _dryRun              = dryRun;
        _triesRemaining      = tryN ?? -1;
        _tryLimit            = tryN ?? -1;
        _outerCts            = outerCts;
        _csvPath             = $"CrossArbExecution_{DateTime.UtcNow:yyyyMMdd_HHmmss}.csv";
        _csvWriterTask = Task.Run(RunCsvWriterAsync);
        var journalDir = Path.GetDirectoryName(Path.GetFullPath(_journalPath));
        if (journalDir != null) Directory.CreateDirectory(journalDir);

        // Load pair blocklist written by prod_cross_arb.py (pair-mismatch settlements)
        string blPath = Path.Combine(AppContext.BaseDirectory, "cross_pair_blocklist.json");
        if (!File.Exists(blPath))
            blPath = Path.Combine(AppContext.BaseDirectory, "..", "..", "..", "cross_pair_blocklist.json");
        if (File.Exists(blPath))
        {
            var tickers = JsonSerializer.Deserialize<List<string>>(File.ReadAllText(blPath)) ?? [];
            foreach (var t in tickers) _blocklist.Add(t);
            if (_blocklist.Count > 0)
                Console.WriteLine($"[BLOCKLIST] {_blocklist.Count} pair(s) blocked (cross_pair_blocklist.json)");
        }
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
            _ = Task.Run(RunEarlyExitMonitorAsync);
            return;
        }
        await RefreshBalancesAsync(initial: true);
        _ = Task.Run(PeriodicBalanceRefreshLoop);
        _ = Task.Run(RunEarlyExitMonitorAsync);
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
        if (_halted || _connectionHalted)
        {
            DebugLog.Trades($"ExecuteAsync {pairId}: skipped — {(_halted ? "bot halted (manual reset required)" : "connection halted")}");
            return;
        }
        // Per-pair in-flight guard: prevents two concurrent OnArbOpened callbacks from
        // both passing the cooldown/open-position check before either one sets the cooldown.
        if (!_inFlight.TryAdd(pairId, 0))
        {
            DebugLog.Trades($"ExecuteAsync {pairId}: skipped — already in-flight");
            return;
        }
        try   { await ExecuteLockedAsync(pairId, arbType); }
        finally { _inFlight.TryRemove(pairId, out _); }
    }

    private async Task ExecuteLockedAsync(string pairId, string arbType)
    {
        long now = DateTimeOffset.UtcNow.ToUnixTimeSeconds();

        // Guard: cooldown or open position on this pair
        if (_cooldownUntil.TryGetValue(pairId, out long cd) && now < cd)
        {
            DebugLog.Trades($"ExecuteAsync {pairId}: skipped — cooldown active for {cd - now}s more");
            return;
        }
        if (!AllowScaleIn && _openPositions.ContainsKey(pairId))
        {
            DebugLog.Trades($"ExecuteAsync {pairId}: skipped — open position already tracked (AllowScaleIn=false)");
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
        double  venueSkewMs = 0;

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

        // Venue time-skew: significant drift means one book is stale — log and journal
        {
            DateTime kLastDelta = (arbType == "K_YES_P_NO" ? kYes : kNo).LastDeltaAt;
            DateTime pLastDelta = (arbType == "K_YES_P_NO" ? pNo  : pYes).LastDeltaAt;
            venueSkewMs = Math.Abs((kLastDelta - pLastDelta).TotalMilliseconds);
            if (venueSkewMs > 500 && kLastDelta.Ticks > 0 && pLastDelta.Ticks > 0)
            {
                Console.WriteLine(
                    $"[TIME SKEW] {pair.Label} | K last={kLastDelta:HH:mm:ss.fff} " +
                    $"P last={pLastDelta:HH:mm:ss.fff} skew={venueSkewMs:0}ms — stale book may distort arb");
                await JournalAsync(JsonSerializer.Serialize(new {
                    t = DateTime.UtcNow, @event = "TIME_SKEW",
                    pairId, arbType,
                    venueSkewMs = Math.Round(venueSkewMs, 1),
                    kLastDelta, pLastDelta
                }));
            }
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
        if (_minBuy && contracts > 1) contracts = 1;  // --min-buy: trade bare minimum regardless of maxBet
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

        string execId = string.Empty;

        // Blocklist check — pairs flagged by prod_cross_arb.py for pair mismatch at settlement
        if (_blocklist.Contains(pair.KalshiTicker))
        {
            lock (_balanceLock) { _kalshiBalanceUsd += kalshiCost; _polyBalanceUsd += polyCost; }
            return;
        }

        // Per-pair position limit — prevent a single bad pair from dominating bankroll
        decimal pairInvested = _perPairInvested.GetOrAdd(pairId, 0m);
        if (pairInvested + estimatedCost > MaxPerPairExposureUsd)
        {
            Console.WriteLine(
                $"[EXEC SKIP] {pair.Label} | Per-pair limit " +
                $"${pairInvested:0.00}+${estimatedCost:0.00} > ${MaxPerPairExposureUsd:0.00}");
            lock (_balanceLock) { _kalshiBalanceUsd += kalshiCost; _polyBalanceUsd += polyCost; }
            return;
        }

        // Exposure check (thread-safe)
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
        Console.WriteLine(_dryRun
            ? $"[DRY RUN EXEC] {pair.Label} | {arbType} | K-{kalshiSide}={kLegAsk:0.0000} " +
              $"P={pLegAsk:0.0000} net=${netNow:0.0000} | {contracts} contracts est.${estimatedCost:0.00}"
            : $"[EXEC] {pair.Label} | {arbType} | K-{kalshiSide}={kLegAsk:0.0000} " +
              $"P={pLegAsk:0.0000} net=${netNow:0.0000} | {contracts} contracts @ ${estimatedCost:0.00}");
        Console.ResetColor();

        var t0 = DateTime.UtcNow;
        execId = $"AX_{t0:yyyyMMddHHmmss}_{pair.KalshiTicker}";

        // Durable intent record — written before any order is sent
        await JournalAsync(JsonSerializer.Serialize(new {
            t = t0, @event = "INTENT", execId,
            pairId, arbType, kSide = kalshiSide,
            kAsk = kLegAsk, pAsk = pLegAsk, netDetected = netNow,
            contracts, estCost = estimatedCost,
            dryRun = _dryRun
        }));

        // Fire both legs simultaneously
        var kalshiTask = PlaceKalshiLegAsync(pair.KalshiTicker, kalshiSide, kPriceCents, contracts, execId);
        var polyTask   = PlacePolyLegAsync(polyToken, pLegAsk, polyShares, pair.IsNegRisk);
        await Task.WhenAll(kalshiTask, polyTask);

        var (kOrderId, kStatus, kFilled) = kalshiTask.Result;
        var (pFilled, pActualPrice)       = polyTask.Result;

        // Balanced quantity: the portion of each leg that is fully hedged.
        // Any excess on one side is an unhedged delta requiring recovery.
        decimal balancedQty  = Math.Min(kFilled, pFilled);
        decimal kUnhedged    = kFilled - balancedQty;  // excess Kalshi contracts
        decimal pUnhedged    = pFilled - balancedQty;  // excess Poly shares
        bool    neitherFilled   = kFilled == 0 && pFilled == 0;
        decimal actualNetPerSet = 0m;

        // Stale price: pre-check passed but one leg's limit wasn't met — price moved in flight
        bool staleSuspected = !neitherFilled && (kFilled == 0 || pFilled == 0);
        if (staleSuspected)
        {
            Console.ForegroundColor = ConsoleColor.DarkYellow;
            Console.WriteLine(
                $"[STALE PRICE] {pair.Label} | detectedNet={netNow:0.0000} " +
                $"kFilled={kFilled} pFilled={pFilled:0.00} — one leg missed limit, price may have moved");
            Console.ResetColor();
            await JournalAsync(JsonSerializer.Serialize(new {
                t = DateTime.UtcNow, @event = "STALE_PRICE",
                pairId, arbType, detectedNet = netNow,
                kFilled, pFilled = (double)pFilled, dryRun = _dryRun
            }));
        }

        if (balancedQty > 0)
        {
            decimal actualCost  = kLegAsk * balancedQty + pActualPrice * balancedQty;
            // Actual net cost per set using confirmed fill prices + fees.
            // kLegAsk is the IOC limit price (fills at or below this). pActualPrice is Poly's
            // reported average fill price and may exceed pLegAsk if the book was walked.
            actualNetPerSet = kLegAsk + pActualPrice + KalshiFee(kLegAsk) + PolyFee(pActualPrice);
            bool    arbProfitable   = actualNetPerSet < 1.0m;
            decimal actualProfit    = balancedQty * (1.0m - actualNetPerSet);

            _openPositions[pairId] = new ArbPosition(
                pairId, arbType, balancedQty, balancedQty, kLegAsk, pActualPrice, t0, execId);
            Interlocked.Increment(ref _totalExecuted);
            DecrementTryLimit();
            lock (_exposureLock) { _totalInvested += actualCost; _totalProjectedProfit += actualProfit; }
            _perPairInvested.AddOrUpdate(pairId, actualCost, (_, old) => old + actualCost);

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

            // ── Per-trade max loss tripwire ───────────────────────────────────
            if (actualNetPerSet > 1.0m)
            {
                decimal expectedEdge = 1.0m - netNow;
                decimal actualLoss   = actualNetPerSet - 1.0m;
                if (actualLoss > TradeMaxLossMult * expectedEdge)
                {
                    await JournalAsync(JsonSerializer.Serialize(new {
                        t = DateTime.UtcNow, @event = "HALT_TRIPWIRE",
                        reason = "per_trade_loss", pairId, dryRun = _dryRun,
                        actualNet = actualNetPerSet, detectedNet = netNow,
                        maxAllowed = 1.0m + TradeMaxLossMult * expectedEdge
                    }));
                    Console.ForegroundColor = ConsoleColor.Red;
                    Console.WriteLine(
                        $"[HALT] {pair.Label} | Per-trade tripwire: loss={actualLoss:0.0000} > " +
                        $"{TradeMaxLossMult}× edge={expectedEdge:0.0000}. Manual reset required.");
                    Console.ResetColor();
                    _halted = true;
                }

                // ── Per-day max loss tripwire ─────────────────────────────────
                decimal tradeLoss = actualLoss * balancedQty;
                bool dayHalt = false;
                lock (_dayLossLock)
                {
                    DateOnly today = DateOnly.FromDateTime(DateTime.UtcNow);
                    if (today > _dayStart) { _dayLossUsd = 0m; _dayStart = today; }
                    _dayLossUsd += tradeLoss;
                    dayHalt = _dayLossUsd >= _maxDayLossUsd;
                }
                if (dayHalt && !_halted)
                {
                    await JournalAsync(JsonSerializer.Serialize(new {
                        t = DateTime.UtcNow, @event = "HALT_TRIPWIRE",
                        reason = "per_day_loss", pairId, dryRun = _dryRun,
                        dayLoss = _dayLossUsd, maxDayLoss = _maxDayLossUsd
                    }));
                    Console.ForegroundColor = ConsoleColor.Red;
                    Console.WriteLine(
                        $"[HALT] Per-day tripwire: cumulative loss ${_dayLossUsd:0.00} >= " +
                        $"max ${_maxDayLossUsd:0.00}. Manual reset required.");
                    Console.ResetColor();
                    _halted = true;
                }
            }

            // ── Fee model tracking ────────────────────────────────────────────────
            decimal modeledFees = (KalshiFee(kLegAsk) + PolyFee(pLegAsk)) * balancedQty;
            decimal netVar      = (actualNetPerSet - netNow) * balancedQty;
            bool emitDailyReport = false;
            decimal reportModeled = 0m, reportNetVar = 0m;
            int reportCount = 0;
            lock (_feeTrackingLock)
            {
                DateOnly today = DateOnly.FromDateTime(DateTime.UtcNow);
                if (today > _feeTrackingDay)
                {
                    emitDailyReport      = true;
                    reportModeled        = _dailyModeledFeesUsd;
                    reportNetVar         = _dailyNetVarUsd;
                    reportCount          = _dailyTradeCount;
                    _dailyModeledFeesUsd = 0m;
                    _dailyNetVarUsd      = 0m;
                    _dailyTradeCount     = 0;
                    _feeTrackingDay      = today;
                }
                _dailyModeledFeesUsd += modeledFees;
                _dailyNetVarUsd      += netVar;
                _dailyTradeCount++;
            }
            if (emitDailyReport)
            {
                decimal drift = reportModeled > 0 ? Math.Abs(reportNetVar) / reportModeled : 0m;
                await JournalAsync(JsonSerializer.Serialize(new {
                    t = DateTime.UtcNow, @event = "DAILY_REPORT", dryRun = _dryRun,
                    trades = reportCount, modeledFeesUsd = reportModeled,
                    netVarUsd = reportNetVar, driftPct = (double)(drift * 100)
                }));
                if (drift > 0.10m)
                {
                    Console.ForegroundColor = ConsoleColor.Yellow;
                    Console.WriteLine(
                        $"[FEE MODEL DRIFT] Previous day: {reportCount} trades | " +
                        $"modeled=${reportModeled:0.00} var=${reportNetVar:+0.00;-0.00} drift={drift:0.0%} — audit recommended");
                    Console.ResetColor();
                }
                else
                    Console.WriteLine(
                        $"[DAILY REPORT] {reportCount} trades | " +
                        $"modeled fees=${reportModeled:0.00} cost_var=${reportNetVar:+0.00;-0.00} drift={drift:0.0%}");
            }
        }

        RecoveryResult? recovery = null;
        if (kUnhedged > 0 || pUnhedged > 0)
        {
            Console.WriteLine(
                $"[EXEC UNHEDGED] {pair.Label} | " +
                $"kFilled={kFilled} pFilled={pFilled:0.00} balanced={balancedQty} " +
                $"kExcess={kUnhedged} pExcess={pUnhedged:0.00} — starting recovery");
            recovery = await RecoverUnhedgedAsync(pair, arbType, kalshiSide, polyToken,
                kFilled, pFilled, kLegAsk, pActualPrice, execId);
        }
        else if (neitherFilled)
        {
            Console.WriteLine($"[EXEC MISS] {pair.Label} | Neither leg filled. k-status={kStatus}");
        }

        await JournalAsync(JsonSerializer.Serialize(new {
            t = DateTime.UtcNow,
            @event = neitherFilled ? "MISS" : "FILLED",
            pairId, kFilled, pFilled = (double)pFilled,
            balanced = balancedQty, kStatus, dryRun = _dryRun,
            modeledNet = balancedQty > 0 ? (object)Math.Round(netNow, 6)          : null,
            actualNet  = balancedQty > 0 ? (object)Math.Round(actualNetPerSet, 6) : null
        }));

        // ── Execution complete — single comprehensive record of the full trade ──
        {
            string execOutcome = neitherFilled                           ? "MISS"
                : recovery?.Outcome.StartsWith("HALT") == true          ? "HALTED"
                : recovery != null && recovery.Outcome != "NONE"        ? "FILLED_WITH_CLEANUP"
                : "FILLED";

            // True final position after all cleanup (hedge add or reversal may have changed qty)
            var finalPos  = _openPositions.TryGetValue(pairId, out var fp) ? fp : null;
            decimal kHeld = finalPos?.KalshiContracts ?? 0m;
            decimal pHeld = finalPos?.PolyShares      ?? 0m;

            await JournalAsync(JsonSerializer.Serialize(new {
                t = DateTime.UtcNow, @event = "EXECUTION_COMPLETE",
                execId, pairId, arbType, label = pair.Label, dryRun = _dryRun,

                detected = new {
                    kAsk = kLegAsk, pAsk = pLegAsk,
                    netCost = Math.Round(netNow, 6),
                    contracts, estCostUsd = Math.Round(estimatedCost, 4),
                    venueSkewMs = Math.Round(venueSkewMs, 1)
                },

                fills = new {
                    kalshi = new {
                        ordered = contracts, filled = kFilled,
                        limitCents = kPriceCents, fillPrice = kLegAsk,
                        feePerContract = Math.Round(KalshiFee(kLegAsk), 6),
                        status = kStatus
                    },
                    poly = new {
                        ordered = polyShares, filled = Math.Round(pFilled, 6),
                        limitPrice = pLegAsk,
                        avgFillPrice = pFilled > 0 ? Math.Round(pActualPrice, 6) : (object?)null,
                        feePerShare  = pFilled > 0 ? (object?)Math.Round(PolyFee(pActualPrice), 6) : null,
                        slippagePct  = pFilled > 0 && pLegAsk > 0
                            ? (object?)Math.Round((pActualPrice - pLegAsk) / pLegAsk * 100m, 4) : null
                    }
                },

                hedge = new {
                    balanced = balancedQty, kExcess = kUnhedged, pExcess = pUnhedged,
                    recovery = recovery == null || recovery.Outcome == "NONE" ? null : (object?)new {
                        outcome = recovery.Outcome,
                        qty     = Math.Round(recovery.RecoveredQty, 6),
                        lossUsd = Math.Round(recovery.LossUsd, 6)
                    }
                },

                position = kHeld > 0 ? (object?)new {
                    kHeld, pHeld,
                    kEntryPrice = kLegAsk,
                    pAvgPrice   = Math.Round(pActualPrice, 6),
                    totalCostUsd       = Math.Round(kLegAsk * kHeld + pActualPrice * pHeld, 4),
                    modeledNetPerSet   = Math.Round(netNow, 6),
                    actualNetPerSet    = Math.Round(actualNetPerSet, 6),
                    projectedProfitUsd = Math.Round(kHeld * (1.0m - actualNetPerSet), 4)
                } : null,

                outcome = execOutcome,
                stalePriceSuspected = staleSuspected,
                durationMs = (long)(DateTime.UtcNow - t0).TotalMilliseconds
            }));
        }

        // Release exposure only when nothing filled at all.
        // Unhedged delta keeps exposure tracked until recovery resolves it.
        if (neitherFilled)
            lock (_exposureLock) { _totalExposure -= estimatedCost; }

        if (!neitherFilled)
        {
            // Re-fetch real balances after execution — not needed in dry-run (simulated clients
            // return dummy values that would overwrite the executor's tracked simulation balances).
            if (!_dryRun) await RefreshBalancesAsync();
            // Post-trade position reconciliation — skipped in dry-run because the simulated client
            // tracks total fills while the executor tracks balanced fills, causing spurious mismatches.
            if (!_dryRun && balancedQty > 0)
                _ = Task.Run(async () => await ReconcileTradeAsync(pair, arbType, balancedQty, balancedQty, execId));
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
        string ticker, string side, int priceCents, int count, string execId = "")
    {
        string clientId = string.IsNullOrEmpty(execId)
            ? $"CAXARB_{ticker}_{DateTimeOffset.UtcNow.ToUnixTimeMilliseconds()}"
            : $"CAXARB_{execId}";
        DebugLog.Trades($"PlaceKalshiLegAsync: {ticker} {side} {priceCents}¢ × {count} clientId={clientId}");
        try
        {
            var (orderId, status, fillImm) = await _kalshi.PlaceOrderAsync(
                ticker, side, priceCents, count, clientOrderId: clientId);
            Interlocked.Exchange(ref _kalshiConsecErrors, 0);
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

            // Settle window: the IOC may have gained partial fills between our last poll and the
            // timeout. One final REST check makes the fill count authoritative before returning.
            await Task.Delay(200).ConfigureAwait(false);
            var (settleStatus, settleFill) = await _kalshi.PollOrderAsync(orderId);
            DebugLog.Trades($"PlaceKalshiLegAsync: settle-poll after {polls} polls — status={settleStatus} fill={settleFill}");
            return (orderId, settleStatus is "executed" or "canceled" ? settleStatus : "timeout",
                    Math.Max(settleFill, fillImm));
        }
        catch (HttpRequestException ex) when (ex.StatusCode == System.Net.HttpStatusCode.TooManyRequests)
        {
            Console.WriteLine($"[KALSHI RATE LIMIT] {ticker} — 429, retrying in 1s");
            DebugLog.Trades($"PlaceKalshiLegAsync: 429 on {ticker}, backing off 1s");
            await Task.Delay(1_000);
            try
            {
                string retryId = $"CAXARB_{ticker}_{DateTimeOffset.UtcNow.ToUnixTimeMilliseconds()}R";
                var (oid2, st2, fi2) = await _kalshi.PlaceOrderAsync(
                    ticker, side, priceCents, count, clientOrderId: retryId);
                Interlocked.Exchange(ref _kalshiConsecErrors, 0);
                DebugLog.Trades($"PlaceKalshiLegAsync: 429-retry placed oid={oid2} status={st2} fill={fi2}");
                return (oid2, st2, fi2);
            }
            catch (Exception retryEx)
            {
                Console.WriteLine($"[KALSHI LEG ERROR] {ticker} (after 429): {ApiErrorHelper.ClassifyKalshi(retryEx)}");
                DebugLog.Trades($"PlaceKalshiLegAsync: 429 retry failed for {ticker}: {retryEx.Message}");
                await CheckMaintenanceThresholdAsync("kalshi", Interlocked.Increment(ref _kalshiConsecErrors));
                return ("", "error", 0m);
            }
        }
        catch (Exception ex)
        {
            Console.WriteLine($"[KALSHI LEG ERROR] {ticker}: {ApiErrorHelper.ClassifyKalshi(ex)}");
            DebugLog.Trades($"PlaceKalshiLegAsync exception for {ticker}: {ex}");
            await CheckMaintenanceThresholdAsync("kalshi", Interlocked.Increment(ref _kalshiConsecErrors));
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
        string tokenId, decimal price, decimal shares, bool negRisk = false)
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
                        negRisk: negRisk, feeRateBps: feeRate);
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
            Interlocked.Exchange(ref _polyConsecErrors, 0);

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
        catch (HttpRequestException ex) when (ex.StatusCode == System.Net.HttpStatusCode.TooManyRequests)
        {
            Console.WriteLine($"[POLY RATE LIMIT] {tokenShort}... — 429, retrying in 1s");
            DebugLog.Trades($"PlacePolyLegAsync: 429 on {tokenShort}, backing off 1s");
            await Task.Delay(1_000);
            try
            {
                string result2 = await _poly.SubmitOrderAsync(
                    tokenId, Math.Min(0.99m, price), shares, side: 0, negRisk: negRisk, feeRateBps: 0);
                if (!string.IsNullOrEmpty(result2))
                {
                    Interlocked.Exchange(ref _polyConsecErrors, 0);
                    using var doc2 = JsonDocument.Parse(result2);
                    var root2 = doc2.RootElement;
                    if (root2.TryGetProperty("success", out var sv2) && sv2.GetBoolean())
                    {
                        (decimal fs2, decimal su2) = ExtractPolyFill(root2, isSell: false);
                        if (fs2 > 0)
                        {
                            decimal avg2 = su2 > 0 ? su2 / fs2 : price;
                            DebugLog.Trades($"PlacePolyLegAsync: 429-retry filled={fs2} avg={avg2:0.0000}");
                            return (fs2, avg2);
                        }
                    }
                }
            }
            catch (Exception retryEx)
            {
                Console.WriteLine($"[POLY LEG ERROR] {tokenShort}... (after 429): {ApiErrorHelper.ClassifyPoly(retryEx)}");
                DebugLog.Trades($"PlacePolyLegAsync: 429 retry failed for {tokenShort}: {retryEx.Message}");
            }
            await CheckMaintenanceThresholdAsync("poly", Interlocked.Increment(ref _polyConsecErrors));
            return (0m, 0m);
        }
        catch (Exception ex)
        {
            Console.WriteLine($"[POLY LEG ERROR] {tokenShort}...: {ApiErrorHelper.ClassifyPoly(ex)}");
            DebugLog.Trades($"PlacePolyLegAsync exception for {tokenShort}: {ex}");
            await CheckMaintenanceThresholdAsync("poly", Interlocked.Increment(ref _polyConsecErrors));
            return (0m, 0m);
        }
    }

    // ── Poly FAK sell (reversal) ──────────────────────────────────────────────

    private async Task<(decimal SoldShares, decimal AvgPrice)> PlacePolySellAsync(
        string tokenId, decimal shares, bool negRisk = false)
    {
        string tokenShort = tokenId[..Math.Min(12, tokenId.Length)];
        DebugLog.Trades($"PlacePolySellAsync: token={tokenShort}... shares={shares}");
        try
        {
            // FAK sell: 0.01 floor so it matches any buyer; actual fill is at best bid
            string result = await _poly.SubmitOrderAsync(
                tokenId, 0.01m, shares, side: 1 /*SELL*/, negRisk: negRisk, feeRateBps: 0);

            if (string.IsNullOrEmpty(result)) return (0m, 0m);

            using var doc = JsonDocument.Parse(result);
            var root = doc.RootElement;
            if (!root.TryGetProperty("success", out var sv) || !sv.GetBoolean())
            {
                DebugLog.Trades($"PlacePolySellAsync: success=false — {result[..Math.Min(200, result.Length)]}");
                return (0m, 0m);
            }

            // SELL: makingAmount = shares sold, takingAmount = USDC received
            (decimal soldShares, decimal usdcReceived) = ExtractPolyFill(root, isSell: true);
            decimal avgPrice = soldShares > 0 && usdcReceived > 0 ? usdcReceived / soldShares : 0m;
            DebugLog.Trades($"PlacePolySellAsync: soldShares={soldShares} avgPrice={avgPrice:0.0000}");
            return (soldShares, avgPrice);
        }
        catch (Exception ex)
        {
            Console.WriteLine($"[POLY SELL ERROR] {tokenShort}...: {ApiErrorHelper.ClassifyPoly(ex)}");
            DebugLog.Trades($"PlacePolySellAsync exception for {tokenShort}: {ex}");
            return (0m, 0m);
        }
    }

    // ── Unhedged delta recovery ───────────────────────────────────────────────

    private async Task<RecoveryResult> RecoverUnhedgedAsync(
        CrossPair pair, string arbType,
        string kalshiSide, string polyToken,
        decimal kFilled, decimal pFilled,
        decimal kLegAsk, decimal pActualPrice,
        string execId = "")
    {
        decimal balancedQty = Math.Min(kFilled, pFilled);
        decimal kUnhedged   = kFilled - balancedQty;
        decimal pUnhedged   = pFilled - balancedQty;

        // ── Case A: Kalshi filled more — own excess Kalshi contracts ─────────
        if (kUnhedged > 0)
        {
            decimal kUnhedgedValue = kUnhedged * kLegAsk;

            if (kUnhedgedValue < CleanupDustUsd)
            {
                lock (_cleanupLock) { _totalCleanupCostUsd += kUnhedgedValue; }
                await JournalAsync(JsonSerializer.Serialize(new {
                    t = DateTime.UtcNow, @event = "CLEANUP_DUST", execId,
                    pair = pair.PairId, leg = "kalshi", qty = kUnhedged, absorbedUsd = kUnhedgedValue
                }));
                Console.ForegroundColor = ConsoleColor.DarkYellow;
                Console.WriteLine($"[CLEANUP DUST] {pair.Label} | Absorbing {kUnhedged} Kalshi dust (${kUnhedgedValue:0.00}) — no halt");
                Console.ResetColor();
                return new RecoveryResult("DUST_ABSORBED_KALSHI", kUnhedged, kUnhedgedValue);
            }

            bool hasPolyBook = _books.TryGetValue($"P:{polyToken}", out var pBook);
            if (!hasPolyBook)
            {
                DebugLog.Trades($"RecoverUnhedgedAsync {pair.Label}: Poly book missing for {polyToken} — skipping hedge, falling through to reverse");
                Console.WriteLine($"[RECOVER] {pair.Label} | Poly book missing — skipping hedge, falling through to reverse");
            }
            bool skipHedgeA = kUnhedgedValue < CleanupHedgeSkipUsd || !hasPolyBook;

            if (!skipHedgeA)
            {
                decimal currentPolyAsk = pBook!.GetBestAskPrice();
                decimal hedgeNet = kLegAsk + currentPolyAsk + KalshiFee(kLegAsk) + PolyFee(currentPolyAsk);
                DebugLog.Trades($"RecoverUnhedgedAsync {pair.Label}: kUnhedged={kUnhedged} polyAsk={currentPolyAsk:0.0000} hedgeNet={hedgeNet:0.0000}");

                if (hedgeNet < 1.0m)
                {
                    Console.ForegroundColor = ConsoleColor.Yellow;
                    Console.WriteLine($"[RECOVER] {pair.Label} | kExcess={kUnhedged} hedgeNet={hedgeNet:0.0000} — completing hedge on Poly");
                    Console.ResetColor();

                    decimal polyHedgeLimit = Math.Min(1.0m, currentPolyAsk + RecoveryHedgeSlippageCents / 100m);
                    var (polyFill2, _) = await PlacePolyLegAsync(polyToken, polyHedgeLimit, kUnhedged, pair.IsNegRisk);
                    if (polyFill2 > 0)
                    {
                        decimal additional = Math.Min(kUnhedged, polyFill2);
                        if (_openPositions.TryGetValue(pair.PairId, out var pos))
                            _openPositions[pair.PairId] = pos with
                            {
                                KalshiContracts = pos.KalshiContracts + additional,
                                PolyShares      = pos.PolyShares      + additional
                            };
                        Console.ForegroundColor = ConsoleColor.Green;
                        Console.WriteLine($"[RECOVER OK] {pair.Label} | hedge completed +{additional} sets via Poly retry");
                        Console.ResetColor();
                        return new RecoveryResult("HEDGE_COMPLETED", additional, 0);
                    }
                    Console.WriteLine($"[RECOVER] {pair.Label} | Poly hedge retry failed — reversing Kalshi excess");
                }
                else
                {
                    Console.WriteLine($"[RECOVER] {pair.Label} | hedgeNet={hedgeNet:0.0000} >= 1.0 — reversing {kUnhedged} Kalshi {kalshiSide} directly");
                }
            }
            else if (kUnhedgedValue < CleanupHedgeSkipUsd)
            {
                Console.WriteLine($"[CLEANUP SKIP HEDGE] {pair.Label} | kExcess={kUnhedged} value=${kUnhedgedValue:0.00} < ${CleanupHedgeSkipUsd:0.00} — reversing directly");
            }

            // Reverse: sell excess Kalshi contracts back at best bid − buffer
            string kBookKey = arbType == "K_YES_P_NO" ? $"K:{pair.KalshiTicker}" : $"K:{pair.KalshiTicker}_NO";
            decimal kBestBid = _books.TryGetValue(kBookKey, out var kBook) ? kBook.GetBestBidPrice() : 0m;
            if (kBestBid > 0m)
            {
                int kReverseCents = Math.Max(1, (int)Math.Floor((kBestBid - ReverseBufferCents / 100m) * 100));
                DebugLog.Trades($"RecoverUnhedgedAsync: selling {kUnhedged} Kalshi {kalshiSide} @ {kReverseCents}¢");
                try
                {
                    var (_, _, revFill) = await _kalshi.PlaceOrderAsync(
                        pair.KalshiTicker, kalshiSide, kReverseCents, (int)kUnhedged, action: "sell");
                    if (revFill > 0)
                    {
                        decimal reversalLoss = revFill * (kLegAsk - kReverseCents / 100m);
                        if (reversalLoss > 0)
                            lock (_cleanupLock) { _totalCleanupCostUsd += reversalLoss; }
                        await JournalAsync(JsonSerializer.Serialize(new {
                            t = DateTime.UtcNow, @event = "CLEANUP_REVERSED", execId,
                            pair = pair.PairId, leg = "kalshi", qty = kUnhedged,
                            entryPrice = kLegAsk, reversalPrice = kReverseCents / 100m,
                            loss = Math.Max(0m, reversalLoss)
                        }));
                        Console.ForegroundColor = ConsoleColor.Yellow;
                        Console.WriteLine($"[RECOVER REVERSED] {pair.Label} | sold {revFill} Kalshi {kalshiSide} @ {kReverseCents}¢");
                        Console.ResetColor();
                        return new RecoveryResult("REVERSED_KALSHI", revFill, Math.Max(0m, reversalLoss));
                    }
                }
                catch (Exception ex)
                {
                    Console.WriteLine($"[RECOVER ERROR] {pair.Label} | Kalshi reverse exception: {ApiErrorHelper.ClassifyKalshi(ex)}");
                }
            }
            else
            {
                Console.WriteLine($"[RECOVER] {pair.Label} | Kalshi bid side empty — skipping doomed reverse");
            }

            Console.ForegroundColor = ConsoleColor.Red;
            Console.WriteLine($"[HALT] {pair.Label} | Reverse order failed — unhedged position open. Manual reset required.");
            Console.ResetColor();
            _halted = true;
            return new RecoveryResult("HALT", 0, kUnhedgedValue);
        }

        // ── Case B: Poly filled more — own excess Poly shares ────────────────
        if (pUnhedged > 0)
        {
            decimal pUnhedgedValue = pUnhedged * pActualPrice;

            if (pUnhedgedValue < CleanupDustUsd)
            {
                lock (_cleanupLock) { _totalCleanupCostUsd += pUnhedgedValue; }
                await JournalAsync(JsonSerializer.Serialize(new {
                    t = DateTime.UtcNow, @event = "CLEANUP_DUST", execId,
                    pair = pair.PairId, leg = "poly", qty = pUnhedged, absorbedUsd = pUnhedgedValue
                }));
                Console.ForegroundColor = ConsoleColor.DarkYellow;
                Console.WriteLine($"[CLEANUP DUST] {pair.Label} | Absorbing {pUnhedged:0.00} Poly dust (${pUnhedgedValue:0.00}) — no halt");
                Console.ResetColor();
                return new RecoveryResult("DUST_ABSORBED_POLY", pUnhedged, pUnhedgedValue);
            }

            string kHedgeKey = arbType == "K_YES_P_NO" ? $"K:{pair.KalshiTicker}" : $"K:{pair.KalshiTicker}_NO";
            bool hasKalshiBook = _books.TryGetValue(kHedgeKey, out var kHedgeBook);
            if (!hasKalshiBook)
            {
                DebugLog.Trades($"RecoverUnhedgedAsync {pair.Label}: Kalshi book missing for {kHedgeKey} — skipping hedge, falling through to reverse");
                Console.WriteLine($"[RECOVER] {pair.Label} | Kalshi book missing — skipping hedge, falling through to reverse");
            }
            bool skipHedgeB = pUnhedgedValue < CleanupHedgeSkipUsd || !hasKalshiBook;

            if (!skipHedgeB)
            {
                decimal currentKalshiAsk = kHedgeBook!.GetBestAskPrice();
                int currentKCents = Math.Max(1, (int)Math.Ceiling(currentKalshiAsk * 100) + RecoveryHedgeSlippageCents);
                decimal hedgeNet = currentKalshiAsk + pActualPrice + KalshiFee(currentKalshiAsk) + PolyFee(pActualPrice);
                DebugLog.Trades($"RecoverUnhedgedAsync {pair.Label}: pUnhedged={pUnhedged} kalshiAsk={currentKalshiAsk:0.0000} hedgeNet={hedgeNet:0.0000}");

                if (hedgeNet < 1.0m)
                {
                    int hedgeQty = (int)Math.Floor(pUnhedged);
                    if (hedgeQty == 0)
                    {
                        // Entire position is sub-1-share — Kalshi min is 1 contract, can't hedge
                        decimal fracValue = pUnhedged * pActualPrice;
                        lock (_cleanupLock) { _totalCleanupCostUsd += fracValue; }
                        await JournalAsync(JsonSerializer.Serialize(new {
                            t = DateTime.UtcNow, @event = "CLEANUP_DUST", execId,
                            pair = pair.PairId, leg = "poly_fractional", qty = pUnhedged, absorbedUsd = fracValue
                        }));
                        Console.ForegroundColor = ConsoleColor.DarkYellow;
                        Console.WriteLine($"[CLEANUP DUST] {pair.Label} | Absorbing {pUnhedged:0.0000} fractional Poly dust (${fracValue:0.00}) — sub-1-share, can't hedge on Kalshi");
                        Console.ResetColor();
                        return new RecoveryResult("DUST_ABSORBED_POLY", pUnhedged, fracValue);
                    }

                    Console.ForegroundColor = ConsoleColor.Yellow;
                    Console.WriteLine($"[RECOVER] {pair.Label} | pExcess={pUnhedged:0.0000} hedgeQty={hedgeQty} hedgeNet={hedgeNet:0.0000} — completing hedge on Kalshi");
                    Console.ResetColor();

                    var (_, _, kFill2) = await PlaceKalshiLegAsync(
                        pair.KalshiTicker, kalshiSide, currentKCents, hedgeQty, execId);
                    if (kFill2 > 0)
                    {
                        decimal additional = Math.Min((decimal)hedgeQty, kFill2);
                        // Absorb any sub-1-share fractional remainder that can't be hedged on Kalshi
                        decimal fracRemainder = pUnhedged - additional;
                        if (fracRemainder > 0)
                        {
                            decimal fracValue = fracRemainder * pActualPrice;
                            lock (_cleanupLock) { _totalCleanupCostUsd += fracValue; }
                            DebugLog.Trades($"RecoverUnhedgedAsync {pair.Label}: absorbing {fracRemainder:0.0000} fractional remainder (${fracValue:0.00})");
                        }
                        if (_openPositions.TryGetValue(pair.PairId, out var pos))
                            _openPositions[pair.PairId] = pos with
                            {
                                KalshiContracts = pos.KalshiContracts + additional,
                                PolyShares      = pos.PolyShares      + additional
                            };
                        Console.ForegroundColor = ConsoleColor.Green;
                        Console.WriteLine($"[RECOVER OK] {pair.Label} | hedge completed +{additional} sets via Kalshi retry");
                        Console.ResetColor();
                        return new RecoveryResult("HEDGE_COMPLETED", additional, 0);
                    }
                    Console.WriteLine($"[RECOVER] {pair.Label} | Kalshi hedge retry failed — reversing Poly excess");
                }
                else
                {
                    Console.WriteLine($"[RECOVER] {pair.Label} | hedgeNet={hedgeNet:0.0000} >= 1.0 — reversing {pUnhedged:0.00} Poly shares directly");
                }
            }
            else if (pUnhedgedValue < CleanupHedgeSkipUsd)
            {
                Console.WriteLine($"[CLEANUP SKIP HEDGE] {pair.Label} | pExcess={pUnhedged:0.00} value=${pUnhedgedValue:0.00} < ${CleanupHedgeSkipUsd:0.00} — reversing directly");
            }

            // Reverse: sell excess Poly shares back
            var (soldShares, soldPrice) = await PlacePolySellAsync(polyToken, pUnhedged, pair.IsNegRisk);
            if (soldShares > 0)
            {
                decimal reversalLoss = soldShares * (pActualPrice - soldPrice);
                if (reversalLoss > 0)
                    lock (_cleanupLock) { _totalCleanupCostUsd += reversalLoss; }
                await JournalAsync(JsonSerializer.Serialize(new {
                    t = DateTime.UtcNow, @event = "CLEANUP_REVERSED", execId,
                    pair = pair.PairId, leg = "poly", qty = pUnhedged,
                    entryPrice = pActualPrice, reversalPrice = soldPrice,
                    loss = Math.Max(0m, reversalLoss)
                }));
                Console.ForegroundColor = ConsoleColor.Yellow;
                Console.WriteLine($"[RECOVER REVERSED] {pair.Label} | sold {soldShares:0.00} Poly shares @ ${soldPrice:0.0000}");
                Console.ResetColor();
                return new RecoveryResult("REVERSED_POLY", soldShares, Math.Max(0m, reversalLoss));
            }

            Console.ForegroundColor = ConsoleColor.Red;
            Console.WriteLine($"[HALT] {pair.Label} | Reverse order failed — unhedged position open. Manual reset required.");
            Console.ResetColor();
            _halted = true;
            return new RecoveryResult("HALT", 0, pUnhedgedValue);
        }

        return new RecoveryResult("NONE", 0, 0);
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
        const int MaxRetries = 3;
        for (int attempt = 0; attempt < MaxRetries; attempt++)
        {
            try
            {
                using var sw = new StreamWriter(_csvPath, append: attempt > 0, Encoding.UTF8) { AutoFlush = false };
                await foreach (var line in _csvChannel.Reader.ReadAllAsync())
                {
                    await sw.WriteLineAsync(line);
                    await sw.FlushAsync();
                }
                return; // channel completed cleanly
            }
            catch (OperationCanceledException) { return; }
            catch (Exception ex)
            {
                Console.ForegroundColor = ConsoleColor.Red;
                Console.WriteLine($"[CSV WRITER ERROR] Attempt {attempt + 1}/{MaxRetries}: {ex.GetType().Name}: {ex.Message}");
                Console.ResetColor();
                if (attempt + 1 >= MaxRetries)
                {
                    Console.ForegroundColor = ConsoleColor.Red;
                    Console.WriteLine($"[CSV WRITER DEAD] CSV writer failed after {MaxRetries} attempts — execution data will NOT be saved to {_csvPath}. Check disk/permissions.");
                    Console.ResetColor();
                    return;
                }
                await Task.Delay(5_000);
            }
        }
    }

    // ── Trade journal ─────────────────────────────────────────────────────────

    private async Task JournalAsync(string json)
    {
        await _journalLock.WaitAsync();
        try   { await File.AppendAllTextAsync(_journalPath, json + "\n"); }
        finally { _journalLock.Release(); }
    }

    // ── Position reconciliation ───────────────────────────────────────────────

    public async Task<List<ReconciliationEntry>> ReconcilePositionsAsync(IEnumerable<CrossPair> pairs)
    {
        var report = new List<ReconciliationEntry>();
        List<(string Ticker, int Position)> kalshiPos;
        try   { kalshiPos = await _kalshi.GetPositionsAsync(); }
        catch { kalshiPos = []; }
        var kalshiByTicker = kalshiPos.ToDictionary(p => p.Ticker, p => p.Position);

        foreach (var pair in pairs)
        {
            try
            {
                decimal polyYes = await _poly.GetTokenBalanceAsync(pair.PolyYesTokenId);
                decimal polyNo  = await _poly.GetTokenBalanceAsync(pair.PolyNoTokenId);
                kalshiByTicker.TryGetValue(pair.KalshiTicker, out int kPos);

                decimal kQty   = Math.Abs(kPos);
                decimal pQty   = Math.Max(polyYes, polyNo);
                string  status = (kQty == 0 && pQty == 0) ? "CLEAN"
                               : (kQty > 0 && pQty > 0)   ? "MATCHED_POSITION"
                               : (kQty > 0)                ? "UNHEDGED_KALSHI"
                               :                             "UNHEDGED_POLY";
                string notes = $"K={kPos} polyYes={polyYes:0.00} polyNo={polyNo:0.00}";
                report.Add(new ReconciliationEntry(pair.PairId, pair.Label, status, kQty, pQty, notes));
            }
            catch (Exception ex)
            {
                report.Add(new ReconciliationEntry(pair.PairId, pair.Label,
                    "RECONCILE_ERROR", 0, 0, ex.Message));
            }
        }
        return report;
    }

    public async Task ReconcileOnStartupAsync(IEnumerable<CrossPair> pairs)
    {
        Console.WriteLine("[RECONCILE] Checking both venues for open positions from prior runs...");
        var entries = await ReconcilePositionsAsync(pairs);
        int issues = 0;
        foreach (var e in entries)
        {
            if (e.Status == "CLEAN") continue;
            issues++;
            Console.ForegroundColor = e.Status.StartsWith("UNHEDGED") ? ConsoleColor.Red : ConsoleColor.Yellow;
            Console.WriteLine($"[RECONCILE] {e.Label} | {e.Status} | K={e.KalshiQty} P={e.PolyQty} | {e.Notes}");
            Console.ResetColor();
        }
        if (issues == 0)
            Console.WriteLine("[RECONCILE] All pairs clean — no prior positions detected.");
    }

    // ── Post-trade reconciliation ─────────────────────────────────────────────

    private async Task ReconcileTradeAsync(CrossPair pair, string arbType, decimal expectedKalshi, decimal expectedPoly, string execId = "")
    {
        try
        {
            await Task.Delay(2_000); // brief settle window before querying venues
            var kalshiPositions = await _kalshi.GetPositionsAsync();
            int kPos = kalshiPositions.FirstOrDefault(p => p.Ticker == pair.KalshiTicker).Position;
            string polyTokenId = arbType == "K_YES_P_NO" ? pair.PolyNoTokenId : pair.PolyYesTokenId;
            decimal polyBal    = await _poly.GetTokenBalanceAsync(polyTokenId);
            decimal kActual    = Math.Abs(kPos);
            bool kMismatch = Math.Abs(kActual - expectedKalshi) > 0.5m;
            bool pMismatch = Math.Abs(polyBal  - expectedPoly)  > 0.5m;
            if (kMismatch || pMismatch)
            {
                _halted = true;
                Console.ForegroundColor = ConsoleColor.Red;
                Console.WriteLine(
                    $"[RECONCILE ALERT] {pair.Label} | " +
                    $"K: local={expectedKalshi} venue={kActual} | " +
                    $"P: local={expectedPoly:0.00} venue={polyBal:0.00}");
                Console.WriteLine("[RECONCILE ALERT] Bot halted — manual reset required. Verify positions before resuming.");
                Console.ResetColor();
                await JournalAsync(JsonSerializer.Serialize(new {
                    t = DateTime.UtcNow, @event = "RECONCILE_MISMATCH", execId,
                    pair = pair.PairId, arbType,
                    kExpected = expectedKalshi, kVenue = kActual,
                    pExpected = expectedPoly,   pVenue = polyBal,
                    halted = true
                }));
            }
            else
                DebugLog.Trades($"ReconcileTradeAsync {pair.Label}: confirmed K={kActual} P={polyBal:0.00}");
        }
        catch (Exception ex)
        {
            Console.ForegroundColor = ConsoleColor.Red;
            Console.WriteLine($"[RECONCILE ERROR] {pair.Label}: reconciliation threw {ex.GetType().Name}: {ex.Message} — halting bot");
            Console.ResetColor();
            _halted = true;
            await JournalAsync(JsonSerializer.Serialize(new {
                t = DateTime.UtcNow, @event = "RECONCILE_ERROR", execId,
                pair = pair.PairId, exType = ex.GetType().Name, message = ex.Message,
                halted = true
            }));
        }
    }

    // ── Venue maintenance detection ───────────────────────────────────────────

    private async Task CheckMaintenanceThresholdAsync(string venue, int consec)
    {
        if (consec >= MaintenanceErrorThreshold && !_connectionHalted)
        {
            Console.ForegroundColor = ConsoleColor.Red;
            Console.WriteLine(
                $"[MAINTENANCE] {venue}: {consec} consecutive REST failures — halting new trades");
            Console.ResetColor();
            _connectionHalted = true;
            await JournalAsync(JsonSerializer.Serialize(new {
                t = DateTime.UtcNow, @event = "VENUE_MAINTENANCE",
                venue, consecutiveErrors = consec
            }));
        }
    }

    // ── Connection watchdog controls ──────────────────────────────────────────

    public void HaltForConnectionLoss()  => _connectionHalted = true;
    public void ResumeFromConnectionLoss() => _connectionHalted = false;

    /// <summary>Lightweight REST liveness check. Returns true if the venue is reachable.</summary>
    public async Task<bool> PingKalshiAsync()
    {
        try { await _kalshi.GetBalanceCentsAsync(); return true; }
        catch { return false; }
    }

    public async Task<bool> PingPolyAsync()
    {
        try { await _poly.GetUsdcBalanceAsync(); return true; }
        catch { return false; }
    }

    private void DecrementTryLimit()
    {
        if (_triesRemaining < 0) return;
        int left = Interlocked.Decrement(ref _triesRemaining);
        if (left == 0)
        {
            Console.ForegroundColor = ConsoleColor.Cyan;
            Console.WriteLine($"[TRY LIMIT] All {_tryLimit} arb(s) completed — shutting down cleanly.");
            Console.ResetColor();
            _outerCts?.Cancel();
        }
    }

    // ── Early exit monitor ────────────────────────────────────────────────────

    /// <summary>
    /// Called on every book update — same path as arb detection.
    /// Fires CheckEarlyExitAsync as a background task when the updated book belongs
    /// to a pair with an open position and the exit threshold may be met.
    /// </summary>
    public void OnBookUpdate(string bookKey)
    {
        if (_halted || _connectionHalted || _openPositions.IsEmpty) return;

        foreach (var (pairId, pos) in _openPositions.ToArray())
        {
            var pair = _telemetry.GetPair(pairId);
            if (pair == null) continue;

            string kBidKey = pos.ArbType == "K_YES_P_NO"
                ? $"K:{pair.KalshiTicker}"
                : $"K:{pair.KalshiTicker}_NO";
            string pBidKey = pos.ArbType == "K_YES_P_NO"
                ? $"P:{pair.PolyNoTokenId}"
                : $"P:{pair.PolyYesTokenId}";

            if (bookKey != kBidKey && bookKey != pBidKey) continue;
            if (!_earlyExitScheduled.TryAdd(pairId, 0)) continue;

            _ = Task.Run(async () =>
            {
                try   { await CheckEarlyExitAsync(pairId, pos); }
                finally { _earlyExitScheduled.TryRemove(pairId, out _); }
            });
        }
    }

    private async Task RunEarlyExitMonitorAsync()
    {
        while (!_cts.Token.IsCancellationRequested)
        {
            try { await Task.Delay(EarlyExitFallbackIntervalMs, _cts.Token); }
            catch (TaskCanceledException) { break; }

            if (_halted || _connectionHalted || _openPositions.IsEmpty) continue;

            foreach (var (pairId, pos) in _openPositions.ToArray())
            {
                if (_cts.Token.IsCancellationRequested) break;
                try { await CheckEarlyExitAsync(pairId, pos); }
                catch (Exception ex)
                {
                    DebugLog.Trades($"RunEarlyExitMonitorAsync {pairId}: {ex.GetType().Name}: {ex.Message}");
                }
            }
        }
    }

    private async Task CheckEarlyExitAsync(string pairId, ArbPosition pos)
    {
        if (EarlyExitThreshold <= 0m) return;

        var pair = _telemetry.GetPair(pairId);
        if (pair == null) return;

        string kBidKey    = pos.ArbType == "K_YES_P_NO" ? $"K:{pair.KalshiTicker}" : $"K:{pair.KalshiTicker}_NO";
        string pBidKey    = pos.ArbType == "K_YES_P_NO" ? $"P:{pair.PolyNoTokenId}" : $"P:{pair.PolyYesTokenId}";
        string kalshiSide = pos.ArbType == "K_YES_P_NO" ? "yes" : "no";
        string polyToken  = pos.ArbType == "K_YES_P_NO" ? pair.PolyNoTokenId : pair.PolyYesTokenId;

        if (!_books.TryGetValue(kBidKey, out var kBook) || !_books.TryGetValue(pBidKey, out var pBook)) return;

        decimal kBid = kBook.GetBestBidPrice();
        decimal pBid = pBook.GetBestBidPrice();
        if (kBid <= 0m || pBid <= 0m) return;

        decimal entryCostPerSet      = pos.KalshiEntryPrice + pos.PolyEntryPrice;
        decimal expectedProfitPerSet = 1.0m - entryCostPerSet
            - KalshiFee(pos.KalshiEntryPrice) - PolyFee(pos.PolyEntryPrice);
        if (expectedProfitPerSet <= 0m) return;

        decimal unrealizedPnlPerSet = (kBid + pBid) - entryCostPerSet;
        decimal unrealizedPnlTotal  = unrealizedPnlPerSet * pos.KalshiContracts;

        DebugLog.Trades(
            $"EarlyExit {pair.Label}: kBid={kBid:0.0000} pBid={pBid:0.0000} " +
            $"unrealizedPnl/set={unrealizedPnlPerSet:0.0000} " +
            $"hurdle={EarlyExitThreshold * expectedProfitPerSet:0.0000} " +
            $"total=${unrealizedPnlTotal:0.00}");

        if (unrealizedPnlPerSet < EarlyExitThreshold * expectedProfitPerSet) return;
        if (unrealizedPnlTotal  < EarlyExitMinProfitUsd) return;

        // Guard: reuse _inFlight so an OnArbOpened callback can't race with our exit.
        if (!_inFlight.TryAdd(pairId, 0))
        {
            DebugLog.Trades($"CheckEarlyExitAsync {pairId}: already in-flight, skipping this cycle");
            return;
        }
        try
        {
            // Re-read position — it may have been removed by a concurrent fill or recovery.
            if (!_openPositions.TryGetValue(pairId, out var currentPos)) return;

            Console.ForegroundColor = ConsoleColor.Cyan;
            Console.WriteLine(
                $"[EARLY EXIT] {pair.Label} | unrealizedPnl=${unrealizedPnlTotal:0.00} ≥ " +
                $"{EarlyExitThreshold:P0} × expected=${(expectedProfitPerSet * currentPos.KalshiContracts):0.00} " +
                $"— closing early (kBid={kBid:0.0000} pBid={pBid:0.0000})");
            Console.ResetColor();

            await JournalAsync(JsonSerializer.Serialize(new {
                t = DateTime.UtcNow, @event = "EARLY_EXIT_INTENT", execId = currentPos.ExecId,
                pairId, label = pair.Label, arbType = pos.ArbType,
                kalshiContracts = currentPos.KalshiContracts, polyShares = currentPos.PolyShares,
                kBid, pBid, unrealizedPnlPerSet, unrealizedPnlTotal,
                expectedProfitTotal = expectedProfitPerSet * currentPos.KalshiContracts,
                threshold = EarlyExitThreshold, dryRun = _dryRun
            }));

            if (_dryRun)
            {
                _openPositions.TryRemove(pairId, out _);
                Interlocked.Increment(ref _earlyExitsCompleted);
                Console.ForegroundColor = ConsoleColor.Cyan;
                Console.WriteLine($"[EARLY EXIT DRY-RUN] {pair.Label} | position closed (simulated)");
                Console.ResetColor();
                await JournalAsync(JsonSerializer.Serialize(new {
                    t = DateTime.UtcNow, @event = "EARLY_EXIT_COMPLETE", execId = currentPos.ExecId,
                    pairId, outcome = "DRY_RUN",
                    kSold = currentPos.KalshiContracts, pSold = currentPos.PolyShares,
                    unrealizedPnlTotal, dryRun = true
                }));
                return;
            }

            // Live: simultaneously sell both legs
            int kSellCents = Math.Max(1, (int)Math.Floor(kBid * 100));
            var kSellTask = Task.Run(async () =>
            {
                try
                {
                    return await _kalshi.PlaceOrderAsync(
                        pair.KalshiTicker, kalshiSide, kSellCents, (int)currentPos.KalshiContracts, action: "sell");
                }
                catch (Exception ex)
                {
                    Console.WriteLine($"[EARLY EXIT ERROR] {pair.Label} Kalshi sell: {ApiErrorHelper.ClassifyKalshi(ex)}");
                    DebugLog.Trades($"CheckEarlyExitAsync {pairId} Kalshi sell ex: {ex}");
                    return ("", "error", 0m);
                }
            });
            var pSellTask = PlacePolySellAsync(polyToken, currentPos.PolyShares, pair.IsNegRisk);
            await Task.WhenAll(kSellTask, pSellTask);

            var (_, kStatus, kSold) = kSellTask.Result;
            var (pSold, pAvgPrice)  = pSellTask.Result;

            bool kOk = kSold > 0;
            bool pOk = pSold > 0;

            if (kOk && pOk)
            {
                _openPositions.TryRemove(pairId, out _);
                Interlocked.Increment(ref _earlyExitsCompleted);
                decimal kProceeds  = kSold * (kSellCents / 100m);
                decimal pProceeds  = pSold * pAvgPrice;
                decimal realizedPnl = (kProceeds + pProceeds)
                    - (currentPos.KalshiContracts * currentPos.KalshiEntryPrice
                    +  currentPos.PolyShares      * currentPos.PolyEntryPrice);
                Console.ForegroundColor = ConsoleColor.Cyan;
                Console.WriteLine(
                    $"[EARLY EXIT OK] {pair.Label} | sold K={kSold}@{kSellCents}¢ P={pSold:0.00}sh@${pAvgPrice:0.0000} " +
                    $"realizedPnl=${realizedPnl:0.00}");
                Console.ResetColor();
                await JournalAsync(JsonSerializer.Serialize(new {
                    t = DateTime.UtcNow, @event = "EARLY_EXIT_COMPLETE", execId = currentPos.ExecId,
                    pairId, outcome = "FILLED",
                    kSold, kSellCents, pSold, pAvgPrice, realizedPnl
                }));
            }
            else if (!kOk && !pOk)
            {
                // Neither leg filled — books may have moved. Leave position intact; monitor will retry.
                Console.WriteLine($"[EARLY EXIT MISSED] {pair.Label} | both legs unfilled — leaving open, will retry");
                await JournalAsync(JsonSerializer.Serialize(new {
                    t = DateTime.UtcNow, @event = "EARLY_EXIT_MISSED", execId = currentPos.ExecId,
                    pairId, kStatus, kSold, pSold
                }));
            }
            else
            {
                // One leg sold, the other did not — this creates an unhedged position. Halt to prevent compounding.
                Console.ForegroundColor = ConsoleColor.Red;
                Console.WriteLine(
                    $"[EARLY EXIT PARTIAL] {pair.Label} | kOk={kOk} pOk={pOk} " +
                    $"— one leg sold, other did not. Halting for manual review.");
                Console.ResetColor();
                _halted = true;
                await JournalAsync(JsonSerializer.Serialize(new {
                    t = DateTime.UtcNow, @event = "EARLY_EXIT_PARTIAL", execId = currentPos.ExecId,
                    pairId, kOk, pOk, kSold, kSellCents, pSold, pAvgPrice, halted = true
                }));
            }
        }
        finally
        {
            _inFlight.TryRemove(pairId, out _);
        }
    }

    public async Task ShutdownAsync()
    {
        _cts.Cancel();
        _cts.Dispose();
        _csvChannel.Writer.TryComplete();
        // Wait for the CSV writer to drain all buffered rows (5s timeout in case it's stuck)
        await Task.WhenAny(_csvWriterTask, Task.Delay(5_000));
    }
}
