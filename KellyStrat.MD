# Kalshi +EV Taker Bot (Strategy Specification)

> **Direction changed 2026-08-21: TAKER, not maker.** The original draft specified resting maker orders.
> That is still the higher-EV route on paper, but it buys its edge with fill-rate uncertainty and adverse
> selection, and it requires winning a cancel-versus-fill race we have no way to measure yet. Taker gives up
> ~2.3c per contract and gets certain fills, no resting-order adverse selection, and no cancel/replace logic.
> The maker variant is kept in §7 because it is where this should go once the taker version has settled data.

## 1. System Objective
An automated +EV value-betting bot on Kalshi, using Pinnacle as the fair-value oracle.

*   **The Oracle:** Pinnacle's live WS odds, de-vigged, are ground truth.
*   **The Execution:** Cross the spread on Kalshi — take the ask, immediately, via API.
*   **The Risk Model:** Bayesian shrinkage on Pinnacle's live vig, plus simultaneous Kelly damping.
*   **No Hedging:** Nothing is placed on Pinnacle. Pinnacle is a price source, not a venue.

### Why this replaces the arb bot
The arb required `kAsk + pAsk < 1` — the Kalshi price had to beat Pinnacle's **vigged** price. A +EV bet
only requires it to beat Pinnacle's **fair** price, `kAsk < 1 − pAsk/S`. Since `S > 1`, **every arb is also
a +EV bet, and +EV is the strictly looser condition.** Measured over 397 windows carrying REST prices on
both sides (2026-08-20/21):

| | count | rate |
|---|---|---|
| REST-confirmed arb (`kAsk + pAsk + fee < 1`) | 23 | 5.8% |
| +EV as taker vs de-vigged Pinnacle | 70 | **17.6%** |
| arbs that are also +EV | 23 | **100% of arbs** |

Three times the opportunities, and the two-leg execution problem — camping, slip re-quotes, the ~12s
round trip, login fragility — disappears entirely. What is given up is that an arb was **risk-free** and a
value bet is not: it is positive expectation with variance, and only settlement data can confirm it.

---

## 2. Measured inputs (2026-08-20/21, ~400 windows)
Recorded so these are not re-derived or misremembered. All fee-inclusive.

*   **Pinnacle vig (two-way sum `S`):** median **1.0345**
*   **Kalshi bid-ask spread:** p10 1c · median **1c** · p75 1c · p90 2c
    *   Consequence: on a 1c spread there is no room to post inside it. A maker quote joins the bid.
*   **Pinnacle price accuracy vs REST:** median **+0.00c** error, wrong 7% of the time — Pinnacle is exact.
*   **Kalshi WS ask vs Kalshi REST ask:** median **+4c optimistic**, worse 95% of the time.
    *   **Therefore: never price off the Kalshi WS book. REST is the price.** This is what made the arb
        bot chase phantoms — 84% of its detections were not there at REST.
*   **EV distribution, taker, 0.20–0.80 window, full fee:** median −2.09c, **+EV 17.6%**, >1c 7.2%, >3.5c 0.9%
    *   The negative median is the population, not the strategy — it is the cost of taking every price
        indiscriminately. The strategy is the selected tail.

---

## 3. Core Loop
1.  **Ingest oracle:** live decimal odds from Pinnacle WS (via the sidecar reader).
2.  **Fair value:** de-vig to `P_true`; record overround `V = S − 1`.
3.  **Fetch Kalshi price: REST, not WS.** The WS ask is 4c optimistic and must not price a trade.
4.  **Price the order:** `P_exec = Best_Ask` — we cross, deliberately.
5.  **EV:** `EV = P_true − (P_exec + Taker_Fee)`; require `EV ≥ EV_MIN` (§4).
6.  **Size:** Full Kelly → Bayesian shrinkage → simultaneous damping (§5).
7.  **Risk check, then IOC:** hard caps, then an immediate-or-cancel taker order.

---

## 4. Fair Value, Fee and Threshold

### A. De-vigging
Primary (proportional): `q1 = 1/O1`, `q2 = 1/O2`, `S = q1 + q2`, `P_true = q1/S`, `V = S − 1`.

**Log a Shin de-vig alongside it and record both.** Proportional de-vig is known to overstate favourites
and understate longshots, and `P_true` *is* the edge — a bias here is not a rounding error, it is the
strategy being wrong in a direction. The difference between the two methods is a free running measure of
how much the choice is contributing.

### B. Taker fee — the dominant cost
`Fee = 0.07 · P · (1 − P)`, charged in full. The arc peaks at the money:

| P | 0.05 | 0.10 | 0.20 | 0.35 | 0.50 |
|---|---|---|---|---|---|
| fee | 0.33c | 0.63c | 1.12c | 1.59c | **1.75c** |

Against a 1–2c edge this is the single largest line item, and it is **larger near 0.50 than at the wings**.
The 0.20–0.80 operational window is therefore a trade-off, not a free filter: it avoids longshot de-vig
error and pays up to 1.75c for the privilege. Worth revisiting with settlement data.

### C. Threshold
`EV_MIN` is a **calibration decision, not a constant.** Measured taker rates:

| `EV_MIN` | share of windows |
|---|---|
| > 0 | 17.6% |
| > 1c | 7.2% |
| > 2c | ~3% |
| > 3.5c | **0.9%** |

The original 3.5% was set for the maker model, where it admitted 6–7%. As a taker it admits **under 1%**
and the bot would essentially never fire. Start at **1c** for observation, and set the live value from
settlement results — not from this table, which is detection-time data on a biased sample.

---

## 5. Sizing
1.  **Full Kelly:** `f = (P_true − P_cost) / (1 − P_cost)` where `P_cost = P_exec + Fee`.
    *   Use the fee-inclusive cost in **both** EV and Kelly. The first draft used bare `P_quote` here and
        fee-inclusive cost in EV, which over-sizes slightly and compounds.
2.  **Bayesian shrinkage:** `alpha = max(0.10, 0.35 · (1 − V/0.08))`.
    *   At the measured `V = 0.0345` this gives **alpha ≈ 0.20** — fifth-Kelly. Sound, and it degrades
        correctly as Pinnacle's vig widens (a wider vig means a less certain oracle).
3.  **Simultaneous damping:** `E_active = open exposure / bankroll`;
    `beta = 1.0` if `E_active ≤ 0.10`, else `max(0, 1 − (E_active − 0.10)/0.20)`.
4.  **Final:** `Target = Bankroll · min(0.03, f · alpha · beta)`; `Contracts = floor(Target / P_exec)`.
    *   Watch `floor()` at small bankrolls: it silently rounds a valid signal to **zero contracts**. Log
        when that happens rather than skipping in silence.

---

## 6. Risk Guardrails
1.  **Single trade cap:** never above **3%** of bankroll.
2.  **Correlated event cap:** per `Event_ID`, reject if `existing + new > 6%` of bankroll. Both sides of one
    match are the same risk, not two.
3.  **IOC only.** Immediate-or-cancel taker orders. No resting orders, therefore **no cancel/replace logic
    at all** — a whole class of races removed versus the maker draft.
4.  **Price re-check inside the order:** the limit is `Best_Ask`; if the book has moved, the IOC simply
    does not fill. That is the correct outcome and needs no separate guard.
5.  **Circuit breaker:** if 24h PnL ≤ **−15%**, cancel everything, stop the loop, log CRITICAL.
6.  **Stale-oracle guard:** if the Pinnacle price backing `P_true` is older than N seconds, do not fire.
    A value bet against a stale oracle is a bet against nothing.

---

## 7. Deferred: the maker variant
Once settlement data exists, revisit. Measured maker economics (pay the bid, fee ×0.25): median **+0.22c**,
**+EV 54.5%**, >3.5c 6.1% — roughly **3× the taker's opportunity rate**. The cost is:

*   **Adverse selection.** A resting bid fills when someone chooses to sell to you; on a market where
    Pinnacle is the oracle, informed flow is disproportionately on the other side. Realised EV will be
    below quoted EV, systematically.
*   **Fill rate is unknown.** 25 quotes/day filling 3 times is a different business to filling 20.
*   **The cancel-versus-fill race.** You lose it exactly when it matters most.

None of this is disqualifying — but all of it needs measurement the taker version will produce for free.

---

## 8. Milestones

**M0 — Observation only. Build this first.**
No orders. Scan pairs continuously, compute `P_true` (both de-vig methods), fetch the Kalshi REST ask,
compute EV and Kelly size, and **log what it would have done**. Reuses the sidecar, the WS reader,
`cross_pairs.json`, `KalshiOrderClient` and the telemetry CSV pattern.

**M1 — Settlement validation. Build this second, before any money.**
Resolve every logged signal against the Kalshi settlement and compare realised outcomes to `P_true`.
*This is the only thing that can tell a wrong model from bad luck.* An arb self-verified at fill; a value
bet does not verify for hundreds of settlements. Without M1 the temptation is to intervene on noise.

Two questions M1 answers and nothing else can:
*   Is Pinnacle's line actually predictive **on ITF/challenger tennis**? The sharp-book assumption is
    established for major markets; these are small events where Pinnacle's own limits are €500–2000.
*   Is calibration flat across the 0.20–0.80 range, or is one end carrying all the error?

**M2 — Live, minimum size.** Only after M1 shows calibration holding over a meaningful sample.

**M3 — Sizing up, then the maker variant.**
