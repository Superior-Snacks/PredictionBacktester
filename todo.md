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

## Known Bugs — Fix Before First Dry-Run

These were found by static audit; none require a live run to trigger.

- [X] **Dry-run cooldown missing**: `_cooldownUntil[pairId]` is only set on the live path (after the dry-run early return). The same pair can re-execute on every scan cycle with no throttle — floods the journal. Fix: set `_cooldownUntil[pairId] = now + _pairCooldownSeconds` at the top of the dry-run block.
- [X] **Dry-run bypasses per-pair exposure limit**: `_perPairInvested` is only incremented on the live path. Dry-run can simulate unlimited exposure to a single pair. Fix: mirror the per-pair update inside the dry-run block after `simKFilled > 0`.
- [X] **Dry-run bypasses total exposure limit**: `_totalExposure` is only incremented on the live path. Fix: same — add `_totalExposure += estimatedCost` inside the dry-run block (inside `_exposureLock`).
- [X] **Dry-run bypasses blocklist check**: Blocklist check is after the dry-run return, so blocked pairs get journaled as valid dry-run trades. Fix: move the blocklist check (and the balance restore on block) to before the `if (_dryRun)` block, or add a redundant check inside it.
- [X] **Journal directory not created on startup**: `_journalPath` is a bare filename (no directory), relying on the working directory being writable. `File.AppendAllTextAsync` will throw if the parent directory doesn't exist. Verify working directory at startup or use `Directory.CreateDirectory` before first write.
- [ ] **`PolyFee` formula needs empirical verification**: Formula is `0.04 × p² × (1−p)` — non-standard. At p=0.50 this yields $0.005/share vs Polymarket's ~$0.02/share actual fee on a resolving YES. Verify against real fee receipts before production or the edge model is optimistic.

## Verification Checklist — First Dry-Run Session

Run through these during/after the first dry-run to confirm correct behavior.

- [ ] **Populate `cross_pairs.json` first** — currently empty `[]`; executor idles forever with no pairs and logs nothing. Run `pair_markets.py` or manually add at least one verified pair.
- [ ] **Journal file is created and parseable**: After first dry-run trade fires, confirm `CrossArbJournal_*.jsonl` exists in the working directory and contains valid JSON lines with `"event":"INTENT"` and `"event":"EXECUTION_COMPLETE"`.
- [ ] **`prod_cross_arb.py` parses dry-run journal**: Run `python KalshiPolyCross/prod_cross_arb.py --no-api` against the dry-run journal. Confirm it shows non-zero event counts rather than "0 events found".
- [ ] **`dryRun: true` visible in raw journal**: Grep the JSONL for `"dryRun":true` — confirm all dry-run records are tagged so they can be distinguished from live records if journals are ever merged.
- [ ] **Balance depletes correctly**: Watch console balance-after values across trades; should decrease monotonically as simulated cost accumulates from $1,000 seed.
- [ ] **Cooldown respected between pairs** (after bug fix above): Same pair should not fire twice within `_pairCooldownSeconds`.
- [ ] **TIME_SKEW events fire on skewed books**: If any journal entry shows `"event":"TIME_SKEW"`, verify the `venueSkewMs` field is present and >500.
- [ ] **Halt path reachable**: Manually block one venue's REST endpoint (firewall rule or kill network) and confirm `[MAINTENANCE]` appears in console after 5 failures and `VENUE_MAINTENANCE` is journaled. Confirm new trades stop.
- [ ] **`prod_cross_arb.py` settlement section handles dry-run**: Confirm the settlement/blocklist section of the analyzer doesn't crash on `"status":"simulated"` in the fills object.
- [ ] **429 retry path**: Confirm that if Kalshi returns 429, the log shows `[KALSHI RATE LIMIT]` and the retry fires 1s later (not an immediate leg-fail).

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
