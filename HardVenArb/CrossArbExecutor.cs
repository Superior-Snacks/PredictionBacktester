using System.Collections.Concurrent;
using System.Text;
using System.Text.Json;
using System.Text.RegularExpressions;
using System.Threading.Channels;
using PredictionBacktester.Engine;
using PredictionBacktester.Engine.LiveExecution;

namespace HardVenArb;

public record ReconciliationEntry(
    string PairId, string Label, string Status,
    decimal KalshiQty, decimal HardVenQty, string Notes);

public record ArbPosition(
    string   PairId,
    string   ArbType,
    decimal  KalshiContracts,
    decimal  HardVenShares,
    decimal  KalshiEntryPrice,
    decimal  HardVenEntryPrice,
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
    string  Outcome,        // HEDGE_COMPLETED | REVERSED_KALSHI | HELD_HARDVEN (excess Pinnacle held to settlement —
                            // never reversed) | DUST_ABSORBED_KALSHI | DUST_ABSORBED_HARDVEN | ORPHANED | HALT
    decimal RecoveredQty,   // contracts/shares successfully hedged or reversed
    decimal LossUsd         // realized loss from the cleanup action
);

// A single naked leg left by a PARTIAL early exit, parked for the 60s monitor loop to keep flattening.
// Attempts counts failed flatten passes; past the cap we stop retrying and let the leg ride to settlement.
public record PendingReversal(
    CrossPair Pair, string Leg /* "kalshi" | "hardven" */, string KalshiSide, string KBookKey,
    string HardVenToken, bool NegRisk, decimal Qty, decimal EntryPrice, string ExecId, int Attempts = 0);

/// <summary>
/// Fires simultaneous IOC/FAK orders on both legs when CrossPlatformArbTelemetryStrategy
/// detects a cross-platform arb window. Kalshi leg uses IOC via PlaceOrderAsync;
/// HardVen leg uses FAK via HardVenOrderClient (HARDVEN_GNOSIS_SAFE signing).
/// </summary>
public class CrossArbExecutor
{
    private readonly IKalshiOrderExecutor _kalshi;
    private readonly IHardVenOrderExecutor _hardven;
    private readonly CrossPlatformArbTelemetryStrategy _telemetry;
    private readonly ConcurrentDictionary<string, LocalOrderBook> _books;
    private readonly CrossArbRestVerifier? _restVerifier;

    // PRE-LIVE-ONLY execution gate (pre-live-first phase). Pre-live lines are stable, so both legs are filled
    // SIMULTANEOUSLY (the current parallel model). In-play lines move fast and need the Pinnacle-FIRST sequential
    // model, which isn't built yet — so skip in-play arbs for now. HARDVEN_PRELIVE_ONLY=0 re-enables in-play
    // (only once the in-play execution model exists). Telemetry-only mode never reaches here.
    private readonly bool _preLiveOnly = Environment.GetEnvironmentVariable("HARDVEN_PRELIVE_ONLY") != "0";

    // WS-VERIFY gate: never place a real bet on a HardVen leg whose price is SCREENING-ONLY (an httpx re-seed of
    // an untabbed tail league, possibly stale/delayed) — only on a leg confirmed on the live browser WS
    // (sidecar tag wv=true). The telemetry fires /verify on the same window open to promote that league to a live
    // tab, so once it's WS-covered the re-opened arb executes. No-op in non-reader mode (wv always true).
    // HARDVEN_REQUIRE_WS_VERIFIED=0 disables (e.g. paho/dedicated-WS mode where everything is already live).
    private readonly bool _requireWsVerified = Environment.GetEnvironmentVariable("HARDVEN_REQUIRE_WS_VERIFIED") != "0";

    // When venue time-skew exceeds this value, block and REST-verify before firing.
    private const double StaleGateMs = 5_000.0;
    // When either book's absolute age exceeds this, REST-verify regardless of relative skew.
    private const double AbsoluteStaleMs = 30_000.0;

    // ── Early exit tuning ─────────────────────────────────────────────────────
    // Triggered on every book update for pairs with an open position (event-driven),
    // with a 60 s fallback timer in case a book update is missed.
    // EarlyExitThreshold: fraction of expected settlement profit required to exit early.
    //   0   = never exit early (hold to settlement)
    //   0.5 = exit when unrealized PnL ≥ 50 % of expected profit
    //   0.6 = exit when unrealized PnL ≥ 60 % of expected profit   ← default
    //   1.0 = only exit when full settlement value is available on the bid
    //   NOTE: only applies when --min-buy is OFF; --min-buy forces break-even early-exit (see breakEvenMode).
    private static decimal  EarlyExitThreshold         = 0.60m;
    private const  decimal  EarlyExitMinProfitUsd      = 0.05m;  // skip micro-exits below this
    // Break-even mode fires at exactly ≥0, so normal fill slippage flips the exit negative (the
    // "exit bleed"). Require a few ticks past break-even before selling. Non-breakeven mode already
    // has a positive hurdle (EarlyExitThreshold × expectedProfit) so it doesn't need this cushion.
    private const  decimal  ExitCushionPerSet          = 0.004m;
    private const  int      EarlyExitFallbackIntervalMs  = 60_000; // fallback poll if a book update was missed
    private const  decimal  MinRestoreHardVenShares          = 1.0m;  // a HardVen leg reduced to dust means the position was closed — don't resurrect it
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
    private bool _hardvenLowAlerted;
    private readonly int     _executionWindowWeeks;  // --wN: only execute arbs whose Kalshi close date is within N weeks of now (0 = no window)
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
    // EARLY EXIT closes a position early by selling BOTH legs to lock profit before settlement. The Pinnacle
    // leg is IRREVERSIBLE (can't be sold back), and the model is HOLD-TO-SETTLEMENT / only-adjust-Kalshi, so
    // early exit is OFF by default. HARDVEN_EARLY_EXIT=1 re-enables it (only meaningful on a reversible venue).
    private readonly bool    _earlyExitEnabled = Environment.GetEnvironmentVariable("HARDVEN_EARLY_EXIT") == "1";
    private          int     _triesRemaining       = -1;  // -1 = unlimited
    private          int     _tryLimit             = -1;  // original N, for display
    private          CancellationTokenSource? _outerCts;
    private readonly ConcurrentDictionary<string, byte>    _inFlight          = new();
    private readonly ConcurrentDictionary<string, byte>    _earlyExitScheduled = new();
    private readonly ConcurrentDictionary<string, decimal> _perPairInvested        = new();
    private readonly ConcurrentDictionary<string, int>     _settlementAbsentTicks  = new();
    private readonly ConcurrentDictionary<string, int>                   _hardvenFeeRates  = new();
    private readonly ConcurrentDictionary<string, (decimal R, double E)> _hardvenFeeParams = new();
    private readonly ConcurrentDictionary<string, string>                 _hardvenTickSizes;
    private readonly HashSet<string>                        _blocklist       = new(StringComparer.OrdinalIgnoreCase);
    private readonly object                                 _blocklistLock   = new();
    private readonly bool          _logErrors;
    private readonly string        _errorLogPath = "error_log.txt";
    private readonly SemaphoreSlim _errorLogLock = new(1, 1);
    private          int _kalshiConsecErrors = 0;
    private          int _hardvenConsecErrors   = 0;
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
    // Ops rule: the only halts are daily-loss tripwire, manual, and network errors. A naked leg is always
    // worked, but ONLY on the KALSHI side — Kalshi is the exchange leg we can adjust: excess KALSHI is reversed
    // (sold back); excess PINNACLE (Kalshi under-filled) is hedged UP on Kalshi if net ≤ _hedgeMaxNet, else HELD
    // to settlement. The Pinnacle bet is irreversible, so it is NEVER sold back and never orphaned.
    private readonly bool    _perTradeTripwire;     // halt on a single fill landing >N× worse than its edge
    private readonly decimal _tradeMaxLossMult;     // the N above (was the const 3.0)
    private readonly decimal _hedgeMaxNet;          // complete a hedge only if net ≤ this (1.0 = break-even)
    private readonly int     _reverseFloorCents;    // Kalshi reverse sweeps the book down to this price (Kalshi only)
    private readonly int     _reverseMaxAttempts;   // sweep attempts before orphaning the remainder
    // Cap the HardVen IOC limit buffer to this fraction of the ask price. Without the cap, the flat
    // halfAllow buffer doubles the dollar budget on cheap legs (ask=0.05 → limit=0.10+).
    private const  decimal  MaxHardVenLimitBufferPct      = 0.15m;

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
    private          decimal _hardvenBalanceUsd;
    private readonly object  _balanceLock = new();

    // ── Fee model (must mirror CrossPlatformArbTelemetryStrategy) ────────────
    // Kalshi: 0.07 × p × (1-p) per contract (server-side, modelled client-side).
    // HardVen:   r × (p×(1-p))^e per share — r and e from /clob-markets fd, fetched at startup.
    //   _hardvenFeeRates  stores base_fee from /fee-rate  → feeRateBps for order submission only.
    //   _hardvenFeeParams stores (r, e) from /clob-markets → fee math only.
    private static decimal KalshiFee(decimal p) => 0.07m * p * (1m - p);
    private decimal HardVenFee(decimal p, string tokenId)
    {
        if (_hardvenFeeParams.TryGetValue(tokenId, out var fp))
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
            string  pBidKey   = pos.ArbType == "K_YES_P_NO" ? $"H:{pair?.HardVenNoTokenId}" : $"H:{pair?.HardVenYesTokenId}";
            string  hardvenToken = pos.ArbType == "K_YES_P_NO" ? pair?.HardVenNoTokenId ?? "" : pair?.HardVenYesTokenId ?? "";
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
                    decimal entryFees = KalshiFee(pos.KalshiEntryPrice) + HardVenFee(pos.HardVenEntryPrice, hardvenToken);
                    decimal exitFees  = KalshiFee(kBid)                 + HardVenFee(pBid,               hardvenToken);
                    decimal pnlPerSet = (kBid + pBid) - exitFees - (pos.KalshiEntryPrice + pos.HardVenEntryPrice) - entryFees;
                    unrealizedPnl     = pnlPerSet * pos.KalshiContracts;
                }
                // Can monitor via REST even when WS books are stale
                exitEligible = (kBid > 0m && pBid > 0m) || _restVerifier != null;
            }

            result.Add(new PositionStatus(pairId, label, pos.ArbType,
                pos.KalshiContracts, pos.HardVenShares,
                pos.KalshiEntryPrice, pos.HardVenEntryPrice,
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
    public decimal HardVenBalanceUsd       { get { lock (_balanceLock)  return _hardvenBalanceUsd;          } }
    public decimal TotalExposure        { get { lock (_exposureLock) return _totalExposure;           } }
    public decimal TotalInvested        { get { lock (_exposureLock) return _totalInvested;           } }
    public decimal TotalProjectedProfit { get { lock (_exposureLock) return _totalProjectedProfit;    } }

    public CrossArbExecutor(
        IKalshiOrderExecutor            kalshi,
        IHardVenOrderExecutor        hardven,
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
        ConcurrentDictionary<string, string>? hardvenTickSizes = null,
        CrossArbRestVerifier? restVerifier = null,
        decimal hedgeMaxNet         = 1.0m,
        int     reverseFloorCents   = 1,
        int     reverseMaxAttempts  = 4,
        decimal tradeMaxLossMult    = 3.0m,
        bool    perTradeTripwire    = true,
        decimal minPlausibleNet     = 0.90m,
        DiscordNotifier? discord    = null,
        decimal lowBalanceAlertUsd  = 15m,
        int     executionWindowWeeks = 0)
    {
        _kalshi              = kalshi;
        _hardven                = hardven;
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
        _executionWindowWeeks = executionWindowWeeks;
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
        _hardvenTickSizes       = hardvenTickSizes ?? new();
        _csvPath             = $"CrossArbExecution_{DateTime.UtcNow:yyyyMMdd_HHmmss}.csv";
        _csvWriterTask = Task.Run(RunCsvWriterAsync);
        var journalDir = Path.GetDirectoryName(Path.GetFullPath(_journalPath));
        if (journalDir != null) Directory.CreateDirectory(journalDir);

        // Surface Kalshi 429 back-offs to the journal. The real client is shared across order POSTs,
        // book-refresh GETs, balance/position polls and REST verification, so this one hook captures
        // every rate-limit retry app-wide. (Dry-run uses a sim client — nothing to wire.)
        if (_kalshi is KalshiOrderClient kalshiClient)
            kalshiClient.RateLimitRetryLogger = OnKalshiRateLimitRetry;

        // HardVen order-retry logging: the stub HardVenOrderClient has no OrderRetryLogger yet.
        // TODO: wire OnHardVenOrderRetry once HardVenOrderClient implements a retry-logger hook.

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
            lock (_balanceLock) { _kalshiBalanceUsd = 1000m; _hardvenBalanceUsd = 1000m; }
            Console.WriteLine("[BALANCE INIT] Dry-run: simulation seeded at $1,000.00 on each platform");
            _ = Task.Run(RunEarlyExitMonitorAsync);
            AnnounceStartup();
            return;
        }
        await RefreshBalancesAsync(initial: true);
        await PrefetchFeeRatesAsync();
        AnnounceStartup();
        _ = Task.Run(PeriodicBalanceRefreshLoop);
        _ = Task.Run(RunEarlyExitMonitorAsync);
        _ = Task.Run(async () =>
        {
            while (!_cts.Token.IsCancellationRequested)
            {
                try { await Task.Delay(30_000, _cts.Token); }
                catch (TaskCanceledException) { break; }
                await PingKalshiAsync();
                await PingHardVenAsync();
            }
        });
    }

    // One-shot startup announcement to Discord — fires once the long HardVen fee prefetch finishes and the
    // bot is entering its trading phase. Also serves as a live webhook smoke test on every (re)start.
    private void AnnounceStartup()
    {
        decimal k, p;
        lock (_balanceLock) { k = _kalshiBalanceUsd; p = _hardvenBalanceUsd; }
        int pairCount = _telemetry.GetAllPairs().Count();
        string mode = _dryRun ? "DRY-RUN" : "LIVE";
        DiscordAlert($"✅ {mode} started — startup complete (HardVen fees prefetched), monitoring {pairCount} pair(s). Cash: Kalshi ${k:0.00} / HardVen ${p:0.00}.");
    }

    private async Task PrefetchFeeRatesAsync()
    {
        var tokens = _telemetry.GetAllPairs()
            .SelectMany(p => new[] { p.HardVenYesTokenId, p.HardVenNoTokenId })
            .Where(t => !string.IsNullOrEmpty(t))
            .Distinct()
            .ToList();
        if (tokens.Count == 0) return;
        Console.WriteLine($"[FEE PREFETCH] Fetching fee parameters for {tokens.Count} HardVen token(s)...");
        int idx = 0;
        foreach (var token in tokens)
        {
            idx++;
            string tok = token[..Math.Min(8, token.Length)];

            int bps = await _hardven.GetTakerFeeAsync(token);
            _hardvenFeeRates[token] = bps;
            await Task.Delay(500);

            var (r, e) = await _hardven.GetFeeParamsAsync(token);
            _hardvenFeeParams[token] = (r, e);
            await Task.Delay(500);

            string ts = await _hardven.GetTickSizeAsync(token);
            _hardvenTickSizes[token] = ts;
            Console.WriteLine($"[FEE PREFETCH] ({idx}/{tokens.Count}) {tok}... order={bps} bps  math: r={r:0.000} e={e:0.0}  tick={ts}");
            await Task.Delay(500);
        }
        _telemetry.HardVenFeeRates  = _hardvenFeeRates;
        _telemetry.HardVenFeeParams = _hardvenFeeParams;
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
            decimal newHardVen   = await _hardven.GetUsdcBalanceAsync();
            lock (_balanceLock)
            {
                _kalshiBalanceUsd = newKalshi; _hardvenBalanceUsd = newHardVen;
                // Low-cash alert, debounced per side: fire once on cross-below, re-arm on cross-above,
                // so the ~5-min balance poll doesn't repeat the alert every cycle.
                if (newKalshi < _lowBalanceAlertUsd) { if (!_kalshiLowAlerted) { _kalshiLowAlerted = true; DiscordAlert($"⚠️ Low cash: Kalshi ${newKalshi:0.00} < ${_lowBalanceAlertUsd:0.00} — top up to avoid balance-skipping arbs."); } }
                else _kalshiLowAlerted = false;
                if (newHardVen < _lowBalanceAlertUsd) { if (!_hardvenLowAlerted) { _hardvenLowAlerted = true; DiscordAlert($"⚠️ Low cash: HardVen ${newHardVen:0.00} < ${_lowBalanceAlertUsd:0.00} — top up to avoid balance-skipping arbs."); } }
                else _hardvenLowAlerted = false;
            }
            string tag = initial ? "[BALANCE INIT]" : "[BALANCE]";
            Console.WriteLine($"{tag} Kalshi=${newKalshi:0.00} HardVen=${newHardVen:0.00}");
            DebugLog.Balance($"RefreshBalancesAsync: K=${newKalshi:0.00} P=${newHardVen:0.00} initial={initial}");
            await JournalAsync(JsonSerializer.Serialize(new {
                t = DateTime.UtcNow, @event = "BALANCE",
                kalshi = newKalshi, hardven = newHardVen, initial
            }));
            if (initial)
                await JournalAsync(JsonSerializer.Serialize(new {
                    t = DateTime.UtcNow, @event = "RUN_CONFIG",
                    singleEntry = _singleEntry, minBuy = _minBuy,
                    maxBetUsd = _maxBetUsd, maxExposureUsd = _maxExposureUsd,
                    executionThreshold = _executionThreshold, execNetFloor = _execNetFloor,
                    minPlausibleNet = _minPlausibleNet, lowBalanceAlertUsd = _lowBalanceAlertUsd,
                    executionWindowWeeks = _executionWindowWeeks,
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
                heldK = heldPos.KalshiContracts, heldP = heldPos.HardVenShares,
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

        // --wN rolling execution window: only fire arbs whose Kalshi close (settlement) date is within N
        // weeks of *now*. Evaluated live on every attempt (NOT a startup filter), so the window rolls
        // forward each day — a far-out pair becomes eligible once it crosses into range. Fail-closed: a
        // pair with no date can't be confirmed in-window, so it's skipped while a window is active.
        if (_executionWindowWeeks > 0)
        {
            DateTime  horizon   = DateTime.UtcNow.AddDays(_executionWindowWeeks * 7);
            DateTime? settleUtc = pair.SettlementDate?.ToDateTime(TimeOnly.MinValue, DateTimeKind.Utc);
            if (settleUtc is null || settleUtc.Value > horizon)
            {
                double weeksOut = settleUtc is null ? -1.0 : (settleUtc.Value - DateTime.UtcNow).TotalDays / 7.0;
                string dateStr  = pair.SettlementDate?.ToString("yyyy-MM-dd") ?? "no-date";
                Console.WriteLine($"[EXEC SKIP] {pair.Label}: settles {dateStr} (~{(weeksOut < 0 ? 0 : weeksOut):0.0}w out) — outside {_executionWindowWeeks}w window");
                await JournalAsync(JsonSerializer.Serialize(new {
                    t = DateTime.UtcNow, @event = "EXEC_SKIP", pairId, arbType,
                    reason = "OUTSIDE_WINDOW", settlementDate = dateStr,
                    weeksOut = weeksOut < 0 ? (object?)null : Math.Round(weeksOut, 1),
                    windowWeeks = _executionWindowWeeks
                }));
                return;
            }
        }

        // Books are needed for stale-gate timestamp checks; prices come from the detection event.
        if (!_books.TryGetValue($"K:{pair.KalshiTicker}",    out var kYes))
        { Console.WriteLine($"[EXEC SKIP] {pair.Label}: missing book K:{pair.KalshiTicker} — WS not yet subscribed"); return; }
        if (!_books.TryGetValue($"K:{pair.KalshiTicker}_NO", out var kNo))
        { Console.WriteLine($"[EXEC SKIP] {pair.Label}: missing book K:{pair.KalshiTicker}_NO — WS not yet subscribed"); return; }
        if (!_books.TryGetValue($"H:{pair.HardVenYesTokenId}",  out var pYes))
        { Console.WriteLine($"[EXEC SKIP] {pair.Label}: missing book P:yes — WS not yet subscribed"); return; }
        if (!_books.TryGetValue($"H:{pair.HardVenNoTokenId}",   out var pNo))
        { Console.WriteLine($"[EXEC SKIP] {pair.Label}: missing book P:no — WS not yet subscribed"); return; }

        // kLegAsk / pLegAsk are the prices captured at detection time — no WS re-read here.
        // Avoids the race where Task.Run scheduling delay lets the book update before we read it.
        // The stale gate below will REST-override them when the book age is > StaleGateMs.
        string  kalshiSide, hardvenToken;
        double  venueSkewMs = 0;

        if (arbType == "K_YES_P_NO")
        {
            kalshiSide = "yes";
            hardvenToken  = pair.HardVenNoTokenId;
        }
        else // K_NO_P_YES
        {
            kalshiSide = "no";
            hardvenToken  = pair.HardVenYesTokenId;
        }

        // PRE-LIVE-ONLY gate: the chosen HardVen leg's match is in-play → skip (simultaneous fill only holds for
        // stable pre-live lines; in-play needs the Pinnacle-first model, not built yet). Both HardVen legs share
        // the match state, so checking the chosen one is enough.
        var chosenHardVenBook = arbType == "K_YES_P_NO" ? pNo : pYes;
        if (_preLiveOnly && chosenHardVenBook.IsLive)
        {
            Console.WriteLine($"[EXEC SKIP] {pair.Label}: IN-PLAY — pre-live-only gate is ON (HARDVEN_PRELIVE_ONLY=0 to allow in-play)");
            return;
        }

        // WS-VERIFY gate: the chosen HardVen leg's price must be confirmed on the live browser WS, not a
        // screening-only httpx re-seed of an untabbed tail league. If unverified, skip — the telemetry has
        // already fired /verify on this window open (promoting the league to a live tab), so the re-opened arb
        // will pass once it's WS-covered. Never risks real money on a possibly-stale screening price.
        if (_requireWsVerified && !_telemetry.IsHardVenVerified(hardvenToken))
        {
            Console.WriteLine($"[EXEC SKIP] {pair.Label}: HardVen leg NOT WS-verified (screening-only) — " +
                              "awaiting verify tab; will execute once WS-confirmed");
            return;
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
                decimal freshNet = freshK + freshP + KalshiFee(freshK) + HardVenFee(freshP, hardvenToken);
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
        decimal netNow = kLegAsk + pLegAsk + KalshiFee(kLegAsk) + HardVenFee(pLegAsk, hardvenToken);
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
        // HardVen gets the remaining margin as buffer; floor at exact ask if margin is thin.
        decimal halfAllow   = (_executionThreshold - netNow) / 2m;
        int     kPriceCents = (int)Math.Floor((kLegAsk + 0.01m) * 100m);
        decimal pBufferCap  = pLegAsk * MaxHardVenLimitBufferPct;
        decimal pLimitAsk   = Math.Min(0.99m, pLegAsk + Math.Min(Math.Max(0m, halfAllow - 0.01m), pBufferCap));
        DebugLog.Trades($"ExecuteAsync {pair.Label}: halfAllow={halfAllow:0.00000} kLimit={kPriceCents}¢ pLimit={pLimitAsk:0.0000}");
        decimal pricePerSet = kLegAsk + pLegAsk;

        // HardVen minimum: per-market orderMinSize and the CLOB's hard $1 dollar floor.
        // Floor pLegAsk to the order tick (0.01) so the minimum count guarantees
        // makerAmount >= $1 after the same rounding SubmitOrderAsync applies.
        decimal pLegAskForMin = Math.Max(0.01m, Math.Floor(pLegAsk * 100m) / 100m);
        int hardvenMinByShare    = (int)Math.Ceiling(pair.HardVenMinSize);
        int hardvenMinByDollar   = (int)Math.Ceiling(1.00m / pLegAskForMin);
        int hardvenMinContracts  = Math.Max(hardvenMinByShare, hardvenMinByDollar);

        // --min-buy: trade exactly the HardVen floor amount (ignores maxBet — test/debug mode)
        int contracts = _minBuy
            ? hardvenMinContracts
            : (int)Math.Floor(_maxBetUsd / pricePerSet);

        if (contracts < hardvenMinContracts)
        {
            Console.WriteLine(
                $"[EXEC SKIP] {pair.Label} | pricePerSet=${pricePerSet:0.0000} > maxBet=${_maxBetUsd:0.00} " +
                $"(need {hardvenMinContracts} contracts, pLeg=${pLegAsk:0.0000})");
            return;
        }
        decimal hardvenShares    = contracts;
        decimal kalshiCost    = kLegAsk * contracts;
        decimal hardvenCost      = pLegAsk * contracts;
        decimal estimatedCost = kalshiCost + hardvenCost;
        decimal minBuffer     = _maxBetUsd * _balanceBufferPct;

        // Balance check — scale contracts down to what's affordable if needed.
        // All sizing and speculative reservation happen inside a single lock to prevent
        // concurrent executions from double-spending the same balance.
        int     idealContracts = contracts;
        decimal kBalSnap, pBalSnap;
        lock (_balanceLock)
        {
            kBalSnap = _kalshiBalanceUsd;
            pBalSnap = _hardvenBalanceUsd;

            // Max contracts each side can fund while preserving the buffer reserve.
            int kAffordable = kLegAsk > 0 ? (int)Math.Floor((kBalSnap - minBuffer) / kLegAsk) : 0;
            int pAffordable = pLegAsk > 0 ? (int)Math.Floor((pBalSnap - minBuffer) / pLegAsk) : 0;
            contracts = Math.Min(contracts, Math.Min(kAffordable, pAffordable));

            if (contracts >= 1)
            {
                // Recompute costs at the (possibly reduced) contract count and reserve.
                hardvenShares    = contracts;
                kalshiCost    = kLegAsk * contracts;
                hardvenCost      = pLegAsk * contracts;
                estimatedCost = kalshiCost + hardvenCost;
                _kalshiBalanceUsd -= kalshiCost;
                _hardvenBalanceUsd   -= hardvenCost;
            }
            else
            {
                // Nothing reserved — zero out so the restoration below is a no-op.
                kalshiCost = hardvenCost = estimatedCost = 0m;
            }
        }
        if (contracts < hardvenMinContracts)
        {
            lock (_balanceLock)
            {
                _kalshiBalanceUsd += kalshiCost;
                _hardvenBalanceUsd   += hardvenCost;
            }
            Console.WriteLine(
                $"[EXEC SKIP] {pair.Label} | balance-limited to {contracts} contract(s), need ≥ {hardvenMinContracts} " +
                $"(K=${kBalSnap:0.00} P=${pBalSnap:0.00} need K≈${kLegAsk * hardvenMinContracts:0.00} P≈${pLegAsk * hardvenMinContracts:0.00})");
            DebugLog.Trades($"ExecuteAsync {pair.Label}: skipped — {contracts} contracts < hardvenMin {hardvenMinContracts} (balance-limited)");
            return;
        }

        // Depth gate: only fire if both venues can fill the full order at our limit prices.
        // Measures volume at or below each limit — not top-N regardless of price — so a book
        // with 12 contracts at 15¢ doesn't count toward a 9¢ HardVen limit.
        {
            var kBook = arbType == "K_YES_P_NO" ? kYes : kNo;
            var pBook = arbType == "K_YES_P_NO" ? pNo  : pYes;
            decimal kLimitDec     = kLegAsk + 0.01m;   // mirrors kPriceCents calc above
            decimal kDepthAtLimit = kBook.GetAskVolumeAtOrBelow(kLimitDec);
            decimal pDepthAtLimit = pBook.GetAskVolumeAtOrBelow(pLimitAsk);
            if (Math.Min(kDepthAtLimit, pDepthAtLimit) < contracts)
            {
                lock (_balanceLock) { _kalshiBalanceUsd += kalshiCost; _hardvenBalanceUsd += hardvenCost; }
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
            lock (_balanceLock) { _kalshiBalanceUsd += kalshiCost; _hardvenBalanceUsd += hardvenCost; }
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
            lock (_balanceLock) { _kalshiBalanceUsd += kalshiCost; _hardvenBalanceUsd += hardvenCost; }
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
            lock (_balanceLock) { _kalshiBalanceUsd += kalshiCost; _hardvenBalanceUsd += hardvenCost; }
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

        // Snapshot pre-trade HardVen balance concurrently with leg orders so reconcile can
        // compute a delta rather than comparing against total wallet balance. This prevents
        // spurious RECONCILE_MISMATCH halts when a pre-existing HardVen balance is present
        // (e.g. orphaned shares from a prior run where Kalshi settled but HardVen has not).
        // Returns null (not 0) on a failed read so reconcile can tell a real zero balance from a
        // flaky API call and refuse to halt against a fabricated prior.
        Task<decimal?> priorHardVenBalTask = _dryRun
            ? Task.FromResult<decimal?>(0m)
            : SnapshotHardVenBalanceAsync(hardvenToken);

        // Fire both legs simultaneously
        var kalshiTask = PlaceKalshiLegAsync(pair.KalshiTicker, kalshiSide, kPriceCents, contracts, execId, execLog);
        var hardvenTask   = PlaceHardVenLegAsync(hardvenToken, pLimitAsk, hardvenShares, pair.IsNegRisk, execLog);

        // Catch any unhandled leg exception so the OTHER leg's fill is still visible.
        // PlaceXxxLegAsync both have general catch blocks, but those blocks call
        // CheckMaintenanceThresholdAsync which can itself throw, propagating out.
        // If Task.WhenAll throws here we must still reach the recovery section below.
        Exception? legException = null;
        try { await Task.WhenAll(kalshiTask, hardvenTask); }
        catch (Exception ex) { legException = ex; }

        var (kOrderId, kStatus, kFilled) = kalshiTask.IsCompletedSuccessfully
            ? kalshiTask.Result : ("", "error", 0m);
        var (pFilled, pActualPrice) = hardvenTask.IsCompletedSuccessfully
            ? hardvenTask.Result : (0m, 0m);

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

        // Balanced quantity: the portion of each leg that is fully hedged. Floored to a whole number —
        // the Kalshi leg trades in whole contracts only, so a fractional matched qty (e.g. HardVen FAK
        // fills 0.45 vs Kalshi's 31 whole contracts) can never be held or reconciled on Kalshi. Rounding
        // down to whole sets keeps the tracked position integer-valid; any fractional remainder on either
        // leg falls out as unhedged excess and is reversed/absorbed below.
        decimal balancedQty  = Math.Floor(Math.Min(kFilled, pFilled));
        decimal kUnhedged    = kFilled - balancedQty;  // excess Kalshi contracts
        decimal pUnhedged    = pFilled - balancedQty;  // excess HardVen shares
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
            // kLegAsk is the IOC limit price (fills at or below this). pActualPrice is HardVen's
            // reported average fill price and may exceed pLegAsk if the book was walked.
            actualNetPerSet = kLegAsk + pActualPrice + KalshiFee(kLegAsk) + HardVenFee(pActualPrice, hardvenToken);
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
            decimal modeledFees = (KalshiFee(kLegAsk) + HardVenFee(pLegAsk, hardvenToken)) * balancedQty;
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
            recovery = await RecoverUnhedgedAsync(pair, arbType, kalshiSide, hardvenToken,
                kFilled, pFilled, kLegAsk, pActualPrice, execId, execLog);
        }
        else if (neitherFilled)
        {
            hadError = true;
            Emit(execLog, $"[EXEC MISS] {pair.Label} | Neither leg filled. k-status={kStatus}");
        }

        // Dust fold: on DUST_ABSORBED_HARDVEN the venue holds pFilled but the tracked position only has
        // balancedQty. Fold the absorbed shares into HardVenShares so (a) the early exit sweeps them every
        // cycle (the live HardVen sell takes the fractional HardVenShares un-floored) instead of orphaning a
        // remainder that accumulates across cycles past reconcile's 0.5-share tolerance, and (b) reconcile's
        // expected venue (pFilled) matches the position even when the pre-trade balance snapshot reads 0.
        if (recovery?.Outcome == "DUST_ABSORBED_HARDVEN"
            && _openPositions.TryGetValue(pairId, out var dustPos))
        {
            decimal folded = dustPos.HardVenShares + recovery.RecoveredQty;
            _openPositions[pairId] = dustPos with { HardVenShares = folded };
            DebugLog.Trades($"ExecuteAsync {pair.Label}: folded {recovery.RecoveredQty:0.####} absorbed HardVen dust → HardVenShares={folded:0.####}.");
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
            decimal pHeld = finalPos?.HardVenShares      ?? 0m;

            // Net/cost from the FINAL position, not the initial-fill locals. On a
            // hedge-completed-from-zero entry balancedQty==0, so pActualPrice/actualNetPerSet
            // are still 0 and would log pAvgPrice=0, net=0, projected=kHeld*(1-0). finalPos
            // carries the real blended prices set in the recovery branch.
            decimal finalNet = finalPos != null
                ? finalPos.KalshiEntryPrice + finalPos.HardVenEntryPrice
                  + KalshiFee(finalPos.KalshiEntryPrice) + HardVenFee(finalPos.HardVenEntryPrice, hardvenToken)
                : actualNetPerSet;

            // Exposure/projection from the FINAL position, so a hedge-completed-from-zero entry
            // (balancedQty==0) is counted and a pure miss (kHeld==0) contributes nothing. Counts
            // exactly when the EXECUTION_COMPLETE record below emits a position block (kHeld > 0).
            if (kHeld > 0)
            {
                decimal finalCost   = finalPos!.KalshiEntryPrice * kHeld + finalPos.HardVenEntryPrice * pHeld;
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
                    hardven = new {
                        ordered = hardvenShares, filled = Math.Round(pFilled, 6),
                        limitPrice = pLegAsk,
                        avgFillPrice = pFilled > 0 ? Math.Round(pActualPrice, 6) : (object?)null,
                        feePerShare  = pFilled > 0 ? (object?)Math.Round(HardVenFee(pActualPrice, hardvenToken), 6) : null,
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
                    pAvgPrice   = Math.Round(finalPos.HardVenEntryPrice, 6),
                    totalCostUsd       = Math.Round(finalPos.KalshiEntryPrice * kHeld + finalPos.HardVenEntryPrice * pHeld, 4),
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
            if (_kalshi is SimulatedVenuePositionClient kSim) kSim.InjectMismatch(pair.KalshiTicker, +1);
            // HardVen sim balance-mismatch injection omitted — stub-only scaffold has no SimulatedHardVenClient.
            // TODO: restore (compute injectTokenId from pair.HardVen{No,Yes}TokenId) once a HardVen sim exists.
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
                decimal? priorHardVenBal = priorHardVenBalTask.IsCompletedSuccessfully ? priorHardVenBalTask.Result : null;
                // Order-poll reflects fills immediately; the positions endpoint lags 10–20s.
                // A clean Kalshi reverse can still use order-poll: poll the original buy fill and
                // subtract the venue-confirmed reversed qty. Other kUnhedged>0 outcomes
                // (hedge-completed / dust-absorbed) still fall back to the positions endpoint.
                bool kReversed = recovery?.Outcome == "REVERSED_KALSHI";
                string reconcileOrderId = (kUnhedged == 0 || kReversed) ? kOrderId : "";
                decimal reversedKalshiQty = kReversed ? recovery!.RecoveredQty : 0m;
                // pFilled (not balancedQty) whenever the Pinnacle position was left INTACT on the venue:
                //   * Case-A recovery only touched Kalshi (reversed/absorbed the Kalshi excess), OR
                //   * Case-B recovery — Pinnacle is NEVER reversed now, so both HELD_HARDVEN (excess held to
                //     settlement) and HEDGE_COMPLETED (excess hedged up on Kalshi) leave Pinnacle at pFilled.
                // Using pFilled keeps the held/intact excess from tripping reconcile's >0.5 HardVen over-read halt.
                decimal expectedHardVenVenue =
                    recovery?.Outcome is "REVERSED_KALSHI" or "DUST_ABSORBED_KALSHI"
                                      or "HELD_HARDVEN" or "HEDGE_COMPLETED" or "DUST_ABSORBED_HARDVEN"
                        ? pFilled : balancedQty;
                // A HardVen overfill reversal (bought too many, sold the excess in-trade) can race the
                // pre-trade snapshot and poison reconcile's delta check — flag it so reconcile trusts
                // the absolute position instead of the (contaminated) delta.
                bool hardvenOverfillReversed = recovery?.Outcome == "REVERSED_HARDVEN";
                _ = Task.Run(async () => await ReconcileTradeAsync(pair, arbType, balancedQty, expectedHardVenVenue, execId, reconcileOrderId, priorHardVenBal, reversedKalshiQty, hardvenOverfillReversed));
            }
        }
        else
        {
            // Nothing filled — restore speculative balance reservation in full.
            lock (_balanceLock) { _kalshiBalanceUsd += kalshiCost; _hardvenBalanceUsd += hardvenCost; }
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

    // ── HardVen FAK leg ────────────────────────────────────────────────────
    // Routes through HARDVEN_PROXY_ADDRESS (Gnosis Safe) via HardVenOrderClient —
    // identical EIP-712 HARDVEN_GNOSIS_SAFE signing as PredictionLiveProduction.

    // Mirrors TryExtractFillFromResponse in HardVenLiveBroker.
    // BUY:  takingAmount = shares received, makingAmount = USDC spent
    // SELL: takingAmount = USDC received,  makingAmount = shares sold
    private static (decimal Shares, decimal Dollars) ExtractHardVenFill(JsonElement root, bool isSell)
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

    private async Task<(decimal FilledShares, decimal AvgPrice)> PlaceHardVenLegAsync(
        string tokenId, decimal price, decimal shares, bool negRisk = false, List<string>? execLog = null)
    {
        string tokenShort = tokenId[..Math.Min(12, tokenId.Length)];
        Emit(execLog, $"[ORDER P] BUY token={tokenShort}... price={price:0.0000} shares={shares}");
        DebugLog.Trades($"PlaceHardVenLegAsync: token={tokenShort}... price={price:0.0000} shares={shares}");
        try
        {
            decimal limitPrice = Math.Min(0.99m, price);
            DebugLog.Trades($"PlaceHardVenLegAsync: limitPrice={limitPrice:0.0000} (evaluated arb price)");

            string result = "";
            // Lazy-fetch fee rate once per token; cache for all subsequent calls.
            // HardVen requires feeRateBps to match the market's rate exactly when non-zero.
            if (!_hardvenFeeRates.ContainsKey(tokenId))
                _hardvenFeeRates[tokenId] = await _hardven.GetTakerFeeAsync(tokenId);
            int feeRate = _hardvenFeeRates[tokenId];
            string tickSize = _hardvenTickSizes.GetValueOrDefault(tokenId, "0.01");
            for (int attempt = 0; attempt < 3; attempt++)
            {
                try
                {
                    DebugLog.Trades($"PlaceHardVenLegAsync: attempt {attempt + 1} negRisk={negRisk} feeRateBps={feeRate} tickSize={tickSize}");
                    result = await _hardven.SubmitOrderAsync(
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
                        DebugLog.Trades($"PlaceHardVenLegAsync: fee autocorrect — retrying with feeRateBps={fee}");
                        _hardvenFeeRates[tokenId] = fee;
                        feeRate = fee;
                    }
                    else
                        throw;
                }
            }

            if (string.IsNullOrEmpty(result))
            {
                Emit(execLog, $"[FILL P WARN] {tokenShort}... empty result from SubmitOrderAsync");
                DebugLog.Trades($"PlaceHardVenLegAsync: empty result from SubmitOrderAsync");
                return (0m, 0m);
            }
            Interlocked.Exchange(ref _hardvenConsecErrors, 0);

            using var doc = JsonDocument.Parse(result);
            var root = doc.RootElement;

            if (!root.TryGetProperty("success", out var sv) || !sv.GetBoolean())
            {
                Emit(execLog, $"[FILL P WARN] {tokenShort}... success=false — {result[..Math.Min(200, result.Length)]}");
                DebugLog.Trades($"PlaceHardVenLegAsync: success=false in response — {result[..Math.Min(200, result.Length)]}");
                return (0m, 0m);
            }

            string orderId = root.TryGetProperty("orderID", out var oidEl) ? oidEl.GetString() ?? "" : "";
            string respStatus = root.TryGetProperty("status", out var stEl) ? stEl.GetString() ?? "" : "";
            Console.ForegroundColor = ConsoleColor.Green;
            Emit(execLog, $"[FILL P]  {tokenShort}... placed orderID={orderId} status={respStatus}");
            Console.ResetColor();
            DebugLog.Trades($"PlaceHardVenLegAsync: orderID={orderId} status={respStatus}");

            // BUY: takingAmount = shares received, makingAmount = USDC spent
            (decimal filledShares, decimal spentUsdc) = ExtractHardVenFill(root, isSell: false);
            DebugLog.Trades($"PlaceHardVenLegAsync: response fill — shares={filledShares} spent={spentUsdc}");

            // REST poll fallback — production uses this when the POST response doesn't carry fill
            // amounts (e.g. status=delayed). FAK should fill immediately, but poll once to be safe.
            if (filledShares <= 0 && !string.IsNullOrEmpty(orderId))
            {
                DebugLog.Trades($"PlaceHardVenLegAsync: no fill in response — polling orderID={orderId}");
                try
                {
                    string pollResult = await _hardven.GetOrderAsync(orderId);
                    using var pollDoc = JsonDocument.Parse(pollResult);
                    var pollRoot = pollDoc.RootElement;
                    JsonElement orderData = pollRoot.ValueKind == JsonValueKind.Array && pollRoot.GetArrayLength() > 0
                        ? pollRoot[0] : pollRoot;

                    string pollStatus = orderData.TryGetProperty("status", out var ps) ? ps.GetString() ?? "" : "";
                    DebugLog.Trades($"PlaceHardVenLegAsync: poll status={pollStatus}");

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

                        DebugLog.Trades($"PlaceHardVenLegAsync: poll fill — shares={filledShares} spent={spentUsdc}");
                    }
                }
                catch (Exception pollEx)
                {
                    Emit(execLog, $"[FILL P WARN] {tokenShort}... poll failed for orderID={orderId}: {pollEx.Message}");
                    DebugLog.Trades($"PlaceHardVenLegAsync: poll failed for orderID={orderId}: {pollEx.Message}");
                }
            }

            if (filledShares <= 0)
            {
                Emit(execLog, $"[FILL P WARN] {tokenShort}... filledShares=0 after response+poll — FAK killed or no liquidity");
                DebugLog.Trades($"PlaceHardVenLegAsync: filledShares=0 after response+poll — FAK killed or no liquidity");
                return (0m, 0m);
            }

            decimal avgPrice = spentUsdc > 0 ? spentUsdc / filledShares : price;
            Console.ForegroundColor = ConsoleColor.Green;
            Emit(execLog, $"[FILL P]  {tokenShort}... filled={filledShares} avgPrice={avgPrice:0.0000}");
            Console.ResetColor();
            DebugLog.Trades($"PlaceHardVenLegAsync: filled={filledShares} avgPrice={avgPrice:0.0000}");
            _ = _hardven.UpdateBalanceAllowanceAsync(tokenId); // give CLOB a head start on settlement
            return (filledShares, avgPrice);
        }
        catch (HttpRequestException ex) when (ex.StatusCode == System.Net.HttpStatusCode.TooManyRequests)
        {
            Emit(execLog, $"[HARDVEN RATE LIMIT] {tokenShort}... — 429, retrying in 1s");
            DebugLog.Trades($"PlaceHardVenLegAsync: 429 on {tokenShort}, backing off 1s");
            await Task.Delay(1_000);
            Exception? retryFailure = null;
            try
            {
                string result2 = await _hardven.SubmitOrderAsync(
                    tokenId, Math.Min(0.99m, price), shares, side: 0, negRisk: negRisk, feeRateBps: 0);
                if (!string.IsNullOrEmpty(result2))
                {
                    Interlocked.Exchange(ref _hardvenConsecErrors, 0);
                    using var doc2 = JsonDocument.Parse(result2);
                    var root2 = doc2.RootElement;
                    if (root2.TryGetProperty("success", out var sv2) && sv2.GetBoolean())
                    {
                        (decimal fs2, decimal su2) = ExtractHardVenFill(root2, isSell: false);
                        if (fs2 > 0)
                        {
                            decimal avg2 = su2 > 0 ? su2 / fs2 : price;
                            Console.ForegroundColor = ConsoleColor.Green;
                            Emit(execLog, $"[FILL P]  {tokenShort}... 429-retry filled={fs2} avg={avg2:0.0000}");
                            Console.ResetColor();
                            DebugLog.Trades($"PlaceHardVenLegAsync: 429-retry filled={fs2} avg={avg2:0.0000}");
                            _ = _hardven.UpdateBalanceAllowanceAsync(tokenId);
                            return (fs2, avg2);
                        }
                    }
                }
            }
            catch (Exception retryEx)
            {
                Emit(execLog, $"[HARDVEN LEG ERROR] {tokenShort}... (after 429): {ApiErrorHelper.ClassifyHardVen(retryEx)}");
                DebugLog.Trades($"PlaceHardVenLegAsync: 429 retry failed for {tokenShort}: {retryEx.Message}");
                retryFailure = retryEx;
            }
            // Count toward maintenance only when the retry failed with a genuine venue-health error. A
            // successful-but-no-fill retry (retryFailure==null; counter already reset above) or an
            // order-level rejection means HardVen is UP — don't trip a spurious CONNECTION HALT.
            if (retryFailure is not null && !ApiErrorHelper.IsHardVenOrderRejection(retryFailure))
                await CheckMaintenanceThresholdAsync("hardven", Interlocked.Increment(ref _hardvenConsecErrors));
            return (0m, 0m);
        }
        catch (Exception ex)
        {
            Emit(execLog, $"[HARDVEN LEG ERROR] {tokenShort}...: {ApiErrorHelper.ClassifyHardVen(ex)}");
            DebugLog.Trades($"PlaceHardVenLegAsync exception for {tokenShort}: {ex}");
            // Only genuine venue-health failures (5xx, timeout, connection, sustained 429) count toward
            // the maintenance/outage tripwire. A FAK no-match / order rejection means HardVen is UP and just
            // refused this order — don't let a fast-market streak of them trip a spurious CONNECTION HALT.
            // (Mirrors the clean 0-fill path above, which also doesn't increment.)
            if (!ApiErrorHelper.IsHardVenOrderRejection(ex))
                await CheckMaintenanceThresholdAsync("hardven", Interlocked.Increment(ref _hardvenConsecErrors));
            return (0m, 0m);
        }
    }

    // ── HardVen FAK sell (reversal) ──────────────────────────────────────────────

    private async Task<(decimal SoldShares, decimal AvgPrice)> PlaceHardVenSellAsync(
        string tokenId, decimal shares, bool negRisk = false, List<string>? execLog = null)
    {
        // HardVen CLOB rejects sell maker amounts with more than 2 decimal places.
        shares = Math.Floor(shares * 100m) / 100m;
        if (shares <= 0m) return (0m, 0m);

        string tokenShort = tokenId[..Math.Min(12, tokenId.Length)];
        Emit(execLog, $"[ORDER P] SELL token={tokenShort}... shares={shares}");
        DebugLog.Trades($"PlaceHardVenSellAsync: token={tokenShort}... shares={shares}");
        try
        {
            // Force CLOB to refresh its cached token balance from on-chain state.
            // Required — tokens may not be settled yet after the buy leg.
            try { await _hardven.UpdateBalanceAllowanceAsync(tokenId); } catch { /* best-effort */ }

            // FAK sell: 0.01 floor so it matches any buyer; actual fill is at best bid
            string result = "";
            int feeRate = _hardvenFeeRates.GetValueOrDefault(tokenId, 0);
            const int maxSellRetries = 10;

            for (int attempt = 1; attempt <= maxSellRetries; attempt++)
            {
                try
                {
                    result = await _hardven.SubmitOrderAsync(
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
                        DebugLog.Trades($"PlaceHardVenSellAsync: fee autocorrect — retrying with feeRateBps={fee}");
                        _hardvenFeeRates[tokenId] = fee;
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
                    try { await _hardven.UpdateBalanceAllowanceAsync(tokenId); } catch { /* best-effort */ }
                }
            }

            if (string.IsNullOrEmpty(result)) return (0m, 0m);

            using var doc = JsonDocument.Parse(result);
            var root = doc.RootElement;
            if (!root.TryGetProperty("success", out var sv) || !sv.GetBoolean())
            {
                Emit(execLog, $"[FILL P WARN] {tokenShort}... sell success=false — {result[..Math.Min(200, result.Length)]}");
                DebugLog.Trades($"PlaceHardVenSellAsync: success=false — {result[..Math.Min(200, result.Length)]}");
                return (0m, 0m);
            }

            // SELL: makingAmount = shares sold, takingAmount = USDC received
            (decimal soldShares, decimal usdcReceived) = ExtractHardVenFill(root, isSell: true);
            decimal avgPrice = soldShares > 0 && usdcReceived > 0 ? usdcReceived / soldShares : 0m;
            Console.ForegroundColor = ConsoleColor.Green;
            Emit(execLog, $"[FILL P]  {tokenShort}... sold={soldShares} avgPrice={avgPrice:0.0000}");
            Console.ResetColor();
            DebugLog.Trades($"PlaceHardVenSellAsync: soldShares={soldShares} avgPrice={avgPrice:0.0000}");
            return (soldShares, avgPrice);
        }
        catch (Exception ex)
        {
            Emit(execLog, $"[HARDVEN SELL ERROR] {tokenShort}...: {ApiErrorHelper.ClassifyHardVen(ex)}");
            DebugLog.Trades($"PlaceHardVenSellAsync exception for {tokenShort}: {ex}");
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
        string kalshiSide, string hardvenToken,
        decimal kFilled, decimal pFilled,
        decimal kLegAsk, decimal pActualPrice,
        string execId = "", List<string>? execLog = null)
    {
        // Floor to whole sets — Kalshi holds whole contracts only (see ExecuteAsync). A fractional
        // smaller-leg (HardVen) fill leaves BOTH legs with excess: the integer Kalshi excess reverses
        // cleanly, the sub-unit HardVen remainder (<1 share) is absorbed in the reverse path below.
        decimal balancedQty = Math.Floor(Math.Min(kFilled, pFilled));
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

            bool hasHardVenBook = _books.TryGetValue($"H:{hardvenToken}", out var pBook);
            // MODEL: recovery only ever adjusts KALSHI. An excess Kalshi leg is REVERSED on Kalshi (below) — we
            // never BUY more Pinnacle to hedge it (that would place another irreversible sportsbook bet). So the
            // buy-Pinnacle hedge that follows is intentionally bypassed (skipHedgeA=true); kept only so the diff
            // stays small — it is dead code and can be deleted once the no-Pinnacle-buy model is settled.
            bool skipHedgeA = true;
            _ = hasHardVenBook; _ = pBook;   // silence "assigned but unused" now that the hedge path is bypassed

            if (!skipHedgeA)
            {
                decimal currentHardVenAsk = pBook!.GetBestAskPrice();
                decimal hedgeNet = kLegAsk + currentHardVenAsk + KalshiFee(kLegAsk) + HardVenFee(currentHardVenAsk, hardvenToken);
                DebugLog.Trades($"RecoverUnhedgedAsync {pair.Label}: kUnhedged={kUnhedged} hardvenAsk={currentHardVenAsk:0.0000} hedgeNet={hedgeNet:0.0000}");

                if (hedgeNet <= _hedgeMaxNet)
                {
                    Console.ForegroundColor = ConsoleColor.Yellow;
                    Emit(execLog, $"[RECOVER] {pair.Label} | kExcess={kUnhedged} hedgeNet={hedgeNet:0.0000} — completing hedge on HardVen");
                    Console.ResetColor();

                    decimal hardvenHedgeLimit = Math.Min(1.0m, currentHardVenAsk + RecoveryHedgeSlippageCents / 100m);
                    // Skip hedge if fractional Kalshi fill makes HardVen order < $1 CLOB minimum
                    decimal hardvenHedgeCost  = Math.Floor(hardvenHedgeLimit * 100m) / 100m * kUnhedged;
                    bool hardvenHedgeTooSmall = hardvenHedgeCost < 1.00m;
                    // Symmetry with Case B: never let a recovery hedge push total open exposure past the cap.
                    bool hardvenHedgeBreachesCap;
                    lock (_exposureLock) hardvenHedgeBreachesCap = _totalExposure + hardvenHedgeCost > _maxExposureUsd;
                    bool skipHardVenHedge = hardvenHedgeTooSmall || hardvenHedgeBreachesCap;
                    if (hardvenHedgeTooSmall)
                        Emit(execLog, $"[RECOVER] {pair.Label} | HardVen hedge cost ${hardvenHedgeCost:0.00} < $1 min — reversing Kalshi excess directly");
                    else if (hardvenHedgeBreachesCap)
                        Emit(execLog, $"[RECOVER] {pair.Label} | HardVen hedge ${hardvenHedgeCost:0.00} would push exposure ${_totalExposure:0.00}→${_totalExposure + hardvenHedgeCost:0.00} past cap ${_maxExposureUsd:0.00} — reversing Kalshi excess instead");
                    (decimal hardvenFill2, decimal hardvenFill2Price) = skipHardVenHedge ? (0m, 0m)
                        : await PlaceHardVenLegAsync(hardvenToken, hardvenHedgeLimit, kUnhedged, pair.IsNegRisk, execLog);
                    if (hardvenFill2 > 0)
                    {
                        decimal additional   = Math.Min(kUnhedged, hardvenFill2);
                        decimal remainderQty = kUnhedged - additional;     // Kalshi left unhedged if the hedge UNDER-filled
                        decimal hardvenExcess   = hardvenFill2 - additional;     // naked HardVen bought past the need if it OVER-filled
                        if (_openPositions.TryGetValue(pair.PairId, out var pos))
                        {
                            decimal newP = pos.HardVenShares + additional;
                            _openPositions[pair.PairId] = pos with
                            {
                                KalshiContracts = pos.KalshiContracts + additional,
                                HardVenShares      = newP,
                                HardVenEntryPrice  = newP > 0
                                    ? (pos.HardVenShares * pos.HardVenEntryPrice + additional * hardvenFill2Price) / newP
                                    : hardvenFill2Price
                            };
                        }
                        else
                            _openPositions[pair.PairId] = new ArbPosition(
                                pair.PairId, arbType, additional, additional,
                                kLegAsk, hardvenFill2Price, DateTime.UtcNow, execId);
                        lock (_exposureLock) { _totalExposure += additional * hardvenFill2Price; }
                        await JournalAsync(JsonSerializer.Serialize(new {
                            t = DateTime.UtcNow, @event = "CLEANUP_HEDGE_COMPLETED", execId,
                            pair = pair.PairId, leg = "hardven",
                            hedgedQty = additional, remainderQty = Math.Round(remainderQty, 6),
                            hardvenFillPrice = Math.Round(hardvenFill2Price, 6)
                        }));
                        Console.ForegroundColor = ConsoleColor.Green;
                        Emit(execLog, $"[RECOVER OK] {pair.Label} | hedge completed +{additional} sets via HardVen retry");
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
                                // Partial HardVen hedge left Kalshi contracts unhedged — reverse them out
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
                        if (hardvenExcess > 0m)
                        {
                            decimal exValue = hardvenExcess * hardvenFill2Price;
                            if (exValue < CleanupDustUsd)
                            {
                                lock (_cleanupLock) { _totalCleanupCostUsd += exValue; }
                                await JournalAsync(JsonSerializer.Serialize(new {
                                    t = DateTime.UtcNow, @event = "CLEANUP_DUST", execId,
                                    pair = pair.PairId, leg = "hardven_hedge_overfill",
                                    qty = Math.Round(hardvenExcess, 6), absorbedUsd = Math.Round(exValue, 4)
                                }));
                                Console.ForegroundColor = ConsoleColor.DarkYellow;
                                Emit(execLog, $"[CLEANUP DUST] {pair.Label} | Absorbing {hardvenExcess:0.00} HardVen hedge-overfill dust (${exValue:0.00}) — no halt");
                                Console.ResetColor();
                            }
                            else
                            {
                                var (exSold, exPx) = await PlaceHardVenSellAsync(hardvenToken, hardvenExcess, pair.IsNegRisk, execLog);
                                decimal exLoss = Math.Max(0m, exSold * (hardvenFill2Price - exPx));
                                lock (_cleanupLock) { _totalCleanupCostUsd += exLoss; }
                                await JournalAsync(JsonSerializer.Serialize(new {
                                    t = DateTime.UtcNow, @event = "CLEANUP_REVERSED", execId,
                                    pair = pair.PairId, leg = "hardven_hedge_overfill",
                                    reason = "OVERFILL",
                                    soldShares = Math.Round(exSold, 6), soldPrice = Math.Round(exPx, 6),
                                    reversalLossUsd = Math.Round(exLoss, 4)
                                }));
                                Console.ForegroundColor = ConsoleColor.Yellow;
                                Emit(execLog, $"[RECOVER] {pair.Label} | reversed {exSold:0.00}/{hardvenExcess:0.00} HardVen hedge-overfill @ {exPx:0.0000} loss=${exLoss:0.00}");
                                Console.ResetColor();
                            }
                        }
                        return new RecoveryResult("HEDGE_COMPLETED", additional, 0);
                    }
                    Emit(execLog, $"[RECOVER] {pair.Label} | HardVen hedge retry failed — reversing Kalshi excess");
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

            // Sub-unit fill leftover: when the smaller (HardVen) leg filled a fraction below one whole set,
            // balancedQty floored down and left this naked HardVen remainder alongside the full Kalshi
            // excess. It's always <1 share (<$1 — under HardVen's $1 CLOB min, so un-sellable), so absorb it
            // as dust and let it settle on its own — nothing left untracked. Normal single-excess
            // recoveries have pUnhedged==0 here and skip this.
            if (pUnhedged > 0m)
            {
                decimal pRemValue = pUnhedged * pActualPrice;
                lock (_cleanupLock) { _totalCleanupCostUsd += pRemValue; }
                await JournalAsync(JsonSerializer.Serialize(new {
                    t = DateTime.UtcNow, @event = "CLEANUP_DUST", execId,
                    pair = pair.PairId, leg = "hardven_subunit_remainder",
                    qty = Math.Round(pUnhedged, 6), absorbedUsd = Math.Round(pRemValue, 4)
                }));
                Console.ForegroundColor = ConsoleColor.DarkYellow;
                Emit(execLog, $"[CLEANUP DUST] {pair.Label} | Absorbing {pUnhedged:0.00} HardVen sub-unit remainder (${pRemValue:0.00}) — no halt");
                Console.ResetColor();
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

        // ── Case B: HardVen (Pinnacle) filled more than Kalshi (Kalshi under-filled) ──
        // The Pinnacle bet is IRREVERSIBLE — a placed sportsbook bet stands — so we ONLY ever adjust KALSHI:
        // hedge as much of the excess as we can by BUYING MORE KALSHI, and HOLD whatever we can't hedge
        // (sub-1-contract, hedge net too poor, exposure cap, missing/short Kalshi fill) as a directional Pinnacle
        // position to settlement. We NEVER sell the Pinnacle leg back and NEVER orphan it (a held bet is
        // intentional, not a stuck error). Pinnacle therefore always ends at pFilled → reconcile expects pFilled.
        if (pUnhedged > 0)
        {
            // Record the leftover excess Pinnacle as HELD to settlement (no reversal, no halt, no orphan).
            async Task<RecoveryResult> HoldHardVenAsync(decimal qty, string reason)
            {
                decimal val = qty * pActualPrice;
                lock (_cleanupLock) { _totalCleanupCostUsd += val; }   // theoretical worst case if the held leg loses
                await JournalAsync(JsonSerializer.Serialize(new {
                    t = DateTime.UtcNow, @event = "HELD_HARDVEN", execId,
                    pair = pair.PairId, leg = "hardven", reason,
                    qty = Math.Round(qty, 6), heldValueUsd = Math.Round(val, 4)
                }));
                Console.ForegroundColor = ConsoleColor.Yellow;
                Emit(execLog, $"[RECOVER HOLD] {pair.Label} | holding {qty:0.00} unhedged Pinnacle share(s) to settlement ({reason}) — Pinnacle is never reversed");
                Console.ResetColor();
                return new RecoveryResult("HELD_HARDVEN", qty, val);
            }

            int    hedgeQty      = (int)Math.Floor(pUnhedged);
            string kHedgeKey     = arbType == "K_YES_P_NO" ? $"K:{pair.KalshiTicker}" : $"K:{pair.KalshiTicker}_NO";
            bool   hasKalshiBook = _books.TryGetValue(kHedgeKey, out var kHedgeBook);

            // No whole contract to hedge (sub-1-share) or no Kalshi book to hedge on → hold the whole excess.
            if (hedgeQty < 1 || !hasKalshiBook)
                return await HoldHardVenAsync(pUnhedged, hedgeQty < 1 ? "SUB_1_CONTRACT" : "NO_KALSHI_BOOK");

            decimal currentKalshiAsk = kHedgeBook!.GetBestAskPrice();
            decimal hedgeNet = currentKalshiAsk + pActualPrice + KalshiFee(currentKalshiAsk) + HardVenFee(pActualPrice, hardvenToken);
            DebugLog.Trades($"RecoverUnhedgedAsync {pair.Label}: pUnhedged={pUnhedged} kalshiAsk={currentKalshiAsk:0.0000} hedgeNet={hedgeNet:0.0000}");

            // Hedge net worse than allowed → locking a hedge loss is worse than holding the directional bet.
            if (hedgeNet > _hedgeMaxNet)
                return await HoldHardVenAsync(pUnhedged, $"HEDGE_NET_{hedgeNet:0.000}_gt_maxNet");

            // Hedge would breach the exposure cap → hold rather than enlarge exposure.
            decimal hedgeCost = hedgeQty * currentKalshiAsk;
            bool breachesCap;
            lock (_exposureLock) breachesCap = _totalExposure + hedgeCost > _maxExposureUsd;
            if (breachesCap)
            {
                Emit(execLog, $"[RECOVER] {pair.Label} | hedge ${hedgeCost:0.00} would breach the ${_maxExposureUsd:0.00} exposure cap — holding");
                return await HoldHardVenAsync(pUnhedged, "EXPOSURE_CAP");
            }

            // Hedge the whole-contract part on Kalshi (buy more Kalshi to match the excess Pinnacle).
            int currentKCents = Math.Max(1, (int)Math.Ceiling(currentKalshiAsk * 100) + RecoveryHedgeSlippageCents);
            Console.ForegroundColor = ConsoleColor.Yellow;
            Emit(execLog, $"[RECOVER] {pair.Label} | pExcess={pUnhedged:0.0000} hedgeQty={hedgeQty} hedgeNet={hedgeNet:0.0000} — hedging on Kalshi (Pinnacle held)");
            Console.ResetColor();
            var (_, _, kFill2) = await PlaceKalshiLegAsync(
                pair.KalshiTicker, kalshiSide, currentKCents, hedgeQty, execId + "_RH", execLog);
            decimal additional = Math.Min((decimal)hedgeQty, Math.Max(0m, kFill2));
            if (additional > 0m)
            {
                if (_openPositions.TryGetValue(pair.PairId, out var pos))
                {
                    decimal newK = pos.KalshiContracts + additional;
                    _openPositions[pair.PairId] = pos with
                    {
                        KalshiContracts  = newK,
                        HardVenShares    = pos.HardVenShares + additional,
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
                // Keep the exposure reservation aligned with the contracts the hedge added.
                lock (_exposureLock) { _totalExposure += additional * currentKalshiAsk; }
                await JournalAsync(JsonSerializer.Serialize(new {
                    t = DateTime.UtcNow, @event = "CLEANUP_HEDGE_COMPLETED", execId,
                    pair = pair.PairId, leg = "kalshi", hedgedQty = additional, kFillPrice = Math.Round(currentKalshiAsk, 6)
                }));
                Console.ForegroundColor = ConsoleColor.Green;
                Emit(execLog, $"[RECOVER OK] {pair.Label} | hedged +{additional} on Kalshi (Pinnacle held)");
                Console.ResetColor();
            }
            else
                Emit(execLog, $"[RECOVER] {pair.Label} | Kalshi hedge filled 0 — holding the excess Pinnacle");

            // Anything still unhedged (Kalshi hedge shortfall + the sub-1-share remainder) is HELD to settlement.
            decimal stillUnhedged = pUnhedged - additional;
            if (stillUnhedged > 0m)
                return await HoldHardVenAsync(stillUnhedged, additional > 0m ? "PARTIAL_HEDGE_REMAINDER" : "KALSHI_HEDGE_FILLED_0");

            return new RecoveryResult("HEDGE_COMPLETED", additional, 0);
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
                "Time,PairId,Label,ArbType,KalshiTicker,HardVenToken," +
                "KPriceCents,KLegAsk,PLegAsk,NetCost," +
                "KFilled,PFilled,PAvgPrice,KStatus,DryRun");
        }
        string row = string.Join(",",
            t.ToString("yyyy-MM-dd HH:mm:ss.fff"),
            Quote(pair.PairId),
            Quote(pair.Label),
            arbType,
            pair.KalshiTicker,
            Quote(arbType == "K_YES_P_NO" ? pair.HardVenNoTokenId : pair.HardVenYesTokenId),
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

    private void OnHardVenOrderRetry(PolyOrderRetryInfo r) => _ = JournalHardVenOrderRetryAsync(r);

    private async Task JournalHardVenOrderRetryAsync(PolyOrderRetryInfo r)
    {
        try
        {
            await JournalAsync(JsonSerializer.Serialize(new {
                t = DateTime.UtcNow, @event = "HARDVEN_ORDER_RETRY",
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

                // Fast-path: no Kalshi position → can't have an unhedged HardVen position.
                // Skip the 2 eth_calls — this avoids 2000+ RPC calls on a clean start.
                if (kPos == 0)
                {
                    report.Add(new ReconciliationEntry(pair.PairId, pair.Label, "CLEAN", 0, 0, ""));
                    continue;
                }

                decimal hardvenYes = await _hardven.GetTokenBalanceAsync(pair.HardVenYesTokenId);
                decimal hardvenNo  = await _hardven.GetTokenBalanceAsync(pair.HardVenNoTokenId);

                decimal kQty   = Math.Abs(kPos);
                decimal pQty   = Math.Max(hardvenYes, hardvenNo);
                string  status = (kQty == 0 && pQty == 0) ? "CLEAN"
                               : (kQty > 0 && pQty > 0)   ? "MATCHED_POSITION"
                               : (kQty > 0)                ? "UNHEDGED_KALSHI"
                               :                             "UNHEDGED_HARDVEN";
                string notes = $"K={kPos} hardvenYes={hardvenYes:0.00} hardvenNo={hardvenNo:0.00}";
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
        bool checkHardVen = _restVerifier != null;
        Console.WriteLine($"[VALIDATE] Checking {pairList.Count} pair(s) — Kalshi status + {(checkHardVen ? "HardVen tokens" : "Kalshi only")}...");
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

            // ── HardVen (both tokens) ─────────────────────────────────────
            // TODO: re-enable once CheckHardVenTokenAsync is verified against negRisk markets.
            // The tick_size check was incorrectly blocking valid active pairs (negRisk tokens
            // may not return tick_size on the /book endpoint).
            // if (blockReason == null && checkHardVen)
            // {
            //     bool yesOk = await _restVerifier!.CheckHardVenTokenAsync(pair.HardVenYesTokenId);
            //     bool noOk  = await _restVerifier!.CheckHardVenTokenAsync(pair.HardVenNoTokenId);
            //     if (!yesOk || !noOk)
            //         blockReason = !yesOk && !noOk ? "HardVen YES+NO tokens invalid"
            //                     : !yesOk          ? "HardVen YES token invalid"
            //                     :                   "HardVen NO token invalid";
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
            Console.WriteLine($"[RECONCILE] {e.Label} | {e.Status} | K={e.KalshiQty} P={e.HardVenQty} | {e.Notes}");
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
        var hedgeHardVen   = new Dictionary<string, decimal>();
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
                        if (fills.TryGetProperty("hardven", out var pf) &&
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
                    if (leg == "hardven" &&
                        root.TryGetProperty("hardvenFillPrice", out var pfp2) && pfp2.ValueKind == JsonValueKind.Number)
                        hedgeHardVen[pairId] = pfp2.GetDecimal();
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
            if (e.PEntry == 0m && hedgeHardVen.TryGetValue(pid, out var pp))
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

            string hardvenToken = entry.ArbType == "K_NO_P_YES" ? pair.HardVenYesTokenId : pair.HardVenNoTokenId;
            decimal pQty = 0m;
            try   { pQty = await _hardven.GetTokenBalanceAsync(hardvenToken); }
            catch (Exception ex)
            {
                Console.WriteLine($"[RESTORE] {pair.Label} | HardVen balance failed: {ex.Message}");
                continue;
            }

            if (pQty < MinRestoreHardVenShares)
            {
                Console.ForegroundColor = ConsoleColor.Yellow;
                Console.WriteLine($"[RESTORE] {pair.Label} | HardVen balance {pQty:0.000} below dust floor — treating as closed, skipping");
                Console.ResetColor();
                continue;
            }

            // Orphan guard: Kalshi leg gone but HardVen still held — legs decoupled (settlement/void closed
            // only the Kalshi side). Do not restore a phantom position.
            if (kQty == 0m)
            {
                Console.ForegroundColor = ConsoleColor.Yellow;
                Console.WriteLine($"[RESTORE] {pair.Label} | ORPHANED HardVen {pQty:0.00} with no Kalshi — needs manual review / reverse, not restore");
                Console.ResetColor();
                await JournalAsync(JsonSerializer.Serialize(new {
                    t = DateTime.UtcNow, @event = "RESTORE_ORPHANED_HARDVEN",
                    pairId, label = pair.Label, pShares = pQty, hardvenToken
                }));
                _orphanedPairs[pairId] = 0;   // block re-entry — the stranded HardVen would collide with a fresh fill
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

    /// <summary>Pre-trade HardVen balance snapshot. Retries once; returns null (inconclusive) rather
    /// than a fabricated 0 if the read fails, so reconcile won't false-halt against a fake prior.</summary>
    private async Task<decimal?> SnapshotHardVenBalanceAsync(string hardvenToken)
    {
        for (int attempt = 1; attempt <= 2; attempt++)
        {
            try { return await _hardven.GetTokenBalanceAsync(hardvenToken); }
            catch (Exception ex)
            {
                DebugLog.Trades($"SnapshotHardVenBalanceAsync attempt {attempt}/2 failed: {ex.Message}");
                if (attempt < 2) await Task.Delay(500);
            }
        }
        return null;   // inconclusive — caller must NOT treat as a real zero balance
    }

    private async Task ReconcileTradeAsync(CrossPair pair, string arbType, decimal expectedKalshi, decimal expectedHardVen, string execId = "", string kOrderId = "", decimal? priorHardVenBalance = null, decimal reversedKalshiQty = 0m, bool hardvenOverfillReversed = false)
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
            string hardvenTokenId = arbType == "K_YES_P_NO" ? pair.HardVenNoTokenId : pair.HardVenYesTokenId;
            // HardVen balance API has the same propagation lag as Kalshi's positions endpoint (often minutes).
            // priorValid=false means the pre-trade snapshot failed -> no baseline -> Measured() falls back
            // to the absolute read. Either way, a HardVen read BELOW the confirmed fill is treated as lag and
            // never halts (see below); only an over-read or a Kalshi order-poll mismatch can halt.
            bool    priorValid = priorHardVenBalance.HasValue;
            decimal prior      = priorHardVenBalance ?? 0m;
            decimal hardvenBal    = prior;
            // Measured HardVen position attributable to THIS trade:
            //   * prior-valid, normal       -> delta vs pre-trade snapshot (so orphaned prior-run shares don't count)
            //   * prior-valid, overfill-rev  -> absolute (an in-trade overfill->reversal races & poisons the snapshot/delta)
            //   * no prior snapshot          -> absolute (no trusted baseline to delta against)
            // Retry until it reaches at least half of expected, to let HardVen's data-API indexing lag clear.
            decimal Measured() => (priorValid && !hardvenOverfillReversed) ? hardvenBal - prior : hardvenBal;
            for (int pAttempt = 1; pAttempt <= maxAttempts; pAttempt++)
            {
                hardvenBal = await _hardven.GetTokenBalanceAsync(hardvenTokenId);
                if (Measured() >= expectedHardVen * 0.5m) break;
                if (pAttempt < maxAttempts) await Task.Delay(retryDelayMs);
                DebugLog.Trades($"ReconcileTradeAsync {pair.Label}: HardVen attempt {pAttempt}/{maxAttempts} measured={Measured():0.00} hardvenBal={hardvenBal:0.00} (expected ~{expectedHardVen:0.00}, priorValid={priorValid}) - retrying");
            }
            decimal hardvenDelta    = hardvenBal - prior;
            decimal hardvenMeasured = Measured();
            bool    kMismatch    = Math.Abs(kActual - expectedKalshi) > 0.5m;
            // HardVen direction matters. An UNDER-read (measured < expected) right after a CONFIRMED
            // execution-time fill is almost always HardVen data-API indexing lag -- which can run
            // minutes, well past our ~30s retry window -- NOT a real naked leg. The fill already
            // confirmed the position, so never halt on it: journal inconclusive and keep trading.
            // Only an OVER-read (measured > expected) means real un-reversed excess exposure -> halt.
            // (priorValid uses the delta so orphaned prior-run shares can't masquerade as an over-read.)
            bool    pUnder = hardvenMeasured < expectedHardVen - 0.5m;
            bool    pOver  = hardvenMeasured > expectedHardVen + 0.5m;
            if (pUnder)
            {
                Console.ForegroundColor = ConsoleColor.Yellow;
                Console.WriteLine(
                    $"[RECONCILE INCONCLUSIVE] {pair.Label} | HardVen venue reads {hardvenMeasured:0.00} < expected {expectedHardVen:0.00} " +
                    $"after a confirmed fill -- suspected data-API lag, NOT halting (position trusted from fill).");
                Console.ResetColor();
                await JournalAsync(JsonSerializer.Serialize(new {
                    t = DateTime.UtcNow, @event = "RECONCILE_INCONCLUSIVE", execId,
                    pair = pair.PairId, arbType,
                    kExpected = expectedKalshi, kVenue = kActual,
                    pExpected = expectedHardVen, pVenue = hardvenBal, pMeasured = hardvenMeasured,
                    pPriorValid = priorValid, hardvenOverfillReversed,
                    reason = "HARDVEN_UNDER_READ_SUSPECTED_LAG", halted = false
                }));
            }
            if (kMismatch || pOver)
            {
                _halted = true;
                string cause = kMismatch && pOver ? "Kalshi mismatch + HardVen over-read"
                             : kMismatch          ? "Kalshi mismatch"
                                                  : "HardVen over-read";
                DiscordAlert($"🚨 HARD HALT — reconcile mismatch on {pair.Label} ({cause}): K local={expectedKalshi} venue={kActual} | P local={expectedHardVen:0.00} venue={hardvenBal:0.00}. Manual position check + reset required.");
                Console.ForegroundColor = ConsoleColor.Red;
                Console.WriteLine(
                    $"[RECONCILE ALERT] {pair.Label} | {cause} | " +
                    $"K: local={expectedKalshi} venue={kActual} | " +
                    $"H: local={expectedHardVen:0.00} venue={hardvenBal:0.00} measured={hardvenMeasured:0.00}");
                Console.WriteLine("[RECONCILE ALERT] Bot halted -- manual reset required. Verify positions before resuming.");
                Console.ResetColor();
                await JournalAsync(JsonSerializer.Serialize(new {
                    t = DateTime.UtcNow, @event = "RECONCILE_MISMATCH", execId,
                    pair = pair.PairId, arbType,
                    kExpected = expectedKalshi, kVenue = kActual,
                    pExpected = expectedHardVen,   pVenue = hardvenBal,
                    pPrior = priorValid ? (object?)prior : null, pPriorValid = priorValid, pDelta = hardvenDelta,
                    pMeasured = hardvenMeasured, hardvenOverfillReversed,
                    kHalt = kMismatch, pOverHalt = pOver, halted = true
                }));
            }
            else if (!pUnder)
                DebugLog.Trades($"ReconcileTradeAsync {pair.Label}: confirmed K={kActual} P_measured={hardvenMeasured:0.00} (hardvenBal={hardvenBal:0.00} prior={prior:0.00} priorValid={priorValid})");
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

    public async Task<bool> PingHardVenAsync()
    {
        try { await _hardven.GetUsdcBalanceAsync(); return true; }
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
                ? $"H:{pair.HardVenNoTokenId}"
                : $"H:{pair.HardVenYesTokenId}";

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
                                        + pos.HardVenShares      * pos.HardVenEntryPrice;
                    lock (_exposureLock) { _totalExposure = Math.Max(0m, _totalExposure - settledCost); }
                    Console.ForegroundColor = ConsoleColor.Cyan;
                    Console.WriteLine($"[SETTLEMENT] {pair.Label} | position settled — removed from tracking");
                    Console.ResetColor();
                    await JournalAsync(JsonSerializer.Serialize(new {
                        t            = DateTime.UtcNow, @event = "SETTLEMENT_DETECTED",
                        execId       = pos.ExecId, pairId, label = pair.Label,
                        kContracts   = pos.KalshiContracts, kEntryPrice = pos.KalshiEntryPrice,
                        pShares      = pos.HardVenShares,      pEntryPrice = pos.HardVenEntryPrice,
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
        else // hardven
        {
            var (sold, price) = await PlaceHardVenSellAsync(pr.HardVenToken, pr.Qty, pr.NegRisk);
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
        // Early exit sells BOTH legs to close before settlement — but the Pinnacle leg is irreversible, so this
        // is OFF by default (hold-to-settlement model). Both entry points funnel here, so this one gate covers
        // the monitor loop and the book-update path.
        if (!_earlyExitEnabled) return;
        bool breakEvenMode = _minBuy;
        if (!breakEvenMode && EarlyExitThreshold <= 0m) return;

        var pair = _telemetry.GetPair(pairId);
        if (pair == null) return;

        string kBidKey    = pos.ArbType == "K_YES_P_NO" ? $"K:{pair.KalshiTicker}" : $"K:{pair.KalshiTicker}_NO";
        string pBidKey    = pos.ArbType == "K_YES_P_NO" ? $"H:{pair.HardVenNoTokenId}" : $"H:{pair.HardVenYesTokenId}";
        string kalshiSide = pos.ArbType == "K_YES_P_NO" ? "yes" : "no";
        string hardvenToken  = pos.ArbType == "K_YES_P_NO" ? pair.HardVenNoTokenId : pair.HardVenYesTokenId;

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
            var (pVwap, pFill) = pBook!.GetExecutableBidVwap(pos.HardVenShares);
            if (kFill < pos.KalshiContracts - 0.5m || pFill < pos.HardVenShares - 0.5m)
            {
                DebugLog.Trades($"CheckEarlyExitAsync {pair.Label}: thin exit depth — " +
                    $"kFill={kFill:0.##}/{pos.KalshiContracts:0.##} pFill={pFill:0.##}/{pos.HardVenShares:0.##} — holding");
                return;
            }
            if (kVwap > 0m) kBid = kVwap;
            if (pVwap > 0m) pBid = pVwap;
        }

        decimal entryCostPerSet      = pos.KalshiEntryPrice + pos.HardVenEntryPrice;
        decimal expectedProfitPerSet = 1.0m - entryCostPerSet
            - KalshiFee(pos.KalshiEntryPrice) - HardVenFee(pos.HardVenEntryPrice, hardvenToken);
        if (expectedProfitPerSet <= 0m) return;

        decimal exitFeesPerSet      = KalshiFee(kBid) + HardVenFee(pBid, hardvenToken);
        decimal entryFeesPerSet     = KalshiFee(pos.KalshiEntryPrice) + HardVenFee(pos.HardVenEntryPrice, hardvenToken);
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
                kalshiContracts = currentPos.KalshiContracts, hardvenShares = currentPos.HardVenShares,
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
                                + currentPos.HardVenShares      * currentPos.HardVenEntryPrice;
                lock (_exposureLock) { _totalExposure = Math.Max(0m, _totalExposure - dryCost); }
                lock (_balanceLock)  { _kalshiBalanceUsd += currentPos.KalshiContracts * kBid;
                                       _hardvenBalanceUsd   += currentPos.HardVenShares      * pBid; }
                Console.ForegroundColor = ConsoleColor.Cyan;
                Console.WriteLine($"[EARLY EXIT DRY-RUN] {pair.Label} | position closed (simulated)");
                Console.ResetColor();
                await JournalAsync(JsonSerializer.Serialize(new {
                    t = DateTime.UtcNow, @event = "EARLY_EXIT_COMPLETE", execId = currentPos.ExecId,
                    pairId, outcome = "DRY_RUN",
                    kSold = currentPos.KalshiContracts, pSold = currentPos.HardVenShares,
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
            var pSellTask = PlaceHardVenSellAsync(hardvenToken, currentPos.HardVenShares, pair.IsNegRisk);
            await Task.WhenAll(kSellTask, pSellTask);

            var (_, kStatus, kSold) = kSellTask.Result;
            var (pSold, pAvgPrice)  = pSellTask.Result;

            bool kOk = kSold >= currentPos.KalshiContracts - 0.5m;
            bool pOk = pSold >= currentPos.HardVenShares      - 0.5m;

            // HardVen is out but Kalshi partially filled: sweep 3 ticks deeper to close the unhedged leg.
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
                                      + currentPos.HardVenShares      * currentPos.HardVenEntryPrice;
                lock (_exposureLock) { _totalExposure = Math.Max(0m, _totalExposure - exitEntryCost); }
                decimal kProceeds   = kSold * (kSellCents / 100m);
                decimal pProceeds   = pSold * pAvgPrice;
                decimal kExitFee    = KalshiFee(kSellCents / 100m) * kSold;
                decimal pExitFee    = HardVenFee(pAvgPrice, hardvenToken) * pSold;
                decimal kEntryFee   = KalshiFee(currentPos.KalshiEntryPrice) * currentPos.KalshiContracts;
                decimal pEntryFee   = HardVenFee(currentPos.HardVenEntryPrice, hardvenToken) * currentPos.HardVenShares;
                decimal realizedPnl = (kProceeds + pProceeds)
                    - (kExitFee + pExitFee)
                    - (currentPos.KalshiContracts * currentPos.KalshiEntryPrice
                    +  currentPos.HardVenShares      * currentPos.HardVenEntryPrice)
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
                if (!pOk) { nakedLeg = "hardven";   nakedQty = currentPos.HardVenShares      - pSold; nakedEntry = currentPos.HardVenEntryPrice;  }
                else      { nakedLeg = "kalshi"; nakedQty = currentPos.KalshiContracts - kSold; nakedEntry = currentPos.KalshiEntryPrice; }

                _openPositions.TryRemove(pairId, out _);
                decimal partialEntryCost = currentPos.KalshiContracts * currentPos.KalshiEntryPrice
                                         + currentPos.HardVenShares      * currentPos.HardVenEntryPrice;
                lock (_exposureLock) { _totalExposure = Math.Max(0m, _totalExposure - partialEntryCost); }
                _orphanedPairs[pairId] = 0;

                string kBookKey = pos.ArbType == "K_YES_P_NO" ? $"K:{pair.KalshiTicker}" : $"K:{pair.KalshiTicker}_NO";
                var pr = new PendingReversal(pair, nakedLeg, kalshiSide, kBookKey, hardvenToken,
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
