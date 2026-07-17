# HardVenArb — verification checklist (reader + executor changes, 2026-07-16/17)

Two layers were built: the **reader/coverage layer** (sidecar — browser-WS odds, tab manager, freshness, verify)
and the **executor layer** (C# bot — paper fills, execution gates, no-reverse recovery). Both need the **live
logged-in Pinnacle sidecar** (real paired odds are required to form an arb; the mock adapter can't pair with
real Kalshi). Nothing here places a real bet — the executor runs in `--dry-run` (simulated fills).

## Setup

Sidecar (reader mode + 3-tier tabs + auto-pair), browser logged in:
```
PINNACLE_SESSION_SOURCE=browser PINNACLE_DEDICATED_WS=0 PINNACLE_WINDOW_WS_READ=1 \
HARDVEN_TAB_MANAGER=1 HARDVEN_AUTO_PAIR=1 \
  python -m uvicorn app:app --port 8787
```
Then the bot in **paper mode** (separate terminal, repo root): `dotnet run --project HardVenArb -- --dry-run`
(add `--telemetry` instead if you only want to exercise the reader layer + CSV, no execution.)

Pick a window with **live/soon tennis** so there are pre-live windows to observe.

---

## Phase 1 — reader / coverage layer (watch the sidecar log + endpoints)

| # | Change | How to verify | PASS signal |
|---|--------|---------------|-------------|
| A1 | Reader is the sole odds source | startup + `[WS-READ]` line | `ready — WS mode, DEDICATED WS DISABLED`; `[WS-READ] … GREEN — odds flow`; session-held line shows `ws=window-reader` |
| A2 | Connection-heartbeat liveness (no false-dead on quiet lines) | session-held line during a quiet spell | `feed_live=True` holds and `ws_hb=Ns` stays low (< ~30s) even when few odds are moving; stable pre-live books don't vanish |
| A3 | Tab manager 3-tier (board + 12 dedicated + 1 rove) | `[TAB-MGR]` lines | `league tab manager ON - 12 dedicated gap tabs + 1 roving tail tab`; then `opened dedicated tab for gap league …` and `ROVE -> league …` cycling |
| A4 | Board-featured leagues excluded from dedicated tabs | `[TAB-MGR] paired=… covered=… (board=B) …` + `GET /debug/reader` | `board_lids` non-empty in `/debug/reader`; **no** `opened dedicated tab` line for a league that's in `board_lids` |
| A5 | Soonest-start ranking of dedicated tabs | order of `opened dedicated tab` lines vs `hardven_start_time` in cross_pairs.json | near-term leagues get the dedicated tabs first; later ones ride the rove |
| A6 | Per-token freshness (no phantom on frozen tail) | telemetry CSV | windows only on fresh books; a tail league that stops updating **ages out** (no lingering fat "open arb" on a stale price) |
| A7 | Authed re-seed backstop | `[PINNACLE] reader re-seed:` line | `reader price backstop ON — authed re-seed …`; then `reader re-seed: N league(s), M token(s) …` every ~30s |
| A8 | Verify-on-detection (wv + /verify) | `curl "127.0.0.1:8787/odds?selections=<tok>"` → has `"wv"`; `[VERIFY]`/`[TAB-MGR] VERIFY` on a tail arb; CSV | `/odds` selections carry `wv`; a screening-only arb triggers `[VERIFY] league <id>: {"status":"opened"}` + `[TAB-MGR] VERIFY - opened tab on demand`; CSV has a **`HardVenWsVerified`** column (0/1) |

Coverage sanity (optional): `python coverage_check.py --ttl 120` and `curl 127.0.0.1:8787/debug/reader`.

---

## Phase 2 — paper execution layer (`--dry-run`, watch the bot log)

| # | Change | How to verify | PASS signal |
|---|--------|---------------|-------------|
| B1 | Paper HardVen client (dry-run no longer throws) | let an arb execute in `--dry-run` | `[FILL P] … placed` + `[EXEC OK]`/`[EXEC SLIPPAGE]`; **no** `NotImplementedException` on the HardVen leg (this was broken before) |
| B2 | Pre-live-only gate | in-play arb detected | `[EXEC SKIP] … IN-PLAY (HARDVEN_PRELIVE_ONLY=1)`; only pre-live arbs execute |
| B3 | Execution WS-verify gate | screening-only arb detected | `[EXEC SKIP] … NOT WS-verified`; execution only on WS-confirmed legs |
| B5 | Early exit OFF (hold to settlement) | over a run | no `[EARLY EXIT …]` sells; positions stay open to settlement |

## Phase 3 — no-reverse recovery (`--dry-run --scenario <partial-fill>`)

Force Kalshi to under-fill so the recovery path runs (real pre-live rarely under-fills). Use a scenario / fill
profile with a partial-fill or Kalshi-leg-fail rate (see `Simulation/FailureScenarios.cs`), e.g.
`dotnet run --project HardVenArb -- --dry-run --scenario <name>`.

| # | Change | PASS signal | FAIL signal |
|---|--------|-------------|-------------|
| B4a | Excess Pinnacle → hedge up on Kalshi | `[RECOVER OK] … hedged +N on Kalshi (Pinnacle held)` | any `[RECOVER REVERSED] … HardVen` (selling Pinnacle) |
| B4b | Un-hedgeable excess → held to settlement | `[RECOVER HOLD] … holding N unhedged Pinnacle share(s) to settlement`; journal `HELD_HARDVEN` | an `ORPHANED` on the HardVen leg, or a sell |
| B4c | Excess Kalshi → reverse Kalshi only | `REVERSED_KALSHI` (never buys more Pinnacle) | a HardVen buy in recovery |
| B4d | No false reconcile halt on a held excess | trade completes, no halt | `[RECONCILE ALERT] … HardVen over-read … Bot halted` |

---

## Data to collect (feeds the eventual betting + UI work)

1. **`CrossArbTelemetry_YYYYMMDD.csv`** — the pre-live window tape, now with **`HardVenWsVerified`**. Analyze:
   `python analyze_cross_arb.py --pre-live` (and filter/trust `HardVenWsVerified=1` rows). This is the honest
   pre-live edge/volume the real bets will target.
2. **The `--dry-run` execution journal** (the `[ORDER P]`/`[FILL P]`/`[RECOVER …]` sequence + JSON journal
   events) — this is the blueprint for the **UI bet-slip flow**: order → fill shape → hold/hedge outcomes.
   Capture how often each recovery branch fires so the UI/recovery design matches reality.
3. **Coverage** — `/debug/reader` (`board_lids` + live-mid count) and `coverage_check.py` → how much of the
   paired slate the 3-tier tab setup actually delivers.

## Reading it

- The two execution **gates** (B2/B3) should make the bot *conservative* — it skips a lot early on (in-play +
  unverified tail) and only fires on clean pre-live WS-verified arbs. Fewer executions is correct.
- Everything is `--dry-run`: no real money. When these all pass, the remaining live pieces are the UI bet-slip
  (`_place_via_ui`) and live fill-confirmation — which is exactly what the captured data above informs.
