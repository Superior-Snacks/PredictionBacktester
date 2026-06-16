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
