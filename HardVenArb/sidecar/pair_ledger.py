#!/usr/bin/env python3
"""
pair_ledger.py — append-only record of EVERY (kalshi_ticker -> pinnacle_token) mapping ever used.

WHY THIS EXISTS. `cross_pairs.json` is REWRITTEN by each pairing run (every ~90 minutes in a live session),
so a mapping that existed at 14:00 and was replaced at 15:30 leaves no trace. That is precisely the window
in which a flipped pair does its damage: it writes telemetry under the wrong orientation, gets corrected on
the next run, and afterwards nothing on disk shows it was ever wrong. Verification after the fact then has
nothing to verify.

So: snapshot the mapping on every change. One line per (ticker, token) pair, first seen and last seen. The
file is append-only and tiny (a few hundred rows a day), and it is what makes retroactive verification
possible — oddspapi's /v4/historical-odds is FREE and retains data since January 2026, so any mapping
recorded here can be checked later without spending quota.

    python pair_ledger.py                 # record the current cross_pairs.json state
    python pair_ledger.py --watch 60      # poll the file and record changes every 60s
    python pair_ledger.py --report        # what mappings have been used, and which changed mid-day
"""
from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
PAIRS = HERE.parent / "cross_pairs.json"
LEDGER = HERE.parent.parent / "pair_ledger.jsonl"


def _load_pairs(path: Path) -> dict:
    """{kalshi_ticker: yes_token} for every filled row. A mid-write read yields junk — treat as no data."""
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    rows = d if isinstance(d, list) else d.get("pairs", d)
    out = {}
    for r in rows:
        tk, yt = r.get("kalshi_ticker"), r.get("hardven_yes_token")
        if tk and yt:
            out[tk] = {"yes": yt, "no": r.get("hardven_no_token"),
                       "outcome": r.get("kalshi_outcome") or r.get("outcome") or "",
                       "name": r.get("hardven_yes_name") or ""}
    return out


def _known() -> set:
    """(ticker, yes_token) pairs already on record — the ledger is append-only, so never rewrite."""
    seen = set()
    if LEDGER.exists():
        for line in LEDGER.read_text(encoding="utf-8").splitlines():
            try:
                e = json.loads(line)
                seen.add((e.get("ticker"), e.get("yes_token")))
            except Exception:
                continue
    return seen


def record(quiet: bool = False) -> int:
    cur, seen = _load_pairs(PAIRS), _known()
    new = 0
    with LEDGER.open("a", encoding="utf-8") as fh:
        for tk, v in sorted(cur.items()):
            if (tk, v["yes"]) in seen:
                continue
            fh.write(json.dumps({"at": round(time.time()), "ticker": tk, "yes_token": v["yes"],
                                 "no_token": v["no"], "kalshi_outcome": v["outcome"],
                                 "stored_name": v["name"], "verified": None}) + "\n")
            new += 1
    if new and not quiet:
        print(f"[LEDGER] recorded {new} new mapping(s) -> {LEDGER.name}")
    return new


def report() -> None:
    if not LEDGER.exists():
        print("[LEDGER] nothing recorded yet."); return
    rows = []
    for line in LEDGER.read_text(encoding="utf-8").splitlines():
        try: rows.append(json.loads(line))
        except Exception: continue
    by = {}
    for e in rows:
        by.setdefault(e["ticker"], []).append(e)
    changed = {k: v for k, v in by.items() if len({x["yes_token"] for x in v}) > 1}
    print(f"[LEDGER] {len(rows)} mapping(s) across {len(by)} ticker(s); "
          f"{len(changed)} ticker(s) CHANGED token mid-life.")
    # A ticker whose token changed is the interesting case: one of those orientations was wrong, and the
    # telemetry written under the earlier one is suspect even though the file now looks correct.
    for tk, v in sorted(changed.items()):
        print(f"   {tk}")
        for e in sorted(v, key=lambda x: x["at"]):
            print(f"      {time.strftime('%m-%d %H:%M', time.localtime(e['at']))}  {e['yes_token']}"
                  f"   (stored name '{e['stored_name']}')")
    unver = sum(1 for e in rows if e.get("verified") is None)
    print(f"   {unver} mapping(s) never verified against an independent source.")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--watch", type=float, default=0, help="poll cross_pairs.json every N seconds")
    ap.add_argument("--report", action="store_true")
    a = ap.parse_args()
    if a.report:
        report(); return
    if a.watch <= 0:
        record(); return
    print(f"[LEDGER] watching {PAIRS.name} every {a.watch:g}s -> {LEDGER.name}  (ctrl-c to stop)")
    last = None
    while True:
        try:
            m = PAIRS.stat().st_mtime
            if m != last:
                last = m
                record()
        except FileNotFoundError:
            pass
        except KeyboardInterrupt:
            break
        time.sleep(a.watch)


if __name__ == "__main__":
    main()
