"""
pinnacle_adapter.py — Pinnacle (pinnacle.bet) odds via its private "Arcadia" API.

TWO odds sources, selectable with PINNACLE_ODDS_MODE:
  "ws"   (DEFAULT) — MQTT-over-WebSocket PUSH feed (real-time, gentle, looks like the real client). This is
                     what the browser uses after the initial REST snapshot. Best for LIVE arbs.
  "rest" (fallback) — serial polling of /leagues/{id}/markets/straight (use if the WS is blocked/awkward).

WHY NO PLAYWRIGHT: api.arcadia.pinnacle.com answers a plain HTTP/WS client — `curl` returns JSON, so there
is NO Cloudflare *browser-challenge* on these endpoints (Cloudflare is just the CDN; the gate is the
x-api-key/x-session headers + the MQTT CONNECT auth). Pinnacle CLOSED its official API, so this replays the
website's own private API with a scraped session → OPERATE GENTLY (account-ban risk): the WS is passive
(no polling), the REST fallback is serial+jittered+backoff. No concurrency (that got the bookmaker acct banned).

WEBSOCKET (mode "ws") — MQTT 3.1.1 over WSS at wss://api.arcadia.pinnacle.com/ws (subprotocol "mqtt"):
  CONNECT   username = ACCOUNT ID (PINNACLE_WS_USERNAME), password = "{x-session}|{suffix}" (PINNACLE_WS_PASSWORD).
  SUBSCRIBE topics "matchups/reg/lg/{leagueId}/{pre|live/ld|live/dz|live/both}" (reg=regular incl moneyline).
  PUBLISH   payload = JSON {op:"upd"|"add"|"del", pk:matchupId, rec:{id, league{id}, participants[...],
            markets:[{key:"s;{period};{type}", period, type, status, prices:[{designation:"home"|"away"|
            "draw", price(AMERICAN), points?}], limits:[{type:"maxRiskStake", amount}]}]}}.
  Full-game 2-way moneyline = market period 0 & type "moneyline" → prices home/away (3 = +draw).

SELECTION-ID (the token in cross_pairs.json): "{leagueId}:{matchupId}:{designation}" (designation = home|
  away|draw — semantic, taken straight from the WS payload; catalog() emits the same from the matchup's
  participant alignment, so WS odds and REST catalog keys MATCH).

FRESHNESS on a PUSH feed: a STABLE price does not re-tick, so we must NOT let its ts age while the WS is
healthy — the live connection IS the freshness guarantee (Pinnacle pushes any change/suspend). So odds()
stamps ts=now WHILE CONNECTED; on disconnect it serves the stored ts → it ages → the C# gate clears the book.

CONFIG (env): PINNACLE_WS_USERNAME, PINNACLE_WS_PASSWORD (WS auth); PINNACLE_API_KEY (defaults to the
  observed static site key); PINNACLE_SESSION, PINNACLE_DEVICE_UUID (REST headers for catalog()/rest mode);
  PINNACLE_CATALOG_LEAGUES (CSV league ids for catalog/pairing); PINNACLE_ODDS_MODE (ws|rest, default ws);
  PINNACLE_REFRESH_SEC (rest mode cadence, default 15 — GENTLE), PINNACLE_ACTIVE_TTL_SEC (default 180),
  PINNACLE_REQUEST_JITTER_MS (rest mode, default 250).

NOTE: requires `paho-mqtt` for ws mode (pip install paho-mqtt). UNTESTED against the live WS — verify the
upgrade isn't Cloudflare-challenged on first connect; if it is, fall back to PINNACLE_ODDS_MODE=rest.
"""
from __future__ import annotations

import asyncio
import json
import os
import random
import threading
import time
from datetime import datetime
from typing import Optional

import httpx

from book_adapter import BookAdapter, BetResult, CatalogEntry, Selection
import sports as sports_cfg   # unified sport catalog (active sport ids default the lifecycle set)

REST_BASE = os.environ.get("PINNACLE_API_BASE", "https://api.arcadia.pinnacle.com/0.1")
# GUEST API: same board structure (sports/leagues/matchups/markets, incl. price `designation`) served with ONLY
# the public x-api-key — NO user session. Used for catalog/pairing so enumeration never depends on the authed
# x-session (which may be stale, not-yet-captured in browser mode, or logged out). Authed REST is for live odds.
GUEST_BASE = os.environ.get("PINNACLE_GUEST_BASE", "https://guest.api.arcadia.pinnacle.com/0.1")
WS_HOST = os.environ.get("PINNACLE_WS_HOST", "api.arcadia.pinnacle.com")
WS_PATH = os.environ.get("PINNACLE_WS_PATH", "/ws")
DEFAULT_API_KEY = "CmX2KcMrXuFmNg6YFbmTxE0y9CIrOi0R"   # static public site client key (not a per-user secret)
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/149.0.0.0 Safari/537.36")
_SIDES = ("home", "away", "draw")


def american_to_decimal(american) -> float:
    """American odds -> decimal. +135 -> 2.35, -159 -> 1.629. 0/invalid -> 0.0."""
    try:
        a = float(american)
    except (TypeError, ValueError):
        return 0.0
    if a == 0:
        return 0.0
    return 1.0 + (a / 100.0 if a > 0 else 100.0 / abs(a))


def _max_risk(limits) -> float:
    for lim in limits or []:
        if lim.get("type") == "maxRiskStake":
            try:
                return float(lim.get("amount") or 0)
            except (TypeError, ValueError):
                return 0.0
    return 0.0


def _cutoff_ts(cutoff) -> Optional[float]:
    """Parse Pinnacle's ISO-8601 `cutoffAt` (betting-close time, UTC) → unix seconds. None if absent/bad."""
    if not cutoff:
        return None
    try:
        return datetime.fromisoformat(str(cutoff).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def _strip_units(name: str) -> str:
    """Drop Pinnacle's per-matchup unit suffix: 'Zizou Bergs (Sets)' / 'Toby Samuel (Games)' -> the bare name.
    The winner matchup is sometimes labelled '(Sets)' (no clean variant exists), so we keep it but clean the
    name for pairing. Names without a '(' are returned unchanged (baseball etc.)."""
    return (name or "").split("(")[0].strip()


class PinnacleAdapter(BookAdapter):
    name = "pinnacle"

    def __init__(self) -> None:
        self._mode = os.environ.get("PINNACLE_ODDS_MODE", "ws").strip().lower()
        self._api_key = os.environ.get("PINNACLE_API_KEY", DEFAULT_API_KEY)
        self._session = os.environ.get("PINNACLE_SESSION", "")
        self._device = os.environ.get("PINNACLE_DEVICE_UUID", "")
        self._ws_user = os.environ.get("PINNACLE_WS_USERNAME", "")
        self._ws_pass = os.environ.get("PINNACLE_WS_PASSWORD", "")
        self._catalog_leagues = [x.strip() for x in
                                 os.environ.get("PINNACLE_CATALOG_LEAGUES", "").split(",") if x.strip()]
        # sport ids whose leagues are EPHEMERAL (tennis=33: per-tournament-per-round, change daily/intraday) →
        # auto-discover today's leagues via /sports/{id}/leagues each catalog() call, instead of hand-listing.
        # DEFAULT from the unified catalog (sports.py, honors HARDVEN_SPORTS) so adding a sport there flows into
        # pairing with no separate env edit — same single-source-of-truth as _lifecycle_sports below. An explicit
        # PINNACLE_CATALOG_SPORTS still overrides (backward compat / to narrow catalog scope).
        _default_catalog_sports = ",".join(str(i) for i in sports_cfg.pinnacle_ids())
        self._catalog_sports = [x.strip() for x in
                                os.environ.get("PINNACLE_CATALOG_SPORTS", _default_catalog_sports).split(",")
                                if x.strip()]
        # DRIFT GUARD: an explicit PINNACLE_CATALOG_SPORTS that omits an ENABLED sport (HARDVEN_SPORTS) is the
        # silent-0-pairs trap — that sport SCHEDULES + SCAFFOLDS but its games never enter /catalog, so pairing
        # matches them against the wrong board → 0 fills. Warn loudly at startup (only when explicitly set + short).
        _missing_cat = [str(i) for i in sports_cfg.pinnacle_ids() if str(i) not in self._catalog_sports]
        if os.environ.get("PINNACLE_CATALOG_SPORTS") and _missing_cat:
            print(f"[PINNACLE] *** WARNING: PINNACLE_CATALOG_SPORTS={self._catalog_sports} is MISSING enabled sport "
                  f"id(s) {_missing_cat} (from HARDVEN_SPORTS). Those sports will schedule + scaffold but NEVER "
                  f"PAIR (catalog skips them → 0 pairs). Add them, or UNSET PINNACLE_CATALOG_SPORTS to track sports.py.")
        self._cache: dict[str, Selection] = {}            # "{lid}:{mid}:{designation}" -> Selection
        self._cache_lock = threading.Lock()               # paho thread writes; asyncio reads
        self._active_leagues: dict[str, float] = {}       # leagueId -> unix ts last requested via /odds
        self._subscribed: set[str] = set()                # leagueIds subscribed on the WS
        self._seeded: set[str] = set()                    # leagueIds REST-seeded once (pre-match snapshot)
        self._http: Optional[httpx.AsyncClient] = None     # authed (live odds seed + rest mode)
        self._guest_http: Optional[httpx.AsyncClient] = None  # guest (catalog/pairing — no session needed)
        # ── WS state ──
        self._client = None
        self._connected = False
        self._ws_started = False                          # LAZY: WS connects only on the first /odds w/ a league
        self._ws_gave_up = False                          # SESSION-DEATH latch → stop retrying a DEAD session
        # Give up ONLY on genuine session death, NEVER on a transient drop (a real browser tab retries those
        # forever). Death signals: WS CONNACK auth-reject (rc 4/5) N× in a row, REST 401/403 M× in a row, or a
        # REST guest-redirect. A transient network/server drop keeps auto-reconnecting (paho 1–60s) — see _ws_watchdog.
        self._ws_auth_rejects = 0                         # consecutive WS CONNACK auth rejections (rc 4/5)
        self._rest_auth_fails = 0                         # consecutive REST 401/403 on AUTHED calls
        self._ws_auth_giveup = int(os.environ.get("PINNACLE_WS_AUTH_GIVEUP", "2"))
        self._rest_auth_giveup = int(os.environ.get("PINNACLE_REST_AUTH_GIVEUP", "3"))
        # A WS auth-reject with a still-LOGGED-IN browser is a stale x-session in paho's creds (a session rotation
        # paho reconnected through), NOT a dead login. Recover by forcing a browser RE-MINT (reload → fresh
        # x-session → pushed to paho), letting paho retry — instead of a permanent give-up. Cap the re-mints per
        # outage so a genuinely dead session still ends. See _on_connect (rc 4/5).
        self._loop: Optional[asyncio.AbstractEventLoop] = None   # main loop, captured in _start_ws (paho callbacks are off-loop)
        self._ws_remints = 0                              # re-mints attempted this outage (reset on a clean connect)
        self._ws_remint_cap = int(os.environ.get("PINNACLE_WS_REMINT_CAP", "6"))
        self._last_remint = 0.0
        self._remint_throttle_sec = float(os.environ.get("PINNACLE_WS_REMINT_THROTTLE_SEC", "30"))
        self._ws_watchdog_task: Optional[asyncio.Task] = None
        self._reconciler_task: Optional[asyncio.Task] = None   # staggered league subscribes (organic timing)
        self._status_task: Optional[asyncio.Task] = None       # browser-like /status liveness ping
        self._status_ping_sec = float(os.environ.get("PINNACLE_STATUS_PING_SEC", "30"))
        self._subscribe_gap_sec = float(os.environ.get("PINNACLE_SUBSCRIBE_GAP_SEC", "3"))
        self._session_ka_task: Optional[asyncio.Task] = None   # session keepalive (vs inactivity logout)
        self._session_ka_sec = float(os.environ.get("PINNACLE_SESSION_KEEPALIVE_SEC", "240"))
        self._session_expired = False                          # terminal: a guest-redirect → stop everything
        self._debug_ws = os.environ.get("PINNACLE_DEBUG_WS") == "1"  # log each WS cache update (prove live=WS)
        self._debug_status = os.environ.get("PINNACLE_DEBUG_STATUS") == "1"  # log market OFFLINE/suspend transitions
        if self._debug_ws:
            print("[PINNACLE] WARNING: PINNACLE_DEBUG_WS=1 logs EVERY WS odds update — the log grows ~18MB/day "
                  "(90MB over a week). Unset it for production / long unattended runs.")
        self._ws_dump_path = os.environ.get("PINNACLE_WS_DUMP", "")          # JSONL dump of EVERY incoming WS record
        self._ws_dump_fh = None                                              # (derivative recon: do Games matchups arrive?)
        # ── session SOURCE: "env" (DEFAULT — creds from PINNACLE_SESSION/.env, paste-the-token) or "browser"
        # (a managed, logged-in Playwright window mints + HOLDS the session and feeds creds in LIVE; see
        # pinnacle_session.py). In browser mode the feed stays idle until login is captured (_session_ready).
        self._session_source = os.environ.get("PINNACLE_SESSION_SOURCE", "env").strip().lower()
        self._browser = None                                   # PinnacleBrowserSession when source == "browser"
        self._session_ready = self._session_source != "browser"  # env mode = ready now; browser waits for login
        self._balance = 0.0                                    # last wallet amount (account currency, e.g. EUR)
        self._balance_currency = ""
        # ── BETTING (M1) SAFETY CONTRACT — established BEFORE any placement code so real money can never fire
        # without the explicit gate. Actual placement goes through the browser UI (bet slip) and is DEFERRED;
        # until then place_bet() previews only. HARDVEN_BET_ENABLE=1 is required for a real send; HARDVEN_MAX_STAKE
        # hard-caps the per-bet stake (account currency); _bet_lock serialises bets (one browser session = one at a
        # time). See HARDVEN_TODO §D/E.
        self._bet_enabled = os.environ.get("HARDVEN_BET_ENABLE") == "1"
        try:
            self._max_stake = float(os.environ.get("HARDVEN_MAX_STAKE") or 10.0)
        except ValueError:
            self._max_stake = 10.0
        self._bet_lock = asyncio.Lock()
        # LIFECYCLE: opt-in schedule-driven open/close of the browser (human session rhythm). Off = hold open.
        self._lifecycle_on = os.environ.get("PINNACLE_LIFECYCLE") == "1"
        # default the lifecycle sport ids from the unified catalog (respects HARDVEN_SPORTS); env still overrides
        _default_sports = ",".join(str(i) for i in sports_cfg.pinnacle_ids())
        self._lifecycle_sports = [int(s) for s in os.environ.get("PINNACLE_LIFECYCLE_SPORTS", _default_sports).split(",")
                                  if s.strip().isdigit()]

        def _cfg_int(name: str, default: int) -> int:
            try:
                return int((os.environ.get(name) or "").strip() or default)
            except ValueError:
                return default
        # window shaping (block selection): open PINNACLE_LEAD_MIN before a block, keep the densest
        # PINNACLE_MAX_BLOCKS (0 = unlimited) with ≥ PINNACLE_MIN_GAMES matches each.
        self._lifecycle_lead = _cfg_int("PINNACLE_LEAD_MIN", 15)
        self._lifecycle_max_blocks = _cfg_int("PINNACLE_MAX_BLOCKS", 4)
        self._lifecycle_min_games = _cfg_int("PINNACLE_MIN_GAMES", 1)
        self._lifecycle_session_hours = float(os.environ.get("PINNACLE_SESSION_HOURS", "0"))  # >0 = discrete Nh density-sessions
        self._lifecycle_manual_plan = os.environ.get("PINNACLE_MANUAL_PLAN", "").strip() or None  # test override (short cycle)
        self._lifecycle_today_only = os.environ.get("PINNACLE_SESSION_TODAY_ONLY", "1") != "0"  # plan only today's games (default ON)
        self._lifecycle = None
        self._lifecycle_task = None

        # AUTO-PAIR: opt-in scheduled re-pairing (startup + daily at HARDVEN_PAIR_HOUR local). Account-free
        # (Kalshi public + Pinnacle guest + the sidecar /catalog); the C# bot hot-reloads the result.
        self._auto_pair = os.environ.get("HARDVEN_AUTO_PAIR") == "1"
        self._pair_hour = _cfg_int("HARDVEN_PAIR_HOUR", 5)
        self._pair_startup_delay = _cfg_int("HARDVEN_PAIR_STARTUP_DELAY", 8)
        # intraday re-pair cadence (min): pairs LIVE/late-appearing games that the daily 5am run would miss.
        # Default 90 min — gentle (a handful of guest /catalog calls per run) and merge-safe (pairHard carries
        # filled pairs). Set HARDVEN_PAIR_INTERVAL_MIN=0 to restore daily-only re-pairing.
        self._pair_interval_min = _cfg_int("HARDVEN_PAIR_INTERVAL_MIN", 90)
        self._pairing = None
        self._pairing_task = None
        # in-play diagnostics: count WS messages per topic-class so a run reveals whether /live (in-play) is
        # actually being delivered (in-play arbs went missing after day 1 — see _refresh_league live-preserve fix).
        self._ws_live_msgs = 0
        self._ws_pre_msgs = 0
        self._requested_ids: set = set()   # selection ids the C# bot actually asks for (the PAIRED tokens) — to
                                           # measure how many WATCHED tokens are live vs the whole cache being live
        # ── REST-mode state ──
        self._refresh_task: Optional[asyncio.Task] = None
        self._refresh_sec = float(os.environ.get("PINNACLE_REFRESH_SEC", "15"))
        self._active_ttl = float(os.environ.get("PINNACLE_ACTIVE_TTL_SEC", "180"))
        self._jitter_ms = float(os.environ.get("PINNACLE_REQUEST_JITTER_MS", "250"))
        self._backoff_sec = 0.0
        self._rate_limited = False
        self._rl_total = 0
        self._last_hb = 0.0
        # session-lifetime instrumentation — measures Pinnacle's REAL inactivity-logout window (env
        # PINNACLE_SESSION_AGE_LOG_SEC, default 300s = 5m). A periodic "session held Xm" heartbeat + a final
        # "held Xm before this stop" on give-up turn the 2h keepalive/idle test into a precise measurement.
        self._session_started_at = 0.0                    # unix time the CURRENT session became live (0 = none)
        self._session_age_task: Optional[asyncio.Task] = None
        self._session_age_log_sec = float(os.environ.get("PINNACLE_SESSION_AGE_LOG_SEC", "300"))
        self._survive_min = float(os.environ.get("PINNACLE_SESSION_SURVIVE_MIN", "35"))  # milestone: past the ~30m danger zone
        self._survive_logged = False                      # one-time "SURVIVED" flag per session (unattended pass/fail)

    # ── lifecycle ──────────────────────────────────────────────────────────────
    async def startup(self) -> None:
        self._http = httpx.AsyncClient(
            headers={"accept": "application/json", "content-type": "application/json",
                     "origin": "https://www.pinnacle.bet", "referer": "https://www.pinnacle.bet/",
                     "user-agent": USER_AGENT, "x-api-key": self._api_key,
                     "x-device-uuid": self._device, "x-session": self._session},
            timeout=15.0)
        if self._session_source == "browser":
            # Launch the managed login window FIRST (non-blocking) and let creds arrive via the callback. The
            # feed (REST seed + WS) gates itself on _session_ready, so startup returns promptly → FastAPI serves
            # /health right away (the C# bot sees the sidecar is up, session_ready=false) while you log in.
            # A browser-launch failure (Chrome missing, profile locked, no display) must NOT kill the sidecar:
            # catalog/pairing still works via the GUEST API, and you can fall back to PINNACLE_SESSION_SOURCE=env.
            try:
                from pinnacle_session import PinnacleBrowserSession
                self._browser = PinnacleBrowserSession(self._on_browser_creds)
                if self._lifecycle_on:
                    # schedule-driven: the lifecycle task opens/closes the browser per the game windows (the
                    # window opens it the first time too — don't start it here). Stays dark until the first one.
                    from lifecycle import PinnacleLifecycle
                    self._lifecycle = PinnacleLifecycle(self._browser, self._lifecycle_sports,
                                                        on_open=self._on_session_opening,
                                                        on_close=self._on_session_closed,
                                                        lead_min=self._lifecycle_lead,
                                                        min_games=self._lifecycle_min_games,
                                                        max_blocks=(self._lifecycle_max_blocks or None),
                                                        session_hours=self._lifecycle_session_hours,
                                                        manual_plan=self._lifecycle_manual_plan,
                                                        today_only=self._lifecycle_today_only)
                    self._lifecycle_task = asyncio.create_task(self._lifecycle.run())
                    mode = (f"MANUAL PLAN {self._lifecycle_manual_plan}" if self._lifecycle_manual_plan
                            else f"{self._lifecycle_session_hours:g}h density-sessions" if self._lifecycle_session_hours > 0
                            else f"gap-merge, densest {self._lifecycle_max_blocks} blocks")
                    print(f"[PINNACLE] session source = BROWSER + LIFECYCLE (sports={self._lifecycle_sports}, "
                          f"{mode}, lead {self._lifecycle_lead}m) — the browser opens/closes on the game "
                          "schedule; dark between sessions.")
                else:
                    await self._browser.start()
                    print("[PINNACLE] session source = BROWSER — log in to the window; the feed waits for "
                          "capture, then seeds + connects automatically.")
            except Exception as ex:
                self._browser = None
                print(f"[PINNACLE] BROWSER session launch FAILED ({type(ex).__name__}: {ex}). Sidecar stays up — "
                      "catalog/pairing still works (guest API); for live odds, fix the browser or set "
                      "PINNACLE_SESSION_SOURCE=env with a fresh PINNACLE_SESSION.")
        if self._mode == "rest":
            self._refresh_task = asyncio.create_task(self._refresh_loop())
            print(f"[PINNACLE] ready — REST poll mode (gentle serial, {self._refresh_sec:g}s). "
                  "Odds id = '<leagueId>:<matchupId>:<designation>'.")
        else:
            print("[PINNACLE] ready — WS mode (LAZY: connects to Pinnacle on the FIRST /odds that names a "
                  "league; nothing is sent before that). Odds id = '<leagueId>:<matchupId>:<designation>'.")

        # session-age heartbeat (persistent across logout/recovery). Env mode is live from startup → mark now;
        # browser mode marks on capture (_on_browser_creds).
        self._session_age_task = asyncio.create_task(self._session_age_heartbeat())
        if self._session_source != "browser":
            self._mark_session_started("env token")

        # AUTO-PAIR: schedule the daily re-pairing pipeline (account-free; independent of the session/mode).
        if self._auto_pair:
            from pairing_scheduler import PairingScheduler
            self._pairing = PairingScheduler(hour=self._pair_hour, initial_delay=self._pair_startup_delay,
                                             interval_min=self._pair_interval_min)
            self._pairing_task = asyncio.create_task(self._pairing.run())
            cadence = (f"every {self._pair_interval_min} min (intraday — pairs live/late-appearing games)"
                       if self._pair_interval_min > 0 else f"daily {self._pair_hour:02d}:00 local")
            print(f"[PINNACLE] AUTO-PAIR on — pairing at startup (+{self._pair_startup_delay}s) then {cadence}. "
                  f"cross_pairs.json + derivative_pairs.json hot-reload into the bot.")

    async def shutdown(self) -> None:
        if self._refresh_task and not self._refresh_task.done():
            self._refresh_task.cancel()
        if self._ws_watchdog_task and not self._ws_watchdog_task.done():
            self._ws_watchdog_task.cancel()
        for t in (self._reconciler_task, self._status_task, self._session_ka_task,
                  self._lifecycle_task, self._pairing_task, self._session_age_task):
            if t and not t.done():
                t.cancel()
        if self._client is not None:
            try:
                self._client.loop_stop()
                self._client.disconnect()
            except Exception:
                pass
        if self._browser is not None:
            try:
                await self._browser.stop()
            except Exception:
                pass
        if self._ws_dump_fh is not None:
            try:
                self._ws_dump_fh.close()
            except Exception:
                pass
        if self._guest_http:
            try:
                await self._guest_http.aclose()
            except Exception:
                pass
        if self._http:
            try:
                await self._http.aclose()
            except Exception:
                pass

    @staticmethod
    def _parse_sid(sid: str):
        """Token → tuple whose [1] is the leagueId (used to track active leagues), for BOTH shapes:
        moneyline 3-seg → ('moneyline', lid, mid, designation);
        derivative 5-seg → ('spread'|'total', lid, mid, points: float, side). None if neither."""
        p = sid.split(":")
        if len(p) == 3 and p[0] and p[1] and p[2] in _SIDES:
            return ("moneyline", p[0], p[1], p[2])
        if len(p) == 5 and p[0] and p[1] and p[2] in ("spread", "total"):
            try:
                pts = float(p[3])
            except ValueError:
                return None
            if (p[2] == "spread" and p[4] in ("home", "away")) or (p[2] == "total" and p[4] in ("over", "under")):
                return (p[2], p[0], p[1], pts, p[4])
        return None

    async def odds(self, selection_ids: list[str]) -> dict[str, Selection]:
        now = time.time()
        self._requested_ids.update(selection_ids)   # remember the WATCHED (paired) tokens for the live diagnostic
        for sid in selection_ids:
            p = self._parse_sid(sid)
            if p:
                self._active_leagues[p[1]] = now   # p[1] = leagueId for both moneyline + derivative tokens
        # BROWSER session source, not logged in yet → no valid creds to seed/connect with. Serve the (empty)
        # cache so /odds still answers; the C# freshness gate keeps the books cleared until odds flow.
        if self._session_source == "browser" and not self._session_ready:
            return self._read_cache(selection_ids, now)
        if self._mode == "ws":
            # REST-seed each new league ONCE: the WS streams CHANGES, not an on-subscribe snapshot, so a
            # stable pre-match line never arrives over the WS until it moves. One /markets/straight snapshot
            # populates every current game (pre-match + live); the WS keeps them fresh after. Mimics the
            # browser exactly (initial REST snapshot, then WS — no re-polling).
            for lid in [l for l in list(self._active_leagues.keys()) if l not in self._seeded]:
                self._seeded.add(lid)                     # add before await so concurrent /odds don't double-seed
                await self._refresh_league(lid)
            if not self._ws_started and self._active_leagues:
                self._start_ws()                          # LAZY connect (the reconciler subscribes leagues gradually)
        return self._read_cache(selection_ids, now)

    def _feed_live(self) -> bool:
        """True only while the Pinnacle feed is GENUINELY live — WS connected, session not expired, and (browser
        source) logged in. Drives the ts-freshness stamp below: while live, a stable price is still fresh (the WS
        would push any change/suspend); when NOT live, we serve the STORED ts so it AGES → the C# freshness gate
        clears the book → NO arb is ever computed on a frozen/stale Pinnacle number after a logout/disconnect."""
        return (self._connected and not self._session_expired
                and (self._session_source != "browser" or self._session_ready))

    def _read_cache(self, selection_ids: list[str], now: float) -> dict[str, Selection]:
        out: dict[str, Selection] = {}
        live = self._feed_live()
        with self._cache_lock:
            for sid in selection_ids:
                s = self._cache.get(sid)
                if not s:
                    continue
                # WS push: stamp ts=now WHILE the feed is LIVE (connection = freshness; a stable price won't
                # re-tick but is still live — Pinnacle pushes any change/suspend). Not live (disconnected / given
                # up / logged out) → serve stored ts → it ages → C# clears. REST mode: the poller already stamps
                # ts on each fetch, so serve it as-is.
                ts = now if (self._mode == "ws" and live) else s.ts
                # Pass through the cached STATUS ("open" / "suspended") so an OFFLINE Pinnacle market reaches
                # the C# as suspended → empty book → no arb. (Was hardcoded "open", which hid suspensions.)
                status = s.status
                # OFFLINE gate (poll-time): a matchup that closes betting STOPS being pushed, so its cached
                # "open" token is never reconciled away, and the GLOBAL-liveness stamp above keeps serving it
                # FRESH (ts=now) even 8 min after Pinnacle went silent on it → phantom arb on a frozen line.
                # cutoffAt is the authoritative betting-close time: once it passes, force suspended here so the
                # C# gets an empty book — independent of push activity, with the WS still connected.
                if s.cutoff and s.cutoff <= now:
                    status = "suspended"
                out[sid] = Selection(s.selection_id, s.decimal_odds, s.max_stake, status, ts, s.live, s.cutoff)
        return out

    # ── browser session source: receive live creds + expose status ────────────────
    def _on_browser_creds(self, creds: dict) -> None:
        """Callback from PinnacleBrowserSession on every credential change. Pushes the freshest x-session /
        device / api-key into the live httpx headers and the WS password into paho (so its next reconnect
        authenticates with the latest token). A NEW x-session (e.g. guest→logged-in, or a rotation after a
        give-up) clears the terminal latches so the feed can come back to life."""
        old_session = self._session
        sess = creds.get("session") or ""
        if sess:
            self._session = sess
            if self._http is not None:
                self._http.headers["x-session"] = sess
        dev = creds.get("device") or ""
        if dev:
            self._device = dev
            if self._http is not None:
                self._http.headers["x-device-uuid"] = dev
        key = creds.get("api_key") or ""
        if key:
            self._api_key = key
            if self._http is not None:
                self._http.headers["x-api-key"] = key
        ws_user = creds.get("ws_user") or ""
        ws_pass = creds.get("ws_pass") or ""
        if ws_user:
            self._ws_user = ws_user
        ws_pass_changed = bool(ws_pass) and ws_pass != self._ws_pass   # a re-captured suffix rotates this
        if ws_pass:
            self._ws_pass = ws_pass
            if self._client is not None:                  # live paho client → use fresh creds on next reconnect
                try:
                    self._client.username_pw_set(self._ws_user, self._ws_pass)
                except Exception:
                    pass
        was_ready = self._session_ready
        self._session_ready = bool(creds.get("ready"))
        # A NEW live session begins on the ready FALSE→TRUE transition (initial login OR recovery-after-logout) OR
        # when the token ROTATES while already live. NB: on initial login the session value is stored above before
        # `ready` flips true, so `sess != old_session` is false by now — the became_ready check is what catches it.
        became_ready = self._session_ready and not was_ready
        rotated = self._session_ready and bool(sess) and sess != old_session
        if became_ready or rotated:
            self._session_expired = False
            self._ws_auth_rejects = self._rest_auth_fails = 0   # fresh creds → clear the death streaks
            self._mark_session_started("browser login" if became_ready else "session rotated")  # (re)start age tracking
            if self._ws_gave_up:                          # recover from a terminal give-up so /odds restarts the feed
                self._ws_gave_up = False
                self._ws_started = False                  # next odds() relands _start_ws() with the new creds
                self._seeded.clear()                      # re-seed pre-match snapshots under the new session
        # A re-captured SUFFIX changes the WS password WITHOUT changing the x-session (so neither became_ready nor
        # rotated fires) — but a given-up odds WS must still restart with it, since a stale suffix is a top cause
        # of the CONNACK auth-reject. Restart on any fresh WS creds after a give-up.
        if self._ws_gave_up and self._session_ready and ws_pass_changed:
            self._ws_gave_up = False
            self._ws_started = False
            self._ws_auth_rejects = self._ws_remints = 0
            self._session_expired = False
            self._seeded.clear()
            print("[PINNACLE] re-captured WS creds (suffix) → restarting the odds WS (was given up).")
        if became_ready:
            print("[PINNACLE] OK browser session ready — feed will seed + connect on the next /odds.")

    def session_status(self) -> dict:
        st = {"source": self._session_source, "ready": self._session_ready,
              "mode": self._mode, "ws_connected": self._connected, "cache_sel": len(self._cache),
              "balance": self._balance, "currency": self._balance_currency}
        if self._browser is not None:
            st["browser"] = self._browser.status()
        if self._lifecycle is not None:
            lc = self._lifecycle.status()
            st["lifecycle"] = lc
            # scheduled_dark = the browser is intentionally DOWN for a dark window (not a logout). Lets the C#
            # heartbeat stay quiet on a planned close and alert ONLY on an unexpected drop during an open window.
            st["scheduled_dark"] = (lc.get("state") == "dark")
        return st

    # ── lifecycle hooks (called by PinnacleLifecycle on scheduled open/close) ──────
    def _on_session_opening(self) -> None:
        """A scheduled window is opening the browser → RESET the feed latches so the WS restarts fresh once
        creds arrive — unconditionally, since a reopened profile may re-issue the SAME x-session (the value-
        change check in _on_browser_creds wouldn't fire). session_ready stays False until creds are captured."""
        self._ws_gave_up = False
        self._ws_started = False
        self._session_expired = False
        self._ws_auth_rejects = self._rest_auth_fails = 0
        self._seeded.clear()

    def _on_session_closed(self) -> None:
        """A scheduled window closed the browser → stand the feed DOWN: gate odds (no creds now) and stop the
        WS/keepalive so we don't poke Pinnacle during the dark stretch. The C# freshness gate clears the books."""
        self._session_ready = False
        self._give_up_ws("scheduled dark window", clean=True)

    # ── WS (MQTT) odds source ────────────────────────────────────────────────
    def _start_ws(self) -> None:
        self._ws_started = True
        try:
            import paho.mqtt.client as mqtt
        except Exception:
            print("[PINNACLE WS] paho-mqtt not installed (`pip install paho-mqtt`). Falling back to REST mode.")
            self._mode = "rest"
            self._refresh_task = asyncio.create_task(self._refresh_loop())
            return
        if not self._ws_user or not self._ws_pass:
            print("[PINNACLE WS] WARNING: PINNACLE_WS_USERNAME / PINNACLE_WS_PASSWORD not set — set from the WS "
                  "CONNECT frame (username = account id, password = '{x-session}|{suffix}').")
        cid = "sub-" + "".join(random.choices(
            "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789", k=16))
        try:
            self._client = mqtt.Client(client_id=cid, transport="websockets",
                                       callback_api_version=mqtt.CallbackAPIVersion.VERSION1)
        except (TypeError, AttributeError):
            self._client = mqtt.Client(client_id=cid, transport="websockets")   # paho < 2.0
        self._client.username_pw_set(self._ws_user, self._ws_pass)
        # The real browser's WS upgrade carries ONLY Origin + User-Agent (NO x-api-key, NO cookies — auth is
        # entirely in the MQTT CONNECT username/password). Match it exactly to avoid a needless fingerprint diff.
        self._client.ws_set_options(path=WS_PATH, headers={
            "Origin": "https://www.pinnacle.bet", "User-Agent": USER_AGENT})
        try:
            self._client.tls_set()                       # wss
        except Exception:
            pass
        self._client.reconnect_delay_set(min_delay=1, max_delay=60)
        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._client.on_message = self._on_message
        try:
            self._loop = asyncio.get_running_loop()       # so paho's off-loop callbacks can schedule a re-mint
            self._client.connect_async(WS_HOST, 443, keepalive=60)
            self._client.loop_start()                    # background network thread
            self._ws_watchdog_task = asyncio.create_task(self._ws_watchdog())     # WS health monitor (no give-up)
            self._reconciler_task = asyncio.create_task(self._sub_reconciler())   # staggered subscribes
            self._status_task = asyncio.create_task(self._status_ping())          # browser-like liveness ping
            self._session_ka_task = asyncio.create_task(self._session_keepalive()) # vs inactivity logout
            print(f"[PINNACLE WS] connecting wss://{WS_HOST}{WS_PATH} (MQTT). Real-time PUSH; "
                  "id = '<leagueId>:<matchupId>:<designation>'.")
        except Exception as ex:
            print(f"[PINNACLE WS] connect error: {ex}")

    def _topics_for(self, lid: str):
        return [(f"matchups/reg/lg/{lid}/pre", 0),
                (f"matchups/reg/lg/{lid}/live/ld", 0),
                (f"matchups/reg/lg/{lid}/live/dz", 0),
                (f"matchups/reg/lg/{lid}/live/both", 0)]

    def _subscribe_league(self, lid: str) -> None:
        if not self._client or not self._connected or lid in self._subscribed:
            return
        try:
            for topic, qos in self._topics_for(lid):
                self._client.subscribe(topic, qos)
            self._subscribed.add(lid)
            print(f"[PINNACLE WS] subscribed league {lid}")
        except Exception as ex:
            print(f"[PINNACLE WS] subscribe {lid} error: {ex}")

    def _on_connect(self, client, userdata, flags, rc, *a) -> None:
        rc_val = getattr(rc, "value", rc)
        ok = (rc_val == 0)
        self._connected = ok
        if ok:
            self._ws_auth_rejects = 0                      # healthy connect clears the auth-fail streak
            self._ws_remints = 0                           # recovered → re-arm the per-outage re-mint budget
            print("[PINNACLE WS] connected (rc=0).")
            self._subscribed.clear()                      # the reconciler re-subscribes active leagues gradually
        elif rc_val in (4, 5):                            # CONNACK 4=bad user/pass, 5=not authorized
            self._ws_auth_rejects += 1
            print(f"[PINNACLE WS] connect REJECTED (rc={rc}) — session/WS-password invalid "
                  f"({self._ws_auth_rejects}/{self._ws_auth_giveup}).")
            if self._ws_auth_rejects >= self._ws_auth_giveup:
                # The WS password is {x-session}|{suffix}. A reject while the BROWSER is still logged in is almost
                # always a STALE x-session in paho's creds (Pinnacle rotated it and paho reconnected before the
                # browser propagated the new one) — NOT a dead login. Force a browser re-mint (reload → fresh
                # x-session → _on_browser_creds pushes it to paho) and let paho keep retrying. Only give up if the
                # browser has no session, or re-mints keep failing (cap) — then it's a genuine logout.
                if self._browser_has_session() and self._ws_remints < self._ws_remint_cap:
                    self._ws_auth_rejects = 0             # give the re-mint a fresh streak (paho keeps reconnecting)
                    if self._request_remint():           # only counts a re-mint that ACTUALLY fired (else throttled)
                        self._ws_remints += 1
                        print(f"[PINNACLE WS] auth-reject but the browser is LOGGED IN — forcing a WS-cred re-mint "
                              f"({self._ws_remints}/{self._ws_remint_cap}); NOT giving up.")
                else:
                    self._give_up_ws(f"WS auth rejected {self._ws_auth_rejects}x "
                                     f"({'re-mint cap hit' if self._browser_has_session() else 'browser logged out too'})")
        else:                                             # rc=3 server-unavailable etc. → TRANSIENT, let paho retry
            print(f"[PINNACLE WS] connect failed (rc={rc}) — transient, auto-reconnecting.")

    def _browser_has_session(self) -> bool:
        """True if the managed browser still holds a live login (so a WS auth-reject is a stale-token rotation,
        recoverable by a re-mint — not a genuine logout)."""
        if self._browser is None:
            return False
        try:
            return bool(self._browser.status().get("has_session"))
        except Exception:
            return False

    def _request_remint(self) -> bool:
        """Schedule an on-demand browser re-mint (reload → fresh x-session) from the paho callback thread. Throttled
        so overlapping auth-rejects during the reload don't stack reloads. Returns True only when it actually fired
        (so the caller counts it toward the cap); False when throttled or not ready."""
        now = time.time()
        if self._loop is None or self._browser is None or now - self._last_remint < self._remint_throttle_sec:
            return False
        self._last_remint = now
        try:
            asyncio.run_coroutine_threadsafe(self._browser.force_remint(), self._loop)
            return True
        except Exception as ex:
            print(f"[PINNACLE] WS re-mint schedule error: {type(ex).__name__}: {ex}")
            return False

    def _on_disconnect(self, client, userdata, rc, *a) -> None:
        self._connected = False
        print(f"[PINNACLE WS] disconnected (rc={rc}) — auto-reconnecting (books go stale until back).")

    async def _ws_watchdog(self) -> None:
        """WS health MONITOR (no longer a give-up cap). A TRANSIENT drop — network blip, server-unavailable
        (CONNACK rc=3), Cloudflare hiccup, clean disconnect — keeps auto-reconnecting FOREVER via paho's 1–60s
        backoff, exactly like a real browser tab left open; we do NOT give up on it. Only genuine SESSION DEATH
        stops the WS: a CONNACK auth-reject (rc 4/5, in _on_connect) or the REST guest-redirect / repeated
        401-403. This loop just LOGS a prolonged outage once (so an operator knows), then keeps watching."""
        warn_after = float(os.environ.get("PINNACLE_WS_WARN_SEC")
                           or os.environ.get("PINNACLE_WS_GIVEUP_SEC", "120"))   # old env name kept for compat
        last_ok, warned = time.time(), False
        while not self._ws_gave_up:
            try:
                await asyncio.sleep(5)
            except asyncio.CancelledError:
                break
            if self._connected:
                last_ok, warned = time.time(), False
            elif not warned and time.time() - last_ok > warn_after:
                warned = True
                print(f"[PINNACLE WS] down >{warn_after:.0f}s — still auto-reconnecting (transient; a DEAD "
                      "session would have stopped it). Books stay stale until it recovers.")

    def _give_up_ws(self, reason: str = "", clean: bool = False) -> None:
        if self._ws_gave_up:
            return
        # State changes FIRST (before any print) — a print can throw on a cp1252 Windows console, and these must
        # not be skipped. CRITICAL: loop_stop() does NOT fire on_disconnect, so drop _connected ourselves →
        # _read_cache stops stamping ts=now → frozen prices AGE → C# clears the books (no arb on stale numbers).
        self._ws_gave_up = True                       # stops ALL background loops (they check `not _ws_gave_up`)
        self._connected = False
        held_m = (time.time() - self._session_started_at) / 60 if self._session_started_at > 0 else -1.0
        self._session_started_at = 0.0                # session ended → stop age tracking (re-marks on recovery)
        why = f" ({reason})" if reason else ""
        if held_m >= 0:
            print(f"[PINNACLE] *** SESSION HELD {held_m:.0f}m before this stop ***{why}")
        if clean:
            # EXPECTED stop (scheduled dark window / lifecycle close) — the session was fine; nothing to refresh.
            print(f"[PINNACLE] standing the WS + keepalive DOWN{why} — expected close, session was healthy. "
                  "Books go stale -> C# clears them; reopens on the next window.")
        else:
            print(f"[PINNACLE] STOPPING the WS + keepalive{why} - a dead/stale session isn't worth re-trying. "
                  "Refresh PINNACLE_SESSION + PINNACLE_WS_PASSWORD (= newsession|dGGR) and restart, or keep a "
                  "browser open to hold the session (or PINNACLE_ODDS_MODE=rest). Books go stale -> C# clears them.")
        c = self._client
        if c is not None:
            # loop_stop() must NOT run inside the paho loop thread (this can be called from a callback) → offload.
            threading.Thread(target=c.loop_stop, daemon=True).start()

    async def _sub_reconciler(self) -> None:
        """Subscribe to pending (active-but-unsubscribed) leagues ONE AT A TIME with a gap, so the WS
        subscribe pattern looks like a user navigating league to league — not a single scripted burst.
        Also re-subscribes after a reconnect (on_connect clears _subscribed; we refill gradually)."""
        while not self._ws_gave_up:
            try:
                await asyncio.sleep(self._subscribe_gap_sec)
            except asyncio.CancelledError:
                break
            if not self._connected:
                continue
            pending = [l for l in list(self._active_leagues.keys()) if l not in self._subscribed]
            if pending:
                self._subscribe_league(pending[0])        # one per tick = staggered, organic-looking

    def _mark_session_started(self, how: str) -> None:
        """Stamp the moment a session became live (env token at startup, or a fresh browser capture) so the age
        heartbeat + the give-up log can report exactly how long it was held — i.e. Pinnacle's real logout window."""
        self._session_started_at = time.time()
        self._survive_logged = False                      # arm the "SURVIVED past Nm" milestone for this session
        print(f"[PINNACLE] session established ({how}) — age tracking started.")

    async def _session_age_heartbeat(self) -> None:
        """Persistent: every PINNACLE_SESSION_AGE_LOG_SEC, log how long the current session has been held (with
        WS/ready state). Quiet when there's no session. Survives give-up + recovery so a 2h test reads cleanly."""
        while True:
            try:
                await asyncio.sleep(self._session_age_log_sec)
            except asyncio.CancelledError:
                break
            if self._session_started_at > 0:
                held_m = (time.time() - self._session_started_at) / 60
                ws = "connected" if self._connected else ("GAVE-UP" if self._ws_gave_up else "down")
                print(f"[PINNACLE] session held {held_m:.0f}m  (ready={self._session_ready}, ws={ws}, "
                      f"cache={len(self._cache)} sel)")
                if not self._survive_logged and held_m >= self._survive_min:
                    self._survive_logged = True           # unattended pass signal — glance for this line when you're back
                    print(f"[PINNACLE] *** SESSION SURVIVED past {self._survive_min:.0f}m — keepalive is HOLDING "
                          "(was logging out at ~30m). ***")

    async def _status_ping(self) -> None:
        """Browser-like liveness: GET /status periodically (the page does this). Makes the headless session
        emit the same background heartbeat a real tab does. CAMOUFLAGE ONLY — /status carries no session, so
        it does NOT refresh/extend the x-session; that needs the separate token-refresh call (TODO)."""
        while not self._ws_gave_up:
            try:
                await asyncio.sleep(self._status_ping_sec)
            except asyncio.CancelledError:
                break
            await self._http_get("/status", authed=False)   # camouflage only — carries no session; never an auth signal

    async def _session_keepalive(self) -> None:
        """Keep the x-session alive vs the ~90-min INACTIVITY logout (an idle session — only /status, no
        AUTHED calls — gets logged out). Every PINNACLE_SESSION_KEEPALIVE_SEC, re-fetch each active league's
        /markets/straight: an AUTHED origin hit (carries x-session, must-revalidate past its 5s cache) that
        should reset the server-side inactivity timer. Doubles as a pre-match RE-SEED to reconcile any drift
        the WS missed. (If the timeout turns out to be UI-activity-based, this won't help → we'd need re-auth.)"""
        while not self._ws_gave_up:
            try:
                await asyncio.sleep(self._session_ka_sec)
            except asyncio.CancelledError:
                break
            leagues = list(self._active_leagues.keys())
            for lid in leagues:
                if self._ws_gave_up:
                    break                                     # session died mid-cycle → stop immediately
                await self._refresh_league(lid)
                await asyncio.sleep(random.uniform(0, self._jitter_ms / 1000.0))   # gentle spacing
            if leagues and not self._ws_gave_up:
                live_sel = sum(1 for s in self._cache.values() if getattr(s, "live", False))
                # Of the tokens the C# bot actually WATCHES (paired), how many are live right now? If this stays
                # 0 while `live_sel` (the whole cache) is high, the paired games' live data isn't reaching their
                # tokens — the systematic in-play miss. Sample a few live watched ids (or overall) to compare.
                watched_live = [sid for sid in self._requested_ids
                                if (s := self._cache.get(sid)) is not None and getattr(s, "live", False)]
                sample = watched_live[:6] if watched_live else \
                    [sid for sid, s in self._cache.items() if getattr(s, "live", False)][:6]
                print(f"[PINNACLE] session-keepalive: re-fetched {len(leagues)} league(s) (authed → resets the "
                      f"inactivity timer; cache={len(self._cache)} sel, {live_sel} live) | "
                      f"WS msgs live={self._ws_live_msgs}/pre={self._ws_pre_msgs} | "
                      f"WATCHED-live={len(watched_live)}/{len(self._requested_ids)} | "
                      f"sample live{'(watched)' if watched_live else ''}: {sample}")

    def _on_message(self, client, userdata, msg, *a) -> None:
        try:
            data = json.loads(msg.payload.decode("utf-8"))
        except Exception:
            return
        live = "/live/" in (getattr(msg, "topic", "") or "")   # topic = IN-PLAY (…/live/*) vs pre-match (…/pre)
        if live:
            self._ws_live_msgs += 1
        else:
            self._ws_pre_msgs += 1
        try:
            self._apply(data, live)
        except Exception as ex:
            print(f"[PINNACLE WS] apply error: {type(ex).__name__}: {ex}")

    def _apply(self, data: dict, live: bool = False) -> None:
        rec = data.get("rec") or {}
        mid = rec.get("id") if rec.get("id") is not None else data.get("pk")
        lid = (rec.get("league") or {}).get("id")
        if mid is None or lid is None:
            return
        lid, mid = str(lid), str(mid)
        prefix = f"{lid}:{mid}:"
        if self._ws_dump_path:
            self._dump_ws_record(data, lid, mid)
        if data.get("op") == "del":
            with self._cache_lock:
                for k in [k for k in self._cache if k.startswith(prefix)]:
                    del self._cache[k]
            return
        now = time.time()
        # Pinnacle pushes the WHOLE matchup record on any sub-market change, so the markets list is the full
        # current state. Build a token for every OPEN period-0 moneyline / spread / total price (`_market_tokens`
        # mirrors pair_derivatives' keying). RECONCILE: any cached token for this matchup NOT in this push (a
        # line pulled, a market suspended, a side's price gone) is marked SUSPENDED so a stale "open" leg can't
        # sit against a live Kalshi leg (phantom arb). A marketless push (score/clock heartbeat) is ambiguous →
        # leave tokens to the staleness gate.
        markets = rec.get("markets") or []
        updates: dict[str, Selection] = {}
        for mk in markets:
            for token, sel in self._market_tokens(lid, mid, mk, now, live):
                updates[token] = sel
        reconcile = len(markets) > 0
        suspended = 0
        with self._cache_lock:
            if reconcile:
                for k in [k for k in self._cache if k.startswith(prefix) and k not in updates]:
                    old = self._cache[k]
                    if old.status != "suspended":
                        suspended += 1
                    self._cache[k] = Selection(old.selection_id, old.decimal_odds, old.max_stake,
                                               status="suspended", ts=now)
            self._cache.update(updates)
        if updates and self._debug_ws:
            legs = " ".join(f"{k.split(':', 2)[-1]}={v.decimal_odds:.3f}" for k, v in list(updates.items())[:6])
            print(f"[PINNACLE WS-UPD] {data.get('op')} {lid}:{mid}  {legs}")
        if suspended and (self._debug_ws or self._debug_status):
            print(f"[PINNACLE STATUS] {lid}:{mid} → {suspended} leg(s) offline/suspended")

    def _market_tokens(self, lid: str, mid, mk: dict, now: float, live: bool = False):
        """Yield (token, Selection) for each price of an OPEN, full-game (period 0) moneyline / spread / total
        market. `live` = IN-PLAY (came over a …/live/* topic) vs pre-match (…/pre or REST seed). Token keys
        MIRROR pair_derivatives.py: moneyline '{lid}:{mid}:{designation}' (home/away/draw); spread/total
        '{lid}:{mid}:{type}:{points:g}:{designation}'. Skips non-open markets, foreign types, bad prices."""
        t = mk.get("type")
        if mk.get("period") != 0 or t not in ("moneyline", "spread", "total"):
            return
        if mk.get("status") not in (None, "open"):
            return
        # OFFLINE gate: Pinnacle keeps status="open" and shows the last price after betting closes — the
        # `cutoffAt` (betting-close time) is the real "currently offline" tag (confirmed 2026-07-02: 785 open
        # markets sat 20–88 min past cutoff, still displaying a frozen unbettable line). Skip cutoff-passed
        # markets so the reconcile step suspends them → a stale line can't hold a phantom arb. `now` is
        # wall-clock UTC epoch, matching cutoffAt. (limits/max_stake is NOT a signal — never 0 in practice.)
        cutoff = _cutoff_ts(mk.get("cutoffAt"))
        if cutoff is not None and cutoff <= now:
            return
        max_stake = _max_risk(mk.get("limits"))
        for pr in mk.get("prices") or []:
            desig = pr.get("designation")
            dec = american_to_decimal(pr.get("price"))
            if dec <= 1.0:
                continue
            if t == "moneyline":
                if desig not in _SIDES:
                    continue
                token = f"{lid}:{mid}:{desig}"
            else:
                pts = pr.get("points")
                if pts is None:
                    continue
                if not ((t == "spread" and desig in ("home", "away")) or (t == "total" and desig in ("over", "under"))):
                    continue
                token = f"{lid}:{mid}:{t}:{float(pts):g}:{desig}"
            yield token, Selection(token, decimal_odds=dec, max_stake=max_stake, status="open", ts=now,
                                   live=live, cutoff=cutoff or 0.0)

    def _dump_ws_record(self, data: dict, lid: str, mid: str) -> None:
        """RECON (PINNACLE_WS_DUMP=<path>): append a compact summary of EVERY incoming WS record so we can see
        whether the '(Games)' DERIVATIVE matchups (spread/total/team_total) arrive over the current league-level
        subscription, or ride a different topic that needs an extra subscribe. Captures matchupId, the participant
        names (the '(Games)'/'(Sets)' suffix tells us which matchup it is), units/parentId if present, and per
        market its type/period/status/price-count/points-flag/designations. Single-writer (paho thread)."""
        try:
            rec = data.get("rec") or {}
            full = os.environ.get("PINNACLE_WS_DUMP_FULL") == "1"
            mkts = []
            for mk in rec.get("markets") or []:
                prices = mk.get("prices") or []
                mkts.append({"type": mk.get("type"), "period": mk.get("period"), "status": mk.get("status"),
                             # 'la' was a dead guess (never present). The real 'currently offline' suspects are
                             # cutoffAt (betting-cutoff time) and limits→max_stake (0 = pulled) on a status:open market.
                             "cutoffAt": mk.get("cutoffAt"),   # ISO cutoff — if past → offline even when status=open
                             "max_stake": _max_risk(mk.get("limits")),  # 0 = limits pulled → effectively unbettable
                             "keys": sorted(mk.keys()),        # ALL market fields, to spot any OTHER suspend signal
                             "n": len(prices),
                             "pts": any(pr.get("points") is not None for pr in prices),
                             "desig": [pr.get("designation") for pr in prices],
                             "price0": prices[0] if prices else None})  # full first price (spot any per-price suspend field)
            out = {"ts": round(time.time(), 3), "op": data.get("op"), "lid": lid, "mid": mid,
                   "units": rec.get("units"), "parentId": rec.get("parentId"),
                   "names": [p.get("name") for p in (rec.get("participants") or [])],
                   "markets": mkts}
            if full:
                out["raw"] = rec                               # PINNACLE_WS_DUMP_FULL=1: complete record, deep inspection
            line = json.dumps(out, default=str)
            if self._ws_dump_fh is None:
                self._ws_dump_fh = open(self._ws_dump_path, "a", encoding="utf-8")
            self._ws_dump_fh.write(line + "\n")
            self._ws_dump_fh.flush()
        except Exception as ex:
            print(f"[PINNACLE] WS dump error: {type(ex).__name__}: {ex}")

    # ── REST fallback odds source (designation read directly from each price, like the WS) ──
    async def _refresh_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(self._backoff_sec if self._backoff_sec > 0 else self._refresh_sec)
            except asyncio.CancelledError:
                break
            now = time.time()
            leagues = [lg for lg, ts in self._active_leagues.items() if now - ts <= self._active_ttl]
            if not leagues:
                continue
            # Logged out → idle (don't poll Pinnacle with a dead session). The loop keeps sleeping, not calling;
            # it auto-resumes when a fresh login lands (browser source) or the session is restored.
            if self._session_expired or (self._session_source == "browser" and not self._session_ready):
                continue
            self._rate_limited = False
            t0 = time.perf_counter()
            try:
                for i, lid in enumerate(leagues):
                    await self._refresh_league(lid)
                    if i + 1 < len(leagues) and self._jitter_ms > 0:
                        await asyncio.sleep(random.uniform(0, self._jitter_ms / 1000.0))
            except Exception as ex:
                print(f"[PINNACLE] refresh error: {type(ex).__name__}: {ex}")
            dt = time.perf_counter() - t0
            if self._rate_limited:
                self._backoff_sec = min(max(self._backoff_sec, self._refresh_sec) * 2, 120.0)
                print(f"[PINNACLE] rate-limited → backing off to {self._backoff_sec:.0f}s.")
            elif self._backoff_sec > 0:
                print(f"[PINNACLE] rate limit cleared — resuming {self._refresh_sec:g}s.")
                self._backoff_sec = 0.0
            if now - self._last_hb >= 30:
                self._last_hb = now
                rl = f"  429s: {self._rl_total}" if self._rl_total else ""
                print(f"[PINNACLE] refresh: {len(leagues)} leagues (serial) in {dt:.2f}s "
                      f"(cache={len(self._cache)} sel){rl}")

    async def _refresh_league(self, lid: str) -> None:
        """One-shot pre-match SNAPSHOT seed of a league (moneyline + spread + total). Prices carry `designation`
        (home/away/draw or over/under) and `points` DIRECTLY — exactly like the WS — via the SAME `_market_tokens`
        builder, so REST-seeded tokens key identically to WS tokens and to pair_derivatives. (Reads designation
        straight; the OLD code's array-position map silently INVERTED home/away — fixed long ago.)"""
        markets = await self._http_get(f"/leagues/{lid}/markets/straight", count_429=True)
        if not markets:
            return
        now = time.time()
        for mk in markets:
            mid = mk.get("matchupId")
            if mid is None:
                continue
            for token, sel in self._market_tokens(lid, mid, mk, now):
                with self._cache_lock:
                    old = self._cache.get(token)
                    # A REST /markets/straight snapshot is PRE-MATCH-blind — it must NOT downgrade an IN-PLAY tag
                    # the WS set (this 4-min keepalive re-seed was clobbering live→pre-live, so in-play arbs
                    # vanished after the first live game). Keep live once the WS has flagged it; the game's `del`
                    # clears it when it ends.
                    if old is not None and old.live and not sel.live:
                        sel.live = True
                    self._cache[token] = sel

    # ── HTTP (catalog + rest mode) ───────────────────────────────────────────
    def _rest_death_check(self, reason: str) -> None:
        """A REST-replay auth failure (401/403 streak or guest-redirect) wants to declare the session dead. But
        the ODDS WS is the TRUE liveness signal: while paho is CONNECTED, the login is alive and odds are flowing
        (a genuine logout drops/auth-rejects the WS too). So a REST auth failure WHILE THE WS IS UP is just a
        STALE REPLAY x-session — re-sync it from the browser's latest token and DO NOT declare death. This fixes
        the false 'session DOWN' seen while the page stayed logged in (the REST replay 401'd while the WS kept
        streaming odds). Only when the WS is ALSO down is this a real logout."""
        if self._connected:
            self._rest_auth_fails = 0                       # WS healthy → not a logout; stop the fail streak
            if self._http is not None and self._session:
                self._http.headers["x-session"] = self._session   # re-apply the browser's freshest x-session
            print(f"[PINNACLE] {reason}, but the odds WS is CONNECTED — re-synced the REST x-session; NOT a logout "
                  "(the browser still holds the session).")
            return
        self._session_expired = True
        self._session_ready = False                         # WS is ALSO down → a genuine logout
        self._give_up_ws(f"{reason} (session dead — WS also down)")

    async def _http_get(self, path: str, count_429: bool = False, authed: bool = True):
        if not self._http:
            return None
        try:
            r = await self._http.get(REST_BASE + path)
        except Exception as ex:
            print(f"[PINNACLE] GET {path} error: {type(ex).__name__}: {ex}")
            return None    # network error = TRANSIENT — never a session-death signal (don't touch the fail streak)
        if r.status_code == 429:
            if count_429:
                self._rate_limited = True
                self._rl_total += 1
            print(f"[PINNACLE] *** RATE LIMITED (429) on {path} *** — raise PINNACLE_REFRESH_SEC / fewer leagues.")
            return None
        if r.status_code in (401, 403):
            # A single AUTHED 401/403 can be a blip; REPEATED = a dead session (the guest-redirect's sibling for
            # servers that 401 instead of 302). Give up after N in a row so we STOP poking a dead session, but a
            # lone blip just logs and keeps going (transient). Unauthed paths (/status) never count.
            if authed:
                self._rest_auth_fails += 1
                print(f"[PINNACLE] AUTH {r.status_code} on {path} ({self._rest_auth_fails}/{self._rest_auth_giveup}) "
                      "— session may be invalid.")
                if self._rest_auth_fails >= self._rest_auth_giveup:
                    self._rest_death_check(f"REST auth {r.status_code} x{self._rest_auth_fails}")
            else:
                print(f"[PINNACLE] AUTH {r.status_code} on {path}.")
            return None
        if r.status_code in (301, 302, 303, 307, 308):
            loc = r.headers.get("location", "")
            if "guest" in loc.lower():
                print(f"[PINNACLE] {path}: redirected to the GUEST endpoint → the replayed x-session looks EXPIRED.")
                self._rest_death_check("session expired — guest redirect")   # only a real logout if the WS is also down
            else:
                print(f"[PINNACLE] GET {path} HTTP {r.status_code} → {loc}")
            return None
        if r.status_code != 200:
            print(f"[PINNACLE] GET {path} HTTP {r.status_code}")
            return None
        if authed:
            self._rest_auth_fails = 0                       # a good AUTHED response clears the auth-fail streak
        try:
            return r.json()
        except Exception:
            return None

    async def _guest_get(self, path: str):
        """GET structural data from the GUEST API (public key, NO user session) — for catalog/pairing, which is
        just names/leagues/markets and must NOT depend on (or trip the give-up on) the authed x-session. Lazy
        client; follows redirects (the guest host is the redirect target, so it never bounces to itself)."""
        if self._guest_http is None:
            self._guest_http = httpx.AsyncClient(
                headers={"accept": "application/json", "content-type": "application/json",
                         "origin": "https://www.pinnacle.bet", "referer": "https://www.pinnacle.bet/",
                         "user-agent": USER_AGENT, "x-api-key": DEFAULT_API_KEY},
                timeout=20.0, follow_redirects=True)
        try:
            r = await self._guest_http.get(GUEST_BASE + path)
        except Exception as ex:
            print(f"[PINNACLE] GUEST GET {path} error: {type(ex).__name__}: {ex}")
            return None
        if r.status_code != 200:
            print(f"[PINNACLE] GUEST GET {path} HTTP {r.status_code}")
            return None
        try:
            return r.json()
        except Exception:
            return None

    # ── pairing catalog (GUEST API — designation-keyed to match the WS odds; no session needed) ──
    async def _catalog_league_ids(self) -> list[str]:
        """League ids to catalog: explicit PINNACLE_CATALOG_LEAGUES (stable ids — baseball 246/6227/187703)
        PLUS every current league of each PINNACLE_CATALOG_SPORTS (auto-discovery for sports whose 'leagues'
        are ephemeral tournament-rounds — tennis=33 → today's ITF/ATP/WTA events). Doubles leagues are skipped
        (the bot pairs 2-player singles vs Kalshi singles). Re-resolved per catalog() call so it tracks the
        board with no hand-editing."""
        ids = list(self._catalog_leagues)
        for sid in self._catalog_sports:
            for l in (await self._guest_get(f"/sports/{sid}/leagues") or []):
                if (l.get("matchupCount") or 0) > 0 and "doubles" not in (l.get("name", "") or "").lower():
                    ids.append(str(l.get("id")))
        return list(dict.fromkeys(ids))   # dedupe, preserve order

    async def catalog(self) -> list[CatalogEntry]:
        league_ids = await self._catalog_league_ids()
        if not league_ids:
            print("[PINNACLE] catalog(): set PINNACLE_CATALOG_LEAGUES (CSV league ids, e.g. 246=MLB) and/or "
                  "PINNACLE_CATALOG_SPORTS (CSV sport ids, e.g. 33=Tennis) for auto-discovery.")
            return []
        out: list[CatalogEntry] = []
        for i, lid in enumerate(league_ids):
            matchups = await self._guest_get(f"/leagues/{lid}/matchups") or []
            straight = await self._guest_get(f"/leagues/{lid}/markets/straight") or []
            # Catalog ONLY matchups that actually carry an AVAILABLE full-game moneyline — the SAME filter the
            # odds path uses, so every cataloged token is one the odds cache will populate. This is the robust
            # tennis discriminator: Pinnacle lists a "(Games)" (and sometimes a "(Sets)") DERIVATIVE matchup per
            # match with the SAME players, and the /matchups `hasMoneyline` flag is UNRELIABLE live (True even
            # when the market is suspended) → keying off the real market avoids double-pairing. The winner is
            # whichever matchup (clean OR "(Sets)"-labelled) has the live moneyline; the "(Games)" one (no live
            # moneyline) and the tournament outright (a many-way moneyline) are both excluded by this set.
            # matchupId -> the moneyline's price designations. A dict (not a set) so the 3-way DRAW leg can be
            # emitted: soccer matchups expose only 2 PARTICIPANTS (home/away), but the moneyline PRICES carry a
            # third 'draw' designation (which the odds path already tokenises as '{lid}:{mid}:draw' — _SIDES
            # includes 'draw'). Without emitting a draw catalog entry the pairing's Tie leg can never match.
            winner_desigs = {mk.get("matchupId"): [pr.get("designation") for pr in (mk.get("prices") or [])]
                             for mk in straight
                             if mk.get("type") == "moneyline" and mk.get("period") == 0
                             and 2 <= len(mk.get("prices") or []) <= 3}
            for m in matchups:
                if m.get("id") not in winner_desigs:
                    continue
                parts = m.get("participants") or []
                if len(parts) < 2:
                    continue
                if any("/" in (p.get("name") or "") for p in parts):
                    continue   # DOUBLES ("A / B" pairs) sit inside singles leagues too — Kalshi is singles-only
                lg = m.get("league") or {}
                sport = ((lg.get("sport") or {}).get("name")) or ""
                league_name = lg.get("name") or str(lid)
                home = _strip_units(next((p.get("name", "") for p in parts if p.get("alignment") == "home"), ""))
                away = _strip_units(next((p.get("name", "") for p in parts if p.get("alignment") == "away"), ""))
                event = f"{home} vs {away}"
                three_way = sport.strip().lower() == "soccer"
                for p in parts:
                    desig = p.get("alignment")
                    if desig not in _SIDES:
                        continue
                    out.append(CatalogEntry(
                        selection_id=f"{lid}:{m.get('id')}:{desig}",
                        sport=sport, league=league_name, event=event, market="moneyline",
                        selection_name=_strip_units(p.get("name", "")), start_time=m.get("startTime"),
                        three_way=three_way))
                # 3-way DRAW leg: not a participant — synthesise it from the moneyline's 'draw' price so the
                # Tie pairing (Kalshi NO(Tie) + Pinnacle back-Draw) can complete. Odds already serve this token.
                if three_way and "draw" in winner_desigs[m.get("id")]:
                    out.append(CatalogEntry(
                        selection_id=f"{lid}:{m.get('id')}:draw",
                        sport=sport, league=league_name, event=event, market="moneyline",
                        selection_name="Draw", start_time=m.get("startTime"),
                        three_way=three_way))
            if i + 1 < len(league_ids) and self._jitter_ms > 0:
                await asyncio.sleep(random.uniform(0, self._jitter_ms / 1000.0))   # gentle between many leagues
        return out

    # ── M1 (later): betting + wallet confirmation ──
    async def balance(self) -> float:
        """Account cash balance via the authed wallet endpoint — same X-Session/X-Device-UUID/X-API-Key headers
        as the odds feed (the page polls this constantly, so it's a normal call). Returns the numeric amount
        (0.0 if unavailable). The account currency (EUR here — NOT USD; Kalshi is USD) is stashed for /health +
        the min-balance floor; cross-venue stake sizing must FX-convert at M1. Gated on a live session so it
        can't hit the authed endpoint with a stale token pre-login (which would trip the give-up)."""
        if self._session_source == "browser" and not self._session_ready:
            return 0.0
        data = await self._http_get("/wallet/balance")
        if not isinstance(data, dict):
            return 0.0
        try:
            self._balance = float(data.get("amount") or 0.0)
        except (TypeError, ValueError):
            self._balance = 0.0
        self._balance_currency = data.get("currency") or self._balance_currency
        return self._balance

    async def place_bet(self, selection_id: str, stake: float, max_odds: float) -> BetResult:
        """Place a back bet on `selection_id` for `stake` (account currency), accepting only if the offered odds
        are >= max_odds (i.e. price <= requested). SAFETY-GATED SCAFFOLD:

          1. stake > HARDVEN_MAX_STAKE            → reject (hard cap, never overridden).
          2. session not ready                    → reject (can't place without a live login).
          3. HARDVEN_BET_ENABLE != 1 (DEFAULT)    → PREVIEW: log the intended bet, place NOTHING.
          4. enabled                              → serialise on _bet_lock, then _place_via_ui() — the browser
                                                     bet-slip automation, DEFERRED (raises until built).

        This guarantees real money can never fire without the explicit env gate AND an implemented UI path."""
        # 1. hard stake cap
        if stake > self._max_stake:
            return BetResult(accepted=False, stake=stake,
                             reason=f"stake {stake:.2f} > HARDVEN_MAX_STAKE {self._max_stake:.2f} (hard cap)")
        # 2. must have a live session
        if self._session_source == "browser" and not self._session_ready:
            return BetResult(accepted=False, stake=stake, reason="no live Pinnacle session (login not captured)")
        # 3. preview default — nothing is placed unless explicitly enabled
        if not self._bet_enabled:
            print(f"[PINNACLE BET] PREVIEW (HARDVEN_BET_ENABLE!=1) — WOULD place {stake:.2f} on {selection_id} "
                  f"@ max_odds>={max_odds:.4f}. No bet placed.")
            return BetResult(accepted=False, stake=stake,
                             reason="preview only — set HARDVEN_BET_ENABLE=1 to place real bets")
        # 4. real placement — serialise (one browser session) and go through the UI (deferred)
        async with self._bet_lock:
            print(f"[PINNACLE BET] LIVE — placing {stake:.2f} on {selection_id} @ max_odds>={max_odds:.4f}")
            return await self._place_via_ui(selection_id, stake, max_odds)

    async def _place_via_ui(self, selection_id: str, stake: float, max_odds: float) -> BetResult:
        """Fill + submit the Pinnacle bet slip in the managed browser (add selection → set stake → handle the
        'odds changed, accept?' dialog, accepting only if still >= max_odds → capture the confirmation: bet id,
        actual accepted odds, stake). DEFERRED — bet placement will be driven through the UI (see HARDVEN_TODO
        §B/§D). Raising here (rather than returning rejected) makes an accidental enable fail LOUD, not silent."""
        raise NotImplementedError("Pinnacle bet placement via the browser UI is not implemented yet (deferred M1).")

    async def open_bets(self) -> list[dict]:
        return []  # TODO(M1): read My Bets from the UI/authed endpoint for fill confirmation + settlement

    async def bet(self, bet_id: str) -> Optional[dict]:
        return None  # TODO(M1): single-bet status by id (confirmation / settlement)
