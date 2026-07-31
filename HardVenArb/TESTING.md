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

## Phase 4 — preview dress rehearsal (`--dry-run` + `HARDVEN_LIVE_BET_PATH=1`), 2026-07-20

Drives the **real** HardVen placement chain against the live sidecar while Kalshi stays simulated. Nothing can
be placed: the sidecar refuses without `HARDVEN_BET_ENABLE=1` **and** a built `_place_via_ui()` (which raises),
so it replies `accepted=false` → the bot books a failed HardVen leg and runs recovery. That failure IS the
test — it proves every layer up to the click.

```
# sidecar already up + logged in; HARDVEN_BET_ENABLE must stay unset
HARDVEN_LIVE_BET_PATH=1 HARDVEN_PRELIVE_ONLY=0 \
  dotnet run --project HardVenArb -- --dry-run --scenario HappyPath --try 5
```
(`HARDVEN_PRELIVE_ONLY=0` only so an arb actually fires while pre-live windows are scarce — **put it back to
`1` afterwards**. Set it in the shell, not `.env`, or it won't be picked up / will linger.)

| # | Change | PASS signal | FAIL signal |
|---|--------|-------------|-------------|
| C1 | Dress-rehearsal wiring active | `[DRESS REHEARSAL] HARDVEN_LIVE_BET_PATH=1 …` banner at startup | banner absent → still on the sim client |
| C2 | Contracts→stake conversion | sidecar logs `[PINNACLE BET] PREVIEW … WOULD place <stake> on <sel> @ max_odds>=<odds>`; stake ≈ `shares × price / FX`, `max_odds` ≈ `1/price` | stake off by ~the FX rate (EUR/USD mix-up), or `max_odds` inverted |
| C3 | Rejection parsed, not crashed | `[FILL P WARN] … success=false — preview only …` | an unhandled exception on the HardVen leg |
| C4 | Recovery runs on the failed leg | Kalshi excess → `REVERSED_KALSHI` (cheap side, as designed) | `REVERSED_HARDVEN`, or a halt |
| C5 | Stake cap enforced | a size needing > `HARDVEN_MAX_STAKE` is rejected with `stake … > HARDVEN_MAX_STAKE … (hard cap)` | an oversized stake previewing as acceptable |

**Conversion is already unit-verified** (`scratchpad/verify_conv.py`): round-trip contracts→stake→contracts is
exact to the book's 2-dp stake granularity, and the stake is **floored, never nearest-rounded**, so the
irreversible leg is never over-bought (worst case under-fills ~1.5% on a €0.48 long-odds bet).

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

---

# M1 LIVE TEST PLAN (2026-07-22) — everything built 07-20/21/22, in money-risk order

Run these **in order**. Each stage is a gate for the next: don't put real money on the line until the no-money
stages are green. Stages 1–4 risk **nothing**; only Stage 5 places real bets.

**Prereqs for all stages:** sidecar up, browser logged in, **Quick Bet mode ON**, **Decimal odds** selected, a
live/soon **tennis** slate (pre-live windows to act on).

## Stage 1 — sidecar layer, no execution (`--telemetry`, watch the sidecar log + headed browser)

| # | What | PASS | FAIL |
|---|------|------|------|
| L1 | Per-tab organic runs | startup logs `[PINNACLE TAB-ORGANIC] ON …`; over minutes the reader tabs get brought to front + scrolled (visible in the headed Chrome); no crash | no `TAB-ORGANIC ON` line; a console crash; or a popover left open on a tab |
| L2 | **Popover gesture is money-safe** (the critical one) | when the open+dismiss gesture fires, the Quick Bet popover opens then **closes within ~2s**; **balance never moves**; **nothing appears in My Bets** from organic | any bet placed by organic; a popover left open; balance changes with no bet you made |
| L3 | No dedicated tab duplicates the board | `/debug/reader` `board_lids` vs `[TAB-MGR]` opens: **no** `opened dedicated tab` for a league in `board_lids` | a dedicated tab persists for a league that's in `board_lids` |
| L4 | Board **reclaim** as the day moves | when a tabbed gap league later gets featured and stays ≥120s → `[TAB-MGR] reclaimed tab for league … now on the featured board`; the freed slot opens a **different non-board** gap | redundant tab persists >2–3 min after its league joined the board; or reclaim **thrashes** (same league open/close within seconds → raise `HARDVEN_TAB_BOARD_RECLAIM_SEC`) |

## Stage 2 — UI placement, VERIFY-ONLY, no money (`POST /bet/test`, `submit:false`)

Drives the **real** placement path but stops before Place Bet. `{"selection_id":"<lid:mid:home|away>","stake":2}`.
Each attempt auto-captures to `bet_capture_*.jsonl` (read with `python sidecar/bet_capture.py`).

| # | What | PASS | FAIL |
|---|------|------|------|
| L5 | Verify-only happy path | `[PINNACLE BET] VERIFY-ONLY OK … popover matched, NOTHING placed`; the reply names the matched matchup + side + price + max bet; **balance unchanged**, **nothing in My Bets** | places a bet; crashes; or can't find a market that IS on screen |
| L6 | Bet-tab selection order | `[PINNACLE BET] using board tab …` for a league **on the board**; `using dedicated/rove` for a gap league; `using rove-nav` when the board roamed off. Primary board is **not navigated away** | opens a cold hidden tab for a board league; navigates the primary board page away |
| L7 | **Wrong-market defense** (real slate) | pick a match whose moneyline is suspended or that has a `(Games)` row → refuses (`could not select the intended market`), **never** selects the handicap/total/Games shell | selects a handicap/`+1.5`/over-under/Games line |
| L8 | Scroll-to-find | verify-only on a match far down the league page → scrolls, finds, verifies | reports "no row … scanned N viewport(s)" for a match that IS on the page |
| L9 | Decimal-odds guard | on Decimal it proceeds; flip the site to American → refuses `… not decimal odds` | proceeds on American (would misread the price) |
| L10 | Stake entry (React input) | the stake box shows `2`, max bet parsed, `filled ok` | stake reverts to empty / doesn't take |

## Stage 3 — dry-run execution + the new gates (paper, no money) (`--dry-run`, live sidecar)

**Triggering arbs deterministically — the `I`/`O` inject keys (dry-run only).** The gates only run on a detected
arb, and real pre-live arbs are scarce + you can't choose which side Kalshi holds. So dry-run has two inject keys
that fire a *synthetic* arb through the **real** executor path (all gates, **simulated fills — no money**):
- **`I`** = inject a **FAVOURITE**-on-Kalshi arb (kAsk 0.55) → should pass the favourite gate → ladder → fills.
- **`O`** = inject an **UNDERDOG**-on-Kalshi arb (kAsk 0.45) → should hit the favourite-gate **skip** when it's ON.

Each key picks an eligible **pre-live** pair whose 4 books are subscribed (so the pre-live gate passes and fresh
books aren't stale-overridden — the gates trust the injected prices). If you see `inject: no eligible PRE-LIVE
pair …`, wait for a tennis pair's books to go live and retry. Prereq for L13: raise `--max-bet` (≈$22) or every
inject skips with `ladder: no valid rung`.

**Proper end-to-end sim — add `HARDVEN_DRYRUN_UI=1`.** Without it the HardVen fill is pure numbers (no browser).
With it, each injected BUY first drives the **real Pinnacle UI verify-only** (locate the game → click the
moneyline → verify the popover → enter the stake, **nothing placed**) — you watch it in the managed window —
`[DRYRUN UI] Pinnacle moneyline located + verified @ X`, then the fill is simulated so the arb + recovery still
run. A verify failure fails the leg (faithful). This makes the inject a true executor→browser integration test.

| # | What | PASS | FAIL |
|---|------|------|------|
| L11 | Favorite-on-Kalshi gate | tennis arb with Kalshi holding the **underdog** → `[EXEC SKIP] … UNDERDOG on Kalshi` + journal `UNDERDOG_ON_KALSHI`; **favorite-side** arbs execute | an underdog-on-Kalshi tennis arb executes while the gate is ON |
| L12 | `H` live toggle | press `H` → `… hedge OFF …`; underdog arbs now execute; `H` again → `… ON …`, they skip again | key does nothing; state doesn't change behavior |
| L13 | Stake ladder | executed size lands on a rung (10/20/…/50/…); `[LADDER] N → M contracts (stake … ≤1/3 of book max)`. **Config trap:** at default `--max-bet`/`HARDVEN_MAX_STAKE`=10 every arb hits `[EXEC SKIP] … ladder: no valid rung` — raise `--max-bet≈$22` for the €10 rung | an off-ladder stake (e.g. 37); size > 1/3 of book depth; or no fires even after raising the caps |
| L14 | Recovery still green with the gates on | force an under-fill (`--scenario FlakyKalshi`, `HARDVEN_PRELIVE_ONLY=0` in the shell): `[RECOVER OK] hedged on Kalshi (Pinnacle held)`, no `REVERSED_HARDVEN`, no halt (re-confirms 07-18 under the new code) | any Pinnacle sell; a reconcile halt |

## Stage 4 — dress rehearsal: REAL `/bet` call, still no money (`--dry-run` + `HARDVEN_LIVE_BET_PATH=1`)

`HARDVEN_BET_ENABLE` stays **unset**. Kalshi simulated; the HardVen leg hits the real sidecar `/bet`, which
refuses (preview) → the bot books a failed leg → runs recovery.

| # | What | PASS | FAIL |
|---|------|------|------|
| L15 | Whole placement chain, dry | `[DRESS REHEARSAL] HARDVEN_LIVE_BET_PATH=1 …` banner; on an arb the sidecar logs `[PINNACLE BET] PREVIEW … WOULD place`; bot sees `success=false` → `[RECOVER …]`; **no money moves** | a `NotImplementedException`; a real bet; recovery reverses Pinnacle |

## Stage 5 — FIRST REAL BETS, supervised, tiny (`HARDVEN_BET_ENABLE=1`, `HARDVEN_MAX_STAKE=10`)

Do these **watching the screen**, one at a time. Start with `POST /bet/test {…,"submit":true,"stake":2}` (a
single €2 bet, no full arb) before letting the executor place both legs.

| # | What | PASS | FAIL |
|---|------|------|------|
| L16 | Single supervised micro-bet | `[PINNACLE BET] PLACED … @ <odds> (bet <id>)`; POST returned **200**; the bet shows in Pinnacle **My Bets** at the **right match + side + stake**; accepted odds ≥ your floor | wrong match/side/stake; odds worse than floor accepted; **no confirmation within 15s → state UNKNOWN, do NOT retry — reconcile by hand** |
| L16b | Betslip trim after a bet | after L16 (or any `/bet/test`), the side betslip ends **empty**; log shows `[PINNACLE] betslip trim (post-bet): cards N -> 0`; a placed bet is **still** in My Bets (trim didn't cancel it) | trim clicks **Cancel** (cards stay); a placed/accepted bet vanishes from My Bets; `WARNING: not cleared` |
| L16c | Betslip trim while idle | drop 1-2 stray selections into the side slip by hand, then just **leave the bot roaming** (no bet); within a roam cycle the slip clears itself: `[PINNACLE] betslip trim (idle): cards N -> 0` on the visited tab (board or a reader tab) | slip stays full after several roam ticks; trim fires **during** a placement (should be paused); Cancel clicked |
| L17 | Accept-odds prompt (if it appears) | the "odds changed?" prompt → bot does **not** auto-accept (unknown markup) → `accepted=false`, logs the prompt buttons; **no bet**. (Capture it so we can build proper handling) | it clicks the prompt / places at moved odds |
| L18 | Full live arb, both legs | executor fires a pre-live favorite-on-Kalshi tennis arb → Pinnacle (real) + Kalshi (real) both confirm → held to settlement → settles as expected | one leg fills, the other doesn't and recovery mishandles; wrong-market; naked exposure |
| L19 | **Retirement/void observation** (the risk you accepted) | over several bets, catch a tennis retirement → Pinnacle **voids** (stake back) while Kalshi **settles**; with favorite-on-Kalshi the residual is **not a loss** (the Kalshi favorite pays) | a **net loss** on a void → an underdog-on-Kalshi bet slipped through (check the gate) or the hedge premise is off |

## Optional / parked
- **Flicker fix:** on the pre-live tail, `HARDVEN_QUOTE_MAX_AGE_MS≈100000` stops the stale-flicker (re-seed 90s
  vs quote-gate 30s). Set it and confirm stable pre-live tail books stop blinking.
- **`HELD_HARDVEN` branch:** `--dry-run --scenario BothVenuesFlaky` → `[RECOVER HOLD] … holding N unhedged
  Pinnacle share(s) to settlement` + journal `HELD_HARDVEN` (only when the Kalshi hedge ALSO can't complete).
