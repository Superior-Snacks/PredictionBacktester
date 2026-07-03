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
import os
import random
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
        self._ws_urls_seen: set = set()
        self._debug_storage = os.environ.get("PINNACLE_DEBUG_STORAGE") == "1"   # dump localStorage on capture
        self._logged_storage = False
        self._cdp = None
        self._cdp_ws_reqs: set = set()

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
        print("\n" + "=" * 78)
        print("[PINNACLE SESSION] LOG IN in the Pinnacle window that just opened.")
        print("  - If the saved profile already remembers you, capture is automatic.")
        print("  - Browse to ANY sport once so the page opens its odds WebSocket (that yields the WS login).")
        print("  The C# bot stays idle until /health reports session_ready=true. Keep this window OPEN.")
        print("=" * 78 + "\n")

    async def stop(self) -> None:
        for t in (self._activity_task, self._status_task, self._session_refresh_task):
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
        self._activity_task = self._status_task = self._session_refresh_task = None

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
        if self._have_ws:
            return
        if "arcadia.pinnacle.com" not in url:
            return                                       # only the Arcadia MQTT socket
        if not self._seen_ws:
            self._seen_ws = True
            print("[PINNACLE SESSION] Arcadia WS visible to Playwright — watching its frames for the MQTT CONNECT.")
        try:
            ws.on("framesent", self._on_ws_frame)
        except Exception:
            pass

    def _on_ws_frame(self, payload) -> None:
        if self._have_ws or not isinstance(payload, (bytes, bytearray)):  # MQTT is binary; text isn't CONNECT
            return
        self._handle_connect_bytes(bytes(payload), "page.on")

    def _handle_connect_bytes(self, buf: bytes, src: str) -> None:
        """Shared by the page.on AND CDP capture paths: parse an MQTT CONNECT → WS username (account id) +
        '|suffix'. Idempotent (first hit wins). The WS password is reconstructed as '{x-session}|{suffix}' on
        every rotation, so the suffix is all we need to keep from here."""
        if self._have_ws:
            return
        parsed = parse_mqtt_connect(buf)
        if not parsed:
            return
        user = parsed.get("username") or ""
        pw = parsed.get("password") or ""
        if not user or "|" not in pw:
            return
        self._ws_user = user
        self._ws_suffix = pw.rsplit("|", 1)[1]
        self._have_ws = True
        print(f"[PINNACLE SESSION] captured WS login via {src} (account {user[:3]}***, suffix '{self._ws_suffix}').")
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
        try:
            await self._cdp.send("Network.enable")
            await self._cdp.send("Target.setAutoAttach",
                                 {"autoAttach": True, "waitForDebuggerOnStart": False, "flatten": True})
            print("[PINNACLE SESSION] CDP Network capture armed (worker-aware) — 2nd path for the WS login.")
        except Exception as ex:
            print(f"[PINNACLE SESSION] CDP enable failed ({type(ex).__name__}: {ex}); page.on capture only.")

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
        # requestId, so a frame still gets a shot even if we never saw its 'created' event.
        if self._have_ws:
            return
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

    # ── execution interlock (delegates to the organic loop) ───────────────────────
    def pause_activity(self) -> None:
        """Pause organic idle behaviour before placing a bet (so an in-flight scroll/move can't fight the bet
        click on the single page). Resume after. No-op until the organic loop is running."""
        if self._organic:
            self._organic.pause()

    def resume_activity(self) -> None:
        if self._organic:
            self._organic.resume()
