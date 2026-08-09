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

## 3. Open unknowns

### Answered 2026-08-09 by re-mining the existing capture (no account needed)

- [x] **Moneyline market key per sport.** Three spellings, all with `line = None`:
      `tennis_match,all` (tennis, `p1`/`p2`) · `ml` (mma, boxing, basket, `a`/`h`) ·
      `time_win,tp,all,ml` (baseball, af, ih, cricket, esports, rl, darts, snooker, volley, arf).
      3-way is `wdw` / `time_win,tp,reg,wdw` → `three_way=True`, pair NO-only.
      **`time_win,tp,reg,ml` is deliberately NOT a moneyline** — `reg` is regulation-only while `all` includes
      overtime; Kalshi settles on the final result, so pairing them would be a silent mis-hedge.
- [x] **In-play flag** — `event.ir_status`. Non-null ⇒ in-running, e.g.
      `{"time": ["2h", 21], "score": [0, 4], "rc": [0, 0]}`. Absent/null ⇒ pre-match. Drives `Selection.live`.
- [x] **`max_stake` per selection — CONFIRMED ABSENT.** Not in the price feed, and nothing account-level
      substitutes: `credit_limit ["USD", 0.0]`, `max_stake_per_event null`,
      `settings.max_order 999999999999999999` (sentinel), `max_betslips 8` (slip count).
      **DECIDED:** `BIA_ASSUMED_MAX_STAKE`, default **100.0**, announced at startup and published in
      `/health`. Rationale: letting the 1e18 sentinel through does not "disable a limit", it silently
      DELETES the `MaxDepthFraction` sizing gate while everything still looks healthy — the same shape as
      the `balance()→0.0` halt bug and the phantom-fee bug. Revisit the instant a real limit is captured.

### Still need a logged-in session

- [ ] Bet placement request/response (no bet was placed during recon).
- [ ] Balance amount + endpoint. **Until then `balance()` returns `None` (unreadable), never 0.0** —
      BalanceGuard keeps None as UNKNOWN and does not halt.
- [ ] Open bets / bet-status endpoints.
- [ ] Session TTL / re-login cadence (Pinnacle's ~30min idle logout was a major time sink — check early).
      `betinasia_ws` already re-validates and re-logs-in on socket failure, so this degrades rather than dies.

**Next recon run:** log in, place one tiny real bet, let it settle. That single session captures placement,
balance, open_bets, bet status, and limits in one pass. `MAX_FRAME_CHARS` has been raised 4000 → 60000
(`BIA_RECON_MAX_FRAME_CHARS`) — the old value truncated 46% of frames into invalid JSON and the casualties
were exactly the long `event` catalog frames.

### Two feed behaviours the parser must handle (found by replaying the capture)

- A market whose value is **`null` has been WITHDRAWN** — drop it, do not keep the last price. A stale price
  on a dead market is the phantom-arb shape.
- The feed publishes **`0.0` odds** on listed-but-unavailable markets (seen on `tennis_game_win`). 0.0 is not
  a price. Rejected at INGEST, not at read time, so `catalog()` — which walks the same cache — can never
  publish a leg that will never price.

---

## 4. Build order

### Phase 0 — recon completion (blocked on the account)
- [ ] Re-run `betinasia_recon.py` logged in; place + settle one minimum bet.
- [ ] Extract: placement contract, balance, open bets, limits, in-play flag.

### Phase 1 — M0: odds + catalog (unlocks telemetry, zero money) — **BUILT 2026-08-09**
- [x] `sidecar/betinasia_ws.py` — asyncio WS client (`websockets`): login, connect, ping loop, `watch_hcaps`
      batching, frame router, reconnect w/ resubscribe + session re-validate/re-login on failure.
      `handle_frame()` is deliberately pure + synchronous so tests drive it from recorded frames, no socket.
- [x] `sidecar/betinasia_adapter.py` — `BookAdapter` subclass: `startup()`, `odds()` (auto-subscribes ids it
      has not seen), `catalog()` (moneyline + 3-way only), `balance()` → `None`, M1 methods refuse loudly.
- [x] **selection_id scheme** — `{sport}:{comp_id}:{event_key}:{market_key}:{selection}`
      (e.g. `tennis:338:2026-08-05,73551,87843:tennis_match,all:p1`). Colon-safe: no field observed in the
      capture contains a colon. **comp_id is carried inside the id on purpose** — `watch_hcaps` needs it, so
      `odds()` is self-sufficient without a catalog round-trip (mirrors Pinnacle ids carrying the league id).
- [x] Register `betinasia` in `app.py :: load_adapter()`.
- [x] `sidecar/test_betinasia.py` — **54/54**, incl. replaying all 330 parseable recon frames: no crash,
      moneylines recognised across 13 sports, every cached odd a plausible decimal, every real-data id
      round-trips. The 0.0-odds bug above was found by this replay, not by reading the spec.
- [ ] Run alongside Pinnacle telemetry-only and compare coverage. **Blocked on credentials** — the WS token
      *is* the login session_id, so even the odds path needs `BIA_USERNAME`/`BIA_PASSWORD`.

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
