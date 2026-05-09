# PredictionBacktester TODOs

# Cross-Venue Arb Bot — Execution Edge Case Checklist

## Pre-Build Foundation

- [X] Persistent trade journal on disk (every trade intent written before order is sent)
- [X] Position reconciliation function that queries both venues for ground-truth positions
- [X] Reconcile-on-startup: on every bot start, sync local state to venue state before trading
- [X] Side mapping derived from validated pair record only — never inferred from titles/rules at runtime
- [X] Per-trade max loss tripwire (e.g., 3x expected edge) → auto-halt
- [X] Per-day max loss tripwire (e.g., 10% of daily target) → auto-halt
- [X] Auto-halt requires manual reset to resume

## Order Placement

- [X] All orders are limit orders, never market orders
- [X] Limit price = the price your model evaluated, not a few cents better
- [X] Both legs fire simultaneously (Kalshi IOC + Poly FAK)
- [X] Position recorded at min(kFilled, pFilled) — balanced hedged quantity only
- [~] Client-side order IDs for idempotency — covered by _inFlight guard + journal + reconcile-on-startup
- [X] Capital reservation through single capital manager before any order is sent
- [X] If capital reservation fails, trade is skipped (no order sent)

## Failure Detection

- [X] Neither leg fills → no exposure, log and continue
- [X] After fills, compute balanced qty and flag any unhedged delta
- [X] Unhedged delta > 0 → trigger hedge-or-reverse for the excess
- [X] Cancel-fill race condition handled (sleep + reconcile, treat local state as hint only)
- [x] Connection loss to either venue → halt new trades
- [X] Watchdog heartbeat that triggers halt if both venues unreachable >N seconds
- [N/A] Detect "filled but at unexpected price" — impossible with FAK/IOC limit orders; venue enforces price ceiling

## Hedge-or-Reverse Decision Logic

- [X] Function exists and is the single entry point for all unhedged delta recovery
- [N/A] Inputs: time elapsed since fill — no retry loop; hedge-or-reverse is immediate and price-based
- [X] Re-snapshot the opposite venue before deciding (don't trust stale data)
- [X] If completing the hedge at current price still preserves positive edge → retry fill
- [X] If completing the hedge guarantees more loss than reversing → reverse the excess
- [X] Time-bounded retry (don't loop forever)
- [X] Reverse uses higher slippage tolerance than original entry
- [X] If reverse also fails → escalate to alert + halt
- [X] Reverse buffer is a tunable parameter (start ~1-2¢)

## Cleanup Trades (Imbalance Fixes)

- [X] Imbalance smaller than fee-to-edge threshold → reverse rather than chase hedge (skip hedge if value < $1.00)
- [X] Cleanup trade also uses IOC + limit (Kalshi sell = IOC, Poly sell = FAK — already true)
- [X] Cleanup failure escalates (dust < $0.25 absorbed silently; larger positions still halt)
- [X] Track cumulative cleanup cost separately from main P&L (TotalCleanupCostUsd in status dashboard)

## Post-Trade Reconciliation

- [X] After every trade, compare local position state to venue state (ReconcileTradeAsync, fire-and-forget after fill)
- [X] Mismatch logs are alerts, not silent corrections ([RECONCILE ALERT] printed red + journaled as RECONCILE_MISMATCH)
- [X] Daily reconciliation report: modeled fees vs net cost variance per trade (DAILY_REPORT journal entry on day rollover)
- [X] Fee model drift >10% triggers fee model audit ([FEE MODEL DRIFT] warning when |netVar|/modeledFees > 10%)

## Settlement & Post-Mortem

- [X] On settlement, compute expected payout (shares × $1.00) vs actual payout
- [X] Categorize each settled position:
    - [X] PAIR_MISMATCH_BOTH_LOST (both legs lost — pair was wrong)
    - [X] PAIR_MISMATCH_BOTH_WON (both legs won — pair was wrong)
    - [X] EXECUTION_LOSS (pair was right, slippage/fees ate edge)
    - [X] FEE_MODEL_LOSS (pair right, execution right, fees higher than modeled)
    - [X] CLEAN_WIN (everything as expected)
- [X] Pair-mismatch cases automatically blocklist the pair from future trades
- [X] Execution-loss cases trigger review of execution code
- [X] Fee-model-loss cases trigger fee model update
- [X] Per-pair position limits so a single bad pair can't blow up bankroll

## Edge Cases to Specifically Handle

- [X] Self-trade prevention: tag every order with strategy ID
- [X] Stale price between detection and arrival: log when limit doesn't fill
- [X] Time-skew between venues: log timestamp drift, alert if >500ms
- [X] Venue maintenance windows: detect via repeated REST failures, halt trading on that venue
- [X] API rate limits: rate limiter on outbound requests, never let limit-hit cause leg-fail
- [X] Daylight savings / timezone bugs in settlement timing comparisons

## Pre-Live Validation

- [ ] Replay last week of telemetry data against the execution bot in dry-run mode
- [ ] Inject simulated failures (5% partial fill, 1% leg-fail, 2% stale price)
- [ ] Compare simulated P&L to predicted P&L — gap should be <50%
- [ ] If gap >50%, hedging logic needs work before going live
- [ ] Test halt-and-reset path manually
- [ ] Test reconcile-on-restart path: kill bot mid-trade, restart, verify state recovery
- [ ] Test connection-loss path: block one venue's network, verify clean shutdown

## Live Deployment Order

- [ ] Phase 1: dry-run mode for 2-3 days (logs trades it would make, doesn't send)
- [ ] Compare dry-run logs against production sim predictions for sanity
- [ ] Phase 2: live with absolute minimum size (1-5 contracts per arb)
- [ ] Run minimum-size for 1 week minimum, regardless of how good results look
- [ ] Compare realized P&L per trade against expected P&L per trade
- [ ] Phase 3: scale up only after realized matches expected within tolerance
- [ ] Scale-up is gradual (2x, then 2x again, etc.) — never 10x in one step

## Monitoring (Once Live)

- [ ] Real-time dashboard: open positions, P&L today, halt status
- [ ] Alert on: any halt trigger, any unhedged position older than N seconds, any reconciliation mismatch
- [ ] Daily summary: trades attempted, filled, partial, failed, reversed
- [ ] Weekly review: realized vs simulated P&L, fee drift, pair-mismatch incidents
- [ ] Monthly: full audit of execution code against this checklist
