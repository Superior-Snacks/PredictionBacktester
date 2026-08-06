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
from pathlib import Path
from typing import Callable

import schedule as sched
from env_util import atomic_write_json
from notify import Notifier


class PinnacleLifecycle:
    def __init__(self, browser, sports: list[int], on_open: Callable[[], None] | None = None,
                 on_close: Callable[[], None] | None = None, recompute_sec: float = 3600.0,
                 poll_cap_sec: float = 600.0, horizon_hours: int = 36,
                 lead_min: int = 15, trail_min: int = 45, min_gap_min: int = 60,
                 min_games: int = 1, max_blocks: int | None = 4, session_hours: float = 0.0,
                 manual_plan: str | None = None, today_only: bool = True,
                 paired_only: bool = True, jitter_min: float = 0.0):
        self._browser = browser
        self._sports = sports
        self._on_open = on_open or (lambda: None)
        self._on_close = on_close or (lambda: None)
        self._recompute_sec = recompute_sec
        self._poll_cap = poll_cap_sec
        self._horizon = horizon_hours
        # window shaping: open `lead_min` before a block, keep the densest `max_blocks` with ≥ `min_games` each
        self._lead_min = lead_min
        self._trail_min = trail_min
        self._min_gap_min = min_gap_min
        self._min_games = min_games
        self._max_blocks = max_blocks
        self._session_hours = session_hours   # >0 = discrete density-session mode (continuous sports); 0 = gap-merge windows
        self._manual_plan = manual_plan       # path to a hand-written test plan (overrides the slate) — for cycle testing
        self._today_only = today_only         # plan only the CURRENT local day's games (tomorrow's slate is incomplete)
        # Schedule around games we can actually BET (paired with a Kalshi market). Unpaired board games buy a
        # session with nothing to trade — and worse, can win the densest-block contest over a fully-paired one.
        self._paired_only = paired_only
        self._jitter_min = jitter_min         # +/- minutes of deterministic wobble on each window's edges
        self._windows: list = []
        self._win_ts = 0.0
        self._open = False
        self._last_plan: dict = {}            # provenance of the current plan (mode, counts) for status()/file
        self._per_window: list = []           # games attributed to each window (parallel to _windows)
        self._left_behind: list = []          # games in NO window — what this schedule is giving up
        self._skipped: dict = {}              # why games never reached window selection (unpaired / other day)
        self._notify = Notifier()             # Discord (no-op unless DISCORD_WEBHOOK_URL is set)
        self.state = "init"
        self.next_change_secs = None

    async def _refresh_windows(self) -> None:
        """Recompute work windows from the live slate. On a fetch failure OR a transient empty result, KEEP the
        last windows (don't yank the browser shut mid-session over a guest-API blip)."""
        if self._manual_plan:                        # TEST override: a hand-written plan; no slate fetch/compute
            if self._windows:                        # relative offsets are anchored on first load → keep them
                return
            try:
                self._windows = sched.load_manual_plan(self._manual_plan)
                self._win_ts = sched._utcnow().timestamp()
                print(f"[PINNACLE LIFECYCLE] *** MANUAL TEST PLAN *** ({self._manual_plan}) — {len(self._windows)} window(s):")
                for o, c, _ in self._windows:
                    print(f"     open {sched._local(o):%H:%M:%S} → close {sched._local(c):%H:%M:%S}  "
                          f"({(c - o).total_seconds() / 60:.1f}m open)")
            except Exception as ex:
                print(f"[PINNACLE LIFECYCLE] manual plan load FAILED ({type(ex).__name__}: {ex}) — check {self._manual_plan}")
            return
        try:
            starts = await asyncio.to_thread(sched.fetch_starts, self._sports, self._horizon)
        except Exception as ex:
            print(f"[PINNACLE LIFECYCLE] slate fetch failed ({type(ex).__name__}: {ex}); keeping "
                  f"last {len(self._windows)} window(s)")
            return
        fetched = len(starts)
        all_starts = list(starts)
        if self._today_only:                       # plan only TODAY (local) — tomorrow's slate is incomplete
            starts = sched.filter_to_local_day(starts)
        n_today = len(starts)
        other_day = [g for g in all_starts if g not in starts]
        if self._paired_only:                      # only games with a Kalshi counterpart can produce an arb
            before_pair = list(starts)
            starts = sched.filter_to_paired(starts)
            unpaired = [g for g in before_pair if g not in starts]
        else:
            unpaired = []
        n_paired = len(starts)
        # WHY a game never even reached window selection — the difference between "we're skipping this match"
        # and "we couldn't have bet this match anyway".
        self._skipped = {"unpaired": unpaired, "other_day": other_day}
        if self._session_hours > 0:                # discrete ~Nh sessions by game-START density (continuous sports)
            new = sched.compute_sessions(starts, self._session_hours, self._lead_min, self._trail_min,
                                         min_games=self._min_games, max_blocks=self._max_blocks or 4)
            mode = f"{self._session_hours:g}h density-sessions"
        else:                                      # gap-merged windows (clustered sports)
            new = sched.compute_windows(starts, self._lead_min, self._trail_min, self._min_gap_min,
                                        min_games=self._min_games, max_blocks=self._max_blocks)
            mode = f"gap-merge, densest {self._max_blocks}"
        if self._jitter_min > 0:                   # human wobble; applied AFTER selection so blocks don't change
            new = sched.apply_jitter(new, self._jitter_min)
        if not new and self._windows:
            print("[PINNACLE LIFECYCLE] slate returned 0 usable windows; keeping last windows (transient?)")
            return
        self._windows = new
        self._win_ts = sched._utcnow().timestamp()
        # Attribute games to windows AFTER jitter, so "what this window is for" reflects the real boundaries.
        self._per_window, self._left_behind = sched.assign_games(self._windows, starts)
        games_kept = sum(w[2] for w in self._windows)
        scope = f"today-only {n_today}/{fetched} games" if self._today_only else f"{fetched} games"
        if self._paired_only:
            scope += f"; paired {n_paired}/{n_today}"
        jit = f", jitter +/-{self._jitter_min:g}m" if self._jitter_min > 0 else ""
        self._last_plan = {"mode": mode, "fetched": fetched, "today": n_today, "paired": n_paired,
                           "in_window": games_kept, "jitter_min": self._jitter_min,
                           "paired_only": self._paired_only, "today_only": self._today_only}
        print(f"[PINNACLE LIFECYCLE] {len(self._windows)} session(s) planned "
              f"({mode}, lead {self._lead_min}m{jit}; {scope}; {games_kept} in-window).")
        self._write_windows_file()

    async def tick(self, now=None) -> float | None:
        """One decision step: open if `now` is inside a window and we're closed; close if outside and we're
        open. Returns seconds to the next change (for the sleep). Separated from run() so it's unit-testable."""
        now = now or sched._utcnow()
        cur = sched.active_window(self._windows, now)
        inside = cur is not None
        if inside and not self._open:
            self._on_open()                      # adapter resets feed latches BEFORE the session comes up
            await self._browser.start()
            self._open = True
            print("[PINNACLE LIFECYCLE] window OPEN → browser up.")
            self._alert_open(cur, now)
        elif not inside and self._open:
            await self._browser.stop()
            self._open = False
            self._on_close()                     # adapter stands the feed down (session_ready=False)
            print("[PINNACLE LIFECYCLE] window CLOSED → browser down (dark).")
            self._alert_close(now)
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

    # ── Discord: why we're coming up, and what we're giving up ────────────────
    def _games_for(self, window) -> list:
        """The games attributed to `window` (empty if the plan was recomputed since attribution)."""
        try:
            return self._per_window[self._windows.index(window)]
        except (ValueError, IndexError):
            return []

    def _alert_open(self, window, now) -> None:
        if not self._notify.enabled:
            return
        o, c, n = window
        games = self._games_for(window)
        mins = max(0, round((c - now).total_seconds() / 60))
        lines = [f"🟢 **LIVE** until {sched._local(c):%H:%M} local (~{mins}m) — {n} target game(s)",
                 f"• Targets: {sched.describe_games(games, 8)}"]
        # What we are NOT covering, and why. A skipped match the operator can see is a decision;
        # an invisible one is a bug they'd never catch.
        later = [g for g in self._left_behind if g[0] > c]
        if later:
            lines.append(f"• Left behind ({len(later)} later today): {sched.describe_games(later, 4)}")
        unp = self._skipped.get("unpaired") or []
        if unp:
            lines.append(f"• Not bettable ({len(unp)} unpaired on the board): {sched.describe_games(unp, 3)}")
        nxt = [w for w in self._windows if w[0] > c]
        if nxt:
            lines.append(f"• Next window: {sched._local(nxt[0][0]):%a %H:%M} ({nxt[0][2]} game(s))")
        self._notify.send_bg("\n".join(lines))

    def _alert_close(self, now) -> None:
        if not self._notify.enabled:
            return
        upcoming = [w for w in self._windows if w[0] > now]
        if upcoming:
            o, c, n = upcoming[0]
            mins = round((o - now).total_seconds() / 60)
            nxt = (f"next open {sched._local(o):%a %H:%M} local (in {mins // 60}h{mins % 60:02d}m) "
                   f"for {n} game(s): {sched.describe_games(self._games_for(upcoming[0]), 5)}")
        else:
            nxt = "no further windows planned (recomputes hourly)"
        skipping = [g for g in self._left_behind if g[0] > now]
        lines = [f"⚫ **DARK** — closed at {sched._local(now):%H:%M} local", f"• {nxt}"]
        if skipping:
            lines.append(f"• Sleeping through {len(skipping)} game(s): {sched.describe_games(skipping, 4)}")
        self._notify.send_bg("\n".join(lines))

    def _write_windows_file(self) -> None:
        """Publish the CURRENT plan to work_windows.json on every recompute.

        This file used to be written ONLY by `schedule.py --write` and read by nobody — so the copy on disk
        was a months-old preview that looked authoritative. Now it mirrors what the bot is actually doing
        (atomic write, so a reader never sees a partial file). Still advisory: the lifecycle plans in memory
        and does not read this back."""
        try:
            atomic_write_json(sched.OUT, {
                "generated_at": sched._utcnow().isoformat() + "Z",
                "plan": self._last_plan,
                "windows": [{"open": o.isoformat() + "Z", "close": c.isoformat() + "Z", "games": g,
                             "open_local": sched._local(o).strftime("%a %d %b %H:%M"),
                             "close_local": sched._local(c).strftime("%a %d %b %H:%M")}
                            for o, c, g in self._windows],
            })
        except Exception as ex:
            print(f"[PINNACLE LIFECYCLE] could not write {Path(sched.OUT).name}: {type(ex).__name__}: {ex}")

    def status(self) -> dict:
        """State + the ACTUAL planned windows (not just a count) — the schedule half of "what is the bot
        doing and why". Served on GET /debug/schedule."""
        now = sched._utcnow()
        cur = sched.active_window(self._windows, now)
        return {
            "state": self.state, "open": self._open, "windows": len(self._windows),
            "next_change_secs": round(self.next_change_secs) if self.next_change_secs is not None else None,
            "plan": self._last_plan,
            "planned_at": (sched._utcnow().fromtimestamp(self._win_ts).isoformat() + "Z") if self._win_ts else None,
            "current_window": ({"open": cur[0].isoformat() + "Z", "close": cur[1].isoformat() + "Z",
                                "games": cur[2],
                                "closes_in_min": round((cur[1] - now).total_seconds() / 60, 1)}
                               if cur else None),
            "windows_detail": [{"open": o.isoformat() + "Z", "close": c.isoformat() + "Z", "games": g,
                                "open_local": sched._local(o).strftime("%a %d %b %H:%M"),
                                "close_local": sched._local(c).strftime("%a %d %b %H:%M"),
                                "duration_min": round((c - o).total_seconds() / 60),
                                "state": ("NOW" if o <= now <= c else "past" if c < now else "upcoming"),
                                "targets": [{"start": g2[0].isoformat() + "Z", "label": sched._g(g2, 3),
                                             "mid": sched._g(g2, 2), "league": sched._g(g2, 4)}
                                            for g2 in sorted(self._per_window[i], key=lambda x: x[0])]
                                           if i < len(self._per_window) else []}
                               for i, (o, c, g) in enumerate(self._windows)],
            # What the schedule gives up, and why — the counterpart to "targets".
            "left_behind": [{"start": g[0].isoformat() + "Z", "label": sched._g(g, 3),
                             "local": sched._local(g[0]).strftime("%a %H:%M")}
                            for g in sorted(self._left_behind, key=lambda x: x[0])[:40]],
            "skipped": {k: [{"start": g[0].isoformat() + "Z", "label": sched._g(g, 3)}
                            for g in sorted(v, key=lambda x: x[0])[:20]]
                        for k, v in (self._skipped or {}).items() if v},
            "discord": self._notify.enabled,
        }
