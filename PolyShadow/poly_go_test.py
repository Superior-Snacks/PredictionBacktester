"""Score the PRE-REGISTERED Polymarket go/no-go (rules: poly_go_test.json, registered 2026-10-28 21:00 UTC).

The question: do Polymarket's tennis signals pay once the 1-second taker delay has been paid for? Each side is scored
at the first signal whose order would still have filled after the delay (a miss is free), on the same 'as filled'
cushion as poly_shadow_report.py: the edge at the price paid plus Pinnacle's own move by T+300.

Only sides that signal AFTER registration count, and every threshold comes from the JSON, whose SHA-256 is printed so
a later edit to the rules shows. The verdict uses exactly the first N clean readings in entry order, so running this
every day cannot move it. Read-only: nothing here trades, and nothing touches the Kalshi bot.

    python PolyShadow/poly_go_test.py
    python PolyShadow/poly_go_test.py --rules <copy.json>    # replay with a scratch copy (logic checks only)
"""
from __future__ import annotations

import csv
import datetime as dt
import glob
import hashlib
import json
import math
import os
import statistics
import subprocess
import sys
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
RULES = os.path.join(HERE, "poly_go_test.json")
DATA = os.path.join(HERE, "data")
csv.field_size_limit(10 ** 9)


def num(r, k):
    try:
        return float(r.get(k) or "nan")
    except ValueError:
        return float("nan")


def mean_se(v):
    if len(v) < 2:
        return (v[0] if v else float("nan")), float("nan")
    return sum(v) / len(v), statistics.stdev(v) / math.sqrt(len(v))


def fmt(v):
    m, se = mean_se(v)
    return "n/a" if m != m else (f"{m:+.2f}c" if se != se else f"{m:+.2f}c +/-{se:.2f} (n={len(v)})")


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    path = RULES
    if "--rules" in sys.argv:                          # a scratch copy, to replay old data; never the registered file
        path = sys.argv[sys.argv.index("--rules") + 1]
    raw = open(path, "rb").read()
    rules = json.loads(raw)
    sha = hashlib.sha256(raw).hexdigest()[:12]
    start = rules["registered_at"].replace("Z", "")
    pop, dec = rules["population"], rules["decision"]
    filled = set(pop["filled_after_delay"])
    t0 = dt.datetime.fromisoformat(start)
    now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    days = [(t0.date() + dt.timedelta(days=i)).strftime("%Y%m%d") for i in range((now.date() - t0.date()).days + 1)]

    sides = defaultdict(list)                          # (sport, ticker, side) -> SIGNAL rows
    fu = defaultdict(dict)
    for d in days:
        for f in sorted(glob.glob(os.path.join(DATA, f"PolyShadowTelemetry_{d}*.csv"))):
            for r in csv.DictReader(open(f, encoding="utf-8", newline="")):
                if (r.get("Decision") == pop["decision"] and r["Timestamp"][:19] >= start
                        and r.get("InPlay") in ("1", "True", "true")
                        and pop["ask_min"] <= num(r, "RestAsk") <= pop["ask_max"]):
                    sides[(r.get("Sport", ""), r["Ticker"], r["Side"])].append(r)
        for f in sorted(glob.glob(os.path.join(DATA, f"PolyShadowFollowUp_{d}*.csv"))):
            for r in csv.DictReader(open(f, encoding="utf-8", newline="")):
                if r.get("Decision") == "SIGNAL":
                    fu[(r["Ticker"], r["Side"], r["EntryUtc"][:19])][round(num(r, "AgeSec"))] = r

    def reading(r):
        rs = fu.get((r["Ticker"], r["Side"], r["Timestamp"][:19]), {})
        c = [a for a in rs if abs(a - 300) <= 30]
        if not c:
            return None
        b = rs[min(c, key=lambda a: abs(a - 300))]
        return b if b.get("PinState") == "open" and num(b, "NowPTrue") == num(b, "NowPTrue") else None

    def score(sport):
        out, n_sides, n_filled = [], 0, 0             # out: (entry time, cushion, edge at fill, drift)
        for (sp, _, _), rows in sides.items():
            if sp != sport:
                continue
            n_sides += 1
            taken = next((r for r in sorted(rows, key=lambda r: r["Timestamp"]) if r.get("AfterDelay") in filled), None)
            if taken is None:
                continue
            n_filled += 1
            b = reading(taken)
            if b is None:
                continue
            edge = num(taken, "DelayedEvAtEntryP") * 100
            drift = (num(b, "NowPTrue") - num(b, "EntryPTrue")) * 100
            if edge == edge and drift == drift:
                out.append((taken["Timestamp"], edge + drift, edge, drift))
        out.sort()
        return out, n_sides, n_filled

    obs, n_sides, n_filled = score(pop["sport"])
    n = len(obs)
    rate = n / max(1e-9, (now - t0).total_seconds() / 86400)
    first_n, ext_n = dec["first_n"], dec["extend_to_n"]
    print(f"POLYMARKET GO/NO-GO - {pop['sport']} signals as filled after the taker delay   "
          f"registered {rules['registered_at']}   rules sha256 {sha}")
    print(f"   signalled sides since registration: {n_sides}; would have filled after the delay: {n_filled}; "
          f"clean 5-minute readings: {n}")

    def block(sample, label):
        print(f"   {label}")
        print(f"      as-filled cushion   {fmt([x[1] for x in sample])}   "
              f"(edge at the fill {mean_se([x[2] for x in sample])[0]:+.2f}c, Pinnacle drift by 5 min "
              f"{mean_se([x[3] for x in sample])[0]:+.2f}c)")
        return mean_se([x[1] for x in sample])[0]

    verdict = ""
    if n < first_n:
        eta = (t0 + dt.timedelta(days=first_n / rate)).strftime("%a %b %d") if rate > 0 else "unknown"
        print(f"   progress: {n} / {first_n} readings for the decision, ~{rate:.1f}/day -> around {eta}")
        if n >= 3:
            block(obs, "INTERIM (not a verdict - the rules decide only at the decision point):")
    else:
        m1 = block(obs[:first_n], f"DECISION POINT - the first {first_n} readings:")
        if m1 >= dec["go_if_mean_at_or_above"]:
            verdict = "GO"
        elif m1 <= dec["no_go_if_mean_at_or_below"]:
            verdict = "NO-GO"
        elif n < ext_n:
            verdict = f"INCONCLUSIVE at {first_n} - extended to {ext_n} readings ({n} so far)"
        else:
            m2 = block(obs[:ext_n], f"SECOND DECISION POINT - the first {ext_n} readings:")
            verdict = "GO" if m2 >= dec["midpoint_at_extension"] else "NO-GO"

    # THE PRECONDITION: the Kalshi forward test, run as-is (same oracle - a confirmed shift voids a GO here)
    try:
        p = subprocess.run([sys.executable, os.path.join(ROOT, "ev_forward_test.py"), "--line"], cwd=ROOT,
                           capture_output=True, timeout=900)
        lines = (p.stdout or b"").decode("utf-8", "replace").strip().splitlines()
        kline = lines[-1] if lines else "(no output)"
    except Exception as e:
        kline = f"(could not run: {type(e).__name__})"
    if verdict == "GO":
        if kline.startswith("VERDICT: SHIFT"):
            verdict = "NO-GO - the Kalshi forward test confirmed the shift (same Pinnacle oracle)"
        elif not kline.startswith("VERDICT:"):
            verdict = "GO, pending the Kalshi forward test"
    if verdict:
        print(f"   VERDICT: {verdict}")
    print(f"   Kalshi forward test (precondition): {kline}")

    # BY DAY - display only; one day is noise, read the running total
    by_day = defaultdict(list)
    for x in obs:
        by_day[x[0][:10]].append(x[1])
    if by_day:
        print("   BY DAY (display only):")
        run = []
        for d in sorted(by_day):
            run += by_day[d]
            m, se = mean_se(by_day[d])
            rm, _ = mean_se(run)
            print(f"      {d[5:]}   cushion {m:+6.2f}" + (f" +/-{se:4.2f}" if se == se else " " * 8)
                  + f" (n={len(by_day[d]):2})   running {rm:+.2f} (n={len(run)})")

    # supporting only
    bb, bb_sides, bb_filled = score("basketball")
    print(f"   supporting: {n_filled} of {n_sides} tennis sides would have filled after the delay"
          + (f" ({100 * n_filled / n_sides:.0f}%)" if n_sides else "")
          + f"; basketball as filled {fmt([x[1] for x in bb])} ({bb_filled} of {bb_sides} sides filled)")
    print("   Rules: PolyShadow/poly_go_test.json. Read-only; nothing here trades.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
