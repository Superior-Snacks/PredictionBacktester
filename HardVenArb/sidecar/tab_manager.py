"""
tab_manager.py — LEAGUE TAB MANAGER for the browser-WS reader.

WHY: the browser's odds WS is subscription-follows-the-page. The main board (topic sp/{sport}) carries the
sport's TODAY slate — MEASURED 2026-08-06: 12 leagues streaming while only 2 were rendered, so the list's
virtualisation limits RENDERING, not the subscription (an earlier "~25% board coverage" note was an artifact
of measuring a CHANGES-ONLY feed over a short TTL). A LEAGUE page (topic lg/{lid}) streams that league's whole
slate, but subscriptions DON'T accumulate — navigating drops the old league, so one tab = one league. Tabs are
therefore needed for paired leagues the board does NOT carry: in practice the ones whose games are on a LATER
day. (Background tabs stay alive — confirmed 2026-07-16 — so N tabs is viable.)

WHAT: every tick, read the paired leagues (+ their URLs) from cross_pairs.json, ask the reader which leagues
it's actually delivering (reader_live_mids), and open ONE tab per tick for a GAP league (paired but not being
fed) — up to HARDVEN_TAB_MAX. Tabs for leagues that drop out of the pairing (settled / de-paired) are closed.
Keyed off the reader's actually-delivered matchups so it never opens a tab for a league the board already
covers, and never double-opens (a league we have a tab for, or that's being fed, is not a gap).

The GAP model is push-based, so a board-covered league that goes QUIET for > HARDVEN_TAB_COVER_TTL looks like
a gap and may get its own tab — harmless (capped), and it guarantees live CHANGES keep flowing for it.

DEDICATED-TAB ELIGIBILITY (the HOT model): a league deserves a persistent tab only while it has PRE-LIVE
games starting within HARDVEN_TAB_HOT_HOURS (8). Ranking = number of such hot games, most first (tiebreak:
soonest start). A league whose games are all tomorrow — or all already started — is NOT tab-worthy: the
ROVING tail tab sweeps it instead, and it gets promoted the moment it develops hot games. Leagues the
FEATURED BOARD streams are never tabbed (the board carries the whole league), and a dedicated tab whose
league LATER joins the board, loses its last hot game, or gets out-ranked at the periodic re-rank is closed
so the slot covers something that needs it.

ENABLE: HARDVEN_TAB_MANAGER=1 (only meaningful with PINNACLE_WINDOW_WS_READ=1 + a browser session). Knobs:
  HARDVEN_TAB_MAX            (12)  max concurrent manager tabs — the coverage-vs-machine-load ceiling
  HARDVEN_TAB_INTERVAL_SEC  (20)  tick period; also the pacing (≤1 tab opened per tick, organic)
  HARDVEN_TAB_COVER_TTL     (240) a league counts as covered if a matchup pushed within this many seconds
  HARDVEN_TAB_START_DELAY_SEC (45) delay before the first tick (let the board + first pairing settle)
  HARDVEN_TAB_HOT_HOURS      (8)  a game is HOT when it starts within this many hours (and hasn't started)
  HARDVEN_TAB_RESET_MIN      (60) periodic re-rank: close tabs no longer in the top-N hot ranking
  HARDVEN_TAB_EVICT_GRACE_SEC (180) a freshly-opened tab can't be hot-evicted for this long (verify tabs)

Introspection: status() → what every tab is showing vs what it SHOULD show (served on /debug/tabs).
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional


def _dead(pg) -> bool:
    """True if this page is gone (operator closed it, renderer crashed, browser cycled).

    Checked EVERY tick because a closed tab is the one failure that is invisible and harmful: the league
    stays in `_tabs`, so it is excluded from gap selection and looks covered, while nothing streams for it.
    A silent coverage hole is worse than a redundant tab."""
    try:
        return pg is None or pg.is_closed()
    except Exception:
        return True


def _same_page(url: str | None) -> str:
    """Canonical form for 'is this tab still on its league page'. The FRAGMENT must be stripped: Pinnacle
    appends '#period:0' once the board renders, so a tab that never moved compares unequal to its own URL —
    which made the off-station check re-navigate every tab on every tick, churning them and dropping each
    league's WS subscription. The query string goes too (tracking params)."""
    if not url:
        return ""
    return url.split("#")[0].split("?")[0].rstrip("/").lower()


class LeagueTabManager:
    def __init__(self, session, live_mids_fn: Callable[[float], list], pairs_path: str,
                 board_lids_fn: Optional[Callable[[], set]] = None,
                 board_dom_fn: Optional[Callable] = None) -> None:
        self._session = session                  # PinnacleBrowserSession (open_tab / close_tab)
        self._live_mids = live_mids_fn           # callable(ttl) -> list['lid:mid'] the reader delivered
        self._board_lids = board_lids_fn         # callable() -> set(lid) the FEATURED BOARD streams (sp/ topics)
        # async callable() -> set(matchup_id) the board is RENDERING. Pinnacle's board list is VIRTUALISED
        # (~13 of 55 rows in the DOM), and only rendered rows stream — so board coverage is a per-MATCHUP
        # fact, not per-league. A league counts as board-covered only when every one of its HOT games is
        # rendered; otherwise the uncovered ones need a tab. Refreshed once per tick into _board_mids.
        self._board_dom_fn = board_dom_fn
        # NOTE: never populated today — no per-matchup board-render feed exists yet, so this stays
        # empty and contributes nothing to coverage. Kept because the docstring above describes the
        # intended source; treat a non-empty value as an upgrade, not as something to rely on.
        self._board_mids: set = set()
        self._board_scanned: set = set()      # leagues enumerated by the periodic scroll scan
        self._board_dom: set = set()          # leagues judged board-covered this tick
        # Treat "has a game today" as board coverage (the board IS the sport's Today list and its sport topic
        # carries all of it). Only applied while the board is demonstrably streaming, so a dead board can
        # never make everything look covered. HARDVEN_BOARD_COVERS_TODAY=0 reverts to push-only coverage.
        self._board_covers_today = os.environ.get("HARDVEN_BOARD_COVERS_TODAY", "1") != "0"
        self._pairs_path = pairs_path
        self._tabs: dict[str, object] = {}       # leagueId -> page (tabs THIS manager opened)
        self._max = int(os.environ.get("HARDVEN_TAB_MAX", "12"))
        self._interval = float(os.environ.get("HARDVEN_TAB_INTERVAL_SEC", "20"))
        self._cover_ttl = float(os.environ.get("HARDVEN_TAB_COVER_TTL", "240"))
        self._start_delay = float(os.environ.get("HARDVEN_TAB_START_DELAY_SEC", "45"))
        self._task: Optional[asyncio.Task] = None
        self._last_log = 0.0
        self._cap_warned = False
        # ROVING TAIL TAB: one extra tab beyond the `_max` dedicated tabs that SWEEPS the overflow tail (paired
        # leagues the dedicated tabs + board don't cover), re-pointing itself league→league every dwell. Gives the
        # tail opportunistic live-WS touches AND makes the browser actually visit those leagues (so the authed
        # re-seed to them reads as organic browsing, not API-only). Off with HARDVEN_TAB_ROVE=0.
        self._rove_enabled = os.environ.get("HARDVEN_TAB_ROVE", "1") != "0"
        self._rove_dwell = float(os.environ.get("HARDVEN_ROVE_DWELL_SEC", "20"))
        self._rove_page = None
        self._rove_lid: Optional[str] = None
        self._rove_cursor = 0
        self._last_rove = 0.0
        self._league_start: dict[str, float] = {}   # lid -> soonest game start ts (ranks which gaps get tabs)
        self._held = False                          # frozen during a bet: don't open/close/navigate tabs
        # RECLAIM: a dedicated tab whose league LATER appears on the featured board is redundant (the board now
        # covers it). Close it once it's been board-covered continuously for this long (sustained, not a blip
        # from the primary page glancing at another sport) so the slot can cover a still-uncovered league.
        self._board_reclaim_sec = float(os.environ.get("HARDVEN_TAB_BOARD_RECLAIM_SEC", "120"))
        self._tab_board_since: dict[str, float] = {}   # lid -> when its tab's league first went continuously board
        # PER-TAB KEEPALIVE: dedicated tabs are opened once and then just SIT — unlike the main board (reloaded
        # every PINNACLE_RELOGIN_MIN) and the rove tab (navigates every dwell), nothing re-auths them, so they hit
        # Pinnacle's ~30min idle logout (seen as a mass authed-REST guest-redirect + a re-seed returning 0 tokens).
        # Track each dedicated tab's last refresh; reload the stalest one past this age (well under the logout
        # window). A reload = navigate to its own league URL = re-auth the tab AND re-subscribe its odds WS.
        self._keepalive_sec = float(os.environ.get("HARDVEN_TAB_KEEPALIVE_MIN", "15")) * 60.0
        self._tab_alive: dict[str, float] = {}         # lid -> last time its dedicated tab was opened/reloaded
        # HOT model: only games starting within this window (and not yet started) make a league tab-worthy.
        self._hot_hours = float(os.environ.get("HARDVEN_TAB_HOT_HOURS", "8"))
        # Periodic re-rank ("the hourly full reset", as a reconcile): close tabs no longer in the top-N hot
        # ranking. Continuous checks (de-pair / board / hot=0) fire every tick; this one is deliberately slow
        # so a league flapping between rank N and N+1 doesn't churn tabs.
        self._reset_sec = float(os.environ.get("HARDVEN_TAB_RESET_MIN", "60")) * 60.0
        self._last_reset = time.time()
        # A tab opened moments ago (e.g. by request_verify for an arb check) must not be hot-evicted before
        # it has done its job.
        self._evict_grace = float(os.environ.get("HARDVEN_TAB_EVICT_GRACE_SEC", "180"))
        self._league_games: dict[str, dict] = {}       # lid -> {mid: (start_ts|None, precise: bool)}

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self.run())
            rove = f" + 1 roving tail tab ({self._rove_dwell:g}s/league)" if self._rove_enabled else ""
            print(f"[TAB-MGR] league tab manager ON - {self._max} dedicated gap tabs{rove} "
                  f"(tick {self._interval:g}s, cover-ttl {self._cover_ttl:g}s).")

    async def stop(self) -> None:
        """Cancel the loop and drop every tab. Tab closing is best-effort: under the lifecycle this runs AFTER
        the browser has already been stopped, so the pages are dead and closing them will throw — that must
        not stop us clearing state, or the next window would inherit handles to a browser that no longer exists."""
        if self._task and not self._task.done():
            self._task.cancel()
        self._task = None
        for lid, pg in list(self._tabs.items()):
            try:
                await self._session.close_tab(pg)
            except Exception:
                pass
        self._tabs.clear()
        self._tab_board_since.clear()
        self._tab_alive.clear()
        if self._rove_page is not None:
            try:
                await self._session.close_tab(self._rove_page)
            except Exception:
                pass
        self._rove_page = None
        self._rove_lid = None

    async def run(self) -> None:
        try:
            await asyncio.sleep(self._start_delay)       # let the board + first pairing settle
        except asyncio.CancelledError:
            return
        while True:
            try:
                await asyncio.sleep(self._interval)
            except asyncio.CancelledError:
                break
            try:
                await self._tick()
            except Exception as ex:
                print(f"[TAB-MGR] tick error: {type(ex).__name__}: {ex}")

    def covered_lids(self) -> set:
        """Leagues under live WS coverage RIGHT NOW — the dedicated tabs plus the roving tab's current league.
        Used to tag /odds prices as WS-verified vs screening-only for verify-on-detection."""
        lids = set(self._tabs.keys())
        if self._rove_lid:
            lids.add(self._rove_lid)
        return lids

    # ── betting integration ───────────────────────────────────────────────────
    def hold(self, on: bool) -> None:
        """Freeze tab churn during a bet. While held, `_tick` opens/closes/navigates nothing — so a tab the
        executor is placing on can't be re-pointed or closed out from under the bet. Released after."""
        self._held = bool(on)

    def page_for_lid(self, lid: str):
        """The already-open tab showing league `lid`, if any → (page, kind). A dedicated tab or the roving tail
        when it currently sits on `lid`. Lets the executor bet on the tab that already has the arb (natural: a
        user bets on the league they're watching) instead of a cold hidden tab. (None, None) if not covered."""
        lid = str(lid)
        pg = self._tabs.get(lid)
        if pg is not None:
            return pg, "dedicated"
        if self._rove_lid == lid and self._rove_page is not None:
            return self._rove_page, "rove"
        return None, None

    def reader_tabs(self) -> list:
        """[(page, lid|None), …] for every live reader tab — the per-tab organic loop rotates over these."""
        out: list = [(pg, lid) for lid, pg in self._tabs.items()]
        if self._rove_page is not None:
            out.append((self._rove_page, self._rove_lid))
        return out

    async def acquire_rove_for_bet(self, url: str, lid: Optional[str] = None):
        """Point the roving tail tab at `url` to place a bet — the fallback when no tab holds the league (the
        user's 'use the last tab to navigate and bet'). Call `hold(True)` first so the sweep won't fight it; the
        rove resumes sweeping the tail after `hold(False)`. Returns the page, or None if roving is disabled/failed."""
        if not self._rove_enabled:
            return None
        if self._rove_page is None:
            pg = await self._session.open_tab(url)
            if pg is None:
                return None
            self._rove_page = pg
        else:
            # Cosmetic raise (see HARDVEN_ORGANIC_FOCUS in organic.py): focus emulation already reports the page
            # visible+focused to the site, and navigate_tab works on a background tab — so skipping this only
            # stops the taskbar flashing when the window sits on another Windows virtual desktop.
            if os.environ.get("HARDVEN_ORGANIC_FOCUS", "1") != "0":
                try:
                    await self._rove_page.bring_to_front()
                except Exception:
                    pass
            if not await self._session.navigate_tab(self._rove_page, url):
                self._rove_page = None
                self._rove_lid = None
                return None
        # Record WHICH league we parked on. `covered_lids()` reads `_rove_lid`, so leaving it None told the rest
        # of the system this tab covers nothing — which made verify-on-demand wait forever for a WS delta that a
        # STABLE PRE-MATCH line never sends (the WS pushes changes, not heartbeats). Setting it is also more
        # correct for `page_for_lid` (the bet path then finds this tab) and for gap selection (won't re-open it).
        self._rove_lid = str(lid) if lid else None
        self._last_rove = time.time()
        return self._rove_page

    def _covered_now(self) -> set:
        """Leagues already fed → NOT gaps (so we never open a dedicated tab for them): the reader's recent pushes
        (board / dedicated tabs / rove) UNION the FEATURED-BOARD leagues (sport-level topics, generous TTL so a
        briefly-quiet featured league isn't redundantly re-tabbed)."""
        now = time.time()
        # Delivered set, PER MATCHUP. `_live_mids` yields 'lid:mid'; `_board_mids` is bare matchup ids.
        live_keys = set(self._live_mids(self._cover_ttl))
        delivered = {k.split(":", 1)[1] for k in live_keys if ":" in k} | set(self._board_mids)
        push_lids = {k.split(":")[0] for k in live_keys}
        if self._board_lids is not None:
            push_lids |= self._board_lids()

        # A league is covered only when EVERY hot game in it is being delivered. Collapsing to
        # league level (`{k.split(':')[0] for k in live_mids}`) meant ONE matchup pushing marked the
        # whole league covered, so its other games never got a tab and silently never priced:
        # measured 2026-08-10, league 293610 delivered JOHROT and SURKIM while RAIVAN — paired, in
        # play, seeded — returned nothing from /odds all session. The docstring already stated the
        # right rule ("covered only when every one of its HOT games is rendered"); only these two
        # sources were still league-wide. The board is VIRTUALISED (~13 of 55 rows), so per-league
        # coverage was never a safe inference from it.
        candidates = set(self._league_games) | push_lids | self._board_dom
        cov = set()
        for lid in candidates:
            hot = self._hot_mids(lid, now)
            if not hot:
                # Nothing near-term to cover. Keep the league out of the gap queue so a tab slot is
                # not spent on a league whose games are all done or days away.
                cov.add(lid)
                continue
            if all(m in delivered for m in hot):
                cov.add(lid)
        return cov

    async def request_verify(self, lid: str) -> str:
        """VERIFY-ON-DETECTION: promptly open a tab for `lid` (jump the gap queue) so its live WS can confirm an
        arb the bot spotted on screening-only (httpx-re-seed) prices. Returns a status: 'already-open' | 'opened'
        | 'no-url' (not a paired league) | 'at-cap' (raise HARDVEN_TAB_MAX) | 'open-failed'."""
        lid = str(lid)
        if lid in self._tabs:
            return "already-open"
        url = self._load_paired().get(lid)
        if not url:
            return "no-url"
        if len(self._tabs) >= self._max:
            return "at-cap"
        pg = await self._session.open_tab(url)
        if pg is None:
            return "open-failed"
        self._tabs[lid] = pg
        self._tab_alive[lid] = time.time()
        print(f"[TAB-MGR] VERIFY - opened tab on demand for league {lid} -> {url[:70]} "
              f"(tabs={len(self._tabs)}/{self._max})")
        return "opened"

    @staticmethod
    def _parse_ts(s: str):
        """ISO datetime ('2026-07-17T16:10:00Z') or a bare date ('2026-07-17') → unix ts; None if unparseable."""
        if not s:
            return None
        try:
            return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
        except ValueError:
            try:
                return datetime.strptime(s[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()
            except ValueError:
                return None

    def _sort_key(self, lid: str) -> float:
        """Rove-sweep ranking key = the league's soonest game start (∞ if unknown → ranked last)."""
        return self._league_start.get(lid, float("inf"))

    @staticmethod
    def _parse_start(e: dict):
        """Pair entry → (start_ts, precise) using hardven_start_time (precise) or the day-granular
        settlement_date fallback. None if neither parses."""
        s = e.get("hardven_start_time") or ""
        if s:
            try:
                return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp(), True
            except ValueError:
                pass
        s = e.get("settlement_date") or ""
        try:
            return datetime.strptime(s[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp(), False
        except ValueError:
            return None

    def _load_paired(self) -> dict[str, str]:
        """{leagueId: url} for every filled pair that carries a league URL (written by pair_pinnacle). Side
        effects: refreshes self._league_games = {lid: {matchupId: (start_ts, precise)}} (drives the HOT
        ranking) and self._league_start = soonest start per league (rove-sweep order)."""
        try:
            data = json.loads(Path(self._pairs_path).read_text(encoding="utf-8-sig"))
        except Exception:
            return {}
        out: dict[str, str] = {}
        starts: dict[str, float] = {}
        games: dict[str, dict] = {}
        for e in data:
            tok = e.get("hardven_yes_token") or ""
            url = e.get("hardven_league_url") or ""
            if tok.count(":") < 2 or not url:
                continue
            lid, mid = tok.split(":")[0], tok.split(":")[1]
            out.setdefault(lid, url)                  # first URL seen for the league wins (all identical)
            parsed = self._parse_start(e)
            g = games.setdefault(lid, {})
            if parsed is not None:
                # keyed by GAME (matchupId), so the two mirror pairs of a 2-way market count once
                if mid not in g or (parsed[1] and not g[mid][1]):
                    g[mid] = parsed                   # a precise timestamp beats a day-granular one
                starts[lid] = min(starts.get(lid, parsed[0]), parsed[0])
            else:
                g.setdefault(mid, (None, False))
        self._league_start = starts
        self._league_games = games
        return out

    def _hot_games(self, lid: str, now: float) -> int:
        """How many of the league's paired games are HOT = pre-live AND starting within HARDVEN_TAB_HOT_HOURS.
        Precise timestamps: now < start <= now+window (a started game is in-play — dead to a pre-live bot).
        Day-granular fallbacks: hot iff that DAY overlaps the window (can't do better than the date).
        Unknown start: not hot (conservative — the rove still sweeps the league)."""
        edge = now + self._hot_hours * 3600.0
        n = 0
        for ts, precise in self._league_games.get(lid, {}).values():
            if ts is None:
                continue
            if precise:
                if now < ts <= edge:
                    n += 1
            elif ts <= edge and now < ts + 86400.0:
                n += 1
        return n

    def _has_game_today(self, lid: str, now: float) -> bool:
        """Does this league have a paired game starting on TODAY's local date?

        The board's date bar reads `Today (55)` and its sport topic carries that whole list — measured
        2026-08-06: 12 leagues streaming while only 2 were rendered. So membership of today's slate IS board
        coverage, and it is knowable IMMEDIATELY, unlike push-derived coverage which starts empty and only
        fills as prices happen to move. That startup blind spot is what opened dedicated tabs for leagues
        (9417, 237182) that were sitting on the board the whole time."""
        today = datetime.fromtimestamp(now).date()
        for ts, _precise in self._league_games.get(lid, {}).values():
            if ts is not None and datetime.fromtimestamp(ts).date() == today:
                return True
        return False

    def _hot_mids(self, lid: str, now: float) -> set:
        """Matchup ids of this league's HOT games (pre-live within the hot window) — the set the board must
        be rendering in full before we can call the league board-covered."""
        edge = now + self._hot_hours * 3600.0
        out = set()
        for mid, (ts, precise) in self._league_games.get(lid, {}).items():
            if ts is None:
                continue
            if (now < ts <= edge) if precise else (ts <= edge and now < ts + 86400.0):
                out.add(str(mid))
        return out

    def _pre_live_games(self, lid: str, now: float) -> int:
        """Paired games in this league that have NOT started yet — at ANY horizon, not just the hot window.

        The bot is pre-live only, so a league whose games have ALL started is worth nothing: not a dedicated
        tab (hot=0 already excludes it) and not a rove sweep either. Without this the rove kept parking on
        finished/in-play leagues (observed: ITF Men Fano - QF, whose single game had started 2.4h earlier)."""
        n = 0
        for ts, precise in self._league_games.get(lid, {}).values():
            if ts is None:
                n += 1                      # unknown start: keep it sweepable rather than silently dropping it
            elif precise:
                if ts > now:
                    n += 1
            elif ts + 86400.0 > now:        # day-granular: the day hasn't fully passed
                n += 1
        return n

    def _gap_key(self, now: float):
        """Dedicated-tab ranking: MOST hot games first, then soonest start."""
        return lambda lid: (-self._hot_games(lid, now), self._sort_key(lid))

    async def _tick(self) -> None:
        if self._held:            # a bet is in flight — don't open/close/navigate any tab under it
            return
        paired = self._load_paired()
        if not paired:
            return
        now = time.time()
        # The rendered-row set is kept for DIAGNOSTICS ONLY. It is NOT a coverage signal: measured
        # 2026-08-06, the board's sport topic (sp/33) streamed 12 leagues while only 2 were rendered — the
        # SPA virtualises RENDERING, not the subscription. Gating coverage on what's in the viewport would
        # deny board credit to 10 covered leagues and open exactly the redundant tabs we're trying to avoid.
        # Board coverage, best source first:
        #  1. SCROLL SCAN — the board's actual league list, enumerated by scrolling its virtualised list.
        #     An observation, not an inference, and checkable by eye in the log.
        #  2. fallback: today's slate (the board is the sport's Today list) while the board is streaming.
        # Either way it is gated on the board demonstrably pushing, so a dead board never claims coverage.
        pushing = self._board_lids() if self._board_lids is not None else set()
        scanned: set = set()
        if self._board_dom_fn is not None:
            try:
                scanned = {str(x) for x in (await self._board_dom_fn() or set())}
            except Exception:
                scanned = set()
        self._board_scanned = scanned
        if scanned:
            self._board_dom = scanned
        elif self._board_covers_today and pushing:
            self._board_dom = {lid for lid in paired if self._has_game_today(lid, now)}
        else:
            self._board_dom = set()
        board = pushing | self._board_dom
        # 0. prune tabs that no longer EXIST (closed by hand, crashed renderer). Must run before anything
        # else reads _tabs, or a closed tab keeps its league out of gap selection and silently un-covers it.
        for lid in [l for l, pg in self._tabs.items() if _dead(pg)]:
            self._tabs.pop(lid, None)
            self._tab_board_since.pop(lid, None)
            self._tab_alive.pop(lid, None)
            print(f"[TAB-MGR] tab for league {lid} is GONE (closed externally) - dropped; "
                  "the league is a gap again and will be re-opened if it still needs cover")
        if self._rove_page is not None and _dead(self._rove_page):
            print("[TAB-MGR] rove tab is GONE (closed externally) - will re-open on the next sweep")
            self._rove_page, self._rove_lid = None, None
        # 1. prune dedicated tabs whose league is no longer paired (game settled / off today's slate)
        for lid in list(self._tabs):
            if lid not in paired:
                await self._session.close_tab(self._tabs.pop(lid))
                self._tab_board_since.pop(lid, None)
                self._tab_alive.pop(lid, None)
                print(f"[TAB-MGR] closed tab for de-paired league {lid} (tabs={len(self._tabs)})")
        live_keys_now = set(self._live_mids(self._cover_ttl))
        # 1b. RECLAIM tabs the featured board has taken over: a league we opened a tab for (because it WASN'T on
        # the board) that later appears there and STAYS ≥ board_reclaim_sec is now redundant — close it so the
        # slot covers a still-uncovered league instead. Sustained (timer) so a transient board_lids blip from the
        # primary page glancing at another sport doesn't churn tabs. Its coverage continues via the board.
        for lid in list(self._tabs):
            # Board membership is NOT sufficient to reclaim. The board is virtualised, so a league can
            # be listed on it while some of its matchups never render — and gap selection now judges
            # coverage per matchup. Testing only `lid in board` made the two disagree and fight:
            # observed 2026-08-10, league 293612 was reclaimed as "redundant" and re-opened as a gap
            # ~15s later, in a loop, because the board carried the league but not all of its hot games.
            # Keep the tab while ANY hot game is still dark; the slot is doing real work.
            hot = self._hot_mids(lid, now)
            dark = {m for m in hot if f"{lid}:{m}" not in live_keys_now}
            if lid in board and not dark:
                self._tab_board_since.setdefault(lid, now)
                if now - self._tab_board_since[lid] >= self._board_reclaim_sec:
                    await self._session.close_tab(self._tabs.pop(lid))
                    self._tab_board_since.pop(lid, None)
                    self._tab_alive.pop(lid, None)
                    print(f"[TAB-MGR] reclaimed tab for league {lid} - now on the featured board (redundant); "
                          f"slot freed for an uncovered league (tabs={len(self._tabs)}/{self._max})")
            else:
                self._tab_board_since.pop(lid, None)          # dropped off the board → reset the timer
        # 1c. HOT eviction: a dedicated tab whose league no longer has any pre-live game inside the hot window
        # (all started / settled / slid to tomorrow) is a wasted slot — close it; the rove sweeps it instead.
        # Freshly-opened tabs get a grace period so a verify tab can't be evicted mid-job.
        for lid in list(self._tabs):
            if now - self._tab_alive.get(lid, now) < self._evict_grace:
                continue
            if self._hot_games(lid, now) == 0:
                await self._session.close_tab(self._tabs.pop(lid))
                self._tab_board_since.pop(lid, None)
                self._tab_alive.pop(lid, None)
                print(f"[TAB-MGR] closed tab for league {lid} - no pre-live game inside "
                      f"{self._hot_hours:g}h (rove sweeps it now; tabs={len(self._tabs)}/{self._max})")
        # 1d. PERIODIC RE-RANK (the "hourly full reset", done as a reconcile so coverage never blacks out):
        # recompute the ideal top-N hot leagues; close tabs that are no longer in it. The normal opener below
        # then refills the freed slots one per tick. Slow cadence on purpose - a league flapping between rank
        # N and N+1 must not churn tabs every 20s.
        if now - self._last_reset >= self._reset_sec and self._tabs:
            self._last_reset = now
            elig = [l for l in paired
                    if l not in board and self._hot_games(l, now) > 0]
            desired = set(sorted(elig, key=self._gap_key(now))[: self._max])
            outranked = [l for l in self._tabs if l not in desired
                         and now - self._tab_alive.get(l, now) >= self._evict_grace]
            for lid in outranked:
                await self._session.close_tab(self._tabs.pop(lid))
                self._tab_board_since.pop(lid, None)
                self._tab_alive.pop(lid, None)
            if outranked:
                print(f"[TAB-MGR] re-rank: closed {len(outranked)} out-ranked tab(s) "
                      f"({','.join(outranked)}) - hotter leagues take the slots (tabs={len(self._tabs)}/{self._max})")
        # 1e. OFF-STATION check: a dedicated tab that is no longer SHOWING its league (login redirect, error
        # page, stray navigation) reads as covered but delivers nothing. Detect by URL and send it home
        # (re-navigation also re-auths + re-subscribes). One per tick.
        for lid, pg in list(self._tabs.items()):
            try:
                actual = _same_page(pg.url)
            except Exception:
                continue
            expected = _same_page(paired.get(lid))
            if expected and actual and actual != expected:
                self._tab_alive[lid] = now                    # bump → bounded retry, not every tick
                ok = await self._session.navigate_tab(pg, paired[lid])
                print(f"[TAB-MGR] tab for league {lid} was OFF-STATION ({actual[:70]}) - "
                      f"{'sent home' if ok else 'renavigation FAILED'}")
                break
        # 2. gap selection: paired leagues that are uncovered (no board / tab / rove feed) AND currently HOT.
        # A league with zero hot games is not tab-worthy - the roving tail sweeps it until it heats up.
        covered = self._covered_now()
        gaps = sorted((lid for lid in paired
                       if lid not in covered and lid not in self._tabs and lid != self._rove_lid
                       and self._hot_games(lid, now) > 0),
                      key=self._gap_key(now))
        if now - self._last_log > 60:
            self._last_log = now
            nboard = len(board & set(paired))
            rv = f" rove={self._rove_lid}" if self._rove_enabled else ""
            # Freshness of EVERY managed tab so it's visible which is kept alive and which is aging toward the
            # logout: the MAIN board page (reloaded by the session every PINNACLE_RELOGIN_MIN), the ROVE tab
            # (self-refreshing — it navigates every dwell), and each dedicated tab (per-tab keepalive).
            main_age = None
            try:
                fn = getattr(self._session, "main_page_age", None)
                if callable(fn):
                    main_age = fn()
            except Exception:
                main_age = None
            main_s = f" main_idle_min={int(main_age / 60)}" if main_age is not None else ""
            rove_s = ""
            if self._rove_enabled and self._rove_page is not None:
                rove_s = f" rove_idle_sec={int(now - self._last_rove)}"
            ka = ""
            if self._tabs:
                # lid:hotgames/idlemin per dedicated tab - one glance shows WHY each tab holds its slot
                parts = [f"{lid}:{self._hot_games(lid, now)}h/{int((now - self._tab_alive.get(lid, now)) / 60)}m"
                         for lid in sorted(self._tabs)]
                ka = " tabs[lid:hot/idle]=" + ",".join(parts)
            n_hot = sum(1 for l in paired if self._hot_games(l, now) > 0)
            print(f"[TAB-MGR] paired={len(paired)} hot_leagues={n_hot} covered={len(covered & set(paired))} "
                  f"(board={nboard}) tabs={len(self._tabs)}/{self._max} gaps={len(gaps)}{rv}"
                  f"{main_s}{rove_s}{ka}")
        # 3. DEDICATED tabs: give the HOTTEST gap leagues persistent tabs, one per tick, up to the cap
        if gaps and len(self._tabs) < self._max:
            lid = gaps[0]
            pg = await self._session.open_tab(paired[lid])
            if pg is not None:
                self._tabs[lid] = pg
                self._tab_alive[lid] = now
                self._cap_warned = False
                print(f"[TAB-MGR] opened dedicated tab for league {lid} ({self._hot_games(lid, now)} hot "
                      f"game(s) in {self._hot_hours:g}h) -> {paired[lid][:70]} "
                      f"(tabs={len(self._tabs)}/{self._max}, {len(gaps) - 1} gap(s) left)")
        elif gaps and len(self._tabs) >= self._max and not self._cap_warned:
            self._cap_warned = True
            where = "swept by the roving tail tab" if self._rove_enabled else \
                    "left uncovered (raise HARDVEN_TAB_MAX or set HARDVEN_TAB_ROVE=1)"
            print(f"[TAB-MGR] {len(gaps)} gap league(s) beyond the {self._max}-tab cap - {where}.")
        # 3b. PER-TAB KEEPALIVE: reload the stalest dedicated tab whose session is aging toward the idle logout.
        # The main board + the rove refresh themselves; these dedicated tabs otherwise never do → they log out.
        # One per tick, staggered — navigating to its own league URL re-auths the tab AND re-subscribes its WS.
        due = [(lid, self._tab_alive.get(lid, now)) for lid in self._tabs]
        due = [(lid, ts) for lid, ts in due if now - ts >= self._keepalive_sec]
        if due:
            lid, ts = min(due, key=lambda kv: kv[1])          # stalest first
            url = paired.get(lid)
            self._tab_alive[lid] = now                        # bump regardless → bounded retry (not every tick)
            if url and await self._session.navigate_tab(self._tabs[lid], url):
                print(f"[TAB-MGR] keepalive reload league {lid} (idle {int((now - ts) / 60)}m) - re-auth + re-subscribe")
            else:
                print(f"[TAB-MGR] keepalive reload FAILED for league {lid} (idle {int((now - ts) / 60)}m) - will retry")
        # 4. ROVING tail tab: sweep the overflow (gaps the dedicated tabs can't hold)
        if self._rove_enabled:
            await self._rove_tick(paired)

    def status(self) -> dict:
        """What every managed tab is SHOWING vs what it is SUPPOSED to show, and why it holds its slot.
        Served on GET /debug/tabs - the operator's (and the bot's) view of tab state."""
        now = time.time()
        paired = dict(self._load_paired())
        board = set()
        if self._board_lids is not None:
            try:
                board = {str(b) for b in self._board_lids()}
            except Exception:
                pass
        live = {k.split(":")[0] for k in (self._live_mids(self._cover_ttl) or [])}

        def tab_row(lid: str, pg, kind: str) -> dict:
            expected = paired.get(lid, "")
            try:
                actual = (pg.url or "") if pg is not None else ""
            except Exception:
                actual = "<gone>"
            on_station = bool(expected and actual and _same_page(actual) == _same_page(expected))
            g = self._league_games.get(lid, {})
            soonest = self._league_start.get(lid)
            return {
                "lid": lid, "kind": kind,
                "expected_url": expected, "actual_url": actual[:120], "on_station": on_station,
                "hot_games": self._hot_games(lid, now), "paired_games": len(g),
                "soonest_start": (datetime.fromtimestamp(soonest, tz=timezone.utc).isoformat()
                                  if soonest else None),
                "ws_pushing": lid in live, "on_board": lid in board,
                "opened_or_reloaded_min": round((now - self._tab_alive.get(lid, now)) / 60, 1)
                                          if kind == "dedicated" else None,
            }

        tabs = [tab_row(lid, pg, "dedicated") for lid, pg in self._tabs.items()]
        if self._rove_page is not None:
            tabs.append(tab_row(self._rove_lid or "?", self._rove_page, "rove"))
        hot_rank = sorted((l for l in paired if self._hot_games(l, now) > 0), key=self._gap_key(now))
        return {
            "config": {"max": self._max, "hot_hours": self._hot_hours,
                       "reset_min": self._reset_sec / 60, "cover_ttl": self._cover_ttl},
            "tabs": tabs,
            "board_paired_lids": sorted(board & set(paired)),
            # Split the two board signals so a redundant tab is diagnosable. NB the key is PUSHING-BUT-NOT-
            # RENDERED (`board - _board_dom`), not "pushing" — a league that is both pushing and on the board is
            # deliberately excluded, so a rendered league reading False here means nothing. (Misread as a
            # coverage hole on 2026-08-07.) 'board_covered_lids' = the board is rendering it, which covers
            # stable-price leagues that push nothing — the case that used to spawn tabs for leagues plainly
            # visible on the main page.
            "board_pushing_lids": sorted(board - self._board_dom),
            "board_covered_lids": sorted(self._board_dom),        # judged covered by the board this tick
            "board_scanned_lids": sorted(self._board_scanned),  # from the scroll scan (authoritative)
            "board_coverage_source": ("scroll-scan" if self._board_scanned else
                                      "today-slate" if self._board_dom else "push-only"),
            "board_rendered_mids": len(self._board_mids),
            # `covered_by` MUST mirror _covered_now() — the set the gap logic actually consults — or the debug
            # view accuses the manager of holes it doesn't have. It used to test only `board` (_board_lids, the
            # WS-push view) and so reported NONE for leagues covered via _board_dom, i.e. leagues the scroll
            # scan can SEE on the board. 2026-08-07: two hot leagues read covered_by=NONE while the manager was
            # correctly declining to tab them, which looks exactly like a coverage bug. Split the board sources
            # instead of merging them, so "why does this league have no tab" stays answerable at a glance.
            "hot_ranking": [{"lid": l, "hot_games": self._hot_games(l, now),
                             "covered_by": ("board-push" if l in board
                                            else "board-dom" if l in self._board_dom
                                            else "tab" if l in self._tabs
                                            else "rove" if l == self._rove_lid
                                            else "ws" if l in live else "NONE")}
                            for l in hot_rank[:20]],
            "paired_leagues": len(paired),
            "held_for_bet": self._held,
        }

    async def _rove_tick(self, paired: dict[str, str]) -> None:
        """The single roving tab: dwell on the current tail league for HARDVEN_ROVE_DWELL_SEC, then re-point to the
        next overflow-tail league (paired, not board/dedicated-covered). Sweeps the whole tail over time, giving it
        opportunistic live-WS touches and making the browser genuinely visit those leagues."""
        now = time.time()
        if self._rove_page is not None and (now - self._last_rove) < self._rove_dwell:
            return                                            # still dwelling on the current league
        covered = self._covered_now()
        # Sweep only leagues that still have an UNSTARTED game — a fully in-play/finished league can never
        # produce a pre-live arb, so parking the rove there wastes the one roving slot (and looks odd).
        tail = sorted((lid for lid in paired
                       if lid not in self._tabs and lid not in covered and lid != self._rove_lid
                       and self._pre_live_games(lid, now) > 0),
                      key=self._sort_key)                     # sweep soonest-start tail leagues first
        if not tail:
            return                                            # nothing to sweep (all paired leagues are covered)
        self._rove_cursor = (self._rove_cursor + 1) % len(tail)
        lid = tail[self._rove_cursor]
        url = paired.get(lid)
        if not url:
            return
        if self._rove_page is None:
            pg = await self._session.open_tab(url)
            if pg is None:
                return
            self._rove_page = pg
            print(f"[TAB-MGR] ROVE tab opened -> league {lid} (sweeping {len(tail)} tail leagues, "
                  f"{self._rove_dwell:g}s each)")
        else:
            if not await self._session.navigate_tab(self._rove_page, url):
                self._rove_page = None                        # navigation died (tab closed?) -> recreate next tick
                self._rove_lid = None
                return
            print(f"[TAB-MGR] ROVE -> league {lid} ({len(tail)} tail leagues in rotation)")
        self._rove_lid = lid
        self._last_rove = now
