# PredictionBacktester TODOs

# Cross-Venue Arb Bot — Execution Edge Case Checklist

## Pre-Build Foundation

- [ ] Persistent trade journal on disk (every trade intent written before order is sent)
- [ ] Position reconciliation function that queries both venues for ground-truth positions
- [ ] Reconcile-on-startup: on every bot start, sync local state to venue state before trading
- [ ] Side mapping derived from validated pair record only — never inferred from titles/rules at runtime
- [ ] Per-trade max loss tripwire (e.g., 3x expected edge) → auto-halt
- [ ] Per-day max loss tripwire (e.g., 10% of daily target) → auto-halt
- [ ] Auto-halt requires manual reset to resume

## Order Placement

- [ ] All orders are limit orders, never market orders
- [ ] Limit price = the price your model evaluated, not a few cents better
- [ ] Smaller-depth leg sent first (based on pre-trade book snapshot)
- [ ] First leg uses IOC (Immediate-Or-Cancel) — no partial sits
- [ ] Wait for first leg fill confirmation before sizing second leg
- [ ] Second leg quantity = actual first-leg fill quantity, NOT intended quantity
- [ ] Client-side order IDs for idempotency (sending same trade twice is safe)
- [ ] Capital reservation through single capital manager before any order is sent
- [ ] If capital reservation fails, trade is skipped silently (no order sent)

## Failure Detection

- [ ] First leg fully fails → no exposure, log and continue
- [ ] First leg partial-fills → size second leg to actual fill
- [ ] First leg fully fills, second leg fails → trigger hedge-or-reverse decision
- [ ] First leg fully fills, second leg partial-fills → reverse the unhedged delta
- [ ] Both legs partial-fill at different rates → reverse the imbalanced portion
- [ ] Cancel-fill race condition handled (sleep + reconcile, treat local state as hint only)
- [ ] Connection loss to either venue → halt new trades, attempt close-out via REST
- [ ] Watchdog heartbeat that triggers halt if both venues unreachable >N seconds
- [ ] Detect "filled but at unexpected price" (slippage beyond limit tolerance)

## Hedge-or-Reverse Decision Logic

- [ ] Function exists and is the single entry point for all failure recovery
- [ ] Inputs: first leg state, current second-venue quote, time elapsed since first fill
- [ ] Re-snapshot the second venue before deciding (don't trust stale data)
- [ ] If hedging at current price preserves positive edge minus reverse buffer → retry hedge
- [ ] If hedging guarantees more loss than reversing → reverse first leg
- [ ] Time-bounded retry (don't loop forever)
- [ ] Reverse uses higher slippage tolerance than original entry
- [ ] If reverse also fails → escalate to alert + halt
- [ ] Reverse buffer is a tunable parameter (start ~1-2¢)

## Cleanup Trades (Imbalance Fixes)

- [ ] Imbalance smaller than fee-to-edge threshold → reverse rather than chase hedge
- [ ] Cleanup trade also uses IOC + limit
- [ ] Cleanup failure escalates (don't infinite-retry on small dust)
- [ ] Track cumulative cleanup cost separately from main P&L

## Post-Trade Reconciliation

- [ ] After every trade, compare local position state to venue state
- [ ] Mismatch logs are alerts, not silent corrections
- [ ] Daily reconciliation report: realized fees vs. modeled fees per trade
- [ ] Fee model drift >10% triggers fee model audit

## Settlement & Post-Mortem

- [ ] On settlement, compute expected payout (shares × $1.00) vs actual payout
- [ ] Categorize each settled position:
    - [ ] PAIR_MISMATCH_BOTH_LOST (both legs lost — pair was wrong)
    - [ ] PAIR_MISMATCH_BOTH_WON (both legs won — pair was wrong)
    - [ ] EXECUTION_LOSS (pair was right, slippage/fees ate edge)
    - [ ] FEE_MODEL_LOSS (pair right, execution right, fees higher than modeled)
    - [ ] CLEAN_WIN (everything as expected)
- [ ] Pair-mismatch cases automatically blocklist the pair from future trades
- [ ] Execution-loss cases trigger review of execution code
- [ ] Fee-model-loss cases trigger fee model update
- [ ] Per-pair position limits so a single bad pair can't blow up bankroll

## Edge Cases to Specifically Handle

- [ ] Self-trade prevention: tag every order with strategy ID
- [ ] Stale price between detection and arrival: limit price protects you, but log when limits don't fill
- [ ] Order book moves resting order out of queue: only relevant if you have non-IOC resting orders
- [ ] Time-skew between venues: log timestamp drift, alert if >500ms
- [ ] Venue maintenance windows: detect via repeated REST failures, halt trading on that venue
- [ ] API rate limits: rate limiter on outbound requests, never let limit-hit cause leg-fail
- [ ] Daylight savings / timezone bugs in settlement timing comparisons

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