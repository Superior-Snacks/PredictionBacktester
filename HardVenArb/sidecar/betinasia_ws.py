"""
BetInAsia price-feed client — login (httpx) + WebSocket odds stream.

WHY THIS IS SMALL COMPARED TO pinnacle_adapter.py
-------------------------------------------------
BetInAsia authenticates with a plain JSON POST and hands back a `session_id` that IS the WebSocket
token. There is no browser, no CDP capture, no login watcher, no page-reload keepalive:

    POST https://black.betinasia.com/web/sessions/  {username, password}
      -> {"data": {"session_id": "<32-hex>", "can_place_bets": true,
                   "customer_data": {"ccy_code": "USD", ...}}, "status": "ok"}
    GET  /web/sessions/{session_id}/    -> same shape (validates a session)
    wss://black.betinasia.com/cpricefeed/?token=<session_id>&lang=en

PROTOCOL (decoded from sidecar/betinasia_recon_20260805_*.jsonl, 648 WS frames)
------------------------------------------------------------------------------
Every frame is JSON. The normal shape is a BATCH: a list of messages, each `[mtype, key, payload]`.
A few frames arrive as a single bare message, so the reader normalises both (see `iter_messages`).

  OUT  ["ping", "<epoch_ms>"]                              keepalive, ~3s in the real client
       ["watch_hcaps", [[comp_id, sport, event_key], ...]] subscribe a BATCH of events
       ["watch_event", [comp_id, sport, event_key]]        subscribe one outright/multirunner

  IN   ["event",       [sport, event_key], {...}]          catalog metadata (see below)
       ["offers_hcap", [comp_id, sport, event_key], {market_key: [line, [[sel, odds], ...]], ...}]
       ["offers_event",[comp_id, sport, event_key], {"win": [[null, [[runner, odds], ...]]]}]
       ["ok"] / ["error", "event_already_subscribed"] / ["pong", ...] / ["api", {...}]

  event_key   "YYYY-MM-DD,<team1_id>,<team2_id>"  or  "YYYY-MM-DD,multirunner,<id>"
  market_key  "<sport>_<type>,<period>,<unit>"    e.g. "tennis_match,all", "time_win,tp,all,ml"

TWO THINGS THE RAW FEED WILL BITE YOU WITH
------------------------------------------
1. The leading int in `[line, [...]]` is the LINE in quarter-units (42 on an `ahou` = 10.5). It is
   NOT a stake limit. Moneyline markets carry `line = None`.
2. A market whose value is `null` has been WITHDRAWN — the market is gone, not priced at zero. We
   drop it from the cache so `odds()` reports it missing rather than serving a stale price.

The feed carries NO per-selection stake limit; see `betinasia_adapter.MAX_STAKE_NOTE`.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Any, Callable, Iterable, Optional

import httpx
import websockets

BASE_URL = os.environ.get("BIA_BASE_URL", "https://black.betinasia.com")
WS_URL   = os.environ.get("BIA_WS_URL",   "wss://black.betinasia.com/cpricefeed/")

PING_SEC       = float(os.environ.get("BIA_PING_SEC", "3.0"))     # matches the real client's cadence
RECONNECT_SEC  = float(os.environ.get("BIA_RECONNECT_SEC", "5.0"))
HTTP_TIMEOUT   = float(os.environ.get("BIA_HTTP_TIMEOUT", "15.0"))

# ── Subscription shape: stay inside what a browsing human produces ────────────
# Measured off three real sessions (betinasia_recon_20260809_*): 90/57/25 subscribe frames carrying
# 638/334/172 events. Batch sizes ran min 1, MEDIAN 3, max 32 (one 113 outlier); the sustained rate
# was ~2-3 events/sec. So the VOLUME is not what stands out -- a real session subscribes hundreds --
# it is the SHAPE. One 250-wide watch_hcaps is a fingerprint no browser makes.
#
# The page-load burst is authentic and worth reproducing: at t~3s the browser fired 5+10+15+32 back to
# back (~77 events instantly) before settling into a trickle. Reconnecting IS a page load, so
# BURST_EVENTS goes out unpaced and everything after it is paced.
SUB_BATCH      = int(os.environ.get("BIA_SUB_BATCH", "12"))        # events per watch_hcaps frame
SUB_PACE_SEC   = float(os.environ.get("BIA_SUB_PACE_SEC", "0.4"))  # gap between paced batches
SUB_BURST      = int(os.environ.get("BIA_SUB_BURST", "77"))        # unpaced allowance, page-load sized


def iter_messages(frame: Any) -> list[list]:
    """Normalise a decoded frame into a list of `[mtype, ...]` messages.

    The feed sends batches (`[[mtype, key, payload], ...]`) but occasionally a bare single message
    (`[mtype, key, payload]`). Telling them apart is unambiguous: in a bare message element 0 is the
    mtype STRING, whereas in a batch element 0 is itself a list.
    """
    if not isinstance(frame, list) or not frame:
        return []
    if isinstance(frame[0], str):
        return [frame]
    return [m for m in frame if isinstance(m, list) and m]


class BetInAsiaError(RuntimeError):
    pass


class BetInAsiaFeed:
    """Login + WS odds cache. Owns no BookAdapter concepts — it speaks BetInAsia and nothing else."""

    def __init__(self, username: Optional[str] = None, password: Optional[str] = None,
                 on_log: Optional[Callable[[str], None]] = None, passive: bool = False) -> None:
        # PASSIVE mode: this object becomes a pure PARSER. It will not open a socket, will not log in,
        # and will not send a single frame -- `betinasia_observer` pumps it the frames a real browser
        # received. Enforced here rather than by convention because the whole anti-detection position
        # rests on the account never emitting traffic a user would not: a Python WS client carrying the
        # session token is a different TLS fingerprint with no browser origin, which is exactly the
        # kind of second client that stands out. A guard a caller cannot forget beats a rule it can.
        self.passive = passive
        # Credentials come from the environment ONLY. Never a file, never a default: the 2026-08-05
        # recon captured a login POST with the password in cleartext, which is exactly the mistake
        # this rule exists to stop repeating.
        self._user = username or os.environ.get("BIA_USERNAME") or ""
        self._pass = password or os.environ.get("BIA_PASSWORD") or ""
        self._log  = on_log or (lambda m: print(f"[BIA] {m}", flush=True))

        self.session_id: Optional[str] = None
        self.customer_data: dict = {}
        self.can_place_bets: bool = False
        self.currency: str = "USD"
        self.username: str = self._user
        self._http: Optional[httpx.AsyncClient] = None

        # (sport, event_key) -> {"markets": {market_key: (line, {sel: odds})}, "ts": float, "comp_id": int}
        self._books: dict[tuple[str, str], dict] = {}
        # (sport, event_key) -> event metadata payload (catalog source)
        self._events: dict[tuple[str, str], dict] = {}
        self._subs: dict[tuple[str, str], int] = {}      # (sport, event_key) -> comp_id
        self._wanted: set = set()                        # passive: ids the BOT asked for
        self._lock = asyncio.Lock()

        self._ws: Optional[Any] = None
        self._tasks: list[asyncio.Task] = []
        self._stop = asyncio.Event()
        self._connected = False
        self._last_frame_ts: float = 0.0
        self._frames_seen = 0

    # ── properties ────────────────────────────────────────────────────────────
    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def last_frame_age(self) -> float:
        return time.time() - self._last_frame_ts if self._last_frame_ts else float("inf")

    # ── auth ──────────────────────────────────────────────────────────────────
    async def login(self) -> str:
        """POST /web/sessions/ -> session_id. Raises BetInAsiaError on any non-ok reply."""
        if not self._user or not self._pass:
            raise BetInAsiaError("BIA_USERNAME / BIA_PASSWORD are not set in the environment")
        # ONE long-lived client, kept for the whole session. The /v1/* catalog endpoints are authed off
        # whatever the login response sets (cookie), so a throwaway client per call would drop that and
        # every catalog read would come back a guest redirect.
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=HTTP_TIMEOUT)
        r = await self._http.post(f"{BASE_URL}/web/sessions/",
                                  json={"username": self._user, "password": self._pass})
        if r.status_code != 200:
            raise BetInAsiaError(f"login HTTP {r.status_code}")
        body = r.json()
        if body.get("status") != "ok":
            raise BetInAsiaError(f"login rejected: status={body.get('status')!r}")
        data = body.get("data") or {}
        sid = data.get("session_id")
        if not sid:
            raise BetInAsiaError("login ok but no session_id in reply")
        self.session_id     = sid
        self.customer_data  = data.get("customer_data") or {}
        self.can_place_bets = bool(data.get("can_place_bets"))
        self.currency       = (self.customer_data.get("ccy_code") or "USD").upper()
        self.username       = self._user
        # Deliberately never log the session_id: it is a bearer token for the whole account.
        self._log(f"login OK - currency={self.currency} can_place_bets={self.can_place_bets}")
        return sid

    async def validate_session(self) -> bool:
        """GET /web/sessions/{id}/ — cheap liveness probe. False means the session is dead."""
        if not self.session_id or self._http is None:
            return False
        try:
            r = await self._http.get(f"{BASE_URL}/web/sessions/{self.session_id}/")
            return r.status_code == 200 and (r.json() or {}).get("status") == "ok"
        except Exception:
            return False

    # ── REST catalog (the "what leagues / what games" calls) ──────────────────
    async def _get(self, path: str, params: dict | None = None):
        """Authed GET returning the `data` payload, or None when unreadable. Never raises: a catalog
        read that fails must degrade to "we learned nothing", not take the feed down."""
        if self._http is None:
            return None
        try:
            r = await self._http.get(f"{BASE_URL}{path}", params=params or {})
            if r.status_code != 200:
                self._log(f"GET {path} -> HTTP {r.status_code}")
                return None
            body = r.json()
        except Exception as e:
            self._log(f"GET {path} failed: {type(e).__name__}: {e}")
            return None
        if not isinstance(body, dict) or body.get("status") != "ok":
            return None
        return body.get("data")

    async def list_competitions(self, sport: str, limit: int = 200) -> list[dict]:
        """LEAGUES for a sport -> [{id, name, sport, country, val}, ...].

        `val` is this account's current exposure in that competition (it matched the open position
        exactly in the capture), NOT a ranking — do not read it as importance.
        """
        data = await self._get(f"/v1/newcompetitions/{self.username}/suggested/",
                               {"sports": sport, "sport_limit": limit})
        return [d for d in (data or []) if isinstance(d, dict)]

    async def list_events(self, sport: str, ts_from: Optional[str] = None,
                          limit: int = 25) -> list[dict]:
        """GAMES for a sport -> [{id, competition_id, sport, start_ts}, ...].

        `id` IS the WS event_key and `competition_id` IS the comp_id `watch_hcaps` needs, so this
        alone is enough to subscribe. It carries NO team names — those come from the WS `event`
        frames (see `all_events()`), which is why the adapter builds its catalog from those.

        Default limit is 25 because that is exactly what the site's own page asks for. Raising it is
        a request shape the real client never makes, so treat any larger value as a deliberate,
        justified exception rather than a convenience.
        """
        if ts_from is None:
            ts_from = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(time.time() - 3600))
        data = await self._get(f"/v1/events/{self.username}/suggested/",
                               {"sports": sport, "event_ts_from": ts_from, "limit": limit})
        return [d for d in (data or []) if isinstance(d, dict)]

    async def watch_sport(self, sport: str, limit: int = 0) -> int:
        """Subscribe a sport's events, SOONEST FIRST. Returns how many were added.

        Driven off the WS catalog rather than REST: the catalog was pushed to us unprompted, so using
        it costs nothing and adds no request the real client would not make. REST is only a fallback
        for the case where the push has not arrived yet.

        Soonest-first matters for more than politeness -- a far-future game legitimately has no book,
        so subscribing in catalog order buys a pile of silent events and makes "is it priced?"
        unreadable. `limit=0` means no cap; the browser envelope is enforced downstream in
        `_send_watch`, not here.
        """
        cat = [(k, v) for (s, k), v in self._events.items() if s == sport]
        if cat:
            ordered = sorted(cat, key=lambda kv: str((kv[1] or {}).get("start_ts") or "9999"))
            entries = [((v or {}).get("competition_id"), sport, k) for k, v in ordered
                       if (v or {}).get("competition_id") is not None]
        else:
            events = await self.list_events(sport)
            entries = [(e.get("competition_id"), e.get("sport") or sport, e.get("id"))
                       for e in events if e.get("id") and e.get("competition_id") is not None]
        if limit > 0:
            entries = entries[:limit]
        await self.watch(entries)
        return len(entries)

    # ── subscriptions ─────────────────────────────────────────────────────────
    async def watch(self, entries: Iterable[tuple[int, str, str]]) -> None:
        """Subscribe (comp_id, sport, event_key) triples. Idempotent — already-watched keys are kept
        (the server answers a repeat with `error/event_already_subscribed`, which is harmless but noisy)."""
        if self.passive:
            # Emit nothing AND record nothing. Recording intent here was actively harmful: `_subs` is
            # the record of what the PAGE subscribed, and polluting it with what the bot merely ASKED
            # for made coverage reporting claim subscriptions that never happened -- so a sidecar
            # serving zero prices still looked fully subscribed.
            self._wanted.update((sport, ekey) for _c, sport, ekey in entries)
            return
        new: list[list] = []
        async with self._lock:
            for comp_id, sport, ekey in entries:
                k = (sport, ekey)
                if k in self._subs:
                    continue
                self._subs[k] = comp_id
                new.append([comp_id, sport, ekey])
        if new and self._ws is not None:
            await self._send_watch(new)

    async def _send_watch(self, entries: list[list], burst: int = 0) -> None:
        """Emit watch_hcaps in browser-shaped batches. `burst` = how many events may go out unpaced
        (a page load does ~77); everything beyond that is spaced by SUB_PACE_SEC. Pacing lives HERE,
        at the transport, so no caller -- however eager -- can produce a subscription burst a browser
        would never make."""
        sent = 0
        for i in range(0, len(entries), SUB_BATCH):
            batch = entries[i:i + SUB_BATCH]
            try:
                await self._ws.send(json.dumps(["watch_hcaps", batch]))
            except Exception as e:
                self._log(f"watch_hcaps send failed: {type(e).__name__}: {e}")
                return
            sent += len(batch)
            if sent >= burst and i + SUB_BATCH < len(entries):
                await asyncio.sleep(SUB_PACE_SEC)

    # ── reads ─────────────────────────────────────────────────────────────────
    def get_market(self, sport: str, event_key: str, market_key: str):
        """-> (line, {selection: decimal_odds}, ts) or None when absent/withdrawn."""
        book = self._books.get((sport, event_key))
        if not book:
            return None
        m = book["markets"].get(market_key)
        if not m:
            return None
        return m[0], m[1], book["ts"]

    def get_event(self, sport: str, event_key: str) -> Optional[dict]:
        return self._events.get((sport, event_key))

    def all_events(self) -> dict[tuple[str, str], dict]:
        return dict(self._events)

    def stats(self) -> dict:
        priced = sum(1 for b in self._books.values() if (b or {}).get("markets"))
        return {"connected": self._connected, "subs": len(self._subs), "books": len(self._books),
                "priced": priced, "events": len(self._events), "wanted": len(self._wanted),
                "frames": self._frames_seen,
                "last_frame_age": round(self.last_frame_age, 2) if self._last_frame_ts else None}

    # ── frame handling ────────────────────────────────────────────────────────
    def handle_frame(self, frame: Any) -> None:
        """Route one decoded frame into the caches. Pure and synchronous so the tests can drive it
        straight from recorded recon frames with no socket involved."""
        self._frames_seen += 1
        self._last_frame_ts = time.time()
        for msg in iter_messages(frame):
            mtype = msg[0]
            if mtype == "offers_hcap" and len(msg) >= 3:
                self._on_offers(msg[1], msg[2])
            elif mtype == "event" and len(msg) >= 3:
                self._on_event(msg[1], msg[2])
            elif mtype == "offers_event" and len(msg) >= 3:
                self._on_offers(msg[1], msg[2])
            # "ok" / "pong" / "api" / "error" carry no prices; error is logged for subscribe debugging
            elif mtype == "error":
                detail = msg[1] if len(msg) > 1 else ""
                if detail != "event_already_subscribed":
                    self._log(f"WS error frame: {detail}")

    def _on_event(self, key: Any, payload: Any) -> None:
        # event key is [sport, event_key]; offers keys are [comp_id, sport, event_key]
        if not (isinstance(key, list) and len(key) >= 2) or not isinstance(payload, dict):
            return
        sport, ekey = (key[0], key[1]) if len(key) == 2 else (key[1], key[2])
        self._events[(sport, ekey)] = payload

    def _on_offers(self, key: Any, payload: Any) -> None:
        if not (isinstance(key, list) and len(key) >= 3) or not isinstance(payload, dict):
            return
        comp_id, sport, ekey = key[0], key[1], key[2]
        book = self._books.setdefault((sport, ekey), {"markets": {}, "ts": 0.0, "comp_id": comp_id})
        book["comp_id"] = comp_id
        book["ts"] = time.time()
        for market_key, val in payload.items():
            # `null` = the market was WITHDRAWN. Drop it rather than keep the last price: a stale
            # price on a dead market is precisely the phantom-arb shape we fight on the other venue.
            if val is None:
                book["markets"].pop(market_key, None)
                continue
            if not (isinstance(val, list) and len(val) == 2):
                continue
            line, raw = val
            sels: dict[str, float] = {}
            for item in (raw or []):
                if isinstance(item, list) and len(item) >= 2:
                    try:
                        odds = float(item[1])
                    except (TypeError, ValueError):
                        continue
                    # Decimal odds <= 1.0 pay nothing back beyond the stake, so they are not a price:
                    # the real feed emits 0.0 for markets that are listed but currently unavailable
                    # (seen on tennis_game_win in the 2026-08-05 capture). Reject at INGEST, not at
                    # read time, so the invariant "everything cached is a live price" holds for every
                    # reader -- catalog() walks this same cache and would otherwise publish
                    # selections that can never be priced.
                    if odds <= 1.0:
                        continue
                    sels[str(item[0])] = odds
            if sels:
                book["markets"][market_key] = (line, sels)
            else:
                book["markets"].pop(market_key, None)

    # ── connection lifecycle ──────────────────────────────────────────────────
    async def start(self) -> None:
        if self.passive:
            raise BetInAsiaError(
                "passive feed: refusing to open a socket. Frames must be pumped in from the browser "
                "via betinasia_observer -- see the note on __init__(passive=...)")
        if not self.session_id:
            await self.login()
        self._stop.clear()
        self._tasks.append(asyncio.create_task(self._run()))

    async def stop(self) -> None:
        self._stop.set()
        for t in self._tasks:
            t.cancel()
        self._tasks.clear()
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
        self._ws = None
        self._connected = False
        if self._http is not None:
            try:
                await self._http.aclose()
            except Exception:
                pass
            self._http = None

    async def _run(self) -> None:
        """Connect, resubscribe everything, pump frames; reconnect forever until stop()."""
        while not self._stop.is_set():
            try:
                url = f"{WS_URL}?token={self.session_id}&lang=en"
                async with websockets.connect(url, max_size=None) as ws:
                    self._ws = ws
                    self._connected = True
                    self._log("WS connected")
                    # Resubscribe from scratch: the server keeps no state for us across sockets.
                    async with self._lock:
                        entries = [[cid, sp, ek] for (sp, ek), cid in self._subs.items()]
                    if entries:
                        # A reconnect IS a page load, so the page-load burst is the authentic shape here.
                        await self._send_watch(entries, burst=SUB_BURST)
                        self._log(f"resubscribed {len(entries)} event(s)")
                    ping = asyncio.create_task(self._ping_loop(ws))
                    try:
                        async for raw in ws:
                            try:
                                self.handle_frame(json.loads(raw))
                            except json.JSONDecodeError:
                                continue
                    finally:
                        ping.cancel()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self._log(f"WS disconnected: {type(e).__name__}: {e}")
            finally:
                self._connected = False
                self._ws = None
            if self._stop.is_set():
                break
            # A dead session produces an immediate, repeating socket failure. Re-login before the
            # next attempt so an expired session_id self-heals instead of spinning forever.
            if not await self.validate_session():
                try:
                    await self.login()
                    self._log("session was dead - re-logged in")
                except Exception as e:
                    self._log(f"re-login failed: {type(e).__name__}: {e}")
            await asyncio.sleep(RECONNECT_SEC)

    async def _ping_loop(self, ws) -> None:
        while True:
            await asyncio.sleep(PING_SEC)
            try:
                await ws.send(json.dumps(["ping", str(int(time.time() * 1000))]))
            except Exception:
                return
