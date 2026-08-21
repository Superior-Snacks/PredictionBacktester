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

### Running it
```
dotnet run --project KalshiEvBot -- --self-test     # offline, safe any time
dotnet run --project KalshiEvBot -- --check         # validates pairs, opens nothing
dotnet run --project KalshiEvBot -- --once          # needs the sidecar up AND the arb bot stopped
dotnet run --project KalshiEvBot -- --book-audit 15
dotnet run --project KalshiEvBot                    # the M0 session
```
Not yet exercised against live feeds — that needs the sidecar running and the arb bot's Kalshi WS released.

---

## 2. M1 — Settlement validation — **TOOLS BUILT 2026-08-21**

Run with `dotnet run --project KalshiEvBot -- --resolve`. **REST only, no WebSocket**, so it is safe to run
while another bot holds the account's single socket.

- [x] **Resolver** (`SettlementResolver.cs`) — fetches `status`/`result` per ticker, caches to
      `ev_settlements.json`. Field shape verified live: `status` is `"active"`/`"finalized"`, `result` is
      `""` until final then `"yes"`/`"no"`. **Only finalized results are cached** — caching "active" would
      freeze a market as never-settled and quietly shrink the sample forever, which is the one failure a
      calibration report cannot detect from its own output.
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
