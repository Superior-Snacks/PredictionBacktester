#!/usr/bin/env python3
"""Daily orientation check: did any recent SIGNAL trade a flipped pair?

Compares, for every ticker that signalled in the window, the Kalshi outcome name against the Pinnacle
selection name that our token pointed at. Both are already recorded side by side in `pair_ledger.jsonl`
(`kalshi_outcome` and `stored_name`), so this needs NO API, no fixture mapping and no network at all.

WHY THIS AND NOT THE ODDSPAPI CHECKS
------------------------------------
`verify_pairs.py` does an authoritative outcome-ID join, but only against the LIVE pair file, and it costs
billable calls. `verify_signals.py` reconstructs past pairs from price history, but oddspapi maps only ~43%
of past fixtures so it tops out around a third of the signals. This one covers every signal that has a
ledger entry — 31 of 31 on 2026-08-26 — for free, which is what makes it usable as a DAILY habit.

ITS LIMIT, STATED PLAINLY
-------------------------
It is a NAME comparison, and name matching is what produced the flips in the first place. It catches the
common case (the token names a different player entirely) and is blind to the rare one (the venue's own
catalog is mis-labelled). Matching on TOKENS of >=3 characters rather than substrings is deliberate:
"Felipe Meligeni Alves" vs "Felipe Meligeni Rodrigues Alves" and "Jazmin" vs "Jasmin Ortenzi" are the same
player, and a substring test strips both — it did, once.

Compare against the raw `stored_name` recorded at pairing time, never a name derived from our own `teams`
map: that would be circular, and the circular version once reported zero while three rows were mis-oriented.

Exit code 2 if anything is flipped, so it can gate a cron.
"""
from __future__ import annotations
import argparse, csv, datetime as dt, glob, io, json, os, re, sys


def ntok(x: str) -> set:
    return {w for w in re.sub(r"[^a-z ]", " ", (x or "").lower()).split() if len(w) >= 3}


def main() -> int:
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=root)
    ap.add_argument("--hours", type=float, default=24.0, help="look back this many hours (default 24)")
    ap.add_argument("--since", default="", help="explicit ISO cutoff, overrides --hours")
    ap.add_argument("--all", action="store_true", help="check every signal ever logged")
    ap.add_argument("--quiet", action="store_true", help="only print the summary and any flips")
    a = ap.parse_args()

    since = ("" if a.all else
             (a.since or (dt.datetime.now() - dt.timedelta(hours=a.hours)).strftime("%Y-%m-%dT%H:%M")))

    sig = {}
    for f in sorted(glob.glob(os.path.join(a.root, "EvTelemetry_*.csv"))):
        for r in csv.DictReader(io.open(f, encoding="utf-8", errors="replace")):
            if r.get("Decision", "").strip() != "SIGNAL":
                continue
            tk, ts = r.get("Ticker", "").strip(), r.get("Timestamp", "")
            if tk and ts >= since and (tk not in sig or ts < sig[tk]):
                sig[tk] = ts

    led = {}
    lp = os.path.join(a.root, "pair_ledger.jsonl")
    if os.path.exists(lp):
        for line in io.open(lp, encoding="utf-8"):
            try:
                d = json.loads(line)
            except Exception:
                continue
            if d.get("ticker"):
                led.setdefault(d["ticker"], d)          # FIRST entry = the pairing in force when it signalled

    ok = bad = miss = 0
    flips = []
    print(f"[NAMES] signals since {since or 'the beginning'} - {len(sig)} ticker(s)\n")
    for tk, ts in sorted(sig.items(), key=lambda kv: kv[1]):
        e = led.get(tk) or {}
        out, pin = e.get("kalshi_outcome") or "", e.get("stored_name") or ""
        if not out or not pin:
            miss += 1
            if not a.quiet:
                print(f"   ?        {ts[5:16]}  {tk[:46]:<46} (no ledger entry - cannot judge)")
            continue
        if ntok(out) & ntok(pin):
            ok += 1
            if not a.quiet:
                print(f"   OK       {ts[5:16]}  {tk[:46]:<46} {out[:24]:<24} {pin[:24]}")
        else:
            bad += 1
            flips.append((ts, tk, out, pin))
            print(f"   **FLIP** {ts[5:16]}  {tk[:46]:<46} Kalshi '{out}' vs token '{pin}'")

    print(f"\n[NAMES] AGREE {ok}   FLIPPED {bad}   unjudgeable {miss}")
    if bad:
        print("\n   Add these to ev_misoriented.json so --resolve stops grading them:")
        for _ts, tk, _o, _p in flips:
            print(f'     "{tk}",')
    elif ok:
        print("   No orientation errors. Note this is a NAME check - see the module docstring for its limit.")
    return 2 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
