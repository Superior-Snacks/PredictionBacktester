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

## Rollout order

1. Shadow through one real slate (Mon–Wed). `analyze_agg_shadow.py` → all PASS.
2. `HARDVEN_AGG_MODE=live` with the existing gates *on* — `HARDVEN_REQUIRE_WS_VERIFIED=1` still confirms on
   Pinnacle's own WS before firing, so a vendor mistake costs a skipped window, not a naked leg.
3. Only then consider relaxing the WS dependency.

Both modes always poll the inner book. That is load-bearing: `PinnacleAdapter.odds()` registers active leagues,
triggers the REST seed and starts the WS — skipping it would silently kill `verify_now()`.
