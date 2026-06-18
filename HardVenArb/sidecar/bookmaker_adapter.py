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
from bookmaker_gameview import parse_gameview, parse_schedule

BASE_URL = os.environ.get("BOOKMAKER_BASE_URL", "https://be.bookmaker.eu")
GAMEVIEW_URL = "https://be.bookmaker.eu/gateway/BetslipProxy.aspx/GetGameView"
SCHEDULE_URL = "https://be.bookmaker.eu/gateway/BetslipProxy.aspx/GetSchedule"

# Tennis SINGLES league ids for catalog discovery (from GetActiveLeagues; idsport "MU", no doubles).
# These rotate as tournaments come/go — override via BOOKMAKER_CATALOG_LEAGUES="16036,16035,...".
# (Dynamic upgrade: fetch GetActiveLeagues live and filter — needs that request captured.)
DEFAULT_TENNIS_LEAGUES = "16036,16035,16034,16014,20270,20269,16043,15228,16194,19240,20267,20268"

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


def _schedule_body(league_ids) -> dict:
    """GetSchedule POST body (captured). `LeaguesIdList` takes a COMMA-JOINED list → one call covers
    many leagues. Returns every game in those leagues + their moneylines."""
    return {"o": {"BORequestData": {"BOParameters": {
        "BORt": {}, "LeaguesIdList": ",".join(str(x) for x in league_ids), "LanguageId": "0",
        "LineStyle": "E", "ScheduleType": "american", "LinkDeriv": "true",
    }}}}


class BookmakerAdapter(BookAdapter):
    name = "bookmaker"

    def __init__(self) -> None:
        self._odds_cache: dict[str, Selection] = {}        # "<gid>:<lid>:H/V" -> Selection
        self._sched_last_fetch = 0.0                       # unix ts of last GetSchedule
        self._sched_covered: set[str] = set()              # league ids covered by the last fetch
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
        print("[BOOKMAKER] ready. Odds via bulk GetSchedule polling (selection id = '<GameId>:<LeagueId>:H|V').\n"
              "[BOOKMAKER] >>> click into ANY match in the window once: it seeds the 'rtqname' session header "
              "and sets a deep referer, both of which the BetslipProxy calls require.")

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
            print(f"[BOOKMAKER] captured rtqname (…{q[-12:]}) — odds polling enabled.")

    # ── odds (bulk: ONE GetSchedule covers all requested leagues) ───────────────
    @staticmethod
    def _parse_sid(sid: str):
        parts = sid.split(":")
        if len(parts) == 3 and parts[0] and parts[1] and parts[2] in ("H", "V"):
            return parts[0], parts[1], parts[2]   # (game_id, league_id, side)
        return None

    async def odds(self, selection_ids: list[str]) -> dict[str, Selection]:
        # The league id is what GetSchedule needs; one call covers every requested league at once.
        leagues = {p[1] for sid in selection_ids if (p := self._parse_sid(sid))}
        if leagues:
            await self._ensure_schedule(leagues)
        return {sid: self._odds_cache[sid] for sid in selection_ids if sid in self._odds_cache}

    async def _ensure_schedule(self, leagues: set) -> None:
        fresh = (time.time() - self._sched_last_fetch < self._ttl) and leagues.issubset(self._sched_covered)
        if fresh:
            return
        async with self._fetch_lock:
            if (time.time() - self._sched_last_fetch < self._ttl) and leagues.issubset(self._sched_covered):
                return  # filled while we waited for the lock
            data = await self._fetch_schedule(sorted(leagues))
            self._sched_last_fetch = time.time()
            if not data:
                return
            now = time.time()
            for e in parse_schedule(data, pre_match_only=False).values():   # observe live too (telemetry)
                self._cache_entry(e, now)
            self._sched_covered = set(leagues)

    def _cache_entry(self, e: dict, now: float) -> None:
        """Fold one parse entry into the cache, keyed '<idgm>:<idlg>:side'."""
        idgm, idlg, side = e.get("idgm"), e.get("idlg"), e.get("side")
        if not idgm or not idlg:
            return
        full = f"{idgm}:{idlg}:{side}"
        if e["status"] == "open" and e["decimal_odds"]:
            self._odds_cache[full] = Selection(full, decimal_odds=float(e["decimal_odds"]),
                                               max_stake=float(e["max_stake"] or 0.0), status="open", ts=now)
        else:
            # suspended / missing → no usable price, so no arb can fire on it
            self._odds_cache[full] = Selection(full, decimal_odds=1.0, max_stake=0.0, status="suspended", ts=now)

    # ── HTTP via the browser page (carries session cookie + Cloudflare clearance) ─
    async def _post_json(self, url: str, body: dict, label: str) -> Optional[dict]:
        if not self._page:
            return None
        if not self._rtqname:
            print("[BOOKMAKER] no rtqname captured yet — click into any match in the window once.")
        try:
            res = await self._page.evaluate(_GAMEVIEW_JS, {"url": url, "rtqname": self._rtqname, "body": body})
        except Exception as ex:
            print(f"[BOOKMAKER] {label} fetch error: {ex}")
            return None
        status, text = res.get("status"), res.get("text") or ""
        if status != 200:
            print(f"[BOOKMAKER] {label} HTTP {status} (logged in? Cloudflare?) body[:120]={text[:120]!r}")
            return None
        try:
            data = json.loads(text)
        except ValueError:
            print(f"[BOOKMAKER] {label} non-JSON body[:120]={text[:120]!r}")
            return None
        # ASP.NET page methods sometimes wrap the payload in {"d": …}; unwrap if present.
        if isinstance(data, dict) and "d" in data and not ("GameView" in data or "Schedule" in data):
            d = data["d"]
            try:
                data = json.loads(d) if isinstance(d, str) else d
            except ValueError:
                return None
        return data

    async def _fetch_schedule(self, league_ids) -> Optional[dict]:
        return await self._post_json(SCHEDULE_URL, _schedule_body(league_ids), "GetSchedule")

    async def _fetch_gameview(self, game_id: str, league_id: str) -> Optional[dict]:
        return await self._post_json(GAMEVIEW_URL, _gameview_body(game_id, league_id),
                                     f"GetGameView gid={game_id}")

    # ── pairing catalog (bulk Schedule over the tennis-singles leagues) ─────────
    async def catalog(self) -> list[CatalogEntry]:
        leagues = [x.strip() for x in (os.environ.get("BOOKMAKER_CATALOG_LEAGUES")
                                       or DEFAULT_TENNIS_LEAGUES).split(",") if x.strip()]
        data = await self._fetch_schedule(leagues)
        if not data:
            return []
        out: list[CatalogEntry] = []
        for e in parse_schedule(data, pre_match_only=False).values():
            idgm, idlg = e.get("idgm"), e.get("idlg")
            if not idgm or not idlg:
                continue
            out.append(CatalogEntry(
                selection_id=f"{idgm}:{idlg}:{e['side']}",
                sport="TENNIS", league=str(idlg), event=e.get("event", ""),
                market="moneyline", selection_name=e.get("name", ""), start_time=e.get("start"),
            ))
        return out

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
        # Anti-detection: bookmaker.eu blocks API/XHR (GetGameView, schedule) from an automated Chromium even
        # though the page shell loads. Use the REAL installed Chrome and strip the automation flags so
        # navigator.webdriver / --enable-automation don't give us away.
        channel = os.environ.get("BOOKMAKER_CHANNEL", "chrome")
        launch = dict(user_data_dir=user_data, headless=headless,
                      viewport={"width": 1400, "height": 900},
                      args=["--disable-blink-features=AutomationControlled"],
                      ignore_default_args=["--enable-automation"])
        try:
            self._ctx = await self._pw.chromium.launch_persistent_context(channel=channel, **launch)
        except Exception as e:
            print(f"[BOOKMAKER] channel='{channel}' unavailable ({e}); using bundled Chromium "
                  "(more likely to be bot-blocked — install Chrome or set BOOKMAKER_CHANNEL).")
            self._ctx = await self._pw.chromium.launch_persistent_context(**launch)
        await self._ctx.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined});")
        self._ctx.on("request", self._on_request)     # capture rtqname from the site's own traffic
        self._ctx.on("response", self._on_response)   # capture the site's OWN GetGameView responses
        self._page = self._ctx.pages[0] if self._ctx.pages else await self._ctx.new_page()
        await self._page.goto(BASE_URL, wait_until="domcontentloaded")
        print(f"[BOOKMAKER] browser on {BASE_URL} (channel={channel}, headless={headless}). First, confirm the "
              "SITE works in the window — browse the schedule and open a game. If the schedule won't load, "
              "log in / clear Cloudflare; the profile remembers it.")

    # ── intercept the site's own GetGameView responses (most robust odds source) ─
    def _on_response(self, resp) -> None:
        try:
            if "getgameview" in resp.url.lower():
                asyncio.create_task(self._ingest_response(resp))
        except Exception:
            pass

    async def _ingest_response(self, resp) -> None:
        try:
            if resp.status != 200:
                return
            text = await resp.text()
            data = json.loads(text)
        except Exception:
            return
        if isinstance(data, dict) and "d" in data and "GameView" not in data:
            d = data["d"]
            try:
                data = json.loads(d) if isinstance(d, str) else d
            except ValueError:
                return
        now = time.time()
        captured = []
        for e in parse_gameview(data).values():
            idgm, idlg, side = e.get("idgm"), e.get("idlg"), e.get("side")
            if not idgm or not idlg:
                continue
            full = f"{idgm}:{idlg}:{side}"
            if e["status"] == "open" and e["decimal_odds"]:
                self._odds_cache[full] = Selection(full, float(e["decimal_odds"]),
                                                   float(e["max_stake"] or 0.0), "open", now)
                # only NOTE games we're NOT already polling directly (i.e. ones you navigated to) — our own
                # page.evaluate fetches also surface here and would just spam.
                if (idgm, idlg) not in self._last_fetch:
                    captured.append(f"{full}={e['decimal_odds']}")
            else:
                self._odds_cache[full] = Selection(full, 1.0, 0.0, "suspended", now)
        if captured:
            print(f"[BOOKMAKER] intercepted site GetGameView (navigated game) → {captured}")


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
            for _ in range(30):
                result = await a.odds(ids)
                print(f"[SMOKE] {time.strftime('%H:%M:%S')}  (cache={len(a._odds_cache)})")
                for sid in ids:
                    s = result.get(sid)
                    if s:
                        print(f"        {sid}  dec={s.decimal_odds}  implied={round(s.implied_price,4)}  "
                              f"max_contracts={round(s.max_contracts,1)}  {s.status}")
                    else:
                        print(f"        {sid}  (not returned)")
                # also surface anything captured by intercepting the site's own GetGameView (navigate a game)
                extra = {k: v for k, v in a._odds_cache.items() if k not in ids and v.status == "open"}
                for sid, s in list(extra.items())[:8]:
                    print(f"        [intercepted] {sid}  dec={s.decimal_odds}  "
                          f"implied={round(s.implied_price,4)}  {s.status}")
                await asyncio.sleep(10)
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
        finally:
            await a.shutdown()

    asyncio.run(_smoke())
