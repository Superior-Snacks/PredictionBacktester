"""How different is a recorded human reach from the Bezier the bot currently emits?

Four numbers decide whether replaying real trajectories is worth the complexity. Each one is something a
page recording mousemove can compute trivially, and each is something the synthetic path gets wrong in a
fixed, characteristic way:

  REVERSALS        direction changes. Ballistic throw -> undershoot -> correction -> settle. A smoothstep
                   Bezier produces 0-1 by construction; a real reach for a small target usually makes
                   several.
  PEAK VELOCITY    where in the movement the fastest moment falls. Human reaching peaks EARLY (~25-35%)
                   and spends the rest decelerating onto the target. Smoothstep is symmetric: exactly 50%.
  NEAR-STILL TIME  fraction of samples barely moving — hesitation, re-aiming, reading. The Bezier never
                   stops; it eases, which is not the same thing.
  PRESS DURATION   button down-to-up. Already randomised 30-90ms in code; worth confirming against reality.

    python mouse_analyse.py
    python mouse_analyse.py --corpus tennis.json
"""
from __future__ import annotations

import argparse
import json
import math
import statistics as st
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")


def q(v, p):
    v = sorted(v)
    return v[min(len(v) - 1, int(len(v) * p))]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", default="mouse_corpus.json")
    a = ap.parse_args()
    path = Path(__file__).parent / a.corpus
    if not path.exists():
        print(f"no corpus at {path.name} — run mouse_record.py first")
        return 1
    d = json.loads(path.read_text(encoding="utf-8"))
    g = d.get("gestures") or []
    if not g:
        print("corpus is empty")
        return 1

    revs = [x["reversals"] for x in g]
    durs = [x["points"][-1][0] for x in g]
    pms = [x["press_ms"] for x in g]
    peaks, stills, dists, npts = [], [], [], []
    for x in g:
        p = x["points"]
        if len(p) < 6:
            continue
        vs = []
        for i in range(1, len(p)):
            dt = p[i][0] - p[i - 1][0]
            if dt <= 0:
                continue
            vs.append((math.hypot(p[i][1] - p[i - 1][1], p[i][2] - p[i - 1][2]) / dt, p[i][0]))
        if not vs:
            continue
        vmax = max(v for v, _ in vs)
        span = p[-1][0] or 1.0
        peaks.append(next(t for v, t in vs if v == vmax) / span)
        stills.append(sum(1 for v, _ in vs if v < 30) / len(vs))
        dists.append(math.hypot(p[-1][1] - p[0][1], p[-1][2] - p[0][2]))
        npts.append(len(p))

    print(f"{len(g)} gestures, sampled at {d.get('hz')}Hz\n")
    print(f"{'':22} {'recorded':>26}   {'current Bezier':>16}")
    print(f"  reversals            "
          f"min {min(revs)}  med {q(revs, .5)}  max {max(revs):<8}   {'0-1':>16}")
    if peaks:
        print(f"  peak velocity at     "
              f"{st.mean(peaks) * 100:.0f}% of the way through{'':6}   {'exactly 50%':>16}")
        print(f"  near-still samples   "
              f"{st.mean(stills) * 100:.0f}%{'':22}   {'0%':>16}")
    print(f"  approach duration    "
          f"min {min(durs):.2f}s med {q(durs, .5):.2f}s max {max(durs):.2f}s   {'0.14-0.34s':>16}")
    print(f"  press duration       "
          f"min {min(pms):.0f} med {q(pms, .5):.0f} max {max(pms):.0f} ms{'':4}   {'30-90ms':>16}")
    if dists:
        print(f"  travel               med {q(dists, .5):.0f}px  max {max(dists):.0f}px")
        print(f"  points per gesture   med {q(npts, .5)}")

    print("\nverdict:")
    if st.mean(revs) > 1.5:
        print("  REVERSALS clearly exceed what a Bezier makes — the corrective sub-movement is real and")
        print("  is the single most reproducible difference. Replay is worth wiring.")
    else:
        print("  reversals are low; either the gestures were short flicks, or this corpus is not")
        print("  representative of a reach. Record more, aiming at small targets from further away.")
    if peaks and abs(st.mean(peaks) - 0.5) > 0.08:
        print(f"  PEAK VELOCITY sits at {st.mean(peaks)*100:.0f}%, not 50% — the current easing has the")
        print("  wrong shape regardless of whether full replay lands. Even swapping smoothstep for an")
        print("  early-peaked profile would close most of that gap cheaply.")
    if durs and q(durs, .5) > 0.5:
        print(f"  APPROACHES take {q(durs, .5):.2f}s against the bot's 0.14-0.34s. The bot is roughly")
        print("  an order of magnitude too quick to be reaching for anything.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
