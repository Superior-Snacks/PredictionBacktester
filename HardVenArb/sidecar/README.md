# HardVen Sidecar — the bot's "private API" to a sportsbook

A small local service that isolates all the messy, book-specific work (session/login, odds reading,
bet-slip automation, balance/bet confirmation) behind a uniform HTTP API. The C# bot
(`HardVenWebsocketFeed` + `HardVenOrderClient`) only ever talks to this API, so the trading core is
identical across venues — a new sportsbook is just a new **adapter** here, no bot changes.

## Run (mock book — works today, no real sportsbook needed)
```bash
cd HardVenArb/sidecar
pip install -r requirements.txt
HARDVEN_BOOK=mock uvicorn app:app --port 8787
# then:
curl "http://127.0.0.1:8787/health"
curl "http://127.0.0.1:8787/odds?selections=MOCK_NBA_FINALS_SAS,MOCK_NBA_FINALS_NYK"
```

## Run bookmaker.eu — ODDS via STOMP-over-WebSocket (no browser needed)
bookmaker.eu streams odds over a RabbitMQ Web-STOMP feed (`bookmaker_stomp.py`). Reading prices needs
**no browser** — just the WSS URL + the per-session dynamic queue string.
```bash
pip install -r requirements.txt
# supply the feed coordinates (grab from devtools, see Recon), then:
HARDVEN_BOOK=bookmaker \
BOOKMAKER_WSS_URL="wss://…" \
BOOKMAKER_STOMP_QUEUE="_uybxhytg541…" \
uvicorn app:app --port 8787
# smoke-test the raw feed on its own (prints moneylines as they arrive):
BOOKMAKER_WSS_URL="wss://…" BOOKMAKER_STOMP_QUEUE="_…" python bookmaker_stomp.py
```
Each game's moneyline becomes two selections: **`"<lid>:H"`** (home) and **`"<lid>:V"`** (visitor) —
those are the ids you put in `cross_pairs.json` as `hardven_yes_token` / `hardven_no_token`. Odds come
in American and are converted to decimal; `price = 1/decimal_odds`. Env knobs: `BOOKMAKER_WS_ORIGIN`
(if the handshake needs an Origin), `BOOKMAKER_MAX_STAKE` (feed omits it; default 250),
`BOOKMAKER_WS_SUBPROTOCOLS` (default `v12.stomp,v11.stomp,v10.stomp`).

**Betting/login (M1) uses Playwright** and is opt-in (`BOOKMAKER_ENABLE_BROWSER=1`, persistent profile —
log in by hand once, no stored password; or set `BOOKMAKER_USER/PASS` + fill `_start_browser()`). Not
needed for M0 odds.

## Recon — the only manual step (get the feed coordinates)
The odds parsing is already written (verified against a live sample). What's per-session and must be
grabbed once from devtools on bookmaker.eu:
1. **WSS URL** — F12 → **Network** → **WS** filter → the websocket connection → copy its `wss://` URL →
   `BOOKMAKER_WSS_URL`.
2. **Dynamic queue string** — in that WS stream's frames (or the init XHR that precedes it), find the
   `x-queue-name` / `destination:/amq/queue/<…>` value → `BOOKMAKER_STOMP_QUEUE`. (It rotates per
   session, so this is a TODO to auto-parse from the init XHR later; for now set it by hand.)
3. **Origin** (only if the WS handshake 403s) — copy the request's `Origin` header → `BOOKMAKER_WS_ORIGIN`.
4. **lid → game mapping** — watch which `lid` carries the game you want (the `destination` header and the
   moneyline teams tell you), then pair `"<lid>:H"`/`"<lid>:V"` to the matching Kalshi market in
   cross_pairs.json.

## API contract (what the C# bot calls)
| Method | Endpoint | Returns | Used by | Milestone |
|---|---|---|---|---|
| GET | `/health` | `{ok, book, ts}` | watchdog | — |
| GET | `/odds?selections=a,b` | `{selections:{id:{decimal_odds, implied_price, max_stake, max_contracts, status, ts}}}` | `HardVenWebsocketFeed` → `"H:"` books | **M0** |
| GET | `/catalog` | `{selections:[{selection_id, sport, league, event, market, selection_name, start_time, rules_text, rules_url}]}` | pairing pipeline | pairing |
| GET | `/balance` | `{balance}` | `GetUsdcBalanceAsync` | M1 |
| POST | `/bet` | `{accepted, bet_id, actual_odds, stake, reason}` | `SubmitOrderAsync` | M1 |
| GET | `/bets/open` | `{bets:[…]}` | wallet confirmation | M1 |
| GET | `/bets/{id}` | bet record | `GetOrderAsync` | M1 |

**Pricing:** `implied_price = 1 / decimal_odds` (the bot's per-$1-contract cost); `max_contracts =
max_stake × decimal_odds`. Sportsbook fee = 0 (vig is already in the odds).

## Adding a real book
1. Pick the book. **Tech follows the book:** if it has a usable API / clean internal JSON →
   `httpx` (no browser, fastest, can place bets). If JS-heavy / protected / slip-only →
   **Playwright** (real session; intercept the odds JSON, drive the bet slip). Prefer intercepting
   JSON over scraping DOM. For a POC, favor an **arber-tolerant** book (e.g. Pinnacle) — bans/limits
   are the #1 risk.
2. Write `mybook_adapter.py` with a `BookAdapter` subclass (`name = "mybook"`), implementing `odds()`
   first (that's all M0 needs), then `catalog()`, then the betting methods for M1.
3. Register it in `app.py` → `load_adapter()`, and run with `HARDVEN_BOOK=mybook`.

`book_adapter.py` is the full spec (the abstract methods are the checklist). `mock_adapter.py` is a
worked reference. Only `odds()` + `/health` are required for telemetry (M0).

## Build order
- **M0:** mock (now) → real adapter `odds()` → point the C# feed at `/odds` → `--telemetry` logs
  Kalshi↔HardVen arb windows. No betting.
- **M1:** implement `balance()` / `place_bet()` / `open_bets()` / `bet()` + tiny-stakes live.
