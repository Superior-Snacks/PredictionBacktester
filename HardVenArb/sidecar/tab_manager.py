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
from pathlib import Path
from typing import Callable, Optional


class LeagueTabManager:
    def __init__(self, session, live_mids_fn: Callable[[float], list], pairs_path: str) -> None:
        self._session = session                  # PinnacleBrowserSession (open_tab / close_tab)
        self._live_mids = live_mids_fn           # callable(ttl) -> list['lid:mid'] the reader delivered
        self._pairs_path = pairs_path
        self._tabs: dict[str, object] = {}       # leagueId -> page (tabs THIS manager opened)
        self._max = int(os.environ.get("HARDVEN_TAB_MAX", "12"))
        self._interval = float(os.environ.get("HARDVEN_TAB_INTERVAL_SEC", "20"))
        self._cover_ttl = float(os.environ.get("HARDVEN_TAB_COVER_TTL", "240"))
        self._start_delay = float(os.environ.get("HARDVEN_TAB_START_DELAY_SEC", "45"))
        self._task: Optional[asyncio.Task] = None
        self._last_log = 0.0
        self._cap_warned = False

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self.run())
            print(f"[TAB-MGR] league tab manager ON — one tab per gap league (max {self._max}, "
                  f"tick {self._interval:g}s, cover-ttl {self._cover_ttl:g}s).")

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
        self._task = None
        for lid, pg in list(self._tabs.items()):
            await self._session.close_tab(pg)
        self._tabs.clear()

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

    def _load_paired(self) -> dict[str, str]:
        """{leagueId: url} for every filled pair that carries a league URL (written by pair_pinnacle)."""
        try:
            data = json.loads(Path(self._pairs_path).read_text(encoding="utf-8-sig"))
        except Exception:
            return {}
        out: dict[str, str] = {}
        for e in data:
            tok = e.get("hardven_yes_token") or ""
            url = e.get("hardven_league_url") or ""
            if tok.count(":") >= 2 and url:
                out.setdefault(tok.split(":")[0], url)   # first URL seen for the league wins (all identical)
        return out

    async def _tick(self) -> None:
        paired = self._load_paired()
        if not paired:
            return
        # 1. prune tabs whose league is no longer paired (game settled / dropped from today's slate)
        for lid in list(self._tabs):
            if lid not in paired:
                await self._session.close_tab(self._tabs.pop(lid))
                print(f"[TAB-MGR] closed tab for de-paired league {lid} (tabs={len(self._tabs)})")
        # 2. leagues the reader is currently delivering (board OR one of our tabs) → not gaps
        covered = {k.split(":")[0] for k in self._live_mids(self._cover_ttl)}
        gaps = [lid for lid in paired if lid not in covered and lid not in self._tabs]
        now = time.time()
        if now - self._last_log > 60:
            self._last_log = now
            print(f"[TAB-MGR] paired-leagues={len(paired)} covered={len(covered & set(paired))} "
                  f"tabs={len(self._tabs)} gaps={len(gaps)}")
        if not gaps:
            return
        if len(self._tabs) >= self._max:
            if not self._cap_warned:
                self._cap_warned = True
                print(f"[TAB-MGR] {len(gaps)} gap league(s) but at tab cap {self._max} — leaving them uncovered "
                      "(raise HARDVEN_TAB_MAX if the machine can take more tabs/WS).")
            return
        self._cap_warned = False
        # 3. open ONE gap tab this tick (organic pacing; the rest follow on later ticks)
        lid = gaps[0]
        pg = await self._session.open_tab(paired[lid])
        if pg is not None:
            self._tabs[lid] = pg
            print(f"[TAB-MGR] opened tab for gap league {lid} → {paired[lid][:70]} "
                  f"(tabs={len(self._tabs)}/{self._max}, {len(gaps) - 1} gap(s) left)")
