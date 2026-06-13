# HardVenArb — HardVen (sportsbook) side: implementation TODO

Status: **scaffold only.** The Kalshi side + executor + telemetry are cloned and build clean. The
HardVen venue is an open stub (`IHardVenOrderExecutor` / `HardVenOrderClient` / `HardVenWebsocketFeed`).
This doc is the plan to make the HardVen side real.

## Vision (POC)
Kalshi ↔ **one** regular sportsbook. Observe the book's odds via a **web scraper**, detect when a
cross-arb opens vs Kalshi, **place the bet programmatically**, and confirm by **observing the account
("wallet")**. **Pre-match only** to start — the sportsbook side is slow, so only pre-live odds are
stable enough to round-trip an arb.

---

## 0. Five facts that shape the whole design

1. **Odds, not an order book.** A sportsbook posts decimal odds `O` per outcome (+ a max stake), not a
   bid/ask ladder. Map to the executor's price model: **price per $1-payout contract = `1/O`** (implied
   probability). Net cost per matched set = `kAsk + (1/O_hardven) + KalshiFee`. Arb when `< 1.0`.
2. **No fee API.** The bookmaker's margin (vig/overround) is already inside the odds, so **`fee = 0`**
   on the HardVen side. (`GetTakerFeeAsync→0`, `GetFeeParamsAsync→(0,1)`.)
3. **Back-only and IRREVERSIBLE.** On a normal sportsbook you cannot sell/cancel a placed bet (no lay).
   ⇒ two hard consequences: (a) **leg ordering inverts** — place HardVen first, confirm, then Kalshi;
   (b) **recovery can never "reverse" a HardVen leg** — only complete-on-Kalshi or hold to settlement.
4. **Slow + scraped.** Odds via polling (seconds), not a WebSocket. ⇒ **pre-match only**, loosened
   staleness, and the arb edge must be wide enough to survive observe→place→confirm latency.
5. **Settlement is post-match, off "wallet".** No 0/1 token balance — the bet wins or loses after the
   match and the account balance changes. Confirmation = bet appears in "My Bets" + balance moved.

---

## 1. Recommended architecture: scraper sidecar + thin C# clients

Keep the fragile browser automation OUT of the trading core.

- **Python sidecar service** (Playwright/Selenium) — owns: logged-in session, odds polling, bet-slip
  placement, balance + open-bets reads. Exposes a tiny **local API** (HTTP and/or a local WS):
  - `GET  /odds?selections=…`        → `{selectionId: {decimalOdds, maxStake, ts}}`
  - `POST /bet {selectionId, stake, maxOdds}` → `{accepted, actualOdds, stake, betId}` or `{rejected, reason}`
  - `GET  /balance`                  → account cash
  - `GET  /bets/{betId}` / `GET /bets/open` → confirmation + settlement status
- **C# clients just call the sidecar** (so the executor stays unchanged):
  - `HardVenWebsocketFeed` → poll `/odds`, push into `"H:"` books.
  - `HardVenOrderClient` → the 8 methods, each a sidecar call.
- *Alternative:* all-C# via Playwright-for-.NET (one process, but mixes scraping with trading — not
  recommended for the POC).

---

## 2. TODO by component

### A. Pairing system (semi-manual — accept it won't fully automate)
- [ ] **Schema** for `cross_pairs.json` (HardVen variant). Fields per pair:
      `kalshi_ticker`, `hardven_book`, `hardven_event_id` (or URL), `hardven_market`, and the
      **YES/NO → selectionId mapping** (which book selection = Kalshi YES vs NO), plus
      `match_start_time` (UTC, for the pre-live gate) and `label`.
- [ ] **Pairing helper** (Python): given a Kalshi event, query the book's catalog/search and fuzzy-match
      candidate events/markets/selections; present to a human to confirm + record. (Mirror the spirit of
      `pair_markets_v2.py` but for the sportsbook's taxonomy — the Poly Gamma pairing does NOT transfer.)
- [ ] **Outcome-equivalence checks** (manual review): same event, same resolution rule, same YES/NO sense
      (e.g. Kalshi "X wins the title" ↔ book "Outright winner → X"). Mismatched rules = silent loss.
- [ ] Decide handling of book markets with no Kalshi equivalent and vice-versa.

### B. Scraper sidecar service
- [ ] **Pick the POC sportsbook.** Criteria: a scrapable site or semi-stable internal JSON endpoint;
      decent pre-match markets that overlap Kalshi (politics/sports futures/season-long); fundable account;
      tolerable ToS/automation risk; not aggressively bot-protected.
- [ ] **Session management:** login, cookie/session persistence, 2FA, CAPTCHA strategy, geo/IP. Keep-alive
      + re-login on expiry.
- [ ] **Odds polling:** for each subscribed selection, poll every N seconds; parse → `decimalOdds` +
      `maxStake` + timestamp. Convert American/fractional → decimal if needed. Detect "market suspended".
- [ ] **Bet placement:** fill the bet slip for `{selectionId, stake}`, submit, **handle the "odds changed —
      accept?" dialog** (accept only if still ≤ `maxOdds`), capture the confirmation (actual odds, stake,
      bet id). Return accepted/rejected.
- [ ] **Balance + open-bets reads** for confirmation and settlement.
- [ ] **Guardrails:** serialize bets (one browser session), hard max-stake cap, global kill switch, dry-run
      mode that places nothing.

### C. C# HardVen clients (fill the 4 stubs)
- [ ] `HardVenWebsocketFeed` → poll the sidecar `/odds`; for each selection push a single-level book into
      `"H:{selectionId}"`: **ask price = `1/decimalOdds`**, **ask size = `maxStake × decimalOdds`**
      (= max contracts). Call `_telemetry.OnBookUpdate`. **Loosen the staleness guard** for pre-match cadence.
- [ ] `HardVenOrderClient.SubmitOrderAsync` → convert contracts↔stake (`stake = contracts / O`), POST
      `/bet`, return a response the executor can parse as a fill (mirror the JSON shape the executor expects,
      or adapt the executor's HardVen-fill parsing).
- [ ] `GetUsdcBalanceAsync` → `/balance`. `GetTokenBalanceAsync`/`GetOrderAsync` → `/bets` (open-bet
      confirmation = your "wallet" check). `UpdateBalanceAllowanceAsync` → no-op (or refresh cache).
- [ ] `GetTakerFeeAsync→0`, `GetFeeParamsAsync→(0,1)`, `GetTickSizeAsync→` nominal/odds-step.

### D. Executor changes (the non-trivial work — `CrossArbExecutor.cs`)
- [ ] **Invert leg ordering for HardVen:** place the **HardVen bet first** (slow, irreversible), await
      confirmation of the *actual accepted odds/stake*, **then** fire the Kalshi leg sized to it. (Today the
      executor fires Kalshi first then the second venue — gate this behind a HardVen path / flag.)
- [ ] **No-reverse recovery:** a naked HardVen bet **cannot be sold**. Rework `RecoverUnhedgedAsync` so the
      HardVen-excess case can only **complete the hedge on Kalshi** or **hold to settlement** — never
      "reverse HardVen". (The Kalshi-excess case can still reverse Kalshi, as today.)
- [ ] **Stranded-directional policy:** if Kalshi fails *after* the HardVen bet is placed, you hold a
      directional sportsbook position you can't unwind. Define + journal + Discord-alert the policy
      (retry Kalshi hard / accept the directional bet / size so this is tiny).
- [ ] **Sizing:** contracts ↔ stake via odds; respect the book's **min and max stake** (the new depth cap).
- [ ] **Pre-live gate:** only execute when `now < match_start_time` for the pair; skip once in-play
      (reuse the `--wN` gate pattern, but keyed on match start, not settlement date).
- [ ] **Odds slippage:** after the HardVen bet confirms at `actualOdds`, recompute the net with the real
      price before firing Kalshi; if the arb evaporated, you're already committed to HardVen → fall to the
      stranded-directional policy (so keep stakes conservative).
- [ ] **Settlement/reconcile:** add a HardVen settlement path — bets resolve win/lose post-match and the
      balance moves; there is no token-balance-goes-to-0/1. The reconcile + settlement sweeper need a
      HardVen-aware branch (the current Poly token-balance reconcile won't apply).

### E. Milestones (ship in this order)
- [ ] **M0 — Observe-only (`--telemetry`).** Sidecar feeds odds → `"H:"` books → bot detects & logs arb
      windows to CSV/journal. **No betting.** Validates the `1/O` pricing, the pairings, and *that pre-match
      arbs actually appear*. Zero financial risk — do this first and let it run.
- [ ] **M1 — Tiny-stakes live.** One book, a handful of hand-verified pairs, leg-ordering + confirmation +
      guardrails, **$1 stakes**. Validate end-to-end (place → confirm → Kalshi → settle). Optionally start
      with *alert-human-to-place* before full auto-place.
- [ ] **M2 — Harden.** No-reverse recovery, HardVen settlement, account-limit/ban handling, slippage
      tuning, then multi-book "rotation".

### F. Risks / operational (don't skip)
- **Account limiting / bans:** sportsbooks stake-limit or ban consistent arbers — expect it; plan for
  rotation and modest sizing.
- **ToS / legality** of scraping + automated betting in your jurisdiction.
- **Slippage & latency:** odds can move between observe and accept; pre-match + wide edge mitigates.
- **CAPTCHA / 2FA / geo** in the session.
- **Single-session serialization:** one logged-in browser → bets must queue.
- **Funding/withdrawals are manual.**

---

## 3. Open decisions (answer before M1)
- Which sportsbook for the POC?
- Sidecar in Python/Playwright (recommended) or all-C#?
- M1: auto-place the bet, or alert-a-human-to-place while auto-firing Kalshi?
- Stranded-directional policy when Kalshi fails after the bet is placed?
- Stake unit + max, and the minimum net edge required to bother (must exceed latency/slippage risk).
