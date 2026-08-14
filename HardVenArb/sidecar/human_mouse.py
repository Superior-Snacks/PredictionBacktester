"""Human-looking pointer movement, shared across book adapters.

WHY THIS EXISTS. Playwright's `locator.click()` dispatches mousemove -> mousedown -> mouseup at the
element's coordinates and nothing else. The events carry `isTrusted: true`, so they are indistinguishable
from real input ON THEIR OWN -- but a real cursor ARRIVES somewhere. A person reaching for an odds cell
emits a continuous stream of mousemove events along a curved, decelerating path; a bot emits one, at the
destination, from wherever the cursor happened to be. Any page can record `mousemove` and see the
difference without a single fingerprinting API.

So the approach is reproduced: a cubic Bezier with a perpendicular bow (people do not move in straight
lines), smoothstep easing (accelerate out, decelerate in), sub-pixel jitter, and variable per-step timing.

THE PATH IS VISUAL ONLY. The click itself still goes through `locator.click()`, which re-resolves the
element's LIVE position at click time -- essential on a board that reorders as odds tick, where landing on
stale coordinates means clicking whatever slid into them. Never replace that with a coordinate click.

Ported from `pinnacle_adapter._human_move_page` / `_human_click_loc`, which remain in place and unchanged:
that adapter runs the live-money bot and is not worth refactoring underneath a running session. Fold them
into this module when it is next safe to do so.

CURSOR POSITION IS TRACKED PER PAGE. Playwright does not expose it, so we remember where we last put it --
and a bot driving several parked board tabs must not share one cursor between them, or the path on tab B
starts from wherever tab A's cursor was and teleports on the first move.
"""
from __future__ import annotations

import asyncio
import math
import random
import weakref
from typing import Callable, Iterable, Optional

# Sub-pixel wobble applied to every sampled point. Real input is quantised to integer pixels but arrives
# with tremor; this stands in for it and keeps two moves along the same route from being byte-identical.
JITTER_PX = 1.0

# Viewport band treated as "on screen" when deciding whether to wheel toward a target, and where to bring
# it to rest. Absolute pixels rather than a fraction of the window: the browser is launched with
# `viewport=None` so `page.viewport_size` is None, and reading `window.innerHeight` would mean running
# javascript in the page's MAIN world — the exact thing the locator-only reads exist to avoid.
VIEW_TOP, VIEW_BOTTOM, VIEW_REST = 80.0, 700.0, 320.0


def bezier_path(x0: float, y0: float, tx: float, ty: float,
                rng: Optional[random.Random] = None) -> list[tuple[float, float]]:
    """Sampled points from (x0,y0) to about (tx,ty). PURE -- no browser, no I/O, so it can be tested.

    Returns a single point when the move is trivial (<2px): a curve there would be noise, and a person
    making a 1px correction does not arc.
    """
    r = rng or random
    dx, dy = tx - x0, ty - y0
    dist = math.hypot(dx, dy)
    if dist < 2:
        return [(tx, ty)]
    # Perpendicular unit vector -> the bow. Sign is random so the arc is not always the same way round.
    pxu, pyu = -dy / dist, dx / dist
    bow = r.uniform(0.05, 0.20) * dist * r.choice((-1.0, 1.0))
    c1 = (x0 + dx * 0.30 + pxu * bow, y0 + dy * 0.30 + pyu * bow)
    c2 = (x0 + dx * 0.65 + pxu * bow, y0 + dy * 0.65 + pyu * bow)
    steps = int(max(10, min(40, dist / 10)))
    pts: list[tuple[float, float]] = []
    for i in range(1, steps + 1):
        t = i / steps
        s = t * t * (3 - 2 * t)          # smoothstep: slow out of the start, slow into the target
        u = 1 - s
        bx = (u * u * u * x0 + 3 * u * u * s * c1[0] + 3 * u * s * s * c2[0] + s * s * s * tx
              + r.uniform(-JITTER_PX, JITTER_PX))
        by = (u * u * u * y0 + 3 * u * u * s * c1[1] + 3 * u * s * s * c2[1] + s * s * s * ty
              + r.uniform(-JITTER_PX, JITTER_PX))
        pts.append((bx, by))
    return pts


def path_duration(dist: float, rng: Optional[random.Random] = None) -> float:
    """Seconds the whole move should take. Longer reaches take longer, but sub-linearly -- Fitts's law."""
    r = rng or random
    return r.uniform(0.14, 0.34) * (0.6 + dist / 900.0)


class HumanCursor:
    """Remembers where the pointer is, per page, and moves it plausibly."""

    def __init__(self) -> None:
        # Weak keys: parked sport tabs and the rover come and go, and a closed page must not be pinned.
        try:
            self._pos: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()
        except TypeError:                                  # pragma: no cover - defensive
            self._pos = {}                                 # type: ignore[assignment]

    def _get(self, page, tx: float, ty: float) -> tuple[float, float]:
        """Last known position for this page, or a plausible starting point near the target.

        An unknown cursor is NOT assumed to be at (0,0): the first move of a session would then be a
        sweep from the corner, which is itself a tell. Somewhere loosely nearby is what a page that has
        been sitting under a real user's pointer looks like.
        """
        try:
            got = self._pos.get(page)
        except TypeError:
            got = None
        if got:
            return got
        return (tx + random.uniform(-200, 200), ty + random.uniform(-150, 150))

    def _set(self, page, x: float, y: float) -> None:
        try:
            self._pos[page] = (x, y)
        except TypeError:                                  # pragma: no cover - defensive
            pass

    def forget(self, page) -> None:
        try:
            self._pos.pop(page, None)
        except TypeError:                                  # pragma: no cover - defensive
            pass

    async def move(self, page, tx: float, ty: float,
                   sink: Optional[Callable] = None) -> None:
        """Travel to (tx,ty). `sink` overrides where points go (tests); default is page.mouse.move."""
        x0, y0 = self._get(page, tx, ty)
        pts = bezier_path(x0, y0, tx, ty)
        if len(pts) == 1:
            self._set(page, tx, ty)
            return
        total = path_duration(math.hypot(tx - x0, ty - y0))
        emit = sink or page.mouse.move
        for (bx, by) in pts:
            try:
                await emit(bx, by)
            except Exception:
                break                                      # page closed mid-move; not worth raising
            await asyncio.sleep(max(0.004, total / len(pts) * random.uniform(0.6, 1.4)))
        self._set(page, tx, ty)

    async def scroll(self, page, total_px: float, sink: Optional[Callable] = None) -> float:
        """Wheel about `total_px` in human-shaped notches — NEGATIVE scrolls up. Returns distance moved.

        `page.mouse.wheel(0, 1400)` emits ONE event with a 1400px delta, and the board-expansion loop
        emitted an identical one at identical spacing a dozen times per sport. The events are genuine;
        the pattern is not. A real wheel fires in notches (~100-120px each), a trackpad in many smaller
        ones, both at uneven intervals — and people overshoot and flick back.

        Deltas are drawn per notch, pauses are jittered, and roughly one scroll in twelve reverses
        briefly. Cheap: the whole thing still lands in well under a second per 1400px.
        """
        emit = sink or page.mouse.wheel
        sign = 1.0 if total_px >= 0 else -1.0
        target = abs(total_px)
        done = 0.0
        guard = 0
        while done < target and guard < 200:
            guard += 1
            # Overshoot-and-correct, but only once we are far enough in for it to make sense.
            if done > 200 and random.random() < 0.08:
                back = random.uniform(40, 110)
                try:
                    await emit(0, -back * sign)
                except Exception:
                    break
                done -= back
                await asyncio.sleep(random.uniform(0.05, 0.20))
                continue
            step = min(random.uniform(90, 240), target - done)
            try:
                await emit(0, step * sign)
            except Exception:
                break                                  # page closed mid-scroll
            done += step
            await asyncio.sleep(random.uniform(0.04, 0.22))
        return done * sign

    async def click(self, page, loc, timeout: int = 5000) -> bool:
        """Approach the element, then click it for real. False if the click failed.

        Order matters: scroll first (the box is meaningless off screen), then approach the box's centre,
        then a brief pause -- people do not click the instant they arrive -- then the real click with a
        randomised press duration.
        """
        # WHEEL TOWARD IT BEFORE FALLING BACK TO A JUMP. `scroll_into_view_if_needed` teleports the scroll
        # position in one step, and BetInAsia's own bundle registers a scroll listener (measured), so a
        # board expansion that clicks a dozen "Show more" controls would emit a dozen instant jumps where
        # a person emits wheel motion. Try to bring it into view by wheeling first; keep the jump as the
        # fallback for anything that does not respond to it (virtualised lists, elements in a scroll pane).
        box = None
        try:
            for _ in range(6):
                box = await loc.bounding_box()
                if box is None:
                    break
                if VIEW_TOP <= box["y"] <= VIEW_BOTTOM:
                    break                                    # comfortably on screen already
                await self.scroll(page, box["y"] - VIEW_REST)   # signed: negative scrolls up
        except Exception:
            box = None
        try:
            await loc.scroll_into_view_if_needed(timeout=4000)
        except Exception:
            pass
        try:
            box = await loc.bounding_box()
        except Exception:
            box = None
        if box:
            # Aim off-centre. Dead-centre every time is its own signature.
            tx = box["x"] + box["width"] * random.uniform(0.35, 0.65)
            ty = box["y"] + box["height"] * random.uniform(0.35, 0.65)
            await self.move(page, tx, ty)
            await asyncio.sleep(random.uniform(0.04, 0.12))
        try:
            await loc.click(timeout=timeout, delay=random.randint(30, 90))
        except Exception:
            return False
        return True


# One cursor for the process; it is keyed per page internally.
CURSOR = HumanCursor()
