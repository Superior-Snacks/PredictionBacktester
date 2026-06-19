#!/usr/bin/env python3
"""find_lock_field.py — identify which bookmaker.eu field signals an in-play market LOCK.

No site-watching, no noting times. Run the sidecar with BOOKMAKER_LIVE_DEBUG=1 over a LIVE (in-play)
match for ~10-15 min — cricket is ideal (it suspends after every ball) — then run this. It reads the
newest live_debug_*.jsonl and, per game, ranks the fields that TOGGLE between a small set of values
many times. That's the hallmark of a lock flag (vs odds, which drift across many distinct values; vs
static fields, which never change). The top toggling flag — especially a True/False or 1/0 — is the
in-play lock signal; tell it to Claude to add to _is_tradeable.

  python find_lock_field.py                 # newest live_debug_*.jsonl in this folder
  python find_lock_field.py path/to.jsonl
"""
import glob
import json
import sys
from collections import defaultdict


def _transitions(vals: list[str]) -> int:
    return sum(1 for a, b in zip(vals, vals[1:]) if a != b)


def main() -> None:
    if len(sys.argv) > 1:
        path = sys.argv[1]
    else:
        files = sorted(glob.glob("live_debug_*.jsonl"))
        path = files[-1] if files else None
    if not path:
        print("No live_debug_*.jsonl here. Run the sidecar with BOOKMAKER_LIVE_DEBUG=1 over a LIVE "
              "match first (from this sidecar/ folder).")
        return

    recs = [json.loads(line) for line in open(path, encoding="utf-8") if line.strip()]
    by_game: dict = defaultdict(list)
    for r in recs:
        by_game[r.get("idgm")].append(r)

    print(f"reading {path}  ({len(recs)} snapshots, {len(by_game)} live game(s))")
    found_any = False
    for idgm, rs in by_game.items():
        if len(rs) < 3:
            continue   # need a few snapshots to see a toggle
        event = rs[0].get("event", idgm)
        tradeable_seen = sorted({str(r.get("tradeable")) for r in rs})
        cands = []
        for scope in ("game", "line"):
            keys = set().union(*[set(r.get(scope, {}).keys()) for r in rs])
            for k in keys:
                vals = [str(r.get(scope, {}).get(k)) for r in rs]
                distinct = set(vals)
                t = _transitions(vals)
                # lock flag = flips repeatedly but only across a SMALL value set (excludes drifting odds)
                if 2 <= len(distinct) <= 4 and t >= 2:
                    cands.append((t, -len(distinct), scope, k, sorted(distinct)))
        cands.sort(reverse=True)
        print(f"\n=== {event}  ({len(rs)} snapshots, tradeable seen={tradeable_seen}) ===")
        if not cands:
            print("  No toggling fields. Was this game actually in-play AND locking? "
                  "(need open<->locked cycles; let it run longer.)")
            continue
        found_any = True
        for t, neg_nd, scope, k, vals in cands[:8]:
            print(f"  {scope}.{k:<20} toggles={t:<4} values={vals}")
        print("  -> the top toggling flag (especially True/False or 1/0) is the lock signal.")

    if not found_any:
        print("\nNo lock candidates surfaced. Capture a longer in-play window (cricket suspends often).")


if __name__ == "__main__":
    main()
