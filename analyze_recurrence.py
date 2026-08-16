"""Do arbs RECUR on the same game often enough to camp on one?

This decides whether the in-play "pre-arm one game" design pays. The idea: after a game produces an arb,
navigate to it, open the Quick Bet slip with a stake entered, and WAIT — so the next arb on that game is
a single click instead of a navigate-find-click-type-confirm. It removes the Pinnacle-side latency that
made in-play too slow for the parallel model.

It only pays if two things are true, and both are measurable from the existing tape:
  1. RECURRENCE — a game that produces one arb produces more. If most fire once, camping wins nothing.
  2. THE GAP is short enough to be worth waiting through, and the wait does not cost more elsewhere than
     it saves. While parked on one game the bot is not executing anything else, so the opportunity cost
     is every window that opens on another pair during the wait.

    python analyze_recurrence.py                       # all Pinnacle telemetry
    python analyze_recurrence.py --in-play             # in-play windows only (the target regime)
    python analyze_recurrence.py --file CrossArbTelemetry_20260814.csv
"""
from __future__ import annotations

import argparse
import csv
import glob
import statistics
import sys
from collections import defaultdict
from datetime import datetime

sys.stdout.reconfigure(encoding="utf-8")


def parse_ts(s: str):
    for f in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(s.strip(), f)
        except Exception:
            continue
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", default=None)
    ap.add_argument("--in-play", action="store_true")
    ap.add_argument("--pre-live", action="store_true")
    a = ap.parse_args()

    files = [a.file] if a.file else [f for f in glob.glob("CrossArbTelemetry_*.csv") if "_bia_" not in f]
    if not files:
        print("no Pinnacle CrossArbTelemetry_*.csv found (bia files are excluded)")
        return 1

    rows = []
    for fn in files:
        try:
            with open(fn, newline="", encoding="utf-8-sig") as fh:
                for r in csv.DictReader(fh):
                    t = parse_ts(r.get("StartTime") or "")
                    if not t:
                        continue
                    live = (r.get("HardVenInPlay") or "").strip() in ("1", "True", "true")
                    if a.in_play and not live:
                        continue
                    if a.pre_live and live:
                        continue
                    rows.append({"t": t, "pair": r.get("PairId") or "?", "live": live,
                                 "label": (r.get("Label") or "")[:44],
                                 "dur": float(r.get("DurationMs") or 0)})
        except Exception as e:
            print(f"  skipped {fn}: {type(e).__name__}: {e}")
    if not rows:
        print("no rows after filtering")
        return 1
    rows.sort(key=lambda r: r["t"])
    regime = "IN-PLAY" if a.in_play else ("PRE-LIVE" if a.pre_live else "ALL")
    print(f"{len(rows)} windows across {len(files)} file(s)   regime={regime}\n")

    by_pair = defaultdict(list)
    for r in rows:
        by_pair[r["pair"]].append(r)

    # 1. RECURRENCE
    counts = sorted((len(v) for v in by_pair.values()), reverse=True)
    once = sum(1 for c in counts if c == 1)
    print(f"pairs producing >=1 window : {len(counts)}")
    print(f"  produced exactly ONE      : {once}  ({100*once/len(counts):.0f}% of pairs)")
    print(f"  produced 2+               : {len(counts)-once}")
    repeats = sum(c - 1 for c in counts)
    print(f"windows that are a REPEAT on a pair already seen: {repeats}/{len(rows)} "
          f"({100*repeats/len(rows):.0f}%)")
    print(f"  busiest pairs: {counts[:8]}\n")

    # 2. THE GAP between consecutive windows on the same pair
    gaps = []
    for v in by_pair.values():
        v.sort(key=lambda r: r["t"])
        for i in range(1, len(v)):
            gaps.append((v[i]["t"] - v[i-1]["t"]).total_seconds())
    if gaps:
        gaps.sort()
        def pct(p): return gaps[min(len(gaps)-1, int(len(gaps)*p))]
        print(f"gap to the NEXT window on the SAME pair ({len(gaps)} samples, seconds):")
        print(f"  p10 {pct(.10):7.0f}   p25 {pct(.25):7.0f}   median {pct(.50):7.0f}   "
              f"p75 {pct(.75):7.0f}   p90 {pct(.90):7.0f}")
        for horizon in (60, 300, 900, 1800):
            n = sum(1 for g in gaps if g <= horizon)
            print(f"  within {horizon:5}s: {n:5}/{len(gaps)}  ({100*n/len(gaps):.0f}%) "
                  f"— camping this long catches this share of repeats")
        print()

    # 3. OPPORTUNITY COST: while camped on one pair, what happens elsewhere?
    print("opportunity cost of camping (windows on OTHER pairs during the wait):")
    for horizon in (300, 900):
        others = []
        for v in by_pair.values():
            v.sort(key=lambda r: r["t"])
            for i in range(len(v) - 1):
                t0 = v[i]["t"]
                n = sum(1 for r in rows
                        if r["pair"] != v[i]["pair"] and 0 < (r["t"] - t0).total_seconds() <= horizon)
                others.append(n)
        if others:
            print(f"  camping {horizon:4}s: median {statistics.median(others):.0f} window(s) elsewhere, "
                  f"mean {statistics.mean(others):.1f}")
    print("\nRead it this way: camping pays if the REPEAT share is high AND the gap is short AND the")
    print("count elsewhere is low. A high count elsewhere means a sniper covering everything beats a")
    print("camper — unless the camper's speed advantage converts a much larger share of what it sees.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
