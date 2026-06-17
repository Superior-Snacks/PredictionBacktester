"""
BookmakerAdapter — bookmaker.eu integration for the HardVen sidecar.

ODDS are read by SNIFFING the WebSocket frames the site's own page receives, via Playwright + Chrome
DevTools Protocol (CDP `Network.webSocketFrameReceived`). We do NOT open our own socket: bookmaker.eu
sits behind Cloudflare bot-management, and Cloudflare ties `cf_clearance` to the browser's TLS/JA3
fingerprint — a raw Python `websockets` client gets a 200 challenge no matter what cookie/UA it sends.
So we let the real Chrome (which already passes Cloudflare, holds the login, and subscribes to the
right session queue) do the talking, and we just read the bytes off its socket and run them through the
same STOMP parsers in bookmaker_stomp (parse_stomp_frame → parse_markets). This sidesteps Cloudflare,
the session cookie, and the dynamic queue string entirely.

Each game's moneyline becomes two selections:
    "<lid>:H"  → home moneyline   (decimal odds)
    "<lid>:V"  → visitor moneyline (decimal odds)
Put those selection ids in cross_pairs.json as hardven_yes_token / hardven_no_token (YES = the same
outcome as the Kalshi market; NO = the opposite side). Spread/total are parsed too (see bookmaker_stomp)
but not surfaced as selections yet.

CONFIG (env; the sidecar loads the repo root .env):
  BOOKMAKER_HEADLESS       — "1" forces headless. DEFAULT is headful so you can clear Cloudflare / log in
                             by hand the first time; the persistent profile then remembers it.
  BOOKMAKER_USER_DATA_DIR  — persistent Chrome profile dir (keeps login + Cloudflare clearance). Default
                             ".bookmaker_profile".
  BOOKMAKER_WATCH_URL      — optional: a game page URL to open on startup so its odds start streaming.
                             Omit and just click into the match yourself in the headful window.
  BOOKMAKER_MAX_STAKE      — assumed max stake per selection (feed omits it; default 250)
  BOOKMAKER_USER / PASS    — optional auto-login creds (selectors are a TODO; hand-login works via profile)

The old BOOKMAKER_WSS_URL / BOOKMAKER_STOMP_QUEUE / BOOKMAKER_WS_COOKIE vars are no longer used for odds
(the browser owns the socket). They remain only for the standalone bookmaker_stomp.py smoke test.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import time
from typing import Optional

from book_adapter import BookAdapter, BetResult, CatalogEntry, Selection
from bookmaker_stomp import parse_markets, parse_stomp_frame

BASE_URL = os.environ.get("BOOKMAKER_BASE_URL", "https://www.bookmaker.eu")
NULL = "\x00"
# The site multiplexes several sockets (analytics, etc.); the odds feed is RealTimeHandler.ashx.
ODDS_URL_MARKER = "realtimehandler"


class BookmakerAdapter(BookAdapter):
    name = "bookmaker"

    def __init__(self) -> None:
        self._odds_cache: dict[str, Selection] = {}   # "<lid>:H" / "<lid>:V" -> Selection
        self._max_stake = float(os.environ.get("BOOKMAKER_MAX_STAKE", "250"))
        self._bet_lock = asyncio.Lock()
        # Playwright + CDP
        self._pw = None
        self._ctx = None
        self._page = None
        self._cdp = None
        self._odds_request_ids: set[str] = set()  # CDP requestIds for the odds socket(s)
        self._frames_seen = 0

    # ── session lifecycle ──────────────────────────────────────────────────────
    async def startup(self) -> None:
        # The browser is MANDATORY for this book — it's the only way past Cloudflare to the odds feed.
        await self._start_browser()
        await self._attach_cdp()
        watch = os.environ.get("BOOKMAKER_WATCH_URL", "").strip()
        if watch:
            try:
                await self._page.goto(watch, wait_until="domcontentloaded")
                print(f"[BOOKMAKER] opened watch URL → {watch}")
            except Exception as e:
                print(f"[BOOKMAKER] could not open BOOKMAKER_WATCH_URL ({e}); navigate manually.")
        print("[BOOKMAKER] sniffing odds via CDP. Click into a game in the browser window if frames "
              "don't start arriving (look for '[BOOKMAKER] odds socket …').")

    async def shutdown(self) -> None:
        if self._ctx:
            try:
                await self._ctx.close()
            finally:
                if self._pw:
                    await self._pw.stop()

    # ── odds (CDP frame sniff → cache) ─────────────────────────────────────────
    def _on_ws_created(self, params: dict) -> None:
        url = (params.get("url") or "")
        if ODDS_URL_MARKER in url.lower():
            rid = params.get("requestId")
            if rid is not None and rid not in self._odds_request_ids:
                self._odds_request_ids.add(rid)
                print(f"[BOOKMAKER] odds socket detected (requestId={rid}) {url[:60]}…")

    def _on_ws_frame(self, params: dict) -> None:
        rid = params.get("requestId")
        # Once we've positively identified the odds socket(s), only read those. Until then (set empty),
        # read everything so we never miss frames from a socket created before CDP attached.
        if self._odds_request_ids and rid not in self._odds_request_ids:
            return
        resp = params.get("response") or {}
        opcode = resp.get("opcode")
        payload = resp.get("payloadData")
        if payload is None or opcode in (8, 9, 10):  # close / ping / pong control frames
            return
        if opcode == 2:  # binary → base64 in CDP
            try:
                payload = base64.b64decode(payload).decode("utf-8", "replace")
            except Exception:
                return
        # A single WS frame can carry one OR several NULL-terminated STOMP frames — split and parse each.
        for chunk in payload.split(NULL):
            if not chunk.strip():
                continue
            cmd, _, body = parse_stomp_frame(chunk)
            if cmd != "MESSAGE" or not body:
                continue
            try:
                data = json.loads(body)
            except ValueError:
                continue
            if isinstance(data, dict):
                data = [data]
            parsed = parse_markets(data)
            if parsed:
                self._ingest(parsed)

    def _ingest(self, parsed: dict) -> None:
        """Fold each game's moneyline into the selection cache."""
        now = time.time()
        self._frames_seen += 1
        for lid, e in parsed.items():
            ml = e.get("moneyline") or {}
            active = e.get("active", True)
            for side, key in (("home", "H"), ("visitor", "V")):
                sid = f"{lid}:{key}"
                dec = ml.get(side)
                if active and dec and dec > 1.0:
                    self._odds_cache[sid] = Selection(sid, decimal_odds=float(dec),
                                                      max_stake=self._max_stake, status="open", ts=now)
                else:
                    # suspended / missing → empty-ish so no arb can fire on it
                    self._odds_cache[sid] = Selection(sid, decimal_odds=1.0,
                                                      max_stake=0.0, status="suspended", ts=now)

    async def odds(self, selection_ids: list[str]) -> dict[str, Selection]:
        return {sid: self._odds_cache[sid] for sid in selection_ids if sid in self._odds_cache}

    # ── pairing catalog ────────────────────────────────────────────────────────
    async def catalog(self) -> list[CatalogEntry]:
        # TODO(M1+): enumerate the book's events/markets for automated pairing. The feed is push-based
        # (you receive what the page subscribes to), so the catalog likely comes from an HTTP/XHR listing
        # endpoint — capture it via devtools like the odds feed.
        return []

    # ── M1: betting + wallet confirmation (Playwright) ─────────────────────────
    async def balance(self) -> float:
        # TODO(M1): read account balance (Playwright header scrape or the balance XHR).
        return 0.0

    async def place_bet(self, selection_id: str, stake: float, max_odds: float) -> BetResult:
        async with self._bet_lock:  # one bet at a time on the single browser session
            # TODO(M1): drive the bet slip via Playwright (selection → stake → handle "odds changed?"
            # accept only if odds <= max_odds → confirm → read bet id + accepted odds). IRREVERSIBLE.
            return BetResult(accepted=False, reason="place_bet not implemented (M1; needs Playwright bet slip)")

    async def open_bets(self) -> list[dict]:
        return []  # TODO(M1): "My Bets" / open wagers

    async def bet(self, bet_id: str) -> Optional[dict]:
        return None  # TODO(M1)

    # ── Playwright + CDP plumbing ───────────────────────────────────────────────
    async def _start_browser(self) -> None:
        from playwright.async_api import async_playwright
        self._pw = await async_playwright().start()
        user_data = os.environ.get("BOOKMAKER_USER_DATA_DIR", ".bookmaker_profile")
        headless = os.environ.get("BOOKMAKER_HEADLESS") == "1"  # DEFAULT headful (Cloudflare/login)
        self._ctx = await self._pw.chromium.launch_persistent_context(
            user_data_dir=user_data, headless=headless,
            viewport={"width": 1400, "height": 900},
        )
        self._page = self._ctx.pages[0] if self._ctx.pages else await self._ctx.new_page()
        await self._page.goto(BASE_URL, wait_until="domcontentloaded")
        if not (os.environ.get("BOOKMAKER_USER") and os.environ.get("BOOKMAKER_PASS")):
            print("[BOOKMAKER] log in by hand in the browser window (persistent profile keeps the "
                  "session + Cloudflare clearance for next runs).")
        # TODO(recon): submit the login form selectors here if you want auto-login.
        print(f"[BOOKMAKER] browser session ready (headless={headless})")

    async def _attach_cdp(self) -> None:
        # CDP session on the page → subscribe to WebSocket lifecycle + frame events.
        self._cdp = await self._ctx.new_cdp_session(self._page)
        await self._cdp.send("Network.enable")
        self._cdp.on("Network.webSocketCreated", self._on_ws_created)
        self._cdp.on("Network.webSocketFrameReceived", self._on_ws_frame)
        print("[BOOKMAKER] CDP attached (Network.webSocketFrameReceived).")


if __name__ == "__main__":
    # Standalone smoke test of the CDP odds sniff (no FastAPI). A headful Chrome opens — clear Cloudflare
    # / log in / click into a game; this prints the moneyline odds it captures every few seconds.
    #   python sidecar/bookmaker_adapter.py
    # First time only:  pip install -r requirements.txt && playwright install chromium
    from env_util import load_dotenv_upwards

    async def _smoke() -> None:
        load_dotenv_upwards()
        a = BookmakerAdapter()
        await a.startup()
        try:
            while True:
                await asyncio.sleep(5)
                open_sel = {k: v for k, v in a._odds_cache.items() if v.status == "open"}
                print(f"[SMOKE] frames={a._frames_seen} cached={len(a._odds_cache)} open={len(open_sel)}")
                for sid, sel in list(open_sel.items())[:8]:
                    print(f"        {sid}  decimal={sel.decimal_odds}  implied={round(1/sel.decimal_odds, 4)}")
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
        finally:
            await a.shutdown()

    asyncio.run(_smoke())
