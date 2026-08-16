"""Record REAL mouse trajectories, so the bot can replay human movement instead of synthesising it.

WHY NOT JUST KEEP THE BEZIER. The synthetic path is a smooth curve with eased timing and +/-1px jitter.
Real pointing is not smooth: it is a fast ballistic throw that UNDERSHOOTS, then one or more corrective
sub-movements, then a settle, with tremor throughout and pauses that land wherever attention did. Fitts'
law describes the timing; the shape is the part no formula reproduces. A page that records mousemove —
and Pinnacle runs Microsoft Clarity, which does exactly that — sees the difference in the velocity
profile, not in any single coordinate.

RECORDED AT THE OS LEVEL, NOT IN THE PAGE. `GetCursorPos` is polled from this process; nothing is
injected into the document, no listener is added, no native is patched. The page cannot observe the
recording at all — which matters because the last attempt to characterise a site's behaviour by patching
natives produced the account's first captcha.

    python mouse_record.py                    # records until Ctrl+C
    python mouse_record.py --out tennis.json  # separate corpus per context

USE IT LIKE THIS: open Pinnacle, and for a few minutes just USE IT NORMALLY — reach for odds buttons,
open a Quick Bet, hover it, scroll the list, move away. Every press is stored with the ~1.5s of approach
that preceded it. Twenty or thirty gestures is plenty; the replayer warps them to new targets.

The corpus holds only cursor coordinates and click timing — no page content, no account data.
"""
from __future__ import annotations

import argparse
import ctypes
import json
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

if not sys.platform.startswith("win"):
    print("Windows-only (GetCursorPos / GetAsyncKeyState).")
    sys.exit(2)

try:
    ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
except Exception:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        pass

VK_LBUTTON = 0x01
HZ = 125.0                      # ~8ms — above the 60-125Hz a mouse actually reports, so nothing is lost
APPROACH_SEC = 1.8              # how much run-up to keep before each press
MIN_POINTS = 12                 # a "gesture" shorter than this is a twitch, not a reach


class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


def cursor() -> tuple[int, int]:
    p = POINT()
    ctypes.windll.user32.GetCursorPos(ctypes.byref(p))
    return p.x, p.y


def lbutton_down() -> bool:
    # High-order bit = currently down. GetAsyncKeyState, so no hook is installed.
    return bool(ctypes.windll.user32.GetAsyncKeyState(VK_LBUTTON) & 0x8000)


def summarise(g: dict) -> str:
    pts = g["points"]
    dx = pts[-1][1] - pts[0][1]
    dy = pts[-1][2] - pts[0][2]
    dist = (dx * dx + dy * dy) ** 0.5
    dur = pts[-1][0] - pts[0][0]
    return (f"{len(pts):4} pts  {dur:5.2f}s  {dist:6.0f}px  "
            f"press {g['press_ms']:4.0f}ms  reversals {g['reversals']}")


def reversals(pts) -> int:
    """Direction changes — the corrective sub-movements a Bezier does not have. A pure synthetic curve
    scores 0-1; a real reach for a small target typically scores 2-5."""
    n, prev = 0, None
    for i in range(1, len(pts)):
        dx = pts[i][1] - pts[i - 1][1]
        dy = pts[i][2] - pts[i - 1][2]
        if dx == 0 and dy == 0:
            continue
        cur = (1 if dx > 0 else -1 if dx < 0 else 0, 1 if dy > 0 else -1 if dy < 0 else 0)
        if prev and cur != prev:
            n += 1
        prev = cur
    return n


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="mouse_corpus.json")
    ap.add_argument("--max", type=int, default=200, help="stop after this many gestures")
    a = ap.parse_args()
    out = Path(__file__).parent / a.out

    corpus = []
    if out.exists():
        try:
            corpus = json.loads(out.read_text(encoding="utf-8")).get("gestures", [])
            print(f"[REC] appending to {out.name} ({len(corpus)} gestures already)")
        except Exception:
            corpus = []

    print("[REC] recording the REAL cursor. Nothing is injected into any page.")
    print("[REC] Use Pinnacle normally: reach for odds, open a Quick Bet, hover it, scroll, move away.")
    print("[REC] Every LEFT CLICK stores the approach that preceded it. Ctrl+C when done.\n")

    trail: list[tuple[float, int, int]] = []      # (t, x, y) rolling window
    t0 = time.time()
    was_down = False
    down_at = 0.0
    dt = 1.0 / HZ
    try:
        while len(corpus) < a.max:
            now = time.time() - t0
            x, y = cursor()
            if not trail or (x, y) != (trail[-1][1], trail[-1][2]):
                trail.append((now, x, y))
            # Keep only the run-up; anything older is a different gesture.
            cut = now - APPROACH_SEC
            while len(trail) > 2 and trail[0][0] < cut:
                trail.pop(0)

            down = lbutton_down()
            if down and not was_down:
                down_at = now
            elif was_down and not down:
                pts = [(round(t - trail[0][0], 4), px, py) for t, px, py in trail]
                if len(pts) >= MIN_POINTS:
                    g = {"points": pts,
                         "press_ms": round((now - down_at) * 1000, 1),
                         "reversals": reversals(pts),
                         "target": [x, y]}
                    corpus.append(g)
                    print(f"  gesture {len(corpus):3}   {summarise(g)}")
                else:
                    print(f"  (skipped a {len(pts)}-point click — too short to be a reach)")
                trail = trail[-2:]
            was_down = down
            time.sleep(dt)
    except KeyboardInterrupt:
        pass

    if not corpus:
        print("\nnothing recorded.")
        return 1
    out.write_text(json.dumps({"hz": HZ, "gestures": corpus}, indent=1), encoding="utf-8")
    revs = [g["reversals"] for g in corpus]
    durs = [g["points"][-1][0] for g in corpus]
    print(f"\n[REC] {len(corpus)} gestures -> {out.name}")
    print(f"      reversals per gesture: min {min(revs)}  median {sorted(revs)[len(revs)//2]}  max {max(revs)}")
    print(f"      approach duration    : min {min(durs):.2f}s  median {sorted(durs)[len(durs)//2]:.2f}s  "
          f"max {max(durs):.2f}s")
    print("\n      A synthetic Bezier scores 0-1 reversals. If these are consistently higher, that is")
    print("      precisely the signal the current path does not reproduce — and what replay would fix.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
