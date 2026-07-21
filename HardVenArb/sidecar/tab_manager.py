"""
tab_manager.py — LEAGUE TAB MANAGER for the browser-WS reader.

WHY: the browser's odds WS is subscription-follows-the-page. The main tennis board (topic sp/33) is
BOARD-SCOPED (~25% of the slate — it only streams the matches the board renders). A LEAGUE page (topic
lg/{lid}) streams that league's WHOLE slate, but subscriptions DON'T accumulate — navigating drops the old
league, so one tab = one league. Full-slate coverage therefore = one open tab per paired league the main
board isn't already feeding. (Background tabs stay alive — confirmed 2026-07-16 — so N tabs is viable.)

WHAT: every tick, read the paired leagues (+ their URLs) from cross_pairs.json, ask the reader which leagues
it's actually delivering (reader_live_mids), and open ONE tab per tick for a GAP league (paired but not being
fed) — up to HARDVEN_TAB_MAX. Tabs for leagues that drop out of the pairing (settled / de-paired) are closed.
Keyed off the reader's actually-delivered matchups so it never opens a tab for a league the board already
covers, and never double-opens (a league we have a tab for, or that's being fed, is not a gap).

The GAP model is push-based, so a board-covered league that goes QUIET for > HARDVEN_TAB_COVER_TTL looks like
a gap and may get its own tab — harmless (capped), and it guarantees live CHANGES keep flowing for it.

ENABLE: HARDVEN_TAB_MANAGER=1 (only meaningful with PINNACLE_WINDOW_WS_READ=1 + a browser session). Knobs:
  HARDVEN_TAB_MAX            (12)  max concurrent manager tabs — the coverage-vs-machine-load ceiling
  HARDVEN_TAB_INTERVAL_SEC  (20)  tick period; also the pacing (≤1 tab opened per tick, organic)
  HARDVEN_TAB_COVER_TTL     (240) a league counts as covered if a matchup pushed within this many seconds
  HARDVEN_TAB_START_DELAY_SEC (45) delay before the first tick (let the board + first pairing settle)
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional


class LeagueTabManager:
    def __init__(self, session, live_mids_fn: Callable[[float], list], pairs_path: str,
                 board_lids_fn: Optional[Callable[[], set]] = None) -> None:
        self._session = session                  # PinnacleBrowserSession (open_tab / close_tab)
        self._live_mids = live_mids_fn           # callable(ttl) -> list['lid:mid'] the reader delivered
        self._board_lids = board_lids_fn         # callable() -> set(lid) the FEATURED BOARD streams (sp/ topics)
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

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self.run())
            rove = f" + 1 roving tail tab ({self._rove_dwell:g}s/league)" if self._rove_enabled else ""
            print(f"[TAB-MGR] league tab manager ON - {self._max} dedicated gap tabs{rove} "
                  f"(tick {self._interval:g}s, cover-ttl {self._cover_ttl:g}s).")

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
        self._task = None
        for lid, pg in list(self._tabs.items()):
            await self._session.close_tab(pg)
        self._tabs.clear()
        if self._rove_page is not None:
            await self._session.close_tab(self._rove_page)
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

    async def acquire_rove_for_bet(self, url: str):
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
            try:
                await self._rove_page.bring_to_front()
            except Exception:
                pass
            if not await self._session.navigate_tab(self._rove_page, url):
                self._rove_page = None
                self._rove_lid = None
                return None
        self._rove_lid = None           # now parked on a bet league, not a swept tail league
        self._last_rove = time.time()
        return self._rove_page

    def _covered_now(self) -> set:
        """Leagues already fed → NOT gaps (so we never open a dedicated tab for them): the reader's recent pushes
        (board / dedicated tabs / rove) UNION the FEATURED-BOARD leagues (sport-level topics, generous TTL so a
        briefly-quiet featured league isn't redundantly re-tabbed)."""
        cov = {k.split(":")[0] for k in self._live_mids(self._cover_ttl)}
        if self._board_lids is not None:
            cov |= self._board_lids()
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
        """Ranking key = the league's soonest game start (∞ if unknown → ranked last)."""
        return self._league_start.get(lid, float("inf"))

    def _load_paired(self) -> dict[str, str]:
        """{leagueId: url} for every filled pair that carries a league URL (written by pair_pinnacle). Side effect:
        refreshes self._league_start = the SOONEST game start per league (from hardven_start_time, or the
        day-granular settlement_date as a fallback) so gaps + the rove sweep can be ranked soonest-first."""
        try:
            data = json.loads(Path(self._pairs_path).read_text(encoding="utf-8-sig"))
        except Exception:
            return {}
        out: dict[str, str] = {}
        starts: dict[str, float] = {}
        for e in data:
            tok = e.get("hardven_yes_token") or ""
            url = e.get("hardven_league_url") or ""
            if tok.count(":") < 2 or not url:
                continue
            lid = tok.split(":")[0]
            out.setdefault(lid, url)                  # first URL seen for the league wins (all identical)
            ts = self._parse_ts(e.get("hardven_start_time") or e.get("settlement_date") or "")
            if ts is not None:
                starts[lid] = min(starts.get(lid, ts), ts)   # soonest game in the league
        self._league_start = starts
        return out

    async def _tick(self) -> None:
        if self._held:            # a bet is in flight — don't open/close/navigate any tab under it
            return
        paired = self._load_paired()
        if not paired:
            return
        now = time.time()
        board = self._board_lids() if self._board_lids is not None else set()
        # 1. prune dedicated tabs whose league is no longer paired (game settled / off today's slate)
        for lid in list(self._tabs):
            if lid not in paired:
                await self._session.close_tab(self._tabs.pop(lid))
                self._tab_board_since.pop(lid, None)
                print(f"[TAB-MGR] closed tab for de-paired league {lid} (tabs={len(self._tabs)})")
        # 1b. RECLAIM tabs the featured board has taken over: a league we opened a tab for (because it WASN'T on
        # the board) that later appears there and STAYS ≥ board_reclaim_sec is now redundant — close it so the
        # slot covers a still-uncovered league instead. Sustained (timer) so a transient board_lids blip from the
        # primary page glancing at another sport doesn't churn tabs. Its coverage continues via the board.
        for lid in list(self._tabs):
            if lid in board:
                self._tab_board_since.setdefault(lid, now)
                if now - self._tab_board_since[lid] >= self._board_reclaim_sec:
                    await self._session.close_tab(self._tabs.pop(lid))
                    self._tab_board_since.pop(lid, None)
                    print(f"[TAB-MGR] reclaimed tab for league {lid} - now on the featured board (redundant); "
                          f"slot freed for an uncovered league (tabs={len(self._tabs)}/{self._max})")
            else:
                self._tab_board_since.pop(lid, None)          # dropped off the board → reset the timer
        # 2. leagues already covered (featured board / a dedicated tab / the rove) → not gaps; rank SOONEST-first
        covered = self._covered_now()
        gaps = sorted((lid for lid in paired
                       if lid not in covered and lid not in self._tabs and lid != self._rove_lid),
                      key=self._sort_key)
        if now - self._last_log > 60:
            self._last_log = now
            nboard = len(board & set(paired))
            rv = f" rove={self._rove_lid}" if self._rove_enabled else ""
            print(f"[TAB-MGR] paired={len(paired)} covered={len(covered & set(paired))} "
                  f"(board={nboard}) tabs={len(self._tabs)}/{self._max} gaps={len(gaps)}{rv}")
        # 3. DEDICATED tabs: give the top gap leagues persistent tabs, one per tick, up to the cap
        if gaps and len(self._tabs) < self._max:
            lid = gaps[0]
            pg = await self._session.open_tab(paired[lid])
            if pg is not None:
                self._tabs[lid] = pg
                self._cap_warned = False
                print(f"[TAB-MGR] opened dedicated tab for gap league {lid} -> {paired[lid][:70]} "
                      f"(tabs={len(self._tabs)}/{self._max}, {len(gaps) - 1} gap(s) left)")
        elif gaps and len(self._tabs) >= self._max and not self._cap_warned:
            self._cap_warned = True
            where = "swept by the roving tail tab" if self._rove_enabled else \
                    "left uncovered (raise HARDVEN_TAB_MAX or set HARDVEN_TAB_ROVE=1)"
            print(f"[TAB-MGR] {len(gaps)} gap league(s) beyond the {self._max}-tab cap - {where}.")
        # 4. ROVING tail tab: sweep the overflow (gaps the dedicated tabs can't hold)
        if self._rove_enabled:
            await self._rove_tick(paired)

    async def _rove_tick(self, paired: dict[str, str]) -> None:
        """The single roving tab: dwell on the current tail league for HARDVEN_ROVE_DWELL_SEC, then re-point to the
        next overflow-tail league (paired, not board/dedicated-covered). Sweeps the whole tail over time, giving it
        opportunistic live-WS touches and making the browser genuinely visit those leagues."""
        now = time.time()
        if self._rove_page is not None and (now - self._last_rove) < self._rove_dwell:
            return                                            # still dwelling on the current league
        covered = self._covered_now()
        tail = sorted((lid for lid in paired
                       if lid not in self._tabs and lid not in covered and lid != self._rove_lid),
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
