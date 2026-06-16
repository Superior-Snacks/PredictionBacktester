"""
BookmakerAdapter — Playwright-driven adapter for bookmaker.eu (the browser path; no API/broker needed).

The plumbing here is real (persistent logged-in session, odds capture via network interception, a
single-session lock so bets can't race). What's left are the SITE-SPECIFIC bits only you can find by
inspecting the logged-in site — each is marked `TODO(recon)`. See README "Recon" for how to find them:

  1. LOGIN          — _ensure_logged_in(): the login URL + form selectors (or rely on the persistent
                      profile so you log in by hand once and the cookies stick).
  2. ODDS SOURCE    — _looks_like_odds() + _parse_odds(): identify the JSON the site fetches for odds
                      and map it to Selection. (DOM-scrape fallback: _scrape_odds_from_dom().)
  3. SELECTION IDs  — pick the key that identifies one outcome (recommend: the id used in that JSON).
                      That key is what goes in cross_pairs.json `hardven_*_id` and what odds() looks up.
  4. BET SLIP (M1)  — place_bet(): selection→stake→confirm flow + the "odds changed?" handling.

Env: BOOKMAKER_USER, BOOKMAKER_PASS (optional if you log in by hand into the persistent profile),
     BOOKMAKER_HEADFUL=1 (show the browser — recommended while developing / for first login),
     BOOKMAKER_USER_DATA_DIR (persistent profile dir; default ".bookmaker_profile"),
     BOOKMAKER_BASE_URL (default "https://bookmaker.eu").

NOTE: automating an account is subject to the book's ToS and will draw stake-limits/bans on a winning
account — this is the expected #1 risk for the browser path. Use your own account, modest stakes.
"""
from __future__ import annotations

import asyncio
import os
import time
from typing import Optional

from book_adapter import BookAdapter, BetResult, CatalogEntry, Selection

BASE_URL = os.environ.get("BOOKMAKER_BASE_URL", "https://bookmaker.eu")


class BookmakerAdapter(BookAdapter):
    name = "bookmaker"

    def __init__(self) -> None:
        self._pw = None
        self._ctx = None           # persistent browser context
        self._page = None
        self._odds_cache: dict[str, Selection] = {}   # selection_id -> latest Selection
        self._bet_lock = asyncio.Lock()               # serialize bets on the single session

    # ── session lifecycle ──────────────────────────────────────────────────────
    async def startup(self) -> None:
        from playwright.async_api import async_playwright
        self._pw = await async_playwright().start()
        user_data = os.environ.get("BOOKMAKER_USER_DATA_DIR", ".bookmaker_profile")
        headless = os.environ.get("BOOKMAKER_HEADFUL") != "1"
        # Persistent context = cookies/login survive restarts (log in by hand once → no re-login spam,
        # which also reduces bot-detection vs. scripting the login every boot).
        self._ctx = await self._pw.chromium.launch_persistent_context(
            user_data_dir=user_data,
            headless=headless,
            viewport={"width": 1400, "height": 900},
        )
        self._page = self._ctx.pages[0] if self._ctx.pages else await self._ctx.new_page()
        # Capture every response the page fetches; _on_response keeps the odds ones.
        self._page.on("response", lambda r: asyncio.ensure_future(self._on_response(r)))
        await self._page.goto(BASE_URL, wait_until="domcontentloaded")
        await self._ensure_logged_in()
        print(f"[BOOKMAKER] session ready (headless={headless}, profile={user_data})")

    async def shutdown(self) -> None:
        try:
            if self._ctx:
                await self._ctx.close()
        finally:
            if self._pw:
                await self._pw.stop()

    async def _ensure_logged_in(self) -> None:
        user, pw = os.environ.get("BOOKMAKER_USER"), os.environ.get("BOOKMAKER_PASS")
        if not user or not pw:
            print("[BOOKMAKER] BOOKMAKER_USER/PASS not set — relying on the persistent profile. "
                  "Run once with BOOKMAKER_HEADFUL=1 and log in by hand; the session will persist.")
            return
        # TODO(recon): detect the logged-out state and submit the login form. Example shape:
        #   if await self._page.query_selector("SELECTOR_FOR_LOGIN_FORM"):
        #       await self._page.fill("SELECTOR_USERNAME", user)
        #       await self._page.fill("SELECTOR_PASSWORD", pw)
        #       await self._page.click("SELECTOR_SUBMIT")
        #       await self._page.wait_for_load_state("networkidle")
        #   (handle 2FA manually with BOOKMAKER_HEADFUL=1 the first time.)
        return

    # ── odds via network interception ──────────────────────────────────────────
    async def _on_response(self, response) -> None:
        try:
            if not self._looks_like_odds(response.url):
                return
            data = await response.json()
            for sel in self._parse_odds(data):
                self._odds_cache[sel.selection_id] = sel
        except Exception:
            return  # non-JSON / unrelated / parse error — ignore

    def _looks_like_odds(self, url: str) -> bool:
        # TODO(recon): return True for the request(s) that carry odds, e.g.:
        #   return any(s in url for s in ("/odds", "/lines", "/markets", "GetLines"))
        return False

    def _parse_odds(self, data) -> list[Selection]:
        # TODO(recon): map the site's JSON → Selection objects.
        #   selection_id  = the site's stable outcome id (use THIS in cross_pairs.json hardven_*_id)
        #   decimal_odds  = convert from American/fractional if needed (helpers below)
        #   max_stake     = the market/account max if exposed, else a safe constant
        return []

    async def _scrape_odds_from_dom(self, selection_ids: list[str]) -> dict[str, Selection]:
        # Fallback if odds never come through as JSON (rendered straight into HTML).
        # TODO(recon): query the odds cells by a stable selector / data-attribute and build Selections.
        return {}

    async def odds(self, selection_ids: list[str]) -> dict[str, Selection]:
        # Pre-match cadence: a few seconds is fine. The page's own live updates refresh the cache via
        # _on_response while the relevant markets are open; _refresh_if_stale can force a navigation.
        await self._refresh_if_stale(selection_ids)
        return {sid: self._odds_cache[sid] for sid in selection_ids if sid in self._odds_cache}

    async def _refresh_if_stale(self, selection_ids: list[str]) -> None:
        # TODO(recon): if odds for these ids aren't flowing, navigate to their market page(s) so the site
        # re-fetches them (the response handler then refreshes the cache). For pre-match this can be lazy.
        return

    # ── pairing catalog ────────────────────────────────────────────────────────
    async def catalog(self) -> list[CatalogEntry]:
        # TODO(recon): walk the sports/league/event tree (or capture the catalog JSON) → CatalogEntry[].
        # Include start_time (pre-live gate) and rules_text/url (feeds the AI-judge pairing).
        return []

    # ── M1: betting + wallet confirmation ──────────────────────────────────────
    async def balance(self) -> float:
        # TODO(recon): read account balance (intercept its JSON, or scrape the header element).
        return 0.0

    async def place_bet(self, selection_id: str, stake: float, max_odds: float) -> BetResult:
        async with self._bet_lock:  # one bet at a time on the single browser session
            # TODO(recon): click the selection (adds to bet slip) → enter stake → handle the
            #   "odds changed, accept?" prompt (accept ONLY if odds <= max_odds) → confirm →
            #   read back the bet id + accepted odds. IRREVERSIBLE once accepted.
            return BetResult(accepted=False, reason="place_bet not implemented for bookmaker.eu yet")

    async def open_bets(self) -> list[dict]:
        # TODO(recon): read the open/pending bets list ("My Bets" / "Open Wagers").
        return []

    async def bet(self, bet_id: str) -> Optional[dict]:
        # TODO(recon): look up one bet by id (confirmation / settlement status).
        return None


# ── odds-format helpers (sportsbooks often quote American or fractional) ───────
def american_to_decimal(american: float) -> float:
    return 1.0 + (american / 100.0 if american > 0 else 100.0 / abs(american))


def fractional_to_decimal(num: float, den: float) -> float:
    return 1.0 + num / den
