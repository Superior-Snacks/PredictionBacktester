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
from bookmaker_gameview import (parse_gameview, parse_leagues, parse_schedule,
                                _primary_ml_line, _is_tradeable, parse_max_stake, american_to_decimal)

BASE_URL = os.environ.get("BOOKMAKER_BASE_URL", "https://be.bookmaker.eu")
GAMEVIEW_URL = "https://be.bookmaker.eu/gateway/BetslipProxy.aspx/GetGameView"
SCHEDULE_URL = "https://be.bookmaker.eu/gateway/BetslipProxy.aspx/GetSchedule"
LEAGUES_URL  = "https://be.bookmaker.eu/gateway/BetslipProxy.aspx/GetLeagues"

# Catalog discovery is now DYNAMIC: GetLeagues lists every live league → we bulk-GetSchedule them. Filter
# to the match-winner sports we can pair (others = multi-runner/props, skipped). Override with
# BOOKMAKER_CATALOG_SPORTS="TENNIS,SOCCER,..."; or force explicit league ids with BOOKMAKER_CATALOG_LEAGUES.
DEFAULT_CATALOG_SPORTS = {"TENNIS", "SOCCER", "FIFA WORLD CUP", "BASEBALL", "BASKETBALL",
                          "FOOTBALL", "BOXING", "CRICKET", "MARTIAL ARTS", "AUSSIE RULES"}
SCHEDULE_CHUNK = 25   # leagues per GetSchedule call (keep the request/response a sane size)

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

# Multi: fire N POSTs CONCURRENTLY (Promise.all) from inside the page and return all results, in order.
# Used to split a big GetSchedule (many leagues) into smaller per-chunk requests that run in parallel —
# wall-time ≈ the slowest chunk, not the sum. One page, many concurrent fetches (no extra windows needed).
_SCHEDULE_MULTI_JS = """
async (arg) => {
  const headers = {
    'content-type': 'application/json',
    'accept': 'application/json, text/plain, */*',
    'cache-control': 'no-cache',
    'pragma': 'no-cache',
  };
  if (arg.rtqname) headers['rtqname'] = arg.rtqname;
  return await Promise.all(arg.bodies.map(async (b) => {
    try {
      const r = await fetch(arg.url, {
        method: 'POST', headers: headers, body: JSON.stringify(b), credentials: 'include',
      });
      return { status: r.status, text: await r.text() };
    } catch (e) {
      return { status: 0, text: '' };
    }
  }));
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
    many leagues. Returns every game in those leagues + their moneylines.

    LinkDeriv attaches EVERY derivative market (spreads/totals/props) to each game — the main reason a
    multi-league GetSchedule response is fat (multi-MB) and slow. We only read the moneyline right now, so
    BOOKMAKER_SCHEDULE_LINKDERIV=false drops all that bloat for a much smaller/faster response. Default
    stays 'true' (current behaviour); flip it back to true when the props phase actually needs derivatives."""
    ld = os.environ.get("BOOKMAKER_SCHEDULE_LINKDERIV", "true").strip().lower()
    link_deriv = "false" if ld in ("0", "false", "no", "off") else "true"
    return {"o": {"BORequestData": {"BOParameters": {
        "BORt": {}, "LeaguesIdList": ",".join(str(x) for x in league_ids), "LanguageId": "0",
        "LineStyle": "E", "ScheduleType": "american", "LinkDeriv": link_deriv,
    }}}}


def _leagues_body() -> dict:
    """GetLeagues POST body (captured) — the full league directory; no params beyond language."""
    return {"o": {"BORequestData": {"BOParameters": {"BORt": {}, "LanguageId": "0"}}}}


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
        # ── unattended session keep-alive + auto-recovery (Cloudflare / login expiry) ──
        self._recover_lock = asyncio.Lock()
        self._recover_cooldown = float(os.environ.get("BOOKMAKER_RECOVER_COOLDOWN_SEC", "45"))
        self._last_recover = 0.0
        self._recover_task: Optional[asyncio.Task] = None
        self._keepalive_task: Optional[asyncio.Task] = None
        self._autologin_fails = 0
        # ── observability / audit: capture raw bookmaker fields on change ──
        # BOOKMAKER_LIVE_DEBUG=1 → LIVE games only (lock diagnosis → live_debug_*.jsonl).
        # BOOKMAKER_AUDIT=1      → ALL main games (post-mortem arb-verification tape → quote_audit_*.jsonl).
        self._live_debug = os.environ.get("BOOKMAKER_LIVE_DEBUG") == "1"
        self._audit = os.environ.get("BOOKMAKER_AUDIT") == "1"
        self._debug_path = f"{'quote_audit' if self._audit else 'live_debug'}_{int(time.time())}.jsonl"
        self._debug_last_sig: dict = {}
        # ── background schedule refresh (decoupled from /odds) ──
        # A loop keeps the recently-requested leagues' cache warm so /odds is an instant cache read; the
        # GetSchedule fetch (which scales ~linearly with league count) runs CONCURRENTLY in chunks off the
        # request path. Knobs: BOOKMAKER_REFRESH_SEC (cycle), _CHUNK_LEAGUES (leagues per parallel request),
        # _ACTIVE_TTL_SEC (drop a league from the refresh set if not requested for this long).
        self._active_leagues: dict[str, float] = {}   # league_id -> unix ts last requested via /odds
        self._refresh_task: Optional[asyncio.Task] = None
        self._refresh_sec = float(os.environ.get("BOOKMAKER_REFRESH_SEC", "2"))
        self._active_ttl = float(os.environ.get("BOOKMAKER_ACTIVE_TTL_SEC", "120"))
        self._chunk_leagues = max(1, int(os.environ.get("BOOKMAKER_SCHEDULE_CHUNK_LEAGUES", "4")))
        self._last_refresh_log = 0.0                   # throttle the refresh heartbeat to ~every 30s
        self._refresh_log_verbose = os.environ.get("BOOKMAKER_REFRESH_LOG") == "1"  # log EVERY cycle (tuning)
        self._rate_limit_total = 0                     # cumulative 429s on GetSchedule (rate-limit watch)

    # ── session lifecycle ──────────────────────────────────────────────────────
    async def startup(self) -> None:
        await self._start_browser()
        self._keepalive_task = asyncio.create_task(self._keepalive_loop())
        self._refresh_task = asyncio.create_task(self._refresh_loop())
        print("[BOOKMAKER] ready. Odds via BACKGROUND GetSchedule refresh (selection id = '<GameId>:<LeagueId>:H|V').\n"
              "[BOOKMAKER] >>> click into ANY match in the window once: it seeds the 'rtqname' session header "
              "and sets a deep referer, both of which the BetslipProxy calls require.\n"
              f"[BOOKMAKER] refresh every {self._refresh_sec:g}s ({self._chunk_leagues} leagues/chunk, concurrent); "
              "keep-alive + auto-recovery armed.")

    async def shutdown(self) -> None:
        for task in (self._keepalive_task, self._recover_task, self._refresh_task):
            if task and not task.done():
                task.cancel()
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

    # ── unattended keep-alive + auto-recovery ───────────────────────────────────
    def _trigger_recovery(self, reason: str) -> None:
        """Fire-and-forget a session reload (deduped + cooldown-guarded) so the failing odds call returns
        immediately; the NEXT poll benefits once recovery completes. Non-blocking by design."""
        if self._recover_task and not self._recover_task.done():
            return
        self._recover_task = asyncio.create_task(self._recover(reason))

    async def _recover(self, reason: str) -> None:
        """Reload be.bookmaker.eu in the real browser so Cloudflare's MANAGED challenge re-clears and the
        session cookies refresh; rtqname re-captures from the site's own traffic on navigation. NOTE: an
        INTERACTIVE challenge (checkbox/captcha) cannot be auto-solved — that still needs a human (VNC)."""
        if time.time() - self._last_recover < self._recover_cooldown:
            return
        async with self._recover_lock:
            if time.time() - self._last_recover < self._recover_cooldown:
                return
            self._last_recover = time.time()
            if not self._page:
                return
            print(f"[BOOKMAKER] session recovery: reloading {BASE_URL} ({reason})…")
            try:
                await self._page.goto(BASE_URL, wait_until="domcontentloaded", timeout=60_000)
                # let real Chrome run Cloudflare's JS managed challenge + the SPA's own API calls (which
                # re-emit the rtqname header we sniff in _on_request)
                await self._page.wait_for_timeout(
                    int(float(os.environ.get("BOOKMAKER_RECOVER_WAIT_SEC", "8")) * 1000))
                # Distinguish a LOGIN-session expiry (redirect to the login form) from a Cloudflare challenge —
                # recovery can clear Cloudflare but CANNOT re-enter credentials.
                if await self._looks_like_login():
                    print("[BOOKMAKER] *** LOGIN REQUIRED *** — the ACCOUNT session expired (page is on the "
                          "login form). Auto-recovery clears Cloudflare but cannot log in. Log in once in the "
                          "window / via VNC, or set BOOKMAKER_USERNAME + BOOKMAKER_PASSWORD for auto-login.")
                    await self._try_auto_login()
                    return
                ok = "ok" if self._rtqname else "NOT captured — may need a manual click / VNC"
                print(f"[BOOKMAKER] session recovery done (rtqname {ok}).")
            except Exception as ex:
                print(f"[BOOKMAKER] session recovery failed: {ex}")

    async def _looks_like_login(self) -> bool:
        """Heuristic: are we sitting on the login page (account session expired, not just a CF challenge)?
        Checks for a VISIBLE password input — bookmaker.eu keeps a header login widget AND a full login-page
        form in the DOM, so 'any password input exists' is too loose; visibility = a login form is on screen."""
        if not self._page:
            return False
        try:
            if "login" in (self._page.url or "").lower():
                return True
            return await self._first_visible("input[type=password]") is not None
        except Exception:
            return False

    async def _first_visible(self, selector: str):
        """First VISIBLE element matching `selector`. bookmaker.eu renders TWO login forms (the top-right
        header widget and the full app-login-page), and BOTH carry id/name 'account'/'password' + a submit
        button — so a plain query_selector hits whichever is first in the DOM, not necessarily the one on
        screen. We must act on the visible one (and submit ITS form), or the keystrokes/click go nowhere."""
        if not self._page:
            return None
        for el in await self._page.query_selector_all(selector):
            try:
                if await el.is_visible():
                    return el
            except Exception:
                pass
        return None

    async def _try_auto_login(self) -> None:
        """Fallback re-login if the inactivity keep-alive failed and we got logged out anyway. bookmaker.eu's
        form is `input[name=account]` + `input[name=password]`, submitted by pressing Enter (the manual
        action). Two opt-in modes (default off, so we never submit unprompted):
          - BOOKMAKER_AUTOLOGIN=1 → rely on Chrome's SAVED autofill (the profile remembers the login): click
            the fields to commit autofill, then press Enter. No credentials stored anywhere.
          - BOOKMAKER_USERNAME + BOOKMAKER_PASSWORD → type them explicitly.
        Selectors overridable via BOOKMAKER_LOGIN_USER_SEL / _PASS_SEL / _SUBMIT_SEL. DISABLES after 3 fails
        (lockout guard). No CAPTCHA/2FA seen on this form; if one appears it'll just fail and stop."""
        autologin = os.environ.get("BOOKMAKER_AUTOLOGIN") == "1"
        user = os.environ.get("BOOKMAKER_USERNAME")
        pw = os.environ.get("BOOKMAKER_PASSWORD")
        if not self._page or not (autologin or (user and pw)):
            return
        if self._autologin_fails >= 3:
            print("[BOOKMAKER] auto-login disabled after 3 failures (avoiding lockout) — log in manually.")
            return
        acc_sel = os.environ.get("BOOKMAKER_LOGIN_USER_SEL", "input[name=account]")
        pass_sel = os.environ.get("BOOKMAKER_LOGIN_PASS_SEL", "input[name=password]")
        submit_sel = os.environ.get("BOOKMAKER_LOGIN_SUBMIT_SEL")   # unset → click the visible form's submit button
        try:
            acc = await self._first_visible(acc_sel)
            pwd = await self._first_visible(pass_sel)
            if not pwd:
                print("[BOOKMAKER] auto-login: no visible password field — manual login needed.")
                return
            if user and pw:
                if acc:
                    await acc.fill(user)     # fill() fires the input/change events Angular's reactive form needs
                await pwd.fill(pw)
            else:
                # rely on Chrome's saved autofill: click the fields to commit it before submitting
                if acc:
                    await acc.click()
                await pwd.click()
            # Submit. This is an Angular (ngSubmit) form, so a real submit event must fire on the SAME form we
            # just filled: click that form's own submit button — not a bare Enter (Angular may ignore it) and
            # not some other form's button. Fall back to any visible submit button, then to Enter.
            clicked = False
            if submit_sel:
                btn = await self._first_visible(submit_sel)
                if btn:
                    await btn.click(); clicked = True
            if not clicked:
                try:
                    form = (await pwd.evaluate_handle("el => el.closest('form')")).as_element()
                    btn = await form.query_selector("button[type=submit]") if form else None
                except Exception:
                    btn = None
                if not btn:
                    btn = await self._first_visible("button[type=submit]")
                if btn:
                    await btn.click(); clicked = True
            if not clicked:
                await pwd.press("Enter")
            # Poll up to ~12s for the login form to clear (a fixed sleep is too short on a slow server Chrome).
            for _ in range(24):
                await self._page.wait_for_timeout(500)
                if not await self._looks_like_login():
                    break
            if await self._looks_like_login():
                self._autologin_fails += 1
                print(f"[BOOKMAKER] auto-login failed ({self._autologin_fails}/3) — wrong creds / captcha / "
                      "selector? set BOOKMAKER_USERNAME/PASSWORD (+ _LOGIN_*_SEL) or log in manually via VNC.")
            else:
                self._autologin_fails = 0
                print("[BOOKMAKER] auto-login succeeded — session restored.")
        except Exception as ex:
            self._autologin_fails += 1
            print(f"[BOOKMAKER] auto-login error ({self._autologin_fails}/3): {ex}")

    async def _keepalive_loop(self) -> None:
        """Keep the session alive. bookmaker.eu logs out on UI INACTIVITY (an Angular timer that listens to
        mouse/keyboard, NOT our background odds XHR — which is why constant polling still got logged out). So
        each tick we (1) generate a trusted UI gesture to reset that timer, (2) click 'Stay connected' if the
        expiry modal is already up, (3) ping same-origin to keep Cloudflare warm + detect a hard logout.
        Interval via BOOKMAKER_KEEPALIVE_SEC (default 180; keep it well under the site's inactivity timeout)."""
        interval = float(os.environ.get("BOOKMAKER_KEEPALIVE_SEC", "180"))
        while True:
            try:
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                break
            if not self._page:
                continue
            # 1) trusted UI activity — resets the client-side inactivity timer (the actual fix)
            try:
                await self._page.mouse.move(5, 5)
                await self._page.mouse.move(60, 60)
            except Exception:
                pass
            # 2) if the "session about to expire" modal is showing, keep us connected
            try:
                btn = await self._page.query_selector("#modalLogout .btn-green")
                if btn and await btn.is_visible():
                    await btn.click()
                    print("[BOOKMAKER] dismissed inactivity logout ('Stay connected').")
            except Exception:
                pass
            # 3) keep Cloudflare/session warm + detect a hard logout
            try:
                status = await self._page.evaluate(
                    "() => fetch('/', {credentials:'include', cache:'no-store'}).then(r => r.status).catch(() => 0)")
            except Exception:
                status = 0
            if not (isinstance(status, int) and 200 <= status < 400):
                self._trigger_recovery(f"keepalive status={status}")

    # ── odds (bulk: ONE GetSchedule covers all requested leagues) ───────────────
    @staticmethod
    def _parse_sid(sid: str):
        parts = sid.split(":")
        if len(parts) == 3 and parts[0] and parts[1] and parts[2] in ("H", "V", "D"):
            return parts[0], parts[1], parts[2]   # (game_id, league_id, side); D = draw (3-way)
        return None

    async def odds(self, selection_ids: list[str]) -> dict[str, Selection]:
        # Decoupled: just RECORD which leagues are wanted (the background _refresh_loop keeps them warm) and
        # return the cached selections immediately. No GetSchedule on the request path — /odds is an instant
        # cache read. A brand-new league fills in on the next refresh cycle (≤ BOOKMAKER_REFRESH_SEC).
        now = time.time()
        for sid in selection_ids:
            if (p := self._parse_sid(sid)):
                self._active_leagues[p[1]] = now
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
            if self._live_debug or self._audit:
                self._capture_audit(data)
            now = time.time()
            for e in parse_schedule(data, pre_match_only=False).values():   # observe live too (telemetry)
                self._cache_entry(e, now)
            self._sched_covered = set(leagues)

    # ── background refresh (decoupled from /odds; concurrent chunked GetSchedule) ──
    async def _refresh_loop(self) -> None:
        """Keep the cache warm for leagues requested via /odds in the last BOOKMAKER_ACTIVE_TTL_SEC. Each
        cycle fetches them CONCURRENTLY in chunks (≈ slowest-chunk wall-time, not sum), so /odds never blocks
        on a fetch. Robust: any error is logged and the loop keeps going (a dead loop = permanently stale)."""
        while True:
            try:
                await asyncio.sleep(self._refresh_sec)
            except asyncio.CancelledError:
                break
            now = time.time()
            leagues = {lg for lg, ts in self._active_leagues.items() if now - ts <= self._active_ttl}
            if not leagues:
                continue
            try:
                t0 = time.perf_counter()
                n_chunks = await self._refresh_schedule_concurrent(leagues)
                dt = time.perf_counter() - t0
                now2 = time.time()
                if self._refresh_log_verbose or dt > self._refresh_sec or now2 - self._last_refresh_log >= 30:
                    self._last_refresh_log = now2
                    slow = " — SLOWER than the refresh interval (can't keep up; raise it or trim leagues)" \
                        if dt > self._refresh_sec else ""
                    rl = f"  rate-limited 429s: {self._rate_limit_total}" if self._rate_limit_total else ""
                    print(f"[BOOKMAKER] refresh: {len(leagues)} leagues / {n_chunks} chunks in {dt:.2f}s "
                          f"(cache={len(self._odds_cache)} sel){slow}{rl}")
            except Exception as ex:
                print(f"[BOOKMAKER] background refresh error: {type(ex).__name__}: {ex}")

    async def _refresh_schedule_concurrent(self, leagues: set) -> int:
        """Fetch the given leagues' schedules in concurrent chunks and fold them into the odds cache.
        Returns the number of chunks issued (for the heartbeat log)."""
        ordered = sorted(leagues)
        chunks = [ordered[i:i + self._chunk_leagues] for i in range(0, len(ordered), self._chunk_leagues)]
        results = await self._post_schedule_multi([_schedule_body(c) for c in chunks])
        if not results:
            return len(chunks)
        now, parsed_any = time.time(), False
        for data in results:
            if not data:
                continue
            if self._live_debug or self._audit:
                self._capture_audit(data)
            for e in parse_schedule(data, pre_match_only=False).values():   # observe live too (telemetry)
                self._cache_entry(e, now)
            parsed_any = True
        if parsed_any:
            self._sched_last_fetch = now
            self._sched_covered = set(ordered)
        return len(chunks)

    async def _post_schedule_multi(self, bodies: list[dict]) -> list[Optional[dict]]:
        """One page.evaluate that fires all GetSchedule chunk POSTs CONCURRENTLY (Promise.all). Returns one
        parsed payload (or None) per body, in order. Logs every cycle's chunk failures by status code, and:
          - 429 (RATE LIMITED) → logged loudly + counted, but does NOT reload the session (429 = slow down,
            not a dead session; reloading would only add load). Back off via the chunk/refresh env knobs.
          - 401/403/5xx (real session/server failure) → triggers session recovery, as _post_json does."""
        if not self._page or not bodies:
            return []
        if not self._rtqname:
            print("[BOOKMAKER] no rtqname captured yet — click into any match in the window once.")
        try:
            raw = await self._page.evaluate(
                _SCHEDULE_MULTI_JS, {"url": SCHEDULE_URL, "rtqname": self._rtqname, "bodies": bodies})
        except Exception as ex:
            print(f"[BOOKMAKER] GetSchedule(multi) fetch error: {ex}")
            self._trigger_recovery("GetSchedule(multi) evaluate error")
            return []
        out: list[Optional[dict]] = []
        statuses: dict = {}            # non-200 status_code -> count, this cycle
        for res in raw or []:
            status, text = (res or {}).get("status"), (res or {}).get("text") or ""
            if status != 200:
                statuses[status] = statuses.get(status, 0) + 1
                out.append(None)
                continue
            out.append(self._parse_body(text))
        if statuses:
            n_fail = sum(statuses.values())
            print(f"[BOOKMAKER] GetSchedule(multi): {n_fail}/{len(bodies)} chunk(s) failed, statuses={statuses}")
            if 429 in statuses:
                self._rate_limit_total += statuses[429]
                print(f"[BOOKMAKER] *** RATE LIMITED *** 429 ×{statuses[429]} this cycle "
                      f"(cumulative {self._rate_limit_total}) — back off: RAISE BOOKMAKER_SCHEDULE_CHUNK_LEAGUES "
                      "(fewer concurrent requests) and/or BOOKMAKER_REFRESH_SEC (slower cadence). NOT reloading "
                      "the session (429 = slow down, not a dead session).")
            if any(s in (401, 403) or (isinstance(s, int) and s >= 500) for s in statuses):
                print("[BOOKMAKER] GetSchedule(multi): auth/5xx among the failures — triggering session recovery.")
                self._trigger_recovery("GetSchedule(multi) auth/5xx")
        return out

    @staticmethod
    def _parse_body(text: str) -> Optional[dict]:
        """Parse a BetslipProxy response body, unwrapping the ASP.NET {"d": …} envelope if present."""
        try:
            data = json.loads(text)
        except ValueError:
            return None
        if isinstance(data, dict) and "d" in data and not ("GameView" in data or "Schedule" in data):
            d = data["d"]
            try:
                data = json.loads(d) if isinstance(d, str) else d
            except ValueError:
                return None
        return data

    def _capture_audit(self, data: dict) -> None:
        """Raw bookmaker TAPE for post-mortem arb verification (+ lock diagnosis). On change, per main game,
        records the fields needed to independently re-check any arb: teams (mispair), tradeable + MoneyLineStatus
        + freeze + line_present (lock/suspend), implied prices (re-derive net), max_stake (real depth), and
        idgm/idlg + full raw game/line scalars (catch unknown mechanisms). BOOKMAKER_AUDIT=1 = all games;
        BOOKMAKER_LIVE_DEBUG=1 = live only. Cross-check vs CrossArbTelemetry_*.csv with verify_arbs.py."""
        scalar = (str, int, float, bool, type(None))
        leagues = (((data or {}).get("Schedule") or {}).get("Data") or {}).get("Leagues") or {}
        for league in leagues.get("League") or []:
            for dg in league.get("dateGroup") or []:
                for g in dg.get("game") or []:
                    idgm = g.get("idgm")
                    if not idgm or idgm != g.get("famGame"):
                        continue
                    if not self._audit and not g.get("LiveGame"):
                        continue   # live-debug mode captures live games only
                    line = _primary_ml_line(g) or {}
                    gkeys = {k: v for k, v in g.items() if isinstance(v, scalar)}
                    lkeys = {k: v for k, v in line.items() if isinstance(v, scalar)}
                    sig = json.dumps({"g": gkeys, "l": lkeys}, sort_keys=True, default=str)
                    if self._debug_last_sig.get(idgm) == sig:
                        continue   # unchanged since last poll — log transitions only
                    self._debug_last_sig[idgm] = sig
                    dh, dv = american_to_decimal(line.get("hoddst")), american_to_decimal(line.get("voddst"))
                    rec = {"ts": round(time.time(), 3), "idgm": idgm, "idlg": g.get("idlg"),
                           "event": f"{g.get('htm','')} vs {g.get('vtm','')}",
                           "htm": g.get("htm", ""), "vtm": g.get("vtm", ""),
                           "live": bool(g.get("LiveGame", False)), "tradeable": _is_tradeable(g),
                           "line_present": bool(line),
                           "implied_h": round(1.0 / dh, 6) if dh else None,
                           "implied_v": round(1.0 / dv, 6) if dv else None,
                           "max_stake": parse_max_stake(g.get("descgmtyp")),
                           "game": gkeys, "line": lkeys}
                    try:
                        with open(self._debug_path, "a", encoding="utf-8") as f:
                            f.write(json.dumps(rec, default=str) + "\n")
                    except Exception:
                        pass
                    if g.get("LiveGame"):
                        print(f"[BOOKMAKER LIVE] {rec['event']}  tradeable={rec['tradeable']}  "
                              f"MLS={g.get('MoneyLineStatus')} Freeze={g.get('FreezeMoneyLine')} "
                              f"line={'Y' if line else 'GONE'}  odds={line.get('hoddst')}/{line.get('voddst')}")

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
            self._trigger_recovery(f"{label} evaluate error")
            return None
        status, text = res.get("status"), res.get("text") or ""
        if status != 200:
            print(f"[BOOKMAKER] {label} HTTP {status} (logged in? Cloudflare?) body[:120]={text[:120]!r}")
            if status in (401, 403, 429) or (isinstance(status, int) and status >= 500):
                self._trigger_recovery(f"{label} HTTP {status}")
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

    async def _fetch_leagues(self) -> Optional[dict]:
        return await self._post_json(LEAGUES_URL, _leagues_body(), "GetLeagues")

    # ── pairing catalog (DYNAMIC: GetLeagues → bulk Schedule over all match leagues) ─
    async def _discover_league_map(self) -> dict[str, str]:
        """{ league_id: sport } for every match-winner league. Override the sport filter with
        BOOKMAKER_CATALOG_SPORTS, or skip discovery entirely with explicit BOOKMAKER_CATALOG_LEAGUES."""
        forced = os.environ.get("BOOKMAKER_CATALOG_LEAGUES")
        if forced:
            return {x.strip(): "" for x in forced.split(",") if x.strip()}
        data = await self._fetch_leagues()
        if not data:
            return {}
        sports_env = os.environ.get("BOOKMAKER_CATALOG_SPORTS")
        sports = {s.strip().upper() for s in sports_env.split(",")} if sports_env else DEFAULT_CATALOG_SPORTS
        return {lg["id"]: lg["sport"] for lg in parse_leagues(data, sports=sports) if lg["game_count"] > 0}

    async def catalog(self) -> list[CatalogEntry]:
        lid_to_sport = await self._discover_league_map()
        if not lid_to_sport:
            return []
        league_ids = list(lid_to_sport.keys())
        out: list[CatalogEntry] = []
        for i in range(0, len(league_ids), SCHEDULE_CHUNK):
            data = await self._fetch_schedule(league_ids[i:i + SCHEDULE_CHUNK])
            if not data:
                continue
            for e in parse_schedule(data, pre_match_only=False).values():
                idgm, idlg = e.get("idgm"), e.get("idlg")
                if not idgm or not idlg:
                    continue
                out.append(CatalogEntry(
                    selection_id=f"{idgm}:{idlg}:{e['side']}",
                    sport=lid_to_sport.get(str(idlg)) or e.get("category", "") or "",
                    league=str(idlg), event=e.get("event", ""), market="moneyline",
                    selection_name=e.get("name", ""), start_time=e.get("start"),
                    three_way=bool(e.get("three_way")),
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
