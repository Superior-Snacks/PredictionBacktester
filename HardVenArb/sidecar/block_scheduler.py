"""Re-author the day's work windows on a cadence, so the plan follows the slate instead of a fixed clock.

WHY THIS EXISTS
---------------
`PINNACLE_PIN_HOURS` is a STATIC spec: `pinned_windows()` re-emits the same clock times for every future
day. With the density planner suppressed (`PINNACLE_MIN_GAMES` high) those pins become the ONLY windows, so
without something re-authoring them the bot runs yesterday's shape against today's slate forever.

Measured 2026-08-25 against tomorrow's board: today's pins scored 115/149 (77%) in 10.0h open, while
re-optimising scored 119/149 (80%) in **7.5h**. The coverage gain is small; the 2.5h of open time saved is
not, because every open hour is session-age, login risk and exposure spent for nothing.

WHY A STARTUP RUN MATTERS AS MUCH AS THE SCHEDULED ONES
-------------------------------------------------------
Everything is planned forward from `now`, so a sidecar started at 16:00 authors only the blocks still to
come. That is what makes a restart free: there is no day-plan to lose, and no need to reason about which
blocks "already happened". Restarting for a system update mid-afternoon simply produces an afternoon plan.

WHY THE MIDDAY RUN FREEZES WHAT ALREADY OPENED
-----------------------------------------------
Re-optimising the whole day at noon could move a block that is currently OPEN, which would close the browser
mid-session and burn a login for nothing. So blocks whose open time has passed are carried through
untouched, and only the later ones are re-authored — which is also the point of the midday pass: Pinnacle
lists late (measured: min 4.3h lead, median 16.3h), so by noon the afternoon and evening are fully published
in a way they were not at 06:00.

PAST MIDNIGHT (2026-11-09)
--------------------------
A LATEST at or before EARLIEST ("06:00" .. "02:00") makes the plan a SERVICE DAY that ends the next morning,
for the evening hours that pay best (20-23 local paid ~3x the morning per match, see build_blocks). Minutes
are then counted from the service day's midnight, so 01:45 is minute 1545, and the pins are written as clock
times ("20:45-01:45"), which the lifecycle reads as crossing midnight.
Between midnight and LATEST we are still in YESTERDAY's service day. A pass then (a restart, or a configured
hour) must freeze the block that is running: judged on today's clock, 20:45 has "not opened yet", and the
re-plan would close the browser mid-session.
"""
from __future__ import annotations
import asyncio, datetime as dt, os

import build_blocks as bb
import schedule as sched


def _hours_from_env(spec: str) -> list[int]:
    out = []
    for part in (spec or "").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            h = int(part.split(":")[0])
        except ValueError:
            continue
        if 0 <= h <= 23:
            out.append(h)
    return sorted(set(out))


class BlockScheduler:
    """Authors `lifecycle` pins from the live slate at startup and at each configured local hour."""

    def __init__(self, lifecycle, sport_ids, pairs_path: str,
                 hours: str = "06,12", budget_h: float = 10.0, min_gap: int = 50,
                 jitter: int = 7, earliest: str = "06:00", latest: str = "23:00",
                 horizon_h: int = 30, initial_delay: float = 20.0, paired_only: bool = True,
                 objective: str = "starts", curve_path: str | None = None, weights_path: str | None = None):
        self._lc = lifecycle
        # "starts" (the original) or "inplay" (expected live minutes - see build_blocks). The curve is re-read
        # on every pass, so rebuilding live_curve.json takes effect at the next re-author without a restart.
        self._objective = "inplay" if (objective or "").strip().lower() == "inplay" else "starts"
        self._curve_path = curve_path
        # PINNACLE_BLOCK_WEIGHTS: hour_value.json (in-play objective only), re-read every pass like the curve.
        # A relative name lives beside the scheduler. Unset = unweighted, exactly as before.
        w = (weights_path or "").strip()
        self._weights_path = (w if os.path.isabs(w) else os.path.join(os.path.dirname(os.path.abspath(bb.__file__)), w)) if w else None
        self._sport_ids = list(sport_ids)
        self._pairs = pairs_path
        self._hours = _hours_from_env(hours) or [6, 12]
        self._budget_h = budget_h
        self._min_gap = min_gap
        self._jitter = jitter
        self._earliest = earliest
        self._latest = latest
        self._horizon = horizon_h
        self._initial_delay = initial_delay
        self._paired_only = paired_only
        self.last_spec = ""
        self.last_run = None
        self.last_error = ""

    # -- the service day ------------------------------------------------------------------------------
    def service_day(self, now: dt.datetime) -> tuple[dt.datetime, int, int, int]:
        """(midnight, now, earliest, latest): the plan's local midnight and three times as minutes from it.
        Without an overnight LATEST this is just today. With one, an hour between midnight and LATEST still
        belongs to YESTERDAY's plan, so its midnight is yesterday's and `now` reads past 1440."""
        e, l = bb.hm(self._earliest), bb.hm(self._latest)
        latest = l + 1440 if l <= e else l
        clock = now.hour * 60 + now.minute
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        if latest > 1440 and clock < e and clock + 1440 < latest:
            return midnight - dt.timedelta(days=1), clock + 1440, e, latest
        return midnight, clock, e, latest

    def pin_minutes(self, spec: str) -> tuple[int, int]:
        """A live pin ('20:45-01:45') as (open, close) minutes on the service-day scale."""
        e, l = bb.hm(self._earliest), bb.hm(self._latest)
        o, c = (bb.hm(x) for x in str(spec).split("-"))
        if l <= e:                                # overnight plan: a clock time before EARLIEST is after midnight
            o, c = (x + 1440 if x < e else x for x in (o, c))
        if c <= o:                                # a pin that crosses midnight on its own
            c += 1440
        return o, c

    # -- one pass ------------------------------------------------------------------------------------
    def _author(self, why: str, now: dt.datetime | None = None) -> str | None:
        """Compute the block spec for the REST of the service day. Returns None when there is nothing worth
        opening."""
        curve = bb.load_curve(self._curve_path) if self._objective == "inplay" else None
        if self._objective == "inplay" and curve is None:
            print(f"[BLOCKS] {why}: PINNACLE_BLOCK_OBJECTIVE=inplay but no usable live_curve.json - "
                  "scoring by STARTS this pass. Run sidecar/live_curve.py.")
        weights = bb.load_weights(self._weights_path) if (curve and self._weights_path) else None
        if curve and self._weights_path and weights is None:
            print(f"[BLOCKS] {why}: PINNACLE_BLOCK_WEIGHTS set but {self._weights_path} is missing or unusable - "
                  "scoring UNWEIGHTED this pass. Run sidecar/hour_value.py.")
        now = now or dt.datetime.now()
        midnight, now_min, e_min, latest_min = self.service_day(now)
        starts = bb.load_starts(self._sport_ids, self._horizon, self._pairs, self._paired_only,
                                back_hours=6 if curve else 0,
                                window=(midnight, midnight + dt.timedelta(minutes=max(latest_min, 1440))))
        if not starts:
            print(f"[BLOCKS] {why}: no {'paired ' if self._paired_only else ''}games on today's board - "
                  "leaving the current pins alone.")
            return None
        times = sorted(int((bb.local_naive(g[0]) - midnight).total_seconds() // 60) for g in starts)

        # FREEZE ANYTHING ALREADY OPEN. Read the live pins rather than a remembered copy: this scheduler is
        # not the only thing that can set them (Discord /pin, a manual POST), and moving a running block
        # would close the browser mid-session.
        frozen, earliest = [], max(e_min, now_min)
        for spec in (self._lc.status().get("pins") or []):
            try:
                o, c = self.pin_minutes(str(spec))
            except Exception:
                continue
            if o <= now_min:                      # already opened (or already finished) this service day
                frozen.append(bb.Block(o, c))
        if frozen:
            earliest = max(earliest, max(b.close_min for b in frozen) + self._min_gap)

        spent = sum(b.close_min - b.open_min for b in frozen)
        budget = max(int(self._budget_h * 60) - spent, 0)
        blocks = bb.optimise(starts, budget, self._min_gap, self._jitter,
                             earliest, latest_min, now_min,
                             objective="inplay" if curve else "starts", curve=curve,
                             weights=weights, times=times)
        allb = sorted(frozen + blocks)
        if not allb:
            print(f"[BLOCKS] {why}: nothing worth opening for the rest of today - pins unchanged.")
            return None
        cov = sum(1 for t in times
                  if any(b.open_min + self._jitter <= t < b.close_min - self._jitter for b in allb))
        total = sum(b.close_min - b.open_min for b in allb)
        spec = ",".join(str(b) for b in allb)
        print(f"[BLOCKS] {why}: {spec}")
        print(f"[BLOCKS] {why}: {len(allb)} block(s) ({len(frozen)} frozen), {total/60:.2f}h, "
              f"covering {cov}/{len(times)} start(s) ({100*cov/max(len(times),1):.0f}%)")
        if curve:
            got = bb.expected_live(times, allb, curve, self._jitter)
            full = bb.expected_live(times, [bb.Block(0, 2880)], curve)
            print(f"[BLOCKS] {why}: in-play objective - {got/60:.1f} of {full/60:.1f} expected live "
                  f"match-hours covered ({100*got/max(full, 1e-9):.0f}%)")
        if weights:
            got = bb.expected_live(times, allb, curve, self._jitter, weights=weights)
            full = bb.expected_live(times, [bb.Block(0, 2880)], curve, weights=weights)
            print(f"[BLOCKS] {why}: value-weighted - {got/60:.1f} of {full/60:.1f} weighted match-hours "
                  f"covered ({100*got/max(full, 1e-9):.0f}%)")
        return spec

    async def _run_once(self, why: str) -> None:
        try:
            spec = await asyncio.to_thread(self._author, why)      # fetch_starts is blocking httpx
        except Exception as ex:
            self.last_error = f"{type(ex).__name__}: {ex}"
            print(f"[BLOCKS] {why} FAILED ({self.last_error}) - pins left exactly as they were.")
            return
        self.last_error = ""
        self.last_run = dt.datetime.now()
        if not spec or spec == self.last_spec:
            if spec:
                print(f"[BLOCKS] {why}: identical to the running plan - not replanning.")
            return
        self.last_spec = spec
        await self._lc.set_pins(spec)                              # replans immediately

    # -- loop ----------------------------------------------------------------------------------------
    def _secs_until_next(self) -> tuple[float, int]:
        now = dt.datetime.now()
        for h in self._hours:
            t = now.replace(hour=h, minute=0, second=0, microsecond=0)
            if t > now:
                return (t - now).total_seconds(), h
        t = (now + dt.timedelta(days=1)).replace(hour=self._hours[0], minute=0, second=0, microsecond=0)
        return (t - now).total_seconds(), self._hours[0]

    async def run(self) -> None:
        try:
            await asyncio.sleep(self._initial_delay)   # let the HTTP server and the first plan come up
            await self._run_once("startup")
            while True:
                secs, h = self._secs_until_next()
                print(f"[BLOCKS] next re-author at {h:02d}:00 local (in {secs/3600:.1f}h).")
                await asyncio.sleep(secs)
                await self._run_once(f"{h:02d}:00")
        except asyncio.CancelledError:
            pass

    def status(self) -> dict:
        return {"hours": self._hours, "budget_h": self._budget_h, "jitter": self._jitter,
                "objective": self._objective, "window": f"{self._earliest}-{self._latest}",
                "weights": os.path.basename(self._weights_path) if self._weights_path else None,
                "last_spec": self.last_spec, "last_error": self.last_error,
                "last_run": self.last_run.isoformat() if self.last_run else None}
