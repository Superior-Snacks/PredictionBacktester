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

### A. Pairing system (mostly automatable — AI judge + empirical settlement gate)
Reuse `pair_markets_v2.py`'s machinery (unified index, BM25/spaCy/embedding signals, LLM judge with
confidence routing). Name/event matching is the SAME problem as Kalshi↔Poly. The ONE part NOT to trust
the AI on is **resolution-rule equivalence** — a wrong pairing here is a real-money loss (the two venues
settle the same event differently), not a missed opportunity. So layer the automation with gates that
do not depend on the AI being right.

- [x] **Interim manual scaffolder `pairHard.py`** (DONE 2026-06-16): fetches Kalshi open events (public
      API, no auth), allowlists classic full-match 2-way WINNER series (`KXMLBGAME`/`KXNFLGAME`/tennis
      `KX*MATCH`/`KXUFCFIGHT`/…; Sports category is mostly props/futures/esports so an allowlist is
      required), writes `cross_pairs.json` with the Kalshi side filled + `hardven_*_token` BLANK for
      hand-pairing (bot skips blanks). Knobs: `CLASSIC_SERIES`, `--series`, `--days`, `--all`. The
      automated AI-judge pipeline below supersedes this once the scraper `catalog()` exists.
- [ ] **Schema** for `cross_pairs.json` (HardVen variant): `kalshi_ticker`, `hardven_book`,
      `hardven_event_id` (or URL), `hardven_market`, the **YES/NO → selectionId mapping**,
      `match_start_time` (UTC, pre-live gate), `label`, plus a **trust status** (observe-only | trusted |
      blocklisted) and a settled-event count.
- [ ] **Catalog enumeration (prereq):** the scraper must list the book's FULL catalog
      (sport/league/event/market/selection/start-time/odds) and, ideally, per-market **rules text or a
      rules URL**. No comprehensive catalog ⇒ nothing to auto-match against; no rules text ⇒ the AI can't
      catch settlement mismatches, so you lean harder on the settlement back-test below.
- [ ] **Candidate generation (auto):** embedding/BM25/entity-match each Kalshi market → top-K book
      selections (reuse the v2 index + signals). Cheap; narrows thousands → a few per Kalshi market.
- [ ] **AI judge (auto):** per candidate, feed BOTH sides' full context (titles, outcome/selection names,
      **rules text**, dates, sport/league) and ask: *do these resolve to the SAME outcome under the SAME
      conditions?* Output = verdict + confidence + the YES/NO↔selectionId mapping + explicit
      **rule-mismatch flags** (settlement source, void/push, OT vs regulation, futures cutoff, dead-heat,
      postponement). Same confidence routing as v2: high + no flags → candidate pool; medium → review
      queue; low / any rule flag → reject.
- [ ] **Settlement back-test gate (the trust mechanism — auto):** every auto-paired market starts
      **observe-only** and is NEVER bet until proven. After its markets settle, auto-compare: did Kalshi
      and the book resolve to the SAME outcome? Resolved-identically across N past events → promote to
      **trusted** (bettable). A single divergence → auto-blocklist. This trusts observed settlement
      history, not the AI — it's what makes "automatic pairing" safe with real money.
- [ ] **Price-sanity gate:** reuse `_minPlausibleNet` — a "too good" edge flags a likely rule mismatch,
      not free money.
- [ ] Human review handles only the medium-confidence queue + first-time book-taxonomy quirks.

### B. Scraper sidecar service
**✅ Skeleton scaffolded at `HardVenArb/sidecar/`** (2026-06-16): FastAPI app (`app.py`), the
`BookAdapter` contract (`book_adapter.py` — the abstract methods ARE the checklist), a working
`MockBookAdapter` (`mock_adapter.py`), `requirements.txt`, `README.md`. Runs today against the mock
(`HARDVEN_BOOK=mock uvicorn app:app --port 8787`). The remaining items below are about the REAL adapter:
- [x] **POC sportsbook = bookmaker.eu** (chosen 2026-06-16). Bankroll too small for a broker/API book.
- [x] **STOMP odds PARSER — DONE 2026-06-16.** bookmaker.eu streams odds as RabbitMQ Web-STOMP frames
      (feed creds login/passcode=rtweb, host=WebRT). `sidecar/bookmaker_stomp.py` = frame + payload parser
      (`parse_stomp_frame`; `parse_markets`: m=moneyline, s=spread, t=totals; American→decimal; primary
      line i==0). VERIFIED against a live sample (-1603→1.0624, +779→8.79, spread/total all correct). It
      also has a standalone `StompOddsClient` (CONNECT→CONNECTED→SUBSCRIBE loop) — see the ❌ below.
- [x] **❌ Raw-socket odds DEAD END (Cloudflare, confirmed 2026-06-17).** Opening our OWN websocket to
      `RealTimeHandler.ashx` from Python is blocked: bookmaker.eu is behind Cloudflare bot-management, and
      `cf_clearance` is bound to the browser's **TLS/JA3 fingerprint** (not just IP+UA+cookie). Python's
      stdlib-`ssl` handshake ≠ Chrome ⇒ Cloudflare returns an HTTP **200 managed-challenge** instead of the
      101 upgrade, *even with a valid cookie/UA/Origin copied from the browser*. No header-matching fixes
      this. (`bookmaker_stomp.py`'s `__main__` smoke test now adds browser headers + a `_dump_handshake_failure`
      diagnostic — kept for reference, but the standalone client is not the odds path.)
- [x] **★ PRE-MATCH ODDS = `GetGameView` HTTP — DECIDED + PARSER DONE 2026-06-17. This is the POC path.**
      Confirmed empirically: a next-day tennis match does NOT stream over the WS (the WS flood was a *live*
      game's in-play deltas). Opening a match fires a **`GetGameView` XHR** returning a clean JSON board
      snapshot — so for a **pre-match-only POC we just POLL `GetGameView`** every N s (this IS the "odds
      polling" milestone). Parser `sidecar/bookmaker_gameview.py` (`parse_gameview`): main match-winner =
      `game` where `idgm == famGame`; primary line `index=="0"` → `hoddst`/`voddst` (home/visitor American
      moneyline) + `htm`/`vtm` names; `descgmtyp`→max stake; `gmdt`+`gmtm`→start; `MoneyLineStatus`/
      `!MarketsClosed`/`!FreezeMoneyLine`→tradeable. Emits `"<idgm>:H"`(home)/`"<idgm>:V"`(visitor) with
      `decimal_odds`/`implied_price=1/dec`/`max_stake`. VALIDATED vs the real Paul,Tommy(-333→1.3003) vs
      Van de Zandschulp(+256→3.56) capture (vig 1.05; derivatives/props correctly ignored). Fixture:
      `sidecar/gameview_sample.json`. **✅ WORKING END-TO-END (2026-06-17):** `bookmaker_adapter.py` polls
      `POST be.bookmaker.eu/gateway/BetslipProxy.aspx/GetGameView` (body picks the game by `GameId`+`LeagueId`)
      from INSIDE real Chrome via `page.evaluate(fetch, credentials:'include')`. Needed: (a) real Chrome
      (`channel="chrome"`) + stripped automation flags — bundled Chromium gets API-route 403s (bot-detection);
      (b) the `rtqname` session header, auto-captured live from the site's traffic (`context.on("request")`;
      it ROTATES per session). Confirmed: polled an arbitrary pre-match game and got correct odds with NO
      navigation to it. Response-interception (`context.on("response")`) kept as a backup. Selection id =
      `"<GameId>:<LeagueId>:H|V"`.
- [x] **ODDS via Playwright CDP frame-sniff — DONE 2026-06-17, but for IN-PLAY only (out of POC scope).**
      `bookmaker_adapter.py` launches Chrome, attaches CDP, reads `Network.webSocketFrameReceived` (the
      `realtimehandler` socket), runs frames through `parse_stomp_frame`/`parse_markets` → cache. This is
      the way past Cloudflare for the *streaming* (live) feed, but pre-match (the POC) uses `GetGameView`
      above. NOTE: `parse_markets` keys by `lid` which COLLIDES on the live structure (lid=league; the
      unique market is `gid`, main line = `gid==pid`) — needs the gid/pid rework before in-play is usable.
- [ ] **Recon (per session):** the browser binds its own session/queue, so no hand-grabbing
      `BOOKMAKER_WSS_URL`/`BOOKMAKER_STOMP_QUEUE`. Remaining: capture the `GetGameView` request, and map
      each game's `idgm` → its Kalshi market in cross_pairs.json (manual until `catalog()` exists).
      `BOOKMAKER_WSS_URL`/`QUEUE`/`WS_COOKIE` now only feed the standalone STOMP smoke test (in-play).
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
- [x] `HardVenWebsocketFeed` (DONE 2026-06-16) — now an HTTP poller of the sidecar `/odds` (book-agnostic):
      reads `decimal_odds`/`max_contracts`/`status`, pushes a single-ask snapshot into `"H:{id}"`
      (price = `1/decimalOdds`, size = `max_contracts`) via `ProcessBookUpdate` (clears stale levels),
      `MarkDeltaReceived` + `_telemetry.OnBookUpdate`. Suspended/missing → empty ask (no arb). Program.cs
      points at `HARDVEN_SIDECAR_URL` (env, default `http://127.0.0.1:8787`). Builds clean; sidecar `/odds`
      shape verified to match. Poll cadence = `HARDVEN_PING_INTERVAL_MS` (9 s).
- [ ] `HardVenOrderClient.SubmitOrderAsync` → convert contracts↔stake (`stake = contracts / O`), POST
      `/bet`, return a response the executor can parse as a fill (mirror the JSON shape the executor expects,
      or adapt the executor's HardVen-fill parsing).
- [ ] `GetUsdcBalanceAsync` → `/balance`. `GetTokenBalanceAsync`/`GetOrderAsync` → `/bets` (open-bet
      confirmation = your "wallet" check). `UpdateBalanceAllowanceAsync` → no-op (or refresh cache).
- [ ] `GetTakerFeeAsync→0`, `GetFeeParamsAsync→(0,1)`, `GetTickSizeAsync→` nominal/odds-step.

### D. Executor changes (the non-trivial work — `CrossArbExecutor.cs`)
**Keep ONE execution core — drive the differences off venue CAPABILITIES, not venue identity.** The
private-API/`IHardVenOrderExecutor` interface makes the *calls* uniform (great), but it CANNOT hide
behavioral differences: you genuinely can't un-place a sportsbook bet, so the sidecar must not fake a
"reverse" (returning fake success would leave the bot exposed while thinking it flattened). So have the
venue advertise a small **capability descriptor** the executor reads — e.g. `CanReverseFills`
(exchange=true, book=false), leg priority / `IsAnchorLeg` (place-first vs place-second), settlement model
(token-balance-→0/1 vs bet-wins/loses-and-balance-moves), min/max size. The executor branches on these
flags, so the SAME script handles a reversible exchange (Poly) and an irreversible book (HardVen), and a
future venue (e.g. a Betfair-style exchange, which IS reversible) drops in by declaring capabilities — no
executor edits. Add a `VenueCapabilities` to `IHardVenOrderExecutor` (or a `GetCapabilities()`) and make
the items below read it rather than hard-coding "HardVen":
- [ ] **Leg ordering by capability:** the non-reversible / anchor leg places **first** (HardVen: slow,
      irreversible), await
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
- ~~Which sportsbook for the POC?~~ → **bookmaker.eu** (browser path; STOMP odds).
- Sidecar in Python/Playwright (recommended) or all-C#? → Python sidecar in progress.
- M1: auto-place the bet, or alert-a-human-to-place while auto-firing Kalshi?
- Stranded-directional policy when Kalshi fails after the bet is placed?
- Stake unit + max, and the minimum net edge required to bother (must exceed latency/slippage risk).

## 4. Market structures that DON'T fit the 2-way binary arb (adapt later)
The bot pairs ONE Kalshi binary market (YES/NO) with ONE bookmaker 2-outcome selection — it only works
when the two legs are exact **complements** (exactly one pays out). That holds for **2-outcome match
winners** (MLB, tennis, UFC, boxing, NBA, NFL, NHL, WNBA, KBO, NCAAF/B) — the current scope, and what
`pairHard.py` already filters to. Deferred, by structure:

- [ ] **3-way / draw markets — soccer & football (1X2 match result).** Home/Draw/Away means Kalshi
      "Team A wins" YES and the book's "Team A" moneyline are the SAME outcome, not complements (both
      lose on a draw), so a single 2-way pair can't arb it. Adapt later via **either** (a) **multi-leg** —
      hedge Kalshi "A wins" by backing BOTH Draw and B at the book (cover all 3 outcomes), **or** (b) pair
      Kalshi-binary against the book's **Double Chance / Draw-No-Bet** 2-way market (a clean complement,
      if bookmaker.eu offers it AND Kalshi's resolution rule matches). Model the multi-leg version on the
      sibling bot's `PolymarketCategoricalArbStrategy` ("buy all legs when Σcost < $1").
- [ ] **Multi-runner outrights / futures / props** (championship winner, draft pick, first/anytime
      goalscorer, player props): N outcomes → multi-leg, and only where Kalshi lists the matching market.
      bookmaker.eu's `TNT.*` feeds are these (parser already ignores them).
- [ ] **Totals (over/under) & spreads/handicaps** (`mkt.t` / `mkt.s`): these ARE 2-outcome, so they fit
      the binary model — the only blocker is **exact line-matching** (Kalshi's total/spread line must
      equal the book's). **This is the easiest future extension** (no multi-leg): add the matching Kalshi
      total/spread series to `pairHard.py`, and surface `mkt.t`/`mkt.s` as selections (the STOMP parser
      already extracts them).

**Order of difficulty:** 2-way match winners (now) → totals/spreads (next-easiest; 2-way + line-match) →
3-way + multi-runner (a bigger change; needs multi-leg arb like `PolymarketCategoricalArbStrategy`).
