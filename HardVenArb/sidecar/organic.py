"""
organic.py — human-like idle behaviour for the managed Pinnacle tab.

Replaces the robotic "tiny mouse nudge every 200s" keepalive (a perfectly periodic micro-movement is itself a
faint tell) with VARIED, NON-PERIODIC activity: irregular idle gaps (occasionally a long "stepped away" gap),
CURVED mouse paths with an ease-in/ease-out velocity profile + micro-tremor + the odd overshoot-and-correct,
MULTI-NOTCH scrolls (a few small wheel events over time, not one jump), and the occasional navigation to
another sport page. The goal on PINNACLE is modest — Pinnacle is sharp-friendly and doesn't limit/ban winners,
so this is about not looking like an obvious headless scraper + holding the session, NOT impersonating every
human twitch. Pragmatic, not paranoid — but the gestures themselves are now human-shaped, not straight-line
instant teleports (a mouse that travels x1,y1→x2,y2 in a dead-straight line with no accel/decel is a tell).

INTERRUPTIBLE FOR EXECUTION: organic activity and a sub-second bet do NOT conflict — they're separate modes. A
sharp human is also fast; the tell is REGULARITY, not speed. Call pause() before placing a bet (so an in-flight
scroll/move can't fight the bet click on the single page), execute, then resume(). While paused the loop blocks.
"""
from __future__ import annotations

import asyncio
import math
import random
from collections import Counter

# Movement box (viewport-ish). Matches the old code's target range; all points are clamped into it.
_VIEW_X = (80, 1280)
_VIEW_Y = (80, 760)


def _clampx(v: float) -> float:
    return max(_VIEW_X[0], min(_VIEW_X[1], v))


def _clampy(v: float) -> float:
    return max(_VIEW_Y[0], min(_VIEW_Y[1], v))


def _smoothstep(t: float) -> float:
    """Ease-in/ease-out (3t²−2t³): derivative is 0 at both ends → slow start, fast middle, slow settle. Sampling
    the path at smoothstep(i/steps) with ~constant time per step gives the perceived accel/decel of a real move."""
    return t * t * (3.0 - 2.0 * t)


def _cubic(p0, p1, p2, p3, t: float):
    """Cubic Bézier point at t — the curved trajectory (two control points bow the path off the straight line)."""
    u = 1.0 - t
    a, b, c, d = u * u * u, 3 * u * u * t, 3 * u * t * t, t * t * t
    return (a * p0[0] + b * p1[0] + c * p2[0] + d * p3[0],
            a * p0[1] + b * p1[1] + c * p2[1] + d * p3[1])


class OrganicActivity:
    def __init__(self, page, browse_urls: list[str] | None = None,
                 min_gap: float = 20.0, max_gap: float = 150.0,
                 long_gap_chance: float = 0.15, long_gap_max: float = 900.0):
        self._page = page
        self._urls = list(browse_urls or [])
        self._min_gap, self._max_gap = min_gap, max_gap
        self._long_gap_chance, self._long_gap_max = long_gap_chance, long_gap_max
        self._gate = asyncio.Event()
        self._gate.set()                 # set = active; cleared = paused (during a bet)
        self.actions = Counter()         # telemetry / test visibility
        # Playwright doesn't expose the live cursor position, so we track our own (we're the only mover between
        # bets) — a curved move needs a real start point, not a teleport from nowhere.
        self._x = float(random.randint(*_VIEW_X))
        self._y = float(random.randint(*_VIEW_Y))

    # ── execution interlock ───────────────────────────────────────────────────────
    def pause(self) -> None:
        self._gate.clear()

    def resume(self) -> None:
        self._gate.set()

    # ── gestures ──────────────────────────────────────────────────────────────────
    def _pick_target(self):
        """Humans make many small local adjustments and fewer big repositions — mix the two, don't always jump
        across the whole screen."""
        if random.random() < 0.5:
            return (_clampx(self._x + random.uniform(-150, 150)),
                    _clampy(self._y + random.uniform(-150, 150)))
        return (float(random.randint(*_VIEW_X)), float(random.randint(*_VIEW_Y)))

    async def _human_move(self, tx: float, ty: float) -> None:
        """Move the cursor along a CURVED path with ease-in/ease-out speed, per-point micro-tremor, and an
        occasional overshoot-then-correct. Emits one mouse.move per sampled point with a short sleep between —
        so it takes real (variable) time, unlike Playwright's steps= (which fires all points back-to-back)."""
        x0, y0 = self._x, self._y
        tx, ty = _clampx(tx), _clampy(ty)
        dx, dy = tx - x0, ty - y0
        dist = math.hypot(dx, dy)
        if dist < 2.0:
            return
        # perpendicular unit vector → bow the two control points off the straight line (gentle C/S curve)
        pxu, pyu = -dy / dist, dx / dist
        bow = random.uniform(0.05, 0.22) * dist * random.choice((-1.0, 1.0))
        c1 = (x0 + dx * 0.30 + pxu * bow * random.uniform(0.4, 1.0),
              y0 + dy * 0.30 + pyu * bow * random.uniform(0.4, 1.0))
        c2 = (x0 + dx * 0.65 + pxu * bow * random.uniform(0.4, 1.0),
              y0 + dy * 0.65 + pyu * bow * random.uniform(0.4, 1.0))
        # occasional overshoot on longer moves: aim slightly PAST the target, then settle back
        overshoot = dist > 120 and random.random() < 0.22
        aim = (tx, ty)
        if overshoot:
            over = random.uniform(6, 22)
            aim = (tx + dx / dist * over, ty + dy / dist * over)

        steps = int(max(12, min(48, dist / 9)))
        total = random.uniform(0.16, 0.42) * (0.6 + dist / 900)   # bigger moves take longer (Fitts-ish)
        for i in range(1, steps + 1):
            t = _smoothstep(i / steps)
            bx, by = _cubic((x0, y0), c1, c2, aim, t)
            bx += random.uniform(-1.2, 1.2)                        # hand micro-tremor
            by += random.uniform(-1.2, 1.2)
            await self._page.mouse.move(_clampx(bx), _clampy(by))
            await asyncio.sleep(max(0.004, total / steps * random.uniform(0.6, 1.4)))
        if overshoot:
            for _ in range(random.randint(2, 4)):                 # small corrective settle onto the true target
                await self._page.mouse.move(_clampx(tx + random.uniform(-1.0, 1.0)),
                                            _clampy(ty + random.uniform(-1.0, 1.0)))
                await asyncio.sleep(random.uniform(0.012, 0.03))
        self._x, self._y = tx, ty

    async def _scroll_burst(self, total: int, direction: int) -> None:
        """Scroll `total` px as a HANDFUL of smaller wheel notches with variable gaps (a human spins the wheel /
        flicks the trackpad repeatedly), not a single big deltaY jump."""
        remaining = total
        for _ in range(random.randint(3, 8)):
            if remaining <= 0:
                break
            step = min(remaining, random.randint(40, 140))
            remaining -= step
            await self._page.mouse.wheel(0, direction * step)
            await asyncio.sleep(random.uniform(0.03, 0.13))

    # ── the loop ──────────────────────────────────────────────────────────────────
    def _next_gap(self) -> float:
        """Mostly short irregular gaps; occasionally a long 'stepped away' one. Never a fixed cadence."""
        if random.random() < self._long_gap_chance:
            return random.uniform(self._max_gap, self._long_gap_max)
        return random.uniform(self._min_gap, self._max_gap)

    async def tick(self) -> str:
        """Do ONE organic action; returns its name (so a test can drive it deterministically). All actions are
        best-effort — a failure on a weird page must never crash the loop."""
        roll = random.random()
        try:
            if roll < 0.40:
                name = "idle"                       # most ticks: do nothing (a human isn't always acting)
            elif roll < 0.65:
                name = "mouse"
                await self._human_move(*self._pick_target())
            elif roll < 0.88:
                name = "scroll"
                await self._scroll_burst(random.randint(200, 850), direction=1)
                if random.random() < 0.5:
                    await asyncio.sleep(random.uniform(0.4, 2.0))
                    await self._scroll_burst(random.randint(100, 450), direction=-1)   # glance back up
            elif self._urls:
                name = "navigate"
                await self._page.goto(random.choice(self._urls), wait_until="domcontentloaded", timeout=30_000)
            else:
                name = "mouse"
                await self._human_move(*self._pick_target())
        except Exception:
            name = "error"
        self.actions[name] += 1
        return name

    async def run(self) -> None:
        """Background loop: wait an irregular gap, then one action — pausing entirely while a bet is in flight.
        Cancel the task to stop (matches the adapter's other background tasks)."""
        while True:
            try:
                await self._gate.wait()                      # block while paused for execution
                await asyncio.sleep(self._next_gap())
            except asyncio.CancelledError:
                break
            if not self._gate.is_set():                      # paused during the sleep → don't act this tick
                continue
            await self.tick()
