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
import os
from datetime import datetime, timedelta
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
                 paired_only: bool = True, jitter_min: float = 0.0, pin_hours: str = "",
                 min_downtime_min: float = 0.0, max_daily_hours: float = 0.0,
                 fill_to_cap: bool = False, on_banking: Callable[[bool], None] | None = None):
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
        # HARD BOUNDS on the plan — the only rules that limit total uptime. Everything above shapes windows
        # around games; nothing else stops a dense slate (or overlapping pins, which merge_windows unions) from
        # producing one all-day block. 0 = disabled, which is the pre-2026-08-07 behaviour.
        self._min_downtime_min = min_downtime_min   # guaranteed browser-DOWN gap between consecutive windows
        self._max_daily_hours = max_daily_hours     # ceiling on total open time per LOCAL day
        # ACTUAL uptime burned, per local day — what makes the cap a DAILY ceiling instead of a per-plan one.
        # Without it every rebuild hands out a fresh full allowance (and old blocks fall off the front of the
        # slate anyway via fetch_starts' back_hours), so "5h/day" would really mean "5h per recompute".
        # In-memory: a restart forgets the day's spend, which is the safe direction (never over-restricts).
        self._spent_by_day: dict = {}
        self._open_since = None
        self._fill_to_cap = fill_to_cap             # treat the daily cap as a TARGET, not just a ceiling
        # Freeze/unfreeze every automation that touches the browser, for the operator's banking window.
        self._on_banking = on_banking or (lambda on: None)
        self._pre_banking = (None, "")              # override to restore when the banking window expires
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
        # OPERATOR-PINNED HOURS ("more arbs show up in the morning"): local ranges that are ALWAYS part of the
        # plan — merged after block selection so min_games/max_blocks can never drop them, and exempt from the
        # slate entirely (a pin watches LINES of later games; pre-live arbs surface hours before start).
        self._pin_ranges = sched.parse_pin_hours(pin_hours)
        # FORCED REPLAN HOURS (local). The plan is built from whatever the slate holds at compute time, and
        # a sportsbook's day fills in gradually — a plan made at 06:00 is built on a fraction of the games
        # that will exist by 08:00. The hourly recompute eventually catches up, but this pins a deliberate
        # full replan at named hours so the day's real shape is planned once the board is populated.
        self._replan_hours = set()
        for h in (os.environ.get("PINNACLE_REPLAN_HOURS", "") or "").split(","):
            h = h.strip()
            if h.isdigit() and 0 <= int(h) <= 23:
                self._replan_hours.add(int(h))
        self._replanned_at: set = set()      # (date, hour) already forced — never twice in the same hour
        self._pinned_spans: list = []         # the pins' raw spans this plan, for labeling windows "pinned"
        # OPERATOR OVERRIDE (Discord): beats the schedule entirely. Persisted in control.py so a pause/halt
        # survives a restart — a remote "stop trading" that forgets itself is worse than none.
        self._control = None                  # ControlState, injected by the adapter
        self._override: str | None = None     # None | paused | halted | forced
        self._override_until = None           # datetime (forced only)
        self._override_reason = ""
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
        # OPERATOR PINS: merged AFTER selection (immune to min_games/max_blocks) and only when the horizon
        # has ANY paired game at all — pinned hours on a truly empty slate would watch nothing.
        self._pinned_spans = []
        if self._pin_ranges and starts:
            pins = sched.pinned_windows(self._pin_ranges)
            if pins:
                # CARVE, don't merge. merge_windows() glued a session onto an abutting pin and produced 8-13h
                # blocks under a 3h session shape (2026-08-07/08). Carving keeps the pin's exact span AND the
                # session's shape as separate windows; enforce_downtime below then puts a real gap between them.
                new = sched.carve_out_pins(new, pins)
                self._pinned_spans = pins
                mode += f" + {len(pins)} pinned"
        if self._jitter_min > 0:                   # human wobble; applied AFTER selection so blocks don't change
            new = sched.apply_jitter(new, self._jitter_min)
        # HARD BOUNDS, applied LAST — after merge + jitter, because both can close a gap or lengthen a block.
        # Downtime first (it shortens windows, which can only help the budget), then the daily ceiling.
        pre_bound = sum((c - o for o, c, _ in new), timedelta())
        # Per-window ceiling FIRST: nothing else bounds a single window's LENGTH (downtime only spaces windows
        # apart, the daily cap bounds the day's total). Pins are exempt — their span is the instruction.
        max_win_cap = (self._session_hours + (self._lead_min + self._trail_min) / 60.0
                       if self._session_hours > 0 else 0.0)
        if max_win_cap > 0:
            new = sched.cap_window_length(new, max_win_cap, protected=self._pinned_spans)
        if self._min_downtime_min > 0:
            new = sched.enforce_downtime(new, self._min_downtime_min)
        if self._max_daily_hours > 0:
            now_utc = sched._utcnow()
            spent = self._spent_snapshot(now_utc)
            new = sched.cap_daily_hours(new, self._max_daily_hours, protected=self._pinned_spans,
                                        spent=spent, now=now_utc)
            if self._fill_to_cap:      # spend the slack: unused budget is unused pre-live watching time
                # In session mode the configured SHAPE is the ceiling on any one window — filling must not
                # quietly repeal PINNACLE_SESSION_HOURS by stretching a 2h session into a 5h one.
                max_win = (self._session_hours + (self._lead_min + self._trail_min) / 60.0
                           if self._session_hours > 0 else 0.0)
                new = sched.fill_daily_hours(new, self._max_daily_hours, spent=spent, now=now_utc,
                                             min_downtime_min=self._min_downtime_min,
                                             max_window_hours=max_win, protected=self._pinned_spans)
            burned = spent.get(sched._local(now_utc).date(), timedelta()).total_seconds() / 3600
        else:
            burned = 0.0
        post_bound = sum((c - o for o, c, _ in new), timedelta())
        if post_bound != pre_bound:
            mode += " + bounded"
            verb = "trimmed" if post_bound < pre_bound else "grew"
            # PER-DAY, because the cap is per-day. Printing the multi-day total against a per-day ceiling reads
            # as a breach that isn't one (2026-08-07: "10.1h -> 18.7h ... daily cap 10h" was today 8.7h +
            # tomorrow 10.0h, both legal).
            per_day: dict = {}
            for o, c, _g in new:
                d = sched._local(o).date()
                per_day[d] = per_day.get(d, timedelta()) + (c - o)
            days = ", ".join(f"{d:%a %d}: {h.total_seconds() / 3600:.1f}h" for d, h in sorted(per_day.items()))
            print(f"[PINNACLE LIFECYCLE] hard bounds {verb} the plan "
                  f"{pre_bound.total_seconds() / 3600:.1f}h -> {post_bound.total_seconds() / 3600:.1f}h total "
                  f"[{days}] (min downtime {self._min_downtime_min:g}m, daily cap {self._max_daily_hours:g}h/day, "
                  f"fill={'on' if self._fill_to_cap else 'off'}, "
                  f"{burned:.1f}h already burned today).")
        if not new and self._windows:
            print("[PINNACLE LIFECYCLE] slate returned 0 usable windows; keeping last windows (transient?)")
            return
        self._windows = new
        self._win_ts = sched._utcnow().timestamp()
        # Attribute games to windows AFTER merge+jitter, so "what this window is for" reflects the real
        # boundaries — and RECOUNT each window's games from the attribution (merge sums can drift).
        self._per_window, self._left_behind = sched.assign_games(self._windows, starts)
        self._windows = [(o, c, len(self._per_window[i])) for i, (o, c, _n) in enumerate(self._windows)]
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

    def _burn_uptime(self, now) -> None:
        """Bank the session that just ended against its local day's budget. Attributed to the day the session
        OPENED on, matching cap_daily_hours' attribution (a window is charged to the day it opens)."""
        if self._open_since is None:
            return
        day = sched._local(self._open_since).date()
        self._spent_by_day[day] = self._spent_by_day.get(day, timedelta()) + (now - self._open_since)
        self._open_since = None
        # keep only the last few days so a long-lived process doesn't accumulate forever
        for d in [d for d in self._spent_by_day if (sched._local(now).date() - d).days > 2]:
            self._spent_by_day.pop(d, None)

    def _spent_snapshot(self, now):
        """Today's burned uptime INCLUDING the session currently in progress. The live session is counted here
        rather than left to the window's own duration, because a rebuild mid-session must see the hours already
        used — otherwise the cap resets every recompute, which is the bug this whole mechanism exists to stop."""
        snap = dict(self._spent_by_day)
        if self._open and self._open_since is not None:
            day = sched._local(self._open_since).date()
            snap[day] = snap.get(day, timedelta()) + (now - self._open_since)
        return snap

    async def tick(self, now=None) -> float | None:
        """One decision step: open if `now` is inside a window and we're closed; close if outside and we're
        open. Returns seconds to the next change (for the sleep). Separated from run() so it's unit-testable."""
        now = now or sched._utcnow()
        cur = sched.active_window(self._windows, now)
        # A forced window expires on its own so a "force start" can never become a permanent 24/7 session.
        if self._override == "forced" and self._override_until and now >= self._override_until:
            print("[PINNACLE LIFECYCLE] forced window expired - back on schedule.")
            self._set_override(None, "")
        # A banking window expires back into whatever it interrupted — normally the halt that prompted it. It
        # must NOT clear that halt: only the operator knows whether the money actually landed.
        if self._override == "banking" and self._override_until and now >= self._override_until:
            prev, prev_reason = getattr(self, "_pre_banking", (None, ""))
            self._on_banking(False)              # automation back on before the state flips
            self._set_override(prev, prev_reason)
            self._pre_banking = (None, "")
            print(f"[PINNACLE LIFECYCLE] banking window expired - back to {prev or 'schedule'}.")
            if self._notify.enabled:
                self._notify.send_bg(f"🏦 banking window closed — back to **{prev or 'schedule'}**."
                                     + (" Send `resume` when the balance is topped up." if prev else ""))
        if self._override == "banking":
            inside = True                        # site must be UP; this is the one override that opens on a halt
            cur = cur or (now, self._override_until or now, 0)
        elif self._override in ("paused", "halted"):
            inside = False                       # operator pause / balance halt beats the schedule
        elif self._override == "forced":
            inside = True
            cur = cur or (now, self._override_until or now, 0)
        else:
            inside = cur is not None
        if inside and not self._open:
            self._on_open()                      # adapter resets feed latches BEFORE the session comes up
            await self._browser.start()
            self._open = True
            self._open_since = now               # start the daily-budget clock (see _burn_uptime)
            print("[PINNACLE LIFECYCLE] window OPEN → browser up.")
            self._alert_open(cur, now)
        elif not inside and self._open:
            await self._browser.stop()
            self._open = False
            self._burn_uptime(now)               # bank this session against today's cap BEFORE the next replan
            self._on_close()                     # adapter stands the feed down (session_ready=False)
            print("[PINNACLE LIFECYCLE] window CLOSED → browser down (dark).")
            self._alert_close(now)
        self.state = ("banking" if self._override == "banking"
                      else "paused" if self._override == "paused" else "halted" if self._override == "halted"
                      else "forced" if self._override == "forced"
                      else "open" if self._open else "dark")
        if self._override in ("paused", "halted"):
            secs = None                          # nothing will change until an operator resumes
        elif self._override in ("forced", "banking") and self._override_until:
            secs = max(0.0, (self._override_until - now).total_seconds())
        else:
            _, secs = sched.status(self._windows, now)
        self.next_change_secs = secs
        return secs

    # ── operator control (Discord) ────────────────────────────────────────────
    def _set_override(self, mode: str | None, reason: str, until=None, persist: bool = True) -> None:
        self._override, self._override_reason, self._override_until = mode, reason, until
        # `persist=False` is for BANKING only. Persisting it would overwrite the halt underneath in
        # control_state.json, so a sidecar restart mid-banking would come back with NO halt and start trading on
        # the empty account we opened the window to refill. In-memory only ⇒ a restart lands back in the halt.
        if persist and self._control is not None:
            self._control.set_override(mode, reason, until.isoformat() + "Z" if until else None)

    def restore_override(self, mode: str | None, reason: str, until_iso: str | None) -> None:
        """Re-apply a persisted override at startup (no re-save — it came FROM the file)."""
        until = None
        if until_iso:
            try:
                until = datetime.fromisoformat(until_iso.replace("Z", "+00:00")).replace(tzinfo=None)
            except ValueError:
                until = None
        if mode == "forced" and (until is None or until <= sched._utcnow()):
            return                                # a stale forced window must not resurrect
        if mode == "banking":
            return                                # never persisted (see _set_override); can't be restored
        self._override, self._override_reason, self._override_until = mode, reason, until

    async def pause(self, reason: str = "operator") -> dict:
        """Close the site and WAIT (process keeps running; feed stands down). Survives a restart."""
        self._set_override("paused", reason)
        await self.tick()
        if self._notify.enabled:
            self._notify.send_bg(f"⏸️ **PAUSED** ({reason}) — site closed, schedule suspended. `resume` to restart.")
        return self.status()

    async def resume(self) -> dict:
        """Clear a pause OR a balance halt and hand control back to the schedule."""
        prev = self._override
        self._set_override(None, "")
        await self.tick()
        if self._notify.enabled:
            self._notify.send_bg(f"▶️ **RESUMED** (was {prev or 'running'}) — back on schedule. {self._next_line()}")
        return self.status()

    async def force_open(self, minutes: float = 60.0, reason: str = "operator") -> dict:
        """Open NOW, outside the schedule, for `minutes` — then revert to the schedule automatically."""
        until = sched._utcnow() + timedelta(minutes=max(1.0, minutes))
        self._set_override("forced", reason, until)
        await self.tick()
        if self._notify.enabled:
            self._notify.send_bg(f"🔵 **FORCED OPEN** for {minutes:g}m (until "
                                 f"{sched._local(until):%H:%M} local) — {reason}")
        return self.status()

    async def banking(self, minutes: float = 30.0) -> dict:
        """HANDS-OFF BANKING WINDOW: bring the site up in the bot's OWN Chrome profile and then stop touching it,
        so the operator can deposit/withdraw on the very account that places the bets.

        This exists because a balance halt is self-sealing: the halt closes the browser, and a closed browser is
        exactly what you need open to top the account up. So `banking` deliberately OVERRIDES a halt to open the
        site — while keeping trading off (nothing fires in this state) and freezing every automation that would
        steal focus or navigate: tab churn/rove, organic mouse+keyboard activity, and the periodic re-login page
        reload. It auto-reverts after `minutes` (like `forced`) so a forgotten banking window can't leave the bot
        idle and un-keptalive forever, and reverting RESTORES the halt rather than clearing it — clearing is the
        operator's call, via `resume`, once the money has actually landed.
        """
        self._pre_banking = (self._override, self._override_reason)     # usually ("halted", "low balance: …")
        until = sched._utcnow() + timedelta(minutes=max(1.0, minutes))
        self._set_override("banking", "operator banking window", until, persist=False)
        self._on_banking(True)                    # freeze tabs/organic/reload BEFORE the site comes up
        await self.tick()
        if self._notify.enabled:
            prev = self._pre_banking[0]
            self._notify.send_bg(
                f"🏦 **BANKING WINDOW** — site open in the bot's own profile for {minutes:g}m "
                f"(until {sched._local(until):%H:%M} local). Automation is frozen: no tab switching, no "
                f"organic activity, no page reloads, no trading. "
                + (f"Reverts to **{prev}** when it expires — send `resume` once funds have landed."
                   if prev else "Reverts to the schedule when it expires."))
        print(f"[PINNACLE LIFECYCLE] BANKING WINDOW open for {minutes:g}m — automation frozen.")
        return self.status()

    async def halt(self, reason: str) -> dict:
        """BALANCE GUARD (or any hard stop): end the schedule and stay dark until an operator resumes.
        Distinct from `paused` so the alert — and /debug/schedule — say WHY the bot stopped itself."""
        if self._override == "halted":
            return self.status()                  # already halted: don't re-alert every check
        self._set_override("halted", reason)
        await self.tick()
        if self._notify.enabled:
            self._notify.send_bg(f"🛑 **HALTED — {reason}**\nSchedule ended and the site is closed. "
                                 "Top up and send `resume` to restart.")
        print(f"[PINNACLE LIFECYCLE] HALTED: {reason}")
        return self.status()

    def _next_line(self) -> str:
        up = [w for w in self._windows if w[0] > sched._utcnow()]
        return (f"Next window {sched._local(up[0][0]):%a %H:%M} ({up[0][2]} game(s))." if up
                else "No further windows planned yet.")

    async def set_pins(self, spec: str) -> dict:
        """Replace the pinned-hours spec and immediately replan."""
        self._pin_ranges = sched.parse_pin_hours(spec)
        if self._control is not None:
            self._control.pins = [s.strip() for s in (spec or "").split(",") if s.strip()]
            self._control.save()
        self._win_ts = 0.0                        # force a replan on the next refresh
        await self._refresh_windows()
        await self.tick()
        return self.status()

    async def apply_config(self, **kw) -> dict:
        """Change schedule knobs at runtime and replan. Unknown/blank keys are ignored."""
        fields = {"lead_min": "_lead_min", "trail_min": "_trail_min", "min_gap_min": "_min_gap_min",
                  "min_games": "_min_games", "max_blocks": "_max_blocks", "session_hours": "_session_hours",
                  "jitter_min": "_jitter_min", "horizon_hours": "_horizon", "paired_only": "_paired_only",
                  "today_only": "_today_only"}
        applied = {}
        for k, attr in fields.items():
            if kw.get(k) is None:
                continue
            v = kw[k]
            if k in ("paired_only", "today_only"):
                v = str(v).strip().lower() in ("1", "true", "yes", "on")
            elif k in ("max_blocks", "min_games", "horizon_hours"):
                v = int(v) or None if k == "max_blocks" else int(v)
            else:
                v = float(v)
            setattr(self, attr, v)
            applied[k] = v
        if applied and self._control is not None:
            self._control.schedule.update({k: (v if not isinstance(v, float) else round(v, 3))
                                           for k, v in applied.items()})
            self._control.save()
        if applied:
            self._win_ts = 0.0
            await self._refresh_windows()
            await self.tick()
        return {"applied": applied, **self.status()}

    async def run(self) -> None:
        await self._refresh_windows()
        while True:
            try:
                # Forced morning (or any named-hour) replan, in addition to the periodic recompute.
                loc = sched._local(sched._utcnow())
                key = (loc.date().isoformat(), loc.hour)
                if loc.hour in self._replan_hours and key not in self._replanned_at:
                    self._replanned_at = {k for k in self._replanned_at if k[0] == key[0]} | {key}
                    print(f"[PINNACLE LIFECYCLE] scheduled replan at {loc:%H:%M} local - rebuilding the day's "
                          "windows now that the slate has filled in.")
                    self._win_ts = 0.0
                    await self._refresh_windows()
                elif sched._utcnow().timestamp() - self._win_ts > self._recompute_sec:
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

    def _is_pinned(self, window) -> bool:
        """Does this (possibly merged/jittered) window overlap an operator-pinned span?"""
        o, c, _ = window
        return any(po < c and o < pc for po, pc, _g in self._pinned_spans)

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
        pinned = self._is_pinned(window)
        tag = " (operator hours)" if pinned else ""
        lines = [f"🟢 **LIVE**{tag} until {sched._local(c):%H:%M} local (~{mins}m) — {n} target game(s)",
                 f"• Targets: {sched.describe_games(games, 8)}"]
        if pinned:
            # A pin's value is the LINES it watches: pre-live arbs surface hours before start, so list the
            # paired games starting after this window whose prices are live right now.
            watching = sorted((g for p in self._per_window for g in p if g[0] > c), key=lambda g: g[0])
            watching += [g for g in self._left_behind if g[0] > c]
            if watching:
                lines.append(f"• Watching lines for {len(watching)} later game(s): "
                             f"{sched.describe_games(watching, 5)}")
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
            "override": self._override, "override_reason": self._override_reason,
            "override_until": (self._override_until.isoformat() + "Z") if self._override_until else None,
            "pins": [f"{a:02d}:{b:02d}-{c:02d}:{d:02d}" for a, b, c, d in self._pin_ranges],
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
                                "pinned": self._is_pinned((o, c, g)),
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
