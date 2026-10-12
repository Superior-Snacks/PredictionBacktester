"""
test_block_objective.py — PINNACLE_BLOCK_OBJECTIVE=inplay (2026-10-12): windows scored by expected LIVE minutes
from live_curve.json instead of listed starts, and the default left exactly as it was.

    python test_block_objective.py            # all PASS expected
"""
import json
import os
import sys
import tempfile
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import build_blocks as bb


def check(name, got, want):
    ok = got == want
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}: {got} (expected {want})")
    return ok


def at(h, m=0):
    return (datetime(2026, 10, 13, h, m),)


def main() -> int:
    r = []
    # A curve where nothing is live in the first hour after the listed start, then live for 90 minutes - the
    # shape of a "followed by" slate, exaggerated so the two objectives must disagree.
    step = 5
    curve = (step, [0.0] * 12 + [1.0] * 18 + [0.0] * 30)        # live at +60..+150 min
    starts = [at(10)] * 20 + [at(18)] * 2                       # 20 matches listed 10:00, 2 at 18:00

    # ── the default is untouched ──────────────────────────────────────────────
    base = bb.optimise(starts, 120, 50, 7, 360, 1380, 0)
    same = bb.optimise(starts, 120, 50, 7, 360, 1380, 0, objective="starts", curve=curve)
    r.append(check("objective='starts' with a curve = the original optimiser", same, base))
    nocurve = bb.optimise(starts, 120, 50, 7, 360, 1380, 0, objective="inplay", curve=None)
    r.append(check("objective='inplay' WITHOUT a curve falls back to starts", nocurve, base))
    r.append(check("starts objective puts its window around the 10:00 starts",
                   any(b.open_min <= 600 < b.close_min for b in base), True))

    # ── the in-play objective chases the live play, not the listed time ──────
    live = bb.optimise(starts, 120, 50, 7, 360, 1380, 0, objective="inplay", curve=curve)
    r.append(check("in-play objective found a window", len(live) >= 1, True))
    times = [600] * 20 + [1080] * 2
    got_live = bb.expected_live(times, live, curve, 7)
    got_base = bb.expected_live(times, base, curve, 7)
    r.append(check("in-play window covers more expected live minutes than the starts window",
                   got_live > got_base, True))
    r.append(check("in-play window sits on the 11:00-12:30 live span (opens >= 10:45)",
                   all(b.open_min >= 645 for b in live), True))
    r.append(check("grid rule still holds (opens on :15/:45)", all(b.open_min % 60 in (15, 45) for b in live), True))
    r.append(check("budget still holds", sum(b.close_min - b.open_min for b in live) <= 120, True))

    # ── expected_live arithmetic ──────────────────────────────────────────────
    r.append(check("one match fully inside its live span = 90 live minutes",
                   round(bb.expected_live([600], [bb.Block(600, 900)], curve)), 90))
    r.append(check("a window that ends at the listed start sees nothing",
                   round(bb.expected_live([600], [bb.Block(480, 600)], curve)), 0))
    r.append(check("worst-case jitter trims both ends",
                   round(bb.expected_live([600], [bb.Block(660, 750)], curve, jitter_min=7)), 76))

    # ── load_curve ───────────────────────────────────────────────────────────
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "c.json")
        with open(p, "w", encoding="utf-8") as fh:
            json.dump({"step_min": 5, "curve": [0.1, 0.2]}, fh)
        r.append(check("load_curve reads step and curve", bb.load_curve(p), (5, [0.1, 0.2])))
        r.append(check("load_curve on a missing file -> None (scheduler falls back)",
                       bb.load_curve(os.path.join(d, "missing.json")), None))
        with open(p, "w", encoding="utf-8") as fh:
            fh.write("{not json")
        r.append(check("load_curve on a broken file -> None", bb.load_curve(p), None))

    real = bb.load_curve()
    r.append(check("the shipped live_curve.json loads", real is not None, True))
    if real:
        r.append(check("  ...and peaks after the listed start, not at it",
                       max(range(len(real[1])), key=real[1].__getitem__) * real[0] > 30, True))

    n = sum(r)
    print(f"\n{n}/{len(r)} passed")
    return 0 if n == len(r) else 1


if __name__ == "__main__":
    sys.exit(main())
