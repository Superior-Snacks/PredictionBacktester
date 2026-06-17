"""
BookmakerAdapter — bookmaker.eu integration for the HardVen sidecar.

ODDS come from the STOMP-over-WebSocket feed (bookmaker_stomp.StompOddsClient) — no browser needed to
read prices. Each game's moneyline becomes two selections:
    "<lid>:H"  → home moneyline   (decimal odds)
    "<lid>:V"  → visitor moneyline (decimal odds)
Put those selection ids in cross_pairs.json as hardven_yes_token / hardven_no_token (YES = the same
outcome as the Kalshi market; NO = the opposite side). Spread/total are parsed too (see bookmaker_stomp)
but not surfaced as selections yet.

CONFIG (env; the sidecar loads the repo root .env):
  BOOKMAKER_WSS_URL        — wss:// STOMP endpoint               (required for odds)
  BOOKMAKER_STOMP_QUEUE    — dynamic session queue string         (required for odds; from init XHR)
  BOOKMAKER_WS_ORIGIN      — Origin header, if the WS handshake needs it (optional)
  BOOKMAKER_MAX_STAKE      — assumed max stake per selection (feed omits it; default 250)
  BOOKMAKER_ENABLE_BROWSER — "1" to launch Playwright (login + bet placement, M1; not needed for odds)
  BOOKMAKER_USER/PASS, BOOKMAKER_HEADFUL, BOOKMAKER_USER_DATA_DIR — Playwright login (M1)

TODO(recon): the dynamic queue string + WSS URL are per-session. For now supply them via env (grab from
devtools). Later, parse them from the site's init XHR inside _start_browser() so it's automatic.
"""
from __future__ import annotations

import asyncio
import os
import time
from typing import Optional

from book_adapter import BookAdapter, BetResult, CatalogEntry, Selection
from bookmaker_stomp import StompOddsClient

BASE_URL = os.environ.get("BOOKMAKER_BASE_URL", "https://bookmaker.eu")


class BookmakerAdapter(BookAdapter):
    name = "bookmaker"

    def __init__(self) -> None:
        self._odds_cache: dict[str, Selection] = {}   # "<lid>:H" / "<lid>:V" -> Selection
        self._max_stake = float(os.environ.get("BOOKMAKER_MAX_STAKE", "250"))
        self._stomp: Optional[StompOddsClient] = None
        self._stomp_task: Optional[asyncio.Task] = None
        self._bet_lock = asyncio.Lock()
        # Playwright (M1 betting / login / queue recon) — optional, lazy
        self._pw = None
        self._ctx = None
        self._page = None

    # ── session lifecycle ──────────────────────────────────────────────────────
    async def startup(self) -> None:
        if os.environ.get("BOOKMAKER_ENABLE_BROWSER") == "1":
            await self._start_browser()
            # TODO(recon): parse BOOKMAKER_WSS_URL + dynamic queue from the init XHR here, so they don't
            # have to be supplied by hand once the browser session is up.

        ws_url = os.environ.get("BOOKMAKER_WSS_URL", "").strip()
        queue  = os.environ.get("BOOKMAKER_STOMP_QUEUE", "").strip()
        if ws_url and queue:
            self._stomp = StompOddsClient(
                ws_url, queue, self._ingest,
                origin=os.environ.get("BOOKMAKER_WS_ORIGIN") or None,
            )
            self._stomp_task = asyncio.create_task(self._stomp.run())
            print(f"[BOOKMAKER] STOMP odds client started → {ws_url[:48]}…")
        else:
            print("[BOOKMAKER] BOOKMAKER_WSS_URL / BOOKMAKER_STOMP_QUEUE not set — no odds feed. "
                  "Grab them from the site's init XHR (devtools) and set them in .env/env.")

    async def shutdown(self) -> None:
        if self._stomp_task:
            self._stomp_task.cancel()
            try:
                await self._stomp_task
            except (asyncio.CancelledError, Exception):
                pass
        if self._ctx:
            try:
                await self._ctx.close()
            finally:
                if self._pw:
                    await self._pw.stop()

    # ── odds (STOMP feed → cache) ──────────────────────────────────────────────
    def _ingest(self, parsed: dict) -> None:
        """Callback from StompOddsClient: fold each game's moneyline into the selection cache."""
        now = time.time()
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
        # TODO(M1+): enumerate the book's events/markets for automated pairing. The STOMP feed is
        # subscription-based (you receive what's pushed), so the catalog likely comes from an HTTP/XHR
        # listing endpoint — capture it via devtools like the odds feed.
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

    # ── Playwright (M1 / queue recon) — only when BOOKMAKER_ENABLE_BROWSER=1 ────
    async def _start_browser(self) -> None:
        from playwright.async_api import async_playwright
        self._pw = await async_playwright().start()
        user_data = os.environ.get("BOOKMAKER_USER_DATA_DIR", ".bookmaker_profile")
        headless = os.environ.get("BOOKMAKER_HEADFUL") != "1"
        self._ctx = await self._pw.chromium.launch_persistent_context(
            user_data_dir=user_data, headless=headless,
            viewport={"width": 1400, "height": 900},
        )
        self._page = self._ctx.pages[0] if self._ctx.pages else await self._ctx.new_page()
        await self._page.goto(BASE_URL, wait_until="domcontentloaded")
        user, pw = os.environ.get("BOOKMAKER_USER"), os.environ.get("BOOKMAKER_PASS")
        if not user or not pw:
            print("[BOOKMAKER] BOOKMAKER_USER/PASS not set — log in by hand (persistent profile keeps it).")
        # TODO(recon): submit the login form selectors here if you want auto-login.
        print(f"[BOOKMAKER] browser session ready (headless={headless})")
