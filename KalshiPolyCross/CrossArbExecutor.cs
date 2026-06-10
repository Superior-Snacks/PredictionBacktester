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

public record PositionStatus(
    string  PairId,
    string  Label,
    string  ArbType,
    decimal KContracts,
    decimal PShares,
    decimal KEntry,
    decimal PEntry,
    decimal KBid,             // -1 if book unavailable
    decimal PBid,             // -1 if book unavailable
    decimal UnrealizedPnl,
    bool    CanMonitorExit    // true when both bid books are live; false = bot is monitoring blind
);

// Summary of what RecoverUnhedgedAsync did with the unhedged delta.
public record RecoveryResult(
    string  Outcome,        // HEDGE_COMPLETED | REVERSED_KALSHI | REVERSED_POLY | DUST_ABSORBED_KALSHI | DUST_ABSORBED_POLY | ORPHANED | HALT
    decimal RecoveredQty,   // contracts/shares successfully hedged or reversed
    decimal LossUsd         // realized loss from the cleanup action
);

// A single naked leg left by a PARTIAL early exit, parked for the 60s monitor loop to keep flattening.
// Attempts counts failed flatten passes; past the cap we stop retrying and let the leg ride to settlement.
public record PendingReversal(
    CrossPair Pair, string Leg /* "kalshi" | "poly" */, string KalshiSide, string KBookKey,
    string PolyToken, bool NegRisk, decimal Qty, decimal EntryPrice, string ExecId, int Attempts = 0);

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
    private readonly CrossArbRestVerifier? _restVerifier;

    // When venue time-skew exceeds this value, block and REST-verify before firing.
    private const double StaleGateMs = 5_000.0;
    // When either book's absolute age exceeds this, REST-verify regardless of relative skew.
    private const double AbsoluteStaleMs = 30_000.0;

    // ── Early exit tuning ─────────────────────────────────────────────────────
    // Triggered on every book update for pairs with an open position (event-driven),
    // with a 60 s fallback timer in case a book update is missed.
    // EarlyExitThreshold: fraction of expected settlement profit required to exit early.
    //   0   = never exit early (hold to settlement)
    //   0.5 = exit when unrealized PnL ≥ 50 % of expected profit   ← default
    //   1.0 = only exit when full settlement value is available on the bid
    private static decimal  EarlyExitThreshold         = 0.50m;
    private const  decimal  EarlyExitMinProfitUsd      = 0.05m;  // skip micro-exits below this
    // Break-even mode fires at exactly ≥0, so normal fill slippage flips the exit negative (the
    // "exit bleed"). Require a few ticks past break-even before selling. Non-breakeven mode already
    // has a positive hurdle (EarlyExitThreshold × expectedProfit) so it doesn't need this cushion.
    private const  decimal  ExitCushionPerSet          = 0.004m;
    private const  int      EarlyExitFallbackIntervalMs  = 60_000; // fallback poll if a book update was missed
    private const  decimal  MinRestorePolyShares          = 1.0m;  // a Poly leg reduced to dust means the position was closed — don't resurrect it
    private const  int      SettlementConfirmTicks        = 2;     // consecutive absent-from-positions ticks before settling

    // ── Configuration ─────────────────────────────────────────────────────────
    private readonly decimal _maxBetUsd;           // max combined dollar cost per arb entry
    private readonly decimal _balanceBufferPct;    // fraction of maxBetUsd kept as per-platform reserve
    private readonly decimal _maxExposureUsd;
    private readonly bool    _minBuy;              // --min-buy: cap every arb to exactly 1 contract
    private readonly decimal _executionThreshold;
    private readonly decimal _execNetFloor;
    private readonly decimal _minPlausibleNet;     // floor below which a net is "too good to be true" — likely a mispriced/mismatched pair; skip it
    private readonly DiscordNotifier? _discord;    // webhook alerter for halts / naked positions / low cash (null = disabled)
    private readonly decimal _lowBalanceAlertUsd;  // Discord-alert when either venue's cash drops below this
    private bool _kalshiLowAlerted;                // low-cash alert debounce (per side), guarded by _balanceLock
    private bool _polyLowAlerted;
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
    private readonly ConcurrentDictionary<string, decimal> _perPairInvested        = new();
    private readonly ConcurrentDictionary<string, int>     _settlementAbsentTicks  = new();
    private readonly ConcurrentDictionary<string, int>                   _polyFeeRates  = new();
    private readonly ConcurrentDictionary<string, (decimal R, double E)> _polyFeeParams = new();
    private readonly ConcurrentDictionary<string, string>                 _polyTickSizes;
    private readonly HashSet<string>                        _blocklist       = new(StringComparer.OrdinalIgnoreCase);
    private readonly object                                 _blocklistLock   = new();
    private readonly bool          _logErrors;
    private readonly string        _errorLogPath = "error_log.txt";
    private readonly SemaphoreSlim _errorLogLock = new(1, 1);
    private          int _kalshiConsecErrors = 0;
    private          int _polyConsecErrors   = 0;
    private const    int MaintenanceErrorThreshold = 5;
    private volatile bool   _halted               = false; // manual reset required (failed reversal / tripwire)
    private readonly ConcurrentDictionary<string, byte> _orphanedPairs = new();  // venue has a stranded leg — block re-entry until cleared
    // Naked legs left by a PARTIAL early exit: orphaned (no halt) and retried by the 60s monitor loop
    // until they flatten — the "longer retry period" for the early-exit case (vs immediate-only recovery).
    private readonly ConcurrentDictionary<string, PendingReversal> _pendingReversals = new();
    // After this many failed flatten passes (~5 monitor loops ≈ 5 min), stop retrying and let the leg
    // ride to settlement — a decisive winner can leave no makers to sell to, so retrying is futile.
    private const int PendingReversalMaxRetries = 5;
    private volatile bool   _connectionHalted     = false; // auto-clears when both venues reconnect
    private volatile bool   _injectMismatchOnNextTrade = false; // test harness: inject mismatch on next fill
    private const    int    ReverseBufferCents           = 2;  // extra slippage tolerance for reversal orders
    private const    int    RecoveryHedgeSlippageCents   = 2;  // extra slippage tolerance for recovery hedge retries
    private const  decimal  CleanupHedgeSkipUsd   = 1.00m; // skip hedge attempt if unhedged value < $1.00
    private const  decimal  CleanupDustUsd         = 0.25m; // absorb silently (no halt) if reversal fails and value < $0.25

    // Recovery / halt policy (configurable from the constructor; defaults preserve prior behavior).
    // Ops rule: the only halts are daily-loss tripwire, manual, and network errors. A naked leg is
    // always worked (hedge if net ≤ _hedgeMaxNet, else relentless reverse); if it still can't flatten
    // (venue paused/closed) the pair is orphaned and the bot keeps trading — it never halts.
    private readonly bool    _perTradeTripwire;     // halt on a single fill landing >N× worse than its edge
    private readonly decimal _tradeMaxLossMult;     // the N above (was the const 3.0)
    private readonly decimal _hedgeMaxNet;          // complete a hedge only if net ≤ this (1.0 = break-even)
    private readonly int     _reverseFloorCents;    // relentless reverse sweeps the book down to this price
    private readonly int     _reverseMaxAttempts;   // sweep attempts before orphaning the remainder
    // Dollar-denominated Poly FAK can sweep far past the intended count on thin books (e.g. 106 shares
    // when balanced=20). An over-fill that large must be reversed, not matched with a Kalshi hedge that
    // would open a huge unintended position. Guard: if pUnhedged > balancedQty × this fraction, reverse.
    private const  decimal  MaxHedgeOverfillFraction   = 0.25m;
    // Cap the Poly IOC limit buffer to this fraction of the ask price. Without the cap, the flat
    // halfAllow buffer doubles the dollar budget on cheap legs (ask=0.05 → limit=0.10+).
    private const  decimal  MaxPolyLimitBufferPct      = 0.15m;

    // ── Position scaling ──────────────────────────────────────────────────────
    // _singleEntry = true (--single-entry):  one open position per pair at a time.
    //   Re-entry is allowed once the position closes (early exit or settlement).
    //   Applies to both fresh and restored positions.
    // _singleEntry = false (default): scale-in allowed up to MaxPerPairExposureUsd.
    private readonly bool    _singleEntry;
    private const    decimal MaxPerPairExposureUsd = 200m;

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
    // Kalshi: 0.07 × p × (1-p) per contract (server-side, modelled client-side).
    // Poly:   r × (p×(1-p))^e per share — r and e from /clob-markets fd, fetched at startup.
    //   _polyFeeRates  stores base_fee from /fee-rate  → feeRateBps for order submission only.
    //   _polyFeeParams stores (r, e) from /clob-markets → fee math only.
    private static decimal KalshiFee(decimal p) => 0.07m * p * (1m - p);
    private decimal PolyFee(decimal p, string tokenId)
    {
        if (_polyFeeParams.TryGetValue(tokenId, out var fp))
            return fp.R * (decimal)Math.Pow((double)(p * (1m - p)), fp.E);
        return 0.03m * p * (1m - p); // fallback: Sports r=0.03, e=1
    }

    private static void Emit(List<string>? log, string msg)
    {
        Console.WriteLine(msg);
        log?.Add(msg);
    }

    private async Task FlushErrorLogAsync(List<string> log, string pairId, string arbType, DateTime ts)
    {
        string sep    = new string('=', 60);
        string header = $"\n{sep}\nATTEMPT {ts:yyyy-MM-dd HH:mm:ss} UTC  pair={pairId}  arbType={arbType}\n{sep}";
        string block  = header + "\n" + string.Join("\n", log) + "\n";
        await _errorLogLock.WaitAsync();
        try   { await File.AppendAllTextAsync(_errorLogPath, block); }
        finally { _errorLogLock.Release(); }
    }

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

    public List<PositionStatus> GetOpenPositionStatus()
    {
        var result = new List<PositionStatus>();
        foreach (var (pairId, pos) in _openPositions)
        {
            var     pair      = _telemetry.GetPair(pairId);
            string  label     = pair?.Label ?? pairId;
            string  kBidKey   = pos.ArbType == "K_YES_P_NO" ? $"K:{pair?.KalshiTicker}" : $"K:{pair?.KalshiTicker}_NO";
            string  pBidKey   = pos.ArbType == "K_YES_P_NO" ? $"P:{pair?.PolyNoTokenId}" : $"P:{pair?.PolyYesTokenId}";
            string  polyToken = pos.ArbType == "K_YES_P_NO" ? pair?.PolyNoTokenId ?? "" : pair?.PolyYesTokenId ?? "";
            decimal kBid = -1m, pBid = -1m, unrealizedPnl = 0m;
            bool    exitEligible = false;

            if (pair != null
                && _books.TryGetValue(kBidKey, out var kBook)
                && _books.TryGetValue(pBidKey, out var pBook))
            {
                kBid = kBook.GetBestBidPrice();
                pBid = pBook.GetBestBidPrice();
                if (kBid > 0m && pBid > 0m)
                {
                    decimal entryFees = KalshiFee(pos.KalshiEntryPrice) + PolyFee(pos.PolyEntryPrice, polyToken);
                    decimal exitFees  = KalshiFee(kBid)                 + PolyFee(pBid,               polyToken);
                    decimal pnlPerSet = (kBid + pBid) - exitFees - (pos.KalshiEntryPrice + pos.PolyEntryPrice) - entryFees;
                    unrealizedPnl     = pnlPerSet * pos.KalshiContracts;
                }
                // Can monitor via REST even when WS books are stale
                exitEligible = (kBid > 0m && pBid > 0m) || _restVerifier != null;
            }

            result.Add(new PositionStatus(pairId, label, pos.ArbType,
                pos.KalshiContracts, pos.PolyShares,
                pos.KalshiEntryPrice, pos.PolyEntryPrice,
                kBid, pBid, unrealizedPnl, CanMonitorExit: exitEligible));
        }
        return result;
    }
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
        decimal execNetFloor        = 0.975m,
        int     pairCooldownSeconds = 120,
        int     fillTimeoutMs       = 5000,
        decimal maxDayLossUsd            = 20m,
        bool    dryRun                   = false,
        bool    minBuy                   = false,
        bool    singleEntry              = false,
        bool    logErrors                = false,
        int?    tryN                = null,
        CancellationTokenSource? outerCts = null,
        ConcurrentDictionary<string, string>? polyTickSizes = null,
        CrossArbRestVerifier? restVerifier = null,
        decimal hedgeMaxNet         = 1.0m,
        int     reverseFloorCents   = 1,
        int     reverseMaxAttempts  = 4,
        decimal tradeMaxLossMult    = 3.0m,
        bool    perTradeTripwire    = true,
        decimal minPlausibleNet     = 0.90m,
        DiscordNotifier? discord    = null,
        decimal lowBalanceAlertUsd  = 15m)
    {
        _kalshi              = kalshi;
        _poly                = poly;
        _telemetry           = telemetry;
        _books               = books;
        _restVerifier        = restVerifier;
        _maxBetUsd           = maxBetUsd;
        _balanceBufferPct    = balanceBufferPct;
        _maxExposureUsd      = maxExposureUsd;
        _minBuy              = minBuy;
        _singleEntry         = singleEntry;
        _logErrors           = logErrors;
        _executionThreshold  = executionThreshold;
        _execNetFloor        = execNetFloor;
        _minPlausibleNet     = minPlausibleNet;
        _discord             = discord;
        _lowBalanceAlertUsd  = lowBalanceAlertUsd;
        _pairCooldownSeconds = pairCooldownSeconds;
        _fillTimeoutMs       = fillTimeoutMs;
        _maxDayLossUsd       = maxDayLossUsd;
        _dryRun              = dryRun;
        _triesRemaining      = tryN ?? -1;
        _tryLimit            = tryN ?? -1;
        _outerCts            = outerCts;
        _hedgeMaxNet         = hedgeMaxNet;
        _reverseFloorCents   = Math.Max(1, reverseFloorCents);
        _reverseMaxAttempts  = Math.Max(1, reverseMaxAttempts);
        _tradeMaxLossMult    = tradeMaxLossMult;
        _perTradeTripwire    = perTradeTripwire;
        _polyTickSizes       = polyTickSizes ?? new();
        _csvPath             = $"CrossArbExecution_{DateTime.UtcNow:yyyyMMdd_HHmmss}.csv";
        _csvWriterTask = Task.Run(RunCsvWriterAsync);
        var journalDir = Path.GetDirectoryName(Path.GetFullPath(_journalPath));
        if (journalDir != null) Directory.CreateDirectory(journalDir);

        // Surface Kalshi 429 back-offs to the journal. The real client is shared across order POSTs,
        // book-refresh GETs, balance/position polls and REST verification, so this one hook captures
        // every rate-limit retry app-wide. (Dry-run uses a sim client — nothing to wire.)
        if (_kalshi is KalshiOrderClient kalshiClient)
            kalshiClient.RateLimitRetryLogger = OnKalshiRateLimitRetry;

        // Same for Poly 425 "order manager not ready" back-offs (live client only; sim has none).
        if (_poly is PolymarketOrderClient polyClient)
            polyClient.OrderRetryLogger = OnPolyOrderRetry;

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
            await PrefetchFeeRatesAsync();
            lock (_balanceLock) { _kalshiBalanceUsd = 1000m; _polyBalanceUsd = 1000m; }
            Console.WriteLine("[BALANCE INIT] Dry-run: simulation seeded at $1,000.00 on each platform");
            _ = Task.Run(RunEarlyExitMonitorAsync);
            return;
        }
        await RefreshBalancesAsync(initial: true);
        await PrefetchFeeRatesAsync();
        _ = Task.Run(PeriodicBalanceRefreshLoop);
        _ = Task.Run(RunEarlyExitMonitorAsync);
        _ = Task.Run(async () =>
        {
            while (!_cts.Token.IsCancellationRequested)
            {
                try { await Task.Delay(30_000, _cts.Token); }
                catch (TaskCanceledException) { break; }
                await PingKalshiAsync();
                await PingPolyAsync();
            }
        });
    }

    private async Task PrefetchFeeRatesAsync()
    {
        var tokens = _telemetry.GetAllPairs()
            .SelectMany(p => new[] { p.PolyYesTokenId, p.PolyNoTokenId })
            .Where(t => !string.IsNullOrEmpty(t))
            .Distinct()
            .ToList();
        if (tokens.Count == 0) return;
        Console.WriteLine($"[FEE PREFETCH] Fetching fee parameters for {tokens.Count} Poly token(s)...");
        int idx = 0;
        foreach (var token in tokens)
        {
            idx++;
            string tok = token[..Math.Min(8, token.Length)];

            int bps = await _poly.GetTakerFeeAsync(token);
            _polyFeeRates[token] = bps;
            await Task.Delay(500);

            var (r, e) = await _poly.GetFeeParamsAsync(token);
            _polyFeeParams[token] = (r, e);
            await Task.Delay(500);

            string ts = await _poly.GetTickSizeAsync(token);
            _polyTickSizes[token] = ts;
            Console.WriteLine($"[FEE PREFETCH] ({idx}/{tokens.Count}) {tok}... order={bps} bps  math: r={r:0.000} e={e:0.0}  tick={ts}");
            await Task.Delay(500);
        }
        _telemetry.PolyFeeRates  = _polyFeeRates;
        _telemetry.PolyFeeParams = _polyFeeParams;
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

    // Fire-and-forget Discord alert. Safe everywhere — even at a hard halt the process stays alive
    // (halted, not exited), so the post completes; failures are swallowed inside AlertAsync.
    private void DiscordAlert(string message) => _ = _discord?.AlertAsync(message);

    private async Task RefreshBalancesAsync(bool initial = false)
    {
        try
        {
            long    kCents    = await _kalshi.GetBalanceCentsAsync();
            decimal newKalshi = kCents / 100m;
            decimal newPoly   = await _poly.GetUsdcBalanceAsync();
            lock (_balanceLock)
            {
                _kalshiBalanceUsd = newKalshi; _polyBalanceUsd = newPoly;
                // Low-cash alert, debounced per side: fire once on cross-below, re-arm on cross-above,
                // so the ~5-min balance poll doesn't repeat the alert every cycle.
                if (newKalshi < _lowBalanceAlertUsd) { if (!_kalshiLowAlerted) { _kalshiLowAlerted = true; DiscordAlert($"⚠️ Low cash: Kalshi ${newKalshi:0.00} < ${_lowBalanceAlertUsd:0.00} — top up to avoid balance-skipping arbs."); } }
                else _kalshiLowAlerted = false;
                if (newPoly < _lowBalanceAlertUsd) { if (!_polyLowAlerted) { _polyLowAlerted = true; DiscordAlert($"⚠️ Low cash: Poly ${newPoly:0.00} < ${_lowBalanceAlertUsd:0.00} — top up to avoid balance-skipping arbs."); } }
                else _polyLowAlerted = false;
            }
            string tag = initial ? "[BALANCE INIT]" : "[BALANCE]";
            Console.WriteLine($"{tag} Kalshi=${newKalshi:0.00} Poly=${newPoly:0.00}");
            DebugLog.Balance($"RefreshBalancesAsync: K=${newKalshi:0.00} P=${newPoly:0.00} initial={initial}");
            await JournalAsync(JsonSerializer.Serialize(new {
                t = DateTime.UtcNow, @event = "BALANCE",
                kalshi = newKalshi, poly = newPoly, initial
            }));
            if (initial)
                await JournalAsync(JsonSerializer.Serialize(new {
                    t = DateTime.UtcNow, @event = "RUN_CONFIG",
                    singleEntry = _singleEntry, minBuy = _minBuy,
                    maxBetUsd = _maxBetUsd, maxExposureUsd = _maxExposureUsd,
                    executionThreshold = _executionThreshold, execNetFloor = _execNetFloor,
                    minPlausibleNet = _minPlausibleNet, lowBalanceAlertUsd = _lowBalanceAlertUsd,
                    pairCooldownSeconds = _pairCooldownSeconds, dryRun = _dryRun,
                    hedgeMaxNet = _hedgeMaxNet, reverseFloorCents = _reverseFloorCents,
                    reverseMaxAttempts = _reverseMaxAttempts,
                    perTradeTripwire = _perTradeTripwire, tradeMaxLossMult = _tradeMaxLossMult
                }));
        }
        catch (Exception ex)
        {
            string balErr = ex is HttpRequestException ? ApiErrorHelper.ClassifyKalshi(ex) : ex.Message;
            Console.WriteLine($"[BALANCE WARN] Failed to refresh balance: {balErr}");
            DebugLog.Balance($"RefreshBalancesAsync exception: {ex}");
        }
    }

    /// <summary>Wire to telemetry.OnArbOpened — fires on every new WS-detected arb window.</summary>
    public void OnArbOpened(string pairId, decimal netCost, string arbType, decimal depth, decimal kLegAsk, decimal pLegAsk)
    {
        DebugLog.Trades($"OnArbOpened: {pairId} {arbType} net={netCost:0.0000} depth={depth:0.0} K={kLegAsk:0.0000} P={pLegAsk:0.0000}");
        _ = Task.Run(async () =>
        {
            try { await ExecuteAsync(pairId, arbType, kLegAsk, pLegAsk); }
            catch (Exception ex)
            {
                Console.WriteLine($"[EXEC ERROR] {pairId}: {ex.Message}");
                DebugLog.Trades($"ExecuteAsync unhandled exception for {pairId}: {ex}");
            }
        });
    }

    // ── Core execution ────────────────────────────────────────────────────────

    private async Task ExecuteAsync(string pairId, string arbType, decimal detectedKAsk, decimal detectedPAsk)
    {
        if (_halted || _connectionHalted)
        {
            Console.WriteLine($"[EXEC SKIP] {pairId}: {(_halted ? "bot halted (manual reset required)" : "connection halted")}");
            return;
        }
        if (_orphanedPairs.ContainsKey(pairId))
        {
            Console.WriteLine($"[EXEC SKIP] {pairId}: orphaned leg on venue from a prior run — blocked until cleared");
            return;
        }
        // Per-pair in-flight guard: prevents two concurrent OnArbOpened callbacks from
        // both passing the cooldown/open-position check before either one sets the cooldown.
        if (!_inFlight.TryAdd(pairId, 0))
        {
            Console.WriteLine($"[EXEC SKIP] {pairId}: already in-flight (previous attempt still running)");
            return;
        }
        try   { await ExecuteLockedAsync(pairId, arbType, detectedKAsk, detectedPAsk); }
        finally { _inFlight.TryRemove(pairId, out _); }
    }

    private async Task ExecuteLockedAsync(string pairId, string arbType, decimal kLegAsk, decimal pLegAsk)
    {
        long now = DateTimeOffset.UtcNow.ToUnixTimeSeconds();

        // Guard: cooldown or open position on this pair
        if (_cooldownUntil.TryGetValue(pairId, out long cd) && now < cd)
        {
            Console.WriteLine($"[EXEC SKIP] {pairId}: cooldown active for {cd - now}s more");
            return;
        }
        if (_openPositions.TryGetValue(pairId, out var heldPos))
        {
            await JournalAsync(JsonSerializer.Serialize(new {
                t = DateTime.UtcNow, @event = "REENTRY_WHILE_HELD",
                pairId, singleEntry = _singleEntry,
                heldK = heldPos.KalshiContracts, heldP = heldPos.PolyShares,
                heldArbType = heldPos.ArbType,
                detectedNet = Math.Round(kLegAsk + pLegAsk, 4),
                willBlock = _singleEntry
            }));
        }
        if (_openPositions.ContainsKey(pairId))
        {
            Console.WriteLine($"[EXEC SKIP] {pairId}: position already open (single-entry enforced)");
            return;
        }

        var pair = _telemetry.GetPair(pairId);
        if (pair == null)
        {
            Console.WriteLine($"[EXEC SKIP] {pairId}: pair not found in telemetry — possible config mismatch");
            return;
        }

        // Books are needed for stale-gate timestamp checks; prices come from the detection event.
        if (!_books.TryGetValue($"K:{pair.KalshiTicker}",    out var kYes))
        { Console.WriteLine($"[EXEC SKIP] {pair.Label}: missing book K:{pair.KalshiTicker} — WS not yet subscribed"); return; }
        if (!_books.TryGetValue($"K:{pair.KalshiTicker}_NO", out var kNo))
        { Console.WriteLine($"[EXEC SKIP] {pair.Label}: missing book K:{pair.KalshiTicker}_NO — WS not yet subscribed"); return; }
        if (!_books.TryGetValue($"P:{pair.PolyYesTokenId}",  out var pYes))
        { Console.WriteLine($"[EXEC SKIP] {pair.Label}: missing book P:yes — WS not yet subscribed"); return; }
        if (!_books.TryGetValue($"P:{pair.PolyNoTokenId}",   out var pNo))
        { Console.WriteLine($"[EXEC SKIP] {pair.Label}: missing book P:no — WS not yet subscribed"); return; }

        // kLegAsk / pLegAsk are the prices captured at detection time — no WS re-read here.
        // Avoids the race where Task.Run scheduling delay lets the book update before we read it.
        // The stale gate below will REST-override them when the book age is > StaleGateMs.
        string  kalshiSide, polyToken;
        double  venueSkewMs = 0;

        if (arbType == "K_YES_P_NO")
        {
            kalshiSide = "yes";
            polyToken  = pair.PolyNoTokenId;
        }
        else // K_NO_P_YES
        {
            kalshiSide = "no";
            polyToken  = pair.PolyYesTokenId;
        }

        // Venue time-skew: if one book is significantly stale, REST-verify current prices
        // before firing orders. The WS book is unreliable above StaleGateMs.
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

            double kAgeMs = kLastDelta.Ticks > 0 ? (DateTime.UtcNow - kLastDelta).TotalMilliseconds : 0;
            double pAgeMs = pLastDelta.Ticks > 0 ? (DateTime.UtcNow - pLastDelta).TotalMilliseconds : 0;
            double maxAgeMs = Math.Max(kAgeMs, pAgeMs);
            bool staleByAge = maxAgeMs >= AbsoluteStaleMs;

            if ((venueSkewMs >= StaleGateMs || staleByAge) && _restVerifier != null)
            {
                string reason = staleByAge && venueSkewMs < StaleGateMs
                    ? $"age={maxAgeMs:0}ms" : $"skew={venueSkewMs:0}ms";
                Console.WriteLine($"[STALE GATE] {pair.Label} | {reason} — REST-verifying before firing");
                var (freshK, freshP) = await _restVerifier.GetCurrentAsksAsync(pair, arbType);
                if (freshK <= 0m || freshP <= 0m)
                {
                    Console.WriteLine($"[STALE GATE] {pair.Label} | REST fetch failed — skipping");
                    return;
                }
                decimal freshNet = freshK + freshP + KalshiFee(freshK) + PolyFee(freshP, polyToken);
                if (freshNet >= _executionThreshold)
                {
                    Console.WriteLine($"[STALE GATE] {pair.Label} | REST net=${freshNet:0.0000} >= threshold — no arb, skipping");
                    return;
                }
                // Use REST prices as the execution basis
                Console.WriteLine($"[STALE GATE] {pair.Label} | REST confirmed K={freshK:0.0000} P={freshP:0.0000} net=${freshNet:0.0000} — proceeding");
                kLegAsk = freshK;
                pLegAsk = freshP;
            }
        }

        // Re-validate arb still holds at execution time
        decimal netNow = kLegAsk + pLegAsk + KalshiFee(kLegAsk) + PolyFee(pLegAsk, polyToken);
        DebugLog.Trades($"ExecuteAsync {pair.Label}: live check — kLeg={kLegAsk:0.0000} pLeg={pLegAsk:0.0000} net={netNow:0.0000} threshold={_executionThreshold:0.000}");
        if (netNow >= _executionThreshold)
        {
            Console.WriteLine($"[EXEC SKIP] {pair.Label} | net=${netNow:0.0000} >= threshold {_executionThreshold:0.000}");
            await JournalAsync(JsonSerializer.Serialize(new {
                t = DateTime.UtcNow, @event = "EXEC_SKIP", pairId, arbType,
                reason = "NET_TOO_HIGH", netNow, threshold = _executionThreshold,
                kAsk = kLegAsk, pAsk = pLegAsk
            }));
            return;
        }

        if (kLegAsk <= 0.02m || pLegAsk <= 0.02m)
        {
            Console.WriteLine($"[EXEC SKIP] {pair.Label}: price below 2¢ floor — K={kLegAsk:0.0000} P={pLegAsk:0.0000}");
            return;
        }

        // Gate: require minimum net margin before attempting execution.
        // Kalshi prices are whole cents, so any halfAllow < 0.01 rounds back to exact ask —
        // instead we always give Kalshi ask+1¢ and use a separate net floor as the gate.
        if (netNow > _execNetFloor)
        {
            Console.WriteLine($"[EXEC SKIP] {pair.Label} | margin too thin: net=${netNow:0.0000} > floor {_execNetFloor:0.000}");
            await JournalAsync(JsonSerializer.Serialize(new {
                t = DateTime.UtcNow, @event = "EXEC_SKIP", pairId, arbType,
                reason = "THIN_MARGIN", netNow, floor = _execNetFloor, threshold = _executionThreshold
            }));
            return;
        }

        // "Too good to be true" floor: a net this far below 1.00 means the two legs disagree by more
        // than any real cross-platform spread (healthy arbs sit ~0.985-0.995). That's the signature of a
        // mispriced/mismatched pair (the JOR class) — skip it at the source rather than open a phantom
        // position. Non-destructive: just don't trade; the pair stays eligible if it later re-prices.
        if (netNow < _minPlausibleNet)
        {
            Console.ForegroundColor = ConsoleColor.Red;
            Console.WriteLine($"[EXEC SKIP] {pair.Label} | net=${netNow:0.0000} < plausible floor {_minPlausibleNet:0.000} — too good to be true, likely mismatched pair");
            Console.ResetColor();
            await JournalAsync(JsonSerializer.Serialize(new {
                t = DateTime.UtcNow, @event = "EXEC_SKIP", pairId, arbType,
                reason = "IMPLAUSIBLE_NET", netNow, minPlausibleNet = _minPlausibleNet, floor = _execNetFloor
            }));
            return;
        }
        // Kalshi always gets ask+1¢ (one full tick of IOC buffer, regardless of margin).
        // Poly gets the remaining margin as buffer; floor at exact ask if margin is thin.
        decimal halfAllow   = (_executionThreshold - netNow) / 2m;
        int     kPriceCents = (int)Math.Floor((kLegAsk + 0.01m) * 100m);
        decimal pBufferCap  = pLegAsk * MaxPolyLimitBufferPct;
        decimal pLimitAsk   = Math.Min(0.99m, pLegAsk + Math.Min(Math.Max(0m, halfAllow - 0.01m), pBufferCap));
        DebugLog.Trades($"ExecuteAsync {pair.Label}: halfAllow={halfAllow:0.00000} kLimit={kPriceCents}¢ pLimit={pLimitAsk:0.0000}");
        decimal pricePerSet = kLegAsk + pLegAsk;

        // Poly minimum: per-market orderMinSize and the CLOB's hard $1 dollar floor.
        // Floor pLegAsk to the order tick (0.01) so the minimum count guarantees
        // makerAmount >= $1 after the same rounding SubmitOrderAsync applies.
        decimal pLegAskForMin = Math.Max(0.01m, Math.Floor(pLegAsk * 100m) / 100m);
        int polyMinByShare    = (int)Math.Ceiling(pair.PolyMinSize);
        int polyMinByDollar   = (int)Math.Ceiling(1.00m / pLegAskForMin);
        int polyMinContracts  = Math.Max(polyMinByShare, polyMinByDollar);

        // --min-buy: trade exactly the Poly floor amount (ignores maxBet — test/debug mode)
        int contracts = _minBuy
            ? polyMinContracts
            : (int)Math.Floor(_maxBetUsd / pricePerSet);

        if (contracts < polyMinContracts)
        {
            Console.WriteLine(
                $"[EXEC SKIP] {pair.Label} | pricePerSet=${pricePerSet:0.0000} > maxBet=${_maxBetUsd:0.00} " +
                $"(need {polyMinContracts} contracts, pLeg=${pLegAsk:0.0000})");
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
            else
            {
                // Nothing reserved — zero out so the restoration below is a no-op.
                kalshiCost = polyCost = estimatedCost = 0m;
            }
        }
        if (contracts < polyMinContracts)
        {
            lock (_balanceLock)
            {
                _kalshiBalanceUsd += kalshiCost;
                _polyBalanceUsd   += polyCost;
            }
            Console.WriteLine(
                $"[EXEC SKIP] {pair.Label} | balance-limited to {contracts} contract(s), need ≥ {polyMinContracts} " +
                $"(K=${kBalSnap:0.00} P=${pBalSnap:0.00} need K≈${kLegAsk * polyMinContracts:0.00} P≈${pLegAsk * polyMinContracts:0.00})");
            DebugLog.Trades($"ExecuteAsync {pair.Label}: skipped — {contracts} contracts < polyMin {polyMinContracts} (balance-limited)");
            return;
        }

        // Depth gate: only fire if both venues can fill the full order at our limit prices.
        // Measures volume at or below each limit — not top-N regardless of price — so a book
        // with 12 contracts at 15¢ doesn't count toward a 9¢ Poly limit.
        {
            var kBook = arbType == "K_YES_P_NO" ? kYes : kNo;
            var pBook = arbType == "K_YES_P_NO" ? pNo  : pYes;
            decimal kLimitDec     = kLegAsk + 0.01m;   // mirrors kPriceCents calc above
            decimal kDepthAtLimit = kBook.GetAskVolumeAtOrBelow(kLimitDec);
            decimal pDepthAtLimit = pBook.GetAskVolumeAtOrBelow(pLimitAsk);
            if (Math.Min(kDepthAtLimit, pDepthAtLimit) < contracts)
            {
                lock (_balanceLock) { _kalshiBalanceUsd += kalshiCost; _polyBalanceUsd += polyCost; }
                Console.WriteLine(
                    $"[EXEC SKIP] {pair.Label} | depth too thin: need={contracts} " +
                    $"K≤{kLimitDec:0.00}={kDepthAtLimit:0.0} P≤{pLimitAsk:0.0000}={pDepthAtLimit:0.0}");
                await JournalAsync(JsonSerializer.Serialize(new {
                    t = DateTime.UtcNow, @event = "EXEC_SKIP", pairId, arbType,
                    reason = "THIN_DEPTH", contracts,
                    kDepthAtLimit = Math.Round(kDepthAtLimit, 2), kLimit = kLimitDec,
                    pDepthAtLimit = Math.Round(pDepthAtLimit, 2), pLimit = pLimitAsk
                }));
                return;
            }
        }

        if (contracts < idealContracts)
        {
            Console.WriteLine(
                $"[EXEC SCALE] {pair.Label} | {idealContracts}→{contracts} contracts (balance limited) " +
                $"K=${kBalSnap:0.00} P=${pBalSnap:0.00}");
            DebugLog.Balance($"ExecuteAsync {pair.Label}: scaled {idealContracts}→{contracts} contracts");
        }

        string execId = string.Empty;

        // Blocklist check — pairs flagged at startup (closed/invalid) or by runtime 404
        if (_blocklist.Contains(pair.KalshiTicker))
        {
            Console.WriteLine($"[EXEC SKIP] {pair.Label}: on blocklist — skipping");
            lock (_balanceLock) { _kalshiBalanceUsd += kalshiCost; _polyBalanceUsd += polyCost; }
            await JournalAsync(JsonSerializer.Serialize(new {
                t = DateTime.UtcNow, @event = "EXEC_SKIP", pairId, arbType,
                reason = "BLOCKLISTED", ticker = pair.KalshiTicker
            }));
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

        DateTime execStart = DateTime.UtcNow;
        var execLog = _logErrors ? new List<string>() : null;
        bool hadError = false;

        Console.ForegroundColor = ConsoleColor.Cyan;
        Emit(execLog, _dryRun
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

        // Snapshot pre-trade Poly balance concurrently with leg orders so reconcile can
        // compute a delta rather than comparing against total wallet balance. This prevents
        // spurious RECONCILE_MISMATCH halts when a pre-existing Poly balance is present
        // (e.g. orphaned shares from a prior run where Kalshi settled but Poly has not).
        // Returns null (not 0) on a failed read so reconcile can tell a real zero balance from a
        // flaky API call and refuse to halt against a fabricated prior.
        Task<decimal?> priorPolyBalTask = _dryRun
            ? Task.FromResult<decimal?>(0m)
            : SnapshotPolyBalanceAsync(polyToken);

        // Fire both legs simultaneously
        var kalshiTask = PlaceKalshiLegAsync(pair.KalshiTicker, kalshiSide, kPriceCents, contracts, execId, execLog);
        var polyTask   = PlacePolyLegAsync(polyToken, pLimitAsk, polyShares, pair.IsNegRisk, execLog);

        // Catch any unhandled leg exception so the OTHER leg's fill is still visible.
        // PlaceXxxLegAsync both have general catch blocks, but those blocks call
        // CheckMaintenanceThresholdAsync which can itself throw, propagating out.
        // If Task.WhenAll throws here we must still reach the recovery section below.
        Exception? legException = null;
        try { await Task.WhenAll(kalshiTask, polyTask); }
        catch (Exception ex) { legException = ex; }

        var (kOrderId, kStatus, kFilled) = kalshiTask.IsCompletedSuccessfully
            ? kalshiTask.Result : ("", "error", 0m);
        var (pFilled, pActualPrice) = polyTask.IsCompletedSuccessfully
            ? polyTask.Result : (0m, 0m);

        if (legException != null)
        {
            hadError = true;
            Console.ForegroundColor = ConsoleColor.Red;
            Emit(execLog,
                $"[LEG EXCEPTION] {pair.Label} | {legException.Message} — " +
                $"kFilled={kFilled} pFilled={pFilled:0.00}; routing through recovery if needed");
            Console.ResetColor();
            await JournalAsync(JsonSerializer.Serialize(new {
                t = DateTime.UtcNow, @event = "LEG_EXCEPTION",
                pairId, arbType, kFilled, pFilled = (double)pFilled,
                error = legException.Message, dryRun = _dryRun
            }));
        }

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
            hadError = true;
            Console.ForegroundColor = ConsoleColor.DarkYellow;
            Emit(execLog,
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
            actualNetPerSet = kLegAsk + pActualPrice + KalshiFee(kLegAsk) + PolyFee(pActualPrice, polyToken);
            bool    arbProfitable   = actualNetPerSet < 1.0m;
            decimal actualProfit    = balancedQty * (1.0m - actualNetPerSet);

            _openPositions[pairId] = new ArbPosition(
                pairId, arbType, balancedQty, balancedQty, kLegAsk, pActualPrice, t0, execId);
            DebugLog.Trades($"POSITION_ADDED {pairId} reason=FILL k={balancedQty} p={balancedQty}");
            // Exposure/projection aggregates are updated once after recovery (see below), keyed off
            // the FINAL position — so hedge-completed sets beyond balancedQty are counted too.

            if (arbProfitable)
            {
                Console.ForegroundColor = ConsoleColor.Green;
                Emit(execLog,
                    $"[EXEC OK] {pair.Label} | K={balancedQty}@{kPriceCents}¢ | " +
                    $"P={balancedQty:0.00}sh@${pActualPrice:0.0000} | cost=${actualCost:0.00} " +
                    $"actualNet={actualNetPerSet:0.0000} projProfit=${actualProfit:0.00}");
            }
            else
            {
                Console.ForegroundColor = ConsoleColor.Yellow;
                Emit(execLog,
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
                if (_perTradeTripwire && actualLoss > _tradeMaxLossMult * expectedEdge)
                {
                    // The position is already HEDGED (both legs filled) — no directional risk — so we
                    // DON'T halt; just flag the anomaly and keep trading. The loss still flows into the
                    // per-day tripwire below, which is the only loss-based stop.
                    DiscordAlert($"⚠️ Trade-loss anomaly: {pair.Label} — fill {actualLoss:0.0000} worse than {_tradeMaxLossMult}× edge {expectedEdge:0.0000}; position hedged, trading continues. Possible pricing/feed bug.");
                    await JournalAsync(JsonSerializer.Serialize(new {
                        t = DateTime.UtcNow, @event = "TRADE_LOSS_ANOMALY",
                        reason = "per_trade_loss", pairId, dryRun = _dryRun,
                        actualNet = actualNetPerSet, detectedNet = netNow,
                        actualLoss = Math.Round(actualLoss, 4),
                        maxExpected = 1.0m + _tradeMaxLossMult * expectedEdge, halted = false
                    }));
                    Console.ForegroundColor = ConsoleColor.Yellow;
                    Emit(execLog,
                        $"[ANOMALY] {pair.Label} | Per-trade loss {actualLoss:0.0000} > {_tradeMaxLossMult}× " +
                        $"edge {expectedEdge:0.0000} — hedged, continuing (alert).");
                    Console.ResetColor();
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
                    Emit(execLog,
                        $"[HALT] Per-day tripwire: cumulative loss ${_dayLossUsd:0.00} >= " +
                        $"max ${_maxDayLossUsd:0.00}. Manual reset required.");
                    Console.ResetColor();
                    DiscordAlert($"🚨 HARD HALT — per-day loss tripwire: cumulative loss ${_dayLossUsd:0.00} ≥ max ${_maxDayLossUsd:0.00}. Trading stopped for the day; manual reset required.");
                    _halted = true;
                }
            }

            // ── Fee model tracking ────────────────────────────────────────────────
            decimal modeledFees = (KalshiFee(kLegAsk) + PolyFee(pLegAsk, polyToken)) * balancedQty;
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
            hadError = true;
            Emit(execLog,
                $"[EXEC UNHEDGED] {pair.Label} | " +
                $"kFilled={kFilled} pFilled={pFilled:0.00} balanced={balancedQty} " +
                $"kExcess={kUnhedged} pExcess={pUnhedged:0.00} — starting recovery");
            recovery = await RecoverUnhedgedAsync(pair, arbType, kalshiSide, polyToken,
                kFilled, pFilled, kLegAsk, pActualPrice, execId, execLog);
        }
        else if (neitherFilled)
        {
            hadError = true;
            Emit(execLog, $"[EXEC MISS] {pair.Label} | Neither leg filled. k-status={kStatus}");
        }

        // Dust fold: on DUST_ABSORBED_POLY the venue holds pFilled but the tracked position only has
        // balancedQty. Fold the absorbed shares into PolyShares so (a) the early exit sweeps them every
        // cycle (the live Poly sell takes the fractional PolyShares un-floored) instead of orphaning a
        // remainder that accumulates across cycles past reconcile's 0.5-share tolerance, and (b) reconcile's
        // expected venue (pFilled) matches the position even when the pre-trade balance snapshot reads 0.
        if (recovery?.Outcome == "DUST_ABSORBED_POLY"
            && _openPositions.TryGetValue(pairId, out var dustPos))
        {
            decimal folded = dustPos.PolyShares + recovery.RecoveredQty;
            _openPositions[pairId] = dustPos with { PolyShares = folded };
            DebugLog.Trades($"ExecuteAsync {pair.Label}: folded {recovery.RecoveredQty:0.####} absorbed Poly dust → PolyShares={folded:0.####}.");
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
                : recovery?.Outcome == "ORPHANED"                       ? "ORPHANED"
                : recovery != null && recovery.Outcome != "NONE"        ? "FILLED_WITH_CLEANUP"
                : "FILLED";

            // True final position after all cleanup (hedge add or reversal may have changed qty)
            var finalPos  = _openPositions.TryGetValue(pairId, out var fp) ? fp : null;
            decimal kHeld = finalPos?.KalshiContracts ?? 0m;
            decimal pHeld = finalPos?.PolyShares      ?? 0m;

            // Net/cost from the FINAL position, not the initial-fill locals. On a
            // hedge-completed-from-zero entry balancedQty==0, so pActualPrice/actualNetPerSet
            // are still 0 and would log pAvgPrice=0, net=0, projected=kHeld*(1-0). finalPos
            // carries the real blended prices set in the recovery branch.
            decimal finalNet = finalPos != null
                ? finalPos.KalshiEntryPrice + finalPos.PolyEntryPrice
                  + KalshiFee(finalPos.KalshiEntryPrice) + PolyFee(finalPos.PolyEntryPrice, polyToken)
                : actualNetPerSet;

            // Exposure/projection from the FINAL position, so a hedge-completed-from-zero entry
            // (balancedQty==0) is counted and a pure miss (kHeld==0) contributes nothing. Counts
            // exactly when the EXECUTION_COMPLETE record below emits a position block (kHeld > 0).
            if (kHeld > 0)
            {
                decimal finalCost   = finalPos!.KalshiEntryPrice * kHeld + finalPos.PolyEntryPrice * pHeld;
                decimal finalProfit = kHeld * (1.0m - finalNet);
                Interlocked.Increment(ref _totalExecuted);
                lock (_exposureLock) { _totalInvested += finalCost; _totalProjectedProfit += finalProfit; }
                _perPairInvested.AddOrUpdate(pairId, finalCost, (_, old) => old + finalCost);
            }

            DecrementTryLimit();
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
                        feePerShare  = pFilled > 0 ? (object?)Math.Round(PolyFee(pActualPrice, polyToken), 6) : null,
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
                    kEntryPrice = Math.Round(finalPos!.KalshiEntryPrice, 6),
                    pAvgPrice   = Math.Round(finalPos.PolyEntryPrice, 6),
                    totalCostUsd       = Math.Round(finalPos.KalshiEntryPrice * kHeld + finalPos.PolyEntryPrice * pHeld, 4),
                    modeledNetPerSet   = Math.Round(netNow, 6),
                    actualNetPerSet    = Math.Round(finalNet, 6),
                    projectedProfitUsd = Math.Round(kHeld * (1.0m - finalNet), 4)
                } : null,

                outcome = execOutcome,
                stalePriceSuspected = staleSuspected,
                durationMs = (long)(DateTime.UtcNow - t0).TotalMilliseconds
            }));
        }

        // Test harness: if QueueMismatchOnNextTrade() was called, inject +1 mismatch on this pair
        // and force reconciliation to run even in dry-run so the halt path can be verified.
        bool reconcileInDryRun = false;
        if (_injectMismatchOnNextTrade && !neitherFilled && balancedQty > 0)
        {
            _injectMismatchOnNextTrade = false;
            reconcileInDryRun = true;
            string injectTokenId = arbType == "K_YES_P_NO" ? pair.PolyNoTokenId : pair.PolyYesTokenId;
            if (_kalshi is SimulatedVenuePositionClient kSim) kSim.InjectMismatch(pair.KalshiTicker, +1);
            if (_poly   is SimulatedPolymarketClient    pSim) pSim.InjectTokenBalanceMismatch(injectTokenId, +1m);
            Console.ForegroundColor = ConsoleColor.Magenta;
            Console.WriteLine($"[MISMATCH INJECT] Firing on {pair.Label} — reconcile will run to verify halt");
            Console.ResetColor();
        }

        // Release exposure only when nothing filled at all.
        // Unhedged delta keeps exposure tracked until recovery resolves it.
        if (neitherFilled)
            lock (_exposureLock) { _totalExposure -= estimatedCost; }

        if (!neitherFilled)
        {
            // Re-fetch real balances after execution — not needed in dry-run (simulated clients
            // return dummy values that would overwrite the executor's tracked simulation balances).
            if (!_dryRun) _ = Task.Run(async () => await RefreshBalancesAsync());
            // Post-trade position reconciliation — skipped in dry-run normally (simulated client
            // tracks total fills while the executor tracks balanced fills, causing spurious mismatches).
            // reconcileInDryRun overrides this when QueueMismatchOnNextTrade() was pending.
            if ((!_dryRun || reconcileInDryRun) && balancedQty > 0)
            {
                // null = snapshot failed/inconclusive (distinct from a real 0); reconcile won't halt on it.
                decimal? priorPolyBal = priorPolyBalTask.IsCompletedSuccessfully ? priorPolyBalTask.Result : null;
                // Order-poll reflects fills immediately; the positions endpoint lags 10–20s.
                // A clean Kalshi reverse can still use order-poll: poll the original buy fill and
                // subtract the venue-confirmed reversed qty. Other kUnhedged>0 outcomes
                // (hedge-completed / dust-absorbed) still fall back to the positions endpoint.
                bool kReversed = recovery?.Outcome == "REVERSED_KALSHI";
                string reconcileOrderId = (kUnhedged == 0 || kReversed) ? kOrderId : "";
                decimal reversedKalshiQty = kReversed ? recovery!.RecoveredQty : 0m;
                // When Poly dust was absorbed we didn't sell — venue holds pFilled, not balancedQty.
                decimal expectedPolyVenue = recovery?.Outcome == "DUST_ABSORBED_POLY" ? pFilled : balancedQty;
                // A Poly overfill reversal (bought too many, sold the excess in-trade) can race the
                // pre-trade snapshot and poison reconcile's delta check — flag it so reconcile trusts
                // the absolute position instead of the (contaminated) delta.
                bool polyOverfillReversed = recovery?.Outcome == "REVERSED_POLY";
                _ = Task.Run(async () => await ReconcileTradeAsync(pair, arbType, balancedQty, expectedPolyVenue, execId, reconcileOrderId, priorPolyBal, reversedKalshiQty, polyOverfillReversed));
            }
        }
        else
        {
            // Nothing filled — restore speculative balance reservation in full.
            lock (_balanceLock) { _kalshiBalanceUsd += kalshiCost; _polyBalanceUsd += polyCost; }
        }

        EnqueueCsvRow(pair, arbType, t0, kPriceCents, kLegAsk, pLegAsk,
                      kFilled, pFilled, pActualPrice, netNow, kStatus);

        if (_logErrors && hadError && execLog?.Count > 0)
            await FlushErrorLogAsync(execLog, pairId, arbType, execStart);
    }

    // ── Kalshi IOC leg ────────────────────────────────────────────────────────

    private async Task<(string OrderId, string Status, decimal FillCount)> PlaceKalshiLegAsync(
        string ticker, string side, int priceCents, int count, string execId = "", List<string>? execLog = null)
    {
        string clientId = string.IsNullOrEmpty(execId)
            ? $"CAXARB_{ticker}_{DateTimeOffset.UtcNow.ToUnixTimeMilliseconds()}"
            : $"CAXARB_{execId}";
        Emit(execLog, $"[ORDER K] {ticker} {side.ToUpper()} {priceCents}¢ × {count}  clientId={clientId}");
        DebugLog.Trades($"PlaceKalshiLegAsync: {ticker} {side} {priceCents}¢ × {count} clientId={clientId}");
        try
        {
            var (orderId, status, fillImm) = await _kalshi.PlaceOrderAsync(
                ticker, side, priceCents, count, clientOrderId: clientId);
            Interlocked.Exchange(ref _kalshiConsecErrors, 0);
            Console.ForegroundColor = ConsoleColor.Green;
            Emit(execLog, $"[FILL K]  {ticker} placed orderId={orderId} status={status} fillImm={fillImm}");
            Console.ResetColor();
            DebugLog.Trades($"PlaceKalshiLegAsync: placed orderId={orderId} status={status} fillImm={fillImm}");

            if (status == "executed" || fillImm >= count)
                return (orderId, status, fillImm);

            if (string.IsNullOrEmpty(orderId))
            {
                Emit(execLog, $"[FILL K WARN] {ticker} empty orderId with status={status} — not polling");
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
            Console.ForegroundColor = settleStatus == "executed" ? ConsoleColor.Green : ConsoleColor.Yellow;
            Emit(execLog, $"[FILL K]  {ticker} settle-poll after {polls} polls — status={settleStatus} fill={settleFill}");
            Console.ResetColor();
            DebugLog.Trades($"PlaceKalshiLegAsync: settle-poll after {polls} polls — status={settleStatus} fill={settleFill}");
            return (orderId, settleStatus is "executed" or "canceled" ? settleStatus : "timeout",
                    Math.Max(settleFill, fillImm));
        }
        catch (HttpRequestException ex) when (ex.StatusCode == System.Net.HttpStatusCode.TooManyRequests)
        {
            Emit(execLog, $"[KALSHI RATE LIMIT] {ticker} — 429, retrying in 1s");
            DebugLog.Trades($"PlaceKalshiLegAsync: 429 on {ticker}, backing off 1s");
            await Task.Delay(1_000);
            try
            {
                string retryId = $"CAXARB_{ticker}_{DateTimeOffset.UtcNow.ToUnixTimeMilliseconds()}R";
                var (oid2, st2, fi2) = await _kalshi.PlaceOrderAsync(
                    ticker, side, priceCents, count, clientOrderId: retryId);
                Interlocked.Exchange(ref _kalshiConsecErrors, 0);
                Console.ForegroundColor = ConsoleColor.Green;
                Emit(execLog, $"[FILL K]  {ticker} 429-retry placed oid={oid2} status={st2} fill={fi2}");
                Console.ResetColor();
                DebugLog.Trades($"PlaceKalshiLegAsync: 429-retry placed oid={oid2} status={st2} fill={fi2}");
                return (oid2, st2, fi2);
            }
            catch (Exception retryEx)
            {
                Emit(execLog, $"[KALSHI LEG ERROR] {ticker} (after 429): {ApiErrorHelper.ClassifyKalshi(retryEx)}");
                DebugLog.Trades($"PlaceKalshiLegAsync: 429 retry failed for {ticker}: {retryEx.Message}");
                await CheckMaintenanceThresholdAsync("kalshi", Interlocked.Increment(ref _kalshiConsecErrors));
                return ("", "error", 0m);
            }
        }
        catch (HttpRequestException ex) when (ex.StatusCode == System.Net.HttpStatusCode.NotFound)
        {
            // 404 = market doesn't exist or has been delisted — blocklist it so we never fire again.
            Emit(execLog, $"[KALSHI LEG ERROR] {ticker}: 404 — market not found, blocklisting");
            DebugLog.Trades($"PlaceKalshiLegAsync: 404 on {ticker}");
            BlocklistKalshiTicker(ticker);
            return ("", "error", 0m);
        }
        catch (Exception ex)
        {
            Emit(execLog, $"[KALSHI LEG ERROR] {ticker}: {ApiErrorHelper.ClassifyKalshi(ex)}");
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
        string tokenId, decimal price, decimal shares, bool negRisk = false, List<string>? execLog = null)
    {
        string tokenShort = tokenId[..Math.Min(12, tokenId.Length)];
        Emit(execLog, $"[ORDER P] BUY token={tokenShort}... price={price:0.0000} shares={shares}");
        DebugLog.Trades($"PlacePolyLegAsync: token={tokenShort}... price={price:0.0000} shares={shares}");
        try
        {
            decimal limitPrice = Math.Min(0.99m, price);
            DebugLog.Trades($"PlacePolyLegAsync: limitPrice={limitPrice:0.0000} (evaluated arb price)");

            string result = "";
            // Lazy-fetch fee rate once per token; cache for all subsequent calls.
            // Polymarket requires feeRateBps to match the market's rate exactly when non-zero.
            if (!_polyFeeRates.ContainsKey(tokenId))
                _polyFeeRates[tokenId] = await _poly.GetTakerFeeAsync(tokenId);
            int feeRate = _polyFeeRates[tokenId];
            string tickSize = _polyTickSizes.GetValueOrDefault(tokenId, "0.01");
            for (int attempt = 0; attempt < 3; attempt++)
            {
                try
                {
                    DebugLog.Trades($"PlacePolyLegAsync: attempt {attempt + 1} negRisk={negRisk} feeRateBps={feeRate} tickSize={tickSize}");
                    result = await _poly.SubmitOrderAsync(
                        tokenId, limitPrice, shares, side: 0 /*BUY*/,
                        negRisk: negRisk, tickSize: tickSize, feeRateBps: feeRate);
                    break;
                }
                catch (Exception ex) when (
                    (ex.Message.Contains("order_version_mismatch") || ex.Message.Contains("invalid signature")) && !negRisk)
                {
                    Console.ForegroundColor = ConsoleColor.Yellow;
                    Emit(execLog, $"[FILL P WARN] {tokenShort}... {(ex.Message.Contains("invalid signature") ? "invalid signature" : "order_version_mismatch")} — pair tagged non-neg-risk but market is neg-risk; retrying with NEG_RISK_EXCHANGE");
                    Console.ResetColor();
                    negRisk = true;
                }
                catch (Exception ex) when (
                    ex.Message.Contains("invalid fee rate") &&
                    ex.Message.Contains("taker fee:"))
                {
                    var m = Regex.Match(ex.Message, @"taker fee:\s*(\d+)");
                    if (m.Success && int.TryParse(m.Groups[1].Value, out int fee))
                    {
                        DebugLog.Trades($"PlacePolyLegAsync: fee autocorrect — retrying with feeRateBps={fee}");
                        _polyFeeRates[tokenId] = fee;
                        feeRate = fee;
                    }
                    else
                        throw;
                }
            }

            if (string.IsNullOrEmpty(result))
            {
                Emit(execLog, $"[FILL P WARN] {tokenShort}... empty result from SubmitOrderAsync");
                DebugLog.Trades($"PlacePolyLegAsync: empty result from SubmitOrderAsync");
                return (0m, 0m);
            }
            Interlocked.Exchange(ref _polyConsecErrors, 0);

            using var doc = JsonDocument.Parse(result);
            var root = doc.RootElement;

            if (!root.TryGetProperty("success", out var sv) || !sv.GetBoolean())
            {
                Emit(execLog, $"[FILL P WARN] {tokenShort}... success=false — {result[..Math.Min(200, result.Length)]}");
                DebugLog.Trades($"PlacePolyLegAsync: success=false in response — {result[..Math.Min(200, result.Length)]}");
                return (0m, 0m);
            }

            string orderId = root.TryGetProperty("orderID", out var oidEl) ? oidEl.GetString() ?? "" : "";
            string respStatus = root.TryGetProperty("status", out var stEl) ? stEl.GetString() ?? "" : "";
            Console.ForegroundColor = ConsoleColor.Green;
            Emit(execLog, $"[FILL P]  {tokenShort}... placed orderID={orderId} status={respStatus}");
            Console.ResetColor();
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
                    Emit(execLog, $"[FILL P WARN] {tokenShort}... poll failed for orderID={orderId}: {pollEx.Message}");
                    DebugLog.Trades($"PlacePolyLegAsync: poll failed for orderID={orderId}: {pollEx.Message}");
                }
            }

            if (filledShares <= 0)
            {
                Emit(execLog, $"[FILL P WARN] {tokenShort}... filledShares=0 after response+poll — FAK killed or no liquidity");
                DebugLog.Trades($"PlacePolyLegAsync: filledShares=0 after response+poll — FAK killed or no liquidity");
                return (0m, 0m);
            }

            decimal avgPrice = spentUsdc > 0 ? spentUsdc / filledShares : price;
            Console.ForegroundColor = ConsoleColor.Green;
            Emit(execLog, $"[FILL P]  {tokenShort}... filled={filledShares} avgPrice={avgPrice:0.0000}");
            Console.ResetColor();
            DebugLog.Trades($"PlacePolyLegAsync: filled={filledShares} avgPrice={avgPrice:0.0000}");
            _ = _poly.UpdateBalanceAllowanceAsync(tokenId); // give CLOB a head start on settlement
            return (filledShares, avgPrice);
        }
        catch (HttpRequestException ex) when (ex.StatusCode == System.Net.HttpStatusCode.TooManyRequests)
        {
            Emit(execLog, $"[POLY RATE LIMIT] {tokenShort}... — 429, retrying in 1s");
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
                            Console.ForegroundColor = ConsoleColor.Green;
                            Emit(execLog, $"[FILL P]  {tokenShort}... 429-retry filled={fs2} avg={avg2:0.0000}");
                            Console.ResetColor();
                            DebugLog.Trades($"PlacePolyLegAsync: 429-retry filled={fs2} avg={avg2:0.0000}");
                            _ = _poly.UpdateBalanceAllowanceAsync(tokenId);
                            return (fs2, avg2);
                        }
                    }
                }
            }
            catch (Exception retryEx)
            {
                Emit(execLog, $"[POLY LEG ERROR] {tokenShort}... (after 429): {ApiErrorHelper.ClassifyPoly(retryEx)}");
                DebugLog.Trades($"PlacePolyLegAsync: 429 retry failed for {tokenShort}: {retryEx.Message}");
            }
            await CheckMaintenanceThresholdAsync("poly", Interlocked.Increment(ref _polyConsecErrors));
            return (0m, 0m);
        }
        catch (Exception ex)
        {
            Emit(execLog, $"[POLY LEG ERROR] {tokenShort}...: {ApiErrorHelper.ClassifyPoly(ex)}");
            DebugLog.Trades($"PlacePolyLegAsync exception for {tokenShort}: {ex}");
            await CheckMaintenanceThresholdAsync("poly", Interlocked.Increment(ref _polyConsecErrors));
            return (0m, 0m);
        }
    }

    // ── Poly FAK sell (reversal) ──────────────────────────────────────────────

    private async Task<(decimal SoldShares, decimal AvgPrice)> PlacePolySellAsync(
        string tokenId, decimal shares, bool negRisk = false, List<string>? execLog = null)
    {
        // Poly CLOB rejects sell maker amounts with more than 2 decimal places.
        shares = Math.Floor(shares * 100m) / 100m;
        if (shares <= 0m) return (0m, 0m);

        string tokenShort = tokenId[..Math.Min(12, tokenId.Length)];
        Emit(execLog, $"[ORDER P] SELL token={tokenShort}... shares={shares}");
        DebugLog.Trades($"PlacePolySellAsync: token={tokenShort}... shares={shares}");
        try
        {
            // Force CLOB to refresh its cached token balance from on-chain state.
            // Required — tokens may not be settled yet after the buy leg.
            try { await _poly.UpdateBalanceAllowanceAsync(tokenId); } catch { /* best-effort */ }

            // FAK sell: 0.01 floor so it matches any buyer; actual fill is at best bid
            string result = "";
            int feeRate = _polyFeeRates.GetValueOrDefault(tokenId, 0);
            const int maxSellRetries = 10;

            for (int attempt = 1; attempt <= maxSellRetries; attempt++)
            {
                try
                {
                    result = await _poly.SubmitOrderAsync(
                        tokenId, 0.01m, shares, side: 1 /*SELL*/, negRisk: negRisk, feeRateBps: feeRate);
                    break;
                }
                catch (Exception ex) when (
                    (ex.Message.Contains("order_version_mismatch") || ex.Message.Contains("invalid signature")) && !negRisk)
                {
                    Console.ForegroundColor = ConsoleColor.Yellow;
                    Emit(execLog, $"[FILL P WARN] {tokenShort}... {(ex.Message.Contains("invalid signature") ? "invalid signature" : "order_version_mismatch")} on sell — pair tagged non-neg-risk but market is neg-risk; retrying with NEG_RISK_EXCHANGE");
                    Console.ResetColor();
                    negRisk = true;
                }
                catch (Exception ex) when (
                    ex.Message.Contains("invalid fee rate") &&
                    ex.Message.Contains("taker fee:"))
                {
                    var m = Regex.Match(ex.Message, @"taker fee:\s*(\d+)");
                    if (m.Success && int.TryParse(m.Groups[1].Value, out int fee))
                    {
                        DebugLog.Trades($"PlacePolySellAsync: fee autocorrect — retrying with feeRateBps={fee}");
                        _polyFeeRates[tokenId] = fee;
                        feeRate = fee;
                    }
                    else throw;
                }
                catch (Exception ex) when (
                    ex.Message.Contains("not enough balance", StringComparison.OrdinalIgnoreCase) &&
                    attempt < maxSellRetries)
                {
                    if (attempt == 1 || attempt % 3 == 0)
                        Emit(execLog, $"[SELL HAMMER] {tokenShort}... tokens not settled yet. Retrying in 500ms... ({attempt}/{maxSellRetries})");
                    await Task.Delay(500);
                    try { await _poly.UpdateBalanceAllowanceAsync(tokenId); } catch { /* best-effort */ }
                }
            }

            if (string.IsNullOrEmpty(result)) return (0m, 0m);

            using var doc = JsonDocument.Parse(result);
            var root = doc.RootElement;
            if (!root.TryGetProperty("success", out var sv) || !sv.GetBoolean())
            {
                Emit(execLog, $"[FILL P WARN] {tokenShort}... sell success=false — {result[..Math.Min(200, result.Length)]}");
                DebugLog.Trades($"PlacePolySellAsync: success=false — {result[..Math.Min(200, result.Length)]}");
                return (0m, 0m);
            }

            // SELL: makingAmount = shares sold, takingAmount = USDC received
            (decimal soldShares, decimal usdcReceived) = ExtractPolyFill(root, isSell: true);
            decimal avgPrice = soldShares > 0 && usdcReceived > 0 ? usdcReceived / soldShares : 0m;
            Console.ForegroundColor = ConsoleColor.Green;
            Emit(execLog, $"[FILL P]  {tokenShort}... sold={soldShares} avgPrice={avgPrice:0.0000}");
            Console.ResetColor();
            DebugLog.Trades($"PlacePolySellAsync: soldShares={soldShares} avgPrice={avgPrice:0.0000}");
            return (soldShares, avgPrice);
        }
        catch (Exception ex)
        {
            Emit(execLog, $"[POLY SELL ERROR] {tokenShort}...: {ApiErrorHelper.ClassifyPoly(ex)}");
            DebugLog.Trades($"PlacePolySellAsync exception for {tokenShort}: {ex}");
            return (0m, 0m);
        }
    }

    // ── Unhedged delta recovery ───────────────────────────────────────────────

    // Relentlessly flatten a naked Kalshi leg: IOC-sell at the configurable floor so the order sweeps
    // the whole bid book (fills best-first, down to the floor), retrying for partials / settlement lag.
    // Returns (filledContracts, estLossUsd). Never halts — any unfilled remainder is the caller's to orphan.
    private async Task<(int Filled, decimal Loss)> ReverseKalshiLegAsync(
        CrossPair pair, string kalshiSide, int qty, decimal entryAsk, string kBookKey,
        string execId, List<string>? execLog)
    {
        int     remaining   = qty;
        int     totalFilled = 0;
        decimal totalLoss   = 0m;
        // Cached bid is only a loss-accounting proxy (the order API doesn't return the reversal fill
        // price); the ORDER limit is the floor so it sweeps regardless of how stale the cache is.
        decimal proxyBid = _books.TryGetValue(kBookKey, out var kBook) ? kBook.GetBestBidPrice() : 0m;

        for (int attempt = 1; attempt <= _reverseMaxAttempts && remaining > 0; attempt++)
        {
            decimal fill;
            try
            {
                var (_, _, f) = await _kalshi.PlaceOrderAsync(
                    pair.KalshiTicker, kalshiSide, _reverseFloorCents, remaining, action: "sell");
                fill = f;
            }
            catch (Exception ex)
            {
                // 429s are already retried inside the client; reaching here is a harder error
                // (network / venue paused). Stop sweeping — the caller orphans the remainder.
                await JournalAsync(JsonSerializer.Serialize(new {
                    t = DateTime.UtcNow, @event = "CLEANUP_REVERSE_ERROR", execId,
                    pair = pair.PairId, leg = "kalshi", attempt,
                    qty = remaining, exType = ex.GetType().Name, message = ex.Message
                }));
                Emit(execLog, $"[RECOVER ERROR] {pair.Label} | Kalshi reverse attempt {attempt}: {ApiErrorHelper.ClassifyKalshi(ex)}");
                break;
            }

            if (fill > 0)
            {
                int     fi      = (int)fill;
                decimal proxyPx = proxyBid > 0m ? proxyBid : _reverseFloorCents / 100m;
                decimal loss    = fi * Math.Max(0m, entryAsk - proxyPx);
                totalLoss   += loss;
                totalFilled += fi;
                remaining   -= fi;
                await JournalAsync(JsonSerializer.Serialize(new {
                    t = DateTime.UtcNow, @event = "CLEANUP_REVERSED", execId,
                    pair = pair.PairId, leg = "kalshi", qty = fi, attempt,
                    entryPrice = entryAsk, floorCents = _reverseFloorCents, estLoss = Math.Round(loss, 4)
                }));
                Console.ForegroundColor = ConsoleColor.Yellow;
                Emit(execLog, $"[RECOVER REVERSED] {pair.Label} | swept {fi} Kalshi {kalshiSide} (attempt {attempt}), {remaining} left");
                Console.ResetColor();
            }
            else
            {
                await JournalAsync(JsonSerializer.Serialize(new {
                    t = DateTime.UtcNow, @event = "CLEANUP_REVERSE_FAILED", execId,
                    pair = pair.PairId, leg = "kalshi", attempt, qty = remaining, reason = "zero_fill"
                }));
            }
            if (remaining > 0 && attempt < _reverseMaxAttempts)
                await Task.Delay(400);   // let the book refill / settlement catch up before re-sweeping
        }

        if (totalLoss > 0m) lock (_cleanupLock) { _totalCleanupCostUsd += totalLoss; }
        return (totalFilled, totalLoss);
    }

    // Park a leg we couldn't flatten right now (venue paused/closed, or no bids). Ops policy: do NOT
    // halt — block re-entry on this pair and keep trading everything else; clear it manually later.
    private async Task OrphanPairAsync(
        CrossPair pair, string leg, decimal qty, decimal valueUsd, string execId, List<string>? execLog)
    {
        _orphanedPairs[pair.PairId] = 0;
        DiscordAlert($"⚠️ Orphaned: {pair.Label} — {leg} leg couldn't be flattened ({qty:0.##} ≈ ${valueUsd:0.00}); re-entry blocked, sitting un-hedged until manual clear.");
        await JournalAsync(JsonSerializer.Serialize(new {
            t = DateTime.UtcNow, @event = "CLEANUP_ORPHANED", execId,
            pair = pair.PairId, leg,
            unresolvedQty = Math.Round(qty, 6), unresolvedValueUsd = Math.Round(valueUsd, 4)
        }));
        Console.ForegroundColor = ConsoleColor.Magenta;
        Emit(execLog, $"[ORPHANED] {pair.Label} | {leg} leg couldn't be flattened now ({qty:0.##} ≈ ${valueUsd:0.00}) — " +
                      "re-entry blocked, bot keeps running. Clear manually when the venue reopens.");
        Console.ResetColor();
    }

    private async Task<RecoveryResult> RecoverUnhedgedAsync(
        CrossPair pair, string arbType,
        string kalshiSide, string polyToken,
        decimal kFilled, decimal pFilled,
        decimal kLegAsk, decimal pActualPrice,
        string execId = "", List<string>? execLog = null)
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
                Emit(execLog, $"[CLEANUP DUST] {pair.Label} | Absorbing {kUnhedged} Kalshi dust (${kUnhedgedValue:0.00}) — no halt");
                Console.ResetColor();
                return new RecoveryResult("DUST_ABSORBED_KALSHI", kUnhedged, kUnhedgedValue);
            }

            bool hasPolyBook = _books.TryGetValue($"P:{polyToken}", out var pBook);
            if (!hasPolyBook)
            {
                DebugLog.Trades($"RecoverUnhedgedAsync {pair.Label}: Poly book missing for {polyToken} — skipping hedge, falling through to reverse");
                Emit(execLog, $"[RECOVER] {pair.Label} | Poly book missing — skipping hedge, falling through to reverse");
            }
            bool skipHedgeA = kUnhedgedValue < CleanupHedgeSkipUsd || !hasPolyBook;

            if (!skipHedgeA)
            {
                decimal currentPolyAsk = pBook!.GetBestAskPrice();
                decimal hedgeNet = kLegAsk + currentPolyAsk + KalshiFee(kLegAsk) + PolyFee(currentPolyAsk, polyToken);
                DebugLog.Trades($"RecoverUnhedgedAsync {pair.Label}: kUnhedged={kUnhedged} polyAsk={currentPolyAsk:0.0000} hedgeNet={hedgeNet:0.0000}");

                if (hedgeNet <= _hedgeMaxNet)
                {
                    Console.ForegroundColor = ConsoleColor.Yellow;
                    Emit(execLog, $"[RECOVER] {pair.Label} | kExcess={kUnhedged} hedgeNet={hedgeNet:0.0000} — completing hedge on Poly");
                    Console.ResetColor();

                    decimal polyHedgeLimit = Math.Min(1.0m, currentPolyAsk + RecoveryHedgeSlippageCents / 100m);
                    // Skip hedge if fractional Kalshi fill makes Poly order < $1 CLOB minimum
                    decimal polyHedgeCost  = Math.Floor(polyHedgeLimit * 100m) / 100m * kUnhedged;
                    bool polyHedgeTooSmall = polyHedgeCost < 1.00m;
                    // Symmetry with Case B: never let a recovery hedge push total open exposure past the cap.
                    bool polyHedgeBreachesCap;
                    lock (_exposureLock) polyHedgeBreachesCap = _totalExposure + polyHedgeCost > _maxExposureUsd;
                    bool skipPolyHedge = polyHedgeTooSmall || polyHedgeBreachesCap;
                    if (polyHedgeTooSmall)
                        Emit(execLog, $"[RECOVER] {pair.Label} | Poly hedge cost ${polyHedgeCost:0.00} < $1 min — reversing Kalshi excess directly");
                    else if (polyHedgeBreachesCap)
                        Emit(execLog, $"[RECOVER] {pair.Label} | Poly hedge ${polyHedgeCost:0.00} would push exposure ${_totalExposure:0.00}→${_totalExposure + polyHedgeCost:0.00} past cap ${_maxExposureUsd:0.00} — reversing Kalshi excess instead");
                    (decimal polyFill2, decimal polyFill2Price) = skipPolyHedge ? (0m, 0m)
                        : await PlacePolyLegAsync(polyToken, polyHedgeLimit, kUnhedged, pair.IsNegRisk, execLog);
                    if (polyFill2 > 0)
                    {
                        decimal additional   = Math.Min(kUnhedged, polyFill2);
                        decimal remainderQty = kUnhedged - additional;     // Kalshi left unhedged if the hedge UNDER-filled
                        decimal polyExcess   = polyFill2 - additional;     // naked Poly bought past the need if it OVER-filled
                        if (_openPositions.TryGetValue(pair.PairId, out var pos))
                        {
                            decimal newP = pos.PolyShares + additional;
                            _openPositions[pair.PairId] = pos with
                            {
                                KalshiContracts = pos.KalshiContracts + additional,
                                PolyShares      = newP,
                                PolyEntryPrice  = newP > 0
                                    ? (pos.PolyShares * pos.PolyEntryPrice + additional * polyFill2Price) / newP
                                    : polyFill2Price
                            };
                        }
                        else
                            _openPositions[pair.PairId] = new ArbPosition(
                                pair.PairId, arbType, additional, additional,
                                kLegAsk, polyFill2Price, DateTime.UtcNow, execId);
                        lock (_exposureLock) { _totalExposure += additional * polyFill2Price; }
                        await JournalAsync(JsonSerializer.Serialize(new {
                            t = DateTime.UtcNow, @event = "CLEANUP_HEDGE_COMPLETED", execId,
                            pair = pair.PairId, leg = "poly",
                            hedgedQty = additional, remainderQty = Math.Round(remainderQty, 6),
                            polyFillPrice = Math.Round(polyFill2Price, 6)
                        }));
                        Console.ForegroundColor = ConsoleColor.Green;
                        Emit(execLog, $"[RECOVER OK] {pair.Label} | hedge completed +{additional} sets via Poly retry");
                        Console.ResetColor();
                        if (remainderQty > 0m)
                        {
                            decimal remValue = remainderQty * kLegAsk;
                            if (remValue < CleanupDustUsd)
                            {
                                lock (_cleanupLock) { _totalCleanupCostUsd += remValue; }
                                await JournalAsync(JsonSerializer.Serialize(new {
                                    t = DateTime.UtcNow, @event = "CLEANUP_DUST", execId,
                                    pair = pair.PairId, leg = "kalshi_partial_hedge_remainder",
                                    qty = Math.Round(remainderQty, 6), absorbedUsd = Math.Round(remValue, 4)
                                }));
                            }
                            else
                            {
                                // Partial Poly hedge left Kalshi contracts unhedged — reverse them out
                                // relentlessly, orphan only if the venue won't fill. Never halt.
                                string rkKey = arbType == "K_YES_P_NO" ? $"K:{pair.KalshiTicker}" : $"K:{pair.KalshiTicker}_NO";
                                var (rf, _) = await ReverseKalshiLegAsync(
                                    pair, kalshiSide, (int)remainderQty, kLegAsk, rkKey, execId, execLog);
                                int left = (int)remainderQty - rf;
                                if (left > 0)
                                {
                                    await OrphanPairAsync(pair, "kalshi_partial_hedge_remainder", left, left * kLegAsk, execId, execLog);
                                    return new RecoveryResult("ORPHANED", additional, 0);
                                }
                            }
                        }
                        // The hedge FAK is dollar-denominated — a fill below our (bumped) limit over-buys.
                        // Own that excess: sell it back to the intended size (or absorb if dust) so it's
                        // never left untracked (the Cawthorn bug), and flag the pair if the over-buy is
                        // extreme enough to signal a mismatched token.
                        if (polyExcess > 0m)
                        {
                            decimal exValue = polyExcess * polyFill2Price;
                            if (exValue < CleanupDustUsd)
                            {
                                lock (_cleanupLock) { _totalCleanupCostUsd += exValue; }
                                await JournalAsync(JsonSerializer.Serialize(new {
                                    t = DateTime.UtcNow, @event = "CLEANUP_DUST", execId,
                                    pair = pair.PairId, leg = "poly_hedge_overfill",
                                    qty = Math.Round(polyExcess, 6), absorbedUsd = Math.Round(exValue, 4)
                                }));
                                Console.ForegroundColor = ConsoleColor.DarkYellow;
                                Emit(execLog, $"[CLEANUP DUST] {pair.Label} | Absorbing {polyExcess:0.00} Poly hedge-overfill dust (${exValue:0.00}) — no halt");
                                Console.ResetColor();
                            }
                            else
                            {
                                var (exSold, exPx) = await PlacePolySellAsync(polyToken, polyExcess, pair.IsNegRisk, execLog);
                                decimal exLoss = Math.Max(0m, exSold * (polyFill2Price - exPx));
                                lock (_cleanupLock) { _totalCleanupCostUsd += exLoss; }
                                await JournalAsync(JsonSerializer.Serialize(new {
                                    t = DateTime.UtcNow, @event = "CLEANUP_REVERSED", execId,
                                    pair = pair.PairId, leg = "poly_hedge_overfill",
                                    reason = "OVERFILL",
                                    soldShares = Math.Round(exSold, 6), soldPrice = Math.Round(exPx, 6),
                                    reversalLossUsd = Math.Round(exLoss, 4)
                                }));
                                Console.ForegroundColor = ConsoleColor.Yellow;
                                Emit(execLog, $"[RECOVER] {pair.Label} | reversed {exSold:0.00}/{polyExcess:0.00} Poly hedge-overfill @ {exPx:0.0000} loss=${exLoss:0.00}");
                                Console.ResetColor();
                            }
                        }
                        return new RecoveryResult("HEDGE_COMPLETED", additional, 0);
                    }
                    Emit(execLog, $"[RECOVER] {pair.Label} | Poly hedge retry failed — reversing Kalshi excess");
                }
                else
                {
                    Emit(execLog, $"[RECOVER] {pair.Label} | hedgeNet={hedgeNet:0.0000} >= 1.0 — reversing {kUnhedged} Kalshi {kalshiSide} directly");
                }
            }
            else if (kUnhedgedValue < CleanupHedgeSkipUsd)
            {
                Emit(execLog, $"[CLEANUP SKIP HEDGE] {pair.Label} | kExcess={kUnhedged} value=${kUnhedgedValue:0.00} < ${CleanupHedgeSkipUsd:0.00} — reversing directly");
            }

            // Reverse: relentlessly sweep the excess Kalshi contracts out at the floor (re-reading the
            // live book is moot — the floor limit fills against whatever bids exist). Orphan, never halt,
            // anything that still can't fill: a paused/closed market we'll clear on the next run.
            string kBookKey = arbType == "K_YES_P_NO" ? $"K:{pair.KalshiTicker}" : $"K:{pair.KalshiTicker}_NO";
            var (kRevFilled, kRevLoss) = await ReverseKalshiLegAsync(
                pair, kalshiSide, (int)kUnhedged, kLegAsk, kBookKey, execId, execLog);
            int kStillOpen = (int)kUnhedged - kRevFilled;
            if (kStillOpen <= 0)
                return new RecoveryResult("REVERSED_KALSHI", kRevFilled, kRevLoss);
            await OrphanPairAsync(pair, "kalshi", kStillOpen, kStillOpen * kLegAsk, execId, execLog);
            return new RecoveryResult("ORPHANED", kRevFilled, kRevLoss);
        }

        // ── Case B: Poly filled more — own excess Poly shares ────────────────
        if (pUnhedged > 0)
        {
            decimal pUnhedgedValue = pUnhedged * pActualPrice;

            // Shared helper: sell the entire Poly excess and record it. Used by the over-fill guard
            // and the exposure backstop so neither duplicates the sell/journal/lock logic.
            // Returns null when the sell filled nothing; caller falls through to hedge/reverse/halt.
            async Task<RecoveryResult?> ReverseExcessPolyAsync(string reason)
            {
                var (revSold, revPrice) = await PlacePolySellAsync(polyToken, pUnhedged, pair.IsNegRisk, execLog);
                if (revSold <= 0m) return null;
                decimal reversalLoss = Math.Max(0m, revSold * (pActualPrice - revPrice));
                lock (_cleanupLock) { _totalCleanupCostUsd += reversalLoss; }
                await JournalAsync(JsonSerializer.Serialize(new {
                    t = DateTime.UtcNow, @event = "CLEANUP_REVERSED", execId,
                    pair = pair.PairId, leg = "poly", reason,
                    soldShares = Math.Round(revSold, 6), soldPrice = Math.Round(revPrice, 6),
                    reversalLossUsd = Math.Round(reversalLoss, 4)
                }));
                Console.ForegroundColor = ConsoleColor.Yellow;
                Emit(execLog, $"[RECOVER] {pair.Label} | reversed {revSold:0.00} excess Poly @ {revPrice:0.0000} ({reason}) loss=${reversalLoss:0.00}");
                Console.ResetColor();
                return new RecoveryResult("REVERSED_POLY", revSold, reversalLoss);
            }

            if (pUnhedgedValue < CleanupDustUsd)
            {
                lock (_cleanupLock) { _totalCleanupCostUsd += pUnhedgedValue; }
                await JournalAsync(JsonSerializer.Serialize(new {
                    t = DateTime.UtcNow, @event = "CLEANUP_DUST", execId,
                    pair = pair.PairId, leg = "poly", qty = pUnhedged, absorbedUsd = pUnhedgedValue
                }));
                Console.ForegroundColor = ConsoleColor.DarkYellow;
                Emit(execLog, $"[CLEANUP DUST] {pair.Label} | Absorbing {pUnhedged:0.00} Poly dust (${pUnhedgedValue:0.00}) — no halt");
                Console.ResetColor();
                return new RecoveryResult("DUST_ABSORBED_POLY", pUnhedged, pUnhedgedValue);
            }

            // Fix 1 — over-fill guard: a Poly fill far above the balanced size is an accidental
            // over-buy. Never enlarge the position by hedging it on Kalshi — reverse the excess.
            if (pUnhedged > balancedQty * MaxHedgeOverfillFraction)
            {
                var r = await ReverseExcessPolyAsync("OVERFILL");
                if (r is not null) return r;
                Emit(execLog, $"[RECOVER] {pair.Label} | over-fill reverse sold 0 — falling through");
            }

            string kHedgeKey = arbType == "K_YES_P_NO" ? $"K:{pair.KalshiTicker}" : $"K:{pair.KalshiTicker}_NO";
            bool hasKalshiBook = _books.TryGetValue(kHedgeKey, out var kHedgeBook);
            if (!hasKalshiBook)
            {
                DebugLog.Trades($"RecoverUnhedgedAsync {pair.Label}: Kalshi book missing for {kHedgeKey} — skipping hedge, falling through to reverse");
                Emit(execLog, $"[RECOVER] {pair.Label} | Kalshi book missing — skipping hedge, falling through to reverse");
            }
            bool skipHedgeB = pUnhedgedValue < CleanupHedgeSkipUsd || !hasKalshiBook;

            if (!skipHedgeB)
            {
                decimal currentKalshiAsk = kHedgeBook!.GetBestAskPrice();
                int currentKCents = Math.Max(1, (int)Math.Ceiling(currentKalshiAsk * 100) + RecoveryHedgeSlippageCents);
                decimal hedgeNet = currentKalshiAsk + pActualPrice + KalshiFee(currentKalshiAsk) + PolyFee(pActualPrice, polyToken);
                DebugLog.Trades($"RecoverUnhedgedAsync {pair.Label}: pUnhedged={pUnhedged} kalshiAsk={currentKalshiAsk:0.0000} hedgeNet={hedgeNet:0.0000}");

                if (hedgeNet <= _hedgeMaxNet)
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
                        Emit(execLog, $"[CLEANUP DUST] {pair.Label} | Absorbing {pUnhedged:0.0000} fractional Poly dust (${fracValue:0.00}) — sub-1-share, can't hedge on Kalshi");
                        Console.ResetColor();
                        return new RecoveryResult("DUST_ABSORBED_POLY", pUnhedged, fracValue);
                    }

                    // Fix 3 — capital backstop: never let a recovery hedge push total open exposure
                    // past the cap. _totalExposure holds each open position's reservation until it
                    // closes, so this is real cumulative exposure, not just in-flight reservations.
                    decimal hedgeCost = hedgeQty * currentKalshiAsk;
                    bool breachesCap;
                    lock (_exposureLock) breachesCap = _totalExposure + hedgeCost > _maxExposureUsd;
                    if (breachesCap)
                    {
                        Console.ForegroundColor = ConsoleColor.Yellow;
                        Emit(execLog, $"[RECOVER] {pair.Label} | hedge ${hedgeCost:0.00} would push exposure " +
                                      $"${_totalExposure:0.00}→${_totalExposure + hedgeCost:0.00} past cap ${_maxExposureUsd:0.00} — reversing instead");
                        Console.ResetColor();
                        var r = await ReverseExcessPolyAsync("EXPOSURE_CAP");
                        if (r is not null) return r;
                        Emit(execLog, $"[RECOVER] {pair.Label} | exposure-cap reverse sold 0 — proceeding as last resort");
                    }

                    Console.ForegroundColor = ConsoleColor.Yellow;
                    Emit(execLog, $"[RECOVER] {pair.Label} | pExcess={pUnhedged:0.0000} hedgeQty={hedgeQty} hedgeNet={hedgeNet:0.0000} — completing hedge on Kalshi");
                    Console.ResetColor();

                    var (_, _, kFill2) = await PlaceKalshiLegAsync(
                        pair.KalshiTicker, kalshiSide, currentKCents, hedgeQty, execId + "_RH", execLog);
                    if (kFill2 > 0)
                    {
                        decimal additional    = Math.Min((decimal)hedgeQty, kFill2);
                        decimal remainderQty  = pUnhedged - additional;
                        if (_openPositions.TryGetValue(pair.PairId, out var pos))
                        {
                            decimal newK = pos.KalshiContracts + additional;
                            _openPositions[pair.PairId] = pos with
                            {
                                KalshiContracts  = newK,
                                PolyShares       = pos.PolyShares + additional,
                                // currentKalshiAsk is the IOC limit used — best proxy for fill price
                                KalshiEntryPrice = newK > 0
                                    ? (pos.KalshiContracts * pos.KalshiEntryPrice + additional * currentKalshiAsk) / newK
                                    : currentKalshiAsk
                            };
                        }
                        else
                            _openPositions[pair.PairId] = new ArbPosition(
                                pair.PairId, arbType, additional, additional,
                                currentKalshiAsk, pActualPrice, DateTime.UtcNow, execId);
                        // Keep the exposure reservation aligned with the contracts the hedge added,
                        // so the cap stays accurate for future trades and the close-time release matches.
                        lock (_exposureLock) { _totalExposure += additional * currentKalshiAsk; }
                        await JournalAsync(JsonSerializer.Serialize(new {
                            t = DateTime.UtcNow, @event = "CLEANUP_HEDGE_COMPLETED", execId,
                            pair = pair.PairId, leg = "kalshi",
                            hedgedQty = additional, remainderQty = Math.Round(remainderQty, 6),
                            kFillPrice = Math.Round(currentKalshiAsk, 6)
                        }));
                        if (remainderQty > 0m)
                        {
                            decimal remValue = remainderQty * pActualPrice;
                            // Sub-1-share or negligible value: genuinely can't hedge on Kalshi (min 1 contract)
                            if (remainderQty < 1.0m || remValue < CleanupDustUsd)
                            {
                                lock (_cleanupLock) { _totalCleanupCostUsd += remValue; }
                                await JournalAsync(JsonSerializer.Serialize(new {
                                    t = DateTime.UtcNow, @event = "CLEANUP_DUST", execId,
                                    pair = pair.PairId, leg = "poly_partial_hedge_remainder",
                                    qty = Math.Round(remainderQty, 6), absorbedUsd = Math.Round(remValue, 4)
                                }));
                                DebugLog.Trades($"RecoverUnhedgedAsync {pair.Label}: absorbing {remainderQty:0.0000} remainder (${remValue:0.00})");
                            }
                            else
                            {
                                // Partial Kalshi hedge left Poly shares unhedged — sell them back
                                // (PlacePolySellAsync sweeps at the floor with settlement retries), orphan
                                // only if it still won't fill. Never halt.
                                var (rSold, rPx) = await PlacePolySellAsync(polyToken, remainderQty, pair.IsNegRisk, execLog);
                                if (rSold > 0m)
                                {
                                    decimal rLoss = Math.Max(0m, rSold * (pActualPrice - rPx));
                                    if (rLoss > 0m) lock (_cleanupLock) { _totalCleanupCostUsd += rLoss; }
                                    await JournalAsync(JsonSerializer.Serialize(new {
                                        t = DateTime.UtcNow, @event = "CLEANUP_REVERSED", execId,
                                        pair = pair.PairId, leg = "poly_partial_hedge_remainder",
                                        qty = Math.Round(rSold, 6), reversalPrice = Math.Round(rPx, 6), loss = Math.Round(rLoss, 4)
                                    }));
                                }
                                decimal stillP = remainderQty - rSold;
                                if (stillP >= 1.0m)
                                {
                                    await OrphanPairAsync(pair, "poly_partial_hedge_remainder", stillP, stillP * pActualPrice, execId, execLog);
                                    return new RecoveryResult("ORPHANED", additional, 0);
                                }
                            }
                        }
                        Console.ForegroundColor = ConsoleColor.Green;
                        Emit(execLog, $"[RECOVER OK] {pair.Label} | hedge completed +{additional} sets via Kalshi retry");
                        Console.ResetColor();
                        return new RecoveryResult("HEDGE_COMPLETED", additional, 0);
                    }
                    Emit(execLog, $"[RECOVER] {pair.Label} | Kalshi hedge retry failed — reversing Poly excess");
                }
                else
                {
                    Emit(execLog, $"[RECOVER] {pair.Label} | hedgeNet={hedgeNet:0.0000} >= 1.0 — reversing {pUnhedged:0.00} Poly shares directly");
                }
            }
            else if (pUnhedgedValue < CleanupHedgeSkipUsd)
            {
                Emit(execLog, $"[CLEANUP SKIP HEDGE] {pair.Label} | pExcess={pUnhedged:0.00} value=${pUnhedgedValue:0.00} < ${CleanupHedgeSkipUsd:0.00} — reversing directly");
            }

            // Reverse: sell excess Poly shares back
            var (soldShares, soldPrice) = await PlacePolySellAsync(polyToken, pUnhedged, pair.IsNegRisk, execLog);
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
                Emit(execLog, $"[RECOVER REVERSED] {pair.Label} | sold {soldShares:0.00} Poly shares @ ${soldPrice:0.0000}");
                Console.ResetColor();
                return new RecoveryResult("REVERSED_POLY", soldShares, Math.Max(0m, reversalLoss));
            }
            // PlacePolySellAsync already swept at the 1¢ floor with settlement retries; a zero return
            // means the venue/token genuinely won't fill now. Orphan (not halt) and keep trading.
            await JournalAsync(JsonSerializer.Serialize(new {
                t = DateTime.UtcNow, @event = "CLEANUP_REVERSE_FAILED", execId,
                pair = pair.PairId, leg = "poly",
                qty = pUnhedged, reason = "zero_fill"
            }));
            await OrphanPairAsync(pair, "poly", pUnhedged, pUnhedgedValue, execId, execLog);
            return new RecoveryResult("ORPHANED", 0, pUnhedgedValue);
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

    // Hook target on the shared KalshiOrderClient: fire-and-forget journal of each 429 back-off.
    // Runs from inside an HTTP call, so it must not block the retry loop; JournalAsync is lock-serialized.
    private void OnKalshiRateLimitRetry(RateLimitRetryInfo r) => _ = JournalRateLimitRetryAsync(r);

    private async Task JournalRateLimitRetryAsync(RateLimitRetryInfo r)
    {
        try
        {
            await JournalAsync(JsonSerializer.Serialize(new {
                t = DateTime.UtcNow, @event = "KALSHI_RATE_LIMIT_RETRY",
                method = r.Method, path = r.Path, status = r.StatusCode,
                attempt = r.Attempt, maxAttempts = r.MaxAttempts, delaySeconds = r.DelaySeconds
            }));
        }
        catch { /* journaling must never disrupt trading */ }
    }

    private void OnPolyOrderRetry(PolyOrderRetryInfo r) => _ = JournalPolyOrderRetryAsync(r);

    private async Task JournalPolyOrderRetryAsync(PolyOrderRetryInfo r)
    {
        try
        {
            await JournalAsync(JsonSerializer.Serialize(new {
                t = DateTime.UtcNow, @event = "POLY_ORDER_RETRY",
                token = r.TokenId.Length > 12 ? r.TokenId[..12] + "..." : r.TokenId,
                status = r.StatusCode, attempt = r.Attempt, maxAttempts = r.MaxAttempts, delaySeconds = r.DelaySeconds
            }));
        }
        catch { /* journaling must never disrupt trading */ }
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
                kalshiByTicker.TryGetValue(pair.KalshiTicker, out int kPos);

                // Fast-path: no Kalshi position → can't have an unhedged Poly position.
                // Skip the 2 eth_calls — this avoids 2000+ RPC calls on a clean start.
                if (kPos == 0)
                {
                    report.Add(new ReconciliationEntry(pair.PairId, pair.Label, "CLEAN", 0, 0, ""));
                    continue;
                }

                decimal polyYes = await _poly.GetTokenBalanceAsync(pair.PolyYesTokenId);
                decimal polyNo  = await _poly.GetTokenBalanceAsync(pair.PolyNoTokenId);

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

    // ── Blocklist helpers ─────────────────────────────────────────────────────

    // persist=true: writes to cross_pair_blocklist.json (survives restarts).
    // persist=false: session-only — use for startup validation where transient errors are possible.
    private void BlocklistKalshiTicker(string ticker, bool persist = true)
    {
        lock (_blocklistLock) _blocklist.Add(ticker);
        Console.ForegroundColor = ConsoleColor.Yellow;
        string tag = persist ? "persisted" : "session-only";
        Console.WriteLine($"[BLOCKLIST] {ticker} added ({tag}) — skipped for all future executions this session");
        Console.ResetColor();
        if (!persist) return;
        try
        {
            string blPath = Path.Combine(AppContext.BaseDirectory, "cross_pair_blocklist.json");
            if (!File.Exists(blPath))
                blPath = Path.Combine(AppContext.BaseDirectory, "..", "..", "..", "cross_pair_blocklist.json");
            var existing = File.Exists(blPath)
                ? JsonSerializer.Deserialize<List<string>>(File.ReadAllText(blPath)) ?? []
                : new List<string>();
            if (!existing.Contains(ticker, StringComparer.OrdinalIgnoreCase))
            {
                existing.Add(ticker);
                File.WriteAllText(blPath, JsonSerializer.Serialize(existing,
                    new JsonSerializerOptions { WriteIndented = true }));
            }
        }
        catch (Exception ex) { Console.WriteLine($"[BLOCKLIST] Failed to persist {ticker}: {ex.Message}"); }
    }

    public async Task<List<CrossPair>> ValidatePairsAtStartupAsync(IEnumerable<CrossPair> pairs)
    {
        var pairList = pairs.Where(p => !_blocklist.Contains(p.KalshiTicker)).ToList();
        if (pairList.Count == 0) return [];
        bool checkPoly = _restVerifier != null;
        Console.WriteLine($"[VALIDATE] Checking {pairList.Count} pair(s) — Kalshi status + {(checkPoly ? "Poly tokens" : "Kalshi only")}...");
        int blocked = 0;
        foreach (var pair in pairList)
        {
            string? blockReason = null;

            // ── Kalshi ──────────────────────────────────────────────────────
            try
            {
                using var doc  = await _kalshi.GetMarketAsync(pair.KalshiTicker);
                var mkt        = doc.RootElement.TryGetProperty("market", out var m) ? m : doc.RootElement;
                string kStatus = mkt.TryGetProperty("status", out var sEl) ? sEl.GetString() ?? "" : "unknown";
                if (!string.Equals(kStatus, "active", StringComparison.OrdinalIgnoreCase))
                    blockReason = $"Kalshi status={kStatus}";
            }
            catch (HttpRequestException ex) when (ex.StatusCode == System.Net.HttpStatusCode.NotFound)
            {
                blockReason = "Kalshi 404";
            }
            catch (Exception ex)
            {
                Console.WriteLine($"[VALIDATE] {pair.Label} | Kalshi check error: {ex.Message}");
            }

            // ── Polymarket (both tokens) ─────────────────────────────────────
            // TODO: re-enable once CheckPolyTokenAsync is verified against negRisk markets.
            // The tick_size check was incorrectly blocking valid active pairs (negRisk tokens
            // may not return tick_size on the /book endpoint).
            // if (blockReason == null && checkPoly)
            // {
            //     bool yesOk = await _restVerifier!.CheckPolyTokenAsync(pair.PolyYesTokenId);
            //     bool noOk  = await _restVerifier!.CheckPolyTokenAsync(pair.PolyNoTokenId);
            //     if (!yesOk || !noOk)
            //         blockReason = !yesOk && !noOk ? "Poly YES+NO tokens invalid"
            //                     : !yesOk          ? "Poly YES token invalid"
            //                     :                   "Poly NO token invalid";
            // }

            if (blockReason != null)
            {
                // session-only: startup checks can fail transiently (timeout, geo-block).
                // Only execution-time 404s are persisted to cross_pair_blocklist.json.
                BlocklistKalshiTicker(pair.KalshiTicker, persist: false);
                Console.WriteLine($"[VALIDATE] {pair.Label} | {blockReason} — blocked (session-only)");
                await JournalAsync(JsonSerializer.Serialize(new {
                    t = DateTime.UtcNow, @event = "VALIDATE_BLOCKED",
                    pairId = pair.PairId, label = pair.Label,
                    ticker = pair.KalshiTicker, reason = blockReason,
                    sessionOnly = true
                }));
                blocked++;
            }
        }
        int open = pairList.Count - blocked;
        Console.WriteLine(blocked == 0
            ? $"[VALIDATE] All {pairList.Count} pair(s) verified."
            : $"[VALIDATE] {blocked} blocked, {open} open.");
        return pairList.Where(p => !_blocklist.Contains(p.KalshiTicker)).ToList();
    }

    public async Task ReconcileOnStartupAsync(IEnumerable<CrossPair> pairs)
    {
        Console.WriteLine("[RECONCILE] Checking both venues for open positions from prior runs...");
        var pairList = pairs.ToList();
        var entries = await ReconcilePositionsAsync(pairList);
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

        await RestorePositionsFromVenuesAsync(pairList);
        if (!_dryRun) await ValidatePairsAtStartupAsync(pairList); // return value ignored — blocklist updated as side-effect
    }

    private async Task RestorePositionsFromVenuesAsync(IEnumerable<CrossPair> pairs)
    {
        if (_dryRun) return;
        var pairList = pairs.ToList();
        if (pairList.Count == 0 || !File.Exists(_journalPath)) return;

        var pairByPairId = pairList.ToDictionary(p => p.PairId);

        // Scan journal forward: EXECUTION_COMPLETE seeds entry prices; CLEANUP_HEDGE_COMPLETED
        // fills in the price for the recovery leg; SETTLEMENT/EARLY_EXIT removes the pair.
        // JournalKQty is the net held qty (position.kHeld) — subtracts any REVERSED_KALSHI cleanup,
        // so it's used as the correct fallback when the Kalshi positions API is empty or flaky.
        var entryMap = new Dictionary<string, (string ArbType, decimal KEntry, decimal PEntry, decimal JournalKQty)>();
        // Buffer hedge fills separately and apply after the scan — CLEANUP_HEDGE_COMPLETED for a
        // from-zero entry is journaled BEFORE its EXECUTION_COMPLETE, so an inline patch would miss.
        var hedgePoly   = new Dictionary<string, decimal>();
        var hedgeKalshi = new Dictionary<string, decimal>();
        foreach (string line in await File.ReadAllLinesAsync(_journalPath))
        {
            if (string.IsNullOrWhiteSpace(line)) continue;
            try
            {
                using var doc = JsonDocument.Parse(line);
                var root = doc.RootElement;
                if (!root.TryGetProperty("event", out var evEl)) continue;
                string ev = evEl.GetString() ?? "";

                if (ev == "EXECUTION_COMPLETE")
                {
                    if (!root.TryGetProperty("pairId", out var pidEl)) continue;
                    string pairId = pidEl.GetString() ?? "";
                    if (!pairByPairId.ContainsKey(pairId)) continue;
                    string arbType = root.TryGetProperty("arbType", out var atEl) ? atEl.GetString() ?? "" : "";
                    decimal kEntry = 0m, pEntry = 0m, journalKQty = 0m;
                    if (root.TryGetProperty("fills", out var fills))
                    {
                        if (fills.TryGetProperty("kalshi", out var kf) &&
                            kf.TryGetProperty("fillPrice", out var kfp))
                            kEntry = kfp.GetDecimal();
                        if (fills.TryGetProperty("poly", out var pf) &&
                            pf.TryGetProperty("avgFillPrice", out var pfp) &&
                            pfp.ValueKind == JsonValueKind.Number)
                            pEntry = pfp.GetDecimal();
                    }
                    // Net held qty, NOT the gross entry fill. position.kHeld already subtracts any
                    // REVERSED_KALSHI cleanup; reading fills.kalshi.filled is why ABAN restored as 12
                    // instead of the real 5. position is null on MISS / zero-balanced execs → leave 0.
                    if (root.TryGetProperty("position", out var posEl) &&
                        posEl.ValueKind == JsonValueKind.Object &&
                        posEl.TryGetProperty("kHeld", out var khEl) &&
                        khEl.ValueKind == JsonValueKind.Number)
                        journalKQty = khEl.GetDecimal();
                    entryMap[pairId] = (arbType, kEntry, pEntry, journalKQty);
                }
                else if (ev == "CLEANUP_HEDGE_COMPLETED")
                {
                    if (!root.TryGetProperty("pair", out var pidEl)) continue;
                    string pairId = pidEl.GetString() ?? "";
                    string leg = root.TryGetProperty("leg", out var legEl) ? legEl.GetString() ?? "" : "";
                    if (leg == "poly" &&
                        root.TryGetProperty("polyFillPrice", out var pfp2) && pfp2.ValueKind == JsonValueKind.Number)
                        hedgePoly[pairId] = pfp2.GetDecimal();
                    else if (leg == "kalshi" &&
                        root.TryGetProperty("kFillPrice", out var kfp2) && kfp2.ValueKind == JsonValueKind.Number)
                        hedgeKalshi[pairId] = kfp2.GetDecimal();
                }
                else if (ev == "SETTLEMENT_DETECTED" || ev == "EARLY_EXIT_COMPLETE")
                {
                    string pairId = root.TryGetProperty("pairId", out var pidEl2) ? pidEl2.GetString() ?? "" : "";
                    entryMap.Remove(pairId);
                }
            }
            catch { /* malformed line — skip */ }
        }

        // Apply buffered hedge fills now that every EXECUTION_COMPLETE has seeded the map.
        // Only fills the gap when the entry leg's price is still 0 (recovery completed it).
        foreach (var pid in entryMap.Keys.ToList())
        {
            var e = entryMap[pid];
            if (e.PEntry == 0m && hedgePoly.TryGetValue(pid, out var pp))
                entryMap[pid] = (e.ArbType, e.KEntry, pp, e.JournalKQty);
            if (entryMap[pid].KEntry == 0m && hedgeKalshi.TryGetValue(pid, out var kk))
                entryMap[pid] = (entryMap[pid].ArbType, kk, entryMap[pid].PEntry, entryMap[pid].JournalKQty);
        }

        if (entryMap.Count == 0) return;

        // Fetch venue positions with retries. A single empty/failed read must not be trusted —
        // the journal says we hold positions, and trading with an empty book re-enters them.
        List<(string Ticker, int Position)> kalshiPos = new();
        bool fetchOk = false;
        for (int attempt = 1; attempt <= 3; attempt++)
        {
            try { kalshiPos = await _kalshi.GetPositionsAsync(); fetchOk = true; }
            catch (Exception ex) { Console.WriteLine($"[RESTORE] Kalshi fetch attempt {attempt} failed: {ex.Message}"); }
            if (fetchOk && kalshiPos.Count > 0) break;
            if (attempt < 3) await Task.Delay(2000);
        }
        var kalshiByTicker = kalshiPos.ToDictionary(p => p.Ticker, p => p.Position);

        // Contradiction guard: journal expects open positions but the venue returned none after
        // retries. Bad read, not a flat account. Refuse to trade blind — an empty _openPositions
        // disables single-entry and the bot re-enters positions it still holds (the seg9 halt).
        if (kalshiByTicker.Count == 0)
        {
            Console.ForegroundColor = ConsoleColor.Red;
            Console.WriteLine($"[RESTORE] Positions API empty after retries but journal expects {entryMap.Count} open — " +
                              "refusing to trade blind. HALTING; verify Kalshi connectivity and restart.");
            Console.ResetColor();
            await JournalAsync(JsonSerializer.Serialize(new {
                t = DateTime.UtcNow, @event = "RESTORE_FAILED_HALT",
                reason = "POSITIONS_EMPTY_BUT_JOURNAL_NONEMPTY", expectedOpen = entryMap.Count
            }));
            DiscordAlert($"🚨 HARD HALT at startup — Kalshi positions API returned empty but the journal expects {entryMap.Count} open position(s). Refusing to trade blind; verify connectivity and restart.");
            _halted = true;
            return;
        }

        int restored = 0;
        foreach (var (pairId, entry) in entryMap)
        {
            if (!pairByPairId.TryGetValue(pairId, out var pair)) continue;

            // Venue-only: a venue-confirmed 0 means the Kalshi leg is closed, not API lag.
            // The journal fallback is intentionally removed — trusting it manufactured phantom positions (JOR incident).
            decimal kQty = kalshiByTicker.TryGetValue(pair.KalshiTicker, out int kPos) ? Math.Abs(kPos) : 0m;

            string polyToken = entry.ArbType == "K_NO_P_YES" ? pair.PolyYesTokenId : pair.PolyNoTokenId;
            decimal pQty = 0m;
            try   { pQty = await _poly.GetTokenBalanceAsync(polyToken); }
            catch (Exception ex)
            {
                Console.WriteLine($"[RESTORE] {pair.Label} | Poly balance failed: {ex.Message}");
                continue;
            }

            if (pQty < MinRestorePolyShares)
            {
                Console.ForegroundColor = ConsoleColor.Yellow;
                Console.WriteLine($"[RESTORE] {pair.Label} | Poly balance {pQty:0.000} below dust floor — treating as closed, skipping");
                Console.ResetColor();
                continue;
            }

            // Orphan guard: Kalshi leg gone but Poly still held — legs decoupled (settlement/void closed
            // only the Kalshi side). Do not restore a phantom position.
            if (kQty == 0m)
            {
                Console.ForegroundColor = ConsoleColor.Yellow;
                Console.WriteLine($"[RESTORE] {pair.Label} | ORPHANED Poly {pQty:0.00} with no Kalshi — needs manual review / reverse, not restore");
                Console.ResetColor();
                await JournalAsync(JsonSerializer.Serialize(new {
                    t = DateTime.UtcNow, @event = "RESTORE_ORPHANED_POLY",
                    pairId, label = pair.Label, pShares = pQty, polyToken
                }));
                _orphanedPairs[pairId] = 0;   // block re-entry — the stranded Poly would collide with a fresh fill
                continue;
            }

            decimal matched = Math.Min(kQty, pQty);
            decimal orphaned = Math.Abs(kQty - pQty);
            if (orphaned > 0.5m)
            {
                Console.ForegroundColor = ConsoleColor.Yellow;
                Console.WriteLine($"[RESTORE] {pair.Label} | leg imbalance k={kQty} p={pQty:0.00} — restoring matched={matched:0.00}, orphaned={orphaned:0.00}");
                Console.ResetColor();
            }

            string restoreId = $"RESTORED_{DateTime.UtcNow:yyyyMMddHHmmss}";
            _openPositions[pairId] = new ArbPosition(
                pairId, entry.ArbType, matched, matched, entry.KEntry, entry.PEntry, DateTime.UtcNow, restoreId);

            Console.ForegroundColor = ConsoleColor.Cyan;
            Console.WriteLine(
                $"[RESTORE] {pair.Label} | {entry.ArbType} | K={kQty} P={pQty:0.00} matched={matched:0.00} " +
                $"entry={entry.KEntry:0.0000}+{entry.PEntry:0.0000} — added to monitoring");
            Console.ResetColor();

            await JournalAsync(JsonSerializer.Serialize(new {
                t = DateTime.UtcNow, @event = "STARTUP_POSITION_RESTORED",
                execId = restoreId, pairId, label = pair.Label,
                arbType = entry.ArbType, kContracts = matched, pShares = matched,
                kVenue = kQty, pVenue = pQty,
                kEntry = entry.KEntry, pEntry = entry.PEntry
            }));
            restored++;
        }

        if (restored > 0)
            Console.WriteLine($"[RESTORE] {restored} position(s) registered for early-exit and settlement monitoring.");
    }

    // ── Post-trade reconciliation ─────────────────────────────────────────────

    /// <summary>Pre-trade Poly balance snapshot. Retries once; returns null (inconclusive) rather
    /// than a fabricated 0 if the read fails, so reconcile won't false-halt against a fake prior.</summary>
    private async Task<decimal?> SnapshotPolyBalanceAsync(string polyToken)
    {
        for (int attempt = 1; attempt <= 2; attempt++)
        {
            try { return await _poly.GetTokenBalanceAsync(polyToken); }
            catch (Exception ex)
            {
                DebugLog.Trades($"SnapshotPolyBalanceAsync attempt {attempt}/2 failed: {ex.Message}");
                if (attempt < 2) await Task.Delay(500);
            }
        }
        return null;   // inconclusive — caller must NOT treat as a real zero balance
    }

    private async Task ReconcileTradeAsync(CrossPair pair, string arbType, decimal expectedKalshi, decimal expectedPoly, string execId = "", string kOrderId = "", decimal? priorPolyBalance = null, decimal reversedKalshiQty = 0m, bool polyOverfillReversed = false)
    {
        try
        {
            // Kalshi /portfolio/positions lags 10–20 s after a fill. Instead, poll the specific
            // order directly (GET /portfolio/orders/{id}) which reflects the fill immediately.
            // Fall back to GetPositionsAsync only when kOrderId is unavailable.
            const int maxAttempts  = 10;    // 10 × 3s = 30s — comfortably past Kalshi's 10–20s positions lag
            const int retryDelayMs = 3_000;
            decimal kActual = 0m;
            for (int attempt = 1; attempt <= maxAttempts; attempt++)
            {
                await Task.Delay(retryDelayMs);
                if (!string.IsNullOrEmpty(kOrderId))
                {
                    var (pollStatus, pollFill) = await _kalshi.PollOrderAsync(kOrderId);
                    // Entry order shows the full buy fill; held position = buyFill - cleanly-reversed excess.
                    // reversedKalshiQty is 0 for clean fills, so this is a no-op there.
                    kActual = pollFill - reversedKalshiQty;
                    if (pollStatus is "executed" or "canceled") break;  // terminal: fill count is final
                    DebugLog.Trades($"ReconcileTradeAsync {pair.Label}: attempt {attempt}/{maxAttempts} order={kOrderId} status={pollStatus} fill={pollFill} reversed={reversedKalshiQty} — retrying");
                }
                else
                {
                    var kalshiPositions = await _kalshi.GetPositionsAsync();
                    int kPos = kalshiPositions.FirstOrDefault(p => p.Ticker == pair.KalshiTicker).Position;
                    kActual = Math.Abs(kPos);
                    if (Math.Abs(kActual - expectedKalshi) <= 1.0m) break;
                    DebugLog.Trades($"ReconcileTradeAsync {pair.Label}: attempt {attempt}/{maxAttempts} kVenue={kActual} (expected {expectedKalshi}) — retrying");
                }
            }
            string polyTokenId = arbType == "K_YES_P_NO" ? pair.PolyNoTokenId : pair.PolyYesTokenId;
            // Poly balance API has the same propagation lag as Kalshi's positions endpoint (often minutes).
            // priorValid=false means the pre-trade snapshot failed -> no baseline -> Measured() falls back
            // to the absolute read. Either way, a Poly read BELOW the confirmed fill is treated as lag and
            // never halts (see below); only an over-read or a Kalshi order-poll mismatch can halt.
            bool    priorValid = priorPolyBalance.HasValue;
            decimal prior      = priorPolyBalance ?? 0m;
            decimal polyBal    = prior;
            // Measured Poly position attributable to THIS trade:
            //   * prior-valid, normal       -> delta vs pre-trade snapshot (so orphaned prior-run shares don't count)
            //   * prior-valid, overfill-rev  -> absolute (an in-trade overfill->reversal races & poisons the snapshot/delta)
            //   * no prior snapshot          -> absolute (no trusted baseline to delta against)
            // Retry until it reaches at least half of expected, to let Poly's data-API indexing lag clear.
            decimal Measured() => (priorValid && !polyOverfillReversed) ? polyBal - prior : polyBal;
            for (int pAttempt = 1; pAttempt <= maxAttempts; pAttempt++)
            {
                polyBal = await _poly.GetTokenBalanceAsync(polyTokenId);
                if (Measured() >= expectedPoly * 0.5m) break;
                if (pAttempt < maxAttempts) await Task.Delay(retryDelayMs);
                DebugLog.Trades($"ReconcileTradeAsync {pair.Label}: Poly attempt {pAttempt}/{maxAttempts} measured={Measured():0.00} polyBal={polyBal:0.00} (expected ~{expectedPoly:0.00}, priorValid={priorValid}) - retrying");
            }
            decimal polyDelta    = polyBal - prior;
            decimal polyMeasured = Measured();
            bool    kMismatch    = Math.Abs(kActual - expectedKalshi) > 0.5m;
            // Poly direction matters. An UNDER-read (measured < expected) right after a CONFIRMED
            // execution-time fill is almost always Polymarket data-API indexing lag -- which can run
            // minutes, well past our ~30s retry window -- NOT a real naked leg. The fill already
            // confirmed the position, so never halt on it: journal inconclusive and keep trading.
            // Only an OVER-read (measured > expected) means real un-reversed excess exposure -> halt.
            // (priorValid uses the delta so orphaned prior-run shares can't masquerade as an over-read.)
            bool    pUnder = polyMeasured < expectedPoly - 0.5m;
            bool    pOver  = polyMeasured > expectedPoly + 0.5m;
            if (pUnder)
            {
                Console.ForegroundColor = ConsoleColor.Yellow;
                Console.WriteLine(
                    $"[RECONCILE INCONCLUSIVE] {pair.Label} | Poly venue reads {polyMeasured:0.00} < expected {expectedPoly:0.00} " +
                    $"after a confirmed fill -- suspected data-API lag, NOT halting (position trusted from fill).");
                Console.ResetColor();
                await JournalAsync(JsonSerializer.Serialize(new {
                    t = DateTime.UtcNow, @event = "RECONCILE_INCONCLUSIVE", execId,
                    pair = pair.PairId, arbType,
                    kExpected = expectedKalshi, kVenue = kActual,
                    pExpected = expectedPoly, pVenue = polyBal, pMeasured = polyMeasured,
                    pPriorValid = priorValid, polyOverfillReversed,
                    reason = "POLY_UNDER_READ_SUSPECTED_LAG", halted = false
                }));
            }
            if (kMismatch || pOver)
            {
                _halted = true;
                string cause = kMismatch && pOver ? "Kalshi mismatch + Poly over-read"
                             : kMismatch          ? "Kalshi mismatch"
                                                  : "Poly over-read";
                DiscordAlert($"🚨 HARD HALT — reconcile mismatch on {pair.Label} ({cause}): K local={expectedKalshi} venue={kActual} | P local={expectedPoly:0.00} venue={polyBal:0.00}. Manual position check + reset required.");
                Console.ForegroundColor = ConsoleColor.Red;
                Console.WriteLine(
                    $"[RECONCILE ALERT] {pair.Label} | {cause} | " +
                    $"K: local={expectedKalshi} venue={kActual} | " +
                    $"P: local={expectedPoly:0.00} venue={polyBal:0.00} measured={polyMeasured:0.00}");
                Console.WriteLine("[RECONCILE ALERT] Bot halted -- manual reset required. Verify positions before resuming.");
                Console.ResetColor();
                await JournalAsync(JsonSerializer.Serialize(new {
                    t = DateTime.UtcNow, @event = "RECONCILE_MISMATCH", execId,
                    pair = pair.PairId, arbType,
                    kExpected = expectedKalshi, kVenue = kActual,
                    pExpected = expectedPoly,   pVenue = polyBal,
                    pPrior = priorValid ? (object?)prior : null, pPriorValid = priorValid, pDelta = polyDelta,
                    pMeasured = polyMeasured, polyOverfillReversed,
                    kHalt = kMismatch, pOverHalt = pOver, halted = true
                }));
            }
            else if (!pUnder)
                DebugLog.Trades($"ReconcileTradeAsync {pair.Label}: confirmed K={kActual} P_measured={polyMeasured:0.00} (polyBal={polyBal:0.00} prior={prior:0.00} priorValid={priorValid})");
        }
        catch (Exception ex)
        {
            Console.ForegroundColor = ConsoleColor.Red;
            Console.WriteLine($"[RECONCILE ERROR] {pair.Label}: reconciliation threw {ex.GetType().Name}: {ex.Message} — halting bot");
            Console.ResetColor();
            DiscordAlert($"🚨 HARD HALT — reconciliation threw {ex.GetType().Name} on {pair.Label}: {ex.Message}. Manual investigation + reset required.");
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
            DiscordAlert($"⚠️ CONNECTION HALT — {venue}: {consec} consecutive REST failures, new trades paused (auto-recovers on reconnect).");
            _connectionHalted = true;
            await JournalAsync(JsonSerializer.Serialize(new {
                t = DateTime.UtcNow, @event = "VENUE_MAINTENANCE",
                venue, consecutiveErrors = consec
            }));
        }
    }

    // ── Connection watchdog controls ──────────────────────────────────────────

    // Watchdog calls this every loop while disconnected — guard on the flag so the alert fires once per
    // disconnect episode (and de-dupes against the VENUE_MAINTENANCE path, which shares the flag).
    public void HaltForConnectionLoss()
    {
        if (_connectionHalted) return;
        _connectionHalted = true;
        DiscordAlert("⚠️ CONNECTION HALT — venue disconnect detected by the watchdog; new trades paused until reconnect.");
    }
    public void ResumeFromConnectionLoss() => _connectionHalted = false;

    /// <summary>
    /// Test harness (dry-run only): injects a +1 position mismatch on the NEXT trade that fills,
    /// regardless of which pair it is. The mismatch fires after the fills are confirmed and forces
    /// ReconcileTradeAsync to run, letting you verify the halt-on-mismatch path deterministically.
    /// </summary>
    public void QueueMismatchOnNextTrade()
    {
        _injectMismatchOnNextTrade = true;
        Console.ForegroundColor = ConsoleColor.Magenta;
        Console.WriteLine("[MISMATCH INJECT] Flag queued — fires on the next trade that fills, any pair");
        Console.ResetColor();
    }

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

            // Settlement detection: positions where kBid is zero may have resolved on Kalshi.
            // Fetch GetPositionsAsync() lazily — at most one call per 60 s tick.
            if (!_dryRun)
            {
                List<(string Ticker, int Position)>? kalshiPositions = null;
                foreach (var (pairId, pos) in _openPositions.ToArray())
                {
                    var pair = _telemetry.GetPair(pairId);
                    if (pair == null) continue;
                    string kBidKey = pos.ArbType == "K_YES_P_NO"
                        ? $"K:{pair.KalshiTicker}" : $"K:{pair.KalshiTicker}_NO";
                    if (!_books.TryGetValue(kBidKey, out var kBook) || kBook.GetBestBidPrice() > 0m) continue;

                    try { kalshiPositions ??= await _kalshi.GetPositionsAsync(); }
                    catch (Exception ex)
                    {
                        DebugLog.Trades($"Settlement check GetPositionsAsync failed: {ex.Message}");
                        break;
                    }

                    // An empty positions list is far more likely a transient API/pagination failure
                    // than every held position resolving at once. Never settle on it.
                    if (kalshiPositions.Count == 0)
                    {
                        DebugLog.Trades("Settlement check: positions API returned empty — skipping pass this tick");
                        break;
                    }

                    bool stillHeld = kalshiPositions.Any(p =>
                        p.Ticker.Equals(pair.KalshiTicker, StringComparison.OrdinalIgnoreCase) &&
                        Math.Abs(p.Position) > 0);
                    if (stillHeld) { _settlementAbsentTicks[pairId] = 0; continue; }

                    // Debounce: require N consecutive absent ticks so one partial read can't settle a live position.
                    int absent = _settlementAbsentTicks.AddOrUpdate(pairId, 1, (_, n) => n + 1);
                    if (absent < SettlementConfirmTicks)
                    {
                        DebugLog.Trades($"Settlement check {pair.Label}: absent {absent}/{SettlementConfirmTicks} — deferring");
                        continue;
                    }
                    _settlementAbsentTicks.TryRemove(pairId, out _);

                    _openPositions.TryRemove(pairId, out _);
                    DebugLog.Trades($"POSITION_REMOVED {pairId} reason=SETTLEMENT");
                    decimal settledCost = pos.KalshiContracts * pos.KalshiEntryPrice
                                        + pos.PolyShares      * pos.PolyEntryPrice;
                    lock (_exposureLock) { _totalExposure = Math.Max(0m, _totalExposure - settledCost); }
                    Console.ForegroundColor = ConsoleColor.Cyan;
                    Console.WriteLine($"[SETTLEMENT] {pair.Label} | position settled — removed from tracking");
                    Console.ResetColor();
                    await JournalAsync(JsonSerializer.Serialize(new {
                        t            = DateTime.UtcNow, @event = "SETTLEMENT_DETECTED",
                        execId       = pos.ExecId, pairId, label = pair.Label,
                        kContracts   = pos.KalshiContracts, kEntryPrice = pos.KalshiEntryPrice,
                        pShares      = pos.PolyShares,      pEntryPrice = pos.PolyEntryPrice,
                        entryCostUsd = Math.Round(settledCost, 4),
                        heldSince    = pos.EntryTime
                    }));
                    await RefreshBalancesAsync();
                }
            }

            foreach (var (pairId, pos) in _openPositions.ToArray())
            {
                if (_cts.Token.IsCancellationRequested) break;
                try { await CheckEarlyExitAsync(pairId, pos); }
                catch (Exception ex)
                {
                    DebugLog.Trades($"RunEarlyExitMonitorAsync {pairId}: {ex.GetType().Name}: {ex.Message}");
                }
            }

            // The "longer retry period": re-attempt any naked leg parked by a partial early exit until
            // it flattens (e.g. once the venue reopens). Never halts — just keeps trying every 60s.
            foreach (var (pairId, pr) in _pendingReversals.ToArray())
            {
                if (_cts.Token.IsCancellationRequested) break;
                try { await RetryPendingReversalAsync(pr); }
                catch (Exception ex)
                {
                    DebugLog.Trades($"RetryPendingReversal {pairId}: {ex.GetType().Name}: {ex.Message}");
                }
            }
        }
    }

    // Retry a naked leg parked by a partial early exit. Flattens via the relentless reverse helpers;
    // clears on success, else stays queued for the next 60s tick. After PendingReversalMaxRetries failed
    // passes it gives up and lets the leg ride to settlement (a decisive winner leaves no makers to sell
    // to — retrying is futile). Never halts.
    private async Task RetryPendingReversalAsync(PendingReversal pr)
    {
        string  pairId       = pr.Pair.PairId;
        decimal soldOrFilled = 0m;
        decimal remaining    = pr.Qty;

        if (pr.Leg == "kalshi")
        {
            int want = (int)Math.Ceiling(pr.Qty);
            if (want <= 0) { _pendingReversals.TryRemove(pairId, out _); _orphanedPairs.TryRemove(pairId, out _); return; }
            var (filled, _) = await ReverseKalshiLegAsync(
                pr.Pair, pr.KalshiSide, want, pr.EntryPrice, pr.KBookKey, pr.ExecId, null);
            soldOrFilled = filled;
            remaining    = pr.Qty - filled;
        }
        else // poly
        {
            var (sold, price) = await PlacePolySellAsync(pr.PolyToken, pr.Qty, pr.NegRisk);
            if (sold > 0m)
            {
                decimal loss = Math.Max(0m, sold * (pr.EntryPrice - price));
                if (loss > 0m) lock (_cleanupLock) { _totalCleanupCostUsd += loss; }
            }
            soldOrFilled = sold;
            remaining    = pr.Qty - sold;
        }

        if (remaining < 1.0m)   // fully flattened (sub-1 leftover is untradeable dust) → resolved
        {
            _pendingReversals.TryRemove(pairId, out _);
            _orphanedPairs.TryRemove(pairId, out _);   // leg flat again → pair is re-tradeable
            await JournalAsync(JsonSerializer.Serialize(new {
                t = DateTime.UtcNow, @event = "PENDING_REVERSAL_RESOLVED",
                pair = pairId, leg = pr.Leg, flattened = Math.Round(soldOrFilled, 6)
            }));
            Console.ForegroundColor = ConsoleColor.Green;
            Console.WriteLine($"[PENDING REVERSAL OK] {pr.Pair.Label} | flattened naked {pr.Leg} — cleared.");
            Console.ResetColor();
            return;
        }

        // Didn't flatten this pass — bump the counter; give up to settlement past the cap.
        int attempts = pr.Attempts + 1;
        if (attempts >= PendingReversalMaxRetries)
        {
            // No makers (decisive winner / market closed for settlement). Stop retrying and let the leg
            // ride to settlement; keep the pair orphaned so we don't re-enter while it's still held.
            _pendingReversals.TryRemove(pairId, out _);
            DiscordAlert($"⚠️ Abandoned to settlement: {pairId} — {pr.Leg} naked leg couldn't be flattened after {attempts} tries; left to ride to settlement, pair stays orphaned.");
            await JournalAsync(JsonSerializer.Serialize(new {
                t = DateTime.UtcNow, @event = "PENDING_REVERSAL_ABANDONED",
                pair = pairId, leg = pr.Leg, attempts, unresolvedQty = Math.Round(remaining, 6)
            }));
            Console.ForegroundColor = ConsoleColor.Magenta;
            Console.WriteLine($"[PENDING REVERSAL ABANDONED] {pr.Pair.Label} | naked {pr.Leg} {remaining:0.##} unsellable after {attempts} tries — letting it ride to settlement.");
            Console.ResetColor();
            return;
        }
        _pendingReversals[pairId] = pr with { Qty = remaining, Attempts = attempts };
    }

    private async Task CheckEarlyExitAsync(string pairId, ArbPosition pos)
    {
        bool breakEvenMode = _minBuy;
        if (!breakEvenMode && EarlyExitThreshold <= 0m) return;

        var pair = _telemetry.GetPair(pairId);
        if (pair == null) return;

        string kBidKey    = pos.ArbType == "K_YES_P_NO" ? $"K:{pair.KalshiTicker}" : $"K:{pair.KalshiTicker}_NO";
        string pBidKey    = pos.ArbType == "K_YES_P_NO" ? $"P:{pair.PolyNoTokenId}" : $"P:{pair.PolyYesTokenId}";
        string kalshiSide = pos.ArbType == "K_YES_P_NO" ? "yes" : "no";
        string polyToken  = pos.ArbType == "K_YES_P_NO" ? pair.PolyNoTokenId : pair.PolyYesTokenId;

        decimal kBid = 0m, pBid = 0m;
        // Unconditional lookups (not inside the &&) so kBook/pBook are definitely assigned for the
        // executable-bid depth walk below, even when wsLive is false.
        _books.TryGetValue(kBidKey, out var kBook);
        _books.TryGetValue(pBidKey, out var pBook);
        bool wsLive = kBook != null && pBook != null
                   && (kBid = kBook.GetBestBidPrice()) > 0m
                   && (pBid = pBook.GetBestBidPrice()) > 0m;

        if (!wsLive)
        {
            if (_restVerifier == null) return;
            (kBid, pBid) = await _restVerifier.GetCurrentBidsAsync(pair, pos.ArbType);
            if (kBid <= 0m || pBid <= 0m) return;
            DebugLog.Trades($"CheckEarlyExitAsync {pair.Label}: WS books stale — REST bids K={kBid:0.0000} P={pBid:0.0000}");
        }

        // Exit-bleed fix: price off the size-weighted executable bid, not top-of-book, so PnL
        // reflects what we'd actually get selling the FULL size (mirror of the entry depth walk).
        // If the bid side can't absorb the whole size, hold — selling would walk down the ladder
        // and flip a break-even exit negative. Only possible with live WS depth; the REST fallback
        // has no ladder, so the ExitCushionPerSet below is what guards that path.
        if (wsLive)
        {
            var (kVwap, kFill) = kBook!.GetExecutableBidVwap(pos.KalshiContracts);
            var (pVwap, pFill) = pBook!.GetExecutableBidVwap(pos.PolyShares);
            if (kFill < pos.KalshiContracts - 0.5m || pFill < pos.PolyShares - 0.5m)
            {
                DebugLog.Trades($"CheckEarlyExitAsync {pair.Label}: thin exit depth — " +
                    $"kFill={kFill:0.##}/{pos.KalshiContracts:0.##} pFill={pFill:0.##}/{pos.PolyShares:0.##} — holding");
                return;
            }
            if (kVwap > 0m) kBid = kVwap;
            if (pVwap > 0m) pBid = pVwap;
        }

        decimal entryCostPerSet      = pos.KalshiEntryPrice + pos.PolyEntryPrice;
        decimal expectedProfitPerSet = 1.0m - entryCostPerSet
            - KalshiFee(pos.KalshiEntryPrice) - PolyFee(pos.PolyEntryPrice, polyToken);
        if (expectedProfitPerSet <= 0m) return;

        decimal exitFeesPerSet      = KalshiFee(kBid) + PolyFee(pBid, polyToken);
        decimal entryFeesPerSet     = KalshiFee(pos.KalshiEntryPrice) + PolyFee(pos.PolyEntryPrice, polyToken);
        decimal unrealizedPnlPerSet = (kBid + pBid) - exitFeesPerSet - entryCostPerSet - entryFeesPerSet;
        decimal unrealizedPnlTotal  = unrealizedPnlPerSet * pos.KalshiContracts;

        DebugLog.Trades(
            $"EarlyExit {pair.Label}: kBid={kBid:0.0000} pBid={pBid:0.0000} " +
            $"unrealizedPnl/set={unrealizedPnlPerSet:0.0000} " +
            $"hurdle={EarlyExitThreshold * expectedProfitPerSet:0.0000} " +
            $"total=${unrealizedPnlTotal:0.00}");

        if (breakEvenMode)
        {
            if (unrealizedPnlPerSet < ExitCushionPerSet) return;
        }
        else
        {
            if (unrealizedPnlPerSet < EarlyExitThreshold * expectedProfitPerSet) return;
            if (unrealizedPnlTotal  < EarlyExitMinProfitUsd) return;
        }

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
                $"[EARLY EXIT] {pair.Label} | unrealizedPnl=${unrealizedPnlTotal:0.00} " +
                (breakEvenMode
                    ? "≥ 0 (break-even) "
                    : $"≥ {EarlyExitThreshold:P0} × expected=${(expectedProfitPerSet * currentPos.KalshiContracts):0.00} ") +
                $"— closing early (kBid={kBid:0.0000} pBid={pBid:0.0000})");
            Console.ResetColor();

            await JournalAsync(JsonSerializer.Serialize(new {
                t = DateTime.UtcNow, @event = "EARLY_EXIT_INTENT", execId = currentPos.ExecId,
                pairId, label = pair.Label, arbType = pos.ArbType,
                kalshiContracts = currentPos.KalshiContracts, polyShares = currentPos.PolyShares,
                kBid, pBid, unrealizedPnlPerSet, unrealizedPnlTotal,
                expectedProfitTotal = expectedProfitPerSet * currentPos.KalshiContracts,
                threshold = breakEvenMode ? (decimal?)null : EarlyExitThreshold, breakEven = breakEvenMode, dryRun = _dryRun
            }));

            if (_dryRun)
            {
                _openPositions.TryRemove(pairId, out _);
                DebugLog.Trades($"POSITION_REMOVED {pairId} reason=EARLY_EXIT_DRY_RUN");
                Interlocked.Increment(ref _earlyExitsCompleted);
                decimal dryCost = currentPos.KalshiContracts * currentPos.KalshiEntryPrice
                                + currentPos.PolyShares      * currentPos.PolyEntryPrice;
                lock (_exposureLock) { _totalExposure = Math.Max(0m, _totalExposure - dryCost); }
                lock (_balanceLock)  { _kalshiBalanceUsd += currentPos.KalshiContracts * kBid;
                                       _polyBalanceUsd   += currentPos.PolyShares      * pBid; }
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

            bool kOk = kSold >= currentPos.KalshiContracts - 0.5m;
            bool pOk = pSold >= currentPos.PolyShares      - 0.5m;

            // Poly is out but Kalshi partially filled: sweep 3 ticks deeper to close the unhedged leg.
            // Halting here would leave unhedged Kalshi contracts indefinitely; sweeping costs at most 3¢/contract.
            if (!kOk && pOk)
            {
                decimal kRemaining = currentPos.KalshiContracts - kSold;
                for (int sweep = 1; sweep <= 3 && kRemaining >= 0.5m; sweep++)
                {
                    int kRetryPrice = Math.Max(1, kSellCents - sweep);
                    Console.WriteLine($"[EARLY EXIT RETRY] {pair.Label} | K partial {kSold}/{currentPos.KalshiContracts} — sweeping {kRemaining:0} contracts @{kRetryPrice}¢ (pass {sweep}/3)");
                    try
                    {
                        var (_, _, kFilled2) = await _kalshi.PlaceOrderAsync(
                            pair.KalshiTicker, kalshiSide, kRetryPrice, (int)Math.Ceiling(kRemaining), action: "sell");
                        kSold += kFilled2;
                        kRemaining = currentPos.KalshiContracts - kSold;
                    }
                    catch (Exception ex)
                    {
                        Console.WriteLine($"[EARLY EXIT RETRY ERROR] {pair.Label} pass {sweep}: {ApiErrorHelper.ClassifyKalshi(ex)}");
                        break;
                    }
                }
                kOk = kSold >= currentPos.KalshiContracts - 0.5m;
            }

            if (kOk && pOk)
            {
                _openPositions.TryRemove(pairId, out _);
                DebugLog.Trades($"POSITION_REMOVED {pairId} reason=EARLY_EXIT_LIVE");
                // Block re-entry after early exit — the stale book that triggered the exit
                // may still show an apparent arb on the same pair within seconds.
                _cooldownUntil[pairId] = DateTimeOffset.UtcNow.ToUnixTimeSeconds() + _pairCooldownSeconds;
                Interlocked.Increment(ref _earlyExitsCompleted);
                decimal exitEntryCost = currentPos.KalshiContracts * currentPos.KalshiEntryPrice
                                      + currentPos.PolyShares      * currentPos.PolyEntryPrice;
                lock (_exposureLock) { _totalExposure = Math.Max(0m, _totalExposure - exitEntryCost); }
                decimal kProceeds   = kSold * (kSellCents / 100m);
                decimal pProceeds   = pSold * pAvgPrice;
                decimal kExitFee    = KalshiFee(kSellCents / 100m) * kSold;
                decimal pExitFee    = PolyFee(pAvgPrice, polyToken) * pSold;
                decimal kEntryFee   = KalshiFee(currentPos.KalshiEntryPrice) * currentPos.KalshiContracts;
                decimal pEntryFee   = PolyFee(currentPos.PolyEntryPrice, polyToken) * currentPos.PolyShares;
                decimal realizedPnl = (kProceeds + pProceeds)
                    - (kExitFee + pExitFee)
                    - (currentPos.KalshiContracts * currentPos.KalshiEntryPrice
                    +  currentPos.PolyShares      * currentPos.PolyEntryPrice)
                    - (kEntryFee + pEntryFee);
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
                await RefreshBalancesAsync();
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
                // One leg sold, the other did not → a naked, unhedged leg. Ops policy: do NOT halt.
                // Orphan the pair (block re-entry), drop the now-invalid arb position + release its
                // reservation, and queue the naked leg for the 60s monitor to keep flattening.
                string nakedLeg; decimal nakedQty; decimal nakedEntry;
                if (!pOk) { nakedLeg = "poly";   nakedQty = currentPos.PolyShares      - pSold; nakedEntry = currentPos.PolyEntryPrice;  }
                else      { nakedLeg = "kalshi"; nakedQty = currentPos.KalshiContracts - kSold; nakedEntry = currentPos.KalshiEntryPrice; }

                _openPositions.TryRemove(pairId, out _);
                decimal partialEntryCost = currentPos.KalshiContracts * currentPos.KalshiEntryPrice
                                         + currentPos.PolyShares      * currentPos.PolyEntryPrice;
                lock (_exposureLock) { _totalExposure = Math.Max(0m, _totalExposure - partialEntryCost); }
                _orphanedPairs[pairId] = 0;

                string kBookKey = pos.ArbType == "K_YES_P_NO" ? $"K:{pair.KalshiTicker}" : $"K:{pair.KalshiTicker}_NO";
                var pr = new PendingReversal(pair, nakedLeg, kalshiSide, kBookKey, polyToken,
                                             pair.IsNegRisk, nakedQty, nakedEntry, currentPos.ExecId);
                _pendingReversals[pairId] = pr;

                DiscordAlert($"⚠️ Early-exit naked leg: {pair.Label} — {nakedLeg} {nakedQty:0.##} orphaned + queued for 60s-monitor retry; bot keeps running.");
                Console.ForegroundColor = ConsoleColor.Magenta;
                Console.WriteLine($"[EARLY EXIT PARTIAL] {pair.Label} | kOk={kOk} pOk={pOk} — naked {nakedLeg} {nakedQty:0.##} orphaned + queued for retry (no halt).");
                Console.ResetColor();
                await JournalAsync(JsonSerializer.Serialize(new {
                    t = DateTime.UtcNow, @event = "EARLY_EXIT_PARTIAL", execId = currentPos.ExecId,
                    pairId, kOk, pOk, kSold, kSellCents, pSold, pAvgPrice,
                    nakedLeg, nakedQty = Math.Round(nakedQty, 6), halted = false, queuedForRetry = true
                }));

                // Immediate first attempt — flatten now if the venue is open; else the monitor retries every 60s.
                await RetryPendingReversalAsync(pr);
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
