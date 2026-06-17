"""
BookmakerAdapter — bookmaker.eu integration for the HardVen sidecar (PRE-MATCH odds path).

Pre-match odds come from bookmaker.eu's `GetGameView` HTTP endpoint (a clean JSON board snapshot), NOT
the WebSocket — the WS only streams in-play deltas (see bookmaker_stomp.py, kept for a future in-play
path). For a pre-match-only POC we simply POLL GetGameView.

The catch is Cloudflare: a bare HTTP client gets a 200 challenge (cf_clearance is fingerprint+IP bound).
So we run the fetch INSIDE a real logged-in Chrome via Playwright `page.evaluate(fetch(...))` — the request
inherits the browser's session + Cloudflare clearance and sails through. The browser does NOT need to
navigate to each game; GetGameView is a direct query by GameId, so one logged-in be.bookmaker.eu tab can
poll odds for every paired game.

Selection ids are "<GameId>:<LeagueId>:H" (home) / "<GameId>:<LeagueId>:V" (visitor) — both ids are
needed for the GetGameView call, so we encode them in the id and the sidecar stays stateless. Put those
in cross_pairs.json as hardven_yes_token / hardven_no_token (map by the actual PLAYER: Kalshi
"Will X win?" YES → whichever of :H/:V IS player X).

CONFIG (env; the sidecar loads the repo root .env):
  BOOKMAKER_HEADLESS       — "1" forces headless. DEFAULT is headful so you can clear Cloudflare / log in
                             by hand the first time; the persistent profile then remembers it.
  BOOKMAKER_USER_DATA_DIR  — persistent Chrome profile dir (keeps login + Cloudflare clearance). Default
                             ".bookmaker_profile".
  BOOKMAKER_BASE_URL       — page to sit on (default https://be.bookmaker.eu). Must be the be. host so the
                             GetGameView fetch is same-origin.
  BOOKMAKER_ODDS_TTL_MS    — min ms between GetGameView fetches per game (default 4000); the C# feed polls
                             ~every 9 s, so each poll is fresh.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Optional

from book_adapter import BookAdapter, BetResult, CatalogEntry, Selection
from bookmaker_gameview import parse_gameview

BASE_URL = os.environ.get("BOOKMAKER_BASE_URL", "https://be.bookmaker.eu")
GAMEVIEW_URL = "https://be.bookmaker.eu/gateway/BetslipProxy.aspx/GetGameView"

# Run the POST from inside the page so it carries the session cookie + Cloudflare clearance (same-origin).
_GAMEVIEW_JS = """
async (arg) => {
  const headers = {
    'content-type': 'application/json',
    'accept': 'application/json, text/plain, */*',
    'cache-control': 'no-cache',
    'pragma': 'no-cache',
  };
  if (arg.rtqname) headers['rtqname'] = arg.rtqname;
  const r = await fetch(arg.url, {
    method: 'POST',
    headers: headers,
    body: JSON.stringify(arg.body),
    credentials: 'include',
  });
  const text = await r.text();
  return { status: r.status, text: text };
}
"""


def _gameview_body(game_id: str, league_id: str) -> dict:
    """The exact GetGameView POST body shape (captured from the site), parameterised by game/league."""
    return {"o": {"BORequestData": {"BOParameters": {
        "BORt": {}, "GameId": str(game_id), "LeagueId": str(league_id), "LanguageId": "0",
        "LineStyle": "E", "ClientTimeStamp": "", "LinkDeriv": "true", "ShowPeriods": "false",
        "IdEventList": "",
    }}}}


class BookmakerAdapter(BookAdapter):
    name = "bookmaker"

    def __init__(self) -> None:
        self._odds_cache: dict[str, Selection] = {}        # "<gid>:<lid>:H/V" -> Selection
        self._last_fetch: dict[tuple, float] = {}          # (gid, lid) -> unix ts of last GetGameView
        self._ttl = float(os.environ.get("BOOKMAKER_ODDS_TTL_MS", "4000")) / 1000.0
        self._fetch_lock = asyncio.Lock()
        self._bet_lock = asyncio.Lock()
        self._pw = None
        self._ctx = None
        self._page = None
        self._rtqname: Optional[str] = None   # captured from the site's own traffic; required by GetGameView

    # ── session lifecycle ──────────────────────────────────────────────────────
    async def startup(self) -> None:
        await self._start_browser()
        print("[BOOKMAKER] ready. Odds via GetGameView polling (selection id = '<GameId>:<LeagueId>:H|V').\n"
              "[BOOKMAKER] >>> click into ANY match in the window once: it seeds the 'rtqname' session header "
              "and sets a deep referer, both of which GetGameView requires.")

    async def shutdown(self) -> None:
        # The user closing the window mid-run makes close() raise "Connection closed" — swallow it.
        if self._ctx:
            try:
                await self._ctx.close()
            except Exception:
                pass
        if self._pw:
            try:
                await self._pw.stop()
            except Exception:
                pass

    def _on_request(self, req) -> None:
        """Sniff the site's own requests for the session 'rtqname' header so our fetch can replay it."""
        try:
            q = req.headers.get("rtqname")
        except Exception:
            q = None
        if q and q != self._rtqname:
            self._rtqname = q
            print(f"[BOOKMAKER] captured rtqname (…{q[-12:]}) — GetGameView polling enabled.")

    # ── odds (poll GetGameView per requested game) ──────────────────────────────
    @staticmethod
    def _parse_sid(sid: str):
        parts = sid.split(":")
        if len(parts) == 3 and parts[0] and parts[1] and parts[2] in ("H", "V"):
            return parts[0], parts[1], parts[2]   # (game_id, league_id, side)
        return None

    async def odds(self, selection_ids: list[str]) -> dict[str, Selection]:
        games = set()
        for sid in selection_ids:
            p = self._parse_sid(sid)
            if p:
                games.add((p[0], p[1]))
        for gid, lid in games:
            await self._ensure_fresh(gid, lid)
        return {sid: self._odds_cache[sid] for sid in selection_ids if sid in self._odds_cache}

    async def _ensure_fresh(self, game_id: str, league_id: str) -> None:
        key = (game_id, league_id)
        if time.time() - self._last_fetch.get(key, 0.0) < self._ttl:
            return
        async with self._fetch_lock:
            if time.time() - self._last_fetch.get(key, 0.0) < self._ttl:
                return  # filled while we waited for the lock
            data = await self._fetch_gameview(game_id, league_id)
            self._last_fetch[key] = time.time()
            if not data:
                return
            parsed = parse_gameview(data)   # keyed "<idgm>:H/V"
            now = time.time()
            for side in ("H", "V"):
                full = f"{game_id}:{league_id}:{side}"
                e = parsed.get(f"{game_id}:{side}")
                if e and e["status"] == "open" and e["decimal_odds"]:
                    self._odds_cache[full] = Selection(full, decimal_odds=float(e["decimal_odds"]),
                                                       max_stake=float(e["max_stake"] or 0.0),
                                                       status="open", ts=now)
                else:
                    # suspended / missing → no usable price, so no arb can fire on it
                    self._odds_cache[full] = Selection(full, decimal_odds=1.0, max_stake=0.0,
                                                       status="suspended", ts=now)

    async def _fetch_gameview(self, game_id: str, league_id: str) -> Optional[dict]:
        if not self._page:
            return None
        if not self._rtqname:
            print("[BOOKMAKER] no rtqname captured yet — click into any match in the window once.")
        try:
            res = await self._page.evaluate(_GAMEVIEW_JS,
                                            {"url": GAMEVIEW_URL, "rtqname": self._rtqname,
                                             "body": _gameview_body(game_id, league_id)})
        except Exception as e:
            print(f"[BOOKMAKER] GetGameView fetch error gid={game_id}: {e}")
            return None
        status, text = res.get("status"), res.get("text") or ""
        if status != 200:
            print(f"[BOOKMAKER] GetGameView HTTP {status} gid={game_id} "
                  f"(logged in? Cloudflare?) body[:120]={text[:120]!r}")
            return None
        try:
            data = json.loads(text)
        except ValueError:
            print(f"[BOOKMAKER] GetGameView non-JSON gid={game_id} body[:120]={text[:120]!r}")
            return None
        # ASP.NET page methods sometimes wrap the payload in {"d": …}; unwrap if present.
        if isinstance(data, dict) and "d" in data and "GameView" not in data:
            d = data["d"]
            data = json.loads(d) if isinstance(d, str) else d
        return data

    # ── pairing catalog ────────────────────────────────────────────────────────
    async def catalog(self) -> list[CatalogEntry]:
        # TODO(pairing): enumerate upcoming events (there's a schedule/league-listing XHR alongside
        # GetGameView). For now cross_pairs.json is hand-built with "<GameId>:<LeagueId>:H/V" ids.
        return []

    # ── M1: betting + wallet confirmation (Playwright bet slip) ─────────────────
    async def balance(self) -> float:
        return 0.0  # TODO(M1): read account balance.

    async def place_bet(self, selection_id: str, stake: float, max_odds: float) -> BetResult:
        async with self._bet_lock:
            # TODO(M1): drive the bet slip (selection → stake → handle "odds changed?" accept only if
            # odds <= max_odds → confirm → read bet id + accepted odds). IRREVERSIBLE.
            return BetResult(accepted=False, reason="place_bet not implemented (M1; needs Playwright bet slip)")

    async def open_bets(self) -> list[dict]:
        return []  # TODO(M1)

    async def bet(self, bet_id: str) -> Optional[dict]:
        return None  # TODO(M1)

    # ── Playwright ──────────────────────────────────────────────────────────────
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
        self._ctx.on("request", self._on_request)   # capture rtqname from the site's own traffic
        await self._page.goto(BASE_URL, wait_until="domcontentloaded")
        print(f"[BOOKMAKER] browser on {BASE_URL} (headless={headless}). If GetGameView returns 401/Cloudflare, "
              "log in / clear the check in the window — the persistent profile remembers it.")


if __name__ == "__main__":
    # Standalone smoke test: poll GetGameView for the given selection ids and print the odds.
    #   python sidecar/bookmaker_adapter.py 51989880:16036:H 51989880:16036:V
    # First time only:  pip install -r requirements.txt && playwright install chromium
    import sys
    from env_util import load_dotenv_upwards

    async def _smoke() -> None:
        load_dotenv_upwards()
        ids = sys.argv[1:] or ["51989880:16036:H", "51989880:16036:V"]
        a = BookmakerAdapter()
        await a.startup()
        try:
            for _ in range(20):
                result = await a.odds(ids)
                print(f"[SMOKE] {time.strftime('%H:%M:%S')}")
                for sid in ids:
                    s = result.get(sid)
                    if s:
                        print(f"        {sid}  dec={s.decimal_odds}  implied={round(s.implied_price,4)}  "
                              f"max_contracts={round(s.max_contracts,1)}  {s.status}")
                    else:
                        print(f"        {sid}  (not returned)")
                await asyncio.sleep(10)
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
        finally:
            await a.shutdown()

    asyncio.run(_smoke())
