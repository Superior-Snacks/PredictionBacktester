"""
pinnacle_session.py — the MANAGED LOGIN WINDOW for the Pinnacle adapter (PINNACLE_SESSION_SOURCE=browser).

WHAT IT DOES: launches a HEADED, persistent-profile Chromium on pinnacle.bet, YOU log in by hand once, and
it scrapes the live credentials off the site's OWN traffic so the adapter's clean httpx (REST seed) + paho
(MQTT WS odds) feed can replay them — no manual token-pasting, no token-rotation babysitting. The window
then STAYS OPEN as the session anchor: a real logged-in tab holds the server-side session and re-issues a
rotating x-session, which we keep re-capturing and pushing into the running feed. The persistent profile
remembers the login across restarts (so most restarts capture automatically — no re-login).

WHAT IT CAPTURES:
  x-session / x-device-uuid / x-api-key  — from request HEADERS on any api.arcadia.pinnacle.com call (the
                                           app attaches them to every request → key-name-agnostic, robust).
  WS username (account id) + "|suffix"   — parsed ONCE from the MQTT 3.1.1 CONNECT frame the page's own odds
                                           WebSocket sends. Pinnacle's WS password is "{x-session}|{suffix}",
                                           so after the first CONNECT we RECONSTRUCT it from the *live*
                                           x-session + the stable suffix on every rotation (no re-parsing).

DESIGN (why this shape): the adapter's feed stays clean httpx/paho — the browser is PURELY the login
surface + credential source + session-liveness anchor. It does NOT serve odds (the user chose "window holds
session only" — two WS to sharp-friendly Pinnacle is an acceptable footprint; collapsing to a single
browser-parsed WS is a possible later optimization). Pinnacle closed its API and the x-session is minted by
the logged-in web app and rotates, so a real tab is the most reliable + least anomalous way to mint/hold it.

KEEPALIVE: the open tab is the primary anchor (its own heartbeats + WS hold the session). We add a gentle
human-like nudge (a tiny mouse move on a cadence) as belt-and-suspenders against a UI-inactivity logout —
NOT a hammer. The adapter additionally re-hits authed REST on a cadence (its existing session-keepalive).

CONFIG (env): PINNACLE_LOGIN_URL (default https://www.pinnacle.bet/en/), PINNACLE_USER_DATA_DIR (persistent
  profile, default .pinnacle_profile), PINNACLE_HEADLESS ("1" forces headless — DEFAULT headful so you can
  log in), PINNACLE_CHANNEL (default "chrome"; falls back to bundled Chromium), PINNACLE_BROWSER_ACTIVITY_SEC
  (gentle-activity cadence, default 200). Requires Playwright: `pip install playwright && playwright install chromium`.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import random
import re
import time
from pathlib import Path
from typing import Callable, Optional


def parse_mqtt_connect(buf: bytes) -> Optional[dict]:
    """Parse an MQTT 3.1.1 CONNECT packet → {client_id, username, password}. Returns None if `buf` is not a
    CONNECT (fixed-header byte 0x10) or is malformed. Pinnacle's WS password is the UTF-8 string
    '{x-session}|{suffix}'; the username is the account id. (Decode as UTF-8 — MQTT passwords are nominally
    binary but Pinnacle's is text.)"""
    if not buf or buf[0] != 0x10:
        return None
    try:
        i = 1
        # remaining-length varint (we don't need the value, just to advance past it)
        mult, _rem = 1, 0
        while True:
            b = buf[i]; i += 1
            _rem += (b & 0x7F) * mult
            if not (b & 0x80):
                break
            mult *= 128
            if mult > 128 ** 3:
                return None

        def rd(j):                                   # read a 2-byte-length-prefixed field → (bytes, next_index)
            n = (buf[j] << 8) | buf[j + 1]
            return buf[j + 2:j + 2 + n], j + 2 + n

        _proto, i = rd(i)                            # protocol name "MQTT"
        i += 1                                       # protocol level (0x04)
        flags = buf[i]; i += 1                       # connect flags
        i += 2                                       # keepalive (2 bytes)
        cid, i = rd(i)                               # client id
        if (flags >> 2) & 1:                         # will flag → skip will topic + will message
            _, i = rd(i)
            _, i = rd(i)
        username = password = None
        if (flags >> 7) & 1:                         # username present
            u, i = rd(i); username = u.decode("utf-8", "replace")
        if (flags >> 6) & 1:                         # password present
            p, i = rd(i); password = p.decode("utf-8", "replace")
        return {"client_id": cid.decode("utf-8", "replace"), "username": username, "password": password}
    except Exception:
        return None


def parse_mqtt_publish(buf: bytes):
    """Parse an MQTT 3.1.1 PUBLISH packet → (topic, payload_bytes); None if `buf` isn't a PUBLISH (fixed-header
    high nibble == 3). Used by the browser-WS-read feasibility probe to confirm odds frames flow off the page's
    own WS and for which league (the odds payload is JSON {op, pk, rec:{league:{id}...}})."""
    if not buf or (buf[0] >> 4) != 3:
        return None
    try:
        qos = (buf[0] >> 1) & 0x03
        i = 1
        mult = 1
        while True:                                   # skip the remaining-length varint
            b = buf[i]; i += 1
            if not (b & 0x80):
                break
            mult *= 128
            if mult > 128 ** 3:
                return None
        n = (buf[i] << 8) | buf[i + 1]; i += 2         # topic length + topic
        topic = buf[i:i + n].decode("utf-8", "replace"); i += n
        if qos > 0:
            i += 2                                     # packet identifier (QoS 1/2 only)
        return topic, buf[i:]
    except Exception:
        return None


def drain_mqtt_packets(buf: bytearray) -> list:
    """Pull every COMPLETE MQTT packet (full bytes incl. fixed header) off the FRONT of `buf`, consuming them,
    and return them as a list. Leaves an incomplete trailing packet in `buf` for the next call. This is the
    STREAM parser the window-WS reader needs: CDP delivers raw WS frames that are NOT 1:1 with MQTT packets (a
    big PUBLISH spans frames; a frame may hold several packets), so odds are parsed from the accumulated byte
    stream, not per-frame. Raises ValueError on a length desync so the caller can resync (clear the buffer)."""
    out = []
    while len(buf) >= 2:
        rem = 0; mult = 1; i = 1; vbytes = 0; ok = False
        while i < len(buf):
            byte = buf[i]; i += 1; vbytes += 1
            rem += (byte & 0x7F) * mult
            if not (byte & 0x80):
                ok = True
                break
            mult *= 128
            if vbytes >= 4:
                raise ValueError("MQTT remaining-length varint too long (stream desync)")
        if not ok:
            break                                      # varint not fully arrived yet — wait for more bytes
        if rem > 8_000_000:
            raise ValueError("absurd MQTT packet length (stream desync)")
        total = i + rem
        if len(buf) < total:
            break                                      # packet body not fully arrived yet
        out.append(bytes(buf[:total]))
        del buf[:total]
    return out


class PinnacleBrowserSession:
    """Headed Playwright window that mints + holds the Pinnacle session and pushes captured creds to the
    adapter via the `on_creds` callback. `on_creds(creds: dict)` is called on every change with keys:
    session, device, api_key, ws_user, ws_pass, ready."""

    def __init__(self, on_creds: Callable[[dict], None],
                 on_odds: Optional[Callable[[str, bytes], None]] = None,
                 on_idle_trim: Optional[Callable] = None) -> None:
        self._on_creds = on_creds
        self._on_odds = on_odds                        # window-WS reader: called per odds PUBLISH (topic, payload)
        # Optional () -> dict hook the adapter sets, reporting PAYLOAD-derived coverage (real league ids read
        # from rec.league.id) for the [WS-READ] line. The topic scan below can only see SUBSCRIPTION scopes —
        # the featured board's is sport-wide ('sp/33' = tennis), so topics alone can never name the individual
        # leagues on the main page. The payload can, and does.
        self.coverage_fn = None
        self._on_idle_trim = on_idle_trim              # async(page, source) -> tidy the board's side betslip while idle
        self._login_url = os.environ.get("PINNACLE_LOGIN_URL", "https://www.pinnacle.bet/en/")
        # ABSOLUTE, module-anchored profile dir so the SAME saved profile is reused no matter what CWD the
        # sidecar is launched from (a CWD-relative ".pinnacle_profile" would silently fragment into a fresh
        # login per launch dir). A relative env override is anchored too. Gitignored (holds cookies/session).
        _default_profile = Path(__file__).resolve().parent / ".pinnacle_profile"
        self._user_data = str(Path(os.environ.get("PINNACLE_USER_DATA_DIR") or _default_profile).expanduser().resolve())
        self._headless = os.environ.get("PINNACLE_HEADLESS") == "1"     # DEFAULT headful (you log in)
        self._channel = os.environ.get("PINNACLE_CHANNEL", "chrome")
        self._activity_sec = float(os.environ.get("PINNACLE_BROWSER_ACTIVITY_SEC", "120"))  # max normal gap; keepalive-dense
        self._relogin_min = float(os.environ.get("PINNACLE_RELOGIN_MIN", "20"))  # periodic page reload to re-mint the session (< ~30m TTL; 0=off)
        # UNATTENDED AUTO-LOGIN: on (re)open, if the page is sitting on a login form (session dropped across a
        # dark gap or a hard expiry), submit the Chrome-profile-AUTOFILLED credentials by pressing Enter. NO
        # credentials are typed or stored here — the saved profile fills email+password; we only submit, and
        # ONLY when the password field is already non-empty (so first-time MANUAL setup is untouched). Default
        # ON; PINNACLE_AUTO_LOGIN=0 disables (revert to manual login).
        self._auto_login = os.environ.get("PINNACLE_AUTO_LOGIN") != "0"
        self._login_check_sec = float(os.environ.get("PINNACLE_LOGIN_CHECK_SEC", "8"))         # how often to look for a login form
        self._login_submit_cooldown = float(os.environ.get("PINNACLE_LOGIN_SUBMIT_COOLDOWN", "30"))  # min gap between submit attempts
        # Don't re-login while the session is already LIVE. A logged-in Pinnacle tab emits authed API requests
        # constantly (each refreshes _last_capture); a login form present ALONGSIDE recent authed traffic means
        # we're logged in via cookies (a stray/autofilled form), and submitting it needlessly ROTATES the session
        # → the guest-redirect cascade + WS auth-reject churn seen 2026-07-14. So skip the submit while a capture
        # is this fresh; only re-login once authed traffic has been silent this long (a genuine logout).
        self._login_healthy_grace = float(os.environ.get("PINNACLE_LOGIN_HEALTHY_GRACE", "180"))
        # How long after the window opens we still accept "recent x-session traffic" ALONE as proof of a live
        # login. Past this, a live login must ALSO have produced a WS login — see _ensure_logged_in.
        self._ws_login_grace = float(os.environ.get("PINNACLE_WS_LOGIN_GRACE_SEC", "45"))
        self._opened_at = 0.0          # when the browser window was (re)opened, for the grace above
        self._last_login_submit = 0.0
        # ── HOW OLD IS THE ACCOUNT'S LOGIN? ─────────────────────────────────────────────────────────
        # NOT the same question as "how long has this sidecar had a session", and confusing the two cost a
        # session on 2026-08-20: Pinnacle logged the account out for "exceeding the maximum amount of time
        # logged in" while the log read "SESSION HELD 16m". Sixteen minutes was the SIDECAR's age — the
        # Chrome profile is persistent, the login was hours older, and nothing the bot tracked could see it.
        #
        # Stamped on the page's FIRST authed request after a submit we made, and persisted beside the
        # profile so a sidecar restart does not reset it. `None` when unknown (a reused profile that was
        # logged in before this bot ever ran) — unknown must stay distinguishable from zero, because
        # treating an ancient login as brand new is exactly the failure being fixed.
        self._login_stamp_path = Path(self._user_data).parent / ".pinnacle_login_age.json"
        self._login_at: float | None = self._read_login_stamp()
        # Re-login BEFORE the cap rather than being kicked mid-session. 0 = off, which is the default
        # because the real cap has never been measured — see _note_login_age_on_logout, which measures it.
        try:
            self._max_login_min = float(os.environ.get("PINNACLE_MAX_LOGIN_MIN", "0") or 0)
        except ValueError:
            self._max_login_min = 0.0
        self._login_task: Optional[asyncio.Task] = None
        self.last_page_status_ts = 0.0        # when the PAGE last requested /status (see _on_request)
        # sport pages the organic loop occasionally browses to (real session). Default = the home page only
        # (always valid); override with PINNACLE_BROWSE_URLS once you've confirmed the sport-page URLs.
        self._browse_urls = [u.strip() for u in os.environ.get("PINNACLE_BROWSE_URLS", "").split(",") if u.strip()] \
            or [self._login_url]
        # DROP BROWSE URLS FOR A SPORT WE ARE NOT TRADING. The organic loop navigates the primary page around
        # this list to keep the session alive, so a leftover tennis URL does not merely sit in the env — it
        # actively walks the board off the sport being watched, between drift checks, forever. HARDVEN_SPORTS
        # is the single source of truth for which sport we trade; anything here that disagrees with it is
        # stale configuration, and silently obeying it is how the page and the feed end up on different games.
        _slug = self._active_sport_slug()
        if _slug:
            _keep, _drop = [], []
            for _u in self._browse_urls:
                _m = re.search(r"/en/([a-z][a-z-]*)/", _u or "")
                (_keep if (not _m or _m.group(1) in ("account", "login") or _m.group(1) == _slug)
                        else _drop).append(_u)
            if _drop:
                print(f"[PINNACLE SESSION] dropped {len(_drop)} browse URL(s) for another sport "
                      f"(trading '{_slug}'): {', '.join(_drop)}", flush=True)
                self._browse_urls = _keep or [self._login_url]
        # HOME PAGE: where the main board should SIT once we're logged in. Landing on the site root leaves the
        # board showing whatever Pinnacle promotes, so the operator had to click through to the sport by hand
        # every session — and until they did, the board's sport-topic WS wasn't streaming the sport we trade.
        # Defaults to the first browsed sport's /matchups/ page; PINNACLE_HOME_URL overrides.
        self._home_url = (os.environ.get("PINNACLE_HOME_URL") or "").strip() or self._derive_home_url()
        self._went_home = False        # one-shot per session: don't fight the organic layer after the first hop
        # How long after a login SUBMIT the home-navigation must keep its hands off, so it can never abort an
        # in-flight auth POST (the redirect + first authed calls take a few seconds).
        self._home_settle_sec = float(os.environ.get("PINNACLE_HOME_SETTLE_SEC", "20"))
        # How long the board may sit AWAY from the trading sport before it is walked back. Long enough that
        # an operator can browse without being yanked, short enough that a stray click self-heals.
        self._board_drift_sec = float(os.environ.get("PINNACLE_BOARD_DRIFT_SEC", "180"))
        self._off_home_since = 0.0
        self._organic = None
        self._pw = None
        self._ctx = None
        self._page = None
        self._banking_hold = False        # operator banking window → suppress the session-refresh reload
        self._manual_hold  = False        # operator is driving → also suppress the board-drift watchdog
        self._activity_task: Optional[asyncio.Task] = None
        # captured creds
        self._session = ""
        self._device = ""
        self._api_key = ""
        self._ws_user = ""
        self._ws_suffix = ""
        self._have_ws = False
        self._last_capture = 0.0
        self._last_main_refresh = 0.0     # last time the MAIN board page was (re)loaded — its keepalive clock
        # Camping postpones the session refresh (reload() destroys an armed betslip), bounded so the
        # x-session still gets re-minted. See set_camp_hold.
        self._camp_hold = False
        self._camp_skips = 0
        self._camp_max_skips = int(os.environ.get("PINNACLE_CAMP_MAX_SKIPS", "2"))
        self._ready_announced = False
        # diagnostics: surface WHERE capture is stuck (x-session vs the MQTT-CONNECT WS login)
        self._status_task: Optional[asyncio.Task] = None
        self._session_refresh_task: Optional[asyncio.Task] = None
        self._seen_req = False
        self._seen_ws = False
        self._logged_session = False
        # CIRCUIT BREAKER on repeated failed logins. Submitting credentials over and over is the one
        # retry loop on this bot with a real-world cost: it is what credential-stuffing looks like, and
        # this account has already been shown a captcha once. After PINNACLE_LOGIN_MAX_FAILS consecutive
        # submits that never produce a session, stop and go dark rather than keep knocking.
        self._login_fail_streak = 0
        self._login_locked_out = False
        self._login_max_fails = int(os.environ.get('PINNACLE_LOGIN_MAX_FAILS', '5'))
        self._login_verify_sec = float(os.environ.get('PINNACLE_LOGIN_VERIFY_SEC', '20'))
        # SETTLE BEFORE SUBMITTING. A reload re-mints the session and re-renders the form, and the watcher
        # ticks fast enough to catch the page mid-mount: observed 2026-08-18 submitting the instant a force
        # re-mint finished, which made the page flicker, produced a session that failed validation, and only
        # worked on the following attempt. Chrome also needs a beat to actually autofill. So a submit waits
        # this long after any navigation before it is allowed to fire.
        self._login_settle_sec = float(os.environ.get('PINNACLE_LOGIN_SETTLE_SEC', '4'))
        self._login_submit_at = 0.0
        # REST-SIDE PROOF OF A LOGOUT. The DOM signal only works where a LOG IN control is rendered, and
        # Pinnacle does NOT render one on the sport boards (verified 2026-08-18: /tennis/matchups/ carries
        # odds buttons and an icon button, nothing else). The adapter, meanwhile, knows the session is dead
        # the moment authed REST starts guest-redirecting. So it tells us, and the watcher navigates to the
        # login URL where the form demonstrably exists.
        self._known_logged_out = ""
        self._ever_logged_in = False   # have we EVER captured a session with this profile? (evidence creds are saved)
        self._ws_urls_seen: set = set()
        self._debug_storage = os.environ.get("PINNACLE_DEBUG_STORAGE") == "1"   # dump localStorage on capture
        self._logged_storage = False
        self._cdp = None
        self._cdp_ws_reqs: set = set()
        self._cdp_sessions: list = []          # per-tab CDP sessions (multi-tab WS capture: a persistent context
                                               # has no Browser handle for one browser-level session, so we attach
                                               # a CDP session PER PAGE and merge frames by globally-unique requestId)
        self._cdp_attached_pages: set = set()  # id(page) already CDP-armed → idempotent attach
        # WINDOW-WS READER (PINNACLE_WINDOW_WS_READ=1): the real thing — parse odds PUBLISH off the page's own WS
        # and hand each to the adapter via on_odds. The FEASIBILITY PROBE (PINNACLE_WS_READ_PROBE=1) is the same
        # capture minus the on_odds handoff, plus the periodic verdict log. Either arms the received-frame path.
        self._window_ws_read = on_odds is not None
        self._ws_read_probe = os.environ.get("PINNACLE_WS_READ_PROBE") == "1"
        self._ws_stream_buf: dict = {}   # per-Arcadia-WS-requestId byte buffer for MQTT stream reassembly
        self._probe_recv_total = 0       # all received CDP WS frames on the Arcadia socket
        self._probe_recv_publish = 0     # of those, MQTT PUBLISH (an odds update)
        self._probe_leagues: dict = {}   # league id -> last ts we saw a PUBLISH for it (CURRENTLY-active, not
                                         # accumulated) — so a multi-league test shows which leagues STILL stream
        self._probe_topics: set = set()  # distinct topic shapes (capped)
        self._probe_odds_ok = False      # confirmed a PUBLISH payload parses as odds JSON
        self._probe_start = 0.0
        self._probe_task = None
        self._arcadia_last_frame = 0.0   # ts of the last ANY frame on an Arcadia WS (odds OR MQTT keepalive) —
                                         # a CONNECTION heartbeat for odds_ws_alive() (survives quiet odds spells)

    # ── readiness / status ───────────────────────────────────────────────────────
    @property
    def ready(self) -> bool:
        # READY needs the x-session (REST auth) AND the WS login (account id + suffix → MQTT password). The
        # WS pieces only appear once the page opens its odds WebSocket, which it does after you browse a sport.
        return bool(self._session and self._ws_user and self._ws_suffix)

    def main_page_age(self) -> Optional[float]:
        """Seconds since the MAIN board page was last (re)loaded — its keepalive clock (reloaded every
        PINNACLE_RELOGIN_MIN). None if it hasn't loaded yet. Surfaced so the tab manager's freshness line can
        show the main page alongside the dedicated tabs + rove."""
        return (time.time() - self._last_main_refresh) if self._last_main_refresh else None

    def status(self) -> dict:
        return {
            "ready": self.ready,
            "has_session": bool(self._session),
            "has_ws_creds": self._have_ws,
            "account": (self._ws_user[:3] + "***") if self._ws_user else "",
            "last_capture_age_sec": round(time.time() - self._last_capture, 1) if self._last_capture else None,
            "headless": self._headless,
        }

    def odds_ws_alive(self, ttl: float = 150.0) -> bool:
        """CONNECTION-based liveness for the window-WS reader: True while the browser's Arcadia odds WS is OPEN and
        exchanging frames. Two conditions: at least one Arcadia socket is currently open (a clean webSocketClosed
        empties the set → immediate false), AND a frame arrived within `ttl`s. Because MQTT keepalive PINGRESP
        arrives even when NO line is moving, this stays True through a quiet-but-connected feed — so a stable
        pre-match price correctly reads LIVE — and flips false only on a real drop (no frames, not even pings, for
        ttl) or logout. Superior to an odds-recency gate, which false-deads a stable line the moment it stops
        ticking. Requires the received-frame path to be armed (probe or reader mode)."""
        if not self._cdp_ws_reqs:
            return False                                  # no Arcadia odds WS open (clean close detected)
        return self._arcadia_last_frame > 0 and (time.time() - self._arcadia_last_frame) < ttl

    # ── lifecycle ────────────────────────────────────────────────────────────────
    async def start(self) -> None:
        from playwright.async_api import async_playwright
        self._opened_at = time.time()
        reused = Path(self._user_data).exists()
        print(f"[PINNACLE SESSION] {'reusing SAVED' if reused else 'creating NEW'} Chrome profile: {self._user_data}"
              + ("" if reused else " (log in once; it'll be remembered next run)"))
        self._pw = await async_playwright().start()
        # Park the browser WINDOW where it won't cover your desktop — e.g. a second monitor or a dummy-HDMI
        # display. PINNACLE_WINDOW_POS="1920,0" puts it on a 1080p display to the right; PINNACLE_WINDOW_SIZE
        # ="1920,1080" sizes it. Purely an OS window placement (Chrome flags aren't visible to page JS), so it
        # changes NOTHING about the fingerprint — same Windows Chrome, same profile, still fully headed.
        _pos  = (os.environ.get("PINNACLE_WINDOW_POS")  or "").strip()
        _size = (os.environ.get("PINNACLE_WINDOW_SIZE") or "").strip()
        _win_args = []
        if _pos:
            _win_args.append(f"--window-position={_pos}")
        if _size:
            _win_args.append(f"--window-size={_size}")
        if _win_args:
            print(f"[PINNACLE SESSION] window placement: {' '.join(_win_args)} "
                  "(keeps the bot off your primary screen; no fingerprint change).")
        launch = dict(user_data_dir=self._user_data, headless=self._headless,
                      viewport={"width": 1400, "height": 900},
                      args=[*_win_args,
                            "--disable-blink-features=AutomationControlled",
                            # KEEP THE SESSION ALIVE WHEN BACKGROUNDED: Chrome throttles/freezes background-tab
                            # timers, which stops Pinnacle's own setInterval session-refresh → ~30-min logout when
                            # you walk away and the window drops behind others. These keep its timers running.
                            # (Launch flags aren't visible to page JS → no detection cost.)
                            "--disable-background-timer-throttling",
                            "--disable-backgrounding-occluded-windows",
                            "--disable-renderer-backgrounding"],
                      ignore_default_args=["--enable-automation"])
        try:
            self._ctx = await self._pw.chromium.launch_persistent_context(channel=self._channel, **launch)
        except Exception as e:
            print(f"[PINNACLE SESSION] channel='{self._channel}' unavailable ({e}); using bundled Chromium "
                  "(install Chrome or set PINNACLE_CHANNEL for the most human-like fingerprint).")
            self._ctx = await self._pw.chromium.launch_persistent_context(**launch)
        await self._ctx.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined});")
        self._ctx.on("request", self._on_request)        # creds from the site's own Arcadia request headers
        self._ctx.on("page", self._wire_page)            # wire WS capture on any future page/tab too
        self._page = self._ctx.pages[0] if self._ctx.pages else await self._ctx.new_page()
        self._wire_page(self._page)
        await self._start_cdp_capture()                  # 2nd WS-login capture path (sees worker WS page.on misses)
        try:
            # LAND WHERE WE TRADE, as a bookmark would. This opened the site ROOT and then _go_home_once
            # navigated away a moment later - so every session began with a page change right on top of the
            # login, which is the sequence the operator saw log the site straight back out (2026-08-19).
            # Starting on the destination removes that navigation entirely in the normal case: already
            # authenticated, already where we belong, nothing to move. When we are NOT logged in the login
            # flow still navigates to the login URL on its own, so nothing depends on landing there first.
            start_url = self._home_url or self._login_url
            await self._page.goto(start_url, wait_until="domcontentloaded", timeout=60_000)
            print(f"[PINNACLE SESSION] opened on {start_url}", flush=True)
            self._last_main_refresh = time.time()
        except Exception as ex:
            print(f"[PINNACLE SESSION] initial navigation slow/failed ({ex}); the window is open — browse manually.")
        # Human-like idle activity (replaces the old nudge). PINNACLE_ORGANIC=0 disables it — the session still
        # holds via the authed-REST keepalive, so this is a clean toggle for isolating the gestures in testing.
        if os.environ.get("PINNACLE_ORGANIC") != "0":
            from organic import OrganicActivity
            self._organic = OrganicActivity(self._page, browse_urls=self._browse_urls, max_gap=self._activity_sec,
                                            trim_fn=self._on_idle_trim)
            sports = [s[0] for s in self._organic._sports]
            print(f"[PINNACLE ORGANIC] active — sports to flip: {sports or '(NONE — set PINNACLE_BROWSE_URLS to sport /matchups/ pages)'} | "
                  f"browse_urls={len(self._browse_urls)} | gaps ≤{self._activity_sec:g}s")
            self._activity_task = asyncio.create_task(self._organic.run())
        else:
            print("[PINNACLE SESSION] PINNACLE_ORGANIC=0 — organic activity OFF (session held by REST keepalive only).")
        self._status_task = asyncio.create_task(self._status_loop())
        if self._relogin_min > 0:
            self._session_refresh_task = asyncio.create_task(self._session_refresh_loop())
            print(f"[PINNACLE SESSION] session-refresh keepalive ON — page reload every {self._relogin_min:g}m to re-mint "
                  "(the reliable fix vs the ~30m idle logout; PINNACLE_RELOGIN_MIN=0 to disable).")
        if self._auto_login:
            self._login_task = asyncio.create_task(self._login_watch_loop())
            print("[PINNACLE SESSION] auto-login watcher ON — presses Enter on an autofilled login form to re-auth "
                  "unattended across dark gaps (profile fills the credentials; PINNACLE_AUTO_LOGIN=0 to disable).")
        print("\n" + "=" * 78)
        print("[PINNACLE SESSION] LOG IN in the Pinnacle window that just opened.")
        print("  - If the saved profile already remembers you, capture is automatic.")
        print("  - Browse to ANY sport once so the page opens its odds WebSocket (that yields the WS login).")
        print("  The C# bot stays idle until /health reports session_ready=true. Keep this window OPEN.")
        print("=" * 78 + "\n")
        await self._open_test_tabs()                     # PINNACLE_TAB_TEST: multi-tab background-WS survival test

    async def stop(self) -> None:
        for t in (self._activity_task, self._status_task, self._session_refresh_task, self._login_task,
                  self._probe_task):
            if t and not t.done():
                t.cancel()
        try:
            if self._ctx is not None:
                await self._ctx.close()
        except Exception:
            pass
        try:
            if self._pw is not None:
                await self._pw.stop()
        except Exception:
            pass
        # NULL state so start() can cleanly RE-OPEN on the next scheduled window (lifecycle cycles start/stop).
        # Captured creds are intentionally kept (the adapter still has them); the reopened profile re-emits them.
        self._pw = self._ctx = self._page = self._organic = None
        self._activity_task = self._status_task = self._session_refresh_task = self._login_task = None
        self._probe_task = self._cdp = None
        self._cdp_sessions = []
        self._cdp_attached_pages = set()
        self._cdp_ws_reqs = set()
        self._ws_stream_buf = {}

    # ── capture: REST headers (x-session / device / api-key) ──────────────────────
    def _on_request(self, request) -> None:
        try:
            if "arcadia.pinnacle.com" not in (request.url or ""):
                return
            # THE PAGE'S OWN LIVENESS PING. The adapter used to fire its own GET /status every 30s as
            # "camouflage" — but if the tab already polls it, ours is a DUPLICATE arriving on a separate
            # httpx connection with a different TLS fingerprint, next to the real one. That is worse than
            # sending nothing: it is the same request twice, and only one of them looks like the browser.
            # Recording the page's calls lets the adapter stand down whenever the real thing is running.
            if "/status" in (request.url or ""):
                self.last_page_status_ts = time.time()
                if not getattr(self, "_status_seen", False):
                    self._status_seen = True
                    print("[PINNACLE SESSION] the page polls /status itself — our own liveness ping will "
                          "stand down while it does.", flush=True)
            if not self._seen_req:
                self._seen_req = True
                print("[PINNACLE SESSION] seeing Arcadia API requests from the page (auth headers visible).")
            h = request.headers                          # Playwright lowercases header names
            sess = h.get("x-session")
            dev = h.get("x-device-uuid")
            key = h.get("x-api-key")
            changed = False
            if sess and not self._logged_session:
                self._logged_session = True
                self._ever_logged_in = True   # profile just proved it holds a live login → auto-login may submit later
                # A capture is the ONLY thing that proves a submit worked, so it is the only thing that
                # clears the breaker. Anything else (a form disappearing, a redirect) can happen without
                # being logged in.
                if self._login_fail_streak:
                    print(f"[PINNACLE SESSION] login recovered after {self._login_fail_streak} failed "
                          f"attempt(s) - breaker reset.")
                self._login_fail_streak = 0
                submitted = self._login_submit_at
                self._login_submit_at = 0.0
                self._known_logged_out = ""
                print("[PINNACLE SESSION] captured x-session (REST auth ready).")
                # Only a capture that follows OUR submit starts the clock. A capture off a profile that was
                # already signed in says nothing about when the login happened.
                if submitted:
                    self.note_login_established()
                self._logout_age_noted = False        # next logout is a new measurement
            if sess and sess != self._session:
                self._session = sess; changed = True
            if dev and dev != self._device:
                self._device = dev; changed = True
            if key and key != self._api_key:
                self._api_key = key; changed = True
            if sess:
                self._last_capture = time.time()
            if changed:
                self._emit()
        except Exception:
            pass

    # ── capture: MQTT CONNECT frame (WS username + |suffix) ───────────────────────
    def _wire_page(self, page) -> None:
        try:
            page.on("websocket", self._on_websocket)
        except Exception:
            pass

    def _on_websocket(self, ws) -> None:
        url = getattr(ws, "url", "") or ""
        if url and url not in self._ws_urls_seen and len(self._ws_urls_seen) < 10:
            self._ws_urls_seen.add(url)                   # log every distinct WS the PAGE opens (worker test)
            print(f"[PINNACLE SESSION] WS opened by page: {url[:90]}")
        if "arcadia.pinnacle.com" not in url:
            return                                       # only the Arcadia MQTT socket — wire EVERY open (not just
                                                          # the first) so a reload's fresh CONNECT re-captures the suffix
        if not self._seen_ws:
            self._seen_ws = True
            print("[PINNACLE SESSION] Arcadia WS visible to Playwright — watching its frames for the MQTT CONNECT.")
        try:
            ws.on("framesent", self._on_ws_frame)
        except Exception:
            pass

    def _on_ws_frame(self, payload) -> None:
        if not isinstance(payload, (bytes, bytearray)):  # MQTT is binary; text isn't CONNECT
            return
        self._handle_connect_bytes(bytes(payload), "page.on")

    def _handle_connect_bytes(self, buf: bytes, src: str) -> None:
        """Shared by the page.on AND CDP capture paths: parse an MQTT CONNECT → WS username (account id) +
        '|suffix'. LATEST-WINS (not first-wins): re-capture on EVERY CONNECT so a rotated suffix — Pinnacle issues
        a new one on a fresh login/session — is picked up. The WS password is reconstructed as
        '{x-session}|{suffix}', so a stale cached suffix after a re-login is exactly what auth-rejects paho on
        reconnect; re-capturing it here keeps the reconstructed password valid across reopens/reloads."""
        parsed = parse_mqtt_connect(buf)
        if not parsed:
            return
        user = parsed.get("username") or ""
        pw = parsed.get("password") or ""
        if not user or "|" not in pw:
            return
        new_suffix = pw.rsplit("|", 1)[1]
        if self._have_ws and user == self._ws_user and new_suffix == self._ws_suffix:
            return                                        # unchanged → nothing to re-emit
        was = self._have_ws
        self._ws_user = user
        self._ws_suffix = new_suffix
        self._have_ws = True
        print(f"[PINNACLE SESSION] {'re-captured' if was else 'captured'} WS login via {src} "
              f"(account {user[:3]}***, suffix '{new_suffix}').")
        self._emit()

    # ── capture: CDP (Network domain) — sees WebSockets page.on('websocket') misses (incl. Web Workers) ──
    async def _start_cdp_capture(self) -> None:
        """The robust second path: Pinnacle's odds WS likely runs in a Web Worker that page.on('websocket')
        can't see, so the MQTT CONNECT (which carries the WS login) may only be visible at the CDP Network
        level. We enable Network on the PAGE target and AUTO-ATTACH to workers so a worker WS is at least
        DETECTED (and on many Chrome builds its frames surface here). Binary frames (MQTT) arrive base64-encoded
        → decoded before parsing. Best-effort: runs ALONGSIDE page.on; whichever sees the CONNECT first wins.

        MULTI-TAB: a persistent context exposes no Browser handle for a single browser-level CDP session, so we
        attach a per-page session to the primary page AND to every future tab (via the context 'page' event).
        A league page is board-scoped and subscriptions don't accumulate, so full-slate coverage means one tab
        per league; every tab's frames merge into the SHARED reader buffers keyed by globally-unique requestId."""
        self._cdp_sessions = []
        ok = await self._attach_cdp_to_page(self._page, primary=True)
        if not ok:
            return                                       # CDP unavailable → page.on capture only (old behaviour)
        # Cover every tab the user opens later: arm a CDP session as each page is created.
        self._ctx.on("page", lambda p: asyncio.create_task(self._attach_cdp_to_page(p)))
        if self._ws_read_probe or self._window_ws_read:
            self._probe_start = time.time()
            self._probe_task = asyncio.create_task(self._ws_read_probe_loop())
            mode = "READER (odds → adapter)" if self._window_ws_read else "PROBE (count only)"
            print(f"[WS-READ] window-WS {mode} armed (multi-tab) — parsing odds PUBLISH off EVERY tab's OWN WS. "
                  "Summary every 15s. Keep sport boards open (one league per tab for full coverage).")

    async def _attach_cdp_to_page(self, page, primary: bool = False) -> bool:
        """Arm CDP Network capture on ONE page/tab: WS-created + sent-frame (MQTT CONNECT → WS login) always, and
        received-frame (odds PUBLISH → reader) when the probe/reader is on. Called for the primary page and, via
        the context 'page' event, for every tab the user opens — so a one-league-per-tab layout streams all their
        odds into the shared reader. Idempotent per page (guarded by id(page)). Returns False only if CDP itself
        is unavailable on the primary page. Never raises."""
        if id(page) in self._cdp_attached_pages:
            return True                                  # already armed (primary + page-event can both fire)
        self._cdp_attached_pages.add(id(page))
        try:
            cdp = await self._ctx.new_cdp_session(page)
        except Exception as ex:
            self._cdp_attached_pages.discard(id(page))
            if primary:
                print(f"[PINNACLE SESSION] CDP unavailable ({type(ex).__name__}: {ex}); page.on capture only.")
            return False
        cdp.on("Network.webSocketCreated", self._on_cdp_ws_created)
        cdp.on("Network.webSocketFrameSent", self._on_cdp_ws_frame)
        cdp.on("Target.attachedToTarget", self._on_cdp_target)
        if self._ws_read_probe or self._window_ws_read:
            cdp.on("Network.webSocketFrameReceived", self._on_cdp_ws_frame_recv)
            cdp.on("Network.webSocketClosed", self._on_cdp_ws_closed)
        try:
            await cdp.send("Network.enable")
            await cdp.send("Target.setAutoAttach",
                           {"autoAttach": True, "waitForDebuggerOnStart": False, "flatten": True})
        except Exception as ex:
            self._cdp_attached_pages.discard(id(page))
            if primary:
                print(f"[PINNACLE SESSION] CDP enable failed ({type(ex).__name__}: {ex}); page.on capture only.")
            return False
        self._cdp_sessions.append(cdp)
        page.on("close", lambda: self._cdp_attached_pages.discard(id(page)))  # allow re-arm if id is reused
        if primary:
            self._cdp = cdp
            print("[PINNACLE SESSION] CDP Network capture armed (worker-aware, multi-tab) — 2nd path for the WS login.")
        else:
            print(f"[PINNACLE SESSION] CDP armed on a new tab (tabs captured: {len(self._cdp_sessions)}).")
        return True

    async def open_tab(self, url: str):
        """Open a NEW tab, arm CDP capture on it BEFORE navigating (so its odds WS is caught from
        webSocketCreated), navigate to `url`, and return the page. This is how the league tab manager gives the
        reader league-scoped coverage (one tab per gap league). Returns None on failure. Never raises."""
        if self._ctx is None:
            return None
        try:
            pg = await self._ctx.new_page()
            await self._attach_cdp_to_page(pg)           # arm BEFORE navigation so we catch webSocketCreated
            await pg.goto(url, wait_until="domcontentloaded", timeout=60_000)
            return pg
        except Exception as ex:
            print(f"[PINNACLE SESSION] open_tab failed for {url[:70]} ({type(ex).__name__}: {ex}).")
            return None

    async def close_tab(self, page) -> None:
        """Close a tab opened by the tab manager (its CDP state is freed by the page 'close' handler). Best-effort."""
        try:
            if page is not None:
                await page.close()
        except Exception:
            pass

    async def navigate_tab(self, page, url: str) -> bool:
        """Re-point an EXISTING tab to `url` (the roving tail tab reuses one page, sweeping league to league). The
        page's CDP session persists across navigations, so the new league's odds WS is captured just like a fresh
        tab (old WS closes, new one opens — both on the same session). Returns False on failure. Never raises."""
        if page is None:
            return False
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
            return True
        except Exception as ex:
            print(f"[PINNACLE SESSION] navigate_tab failed for {url[:70]} ({type(ex).__name__}: {ex}).")
            return False

    async def fetch_via_page(self, url: str, headers: dict, timeout_ms: int = 15000) -> dict:
        """Feasibility probe: run fetch() INSIDE the logged-in page (genuine Chrome TLS + the page's own origin/
        cookies) so we can test moving the re-seed off the sidecar's httpx into the browser. Returns diagnostics
        {ok, status, n_markets, sample} or {ok:false, error} (a CORS/preflight block on the page's fetch is the
        thing this is checking for). Never raises."""
        if self._page is None:
            return {"ok": False, "error": "no page"}
        js = """async ([url, headers]) => {
          try {
            const r = await fetch(url, {headers, credentials: 'include'});
            let body = null;
            try { body = await r.json(); } catch (e) {}
            const n = Array.isArray(body) ? body.length : (body ? -1 : 0);
            const s = (Array.isArray(body) && body[0]) ? body[0] : null;
            return {ok: r.ok, status: r.status, n_markets: n,
                    sample: s ? {type: s.type, period: s.period, matchupId: s.matchupId,
                                 prices: (s.prices || []).length} : null};
          } catch (e) { return {ok: false, error: String(e)}; }
        }"""
        try:
            return await asyncio.wait_for(self._page.evaluate(js, [url, headers]), timeout=timeout_ms / 1000.0)
        except Exception as ex:
            return {"ok": False, "error": f"{type(ex).__name__}: {ex}"}

    async def _open_test_tabs(self) -> None:
        """PINNACLE_TAB_TEST=<url1>,<url2>[,...] — open each URL in its OWN tab to test multi-tab WS survival.
        In a real headed window only one tab is foregrounded, so the rest are BACKGROUND tabs; watch the
        [WS-READ] 'active(<45s)' line. If the BACKGROUND tabs' league ids stay listed while only one tab is
        focused, background-tab WS survive → one-league-per-tab coverage is viable (the anti-throttle launch
        flags are what should keep those tabs' odds sockets alive). Tabs are left open; focus/navigate by hand."""
        urls = [u.strip() for u in os.environ.get("PINNACLE_TAB_TEST", "").split(",") if u.strip()]
        if not urls:
            return
        print(f"[TAB-TEST] opening {len(urls)} tab(s) for the background-WS survival test:")
        for u in urls:
            pg = await self.open_tab(u)
            if pg is not None:
                print(f"[TAB-TEST]   opened tab → {u[:80]}")
            await asyncio.sleep(2.0)                      # let its odds WS subscribe before opening the next
        print("[TAB-TEST] tabs open. Focus ONE tab; watch [WS-READ] active(<45s). If ALL league ids stay listed "
              "while only one tab is focused, background tabs survive → multi-tab coverage works.")

    def _on_cdp_ws_closed(self, params: dict) -> None:
        reqid = params.get("requestId")
        self._ws_stream_buf.pop(reqid, None)          # free the reassembly buffer for a closed WS
        self._cdp_ws_reqs.discard(reqid)              # drop it from the OPEN set so odds_ws_alive sees the close

    def _on_cdp_ws_frame_recv(self, params: dict) -> None:
        """Feed each SERVER->CLIENT frame on the Arcadia WS into a per-connection byte buffer, drain COMPLETE MQTT
        packets from it (frames are NOT 1:1 with packets), and for each PUBLISH hand the odds to the adapter
        (reader) + tally the probe counters. Sync + fast (runs on the loop; the drain is a cheap byte-walk)."""
        reqid = params.get("requestId")
        if reqid not in self._cdp_ws_reqs:
            return                                        # only the Arcadia odds socket (skip localhost/devtools WS)
        self._arcadia_last_frame = time.time()            # ANY frame (odds OR MQTT keepalive/pong) = WS heartbeat
        resp = params.get("response") or {}
        data = resp.get("payloadData")
        if not data:
            return
        op = resp.get("opcode")
        if op is not None and op >= 8:
            return                                        # control frame (close/ping/pong) — not MQTT data
        try:
            chunk = base64.b64decode(data) if op in (0, 2) else data.encode("utf-8", "replace")
        except Exception:
            return
        buf = self._ws_stream_buf.get(reqid)
        if buf is None:
            buf = bytearray(); self._ws_stream_buf[reqid] = buf
        buf += chunk
        if len(buf) > 8_000_000:                          # runaway (desync) — drop and resync from the next frame
            buf.clear(); return
        try:
            packets = drain_mqtt_packets(buf)
        except ValueError:
            buf.clear(); return                           # length desync — resync
        for pkt in packets:
            self._probe_recv_total += 1
            parsed = parse_mqtt_publish(pkt)
            if not parsed:
                continue                                  # CONNACK / SUBACK / PINGRESP / PUBACK — not odds
            topic, payload = parsed
            if not (3 <= len(topic) <= 120 and re.match(r"^[\w./-]+$", topic)):
                continue                                  # defensive: a mis-framed packet
            self._probe_recv_publish += 1
            if len(self._probe_topics) < 40:
                self._probe_topics.add(topic[:48])
            # SUBSCRIPTION SCOPE, not league: 'sp/33' is the sport-wide featured-board topic (33 = tennis),
            # 'lg/221309' is one league page's topic. Tagged so the [WS-READ] line can't be misread as
            # "only league 33 is covered" — the board streams many leagues under that one sport topic.
            m = re.search(r"/(sp|lg)/(\d+)", topic)
            if m:
                self._probe_leagues[f"{m.group(1)}/{m.group(2)}"] = time.time()
            elif (m2 := re.search(r"matchups/(\d+)", topic)):
                self._probe_leagues[f"mu/{m2.group(1)}"] = time.time()   # single-MATCHUP topic, not a league
            if not self._probe_odds_ok:                   # confirm ONCE that a PUBLISH payload is real odds JSON
                try:
                    obj = json.loads(payload.decode("utf-8", "replace"))
                    if isinstance(obj, dict) and ("op" in obj or "rec" in obj):
                        self._probe_odds_ok = True
                        print(f"[WS-READ] CONFIRMED odds JSON in a received PUBLISH — topic='{topic[:60]}' "
                              f"keys={list(obj)[:6]}")
                except Exception:
                    pass
            if self._on_odds is not None:                 # READER: route the odds into the adapter's cache path
                try:
                    self._on_odds(topic, payload)
                except Exception:
                    pass

    async def _ws_read_probe_loop(self) -> None:
        """PROBE verdict logger: every 15s report received-frame / PUBLISH / league counts so BOTH 'odds flow'
        and 'no frames at all' (worker-hidden) are visible. Runs until the session stops."""
        while True:
            try:
                await asyncio.sleep(15)
            except asyncio.CancelledError:
                break
            now = time.time()
            el = now - self._probe_start
            active = sorted(k for k, ts in self._probe_leagues.items() if now - ts < 45)   # streaming in last 45s
            n, pub = self._probe_recv_total, self._probe_recv_publish
            verdict = ("GREEN — odds flow off the page WS" if pub > 0 and self._probe_odds_ok
                       else "AMBER — WS frames but no odds PUBLISH yet (open a sport with live odds?)" if n > 0
                       else "RED — NO received frames captured (odds WS likely worker-hidden → plan in-page shim)")
            tag = "WS-READ" if self._window_ws_read else "WS-READ-PROBE"
            # Topic scopes tell us WHICH SUBSCRIPTIONS are streaming (sp/=board sport-wide, lg/=one league tab).
            # Payload coverage (via coverage_fn) tells us WHICH REAL LEAGUES are actually pushing — the number
            # that matters, and the one the board's single sport topic hides.
            cov = ""
            if callable(self.coverage_fn):
                try:
                    c = self.coverage_fn() or {}
                    cov = (f" | leagues pushing={c.get('leagues', 0)} "
                           f"(board={c.get('board', 0)}, tabs={c.get('tabs', 0)}) matchups={c.get('matchups', 0)}")
                except Exception:
                    cov = ""
            print(f"[{tag}] {el:.0f}s | recv={n} PUBLISH(odds)={pub} topics={len(active)} "
                  f"odds_json={'yes' if self._probe_odds_ok else 'no'} | {verdict}{cov}"
                  + (f" | topics(<45s): {active[:12]}" if active else ""))

    def _on_cdp_ws_created(self, params: dict) -> None:
        url = params.get("url", "") or ""
        if url and url not in self._ws_urls_seen and len(self._ws_urls_seen) < 12:
            self._ws_urls_seen.add(url)
            print(f"[PINNACLE SESSION] WS seen via CDP: {url[:90]}")
        if "arcadia.pinnacle.com" in url:
            self._cdp_ws_reqs.add(params.get("requestId"))
            if not self._seen_ws:
                self._seen_ws = True
                print("[PINNACLE SESSION] Arcadia WS visible via CDP — watching frames for the MQTT CONNECT.")

    def _on_cdp_ws_frame(self, params: dict) -> None:
        # Parse EVERY sent frame (parse_mqtt_connect self-validates on byte 0x10 + structure) — don't gate on
        # requestId or _have_ws, so a reload's fresh CONNECT re-captures a rotated suffix (latest-wins).
        resp = params.get("response") or {}
        data = resp.get("payloadData")
        if not data:
            return
        try:
            buf = base64.b64decode(data) if resp.get("opcode") == 2 else data.encode("utf-8", "replace")
        except Exception:
            return
        self._handle_connect_bytes(buf, "CDP")

    def _on_cdp_target(self, params: dict) -> None:
        info = params.get("targetInfo") or {}
        if info.get("type") in ("worker", "service_worker", "shared_worker"):
            print(f"[PINNACLE SESSION] worker target attached: {info.get('type')} {(info.get('url') or '')[:70]} "
                  "— if the WS login never captures, the odds WS is likely IN HERE (storage probe is the fallback).")

    async def _probe_storage(self) -> None:
        """Fallback for a worker-hosted WS we can't frame-capture: the page JS builds the MQTT password
        '{x-session}|{suffix}' client-side, so the suffix (often the whole password) lives in localStorage/
        sessionStorage. Once x-session is known, scan storage for a value STARTING WITH it + containing '|' →
        that's the WS password → grab the suffix. PINNACLE_DEBUG_STORAGE=1 dumps all keys (masked) so the
        account-id key can be pinned down on the first login (the suffix alone isn't enough — we also need the
        username/account id, which the CONNECT frame gives directly)."""
        if self._have_ws or not self._session or self._page is None:
            return
        try:
            store = await self._page.evaluate(
                "() => { const o={}; for (const s of [localStorage, sessionStorage]) { "
                "for (let i=0;i<s.length;i++){ const k=s.key(i); o[k]=s.getItem(k); } } return o; }")
        except Exception:
            return
        if self._debug_storage and store and not self._logged_storage:
            self._logged_storage = True
            print("[PINNACLE SESSION] --- storage dump (find the account-id + suffix keys) ---")
            for k, v in store.items():
                vs = v if isinstance(v, str) else str(v)
                print(f"   {k} = {vs[:24]}…({len(vs)})")
        for k, v in (store or {}).items():
            if isinstance(v, str) and self._session and v.startswith(self._session) and "|" in v[len(self._session):]:
                suffix = v.rsplit("|", 1)[1]
                if suffix and not self._ws_suffix:
                    self._ws_suffix = suffix
                    print(f"[PINNACLE SESSION] WS password found in storage '{k}' → suffix '{suffix}' "
                          "(still need the account id for the username — see the storage dump).")
                    self._emit()
                return

    # ── push creds to the adapter ─────────────────────────────────────────────────
    def _emit(self) -> None:
        ws_pass = f"{self._session}|{self._ws_suffix}" if (self._session and self._ws_suffix) else ""
        creds = {"session": self._session, "device": self._device, "api_key": self._api_key,
                 "ws_user": self._ws_user, "ws_pass": ws_pass, "ready": self.ready}
        try:
            self._on_creds(creds)
        except Exception as ex:
            print(f"[PINNACLE SESSION] on_creds callback error: {type(ex).__name__}: {ex}")
        if self.ready and not self._ready_announced:
            self._ready_announced = True
            print("\n" + "=" * 78)
            print("[PINNACLE SESSION] OK SESSION CAPTURED — credentials live. The bot is GO.")
            print("  x-session + WS login captured; the adapter feed will seed + connect with them.")
            print("  Keep this window OPEN — it holds the session. Do not log out.")
            print("=" * 78 + "\n")

    # ── diagnostics: heartbeat showing WHERE capture is stuck ─────────────────────
    async def _status_loop(self) -> None:
        """Until ready, print every 15s what's captured so far so a stuck capture is obvious: x-session comes
        from the page's REST calls (appears once logged in); the WS login comes from the MQTT CONNECT frame
        (appears once you BROWSE a sport — that opens the odds socket). If 'WS login' never flips to YES even
        after browsing, the odds socket likely runs in a Web Worker that page.on('websocket') can't see (→ we
        switch to CDP frame capture)."""
        while True:
            try:
                await asyncio.sleep(15)
            except asyncio.CancelledError:
                break
            if self.ready:
                break
            await self._probe_storage()                  # worker-WS fallback: mine the suffix from storage
            print(f"[PINNACLE SESSION] waiting for capture — x-session: {'YES' if self._session else 'no'}, "
                  f"WS login: {'YES' if self._have_ws else 'no'}"
                  f"{'  (Arcadia WS not yet seen by Playwright)' if not self._seen_ws else ''}. "
                  "Make sure you're logged IN and have BROWSED to a sport.")

    async def _session_refresh_loop(self) -> None:
        """GUARANTEED keepalive vs the ~30-min idle logout: every PINNACLE_RELOGIN_MIN (default 20, safely under
        30) RELOAD the page. A reload re-runs the login via the saved profile → refreshes the session server-side
        and re-emits a fresh x-session, which the adapter picks up via _on_browser_creds (same recovery path as a
        sidecar restart). Reliable because it fires on a fixed schedule — unlike synthetic gestures, which don't
        reset Pinnacle's timer. Pauses organic activity across the reload so a gesture can't fight it."""
        while True:
            try:
                await asyncio.sleep(self._relogin_min * 60)
            except asyncio.CancelledError:
                break
            if self._page is None:
                continue
            if getattr(self, "_banking_hold", False):
                print("[PINNACLE SESSION] session refresh SKIPPED - operator banking window is open.")
                continue
            # A reload while the operator is mid-navigation throws away whatever they were looking at, and
            # unlike the camp hold there is nothing to bound it against: the session can simply be logged
            # back in afterwards, whereas an interrupted human is just interrupted.
            if getattr(self, "_manual_hold", False):
                print("[PINNACLE SESSION] session refresh SKIPPED - manual mode (operator is driving).")
                continue
            # A CAMP IS AN ARMED BETSLIP, AND reload() DESTROYS IT. Soft SPA navigation leaves the Quick
            # Bet portal mounted; a hard reload does not — measured 2026-08-16, a camp died at ~5.4min
            # against a 7min refresh cadence. BOUNDED, because this reload is what re-mints the
            # x-session the odds WS authenticates with: a camp may postpone it, never cancel it.
            if getattr(self, "_camp_hold", False):
                self._camp_skips = getattr(self, "_camp_skips", 0) + 1
                if self._camp_skips <= self._camp_max_skips:
                    print(f"[PINNACLE SESSION] session refresh DEFERRED — a betslip is armed "
                          f"({self._camp_skips}/{self._camp_max_skips} allowed). The x-session is not "
                          f"re-minted while deferred.")
                    continue
                print(f"[PINNACLE SESSION] session refresh FORCED after {self._camp_skips} deferrals — "
                      f"the armed betslip will be destroyed, but the x-session must be re-minted.")
            self._camp_skips = 0
            try:
                self.pause_activity()
                await self._page.reload(wait_until="domcontentloaded", timeout=45_000)
                self._last_main_refresh = time.time()
                print(f"[PINNACLE SESSION] session refresh — reloaded to re-mint (next in {self._relogin_min:g}m).")
            except Exception as ex:
                print(f"[PINNACLE SESSION] session refresh reload error: {type(ex).__name__}: {ex}")
            finally:
                self.resume_activity()
            if self._auto_login:
                await self._ensure_logged_in()   # a hard-expired session shows the login form right after reload

    async def force_remint(self, force_login: bool = False) -> None:
        """On-demand re-mint: reload the page so the saved profile issues a FRESH x-session, which the adapter
        picks up (and pushes into the paho WS password). Triggered when the odds WS gets auth-rejected on a
        session rotation, OR when the adapter detects a mass authed-REST guest-redirect (a real logout). Best-
        effort; never raises. `force_login=True` (the mass-logout path) tells `_ensure_logged_in` to submit the
        login form even if `_last_capture` looks fresh — because a logged-out page keeps SENDING its dead
        x-session, so 'recent capture' is not proof of a live login there."""
        if self._page is None:
            return
        try:
            self.pause_activity()
            await self._page.reload(wait_until="domcontentloaded", timeout=45_000)
            self._last_main_refresh = time.time()
            print("[PINNACLE SESSION] force re-mint — reloaded to refresh the x-session (WS auth-reject recovery).")
        except Exception as ex:
            print(f"[PINNACLE SESSION] force re-mint error: {type(ex).__name__}: {ex}")
        finally:
            self.resume_activity()
        if self._auto_login:
            # Let the reload's cookie re-mint emit authed traffic (refreshing _last_capture) BEFORE checking the
            # form, so _ensure_logged_in's healthy-session guard can skip a redundant login when the reload already
            # restored the session, and only submit on a genuine hard logout. Avoids the re-mint→re-login churn.
            await asyncio.sleep(4)
            await self._ensure_logged_in(force=force_login)

    # ── unattended re-login (submit the profile-autofilled form) ───────────────────
    def _profile_has_saved_login(self) -> bool:
        """True if the persistent Chrome profile has a saved-credentials store on disk — evidence that an
        autofilled login form is REAL (not first-time setup). Chrome writes 'Login Data' once a password is
        saved. This is what lets us submit even when Chrome hides the autofilled value from JS until a gesture."""
        try:
            base = Path(self._user_data)
            for p in (base / "Default" / "Login Data", base / "Default" / "Login Data For Account"):
                if p.exists() and p.stat().st_size > 0:
                    return True
        except Exception:
            pass
        return False

    async def _ensure_logged_in(self, force: bool = False) -> bool:
        """If a visible password field is present and we have EVIDENCE the profile holds saved credentials, the
        page is on a login form (session dropped) → submit it (click to commit autofill, then Enter; button-click
        fallback if it lingers). Evidence = a readable non-empty value OR a session already captured this run OR
        the profile's saved-login store on disk. Why not require a readable value: Chrome commonly hides the
        AUTOFILLED value from `.value` until a user gesture, so the field looks filled but reads empty. With NO
        evidence (empty + never logged in + no saved-login store) this is genuine first-time MANUAL setup → no-op,
        so we never submit blanks. No credentials are typed — the profile fills them. Never raises."""
        if self._page is None:
            return False
        if self._login_locked_out:
            return False                                   # breaker tripped; see _score_login_attempt
        try:
            pw = self._page.locator("input[type=password]:visible").first
            if await pw.count() == 0:
                # NO PASSWORD FIELD IS NOT THE SAME AS LOGGED IN. This used to return immediately, which made
                # the whole watcher a no-op in the most common real logout: Pinnacle drops you back to the
                # BOARD with a 'LOG IN' button in the header and NO form on the page. The form only exists
                # after that button is clicked. So the watcher ticked every 8s against a logged-out session
                # and did nothing — found 2026-08-18 after the bot sat logged out indefinitely.
                if not await self._open_login_form():
                    return False
                pw = self._page.locator("input[type=password]:visible").first
                if await pw.count() == 0:
                    return False
            val = await pw.input_value()
        except Exception:
            return False
        has_creds = bool(val) or self._ever_logged_in or self._profile_has_saved_login()
        if not has_creds:
            return False                                   # empty form + no saved creds → first-time setup; leave it
        # ALREADY LOGGED IN? If we captured authed traffic recently, a visible login form is a stray/autofilled
        # widget, NOT a logout — submitting it would rotate the live session. Only re-login once authed traffic
        # has gone silent (a real logout). This is what stops the post-capture re-login churn. EXCEPTION: `force`
        # (the mass-guest-redirect path) bypasses this — there we have POSITIVE evidence of a logout, and this very
        # guard is what masked it (a logged-out page keeps sending its dead x-session, so _last_capture stays fresh).
        # `_last_capture` alone is NOT proof of a live login: a logged-OUT SPA keeps sending its dead
        # x-session, so the timestamp never goes stale and this guard blocks the re-login forever (flagged
        # 2026-07-24 as masking a real logout; hit for real 2026-08-06 — whether auto-login worked came down
        # to a RACE between "captured x-session" and the watcher's first tick). A genuinely live login also
        # produces a **WS login** (`_have_ws`) once the page opens its odds socket, and a logged-out page
        # never does. So past a startup grace, require BOTH. Inside the grace, keep trusting the capture alone
        # so we don't submit over a healthy session that simply hasn't opened its socket yet (the 2026-07-14
        # churn bug). `force` still bypasses everything.
        session_looks_live = (self._logged_session and self._last_capture
                              and (time.time() - self._last_capture) < self._login_healthy_grace)
        have_ws = bool(getattr(self, "_have_ws", False))
        opened_at = float(getattr(self, "_opened_at", 0.0) or 0.0)
        ws_grace = float(getattr(self, "_ws_login_grace", 45.0))
        if session_looks_live and not have_ws and opened_at \
                and (time.time() - opened_at) > ws_grace:
            session_looks_live = False       # authed traffic but no WS login well after open ⇒ really logged out
            print(f"[PINNACLE SESSION] authed traffic is flowing but NO WS login "
                  f"{int(time.time() - opened_at)}s after open, and a login form is visible - "
                  "treating this as a REAL logout (a logged-out page keeps sending its dead x-session).")
        if (not force) and session_looks_live:
            return False
        # LET THE PAGE SETTLE FIRST. Both a reload and a fresh browser open leave the form mounting for a
        # moment; submitting into that races the render and Chrome's autofill. Cheap to wait — the watcher
        # re-checks every few seconds anyway, so this delays recovery by one tick at most.
        now_ = time.time()
        for stamp, what in ((getattr(self, "_last_main_refresh", 0.0), "page re-mint"),
                            (float(getattr(self, "_opened_at", 0.0) or 0.0), "browser open")):
            if stamp and now_ - stamp < self._login_settle_sec:
                print(f"[PINNACLE SESSION] login form is up but the {what} was "
                      f"{now_ - stamp:.1f}s ago - letting it settle before submitting.", flush=True)
                return False
        if time.time() - self._last_login_submit < self._login_submit_cooldown:
            return False                                   # don't hammer the login form
        self._last_login_submit = time.time()
        self._login_submit_at = self._last_login_submit     # scored by _score_login_attempt on the next ticks
        print(f"[PINNACLE SESSION] login form detected — submitting saved credentials for unattended re-login "
              f"(attempt {self._login_fail_streak + 1}/{self._login_max_fails}, "
              f"value_readable={bool(val)}).")
        self.pause_activity()                              # don't let an organic gesture fight the submit
        try:
            # HUMAN BEAT: a real user reads the page and pauses before submitting; a sub-second auto-submit at a
            # fixed offset is the robotic tell. Randomize the think-time so it's neither instant nor a constant.
            await asyncio.sleep(random.uniform(1.4, 4.2))
            await self._human_approach(pw)                 # drift the cursor to the field first (real mouse path)
            try:
                await pw.click()                           # focus + COMMIT Chrome's autofill (value often hidden until a gesture)
            except Exception:
                pass
            await asyncio.sleep(random.uniform(0.3, 0.7))

            # DID THE AUTOFILL ACTUALLY LAND? The click is meant to commit Chrome's hidden autofill, so after it
            # the value should be readable. When it is not, the field is genuinely EMPTY — the saved-login store
            # exists (that is why we got here) but Chrome is not populating it any more: a profile change, a
            # Chrome update, or a form change on the venue's side.
            #
            # The old code pressed Enter regardless. On an empty field that submits nothing, so the caret just
            # sits in the password box and the watcher retries every 30s, forever, silently. That is exactly the
            # reported symptom, and it is indistinguishable from "the bot is doing nothing".
            #
            # So: type the credentials ourselves if we have them. PINNACLE_USERNAME / PINNACLE_PASSWORD were
            # already in the environment and simply never read here.
            if not await self._typed_credentials_if_blank(pw):
                return True                                # nothing to submit; the reason was logged

            await pw.press("Enter")                        # submit (common human flow for a filled login)
            await asyncio.sleep(2.5)
            if await self._page.locator("input[type=password]:visible").count() > 0:
                await self._click_login_button()           # Enter didn't submit → click the button (with approach)
        except Exception as ex:
            print(f"[PINNACLE SESSION] auto-login submit error: {type(ex).__name__}: {ex}")
        finally:
            self.resume_activity()
        return True

    # ANCHORED to the whole trimmed label on purpose. A substring match also hits things like
    # "Login history" or "Sign in help" that exist on a LOGGED-IN page, which would make the
    # logged-out signal fire against a healthy session and click a stray control.
    _LOGIN_RX = re.compile(r"^\s*(log\s*in|sign\s*in)\s*$", re.I)

    def _read_login_stamp(self) -> float | None:
        try:
            d = json.loads(self._login_stamp_path.read_text(encoding="utf-8"))
            at = float(d.get("at") or 0)
            return at or None
        except Exception:
            return None

    def _write_login_stamp(self, at: float | None) -> None:
        try:
            if at is None:
                self._login_stamp_path.unlink(missing_ok=True)
            else:
                self._login_stamp_path.write_text(json.dumps({"at": at}), encoding="utf-8")
        except Exception:
            pass

    def login_age_sec(self) -> float | None:
        """Seconds since the account logged in, across sidecar restarts. None = unknown."""
        return None if self._login_at is None else max(0.0, time.time() - self._login_at)

    def login_age_str(self) -> str:
        """For the heartbeat: 'login 84m' / 'login age unknown (profile reused)'."""
        a = self.login_age_sec()
        return "login age unknown (profile reused)" if a is None else f"login {a / 60.0:.0f}m"

    def should_relogin(self) -> bool:
        """Is the login old enough that we should re-auth BEFORE the venue kicks us? Off unless
        PINNACLE_MAX_LOGIN_MIN is set, because guessing a cap we have not measured would throw away
        perfectly good sessions."""
        if self._max_login_min <= 0:
            return False
        a = self.login_age_sec()
        return a is not None and a >= self._max_login_min * 60.0

    def note_login_established(self) -> None:
        """A submit WE made has produced a live session — the login clock starts now.

        Only called from the capture path, because a capture is the only proof a submit worked. A reused
        profile that was already logged in does NOT come through here, and correctly leaves the age unknown
        rather than claiming a fresh login.
        """
        self._login_at = time.time()
        self._write_login_stamp(self._login_at)
        print("[PINNACLE SESSION] login clock STARTED — this is a fresh login, so the account's maximum "
              "session age is measured from now.", flush=True)

    def note_login_age_on_logout(self, reason: str) -> None:
        """Record how old the login was when it died. THIS is how the cap gets measured.

        The venue never states the limit, so the only way to learn it is to watch what age the logouts
        happen at. Printed prominently and left in the stamp file's history so a pattern can be seen across
        days rather than guessed at once.
        """
        age = self.login_age_sec()
        if age is None:
            print("[PINNACLE SESSION] logged out, but the login age is UNKNOWN (profile was already logged "
                  "in when this sidecar started) — this tells us nothing about the cap. It will once a "
                  "login made BY the bot is the one that dies.", flush=True)
            return
        print(f"[PINNACLE SESSION] *** LOGIN DIED AT {age / 60.0:.1f} MINUTES OLD *** ({reason}) — if this "
              f"repeats near the same age it is the venue's maximum-session cap, and "
              f"PINNACLE_MAX_LOGIN_MIN should be set a little below it to re-login first.", flush=True)
        try:
            hist = []
            try:
                hist = json.loads(self._login_stamp_path.with_suffix(".history.json")
                                  .read_text(encoding="utf-8"))
            except Exception:
                hist = []
            hist.append({"died_at": round(time.time()), "age_min": round(age / 60.0, 1), "reason": reason})
            self._login_stamp_path.with_suffix(".history.json").write_text(
                json.dumps(hist[-40:], indent=1), encoding="utf-8")
        except Exception:
            pass
        self._login_at = None
        self._write_login_stamp(None)

    async def _score_login_attempt(self) -> None:
        """Decide whether the last submit worked, and trip the breaker after enough that did not.

        A submit is scored only once, `_login_verify_sec` after it was made — long enough for the page to
        navigate, the SPA to re-auth and the x-session to be captured. SUCCESS IS A CAPTURE, nothing weaker:
        the form disappearing proves only that the form disappeared, and a logged-out board has no form on it
        either, which is the whole reason this bug existed.

        On the fifth consecutive failure it stops trying and closes the browser. Repeated credential submits
        are the one retry loop here with an external cost — that pattern is what triggers lockouts and
        captchas, and this account has already seen one. Going dark is recoverable by a human; a locked
        account is not."""
        at = getattr(self, "_login_submit_at", 0.0)
        if not at or self._login_locked_out:
            return
        if time.time() - at < self._login_verify_sec:
            return                                         # too early to judge
        self._login_submit_at = 0.0
        if self._logged_session:
            return                                         # the capture path already reset the streak
        self._login_fail_streak += 1
        n, cap = self._login_fail_streak, self._login_max_fails
        if n < cap:
            print(f"[PINNACLE SESSION] login attempt {n}/{cap} did not produce a session after "
                  f"{self._login_verify_sec:.0f}s - will retry.", flush=True)
            return
        self._login_locked_out = True
        print(f"[PINNACLE SESSION] *** LOGIN BREAKER TRIPPED after {n} consecutive failures *** No further "
              f"login attempts will be made, and the browser is closing. Repeated credential submits are what "
              f"trigger lockouts and captchas, so this stops rather than keeps knocking. Check the account by "
              f"hand (password change? verification hold? captcha?), then restart the sidecar. "
              f"PINNACLE_LOGIN_MAX_FAILS raises the limit.", flush=True)
        try:
            await self.stop()
        except Exception as ex:
            print(f"[PINNACLE SESSION] breaker: browser close failed: {type(ex).__name__}: {ex}", flush=True)

    def _note_logged_out_age(self, reason: str) -> None:
        """Hook so every logout path measures the login age exactly once."""
        if getattr(self, "_logout_age_noted", False):
            return
        self._logout_age_noted = True
        self.note_login_age_on_logout(reason)

    def note_logged_out(self, reason: str) -> None:
        """Called by the adapter when authed REST proves the session is gone (guest-redirect burst / auth
        streak). Idempotent and safe to call repeatedly; cleared on the next successful capture."""
        if self._known_logged_out:
            return
        self._known_logged_out = reason or "REST says logged out"
        print(f"[PINNACLE SESSION] logout reported by the REST side ({self._known_logged_out}) - the login "
              f"watcher will recover it.", flush=True)
        # MEASURE THE CAP. Every logout is a data point about how long a login is allowed to live, and it is
        # the only way the limit can be learned — the venue does not publish it.
        self._note_logged_out_age(self._known_logged_out)

    async def _looks_logged_out(self) -> bool:
        """A visible LOG IN control on the board means the account is OUT. A logged-in page shows the balance
        and account menu there instead, never a login button — so this is the cheapest reliable signal, and
        the only one available when no form is rendered."""
        try:
            b = self._page.locator("button:visible, a:visible").filter(has_text=self._LOGIN_RX)
            return bool(await b.count())
        except Exception:
            return False

    async def _open_login_form(self) -> bool:
        """Click the header LOG IN so the password form exists to submit. Returns True if a form appeared.

        Guarded the same way the submit is: only when the session does NOT look live. Clicking LOG IN on a
        healthy session would at best be a stray dialog and at worst rotate a working login, so 'no form on
        the page' is treated as logged-out ONLY when the other evidence agrees."""
        dom_says_out = await self._looks_logged_out()
        if not dom_says_out and not self._known_logged_out:
            return False
        now = time.time()
        # THE BOARD HAS NO LOGIN CONTROL. Measured 2026-08-18 on /tennis/matchups/ while genuinely logged
        # out: the page renders odds buttons and one icon button, and nothing matching a login affordance —
        # so the DOM signal is simply unavailable there. When the REST side has proved the session is dead,
        # navigate to the login URL (the homepage DOES render 'LOG IN' and the form) and look again. Without
        # this the watcher can be correct, awake, and permanently unable to see the thing it is watching for.
        if not dom_says_out:
            # DON'T RACE THE OTHER RECOVERY. A guest-redirect burst also triggers `force_remint` (reload +
            # re-login), and the reload re-mints the session on its own — observed 2026-08-18 recovering ~2s
            # after this path had already declared failure. Navigating to the login URL on top of an
            # in-flight reload competes with it for the page and turns a working recovery into a race.
            #
            # So stand down while EITHER is fresh: a reload (`_last_main_refresh`) or a submit
            # (`_last_login_submit`). If both go quiet and we are still out, this fires on the next tick.
            for stamp, what in ((getattr(self, "_last_main_refresh", 0.0), "a page re-mint"),
                                (self._last_login_submit, "a login submit")):
                if stamp and now - stamp < self._login_submit_cooldown:
                    return False
            if now - getattr(self, "_last_login_open", 0.0) < self._login_submit_cooldown:
                return False
            self._last_login_open = now
            try:
                # EXACT path compare, not a substring. The login URL is the site ROOT
                # (…/en/), which is a prefix of literally every other page — so `in` reports
                # "already there" from any page on the site and the navigation never happens.
                cur = (self._page.url or "").split("?")[0].split("#")[0].rstrip("/")
                if cur != self._login_url.split("?")[0].split("#")[0].rstrip("/"):
                    print(f"[PINNACLE SESSION] logged out ({self._known_logged_out}) but this page has no "
                          f"login control - going to {self._login_url} to sign back in.", flush=True)
                    await self._page.goto(self._login_url, wait_until="domcontentloaded", timeout=45_000)
                    await asyncio.sleep(2.0)
                if await self._page.locator("input[type=password]:visible").count():
                    return True                 # the homepage renders the form inline
                dom_says_out = await self._looks_logged_out()
            except Exception as ex:
                print(f"[PINNACLE SESSION] could not reach the login page: {type(ex).__name__}: {ex}",
                      flush=True)
                return False
            if not dom_says_out:
                # NOT an error, and deliberately not phrased as one. The other recovery paths (force re-mint,
                # the profile's own autofill on reload) frequently land within seconds of this, so telling the
                # operator to intervene here would be wrong far more often than right. The BREAKER is what
                # escalates a genuine failure, after five scored attempts.
                print("[PINNACLE SESSION] no login control on the login page yet - leaving it to the re-mint "
                      "path and re-checking next tick.", flush=True)
                return False
        # NO TOKEN-FRESHNESS VETO HERE, deliberately. The obvious guard - "authed traffic and a WS login
        # say we're in, so ignore the button" - cannot work: `_have_ws` is set on the first successful
        # login and NEVER cleared, and a logged-out SPA keeps replaying its dead x-session so
        # `_last_capture` never goes stale either. Both would still read healthy minutes after a real
        # logout, so the veto would block every recovery for the life of the process. (Checked
        # 2026-08-18: _have_ws is written in exactly one place and only ever set True.)
        #
        # A rendered LOG IN control is far stronger evidence than either token: a logged-in board shows
        # the balance and account menu in that slot and never a login button. So the DOM decides, and the
        # anchored regex above is what keeps that signal honest.
        self._have_ws = False               # demonstrably out; stop claiming a live WS login
        if now - getattr(self, "_last_login_open", 0.0) < self._login_submit_cooldown:
            return False                    # don't hammer the header button
        self._last_login_open = now
        try:
            b = self._page.locator("button:visible, a:visible").filter(has_text=self._LOGIN_RX).first
            print("[PINNACLE SESSION] logged OUT (a LOG IN control is on the board and no form is open) — "
                  "opening the login form.", flush=True)
            await self._human_approach(b)
            await asyncio.sleep(random.uniform(0.4, 1.1))
            await b.click(timeout=5000)
            for _ in range(12):             # the form is a client-side panel; give it a moment to mount
                await asyncio.sleep(0.5)
                if await self._page.locator("input[type=password]:visible").count():
                    print("[PINNACLE SESSION] login form opened.", flush=True)
                    return True
            print("[PINNACLE SESSION] clicked LOG IN but no password field appeared within 6s.", flush=True)
        except Exception as ex:
            print(f"[PINNACLE SESSION] could not open the login form: {type(ex).__name__}: {ex}", flush=True)
        return False

    async def _typed_credentials_if_blank(self, pw) -> bool:
        """Type PINNACLE_USERNAME / PINNACLE_PASSWORD when autofill left the form empty.

        Returns True if there is now something worth submitting, False if not (and says why).

        THE PROFILE IS STILL THE PRIMARY PATH. This runs only after the click failed to produce a readable
        value, so a healthy autofill is untouched and no credentials are typed in the normal case. But
        "autofill will always work" is an assumption about someone else's browser, and when it breaks the old
        behaviour was an invisible no-op on a 30s loop.

        Typed with a per-key delay rather than `fill()`: `fill()` sets the value in one step and dispatches a
        single input event, which a login form can treat differently from a person typing — and this is the
        one form on the site where looking wrong has consequences beyond a failed click.
        """
        try:
            val = await pw.input_value()
        except Exception:
            val = ""
        if val:
            return True                                    # autofill worked (or the click committed it)

        user = os.environ.get("PINNACLE_USERNAME", "").strip()
        pwd = os.environ.get("PINNACLE_PASSWORD", "")
        if not user or not pwd:
            print("[PINNACLE SESSION] *** LOGIN FORM IS EMPTY AND AUTOFILL DID NOT POPULATE IT *** — the "
                  "profile has a saved-login store but Chrome is not filling it. Nothing was submitted "
                  "(pressing Enter on a blank form does nothing, which is why this looked like the bot "
                  "hanging). Set PINNACLE_USERNAME / PINNACLE_PASSWORD so it can type them, or log in by "
                  "hand once in the managed window to re-save the credentials.", flush=True)
            return False

        try:
            # The username field: whatever visible non-password text/email input sits on the form.
            for sel in ('input[type=email]:visible', 'input[name*="ser" i]:visible',
                        'input[type=text]:visible'):
                u = self._page.locator(sel).first
                if await u.count():
                    if not (await u.input_value() or "").strip():
                        await self._human_approach(u)
                        await u.click()
                        await u.type(user, delay=random.uniform(55, 130))
                        await asyncio.sleep(random.uniform(0.25, 0.6))
                    break
            await self._human_approach(pw)
            await pw.click()
            await pw.type(pwd, delay=random.uniform(55, 130))
            await asyncio.sleep(random.uniform(0.3, 0.8))
            print("[PINNACLE SESSION] autofill was empty — typed the saved credentials from the environment "
                  "instead.", flush=True)
            return True
        except Exception as ex:
            print(f"[PINNACLE SESSION] could not type credentials: {type(ex).__name__}: {ex}", flush=True)
            return False

    async def _human_approach(self, locator) -> None:
        """Best-effort: move the mouse to a locator's centre along organic's HUMAN path before acting, so a
        submit/click is preceded by a real cursor approach (hover), not a teleport. No-op if organic or the
        element box is unavailable. Never raises."""
        try:
            box = await locator.bounding_box()
            if box and self._organic is not None and hasattr(self._organic, "_human_move"):
                await self._organic._human_move(box["x"] + box["width"] * 0.5,
                                                box["y"] + box["height"] * 0.5, clamp=False)
        except Exception:
            pass

    async def _click_login_button(self) -> None:
        """Fallback submit: click a visible Log In / Sign In button, cursor-approaching it first (human path).

        FAILS LOUDLY. Every candidate used to be tried inside a bare `except: continue`, and the function
        returned in silence when all three missed — so a renamed or restructured submit control looked
        exactly like "the bot is sitting there doing nothing", which is precisely how it was reported. If
        none match, dump what IS on the page so the next selector can be written from evidence."""
        # ORDERED BY WHAT PINNACLE ACTUALLY RENDERS. Captured 2026-08-18 from /debug/login:
        #     <button type='button'> 'LOG IN'  class='button-l9TRHt6rdY ellipsis small-CmHfQVtx1F'
        # `type='button'` — so `button[type=submit]` and `input[type=submit]` can NEVER match here, and there
        # is likely no native form submit behind Enter either. That makes the TEXT match the only real path,
        # not a fallback, so it is tried first and by two independent routes: a direct element text match, and
        # the accessible-name role match (which depends on name computation and can quietly stop matching).
        _rx = re.compile(r"log\s*in|sign\s*in", re.I)
        tried = []
        for name, loc in (("button:has-text", self._page.locator("button:visible").filter(has_text=_rx)),
                          ("role=button log/sign in",
                           self._page.get_by_role("button", name=_rx)),
                          ("button[type=submit]", self._page.locator('button[type="submit"]:visible')),
                          ("input[type=submit]", self._page.locator('input[type="submit"]:visible'))):
            try:
                b = loc.first
                n = await b.count()
                if n == 0:
                    tried.append(f"{name}=0")
                    continue
                await self._human_approach(b)
                await asyncio.sleep(random.uniform(0.1, 0.35))
                await b.click(timeout=3000)
                print(f"[PINNACLE SESSION] auto-login: clicked submit button via {name} (cursor-approached).")
                return
            except Exception as ex:
                tried.append(f"{name}!{type(ex).__name__}")
        print(f"[PINNACLE SESSION] *** AUTO-LOGIN COULD NOT SUBMIT *** Enter did not submit and no submit "
              f"control matched ({', '.join(tried)}). The form stays filled and nothing happens — this is the "
              f"'caret blinking, bot idle' state. Buttons actually on the page:", flush=True)
        try:
            for b in await self._login_buttons_on_page():
                print(f"      {b}", flush=True)
        except Exception:
            pass

    async def _login_buttons_on_page(self) -> list:
        """Every clickable-looking control on the current page, for diagnosing a missed submit selector."""
        out = []
        try:
            # WIDER THAN IT LOOKS IT NEEDS TO BE, on purpose. The narrow version (button / [role=button] /
            # a[href*=login]) reported "no login control" on a page that visibly had one — because a login
            # affordance here can be a plain <a> or a <div>, and a diagnostic that misses the thing being
            # diagnosed is worse than none. Anything clickable-looking is listed; the reader filters.
            els = self._page.locator(
                "button:visible, input[type=submit]:visible, [role=button]:visible, "
                "a:visible, [class*='login' i]:visible, [class*='signin' i]:visible, "
                "[data-test-id*='login' i]:visible")
            for i in range(min(await els.count(), 40)):
                e = els.nth(i)
                try:
                    txt = ((await e.inner_text()) or "").strip().replace("\n", " ")[:44]
                except Exception:
                    txt = ""
                try:
                    tag = await e.evaluate("el => el.tagName.toLowerCase()")
                    typ = await e.get_attribute("type") or ""
                    cls = ((await e.get_attribute("class")) or "")[:44]
                except Exception:
                    tag = typ = cls = "?"
                if len(txt) > 30 and not any(k in cls.lower() for k in ("login", "signin")):
                    continue                     # odds cells and content blocks, not controls
                out.append(f"<{tag} type={typ!r}> {txt!r}  class={cls!r}")
        except Exception:
            pass
        return out or ["(none found)"]

    async def login_debug(self) -> dict:
        """Read-only snapshot of what the auto-login watcher sees RIGHT NOW, and which guard is stopping it.

        Written because the failure mode is invisible from the outside: the page looks like a filled login
        form, and every reason the watcher might decline (session looks live, submit cooldown, no saved
        credentials) returns quietly. This answers 'why is it not pressing' without another guessing round."""
        out: dict = {"ok": True}
        if self._page is None:
            return {"ok": False, "error": "no page"}
        try:
            out["url"] = (self._page.url or "")[:120]
            pw = self._page.locator("input[type=password]:visible").first
            out["password_field"] = bool(await pw.count())
            out["password_filled"] = bool((await pw.input_value()) or "") if out["password_field"] else None
        except Exception as ex:
            out["read_error"] = f"{type(ex).__name__}: {ex}"
        now = time.time()
        out["auto_login_enabled"] = bool(self._auto_login)
        out["login_fail_streak"] = self._login_fail_streak
        out["login_max_fails"] = self._login_max_fails
        out["login_locked_out"] = self._login_locked_out
        out["ever_logged_in"] = bool(self._ever_logged_in)
        out["profile_has_saved_login"] = self._profile_has_saved_login()
        out["have_ws_login"] = bool(getattr(self, "_have_ws", False))
        out["last_capture_age_sec"] = round(now - self._last_capture, 1) if self._last_capture else None
        out["secs_since_last_submit"] = round(now - self._last_login_submit, 1)
        out["submit_cooldown_sec"] = self._login_submit_cooldown
        out["cooldown_blocking"] = (now - self._last_login_submit) < self._login_submit_cooldown
        session_looks_live = bool(self._logged_session and self._last_capture
                                  and (now - self._last_capture) < self._login_healthy_grace)
        out["session_looks_live"] = session_looks_live
        out["would_skip_because_session_looks_live"] = session_looks_live and not out["cooldown_blocking"]
        try:
            out["looks_logged_out"] = await self._looks_logged_out()
        except Exception:
            out["looks_logged_out"] = None
        # The case that made the watcher a no-op: OUT, but with no form on the page to submit.
        out["logged_out_with_no_form"] = bool(out.get("looks_logged_out")) and not out.get("password_field")
        out["buttons"] = await self._login_buttons_on_page()
        return out

    def set_camp_hold(self, on: bool) -> None:
        """Ask the session-refresh loop to postpone its reload while a betslip is armed.

        A REQUEST, NOT A VETO. The reload re-mints the x-session that the odds WebSocket authenticates
        with, so a camp that could suppress it indefinitely would trade a betslip for the price feed.
        PINNACLE_CAMP_MAX_SKIPS (default 2) bounds it: at ~7 minutes a cycle that is ~14 minutes of
        postponement, comfortably past the 41s median gap between repeat arbs on the same match, and the
        forced refresh announces itself so a destroyed camp is never a mystery.
        """
        self._camp_hold = bool(on)
        if not on:
            self._camp_skips = 0

    def set_home_url(self, url: str | None) -> str:
        """Re-point the board-drift watchdog. Returns the URL now being enforced.

        `_home_url` is where the watchdog drags the primary page back to whenever it has been elsewhere
        for `_board_drift_sec`. That is exactly right pre-live — a stray click must not leave the board
        parked on some event page — and it is what pulled IN-PLAY mode off the live list on a timer,
        because the live list is not the trading-sport board.

        Re-pointing rather than disabling: the watchdog is the thing that recovers from a stray
        navigation, and in-play needs that recovery MORE than pre-live does (it has one tab and no rove
        to fall back on). Pass None to restore the derived default.
        """
        if url:
            self._home_url = url
        else:
            self._home_url = (os.environ.get("PINNACLE_HOME_URL") or "").strip() or self._derive_home_url()
        self._went_home = False        # let the one-shot re-run against the new target
        self._off_home_since = 0.0     # and do not count drift measured against the OLD home
        print(f"[PINNACLE SESSION] board home is now {self._home_url}", flush=True)
        return self._home_url

    @staticmethod
    def _active_sport_slug() -> str:
        """The one sport we trade, as Pinnacle spells it in a URL. Single source of truth: HARDVEN_SPORTS."""
        try:
            import sports as _sports_cfg
            keys = [s.key for s in _sports_cfg.enabled_sports()]
            return keys[0] if keys else ""
        except Exception:
            return ""

    def _derive_home_url(self) -> str:
        """Where the board SITS: the live in-play list of the sport we trade.

        HARDVEN_SPORTS decides, and a browse URL naming a DIFFERENT sport is treated as a stale leftover
        rather than a preference. That precedence is the fix for a specific failure: this method read
        `_browse_urls` first, so a tennis URL left in the env survived a switch to soccer, became the home
        page, and the drift watchdog then dragged the board back to tennis every few minutes while the feed,
        the catalog and the pairing had all moved. Nothing looked broken; the page and the data simply
        disagreed about the sport.

        Defaults to the LIVE board rather than the pre-match one, because that is where the operator wants
        to sit. Set PINNACLE_HOME_BOARD=prematch if slip placement needs the pre-match list — the live board
        lists live games ONLY, so a pre-match row is not on it and cannot be found by scanning.
        """
        slug = self._active_sport_slug()
        for u in self._browse_urls:
            m = re.search(r"/en/([a-z][a-z-]*)/", u or "")
            if m and slug and m.group(1) not in ("account", "login") and m.group(1) != slug:
                print(f"[PINNACLE SESSION] ignoring browse URL for '{m.group(1)}' — HARDVEN_SPORTS says we "
                      f"trade '{slug}'. Update PINNACLE_BROWSE_URLS or it will keep pulling the board off "
                      f"the sport we are actually watching.", flush=True)
                break                      # a wrong-sport list is not partially usable — derive instead
            if m and m.group(1) not in ("account", "login"):
                # A LIVE BROWSE URL STAYS LIVE. This rebuilt the sport's PRE-MATCH board from the sport
                # name alone, silently discarding the `/live/` the operator had asked for. The drift
                # watchdog then enforced that board, so /inplay/stop -> set_home_url(None) walked the page
                # off the live list at shutdown (observed 2026-08-19) and any drift check would have done
                # the same mid-run. In-play re-points home at the live list while it runs; deriving it
                # correctly means the default no longer fights that.
                if re.search(r"/(live|live-?betting)/?$", (u or "").split("?")[0]):
                    return u.split("?")[0]
                return f"https://www.pinnacle.bet/en/{m.group(1)}/matchups/"
        if slug:
            board = "matchups/" if (os.environ.get("PINNACLE_HOME_BOARD", "live").strip().lower()
                                    == "prematch") else "matchups/live/"
            return f"https://www.pinnacle.bet/en/{slug}/{board}"
        return self._login_url

    async def _go_home_once(self) -> None:
        """Put the board on the sport we trade, once per session, as soon as we're REALLY logged in.

        MUST NOT run while a login is in flight. First cut gated only on `_last_capture > 0` and navigated
        straight over a submitted login form, aborting the in-flight auth POST (2026-08-06: the operator saw
        the login spinner die and `WS login: no`). `_last_capture` is the wrong signal — this module's own
        history records that it is 'the last time an x-session was SENT, not the last time it WORKED', and a
        logged-OUT page keeps sending its dead session. So gate on the page state instead: no visible password
        field, and nothing submitted within the settle window."""
        if self._went_home or self._page is None or not self._home_url:
            return
        if time.time() - self._last_login_submit < self._home_settle_sec:
            return                                    # a submit is still settling — never navigate over it
        try:
            if await self._page.locator("input[type=password]:visible").count() > 0:
                return                                # still on a login form ⇒ not logged in yet
        except Exception:
            return
        try:
            cur = (self._page.url or "").split("#")[0].split("?")[0].rstrip("/").lower()
            if cur == self._home_url.split("#")[0].split("?")[0].rstrip("/").lower():
                self._went_home = True
                return
            self.pause_activity()
            # NO BOARD HOP. An earlier version routed sport-board -> live so the first navigation would
            # not be a deep link. The operator's call is that a bookmark straight to the live page is not
            # odd behaviour, and it is one fewer page change next to the login - which is the thing actually
            # suspected of dropping the session.
            await self._page.goto(self._home_url, wait_until="domcontentloaded", timeout=45_000)
            self._went_home = True
            self._last_main_refresh = time.time()
            print(f"[PINNACLE SESSION] board moved to the trading sport: {self._home_url}")
        except Exception as ex:
            print(f"[PINNACLE SESSION] could not open {self._home_url} ({type(ex).__name__}: {ex}) - "
                  "leaving the board where it is.")
            self._went_home = True          # don't retry every tick; the organic layer will browse anyway
        finally:
            self.resume_activity()

    async def _board_drift_check(self) -> None:
        """Bring the main board back to the trading sport if it has WANDERED — e.g. the operator clicked
        through to a match page or their account, or an organic gesture followed a link.

        The board is the session anchor AND the sport-topic subscription, so leaving it elsewhere silently
        drops board coverage for every league. Deliberately lazy: it must NOT fight a human who is actively
        looking at something, so it only acts once the page has been off-sport for PINNACLE_BOARD_DRIFT_SEC
        (default 180s) — long enough to browse, short enough that an accidental click self-heals. Never runs
        while a login is settling or a bet holds the page."""
        if self._page is None or not self._home_url:
            return
        # MANUAL MODE OWNS THE URL. Every page the operator deliberately opens is, to this watchdog,
        # indistinguishable from drift — so left running it waits out its 180s and then yanks them back to
        # the trading sport mid-task. That is the single most disruptive automation there is for someone
        # trying to use the site by hand, and the one people least expect, because it fires on a timer with
        # no other symptom.
        if getattr(self, "_manual_hold", False):
            return
        if time.time() - self._last_login_submit < self._home_settle_sec:
            return
        try:
            if self._organic is not None and not self._organic._gate.is_set():
                return                                   # paused = a bet is in flight; don't touch the page
        except Exception:
            pass
        try:
            cur = (self._page.url or "").split("#")[0].split("?")[0].rstrip("/").lower()
        except Exception:
            return
        home = self._home_url.split("#")[0].split("?")[0].rstrip("/").lower()
        if cur == home:
            self._off_home_since = 0.0
            return
        now = time.time()
        if not getattr(self, "_off_home_since", 0.0):
            self._off_home_since = now
            return
        if now - self._off_home_since < self._board_drift_sec:
            return
        self._off_home_since = 0.0
        try:
            self.pause_activity()
            await self._page.goto(self._home_url, wait_until="domcontentloaded", timeout=45_000)
            self._last_main_refresh = time.time()
            print(f"[PINNACLE SESSION] board had drifted to {cur[:70]} for "
                  f"{self._board_drift_sec:.0f}s - returned to the trading sport.")
        except Exception as ex:
            print(f"[PINNACLE SESSION] board drift return failed: {type(ex).__name__}: {ex}")
        finally:
            self.resume_activity()

    async def _login_watch_loop(self) -> None:
        """Unattended re-login watcher: periodically look for an autofilled login form and submit it. Covers
        initial open, reopen after a dark gap that logged us out, and a mid-session logout. The `:visible` +
        non-empty gate in _ensure_logged_in makes each tick a no-op unless a real, filled login form is up."""
        while True:
            try:
                await asyncio.sleep(self._login_check_sec)
            except asyncio.CancelledError:
                break
            try:
                await self._score_login_attempt()          # judge the PREVIOUS submit before making another
                await self._ensure_logged_in()
            except Exception:
                pass
            # Once authed traffic proves we're in — however that happened, auto-login OR a manual login —
            # move the board to the sport we actually trade. Hooked here rather than to the auto-login path
            # so it still works on the sessions the operator logs in by hand.
            try:
                if not self._went_home and self._last_capture > 0:
                    await self._go_home_once()
                elif self._went_home:
                    await self._board_drift_check()
            except Exception:
                pass

    # ── execution interlock (delegates to the organic loop) ───────────────────────
    def set_manual(self, on: bool) -> None:
        """OPERATOR IS DRIVING. Suppress the two things that move the page on their own timer.

        Distinct from `set_banking` because it holds one thing banking does not: the BOARD-DRIFT WATCHDOG.
        Banking opens the cashier in its own tab, so the watchdog dragging the main page home is harmless
        there. Manual mode is the opposite case — the operator is navigating the main page deliberately, and
        every URL they choose looks to the watchdog exactly like drift. Left on, it hauls them back to the
        trading sport ~180s into whatever they were doing, which is precisely the "disturbance" being
        removed."""
        self._manual_hold = bool(on)
        what = "ON - session reloads AND the board-drift watchdog are off" if on else "OFF"
        print(f"[PINNACLE SESSION] manual hold {what}.")

    def set_banking(self, on: bool) -> None:
        """Operator banking window: suppress the periodic re-login RELOAD. `pause_activity` isn't enough — the
        refresh loop pauses activity around itself and reloads anyway, which would wipe a part-filled deposit
        form out from under the operator. Cleared when the window ends."""
        self._banking_hold = bool(on)
        print(f"[PINNACLE SESSION] banking hold {'ON - session-refresh reloads suppressed' if on else 'OFF'}.")

    async def open_banking_tab(self, url: str):
        """Open `url` in a NEW tab in the bot's own profile and put it in front — the operator deposits on the
        exact account (and cookies) that place the bets. A separate tab, so nothing the operator does navigates
        the page holding the WS/session."""
        pg = await self.open_tab(url)
        if pg is not None:
            try:
                await pg.bring_to_front()
            except Exception:
                pass
        return pg

    def pause_activity(self) -> None:
        """Pause organic idle behaviour before placing a bet (so an in-flight scroll/move can't fight the bet
        click on the single page). Resume after. No-op until the organic loop is running."""
        if self._organic:
            self._organic.pause()

    def resume_activity(self) -> None:
        if self._organic:
            self._organic.resume()
