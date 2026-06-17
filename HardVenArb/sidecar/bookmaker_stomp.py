"""
bookmaker_stomp.py — STOMP-over-WebSocket odds client for bookmaker.eu (RabbitMQ Web-STOMP).

bookmaker.eu streams live odds over a STOMP feed (fixed feed creds login=rtweb/passcode=rtweb,
host=WebRT). This is the ODDS source for the HardVen sidecar — a direct WebSocket, no browser needed
to read prices. (Playwright in bookmaker_adapter is only for account login + bet placement, and to
obtain the per-session dynamic queue string + WSS URL, which are parsed from the site's init XHR.)

You supply, per session (from devtools / the init response):
  - ws_url : the wss:// endpoint
  - queue  : the dynamic session queue string (e.g. "_uybxhytg541…") used in x-queue-name + destination

Smoke-test the live feed:
  BOOKMAKER_WSS_URL=wss://… BOOKMAKER_STOMP_QUEUE=_xxx python bookmaker_stomp.py

Frame mechanics (per the bookmaker.eu spec):
  • STOMP frames are COMMAND\\n header:val\\n … \\n\\n body, terminated by a NULL byte (\\x00).
  • Handshake: send CONNECT → wait for CONNECTED → send SUBSCRIBE.
  • Heart-beat 20000,20000: send a lone newline ~every 20 s when idle.
  • Incoming MESSAGE: headers, then \\n\\n, then a JSON body with a trailing \\x00 to strip.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import os
from typing import Callable, Optional

import websockets

STOMP_LOGIN    = "rtweb"
STOMP_PASSCODE = "rtweb"
STOMP_HOST     = "WebRT"
NULL = "\x00"

# bookmaker.eu's RealTimeHandler.ashx serves a normal HTTP 200 (instead of the 101 upgrade) when the
# request doesn't look like it came from the site's own browser session. A real browser UA + the
# logged-in session Cookie are what flip it into the WebSocket code path. Override the UA via
# BOOKMAKER_WS_USER_AGENT; paste your browser's Cookie header into BOOKMAKER_WS_COOKIE.
DEFAULT_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


def american_to_decimal(american) -> Optional[float]:
    """American odds → decimal. +779 → 8.79, -1603 → 1.0624. None/0 → None."""
    if american is None:
        return None
    try:
        a = float(american)
    except (TypeError, ValueError):
        return None
    if a == 0:
        return None
    return round(1.0 + (a / 100.0 if a > 0 else 100.0 / abs(a)), 4)


def _build_frame(command: str, headers: dict, body: str = "") -> str:
    """COMMAND\\n k:v\\n … \\n\\n body \\x00  — note the trailing NULL byte STOMP requires."""
    head = "\n".join([command] + [f"{k}:{v}" for k, v in headers.items()])
    return f"{head}\n\n{body}{NULL}"


def _dump_handshake_failure(e: Exception) -> None:
    """When the WS upgrade is rejected, surface the server's actual response so we can see WHY it
    served a non-101 status. websockets carries this differently per version:
      • new (InvalidStatus):     e.response  → .status_code / .headers / .body
      • old (InvalidStatusCode): e.status_code / e.headers (no body)
    """
    resp = getattr(e, "response", None)
    status = getattr(resp, "status_code", None) if resp is not None else getattr(e, "status_code", None)
    headers = getattr(resp, "headers", None) if resp is not None else getattr(e, "headers", None)
    if status is None and headers is None:
        return  # not a handshake-status failure (e.g. DNS/TLS/connection) — nothing extra to show
    print(f"[BOOKMAKER STOMP]   server status: {status}")
    if headers is not None:
        for k in ("content-type", "location", "server", "set-cookie", "www-authenticate"):
            try:
                v = headers.get(k)
            except Exception:
                v = None
            if v:
                print(f"[BOOKMAKER STOMP]   {k}: {v}")
    body = getattr(resp, "body", None)
    if body:
        if isinstance(body, (bytes, bytearray)):
            body = bytes(body).decode("utf-8", "replace")
        snippet = body.strip().replace("\n", " ")[:400]
        if snippet:
            print(f"[BOOKMAKER STOMP]   body: {snippet}")


def parse_stomp_frame(raw: str):
    """(command, headers, body). Heartbeats / empty frames → (None, {}, '')."""
    raw = raw.strip(NULL).replace("\r\n", "\n")  # STOMP 1.2 may use CRLF; normalize to LF
    if not raw.strip():
        return None, {}, ""                      # server heart-beat (lone newline)
    head, _, body = raw.partition("\n\n")
    lines = head.split("\n")
    command = lines[0].strip()
    headers = {}
    for ln in lines[1:]:
        if ":" in ln:
            k, _, v = ln.partition(":")
            headers[k.strip()] = v.strip()
    return command, headers, body.strip(NULL).strip()


def _main_line(arr):
    """Primary line of an m/s/t array — the entry with i==0, else the first."""
    if not arr:
        return None
    for e in arr:
        if e.get("i") == 0:
            return e
    return arr[0]


def parse_markets(payload: list) -> dict:
    """
    Raw bookmaker payload (a list) → {lid: {moneyline, spread, total, active}} with DECIMAL odds.
    Per the data dictionary:
      mkt.m (Moneyline): h = home odds, v = visitor odds (American)
      mkt.s (Spread):    hp/vp = home/visitor handicap, h/v = odds for that side
      mkt.t (Totals):    hp(=vp) = the line, h = OVER odds, v = UNDER odds
    Only the primary line (i==0) is surfaced; alternates are ignored for now.
    """
    out = {}
    for obj in payload or []:
        lid = obj.get("lid")
        if lid is None:
            continue
        mkt = obj.get("mkt") or {}
        if not mkt:
            # Not a line market — bookmaker.eu also pushes flat {tnm,odd} prop/multi-runner feeds on
            # "TNT.*" topics (no "mkt" block). Skip them so they can't clobber a game's cached moneyline.
            continue
        entry = {"lid": lid, "active": bool(obj.get("la", True))}

        m = _main_line(mkt.get("m"))
        if m:
            entry["moneyline"] = {
                "home":             american_to_decimal(m.get("h")),
                "visitor":          american_to_decimal(m.get("v")),
                "home_american":    m.get("h"),
                "visitor_american": m.get("v"),
            }
        s = _main_line(mkt.get("s"))
        if s:
            entry["spread"] = {
                "home":    {"line": s.get("hp"), "odds": american_to_decimal(s.get("h"))},
                "visitor": {"line": s.get("vp"), "odds": american_to_decimal(s.get("v"))},
            }
        t = _main_line(mkt.get("t"))
        if t:
            entry["total"] = {
                "line":  t.get("hp"),
                "over":  american_to_decimal(t.get("h")),
                "under": american_to_decimal(t.get("v")),
            }
        out[lid] = entry
    return out


class StompOddsClient:
    """Owns the WS+STOMP session: connect → CONNECTED → SUBSCRIBE → heartbeat + listen, with reconnect.
    Calls on_markets(parsed_dict) for every MESSAGE payload. Cancel the run() task to stop."""

    def __init__(self, ws_url: str, queue: str,
                 on_markets: Callable[[dict], None],
                 heartbeat_ms: int = 20000,
                 subprotocols=None,
                 origin: Optional[str] = None,
                 debug: bool = False):
        self._url = ws_url
        self._queue = queue
        self._on_markets = on_markets
        self._hb_ms = heartbeat_ms
        self._debug = debug
        self._hb_in = 0      # heartbeats received (proves the socket is alive even with no odds)
        # RabbitMQ Web-STOMP usually negotiates a vNN.stomp subprotocol (as stomp.js does). Override via
        # BOOKMAKER_WS_SUBPROTOCOLS="" to disable if the handshake is rejected.
        self._subprotocols = subprotocols if subprotocols is not None else ["v10.stomp", "v11.stomp", "v12.stomp"]
        self._origin = origin
        self.connected = False

    async def run(self) -> None:
        while True:
            try:
                await self._session()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                print(f"[BOOKMAKER STOMP] {type(e).__name__}: {e} — reconnecting in 5s")
                _dump_handshake_failure(e)
            self.connected = False
            await asyncio.sleep(5)

    async def _session(self) -> None:
        kwargs = {}
        if self._subprotocols:
            kwargs["subprotocols"] = self._subprotocols
        if self._origin:
            kwargs["origin"] = self._origin

        # Make the handshake look like the site's own browser: real UA, session Cookie, and the
        # cache/lang headers a fetch carries. Without these, bookmaker.eu returns HTTP 200 (no upgrade).
        extra = {
            "Accept-Language": "en-US,en;q=0.9",
            "Pragma": "no-cache",
            "Cache-Control": "no-cache",
        }
        cookie = os.environ.get("BOOKMAKER_WS_COOKIE")
        if cookie:
            extra["Cookie"] = cookie
        ua = os.environ.get("BOOKMAKER_WS_USER_AGENT") or DEFAULT_UA

        # websockets renamed extra_headers→additional_headers (legacy vs asyncio impl) and exposes a
        # dedicated user_agent_header; feature-detect so this works across installed versions.
        params = set(inspect.signature(websockets.connect).parameters)
        header_kw = "additional_headers" if "additional_headers" in params else "extra_headers"
        kwargs[header_kw] = extra
        if "user_agent_header" in params:
            kwargs["user_agent_header"] = ua
        else:
            extra.setdefault("User-Agent", ua)

        async with websockets.connect(self._url, **kwargs) as ws:
            # 1) CONNECT → CONNECTED
            await ws.send(_build_frame("CONNECT", {
                "login": STOMP_LOGIN,
                "passcode": STOMP_PASSCODE,
                "host": STOMP_HOST,
                "accept-version": "1.0,1.1,1.2",
                "heart-beat": f"{self._hb_ms},{self._hb_ms}",
            }))
            for _ in range(5):  # skip any leading heartbeats until CONNECTED
                cmd, _, _ = parse_stomp_frame(await ws.recv())
                if cmd == "CONNECTED":
                    break
                if cmd is not None:
                    raise RuntimeError(f"expected CONNECTED, got {cmd!r}")
            else:
                raise RuntimeError("no CONNECTED frame")

            # 2) SUBSCRIBE to the dynamic session queue
            await ws.send(_build_frame("SUBSCRIBE", {
                "durable": "false",
                "auto-delete": "false",
                "x-queue-name": self._queue,
                "x-expires": "10000",
                "ack": "auto",
                "id": "sub-0",
                "destination": f"/amq/queue/{self._queue}",
            }))
            self.connected = True
            print(f"[BOOKMAKER STOMP] subscribed (queue={self._queue[:16]}…)")

            # 3) heartbeat + listen
            hb = asyncio.create_task(self._heartbeat(ws))
            try:
                async for raw in ws:
                    if isinstance(raw, bytes):
                        raw = raw.decode("utf-8", "replace")
                    cmd, headers, body = parse_stomp_frame(raw)
                    if self._debug:
                        if cmd is None:
                            self._hb_in += 1
                            if self._hb_in % 5 == 1:   # don't spam every 20s heartbeat
                                print(f"[BOOKMAKER STOMP] ‹alive: {self._hb_in} heartbeats, no odds frames yet›")
                        else:
                            dest = headers.get("destination", "?")
                            print(f"[BOOKMAKER STOMP] {cmd} dest={dest} body[:140]={body[:140]!r}")
                    if cmd != "MESSAGE" or not body:
                        continue
                    try:
                        payload = json.loads(body)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(payload, dict):
                        payload = [payload]
                    parsed = parse_markets(payload)
                    if parsed:
                        self._on_markets(parsed)
            finally:
                hb.cancel()

    async def _heartbeat(self, ws) -> None:
        interval = max(1.0, self._hb_ms / 1000.0 - 2.0)   # a touch under the negotiated 20 s
        try:
            while True:
                await asyncio.sleep(interval)
                await ws.send("\n")
        except (asyncio.CancelledError, websockets.ConnectionClosed):
            return


if __name__ == "__main__":
    from env_util import load_dotenv_upwards   # smoke test: pick up BOOKMAKER_* from the repo .env
    load_dotenv_upwards()
    url, queue = os.environ.get("BOOKMAKER_WSS_URL"), os.environ.get("BOOKMAKER_STOMP_QUEUE")
    if not url or not queue:
        raise SystemExit("set BOOKMAKER_WSS_URL and BOOKMAKER_STOMP_QUEUE to smoke-test the live feed")

    def show(parsed: dict):
        for lid, e in parsed.items():
            ml = e.get("moneyline", {})
            print(f"lid={lid} active={e['active']}  ML home={ml.get('home')} visitor={ml.get('visitor')}")

    asyncio.run(StompOddsClient(url, queue, show,
                                origin=os.environ.get("BOOKMAKER_WS_ORIGIN") or None,
                                debug=os.environ.get("BOOKMAKER_DEBUG_FRAMES") == "1").run())
