# BetInAsia adapter — what we need, and what's already answered

Second venue behind the same `BookAdapter` seam. **The C# bot, executor, lifecycle, scheduler, balance guard,
telemetry and analyzer do not change** — a book is one Python class plus a pairing script. That was the whole
point of the seam; this is the first time we collect on it.

Source of truth for everything below: `sidecar/betinasia_recon_20260805_*.jsonl` (2 sessions, 435 HTTP + 648 WS
frames, captured 2026-08-05). Gitignored — see SECURITY.

---

## 1. SECURITY — do this first

`betinasia_recon_*.jsonl` contains **`POST /web/sessions/` with the username and password in plaintext**, plus a
live `session_id`. Confirmed gitignored (`.gitignore:408`) and not in `git status`. Still:

- [ ] **Rotate the BetInAsia password** — it has been sitting in a plaintext file on disk.
- [ ] Adapter reads creds from env only (`BIA_USERNAME` / `BIA_PASSWORD`), never a file, never a default.
- [ ] Redact `password` / `session_id` in any future recon writer (the Pinnacle adapter already does this for
      its API key — copy `_redact()` from `agg_oddspapi.py`).

---

## 2. What the recon already answers

### 2.1 No browser required — this is the big one
```
POST https://black.betinasia.com/web/sessions/   {username, password}
  -> {"data": {"session_id": "<32-hex>", "customer_id": …, "can_place_bets": true,
                "customer_data": {"ccy_code": "USD", "credit_limit": [...], …}}, "status": "ok"}

GET  https://black.betinasia.com/web/sessions/{session_id}/     -> same shape (session validation)
```
The `session_id` **is** the WS token:
```
wss://black.betinasia.com/cpricefeed/?token=<session_id>&lang=en
```
So BetInAsia needs **httpx only — no Playwright, no CDP capture, no login watcher, no page-reload keepalive.**
That deletes most of what made `pinnacle_adapter.py` 3k lines. Closest precedent is therefore *not* Pinnacle but
a plain API adapter.

### 2.2 Account is USD
`ccy_code: USD`, and Kalshi is USD → **no FX layer at all**. `HARDVEN_FX_TO_USD`, `fx.py`, and the whole
stake-conversion path collapse to identity. (Keep the code path; just feed it 1.0.)

### 2.3 WS protocol (fully decoded)
Envelope is a list of `[mtype, key, payload]`.

**Outbound**
| Frame | Purpose |
|---|---|
| `["ping","<epoch_ms>"]` | keepalive, ~3s cadence observed |
| `["watch_hcaps",[[comp_id,sport,event_key],…]]` | subscribe a BATCH of events' main/handicap markets |
| `["watch_event",[…]]` | subscribe one event (outright/multirunner) |

**Inbound**
| mtype | Payload | Use |
|---|---|---|
| `event` | `{event_type, start_ts, competition_id, competition_name, country, teams:[{team_id,name}]}` | **catalog()** |
| `offers_hcap` | `{market_key: [line, [[selection, decimal_odds],…]], …}` | **odds()** |
| `offers_event` | `{"win": [[null,[[runner_id, odds],…]]]}` | outrights |
| `ok` / `error` | `["error","event_already_subscribed"]` | subscribe bookkeeping |

**Key formats**
- event key: `"YYYY-MM-DD,<team1_id>,<team2_id>"`, or `"YYYY-MM-DD,multirunner,<id>"` for outrights
- market key: `"<sport>_<type>,<period>,<unit>"` — e.g. `tennis_ah,all,game`, `tennis_ahou,1,game`,
  `tennis_cs,all,set`, `tennis_game_win,1,3`
- the leading int in `[line, [...]]` is the **LINE** (quarter-units: `42` on `ahou` = 10.5), **not** a stake limit
- sports prefixes seen: `tennis`, `fb`, `baseball`, `arf`, `politics`

### 2.4 REST endpoints worth knowing
| Endpoint | Hits | Likely use |
|---|---|---|
| `/web/events/external/` | 46 | REST catalog (probably the cheap `catalog()` source) |
| `/v1/newcompetitions/{user}/suggested/` | 48 | competition list |
| `/web/preferences/{user}/customersettings/` | — | contains `"limit"` keys — check for stake limits |
| `/api/version` | 22 | trivial health probe |

---

## 3. Open unknowns — need a logged-in session to capture

- [ ] **`max_stake` per selection — THE design risk.** `Selection.max_stake` feeds `max_contracts`, which drives
      `StakeLadder.MaxDepthFraction` (never bet >1/3 of book max) and the executor's depth gate. The price
      frames carry **no limit**. Find it: `customersettings` `"limit"` keys, `credit_limit`, per-price data on a
      bet-slip call, or a separate quote endpoint. **If BetInAsia exposes no per-selection limit, decide the
      fallback deliberately** (account-level cap? fixed notional?) — do not let it default to 0 or infinity.
- [ ] Bet placement request/response (never captured — no bet was placed during recon).
- [ ] Balance amount + endpoint (`customer_data` had no balance field in the top level).
- [ ] Open bets / bet-status endpoints.
- [ ] Moneyline market key per sport (the recon sample is heavy on `ah`/`ahou`; confirm the 2-way match-winner key).
- [ ] Does the feed distinguish pre-match vs **in-play**? (`Selection.live` drives the timing model.)
- [ ] Session TTL / re-login cadence (Pinnacle's ~30min idle logout was a major time sink — check early).

**Next recon run:** log in, place one tiny real bet, let it settle. That single session captures placement,
balance, open_bets, bet status, and limits in one pass.

---

## 4. Build order

### Phase 0 — recon completion (blocked on the account)
- [ ] Re-run `betinasia_recon.py` logged in; place + settle one minimum bet.
- [ ] Extract: placement contract, balance, open bets, limits, in-play flag.

### Phase 1 — M0: odds + catalog (unlocks telemetry, zero money)
- [ ] `sidecar/betinasia_ws.py` — asyncio WS client: connect, ping loop, `watch_hcaps` batching,
      frame router, reconnect w/ resubscribe. (Model on `bookmaker_stomp.py`, not the paho code.)
- [ ] `sidecar/betinasia_adapter.py` — `BookAdapter` subclass:
      - `startup()` → login, open WS
      - `odds()` → cache lookup, `ts` freshness, `status`, `cutoff`
      - `catalog()` → from `event` frames and/or `/web/events/external/`
      - `balance()` → `Optional[float]`, **None when unreadable** (the rule that bit us twice on Pinnacle)
      - M1 methods raise/preview until Phase 3
- [ ] **selection_id scheme** — must be stable, parseable, and mirror the pairing script.
      Proposal: `{sport}:{event_key}:{market_key}:{selection}` (e.g. `tennis:2026-08-05,73551,87843:tennis_ah,all,set:p1`)
- [ ] Register `betinasia` in `app.py :: load_adapter()`.
- [ ] Run alongside Pinnacle telemetry-only and compare coverage.

### Phase 2 — pairing
- [ ] `sidecar/pair_betinasia.py` — Kalshi ↔ BIA. Team names come as `{team_id, name}`, so a **team-id map is
      more durable than string matching** — better starting point than the Pinnacle pairer had.
- [ ] Reuse `pair_pinnacle.py`'s price-gate sanity check (catches inverted/wrong-game pairs).
- [ ] Wire into `pairing_scheduler.py`.

### Phase 3 — M1: betting
- [ ] `place_bet()` behind the same safety contract as Pinnacle: `HARDVEN_BET_ENABLE`, `HARDVEN_MAX_STAKE` hard
      cap, `_bet_lock` serialisation, preview-by-default.
- [ ] `open_bets()` / `bet()` for settlement + VOID detection.
- [ ] Publish the `betting` block in `/health` so the **C# preflight** validates the contract (already built).

### Phase 4 — integration
- [ ] `HARDVEN_BOOK=betinasia`; confirm lifecycle/scheduler/balance-guard work unchanged (they should — all
      book-agnostic).
- [ ] Fee model: `HardVenFee()` in `CrossArbExecutor` — is BIA commission-based? (brokers often are)
- [ ] FX = 1.0 (USD account).
- [ ] Decide whether BIA runs *instead of* or *alongside* Pinnacle — two sidecars on different ports is the
      cheap answer and needs no C# change.

---

## 5. What we get for free

Everything except the adapter and the pairer: lifecycle/scheduling/pins/bounds, balance guard + banking window,
Discord control plane, telemetry + kickoff splitting, the executor with its net floor / lock guard / sibling
dedupe / ladder, recovery + orphan handling, and the analyzer. All book-agnostic by construction.

**Biggest risk is Phase 0's `max_stake` question** — it feeds sizing, and sizing is where money is lost.
Answer it before writing Phase 1, because it may change the `Selection` contract.
