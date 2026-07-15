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


class PinnacleBrowserSession:
    """Headed Playwright window that mints + holds the Pinnacle session and pushes captured creds to the
    adapter via the `on_creds` callback. `on_creds(creds: dict)` is called on every change with keys:
    session, device, api_key, ws_user, ws_pass, ready."""

    def __init__(self, on_creds: Callable[[dict], None]) -> None:
        self._on_creds = on_creds
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
        self._last_login_submit = 0.0
        self._login_task: Optional[asyncio.Task] = None
        # sport pages the organic loop occasionally browses to (real session). Default = the home page only
        # (always valid); override with PINNACLE_BROWSE_URLS once you've confirmed the sport-page URLs.
        self._browse_urls = [u.strip() for u in os.environ.get("PINNACLE_BROWSE_URLS", "").split(",") if u.strip()] \
            or [self._login_url]
        self._organic = None
        self._pw = None
        self._ctx = None
        self._page = None
        self._activity_task: Optional[asyncio.Task] = None
        # captured creds
        self._session = ""
        self._device = ""
        self._api_key = ""
        self._ws_user = ""
        self._ws_suffix = ""
        self._have_ws = False
        self._last_capture = 0.0
        self._ready_announced = False
        # diagnostics: surface WHERE capture is stuck (x-session vs the MQTT-CONNECT WS login)
        self._status_task: Optional[asyncio.Task] = None
        self._session_refresh_task: Optional[asyncio.Task] = None
        self._seen_req = False
        self._seen_ws = False
        self._logged_session = False
        self._ever_logged_in = False   # have we EVER captured a session with this profile? (evidence creds are saved)
        self._ws_urls_seen: set = set()
        self._debug_storage = os.environ.get("PINNACLE_DEBUG_STORAGE") == "1"   # dump localStorage on capture
        self._logged_storage = False
        self._cdp = None
        self._cdp_ws_reqs: set = set()
        # WINDOW-WS READ FEASIBILITY PROBE (PINNACLE_WS_READ_PROBE=1): go/no-go for reading odds off the page's
        # OWN WS (instead of the dedicated paho conn) — count received PUBLISH (odds) frames + their leagues.
        self._ws_read_probe = os.environ.get("PINNACLE_WS_READ_PROBE") == "1"
        self._probe_recv_total = 0       # all received CDP WS frames on the Arcadia socket
        self._probe_recv_publish = 0     # of those, MQTT PUBLISH (an odds update)
        self._probe_leagues: set = set() # distinct league ids seen in PUBLISH topics
        self._probe_topics: set = set()  # distinct topic shapes (capped)
        self._probe_odds_ok = False      # confirmed a PUBLISH payload parses as odds JSON
        self._probe_start = 0.0
        self._probe_task = None

    # ── readiness / status ───────────────────────────────────────────────────────
    @property
    def ready(self) -> bool:
        # READY needs the x-session (REST auth) AND the WS login (account id + suffix → MQTT password). The
        # WS pieces only appear once the page opens its odds WebSocket, which it does after you browse a sport.
        return bool(self._session and self._ws_user and self._ws_suffix)

    def status(self) -> dict:
        return {
            "ready": self.ready,
            "has_session": bool(self._session),
            "has_ws_creds": self._have_ws,
            "account": (self._ws_user[:3] + "***") if self._ws_user else "",
            "last_capture_age_sec": round(time.time() - self._last_capture, 1) if self._last_capture else None,
            "headless": self._headless,
        }

    # ── lifecycle ────────────────────────────────────────────────────────────────
    async def start(self) -> None:
        from playwright.async_api import async_playwright
        reused = Path(self._user_data).exists()
        print(f"[PINNACLE SESSION] {'reusing SAVED' if reused else 'creating NEW'} Chrome profile: {self._user_data}"
              + ("" if reused else " (log in once; it'll be remembered next run)"))
        self._pw = await async_playwright().start()
        launch = dict(user_data_dir=self._user_data, headless=self._headless,
                      viewport={"width": 1400, "height": 900},
                      args=["--disable-blink-features=AutomationControlled",
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
            await self._page.goto(self._login_url, wait_until="domcontentloaded", timeout=60_000)
        except Exception as ex:
            print(f"[PINNACLE SESSION] initial navigation slow/failed ({ex}); the window is open — browse manually.")
        # Human-like idle activity (replaces the old nudge). PINNACLE_ORGANIC=0 disables it — the session still
        # holds via the authed-REST keepalive, so this is a clean toggle for isolating the gestures in testing.
        if os.environ.get("PINNACLE_ORGANIC") != "0":
            from organic import OrganicActivity
            self._organic = OrganicActivity(self._page, browse_urls=self._browse_urls, max_gap=self._activity_sec)
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

    async def stop(self) -> None:
        for t in (self._activity_task, self._status_task, self._session_refresh_task, self._login_task):
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

    # ── capture: REST headers (x-session / device / api-key) ──────────────────────
    def _on_request(self, request) -> None:
        try:
            if "arcadia.pinnacle.com" not in (request.url or ""):
                return
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
                print("[PINNACLE SESSION] captured x-session (REST auth ready).")
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
        (Playwright's CDPSession can't send Network.enable to a child worker target — so for a confirmed worker
        WS the storage probe / a follow-up is the fallback; the worker-attach log makes that case obvious.)"""
        try:
            self._cdp = await self._ctx.new_cdp_session(self._page)
        except Exception as ex:
            print(f"[PINNACLE SESSION] CDP unavailable ({type(ex).__name__}: {ex}); page.on capture only.")
            return
        self._cdp.on("Network.webSocketCreated", self._on_cdp_ws_created)
        self._cdp.on("Network.webSocketFrameSent", self._on_cdp_ws_frame)
        self._cdp.on("Target.attachedToTarget", self._on_cdp_target)
        if self._ws_read_probe:
            self._cdp.on("Network.webSocketFrameReceived", self._on_cdp_ws_frame_recv)
        try:
            await self._cdp.send("Network.enable")
            await self._cdp.send("Target.setAutoAttach",
                                 {"autoAttach": True, "waitForDebuggerOnStart": False, "flatten": True})
            print("[PINNACLE SESSION] CDP Network capture armed (worker-aware) — 2nd path for the WS login.")
            if self._ws_read_probe:
                self._probe_start = time.time()
                self._probe_task = asyncio.create_task(self._ws_read_probe_loop())
                print("[WS-READ-PROBE] ON — counting received odds frames off the page's OWN WS (go/no-go for the "
                      "window-per-sport read). Summary every 15s. Browse a sport so its odds WS opens.")
        except Exception as ex:
            print(f"[PINNACLE SESSION] CDP enable failed ({type(ex).__name__}: {ex}); page.on capture only.")

    def _on_cdp_ws_frame_recv(self, params: dict) -> None:
        """PROBE: count SERVER->CLIENT frames on the Arcadia WS and how many are MQTT PUBLISH (odds), plus the
        distinct leagues in their topics. Counting only (fast, off-loop-safe); the loop below logs the verdict."""
        if params.get("requestId") not in self._cdp_ws_reqs:
            return                                        # only the Arcadia odds socket (skip localhost/devtools WS)
        resp = params.get("response") or {}
        data = resp.get("payloadData")
        if not data:
            return
        try:
            buf = base64.b64decode(data) if resp.get("opcode") == 2 else data.encode("utf-8", "replace")
        except Exception:
            return
        self._probe_recv_total += 1
        parsed = parse_mqtt_publish(buf)
        if not parsed:
            return                                        # CONNACK / SUBACK / PINGRESP etc. — not an odds update
        topic, payload = parsed
        self._probe_recv_publish += 1
        if len(self._probe_topics) < 40:
            self._probe_topics.add(topic[:48])
        m = re.search(r"/lg/(\d+)", topic) or re.search(r"(\d{3,})", topic)
        if m:
            self._probe_leagues.add(m.group(1))
        if not self._probe_odds_ok:                       # confirm ONCE that a PUBLISH payload is real odds JSON
            try:
                obj = json.loads(payload.decode("utf-8", "replace"))
                if isinstance(obj, dict) and ("op" in obj or "rec" in obj):
                    self._probe_odds_ok = True
                    print(f"[WS-READ-PROBE] CONFIRMED odds JSON in a received PUBLISH — topic='{topic[:60]}' "
                          f"keys={list(obj)[:6]}")
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
            el = time.time() - self._probe_start
            n, pub, lg = self._probe_recv_total, self._probe_recv_publish, len(self._probe_leagues)
            verdict = ("GREEN — odds flow off the page WS" if pub > 0 and self._probe_odds_ok
                       else "AMBER — WS frames but no odds PUBLISH yet (open a sport with live odds?)" if n > 0
                       else "RED — NO received frames captured (odds WS likely worker-hidden → plan in-page shim)")
            print(f"[WS-READ-PROBE] {el:.0f}s | recv={n} PUBLISH(odds)={pub} leagues={lg} "
                  f"odds_json={'yes' if self._probe_odds_ok else 'no'} | {verdict}"
                  + (f" | leagues: {sorted(self._probe_leagues)[:10]}" if lg else "")
                  + (f" | topics: {sorted(self._probe_topics)[:4]}" if self._probe_topics else ""))

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
            try:
                self.pause_activity()
                await self._page.reload(wait_until="domcontentloaded", timeout=45_000)
                print(f"[PINNACLE SESSION] session refresh — reloaded to re-mint (next in {self._relogin_min:g}m).")
            except Exception as ex:
                print(f"[PINNACLE SESSION] session refresh reload error: {type(ex).__name__}: {ex}")
            finally:
                self.resume_activity()
            if self._auto_login:
                await self._ensure_logged_in()   # a hard-expired session shows the login form right after reload

    async def force_remint(self) -> None:
        """On-demand re-mint: reload the page so the saved profile issues a FRESH x-session, which the adapter
        picks up (and pushes into the paho WS password). Triggered when the odds WS gets auth-rejected on a
        session rotation — the same action as the periodic keepalive, on demand. Best-effort; never raises."""
        if self._page is None:
            return
        try:
            self.pause_activity()
            await self._page.reload(wait_until="domcontentloaded", timeout=45_000)
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
            await self._ensure_logged_in()

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

    async def _ensure_logged_in(self) -> bool:
        """If a visible password field is present and we have EVIDENCE the profile holds saved credentials, the
        page is on a login form (session dropped) → submit it (click to commit autofill, then Enter; button-click
        fallback if it lingers). Evidence = a readable non-empty value OR a session already captured this run OR
        the profile's saved-login store on disk. Why not require a readable value: Chrome commonly hides the
        AUTOFILLED value from `.value` until a user gesture, so the field looks filled but reads empty. With NO
        evidence (empty + never logged in + no saved-login store) this is genuine first-time MANUAL setup → no-op,
        so we never submit blanks. No credentials are typed — the profile fills them. Never raises."""
        if self._page is None:
            return False
        try:
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
        # has gone silent (a real logout). This is what stops the post-capture re-login churn.
        if self._logged_session and self._last_capture and (time.time() - self._last_capture) < self._login_healthy_grace:
            return False
        if time.time() - self._last_login_submit < self._login_submit_cooldown:
            return False                                   # don't hammer the login form
        self._last_login_submit = time.time()
        print(f"[PINNACLE SESSION] login form detected — submitting saved credentials for unattended re-login "
              f"(value_readable={bool(val)}).")
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
            await pw.press("Enter")                        # submit (common human flow for a filled login)
            await asyncio.sleep(2.5)
            if await self._page.locator("input[type=password]:visible").count() > 0:
                await self._click_login_button()           # Enter didn't submit → click the button (with approach)
        except Exception as ex:
            print(f"[PINNACLE SESSION] auto-login submit error: {type(ex).__name__}: {ex}")
        finally:
            self.resume_activity()
        return True

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
        """Fallback submit: click a visible Log In / Sign In button, cursor-approaching it first (human path)."""
        for loc in (self._page.get_by_role("button", name=re.compile(r"log\s*in|sign\s*in", re.I)),
                    self._page.locator('button[type="submit"]:visible'),
                    self._page.locator('input[type="submit"]:visible')):
            try:
                b = loc.first
                if await b.count() == 0:
                    continue
                await self._human_approach(b)
                await asyncio.sleep(random.uniform(0.1, 0.35))
                await b.click(timeout=3000)
                print("[PINNACLE SESSION] auto-login: clicked submit button (cursor-approached).")
                return
            except Exception:
                continue

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
                await self._ensure_logged_in()
            except Exception:
                pass

    # ── execution interlock (delegates to the organic loop) ───────────────────────
    def pause_activity(self) -> None:
        """Pause organic idle behaviour before placing a bet (so an in-flight scroll/move can't fight the bet
        click on the single page). Resume after. No-op until the organic loop is running."""
        if self._organic:
            self._organic.pause()

    def resume_activity(self) -> None:
        if self._organic:
            self._organic.resume()
