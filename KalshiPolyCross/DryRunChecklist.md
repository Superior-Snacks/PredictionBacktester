# Dry-Run Test Checklist

Covers every named scenario and runtime injection key. Work through these in order:
Stage 3 (fill behavior) first, Stage 4 (critical fix verification) second.

---

## Pre-flight

Before running any scenario, verify:

- [ ] `cross_pairs.json` has at least 1 verified pair
- [ ] `KALSHI_API_KEY_ID` and `KALSHI_PRIVATE_KEY_PATH` env vars are set
- [ ] `POLY_API_KEY`, `POLY_API_SECRET`, `POLY_API_PASSPHRASE`, `POLY_PRIVATE_KEY`, `POLY_PROXY_ADDRESS` are set
- [ ] At least one matched Kalshi + Polymarket market is actively trading (books have bid/ask prices)
- [ ] A previous run's journal CSV is not still open in Excel (file lock will silently drop events)

Base command for all dry-run tests:
```
dotnet run --project KalshiPolyCross -- --dry-run --scenario <NAME> [--seed 42] [--try N]
```

`--seed 42` makes fills reproducible. `--try N` auto-exits after N complete arbs.

---

## Stage 3 — Fill Behavior

These test that the fill simulation machinery works correctly under each profile.

---

### S3-1: HappyPath

**What it tests:** Baseline. No failures, default latency. Every trade should complete cleanly.

```
dotnet run --project KalshiPolyCross -- --dry-run --scenario HappyPath --seed 42 --try 5
```

**Watch for:**
- `[TRY LIMIT] 5 arb(s) completed — shutting down cleanly.`
- Each EXECUTION_COMPLETE journal event should have `balancedQty > 0` and `neitherFilled = false`
- `[STATUS]` dashboard: `proj` P&L should be positive or near zero, no losses

**Pass:**
- Journal contains exactly 5 `INTENT` + 5 `EXECUTION_COMPLETE` records
- 0 `RECONCILE_MISMATCH`, `CLEANUP_DUST`, `HALT_TRIPWIRE`, or `CLEANUP_REVERSED` events
- Process exits cleanly (not via Ctrl+C)

**Fail indicators:**
- Fewer than 5 `EXECUTION_COMPLETE` records (trade loop stuck or crashed)
- Any `RECONCILE_MISMATCH` (simulated clients producing spurious mismatches)
- Bot hangs and never reaches try limit

---

### S3-2: FlakyKalshi

**What it tests:** 20% Kalshi entry leg failures + 300ms latency. Exercises Case B recovery
(Poly hedge fills, Kalshi entry misses → unhedged Poly → try to complete hedge on Kalshi or reverse).

```
dotnet run --project KalshiPolyCross -- --dry-run --scenario FlakyKalshi --seed 42 --try 10
```

**Watch for:**
- ~2 of 10 arbs: `kFilled = 0`, `pFilled > 0` in `EXECUTION_COMPLETE`
- Recovery console output: `[RECOVER] ... — completing hedge on Kalshi` or `[REVERSE]`
- `CLEANUP_REVERSED` journal events where recovery reverses the Poly position

**Pass:**
- At least 1 recovery event (CLEANUP_REVERSED or EXECUTION_COMPLETE with recovery note) in 10 arbs
- 0 permanent halts (`_halted = true`) from failed recovery
- `[TRY LIMIT] 10 arb(s) completed` or bot exits normally

**Fail indicators:**
- 0 recovery events in 10 arbs (FlakyKalshi profile not being applied — check `[EXECUTOR]` log shows `FlakyKalshi`)
- `HALT_TRIPWIRE` fires every time a Kalshi leg misses (recovery not reached)
- `RECONCILE_MISMATCH` fires on clean trades (simulated clients out of sync)

---

### S3-3: FlakyPoly

**What it tests:** 20% Polymarket hedge leg failures. Exercises Case A recovery
(Kalshi entry fills, Poly hedge misses → unhedged Kalshi → dust absorption or Poly hedge attempt).

```
dotnet run --project KalshiPolyCross -- --dry-run --scenario FlakyPoly --seed 42 --try 10
```

**Watch for:**
- ~2 of 10 arbs: `pFilled = 0`, `kFilled > 0` in `EXECUTION_COMPLETE`
- `CLEANUP_DUST` journal events if `kUnhedgedContracts × kalshiAsk < $0.25`
- `[RECOVER] ... — completing hedge on Poly` console output if value exceeds dust threshold

**Pass:**
- At least 1 `CLEANUP_DUST` or recovery hedge event in 10 arbs
- 0 permanent halts from orphan unhedged positions
- No `RECONCILE_MISMATCH` events

**Fail indicators:**
- 0 recovery events (FlakyPoly profile not applied)
- `CLEANUP_DUST` never fires despite consistent Poly misses (CleanupDustUsd threshold = $0.25 — check arb prices)
- Bot halts with `HALT_TRIPWIRE` on every Poly miss

---

### S3-4: ChronicSlippage

**What it tests:** 1¢ Kalshi slippage + 2% Poly slippage on every fill. Per-trade and day P&L tripwires fire when slippage compounds.

```
dotnet run --project KalshiPolyCross -- --dry-run --scenario ChronicSlippage --seed 42
```

**Watch for:**
- `[STATUS]` dashboard: `proj` P&L goes negative after several trades
- `HALT_TRIPWIRE` journal event when per-trade loss exceeds the tripwire threshold
- `DAILY_REPORT` events accumulating losses

**Pass:**
- `proj` P&L in status dashboard visibly lower than HappyPath baseline after same number of trades
- At least 1 `HALT_TRIPWIRE` event after sufficient trades (slippage must compound past threshold)
- On halt: `[HALT]` console message, bot stops new trades

**Fail indicators:**
- P&L identical to HappyPath (slippage not being applied — check `GetKalshiFillPrice` is called)
- `HALT_TRIPWIRE` never fires even after 20+ trades (threshold calibration issue)
- Bot halts on the very first trade (slippage formula incorrect, wildly large cost)

---

### S3-5: PartialFillSwamp

**What it tests:** 40% partial fill rate on both venues. Most trades produce fill imbalances,
saturating `RecoverUnhedgedAsync`. No orphan positions should accumulate.

```
dotnet run --project KalshiPolyCross -- --dry-run --scenario PartialFillSwamp --seed 42 --try 10
```

**Watch for:**
- Most `EXECUTION_COMPLETE` events: `balancedQty < requestedQty`
- Frequent `[RECOVER]` console output
- `CLEANUP_DUST` or `CLEANUP_REVERSED` events on imbalanced trades
- No growing list of unclosed open positions

**Pass:**
- At least 4 of 10 arbs show partial fill recovery
- 0 `RECONCILE_MISMATCH` from partial fill imbalances (simulated position tracking must stay consistent)
- 0 permanent halts from failed partial fill recovery

**Fail indicators:**
- All 10 arbs show full fills (partial fill rate not applied)
- `RECONCILE_MISMATCH` on every trade (balanced qty vs venue qty out of sync due to partial fill)
- `_halted = true` after a legitimate partial fill that recovery should have handled

---

### S3-6: BothVenuesFlaky

**What it tests:** 10% leg failures + 15% partials on both venues simultaneously + elevated latency.
Exercises mixed failure recovery paths.

```
dotnet run --project KalshiPolyCross -- --dry-run --scenario BothVenuesFlaky --seed 42 --try 10
```

**Watch for:**
- Mix of `MISS` (both legs missed), `EXECUTION_COMPLETE` (full or partial), and recovery events
- No single failure type dominating — should see variety
- 200ms/160ms latency visible (trades take slightly longer)

**Pass:**
- At least 1 `MISS` event (both legs missed, no recovery needed)
- At least 1 recovery event (one leg filled, one missed)
- At least 1 clean `EXECUTION_COMPLETE` (both filled)
- 0 unexpected maintenance threshold breach (10% fail rate should not hit 5 consecutive Kalshi errors)

**Fail indicators:**
- Only `MISS` events (failures too aggressive, both legs always missing together)
- `VENUE_MAINTENANCE` fires unexpectedly (10% fail rate producing 5+ consecutive errors by chance — rerun with different seed)
- 0 recovery events (failure rates not applied)

---

### S3-7: LatencyStorm

**What it tests:** 2s Kalshi + 1.5s Poly latency, no failures. Fill timeout is 5000ms,
so 2+1.5=3.5s total should clear. Tests timing logic under worst-case latency.

```
dotnet run --project KalshiPolyCross -- --dry-run --scenario LatencyStorm --seed 42 --try 3
```

**Watch for:**
- Each arb takes visibly ~3–4 seconds from INTENT to EXECUTION_COMPLETE
- No `timeout` status in debug trades log
- Status dashboard updates slower than usual (delayed by in-flight latency)

**Pass:**
- All 3 arbs complete with `status = executed` (not `timeout`)
- 0 `HALT_TRIPWIRE` from timeout-related errors
- `[TRY LIMIT] 3 arb(s) completed`

**Fail indicators:**
- `status = timeout` in journal (fill timeout of 5000ms firing despite 3.5s total — check latency values)
- Bot hangs indefinitely (fill path not returning after Task.Delay)
- `EXECUTION_COMPLETE` shows `balancedQty = 0` despite no leg fail rate (timeout misclassified as cancel)

---

## Stage 4 — Critical Fix Verification

These test specific failure modes and verify the executor responds correctly.

---

### S4-1: DustUnhedged

**What it tests:** Poly always fails, Kalshi always fills. Every trade leaves unhedged Kalshi.
Verifies `CleanupDustUsd = $0.25` threshold: small positions absorbed silently, larger ones
trigger `RecoverUnhedgedAsync`.

```
dotnet run --project KalshiPolyCross -- --dry-run --scenario DustUnhedged --seed 42 --try 5
```

**Watch for:**
- Every trade: `pFilled = 0`, `kFilled > 0`
- `CLEANUP_DUST` events if `kUnhedgedContracts × kalshiAsk < $0.25`
- `[RECOVER] ... — completing hedge on Poly` if value exceeds dust threshold
- `[CLEANUP DUST] ... Absorbing N Kalshi dust ($X.XX) — no halt` console output

**Pass:**
- 5 trades, each producing a recovery or dust absorption event
- 0 `RECONCILE_MISMATCH` from the Poly miss (simulated client must correctly track 0 Poly balance)
- `CLEANUP_DUST` fires when `kContracts × ask < $0.25`; recovery fires when above

**Fail indicators:**
- `CLEANUP_DUST` never fires (unhedged positions silently dropped without journaling)
- Bot permanently halts after the first Poly miss (recovery failing immediately)
- `RECONCILE_MISMATCH` fires on legitimate unhedged positions (expected vs actual calculation wrong)

---

### S4-2: HalfCentBoundary

**What it tests:** Kalshi always fails, Poly hedge fills. Case B recovery (unhedged Poly → hedge on Kalshi).
Kalshi hedge uses `Math.Ceiling(ask × 100) + slippage` for `priceCents`.

> **Setup required:** For the boundary condition to trigger, a Kalshi book ask must be at a
> half-cent value (e.g. $0.475). Run with `--debug` to see TRADES log and check `priceCents`.

```
dotnet run --project KalshiPolyCross -- --dry-run --scenario HalfCentBoundary --seed 42 --debug --try 3
```

**Watch for (in TRADES debug log):**
- `PlaceKalshiLegAsync: placing priceCents=N` — if Kalshi ask is $0.475, expect `priceCents = 50`
  - Ceiling(47.5) = 48, + 2 slippage = **50** ✓
  - If floor/round were used: 47 + 2 = **49** ✗ (order would be below ask, likely unfilled)
- `[RECOVER] ... — completing hedge on Kalshi` console output

**Pass:**
- Recovery attempts a Kalshi hedge (not immediate reverse)
- When Kalshi ask is at a half-cent, `priceCents = ceil(ask×100) + 2`
- No `RECONCILE_MISMATCH` from the unhedged Poly position

**Fail indicators:**
- Recovery never attempts Kalshi hedge (falls through to reverse immediately — check `hedgeNet < 1.0` condition)
- `priceCents` uses floor or round instead of ceiling (recovery hedge placed at sub-ask price, never fills)
- Bot halts with `HALT_TRIPWIRE` on the first Case B recovery attempt

---

### S4-3: ReconcileMismatch (M key)

**What it tests:** Runtime position offset injection. Verifies `ReconcileTradeAsync` detects a
mismatch and permanently halts the bot, requiring a restart to clear.

```
dotnet run --project KalshiPolyCross -- --dry-run --scenario HappyPath
```
Then wait for at least 1 `EXECUTION_COMPLETE`, then press **M**.

**Watch for:**
- Immediate console output: `[MISMATCH INJECT] Kalshi <ticker>: offset +1 queued for next reconcile`
- `[MISMATCH INJECT] Poly <token>: +1.0000 balance offset queued for next reconcile`
- After the next trade completes: `RECONCILE_MISMATCH` journal event
- Console: `[HALT]` or similar halt message; bot stops accepting new arbs

**Pass:**
- `RECONCILE_MISMATCH` appears in journal within 1–2 trades of pressing M
- Bot stops processing new `OnArbOpened` events after halt
- Status dashboard shows `[HALTED — manual reset required]`
- Only a restart clears the halt (no auto-recovery)

**Fail indicators:**
- No halt despite M key press (ReconcileTradeAsync not comparing correctly, or mismatch offset not firing)
- Bot auto-clears halt (mismatch should be permanent — `_connectionHalted` auto-clears, `_halted` does not)
- `RECONCILE_MISMATCH` fires immediately with 0 trades (offset firing on wrong call)

---

### S4-4: CancelRace

**What it tests:** 40% Kalshi leg failures; 25% of those are misreported as "executed" with a
phantom 1-contract fill. Venue positions are NOT updated for phantom fills.
`ReconcileTradeAsync` should detect the mismatch and halt.

```
dotnet run --project KalshiPolyCross -- --dry-run --scenario CancelRace --seed 42
```

**Watch for:**
- `EXECUTION_COMPLETE` events where `kFilled = 1` but the trade looks odd (cancel-race phantom)
- Shortly after: `RECONCILE_MISMATCH` journal event (executor expects 1 Kalshi contract, venue has 0)
- `[HALT]` console output; bot stops new trades

**Pass:**
- At least 1 `RECONCILE_MISMATCH` event within the first ~10 trades (25% × 40% = ~10% chance per trade)
- Bot permanently halts after detecting mismatch
- Journal trail shows the phantom fill in `EXECUTION_COMPLETE` then `RECONCILE_MISMATCH` shortly after

**Fail indicators:**
- 0 `RECONCILE_MISMATCH` events after 20+ trades (cancel-race phantom fills not being generated — check `CancelRaceRate = 0.25` in profile)
- Bot continues trading past the mismatch (ReconcileTradeAsync not setting `_halted = true`)
- Phantom fill not appearing in journal (executor not recording the "executed" status from cancel-race)

---

### S4-5: ConnectionLoss (C key)

**What it tests:** Simulated WS reconnect cycle. Executor halts, telemetry closes open arb windows,
then resumes 500ms later.

```
dotnet run --project KalshiPolyCross -- --dry-run --scenario HappyPath
```
Wait for active trading, then press **C**.

**Watch for:**
- `[KEYS] Simulated reconnect — telemetry windows closed, resuming in 500ms`
- After 500ms: `[KEYS] Connection halt cleared — trading resumed`
- Any open arb windows in `CrossArbTelemetry_*.csv` should gain an `EndTime` (window closed)
- `_connectionHalted` briefly true, then false — bot accepts new arbs again

**Pass:**
- Bot halts for ~500ms then resumes (no `_halted = true`, just `_connectionHalted`)
- Open telemetry arb windows receive `EndTime` timestamps (OnKalshiReconnect/OnPolyReconnect fired)
- Status dashboard shows `[CONN HALT — waiting for reconnect]` briefly, then clears

**Fail indicators:**
- Bot permanently halted after C key (resume task not firing — Task.Delay issue)
- Telemetry windows do not close (OnKalshiReconnect/OnPolyReconnect not being called)
- C key has no effect (executor is null or `isDryRun` guard wrong)

---

### S4-6: MaintenanceThreshold

**What it tests:** 6 pre-seeded consecutive Kalshi REST failures. `CheckMaintenanceThresholdAsync`
fires at 5 consecutive errors → `_connectionHalted = true` + `VENUE_MAINTENANCE` journal event.
After errors run out, next successful call auto-clears the halt.

```
dotnet run --project KalshiPolyCross -- --dry-run --scenario MaintenanceThreshold
```

**Watch for:**
- First ~5 arb attempts: `[KALSHI LEG ERROR] ... simulated Kalshi REST failure (maintenance injection)`
- At 5th error: `[MAINTENANCE] kalshi: 5 consecutive REST failures — halting new trades`
- `VENUE_MAINTENANCE` journal event
- After error budget runs out (6th error clears to 0): next successful Kalshi call sets `_kalshiConsecErrors = 0` → `_connectionHalted` auto-clears
- Bot resumes trading normally

**Pass:**
- `VENUE_MAINTENANCE` appears in journal
- Status dashboard briefly shows `[CONN HALT — waiting for reconnect]`
- After the 6th failure, errors reset; bot resumes without restart
- Error count visible: 5 `[KALSHI LEG ERROR]` lines before maintenance fires

**Fail indicators:**
- `VENUE_MAINTENANCE` never fires despite 6 injected errors (errors not reaching `CheckMaintenanceThresholdAsync`)
- Bot permanently halts (maintenance should use `_connectionHalted`, which auto-clears, not `_halted`)
- Counter resets too early (a mid-sequence success clearing `_kalshiConsecErrors = 0` before threshold)

Also test the E key:
```
dotnet run --project KalshiPolyCross -- --dry-run --scenario HappyPath
```
Wait for active trading, press **E** → same VENUE_MAINTENANCE behavior should fire within 5 subsequent Kalshi orders.

---

### S4-7: BookMissing (X key)

**What it tests:** Opposite-side book removed mid-recovery. `RecoverUnhedgedAsync` Case A
skips the Poly hedge when book is missing, falls back to reversing the Kalshi position.

Run `DustUnhedged` (forces Kalshi fill + Poly miss → Case A recovery always fires):
```
dotnet run --project KalshiPolyCross -- --dry-run --scenario DustUnhedged
```
Wait for a `[RECOVER]` console message, then press **X** quickly during the next recovery attempt.

**Watch for:**
- `[KEYS] Removed Poly YES book for <label> — recovery will see missing book`
- Recovery logs: book missing path taken (no `[RECOVER] ... — completing hedge on Poly`)
- `CLEANUP_REVERSED` journal event (Kalshi position reversed instead of hedged)
- `[REVERSE]` console output showing Kalshi sell

**Pass:**
- Recovery falls back to Kalshi reverse when Poly book is absent
- `CLEANUP_REVERSED` event in journal
- No `NullReferenceException` from missing book
- Bot continues running after reverse

**Fail indicators:**
- `NullReferenceException` crash (book null check missing in RecoverUnhedgedAsync)
- Bot hangs waiting for a book that will never return
- Recovery silently does nothing (unhedged Kalshi position left open without journaling)
- X key removes book but recovery still attempts Poly hedge (using stale cached reference)

---

## Journal Quick Reference

| Event | Meaning | Halt type |
|-------|---------|-----------|
| `INTENT` | Arb opportunity detected, orders about to fire | None |
| `EXECUTION_COMPLETE` | Both legs filled (balanced or recovered) | None |
| `MISS` | Both legs missed (0 fills), no position taken | None |
| `FILLED` | One or both legs partially filled | None |
| `CLEANUP_DUST` | Unhedged position below $0.25 — absorbed silently | None |
| `CLEANUP_REVERSED` | Unhedged position reversed (sold back) | None |
| `STALE_PRICE` | Book price stale at execution time | None |
| `HALT_TRIPWIRE` | Per-trade or day loss exceeded threshold | Permanent |
| `DAILY_REPORT` | End-of-day P&L summary | None |
| `RECONCILE_MISMATCH` | Venue positions diverged from local state | Permanent |
| `RECONCILE_ERROR` | Exception during reconciliation query | Permanent |
| `VENUE_MAINTENANCE` | 5+ consecutive REST errors | Connection (auto-clears) |
| `EARLY_EXIT_INTENT` | Settlement detected, starting early exit | None |
| `EARLY_EXIT_COMPLETE` | Early exit succeeded | None |
| `EARLY_EXIT_MISSED` | Early exit order missed | None |
| `EARLY_EXIT_PARTIAL` | One leg of early exit filled, other didn't | Permanent |

**Permanent halt** (`_halted = true`): requires bot restart to clear.  
**Connection halt** (`_connectionHalted = true`): auto-clears when errors stop or reconnect fires.

---

## Common Gotchas

- **No arbs firing:** `cross_pairs.json` empty or books not priced yet (wait 60s for WS data).
- **All trades miss:** Arb threshold too tight for current market spread — check `[STATUS]` for book prices.
- **Scenario not visible in [EXECUTOR] log:** Typo in `--scenario` name (case-insensitive, but must match exactly — see `FromName` for valid names).
- **M/C/E/X keys do nothing:** Only active in `--dry-run` mode, and require stdin to be a TTY (won't work in `screen` without a PTY — use `screen -S name` and attach interactively).
- **Bot exits before try limit:** A permanent halt fired — check journal for `HALT_TRIPWIRE`, `RECONCILE_MISMATCH`, or `RECONCILE_ERROR`.
