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


# CrossArbExecutor — Pre-Live Fix List

Generated from code review of `CrossArbExecutor.cs` and supporting files.
Line numbers reference the version of the file reviewed; verify against current code before editing.

---

## CRITICAL (fix before any live trading)

### [X] 1. Reconciliation mismatch should halt, not just log
**Location:** `ReconcileTradeAsync` (line ~1485)
**Problem:** When venue position differs from local `_openPositions` state, you log an alert and continue. Local state stays wrong, and every subsequent `_openPositions` check uses the wrong number. The fire-and-forget `Task.Run` wrapper also means any exception in the reconciliation lambda is silently lost.
**Fix:** On any meaningful mismatch (`kMismatch || pMismatch`), set `_halted = true` and require manual intervention. Wrap the lambda body in try/catch and journal exceptions so reconciliation errors don't disappear silently.
**Why critical:** This is the one issue that can compound losses silently. If the bot thinks it holds 5 contracts but actually holds 10, it'll mis-size the next 50 trades before you notice.

### [X] 2. Dust absorption check should come before reverse attempts
**Location:** `RecoverUnhedgedAsync`, both Case A and Case B (lines ~1242, ~1337)
**Problem:** A $0.20 unhedged position attempts reverse first (paying fees often larger than $0.20), then falls through to dust absorption only if the reverse fails. You're guaranteed to lose fees on tiny positions that should have been absorbed immediately.
**Fix:** Restructure the recovery flow so dust absorption is the *first* check:
```
if (unhedgedValue < CleanupDustUsd) → absorb, journal, return
else if (skipHedge) → reverse directly
else → try hedge, fall back to reverse on failure
```
**Why critical:** Without this, you're systematically losing money on every small partial-fill cleanup.

### [X] 3. Use Math.Ceiling for hedge limit prices, Math.Floor for reverse limit prices
**Location:** `RecoverUnhedgedAsync` Case B (line ~1280)
**Problem:** Current code uses `Math.Round` for hedge bid prices. At half-cent boundaries this can round *down*, producing a bid below the current ask — which won't fill. Your reverse code correctly uses `Math.Floor` already; the hedge path needs the symmetric `Math.Ceiling`.
**Fix:**
```csharp
int currentKCents = Math.Max(1, (int)Math.Ceiling(currentKalshiAsk * 100));
```
**Why critical:** Will cause silent hedge failures intermittently. Cheap to fix preemptively.

### [X] 4. Thread safety in `_tokens.AddRange` (PolymarketWebsocketFeed)
**Location:** `PolymarketWebsocketFeed.EnqueueSubscribe`
**Problem:** `_tokens` is a plain `List<string>` being mutated by `EnqueueSubscribe` while the reconnect loop iterates `_tokens` to resubscribe. Concurrent mutation will eventually throw `InvalidOperationException` or corrupt the list silently.
**Fix:** Either wrap all `_tokens` access in a lock, switch to `ImmutableList<string>` with atomic replacement, or use a `ConcurrentBag`. Verify the same pattern in `KalshiWebsocketFeed` if it exists there too.
**Why critical:** Will cause reconnect failures and silent token-loss when timing happens to be unlucky. Race conditions are the hardest bugs to diagnose after the fact.

---

## IMPORTANT (fix in the first week of live operation)

### [X] 5. Add recovery hedge slippage tolerance
**Location:** `RecoverUnhedgedAsync` hedge retry paths (Cases A and B)
**Problem:** Recovery hedge retries use the freshly-fetched best ask as the limit price. If the book moves 1¢ between fetch and order arrival, the retry fails for the same reason the original did — leading to unnecessary reverse + loss.
**Fix:** Add a `RecoveryHedgeSlippageCents = 2` constant (parallel to your existing `ReverseBufferCents`) and apply it to hedge retries. The whole point of recovery is being willing to pay slightly worse than entry-time prices to actually fill.
**Why important:** Increases recovery success rate, which is exactly when you most want hedges to succeed.

### [X] 6. Detect empty bid side before sending doomed reverse orders
**Location:** Kalshi reverse path (line ~1213)
**Problem:** If `kBestBid` is 0 (no bids on the book), the reverse limit ends up at 1¢ via the `Math.Max(1, ...)` floor. You send a 1¢ sell that won't fill, pay no fee but waste a request, then fall through.
**Fix:** Check `kBestBid <= 0m` explicitly before posting the reverse order. If no bid side exists, skip directly to the dust/halt branch.
**Why important:** Avoids one wasted REST call and simplifies the post-reverse logic by not having to handle the "reverse posted but didn't fill" subcase separately from the dust case.

### [X] 7. Skip hedge phase (don't halt) when opposite-side book is missing
**Location:** Both halt branches in `RecoverUnhedgedAsync` (lines ~1165, ~1270)
**Problem:** If the opposite-side book is missing from `_books`, you halt immediately. Overkill — you can still reverse the filled side without needing the opposite book.
**Fix:** Missing opposite-side book → skip the hedge attempt, fall through to reverse on the leg you can act on. Halt only if reverse *also* can't proceed.
**Why important:** Reduces unnecessary halts that require manual intervention. The book might be missing for benign reasons (just-added pair, transient state).

### [X] 8. Explicit fractional-to-integer conversion for cross-venue hedges
**Location:** Hedge retry on Case B (line ~1291)
**Problem:** `(int)pUnhedged` truncates silently. If Polymarket filled 7.99 shares, the cast becomes 7 and you leave 0.99 unhedged with no explicit handling. Polymarket supports fractional shares; Kalshi doesn't.
**Fix:**
```csharp
int hedgeQty = (int)Math.Floor(pUnhedged);
if (hedgeQty == 0) {
    // remaining is sub-1-share fractional dust, route to dust absorption
} else {
    var (_, _, kFill2) = await PlaceKalshiLegAsync(..., hedgeQty);
    // any pUnhedged - hedgeQty remainder still needs handling
}
```
**Why important:** Tiny but accumulating leakage. Every cross-venue partial fill on Polymarket can leave sub-share residue that's currently invisible to the recovery logic.

### [X] 9. CSV writer task failure should be detectable
**Location:** Constructor (line ~159) and `RunCsvWriterAsync` (line ~1395)
**Problem:** `_ = Task.Run(RunCsvWriterAsync)` discards the task. If the writer fails on startup (file permissions, disk full) or dies mid-run, you'll silently lose CSV data. The `_csvChannel.Writer.TryWrite(row)` calls continue to succeed because the channel is unbounded — the data just goes nowhere.
**Fix:** Either keep a reference to the writer task and check its status periodically, add a watchdog that verifies the channel reader is alive, or restart the writer on exception. At minimum, the `catch` in `RunCsvWriterAsync` should log loudly (red console) so a writer death is noticed.
**Why important:** Operational silence is dangerous in financial systems. You want to know immediately if your audit trail stops being written.

---

## STRATEGIC (not bugs, but worth deciding explicitly)

### [X] 10. Document or change the "one position per pair" constraint
**Location:** `ExecuteLockedAsync` (line ~278)
**Problem:** `_openPositions.ContainsKey(pairId)` blocks new entries on a pair while *any* position is open. This prevents scaling into a position when prices drop further — the "go deeper on a dip" strategy we discussed previously.
**Decision needed:**
- Keep as-is (one position per pair, hold to settlement/exit) — add a comment explaining this is intentional
- Allow scale-in when current price beats average entry price by some threshold — requires position-averaging logic and likely a per-pair max-investment cap (you have `MaxPerPairExposureUsd = 200` already, but it doesn't gate scale-in)

**Why strategic:** Either is defensible. The first is safer; the second can capture more edge. Worth being explicit about which you've chosen.

### [N/A] 11. Add manual-approval mode for first live week
**Status:** Not present in current code (only `_dryRun` exists)
**Suggestion:** A `--confirm` or `--interactive` flag where the bot finds arbs, prints them, and waits for keyboard input (Y/n) before firing each one. Useful for the first day or two of live trading to catch obvious bugs without risking automated execution at 3am.
**Why strategic:** This isn't a bug, it's a deployment-safety feature. Could save you from a bad first day if there's some failure mode that only appears in live.

### [X] 12. Build settlement post-mortem categorization script
**Status:** Not present in current code
**Suggestion:** A separate script that walks the journal after settlement and categorizes each closed position:
- `CLEAN_WIN` — both legs settled, total payout = $1.00 × shares, profit ≈ expected
- `PAIR_MISMATCH_BOTH_LOST` — both legs paid $0 → pair was wrong
- `PAIR_MISMATCH_BOTH_WON` — both legs paid $1 → pair was wrong
- `EXECUTION_LOSS` — pair right but slippage ate edge
- `FEE_MODEL_LOSS` — pair right, slippage right, fees higher than modeled
- `RECOVERED_*` — went through cleanup, final outcome

Auto-update `cross_pair_blocklist.json` on PAIR_MISMATCH detections.

**Why strategic:** Without this, you'll know *that* trades lost but not *why*. Categorization tells you whether to fix the LLM (pair issues), the execution code (slippage), or the fee model.

### [X] 13. Build early-exit monitoring for open positions
**Status:** Implemented — `RunEarlyExitMonitorAsync` checks every 30s; exits when unrealizedPnl ≥ EarlyExitThreshold (50%) × expectedProfit. EarlyExitThreshold is a mutable static field. Status dashboard shows earlyExit count.

---

## NICE-TO-HAVE (polish, defer until first month of live data)

### [ ] 14. Consider auto-correct vs halt on reconcile mismatch (later)
After running halt-on-mismatch for a while, you'll see the patterns of what mismatches actually look like. Some categories might be safely auto-correctable (e.g., venue eventually-consistent lag). Don't enable auto-correct until you have data on what's actually happening.

### [X] 15. Add a watchdog that periodically pings both venues' REST
If WS feeds go silent for unrelated reasons (network glitch, your end), you want to know whether the venues are actually up or whether you're cut off. A 60-second REST ping to each, comparing to the WS last-message timestamps, would distinguish "venue is quiet" from "we're disconnected."

### [X] 16. Per-trade trace IDs for log correlation
Every order should log a unique trace ID that ties together submit, fill confirmation, any cancel attempts, recovery actions, and reconciliation. Grep-friendly debugging when something weird happens at 3am.

### [ ] 17. Daily summary report at midnight UTC
Trades attempted, filled, partial, reversed, dust-absorbed, halted. Realized vs simulated P&L. Fee model drift. Send to Telegram/email so you see it on your phone.

---

# Dry-Run Enhancement Plan

Goal: exercise as much of the live execution path in dry-run as possible before risking real capital. Current dry-run tests bookkeeping but skips ~80% of the failure-handling code.

---

## Current dry-run coverage

What dry-run already tests:
- Bankroll guards (blocklist, per-pair limit, exposure limit, balance reservation)
- Cooldown management
- Journal events (INTENT, EXECUTION_COMPLETE)
- CSV output
- Try-limit decrement

What dry-run currently does NOT test (the gap to close):
- [X] Fill latency (both legs return instantly in dry-run)
- [X] Slippage (fills always at detected price)
- [X] Partial fills
- [X] Leg failures (full)
- [X] Stale-price scenarios
- [X] Per-trade max-loss tripwire
- [X] Per-day max-loss tripwire
- [X] Fee model drift detection
- [ ] `RecoverUnhedgedAsync` (entire function never called)
- [ ] Hedge retry logic (Cases A and B)
- [ ] Reverse logic (Kalshi and Poly)
- [ ] Dust absorption (< CleanupDustUsd branches)
- [ ] Recovery halt paths (book-missing, reverse-failed)
- [ ] `ReconcileTradeAsync` against simulated mismatches
- [ ] Connection-loss halt (`_connectionHalted`) and resume
- [ ] Maintenance threshold (consecutive REST errors)
- [ ] `ReconcileOnStartupAsync` with simulated pre-existing positions

---

## STAGE 1 — Realistic fill simulation (foundation)

### [X] Build `SimulatedFillProfile` class
Configurable parameters that control how simulated fills behave:
- `FillLatencyMsKalshi` (default 100ms)
- `FillLatencyMsPoly` (default 80ms)
- `KalshiSlippageCents` (cents above limit)
- `PolySlippagePct` (% above limit)
- `PartialFillRate` (0-1 chance of partial)
- `LegFailRate` (0-1 chance of full leg fail)
- `Random Rng` (with optional seed for reproducibility)

### [X] Replace perfect-fill block in current dry-run path
Lines 469-473 in `CrossArbExecutor.cs` currently produce perfect fills. Replace with logic that consults `SimulatedFillProfile` and produces realistic simulated outcomes.

### [X] Add latency simulation
`await Task.Delay((int)profile.FillLatencyMsKalshi)` before returning the simulated fill. Matters for testing time-skew detection and ensuring async ordering behaves correctly.

### [X] Add slippage simulation
Simulated fill price = limit price + slippage (cents for Kalshi, % for Poly). This activates the `[EXEC SLIPPAGE]` branch and the per-trade tripwire when slippage is large enough.

### [X] Add partial-fill simulation
When `Rng.NextDouble() < PartialFillRate`, fill 20-90% of intended quantity. Activates the recovery logic for partial fills (the most common real-world failure mode).

### [X] Add leg-failure simulation
When `Rng.NextDouble() < LegFailRate`, return 0 fill. Activates the "neither leg filled" and "single leg unhedged" recovery paths.

**Pass criteria for Stage 1:** Dry-run produces realistic execution journals with a mix of clean fills, slippage events, partial fills, and full failures. Each `EXECUTION_COMPLETE` event should have plausible values matching what live data would look like.

---

## STAGE 2 — Wire dry-run through full execution path

### [X] Introduce `IKalshiOrderExecutor` and `IPolymarketOrderExecutor` interfaces
Extract the order-placement methods from the concrete clients into interfaces. The real clients implement them; simulated clients implement them differently.

### [X] Build `SimulatedKalshiClient` and `SimulatedPolymarketClient`
Implementations of the interfaces that:
- Apply `SimulatedFillProfile` to determine fill outcomes
- Read current book state from the live `LocalOrderBook` (so simulated fills react to real market state)
- Return simulated order IDs, statuses, and fill counts in the same shape as the real clients
- Update simulated positions internally so `GetPositionsAsync` returns sensible values

### [X] Inject the simulated clients in dry-run mode
At startup, when `--dry-run` is specified, construct the executor with simulated clients instead of real ones. The rest of `ExecuteLockedAsync` runs unchanged.

### [X] Remove the dual `if (_dryRun)` code path
Once dry-run goes through the live execution path, the parallel dry-run block at lines 410-526 becomes unnecessary. Delete it. Dry-run vs live is now a startup-time decision, not an in-flow branch.

**Pass criteria for Stage 2:** A dry-run trade goes through the exact same code as live, including `RecoverUnhedgedAsync` when fills are imbalanced. No code path is exclusive to live mode anymore.

---

## STAGE 3 — Named scenario library

### [X] Build `FailureScenarios` static class with named profiles
Each profile is a preset `SimulatedFillProfile` for a specific failure pattern:
- `HappyPath()` — baseline, no failures
- `FlakyKalshi()` — 20% leg fail rate, 300ms latency
- `FlakyPoly()` — 20% leg fail rate on Poly side
- `ChronicSlippage()` — 1¢ Kalshi slippage, 2% Poly slippage
- `PartialFillSwamp()` — 40% partial fill rate
- `BothVenuesFlaky()` — moderate failures on both sides
- `LatencyStorm()` — high latency, no failures (tests timing logic)

### [X] Add `--scenario <name>` CLI flag
Allows running dry-run with a specific failure profile. Defaults to `HappyPath` if not specified.

### [ ] Run each scenario against fresh telemetry data
For each named scenario, run a dry-run session of at least 1 hour. Log results to a comparison table.

**Pass criteria for Stage 3:** Each scenario produces expected behaviors. Recovery succeeds at appropriate rates. Tripwires fire when expected. No silent failures in any scenario.

---

## STAGE 4 — Targeted scenarios for critical fixes

### [ ] Reconciliation mismatch injection
Add `SimulatedVenuePositionClient` that can return positions different from local state. Allows manually injecting a mismatch to verify halt-on-mismatch behavior (CRITICAL fix #1).

**Test:** Run dry-run, inject mismatch on next reconciliation tick, verify bot halts and requires manual reset.

### [ ] Dust-threshold scenario
Construct a scenario that produces unhedged positions just under `CleanupDustUsd` ($0.25). Verify dust absorption fires first, before any reverse attempt is made (CRITICAL fix #2).

**Test:** Run dry-run with profile that produces ~$0.20 unhedged positions consistently. Verify no reverse orders are attempted; dust is absorbed and journaled correctly.

### [ ] Half-cent boundary scenario
Construct a scenario where the recovery hedge price would land exactly on a half-cent. Verify Math.Ceiling is used (CRITICAL fix #3) and the hedge order is at a fillable price.

**Test:** Set up a market state where the opposite-side ask is at e.g. $0.475. Run dry-run, force recovery, verify the hedge limit price is 48¢ (Ceiling), not 47¢ (Round/Floor).

### [ ] Connection-loss simulation
Add a command (keyboard shortcut or CLI signal) that forces `OnKalshiReconnect()` and `OnPolyReconnect()` events without an actual disconnect. Tests telemetry window closure and `_connectionHalted` flag.

**Test:** Trigger simulated reconnect during an active arb. Verify telemetry windows close correctly, in-flight orders complete or are reconciled, and the bot resumes trading after the reconnect.

### [ ] Maintenance threshold simulation
Force `_kalshiConsecErrors` to climb past `MaintenanceErrorThreshold` (5). Verify `_connectionHalted` fires and prevents new trades. Verify it auto-clears when errors stop.

**Test:** Inject 5+ consecutive REST failures. Verify halt fires. Then inject successful REST calls. Verify halt clears.

### [ ] Book-missing during recovery
Construct a scenario where the opposite-side book disappears mid-recovery. Verify the bot skips the hedge phase but attempts reverse (per fix #7), and only halts if reverse also fails.

**Test:** Manually clear the opposite-side book entry in `_books` during an active recovery. Verify reverse is still attempted on the filled side.

### [ ] Cancel-race simulation
Add a `SimulatedKalshiClient` mode where cancel requests sometimes return "filled" instead of "cancelled". Tests that local state stays consistent with venue state in race conditions.

**Test:** Submit and immediately cancel 20 orders. Verify final position state matches what the simulated venue reports, with no orphan tracking errors.

---

## STAGE 5 — Statistical replay against real telemetry (optional, high-value)

### [ ] Build telemetry CSV replay mode
Add `--replay <csv-path>` flag that reads detected arb windows from a CrossArbTelemetry CSV and fires `OnArbOpened` callbacks at the timestamps recorded in the CSV.

### [ ] Add `--speedup <factor>` flag
Compress real time by a factor (e.g. 10x means 1 hour of real arb activity replays in 6 minutes). Lets you run multi-day simulations quickly.

### [X] Add deterministic seed support
`--seed <int>` makes Random reproducible. Same replay + same seed + same profile = same outcome. Critical for regression testing.

### [ ] Build the pre-live test matrix
Run each combination and record outcomes:

| Scenario | Duration | Pass criteria |
|----------|----------|---------------|
| HappyPath | 24h replay | 100% fills, projected ≈ realized, 0 halts |
| 5% LegFail | 24h replay | 95%+ recover correctly, 0 unbounded losses |
| 20% LegFail | 24h replay | High recovery rate, some dust-absorbed, 0 halts unless reverse genuinely failed |
| ChronicSlippage | 24h replay | Per-trade tripwire fires when expected, day tripwire if drift compounds |
| 30% PartialFill | 24h replay | All partials handled, no orphan unhedged positions in journal |
| ConnectionStorm | 24h replay | Maintenance halt fires and clears, telemetry windows handle reconnects correctly |
| ReconcileMismatch | Single arb | Bot halts on detection, requires manual reset |
| BookMissing | Multiple injected | Skips hedge, attempts reverse |
| DustThreshold | Manual setup | Dust absorbed without reverse attempt |

### [ ] Document outcomes
Write up the results in a `DryRunResults.md` file. For each scenario, record:
- Number of arbs processed
- Realized P&L vs projected P&L
- Recovery success rate
- Any unexpected halts or behaviors
- Notable patterns (e.g. "PartialFillSwamp produced 12% more cleanup cost than expected")

**Pass criteria for Stage 5:** All scenarios complete with expected behavior. Any unexpected outcomes are investigated and either fixed in code or documented as known limitations.

---

## Implementation order recommendation

For a pre-live deployment with limited time:

1. **Stages 1 + 2** (1-3 days of work) — foundation; enables everything else
2. **Stage 3** with 3-4 named profiles (half day) — exercises items 1-9 in the gap list above
3. **Stage 4 targeted scenarios** for the critical fixes you just made (1 day)
4. **Stage 5 replay** (optional, 1-2 days) — gold-standard pre-live validation

Stages 1-3 give you most of the value. Stage 4 specifically validates the critical bug fixes. Stage 5 is the comprehensive test if time allows.

---

## What simulation still can't catch

Even with comprehensive dry-run simulation, these only show up in live:

- Real network jitter and packet loss patterns (simulated latency is uniform; real has fat tails)
- Venue-specific undocumented behaviors (Polymarket and Kalshi each have quirks not in docs)
- Real fee schedule changes (venues sometimes update without notice)
- Real KYC/compliance edge cases (might trip only when actual money is at stake)
- Behavioral fingerprinting (venues may treat live trading accounts differently than read-only API access)

Strategy: go live with the smallest possible position size for the first 48 hours regardless of how clean simulation results are. Watch the journal in real time. Simulation reduces unknowns; it doesn't eliminate them.

---

## Architectural benefits beyond testing

The interface-based refactor (Stage 2) has secondary benefits:
- Eliminates the dual dry-run/live code paths (a known bug source)
- Makes future strategy changes easier to test in isolation
- Enables A/B testing of execution strategies later (e.g. compare two recovery approaches against the same telemetry replay)
- Simplifies onboarding new venues — implement the interface, drop in the new client

Worth doing even setting aside the testing motivation.

---

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
