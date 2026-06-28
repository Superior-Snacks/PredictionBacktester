"""
lifecycle.py — schedule-driven open/close of the Pinnacle browser (human session rhythm).

Ties the three pieces together: schedule.py computes WORK WINDOWS from the game slate, this controller drives
the PinnacleBrowserSession open/closed to match (organic activity + capture run WHILE open), and it goes dark
between windows / overnight. So the bot follows a punter's rhythm instead of a 24/7 server.

OPT-IN (PINNACLE_LIFECYCLE=1). When off, the adapter just holds the browser open (M0/manual). The controller
is browser-agnostic (takes any object with async start()/stop()) so its decision logic is unit-testable with a
mock — no live browser needed to prove it opens at a window, closes after, and reopens the next one.

on_open()/on_close() hooks let the adapter reset its feed latches on (re)open and stand the feed down on close.

CAVEAT (operational): closing the browser between windows relies on the persistent profile's cookies still
being valid at the next open. If Pinnacle logs the session out across a long dark gap, the next window opens to
a login page and needs a manual re-login (fine while login is manual anyway; full unattend = a later concern).
"""
from __future__ import annotations

import asyncio
from typing import Callable

import schedule as sched


class PinnacleLifecycle:
    def __init__(self, browser, sports: list[int], on_open: Callable[[], None] | None = None,
                 on_close: Callable[[], None] | None = None, recompute_sec: float = 3600.0,
                 poll_cap_sec: float = 600.0, horizon_hours: int = 36):
        self._browser = browser
        self._sports = sports
        self._on_open = on_open or (lambda: None)
        self._on_close = on_close or (lambda: None)
        self._recompute_sec = recompute_sec
        self._poll_cap = poll_cap_sec
        self._horizon = horizon_hours
        self._windows: list = []
        self._win_ts = 0.0
        self._open = False
        self.state = "init"
        self.next_change_secs = None

    async def _refresh_windows(self) -> None:
        """Recompute work windows from the live slate. On a fetch failure OR a transient empty result, KEEP the
        last windows (don't yank the browser shut mid-session over a guest-API blip)."""
        try:
            starts = await asyncio.to_thread(sched.fetch_starts, self._sports, self._horizon)
        except Exception as ex:
            print(f"[PINNACLE LIFECYCLE] slate fetch failed ({type(ex).__name__}: {ex}); keeping "
                  f"last {len(self._windows)} window(s)")
            return
        new = sched.compute_windows(starts)
        if not new and self._windows:
            print("[PINNACLE LIFECYCLE] slate returned 0 games; keeping last windows (transient?)")
            return
        self._windows = new
        self._win_ts = sched._utcnow().timestamp()
        print(f"[PINNACLE LIFECYCLE] {len(self._windows)} work window(s) planned ({len(starts)} games).")

    async def tick(self, now=None) -> float | None:
        """One decision step: open if `now` is inside a window and we're closed; close if outside and we're
        open. Returns seconds to the next change (for the sleep). Separated from run() so it's unit-testable."""
        now = now or sched._utcnow()
        inside = sched.active_window(self._windows, now) is not None
        if inside and not self._open:
            self._on_open()                      # adapter resets feed latches BEFORE the session comes up
            await self._browser.start()
            self._open = True
            print("[PINNACLE LIFECYCLE] window OPEN → browser up.")
        elif not inside and self._open:
            await self._browser.stop()
            self._open = False
            self._on_close()                     # adapter stands the feed down (session_ready=False)
            print("[PINNACLE LIFECYCLE] window CLOSED → browser down (dark).")
        self.state = "open" if self._open else "dark"
        _, secs = sched.status(self._windows, now)
        self.next_change_secs = secs
        return secs

    async def run(self) -> None:
        await self._refresh_windows()
        while True:
            try:
                if sched._utcnow().timestamp() - self._win_ts > self._recompute_sec:
                    await self._refresh_windows()
                secs = await self.tick()
                # wake at the next transition, but cap so we also re-poll/recompute periodically; floor avoids spin
                sleep = min(secs if secs is not None else self._poll_cap, self._poll_cap)
                await asyncio.sleep(max(sleep, 5.0))
            except asyncio.CancelledError:
                break
            except Exception as ex:
                print(f"[PINNACLE LIFECYCLE] error: {type(ex).__name__}: {ex}")
                await asyncio.sleep(60)

    def status(self) -> dict:
        return {"state": self.state, "open": self._open, "windows": len(self._windows),
                "next_change_secs": round(self.next_change_secs) if self.next_change_secs is not None else None}
