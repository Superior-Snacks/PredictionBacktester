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
import os
import random
import time
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
        self._user_data = os.environ.get("PINNACLE_USER_DATA_DIR", ".pinnacle_profile")
        self._headless = os.environ.get("PINNACLE_HEADLESS") == "1"     # DEFAULT headful (you log in)
        self._channel = os.environ.get("PINNACLE_CHANNEL", "chrome")
        self._activity_sec = float(os.environ.get("PINNACLE_BROWSER_ACTIVITY_SEC", "200"))
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
        self._seen_req = False
        self._seen_ws = False
        self._logged_session = False
        self._ws_urls_seen: set = set()

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
        self._pw = await async_playwright().start()
        launch = dict(user_data_dir=self._user_data, headless=self._headless,
                      viewport={"width": 1400, "height": 900},
                      args=["--disable-blink-features=AutomationControlled"],
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
        try:
            await self._page.goto(self._login_url, wait_until="domcontentloaded", timeout=60_000)
        except Exception as ex:
            print(f"[PINNACLE SESSION] initial navigation slow/failed ({ex}); the window is open — browse manually.")
        self._activity_task = asyncio.create_task(self._activity_loop())
        self._status_task = asyncio.create_task(self._status_loop())
        print("\n" + "=" * 78)
        print("[PINNACLE SESSION] LOG IN in the Pinnacle window that just opened.")
        print("  - If the saved profile already remembers you, capture is automatic.")
        print("  - Browse to ANY sport once so the page opens its odds WebSocket (that yields the WS login).")
        print("  The C# bot stays idle until /health reports session_ready=true. Keep this window OPEN.")
        print("=" * 78 + "\n")

    async def stop(self) -> None:
        for t in (self._activity_task, self._status_task):
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
        if self._have_ws:
            return
        if not isinstance(payload, (bytes, bytearray)):  # MQTT is binary; text frames aren't CONNECT
            return
        parsed = parse_mqtt_connect(bytes(payload))
        if not parsed:
            return
        user = parsed.get("username") or ""
        pw = parsed.get("password") or ""
        if not user or "|" not in pw:
            return
        self._ws_user = user
        self._ws_suffix = pw.rsplit("|", 1)[1]           # stable per device/build → reused on every rotation
        self._have_ws = True
        print(f"[PINNACLE SESSION] captured WS login (account {user[:3]}***, suffix '{self._ws_suffix}').")
        self._emit()

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
            print(f"[PINNACLE SESSION] waiting for capture — x-session: {'YES' if self._session else 'no'}, "
                  f"WS login: {'YES' if self._have_ws else 'no'}"
                  f"{'  (Arcadia WS not yet seen by Playwright)' if not self._seen_ws else ''}. "
                  "Make sure you're logged IN and have BROWSED to a sport.")

    # ── keepalive: gentle human-like activity ─────────────────────────────────────
    async def _activity_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(self._activity_sec)
            except asyncio.CancelledError:
                break
            if self._page is None:
                continue
            try:
                # Tiny nudge so a UI-inactivity logout doesn't trigger. The open tab + its own heartbeats are
                # the real anchor; this is belt-and-suspenders, deliberately small and infrequent.
                await self._page.mouse.move(random.randint(4, 40), random.randint(4, 40))
            except Exception:
                pass
