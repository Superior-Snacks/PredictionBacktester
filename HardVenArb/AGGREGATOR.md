# Odds aggregator integration

Target architecture: **the aggregator finds the arb, the book's UI places it.** Detection moves off the
multi-tab Pinnacle WS onto a vendor feed covering many sharp books; placement, balance and bet monitoring stay
on the existing Playwright path.

## How it plugs in

Two plug-points, deliberately separate:

| Contract | File | What it is |
|---|---|---|
| `BookAdapter` | `sidecar/book_adapter.py` | a venue we can **bet** at (odds + catalog + balance + place_bet + bets) |
| `AggregatorClient` | `sidecar/agg_client.py` | a feed we can only **read** (quotes, nothing else) |

`sidecar/aggregator_adapter.py` composes them: quotes from the client, money through the inner `BookAdapter`.
Everything else (`verify_now`, `session_status`, `find_bet`, `_bet_lock`, …) delegates to the inner book via
`__getattr__`. **The C# bot is unchanged** — it still polls `GET /odds` and gets `price = 1/decimal_odds,
size = max_contracts`. No executor, telemetry, pairing or gate code moves.

```
HARDVEN_BOOK=aggregator                 turn the composite on
HARDVEN_AGG_PROVIDER=mock               which vendor client (agg_client.load_client)
HARDVEN_AGG_PLACEMENT_BOOK=pinnacle     which book actually takes the bet
HARDVEN_AGG_MODE=shadow|live            shadow (default) = serve the book, log the vendor
HARDVEN_AGG_LIMITS=inner|vendor|assumed where max_stake comes from in live mode (default inner)
HARDVEN_AGG_TS_POLICY=strict|fetch      what to do when the vendor publishes no per-line update time
HARDVEN_AGG_SHADOW=1                    write AggShadow_*.csv (default on)
```

Adding a vendor = one `AggregatorClient` subclass + a line in `load_client()`. Nothing else.

## Proving a vendor before trusting it

**Shadow mode** serves the Pinnacle WS quotes you already trust and logs the vendor's alongside, same tokens,
same instants. Run it through a real slate, then:

```bash
python analyze_agg_shadow.py            # newest AggShadow_*.csv
```

Four sections, in dependency order — a failure at any level makes the next moot:

1. **Coverage** — does it quote the tokens we watch?
2. **Agreement** — when both quote, do they agree once settled? (Measured at the end of each constant-book-price
   epoch. The tape is change-driven, so raw per-row agreement understates a good vendor by ~half — it samples
   mostly mid-transition. The analyzer prints both and judges on the settled figure.)
3. **Follow lag** — book moves, how long until the vendor shows it? **The number that decides whether an
   aggregator can drive detection at all.** Resolution is ~one sidecar poll.
4. **Freshness + limits** — per-line update time young enough for the C# staleness gate; limits published or not.

The analyzer's own lag math is validated against a known answer: `MockAggregatorClient` mirrors the inner book
with an injected delay. 0s control → all PASS with median 1.01s (one poll, the measurement floor); 8s injected →
recovered as **8.07s median**, correctly FAILing follow-lag while PASSing agreement, i.e. diagnosing "accurate
but slow" rather than just "bad."

## What the vendor docs MUST answer

Ordered by how fast a bad answer kills the deal.

**1. Per-line update timestamp — HARD REQUIREMENT.**
Does each quote carry *when that line last changed at that book*? The C# feed gates on it
(`HARDVEN_QUOTE_MAX_AGE_MS`, default 30s) and clears the book when a quote ages out. That gate is the only thing
between a frozen vendor line and a phantom arb — the exact failure the WS-verify gate exists to stop. No
per-line clock ⇒ `HARDVEN_AGG_TS_POLICY=strict` ages every quote out instantly (safe, but the vendor is useless
for detection) and `fetch` stamps fetch time (usable, but the staleness gate is now blind). **Ask explicitly** —
many vendors return only a response-level timestamp, which is not the same thing.

**2. Update latency, and push vs poll.**
Arb windows in this book's own tape last *seconds*. A vendor polling its sources every 30–60s cannot find them
no matter how accurate it is. Ask for the source-poll interval per book, not the API's rate limit. Websocket
push beats REST polling here by more than the numbers suggest, because poll interval adds to source lag.

**3. Pinnacle coverage, and whether book-native IDs are exposed.**
This is the join, and it is where the per-vendor work actually lives:

```
kalshi_ticker  <->  BOOK token (221309:1633332341:home)  <->  vendor key
```

The Pinnacle token cannot be replaced — the UI clicks that row and `cross_pairs.json` pairs against it. So ask:
*do you expose the bookmaker's own event/market/selection ids, or only your normalized ids?* Native ids ⇒ the
map is mechanical. Normalized-only ⇒ you need a matching step per vendor (`agg_map.json`), which is the same
fuzzy-title problem `pair_pinnacle.py` already solves once.

**Pairing upside:** if the vendor publishes *normalized event ids across books*, that replaces title matching
for Kalshi↔book pairing too, and it's the thing that makes book #3 and #4 cheap. Worth weighting heavily.

**4. Market coverage: moneyline vs derivatives.**
`HARDVEN_MONEYLINE_ONLY=1` today because the UI can only place straight moneylines. An aggregator that also
carries spreads/totals doesn't change that — the *UI* is the constraint, not the feed.

**5. Limits / max stake.**
Most read-only aggregators publish none. Not fatal: `HARDVEN_AGG_LIMITS=inner` takes the vendor's price and the
book's own published limit from the WS. But note what that means — you're still dependent on the Pinnacle WS
for depth, so the feed doesn't fully replace it yet. `max_stake` drives `max_contracts` drives the stake
ladder's `MaxDepthFraction`; a fabricated limit produces a fabricated bet size.

**6. Suspended / closed status, in-play flag, cutoff time.**
Needed so a pulled market clears the book rather than serving its last price forever, and so the pre-live gate
(`HARDVEN_PRELIVE_ONLY=1`) works. Cutoff is rarer; the adapter falls back to the book's.

**7. Rate limits vs our shape.**
~322 pairs × 2 tokens = **~644 selections**, polled every ~9s. Ask whether that's one call or 644, and how it
counts against quota. Per-selection billing at this cadence is disqualifying on cost alone.

**8. Historical / replay access.**
If the vendor can serve past snapshots, you can backtest the strategy against its feed *before* paying for
live — and re-run the shadow comparison offline.

**9. Cost, and whether the licence permits automated betting on the output.**
Some odds APIs restrict commercial/automated use in terms of service.

## OddsPAPI — vendor #1 (docs reviewed 2026-08-05, `OddspapiDocks.txt`)

Client: `sidecar/agg_oddspapi.py` (`HARDVEN_AGG_PROVIDER=oddspapi`). Probe: `sidecar/oddspapi_probe.py` —
**run it first when the key lands** (~5 billable requests; `--account-only` for a free key check).

How the checklist came out:

| Question | Answer |
|---|---|
| Per-line update time | **YES** — `changedAt` per selection (+ `bookmakerChangedAt` when Pinnacle reports one). But it's a line-*change* clock, not a heartbeat — see below. |
| Push vs poll | REST polling (cooldowns 0.5–2s/endpoint). **WebSocket exists but is b2b-plan only** (`websocket_access: 0` on the docs' example plan). |
| Book-native ids | **YES — the jackpot answer.** `externalProviders.pinnacleId`/`bookmakerFixtureId` = our `mid`; `bookmakerOutcomeId` = Pinnacle's own designation (`"home"`, `"3.5/under"`). The token join is mechanical; **no `agg_map.json` needed.** Cross-book normalized ids (`fixtureId` + market/outcome ids) also exist → future Kalshi↔book pairing upgrade. |
| Markets | Moneylines + totals/spreads. Fulltime is discriminated by Pinnacle's own market path (`…/0/moneyline`, period 0) so 1st-set/period markets can't leak in. |
| Limits | **YES** — per-selection `limit`. Currency unstated → keep `HARDVEN_AGG_LIMITS=inner`. |
| Suspended / live / cutoff | `suspended` + `marketActive` + `active` (three levels); `startTime` → cutoff. `statusId` enums **conflict between two doc pages** (1=Live vs 1=Scheduled) — the client treats `now >= startTime` as the in-play signal. |
| Quota shape | 1 request = 1 call regardless of size; **`/v4/odds-by-tournaments` batches the whole slate into 1 request per poll.** Errors (4xx/5xx) bill too. `/v4/account` is unmetered (quota telemetry is free). |
| Historical | **`/v4/historical-odds` is always free** (data since Jan 2026) — backtest the feed before paying for cadence. |

**Freshness policy (the one place the generic contract needed vendor thinking):** stamping `ts = changedAt`
would age every *stable* line out of the C# 30s gate (stable pre-match lines are this bot's whole habitat);
stamping fetch time blindly would let a frozen scrape serve phantoms forever. The client serves **poll time
while the slate heartbeat is alive** — any watched selection's `changedAt` within `ODDSPAPI_HEARTBEAT_TTL_SEC`
(default 900) — else the stale `changedAt`, so a frozen scrape ages out. Same shape as
`pinnacle_adapter._feed_live()`.

**Quota math (measured live 2026-08-05, and it decides everything):** the API caps
`tournamentIds` at **5 per call** (400 `INVALID_PARAMETER` above that; discovered empirically — not in the
docs), so 1 poll = `ceil(active_tournaments / 5)` requests, ~3–4 on a typical slate even with the 48h horizon
filter. **The current key's plan is 250 requests/month total** — that is ~1 hour of minute-cadence shadow, or
~20 probe runs. It cannot support continuous polling of any kind. Options, in order of sense:
1. **Free evaluation via `/v4/historical-odds`** (never billed): capture our own Pinnacle WS tape during a
   slate, then pull the vendor's per-change history (`createdAt` per price move) for the same fixtures and
   diff the move times offline — the entire follow-lag + agreement measurement for **zero quota**.
2. **One deliberate shadow burst**: ~45 min at `ODDSPAPI_POLL_SEC=60` during a busy evening ≈ 150–180
   requests — a single real AggShadow tape, then the month's quota is spent.
3. **A paid tier for live mode**: even 60s cadence needs ~130k req/month at 3 chunks/poll. Price that (or the
   b2b WS) before planning any `live` switch.

The client backs off 30 min on `REQUEST_LIMIT_EXCEEDED`, honors per-endpoint cooldown 429s (`RATE_LIMITED`
retryMs — those are rejected pre-endpoint and don't bill), warns at 90% used, and **redacts the API key from
every error message** (httpx otherwise embeds the full URL, key included, in exception text).

```
ODDSPAPI_KEY=...                  # required (client is safely IDLE without it; ODDSPAPI_API_KEY also accepted)
ODDSPAPI_SPORTS=tennis,baseball   # slugs, resolved via /v4/sports
ODDSPAPI_POLL_SEC=60              # HOT-tier cadence (quota driver #1)
ODDSPAPI_HOT_HORIZON_H=8          # a tournament is HOT when a watched PRE-LIVE game starts within this
ODDSPAPI_COLD_POLL_SEC=600        # everything else (pre-live, inside the 48h horizon) polls this slowly
ODDSPAPI_POLL_HORIZON_H=48        # beyond this: not polled at all; already-started games: never polled
ODDSPAPI_DISCOVERY_MIN=45         # fixtures-map refresh
ODDSPAPI_PAUSE_WHEN_DARK=1        # stop polling while the book session is down (lifecycle dark window)
ODDSPAPI_HEARTBEAT_TTL_SEC=900    # slate-liveness window for the freshness stamp
ODDSPAPI_BOOKMAKER=pinnacle
```

**Live-mode wiring rule:** `HARDVEN_QUOTE_MAX_AGE_MS` (C# freshness gate, default 30s) must be ≥ ~3x
`ODDSPAPI_POLL_SEC` or quotes expire between polls and the books flicker empty (the adapter warns at startup).
A wide gate is safe **because** WS-verify re-reads both legs on the real Pinnacle WS before any fire.

**Budget at defaults** (60s hot / 600s cold / pause-when-dark, typical slate ~5 hot + ~30 cold tournaments):
~170 req/active-hour → ~60k/month at 12 active hours/day. `ODDSPAPI_POLL_SEC=120` roughly halves it. This is
the number that picks the paid tier.

**Verified live (probe census, 2026-08-05):** moneyline `bookmakerOutcomeId` **is** `"home"`/`"away"` on the
`…/0/moneyline` path (and the period-1 `…/1/moneyline` right next to it is correctly ignored); spreads **are**
`"{points}/{side}"` (`"6.5/home"`, `"-1.5/home"`); teamTotals/alt-period markets ignored. 176/380 slate tokens
quoted with real limits; effective matchup coverage ~86% (odds responses carry `bookmakerFixtureId` even where
`/fixtures` has a null `pinnacleId`, so odds-level coverage beats discovery-level). A healthy tennis line
showed `changedAt` 8h old — the heartbeat freshness design is necessary, not theoretical. Still unverified:
`limit` currency. Parser is also unit-tested against the docs' exact payloads (16 checks).

## Rollout order

1. Shadow through one real slate (Mon–Wed). `analyze_agg_shadow.py` → all PASS.
2. `HARDVEN_AGG_MODE=live` with the existing gates *on* — `HARDVEN_REQUIRE_WS_VERIFIED=1` still confirms on
   Pinnacle's own WS before firing, so a vendor mistake costs a skipped window, not a naked leg.
3. Only then consider relaxing the WS dependency.

Both modes always poll the inner book. That is load-bearing: `PinnacleAdapter.odds()` registers active leagues,
triggers the REST seed and starts the WS — skipping it would silently kill `verify_now()`.
