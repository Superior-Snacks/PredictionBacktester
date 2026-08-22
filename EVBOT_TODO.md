# +EV Taker Bot — Build Plan

Companion to `KellyStrat.MD` (the strategy). This is the build order and the decisions already made, so
they are not relitigated. Written 2026-08-21.

---

## 0. Decisions already taken

**A NEW, SMALL PROJECT — do not copy `HardVenArb/`.**
A copy inherits ~10,000 lines of C# of which perhaps 15% applies, and every later fix would then have to be
made twice. Sizes, for the record: `CrossArbExecutor.cs` 4481, `Program.cs` 2366, `CampManager.cs` 1101,
`CrossArbRestVerifier.cs` 455, `StakeLadder.cs` 143. All two-leg execution, hedging, recovery, camping —
none of it needed by a single-leg taker.

**Target: ~600–1000 lines for M0.**

### Architecture
```
KalshiEvBot/  (new)
  ├─ references PredictionBacktester.Engine      -> KalshiOrderClient (V2, fees, positions)
  ├─ HTTP to the EXISTING sidecar                -> Pinnacle prices. Do NOT copy the sidecar.
  ├─ reads cross_pairs.json                      -> the pairing already works and re-points itself
  └─ Kalshi WS for DETECTION + REST for VALUATION
```

**The sidecar is shared, never copied.** It owns the browser session and the Pinnacle login, and that
session is the most fragile thing in the system. Two sidecars = two browsers = two logins on one account.

### The three-source split (decided 2026-08-21)
| purpose | source | why |
|---|---|---|
| **Detection** | Kalshi **WS** | push, milliseconds, fires at the right moments |
| **Valuation** | Kalshi **REST** | the WS ask is ~4c optimistic — see §4 |
| **Execution** | **IOC limit at break-even** | a fill is then +EV by construction, whatever the book said |

The limit price is the protection, not the data source. Set it at the highest price that still clears
`EV_MIN`; then a better book fills cheaply, a worse book does not fill, and neither outcome can lose money.
This is why the WS bug (§4) is survivable in execution but **fatal in M0**, where there is no order.

### Not running in parallel with the arb bot
Confirmed with the operator. Removes the one-Kalshi-WS-per-account constraint entirely.

---

## 1. M0 — Observation only  — **BUILT 2026-08-21**

No orders. Logs what it would have done. `KalshiEvBot/` — ~1,150 lines across 8 files, in the solution.

- [x] **Project scaffold** — `KalshiEvBot.csproj`, references `PredictionBacktester.Engine` only.
- [x] **Pair loader** (`EvPair.cs`) — reads `HardVenArb/cross_pairs.json` in place; never keeps its own copy,
      which would drift from the pairing job in silence. Both guards ported: the advisory title-order check
      and the assumption-free event check that drops BOTH rows of a contradiction. Requires BOTH Pinnacle
      tokens — a row the arb bot could trade one-sided is unusable here, because the vig only exists across
      the pair. Measured on the live file: 35 usable of 108 rows, 0 guard drops.
- [x] **Pinnacle price client** (`PinnacleOracle.cs`) — polls the running sidecar's `GET /odds`. Adds no
      request to Pinnacle: the sidecar already holds the socket and answers from what it was pushed.
      Carries over the two freshness policies (per-quote age for Pinnacle, feed-health for a push-only venue).
- [x] **De-vig engine** (`DeVig.cs`) — proportional and Shin, both computed on every signal, both logged.
      `EV_DEVIG` selects which one decides. Shin by bisection, not the closed form.
- [x] **Kalshi WS detection** (`KalshiBookFeed.cs`) — a clean ~240-line feed, not the arb bot's; no
      `MarketStateTracker`, no `LocalOrderBook`, no telemetry coupling. Applies the `_minBookPrice` guard on
      the snapshot path as well as the delta path, retiring one of §4's two candidate causes for free.
- [x] **Kalshi REST valuation** — one `GET /markets/{ticker}` prices BOTH sides. Fires only on candidates
      that survive the free screen, per-ticker cooldown (`EV_RECHECK_COOLDOWN_MS`), concurrency 2.
- [x] **EV + Kelly** (`EvMath.cs`) — fee-inclusive cost in both EV and the Kelly denominator. Alpha capped
      at 0.35 as well as floored at 0.10. `BreakEvenLimit()` computes the IOC limit M2 will send.
- [x] **Telemetry CSV** (`EvTelemetry.cs`) — 43 columns, its own writer. Arity checked on **every** write,
      not once; and a day-file whose header differs is rotated rather than appended to, so two schemas can
      never interleave in one file.
- [x] **`--once`** — warm up, evaluate every pair once, print the tally, exit.

Beyond the original list, because they were cheap and retire open questions:
- [x] **`--self-test`** — 44 offline assertions, no venue and no network: de-vig sums, the fee arc, EV at the
      limit price, both pair guards, telemetry arity, the sizing chain, and *arb ⊂ +EV* as an executable
      claim. It has already earned itself: it caught `OrderFee` inventing a cent on round orders through
      floating-point noise (175.00000000000003 ceilinged to 176), always against us.
- [x] **`--check`** — validates the pair file and exits **before opening any connection**. Kalshi allows one
      WS per account and a second one connects happily and receives nothing, so inspecting the file must not
      be able to blind a bot that is already running.
- [x] **`--book-audit [N]`** — §4's decisive test, as a command.

### The screen, and why an optimistic feed is still allowed to drive it
The WS ask reads LOW, so the EV it implies is an upper bound. A candidate the WS calls uninteresting cannot
be interesting at REST, so screening on it discards only what REST would have rejected anyway — and buys a
REST call per *signal* rather than per *tick*. `EV_PRESCREEN_SLACK` (2c) covers the 5% that run the other way.
Pinnacle moving triggers a full re-screen too: a value bet can open with the Kalshi book perfectly still, and
a bot woken only by Kalshi would never see that kind at all.

### M0 acceptance
Runs a full session without touching the order API — structurally true, the order API is not wired, not
gated — and produces a CSV whose `Ev` column is computed from the **REST** ask. Both WS and REST prices are
columns, with the gap between them, so the phantom is measured rather than assumed.

### Fit for a LONG run — fixed 2026-08-21
Two defects would each have quietly wasted a multi-day session. Both looked healthy from the console, which
is what made them worth hunting before starting rather than after.

- [x] **The watchlist was frozen at startup.** `cross_pairs.json` was read once, so by day two every match
      being watched had finished and none of the day's fixtures were. A fortnight's run would have returned
      one day of data and printed a normal status line throughout. Now `PairReloadLoopAsync` re-reads the
      file whenever the pairing job rewrites it (`EV_PAIR_RELOAD_SEC`, default 120) and wires new markets
      into all three consumers: `KalshiBookFeed.EnqueueSubscribe`, `PinnacleOracle.AddTokens`,
      `EvEvaluator.UpsertPairs`. Existing entries are overwritten, because the pairing job re-points a
      market at a new Pinnacle matchup id when a fixture is re-issued and the stale id prices against a
      dead selection.
      * Markets are **only added, never removed**. A finished market costs one dead subscription; dropping
        it would take it out of the settlement watcher before its result was banked, and the venue does not
        keep obscure markets to be asked again later.
      * A reconnect re-subscribes the CURRENT list, not the startup one.
      * A read landing mid-write is treated as "retry next tick", not as a reason to stop watching.
- [x] **The CSVs never rolled at midnight.** The filename stamp was taken once, so a bot started on the 21st
      was still writing `…_20260821.csv` on the 30th — nothing lost, but every date filter downstream reads
      nine days as one. `RollingCsv.cs` now rolls on the UTC date and is shared by both writers.

- [x] **`--verify`** — runs each subsystem once and prints PASS / WARN / FAIL per item, because most of
      this bot is silent between events (snapshots every 5 min, settlements every 10, a reload only when the
      pairing job writes). A few minutes of console output therefore cannot tell "working" from "never ran",
      which is the same quiet-versus-broken confusion that has caught this project repeatedly. WARN is used
      wherever the cause is legitimately external — no matches in play, session down, nothing settled — so
      those read as conditions rather than defects. The hot-reload check exercises the real path against a
      temporary COPY (the live file is never written), holding back a row the loader will actually keep.
- [x] **Status line now has a second row** carrying snapshot rows, settlement counts, and how long since the
      pairing job last wrote — so a pasted log can be read for health, and a reload that never fires is
      explicable rather than ambiguous.

**Two bugs the first live `--verify` found, which is the argument for having it:**
* `Csv.Read` opened files with share-Read, which conflicts with the writer's own Write handle — it crashed
  `--verify` outright and would have broken the advertised "safe to run alongside" guarantee for
  `--resolve`. Both readers now use `FileShare.ReadWrite`.
* A throwing check aborted the whole run, so checks 9–11 reported nothing at all. Each step is now wrapped
  and a failure becomes a FAIL line.

**Operational dependency:** the reload only helps if the pairing job is actually rewriting
`cross_pairs.json` on a schedule. If that file never changes, the bot has nothing new to pick up.

**Volume, for planning:** ~38 pairs → roughly 11k snapshot rows and 8k telemetry rows per day, about 6 MB.
A fortnight is well under 100 MB.

### Running it
```
dotnet run --project KalshiEvBot -- --self-test     # offline, safe any time (50 checks)
dotnet run --project KalshiEvBot -- --verify        # exercise every subsystem once, PASS/WARN/FAIL
dotnet run --project KalshiEvBot -- --check         # validates pairs, opens nothing
dotnet run --project KalshiEvBot -- --once          # needs the sidecar up AND the arb bot stopped
dotnet run --project KalshiEvBot -- --book-audit 15
dotnet run --project KalshiEvBot                    # the M0 session
```
Not yet exercised against live feeds — that needs the sidecar running and the arb bot's Kalshi WS released.

---

## 1b. THREE-WAY SUPPORT — **BUILT 2026-08-21** (soccer 1X2 and anything else n-way)

Not "add soccer". `pair_pinnacle.py` is one pipeline for every sport and the EV bot has **zero** sport-specific
code — tennis worked only because tennis is two-way. Supporting the 3-way *shape* unlocks ~230 open soccer
events at once (`KXNCAAMSOCCERGAME` 96, `KXMLSGAME` 31, `KXLALIGAGAME` 25, `KXEPLGAME`/`KXSERIEAGAME`/
`KXLIGUE1GAME` 21 each, `KXBUNDESLIGAGAME` 10, `KXUCLGAME` 7 — all verified 3 markets per event), and every
future n-way market for free.

- [x] **`DeVig` generalised to n legs** — `ProportionalN` / `ShinN` over an odds vector; the two-way helpers
      now delegate to them, so tennis and soccer cannot drift apart. Shin's bisection already summed over i,
      so it generalised unchanged.
- [x] **`EvPair.Legs`** — the complete outcome set, with `ThreeWay`. Two-way rows synthesise `[yes, no]`.
- [x] **Nothing reads `NoToken` for pricing any more.** *This was the trap.* On a two-way it is the true
      complement; on a 1X2 it is merely another leg — "not Arsenal" is Coventry **plus** the draw, while
      `NoToken` points at Coventry alone. The evaluator works from `Legs` and takes the complement as
      `1 − P(YesToken)`, which is correct for any number of legs and needs no knowledge of which leg is which.
- [x] **No silent fallback.** A three-way row without a complete `hardven_legs` is DROPPED and reported,
      never downgraded to two-way — that would divide by the wrong S and yield a plausible, wrong `P_true`
      that nothing downstream could catch.
- [x] **Every leg must be fresh, open and quotable**, not just the two named on the row: a 1X2 missing its
      draw price has no valid S, so its home and away legs are unusable too.
- [x] **The event-contradiction guard gained a 3-way arm.** It keyed on `Count() == 2`, so it silently
      skipped every soccer event — leaving the rows with the most ways to be wrong as the only ones with no
      cross-check. Now: one matchup, three distinct YES legs, identical leg sets, or the whole event drops.
- [x] **`pair_pinnacle.py` emits `hardven_legs`** on 3-way rows (it already detected the draw designation,
      handled Tie markets and set `three_way`).
- [x] **`SERIES_SPORT` gained the major soccer leagues.** It had `KXLALIGA2GAME` — the *second* division —
      but none of the top ones, so ~230 events were matching with no league anchor at all.
- [x] **`KXUCLADVANCE` blocked on a settlement mismatch.** It is structurally a clean two-way and pairs
      without complaint, but "advances" includes extra time and penalties while Pinnacle's 1X2 is explicitly
      90 minutes plus stoppage. Every tie level after 90 minutes would be mispriced — and only the ties that
      go long, which is when it costs most. Correct pairing needs Pinnacle's separate to-advance market.
- [x] **Telemetry gained `NumLegs` and `PinOddsAll`**; the console tags a signal `[3-way]`.
- [x] **Self-tests: 71** (from 50), including the complement rule `P(home NO) == P(draw) + P(away)`, that a
      missing draw invalidates the whole book, that the 2-way and n-way paths agree exactly, and that the
      two de-vigs visibly diverge on a 0.98 favourite — which is why both are still logged.

### The sidecar question — ANSWERED, and it found a defect (2026-08-21)
**Pre-match: works.** `pinnacle_adapter.catalog()` sets `three_way = sport == "soccer"` and synthesises the
draw leg from the moneyline's `draw` PRICE (`"draw" in winner_desigs`), because a soccer matchup exposes only
two *participants* — the draw is a price, not a participant. `_SIDES` already includes `draw`, so the odds
path serves `{lid}:{mid}:draw` too. Nothing was missing.

**In-play: was silently broken.** The live-board catalog path — the one that exists because the guest feed
does not list in-play matchups — **hardcoded `three_way=False`** and built legs from participants. So an
in-play soccer game arrived looking exactly like a tennis two-way, and would have been de-vigged on two legs
of a three-way: `S = 1/2.30 + 1/3.10 = 0.758`, giving `P(home) = 0.574` against a true `0.40`. A phantom
edge on every leg, in the direction that makes us bet, on the board the operator was actually watching.

- [x] **Sidecar fix** — `three_way` on the live path is now derived from sport rather than hardcoded. This
      does NOT complete the book (no draw leg is emitted there), so the pairing writes no `hardven_legs` and
      the EV bot drops the row loudly. That is the intended outcome: in-play soccer is not pairable yet, and
      saying so is the point.
- [x] **The incomplete-book guard** (`EvEvaluator.Screen`, and the snapshot log) — reject any book whose
      overround is negative. A bookmaker never offers a negative margin, so `S < 1` is not a generous price,
      it is proof that a leg is missing. Keyed on the **arithmetic**, not on the sport or the `three_way`
      flag, because the flag is exactly what was wrong; any future venue or market that loses a leg is
      caught the same way. Counted as `incomplete-book` on the status line.
- [ ] **In-play soccer coverage** — needs the live path to emit the draw leg from the reader's prices
      (the odds path already tokenises `:draw`). Until then those rows are dropped, not mispriced.

---

## 2. M1 — Settlement validation — **TOOLS BUILT 2026-08-21**

Run with `dotnet run --project KalshiEvBot -- --resolve`. **REST only, no WebSocket**, so it is safe to run
while another bot holds the account's single socket.

### THE VENUE IS NOT AN ARCHIVE — this drove the design
Kalshi does not keep obscure markets available indefinitely. An ITF or challenger match that settled last
week may simply not answer today. So the outcome must be **captured while it exists and stored permanently
by us**; anything that treats the venue as a place to look things up later is a race we can only lose, and
lose *silently* — a purged market is indistinguishable from one that never settled unless the difference
was recorded at the time.

- [x] **Permanent store** (`SettlementStore.cs`) — **append-only JSONL** (`ev_settlements.jsonl`), not a
      cache and not a rewritten JSON blob. It is the only copy, so a failure must cost at most the line
      being written: one interrupted rewrite or one disk-full would otherwise take the whole history with
      nothing to reconstruct it from. Nothing is ever deleted or overwritten — re-reading takes the last
      record per ticker, so a market seen active and later finalized just gains a line, which also leaves
      an audit trail of *when* each outcome was first seen. Imports the older `ev_settlements.json` once.
- [x] **Live watcher** (`SettlementResolver.WatchAsync`) — the half that actually protects the data. The
      running bot polls its own markets every `EV_SETTLE_POLL_MIN` (default 10) and banks each result within
      minutes of settlement. A played match finalizes within the hour, so this wins the race comfortably;
      `--resolve` days later does not.
- [x] **`gone` as a terminal state** — a 404 is recorded as `status: "gone"`, distinct from `active`. "We
      never got an answer" has to be visible in the data, not hidden as a market that is forever pending.
      The calibration report prints lost markets in red and says outright that those observations are
      unrecoverable.
- [x] **Resolver** (`SettlementResolver.cs`) — fetches `status`/`result`/title/close times per ticker.
      Field shape verified live: `status` is `"active"`/`"finalized"`, `result` is `""` until final then
      `"yes"`/`"no"`. Writes only on change or on terminal, so a market re-checked every ten minutes for a
      fortnight does not add two thousand identical lines.
- [x] **Calibration report** (`Calibration.cs`) — deciles of `P_true` vs realised frequency, pooled bias
      with a standard error and a z, and a **Brier score for proportional vs Shin** (the one number that
      compares the two de-vig methods without arguing about thresholds).
- [x] **Splits** — in-play vs pre-match and oracle-age buckets, which is how the oracle-lag question gets
      answered: if in-play calibrates worse, the in-play signals were us reading Pinnacle late.
- [x] **Realised vs quoted EV** — reported LAST and labelled colour, not evidence. See the note below on
      why P&L is the slow way to ask this.
- [x] **Dedupe to one observation per (ticker, side)** by default. The cooldown logs one live opportunity
      repeatedly — six rows in five minutes on one market — and those share a single outcome. Counting them
      as six trials would shrink every interval by over half and manufacture significance from a repeat.
      `--all-obs` overrides.
- [x] **CSV reader** (`Csv.cs`) — hand-rolled RFC4180, handles the quoting the writer emits.

**Settlement timing, measured:** a played match finalizes promptly (an ATP challenger settled within the
hour of its signal). A postponed one sits `active` for days against a fallback `close_time` two weeks out.
So "not settled yet" is normal, not an error, and the report says so rather than showing an empty table.
- [ ] **Three questions M1 exists to answer:**
      - Is Pinnacle's line predictive **on ITF/challenger tennis specifically**? The sharp-book assumption
        is established for majors; these are small events where Pinnacle's own limits are €500–2000.
      - Is calibration flat across 0.20–0.80, or is one end carrying all the error?
      - **Is the in-play edge real, or is it oracle lag?** Both signals in the first session were in-play,
        against a Pinnacle quote 594–1111ms old (WS → sidecar → our ≤3s poll). In-play tennis reprices every
        point, so "Kalshi is mispriced against Pinnacle" and "we are a second behind and Kalshi already
        moved" are the *same observation* at detection time. Split the M1 calibration by `InPlay` and by
        `OracleAgeMs` — if in-play signals settle worse than pre-match ones, this is the reason.

### Why M1 is a calibration report and not a P&L tally
A single signal at p≈0.28 has a payoff standard deviation near 0.45 against an edge of ~0.02 — the noise is
twenty times the signal, and contracts within one signal share an outcome so they do not average it down.
Detecting a 2c edge in **realised P&L** at 2σ therefore needs on the order of a *thousand* settled signals.
Testing whether `P_true` is **calibrated** — pooled predicted vs realised frequency — converges far faster:
a few hundred settlements give roughly ±2 points, which is enough to see the 2–4 point bias that would eat
the whole edge. Grade the model, not the money.

### The fast path: calibration does not need SIGNALS
Signal rows are not independent bets. First session: 8 signal rows = **3** distinct (ticker, side) = **2**
matches, because the re-check cooldown logs the same live opportunity again every 15s. Counting rows
badly overstates how quickly "hundreds of signals" arrives.

But the question that decides the strategy — *is Pinnacle's de-vigged line predictive on this kind of
tennis?* — needs `(P_true, outcome)` pairs, **not** +EV signals. Every logged row carries `P_true`, signal
or not, so all 144 rows of that session are gradeable, not 8.

- [x] **Oracle snapshot log** (`OracleSnapshotLog.cs`) — **BUILT**. One row per pair every
      `EV_SNAPSHOT_MIN` minutes (default 5) to `EvOracleSnap_YYYYMMDD.csv`: `P_true` by both methods,
      Pinnacle's two prices, vig, in-play flag, settlement ticker. **No REST call and no Kalshi
      dependency** — pure sidecar data the bot already polls. Evidence now accumulates at the rate matches
      are PLAYED rather than the rate signals happen, and it accrues even if no +EV window ever opens.
      One row per PAIR, not per side: de-vig forces `P_true(NO) = 1 − P_true(YES)`, so a NO row is the same
      observation written twice and would halve every confidence interval for free. Column names match the
      signal telemetry where they overlap, so one code path grades both files.

### M1 acceptance
**Hundreds of settled signals.** At ~70 +EV signals/day that is 1–2 weeks of M0 running. The build is a
day; the answer is a fortnight. Start M0 early for that reason alone.

---

## 3. M2 / M3 — later
- [ ] **M2** — live at minimum size, IOC only, all §6 guardrails, only after M1 shows calibration holding.
- [ ] **M3** — size up; then evaluate the maker variant (`KellyStrat.MD` §7).

### M2 PREREQUISITE, not yet in the design: the bot has no position awareness
It evaluates each side of each market independently and does not know what it already holds. Harmless in
M0, which buys nothing. In M2 it is not:

* **The cooldown would re-BUY, not just re-log.** `KXWTAMATCH-…BEJKEY-BEJ` NO logged as a signal **six
  times** in five minutes on one opportunity. In M2 that is six orders and ~188 contracts, not one.
* **Both sides of one market can be held at different times, and that is CORRECT** — see below — but it
  must be a deliberate exit, not an accident.
* §6.2's correlated-event cap is specified and **not implemented**.

**Both sides at once is structurally impossible, so it needs no guard.** De-vig forces
`P_true(YES) + P_true(NO) = 1`, while Kalshi's two asks sum to `1 + spread`, so
`EV(YES) + EV(NO) = −spread − fee(yesAsk) − fee(noAsk)`, which is always negative — about −3.6c at the
measured 1c spread. Confirmed on every same-instant pair logged: asks summed to 1.0100, EV sums −0.0173 and
−0.0121, never both positive. The arb bot could see both sides because it compared Kalshi against a
*second venue* that drifts independently; here both sides are priced against one number that sums to 1 by
construction.

**Both sides at DIFFERENT times is how you exit.** Owning 1 YES + 1 NO is $1 guaranteed, so buying the
opposite side closes the position. Observed live: `BEJKEY-BEJ` YES at 0.25 (Bejlek a 28% underdog, 18:25),
then NO at 0.11–0.18 once she was an 83% favourite (18:52–18:57). At the logged sizes that is 0.4116 paid
for a certain 1.0000 — **+0.59 locked per pair**, the first leg having come good and the second banking it.
The reverse case locks a *loss*, and even that is correct EV behaviour (it converts a mark-to-market loss
into a smaller certain one), but it pays a second fee and ties up capital. Either way M2 must do it
knowingly.

---

## 4. The Kalshi WS book was ~4c optimistic — **DOES NOT REPRODUCE in KalshiEvBot**

**Result 2026-08-21.** `--book-audit` on 10 live markets: **20/20 comparisons at exactly +0.0c**, p10 =
median = p90 = 0.0c, REST worse for us in **0/20**. The 39 telemetry rows from the same session agree —
`WsRestGapCents` is 0 on all but one, and that one is **−1c** (the WS was *pessimistic*, the opposite
direction). **59 of 59 observations show no phantom.**

The EV bot does NOT inherit `KalshiWebsocketFeed.cs` / `LocalOrderBook.cs`. It has its own
`KalshiBookFeed.cs`, which applies the `_minBookPrice` guard on the **snapshot path as well as the delta
path** — candidate cause #1 below, fixed by construction.

**What this does and does not establish.** It clears the blocker: EV computed on this bot's WS book is not
measurably optimistic, so §1's acceptance criterion is met on price. It does *not* prove the arb bot's +4c
was that guard. A second explanation survives: the original figure came only from windows that had **passed
an arb screen** — by construction the moments the WS showed an unusually good price — so a book that
occasionally invents a level would be sampled precisely when it did. This bot's pre-screen is far looser,
and `--book-audit` samples markets rather than signals, so neither is subject to that selection. Separating
the two would mean re-running the arb bot's book code side by side, which is not worth doing now.

**In-play now covered too (second audit, busiest-book-first selection).** 20 comparisons, 6 of them on
live in-progress matches: median +0.0c overall and +0.0c in-play, REST worse in 1/20. The single non-zero
pair is one market straddling a 1c spread — YES −1.0c and NO +1.0c on the same ticker at 15ms book age,
i.e. the whole book shifted one tick between the two reads. That is a live match moving, not systematic
optimism. Six in-play comparisons is a thin sample, but the fast-book case is no longer untested.

### The original measurement, for the record
Measured over ~400 windows (2026-08-20/21) on the ARB bot's book implementation:
* REST ask minus WS ask: **p10 +1c · median +4c · p90 +7c**, worse for us **95%** of the time.
* Pinnacle, same test: **median +0.00c**, wrong 7% — Pinnacle is exact.
* **It is not staleness.** 389 of 400 windows opened on a book aged **0ms**, and those carry the +4c.
  The eleven windows on *older* books show **+0.00c**. The freshest books are the wrong ones — consistent
  with an error introduced *on the tick* rather than one that decays.
* The real fill arbitrated it: WS 0.6000, REST 0.6300, **filled 0.6151** — nearer REST.

Candidate causes:
- [x] `ApplySnapshot` did **not** apply the `_minBookPrice` guard that `ApplyDelta` does, so a level admitted
      by a snapshot could never be removed by a delta. Real bug. `KalshiBookFeed.cs` guards both paths.
- [ ] The implied-level derivation (`yesBook` asks built from `noBook` bids, `1 − price`) around delta
      application. Untested; moot unless the gap returns.

**The test is a command:** `dotnet run --project KalshiEvBot -- --book-audit 15`.

---

## 4b. The NEW book problem: the price is right, the SIZE is not

Surfaced by the same audit — a different failure mode, and this one is not fixed.

**Top-of-book can be a sliver.** `KXITFMATCH-26AUG20RAHWEI-RAH` YES quoted an ask ladder of
`0.22×<1 · 0.37×17 · 0.38×18`. The best ask is right, and REST confirms it — but there is **less than one
contract** behind it (it printed as `x0`; the ladder format rounded a fractional size to zero, since fixed) and real liquidity starts **15c worse**. Screening on top-of-book gives a correct EV
for a size nobody can trade.

The IOC limit keeps this safe rather than costly: an order limited at break-even fills the sliver and
cancels the rest, so the money is never at risk. What breaks is the *record* — the `Contracts` column logs a
size that was never available, and M1 would weight its calibration by it.

**The size UNIT question is CLOSED — not a unit bug (2026-08-21).** Checked directly against
`/markets/{ticker}/orderbook`:

| market | WS top-of-book | REST top-of-book |
|---|---|---|
| `KXWTAMATCH-…GAUKOS-GAU` YES | 0.57×232599 | 0.57×236155 |
| `KXWTAMATCH-…GAUKOS-GAU` NO | 0.44×29461 | 0.44×29361 |
| `KXITFWMATCH-…PIENAG-NAG` NO | 0.67×20 | 0.67×20 |

Same scale, same prices, differences only where the book moved between reads. The four-orders-of-magnitude
spread is **real liquidity**: headline WTA names (Gauff, Swiatek/Pegula) carry genuinely deep books while
obscure ITF matches show 4–20 contracts at the top. Sizes can be trusted; the *thinness* cannot.

- [ ] **Decide how M1 weights a signal**: by requested size, or by size actually available at the limit.
      The second is the honest one, and the depth figure is now known to be sound enough to use.

### Parsing note — the shape is not what this repo assumes
`GET /markets/{ticker}/orderbook` returns `{"orderbook_fp": {"yes_dollars": [["0.4300","80"], …],
"no_dollars": […]}}`. The wrapper is `orderbook_fp`, the side keys carry a `_dollars` suffix, and prices are
**dollar strings, not cent integers**. The first version of the audit parser looked for
`orderbook`→`yes`/`no` in cents and returned an empty ladder on every market — the depth check ran and
silently measured nothing. `RestAskLadder` now accepts both namings and both price scales.

**The same wrong assumption is live in the arb bot.** `HardVenArb/BookRefresherService.cs`
(`GetBestBidFromKalshiSide`, wired at `Program.cs:2143`; `KalshiPolyCross` has a copy) reads
`orderbook`→`no` in cents. Against the real payload it finds nothing, returns −1, and takes the
`restYesAsk < 0` branch — which calls `MarkDead()` on both books of every Kalshi market it refreshes.
Traced as far as the honest-book count (`Program.cs:1612`) and camp eligibility; not traced to whether it
gates detection. Not fixed here — the arb bot is being retired — but it is a real fault if it is ever run again.

---

## 5. Do NOT bring across
`CrossArbExecutor` · `CampManager` · `StakeLadder` · `CrossArbRestVerifier` · the slip/UI placement path ·
anything named `hedge`, `recover`, or `camp`. All of it exists to solve two-leg execution, which is the
problem this design deletes.

**Worth copying (small, proven):** the balance guard, the journal pattern, `PairSidesAgree`, the telemetry
CSV writer shape, and the `--camp-check`-style offline self-test idea (an EV/Kelly unit test with no venue).

---

## 6. Open questions
- [ ] **`EV_MIN`.** The 3.5% in the original draft was calibrated for maker; as a taker it admits **0.9%** of
      windows. Start at **1c** for observation (7.2%), set the live value from M1.
- [ ] **The 0.20–0.80 window is a trade-off, not a free filter.** The fee arc peaks at 0.50 (1.75c) and is
      cheapest at the wings (0.33c at 0.05). Revisit with settlement data — the wings may pay for their
      de-vig risk.
- [ ] **Kalshi's actual taker fee multiplier** — confirm against a real fill rather than assuming 0.07·p·(1−p).
      Wired to `EV_FEE_RATE` so the correction needs no rebuild.
- [ ] **Is the arb bot retired or kept runnable?** If kept, be explicit about which process owns the
      sidecar's in-play/camping mode.


---

## 7. ESTIMATOR IMPROVEMENTS — deferred until the tennis sample matches soccer's (opened 2026-08-22)

### The bar before ANY of this gets tuned
Soccer settled **93 distinct markets**; tennis has **10**. Do not choose between the options below until
tennis is at a comparable count (~90 settled markets). Every one of them is a knob that will happily fit
itself to 16 observations and tell you it worked.

### What the first settlement data actually said
```
POOLED (all obs)   n=141  predicted 0.4081  realised 0.3901  diff -0.0180 +/- 0.0408   <- oracle is FINE
SIGNALS ONLY       n=16   predicted 0.3708  realised 0.1875  diff -0.1833 +/- 0.0980   <- the subset is NOT
  soccer signals   n=9    predicted 0.232   realised 0.000
  tennis signals   n=7    predicted 0.549   realised 0.429   (-0.120 +/- 0.166, inside noise)
```

**Diagnosis: this is most likely SELECTION ON A NOISY ESTIMATE, not a broken de-vig.** We filter on
`EV = P_true - cost`. If `P_true = true_p + e`, then conditioning on `EV > threshold` selects rows where `e`
happened to be POSITIVE. The winner's curse — and it explains how the oracle calibrates at -0.011 overall
while its EV-clearing subset calibrates at -0.23. **A better de-vig does not fix selection-on-noise**; it
only shrinks `e`. Any fix has to either reduce the noise or correct for the conditioning.

### The levers, ranked by expected impact
- [ ] **1. Shrink the ESTIMATE toward Kalshi, not just the SIZE.** Today `alpha` shrinks the Kelly fraction
      while `P_true` is used at face value and Kalshi's price is treated as pure noise. It is not — it is a
      market forecast carrying information. Standard forecast combination:
      `P = w*P_pinnacle + (1-w)*P_kalshi_implied`, `w` from RELATIVE PRECISION.
      Attacks the failure directly because it penalises LARGE disagreements most, and large disagreements
      are exactly where we are wrong. **DANGER: shrinking toward the market mechanically reduces
      disagreement and therefore signal count. `w` must come from measured precision — never tune it upward
      until signals reappear, that is fitting the knob to the outcome you wanted.**
- [ ] **2. Empirical recalibration.** Fit `P_calibrated = f(P_raw)` on settled outcomes (Platt scaling or
      isotonic regression) and apply it. Corrects decile-level miscalibration directly, including the
      favourite-longshot inflation measured 2026-08-22 (EV was a smooth FUNCTION OF PRICE: +6.57c at
      sub-5c decaying to ~0 mid-book). Needs a few hundred settlements to fit without overfitting.
- [ ] **3. Add the POWER de-vig as a third method.** `P_i proportional to q_i^k`, solve `k` so they sum to 1.
      Handles favourite-longshot bias differently from Shin and often better. `EvProp`/`EvShin` are already
      logged and Brier-scored per row, so a third costs two columns and lets SETTLEMENT pick the winner
      rather than us picking now.
- [ ] **4. Two signals already logged and never used.**
      `Vig` — a wide book is a less informative de-vig, so bias plausibly scales with it.
      `OracleDepth` (Pinnacle `max_contracts`; 2,387-3,433 on tennis 2026-08-22) — a line with high limits
      is one they are confident in. Both are testable from data already on disk once settlements land.
- [ ] **5. Raise `EV_MIN`.** Crude, and it fights the measured distribution — only **0.9%** of taker windows
      exceeded 3.5c, so a threshold set above the bias nearly eliminates volume. Last resort.

### The principle for tomorrow: INSTRUMENT, DO NOT TUNE
With 16 settled signals, tuning any of the above overfits spectacularly — and the tennis drift may not even
happen (it is currently -0.120 +/- 0.166, i.e. undetermined). **Add the power-method estimate and a
market-shrunk estimate as LOGGED COLUMNS that do NOT drive the decision.** Then when the settlements arrive,
compare four Brier scores and pick — instead of choosing a fix now and discovering in a fortnight it was the
wrong one. Roughly an hour in `DeVig.cs` plus two telemetry columns.

### The decision rule this is all feeding
If tennis signal bias converges toward **zero** as the market count grows, the strategy is real and the
sport was the fix. If it drifts toward soccer's **-0.23**, the same selection effect is present in tennis
too and the sport was never the fix — at which point levers 1 and 2 are the only ones that address the
actual mechanism.
