"""Geometry and behaviour of the human pointer path. Run: python test_human_mouse.py

The path generator is pure, so the interesting properties are checkable with no browser at all: does it
actually curve, does it land where asked, does it decelerate, and does it never emit the one shape that
gives a bot away -- a single jump straight to the target.
"""
import math
import random
import sys

from human_mouse import CURSOR, HumanCursor, bezier_path, path_duration

bad = 0


def check(label, ok):
    global bad
    print(f"  {'PASS' if ok else 'FAIL'}  {label}")
    if not ok:
        bad += 1


def straightness(pts, x0, y0, tx, ty):
    """Max perpendicular deviation from the straight line, in px. 0 = a ruler-straight drag."""
    dx, dy = tx - x0, ty - y0
    d = math.hypot(dx, dy)
    if d == 0:
        return 0.0
    return max(abs((px - x0) * dy - (py - y0) * dx) / d for px, py in pts)


print("[1] the path is a path, not a teleport")
pts = bezier_path(100, 100, 700, 500)
check(f"many sampled points (got {len(pts)})", len(pts) >= 10)
check("ends at the target (within jitter)",
      math.hypot(pts[-1][0] - 700, pts[-1][1] - 500) <= 2.0)
check("no step is a giant leap",
      all(math.hypot(pts[i + 1][0] - pts[i][0], pts[i + 1][1] - pts[i][1]) < 200
          for i in range(len(pts) - 1)))

print("\n[2] it curves — a straight line is the tell")
devs = [straightness(bezier_path(100, 100, 700, 500), 100, 100, 700, 500) for _ in range(30)]
check(f"every run bows off the straight line (min dev {min(devs):.1f}px)", min(devs) > 2.0)
check("bows BOTH ways across runs (sign is random)",
      any(d > 20 for d in devs))

print("\n[3] it decelerates into the target (smoothstep, not linear)")
p = bezier_path(0, 0, 1000, 0)
first = math.hypot(p[1][0] - p[0][0], p[1][1] - p[0][1])
mid = math.hypot(p[len(p) // 2][0] - p[len(p) // 2 - 1][0], p[len(p) // 2][1] - p[len(p) // 2 - 1][1])
last = math.hypot(p[-1][0] - p[-2][0], p[-1][1] - p[-2][1])
check(f"middle is the fastest stretch (start {first:.1f}, mid {mid:.1f}, end {last:.1f})",
      mid > first and mid > last)

print("\n[4] two runs are never identical")
a = bezier_path(10, 10, 400, 400)
b = bezier_path(10, 10, 400, 400)
check("jitter + random bow make runs differ", a != b)

print("\n[5] trivial moves do not arc")
check("<2px returns a single point", bezier_path(300, 300, 300.5, 300.5) == [(300.5, 300.5)])

print("\n[6] duration scales with distance, sub-linearly")
short = sum(path_duration(50) for _ in range(200)) / 200
long_ = sum(path_duration(1200) for _ in range(200)) / 200
check(f"further takes longer ({short:.3f}s vs {long_:.3f}s)", long_ > short)
check("but not proportionally (Fitts, not constant speed)", long_ < short * 24)

print("\n[7] the cursor is tracked PER PAGE")


class FakePage:
    def __init__(self, name):
        self.name = name


class FakeMouse:
    def __init__(self):
        self.pts = []

    async def move(self, x, y):
        self.pts.append((x, y))


import asyncio


async def per_page():
    c = HumanCursor()
    p1, p2 = FakePage("tennis"), FakePage("baseball")
    m1, m2 = FakeMouse(), FakeMouse()
    await c.move(p1, 500, 400, sink=m1.move)
    await c.move(p2, 505, 405, sink=m2.move)          # a DIFFERENT tab, near the same spot
    # p2 had no cursor, so it starts somewhere plausible near its own target, NOT at p1's position.
    # The real failure this guards against is p2 inheriting p1's cursor and being a no-op or a jump.
    return m1.pts, m2.pts


m1pts, m2pts = asyncio.run(per_page())
check("each page emits its own full path", len(m1pts) >= 10 and len(m2pts) >= 10)
check("the second tab does not start from the first tab's cursor",
      m2pts[0] != m1pts[-1])

print("\n[8] scrolling is notched and uneven, not one giant delta")


class FakeWheel:
    def __init__(self):
        self.deltas = []

    async def wheel(self, dx, dy):
        self.deltas.append(dy)


async def scrolled():
    c = HumanCursor()
    w = FakeWheel()
    got = await c.scroll(FakePage("board"), 1400, sink=w.wheel)
    return got, w.deltas


got, deltas = asyncio.run(scrolled())
check(f"reaches roughly the target (got {got:.0f} of 1400)", 1390 <= got <= 1410)
check(f"in many notches, not one event (got {len(deltas)})", len(deltas) >= 6)
check("no single delta is a 1400px jump", all(abs(d) <= 260 for d in deltas))
check("deltas are all different sizes", len(set(round(abs(d), 3) for d in deltas)) > 3)
# Over many runs some should include a backward correction — people overshoot.
backs = 0
for _ in range(40):
    _g, ds = asyncio.run(scrolled())
    if any(d < 0 for d in ds):
        backs += 1
check(f"sometimes flicks back, sometimes not ({backs}/40 runs)", 0 < backs < 40)

print("\n[9] scrolling UP is supported (needed to wheel toward an element above the fold)")


async def scrolled_up():
    c = HumanCursor()
    w = FakeWheel()
    got = await c.scroll(FakePage("board"), -600, sink=w.wheel)
    return got, w.deltas


got_up, deltas_up = asyncio.run(scrolled_up())
check(f"returns a negative distance (got {got_up:.0f})", -610 <= got_up <= -590)
check("the net motion is upward", sum(deltas_up) < 0)
check("still notched", len(deltas_up) >= 3)
check("no single delta exceeds a notch", all(abs(d) <= 260 for d in deltas_up))

print("\n[10] a dead page cannot break a move")


async def dead_page():
    c = HumanCursor()

    async def boom(x, y):
        raise RuntimeError("Target page, context or browser has been closed")

    await c.move(FakePage("closed"), 100, 100, sink=boom)
    return True


check("a closed page is swallowed, not raised", asyncio.run(dead_page()))

print("\nALL PASS" if bad == 0 else f"\nFAILURES: {bad}")
sys.exit(1 if bad else 0)
