"""Score the PRE-REGISTERED Kalshi basketball forward test (rules: bb_forward_test.json, registered 2026-11-09 15:00 UTC).

The question: do basketball signals that follow a big Pinnacle jump (MoveRegime PINNACLE_LED) keep their edge
5 minutes later? Basketball is shadow-only, so nothing was bought: each side is scored at the first deep
PINNACLE_LED signal - where a bot running that filter would have bought - on section 11's cushion, the quoted edge
plus Pinnacle's own move by T+300.

Only sides that signal AFTER registration count, and every threshold comes from the JSON, whose SHA-256 is printed so
a later edit to the rules shows. The verdict uses exactly the first N clean readings in entry order, so running this
every day cannot move it. Read-only: nothing here trades.

    python bb_forward_test.py
    python bb_forward_test.py --line                  # one summary line
    python bb_forward_test.py --rules <copy.json>     # replay with a scratch copy (logic checks only)
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
import sys
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
RULES = os.path.join(HERE, "bb_forward_test.json")
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


def settlements():
    """ticker -> 'yes' / 'no' for markets Kalshi has decided (last record wins, as in the report)."""
    out = {}
    path = os.path.join(HERE, "ev_settlements.jsonl")
    if not os.path.exists(path):
        return out
    for line in open(path, encoding="utf-8"):
        try:
            r = json.loads(line)
        except ValueError:
            continue
        ok = r.get("status") in ("finalized", "determined") and r.get("result") in ("yes", "no")
        out[r["ticker"]] = r["result"] if ok else None
    return out


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
    series = set(pop["series"])
    t0 = dt.datetime.fromisoformat(start)
    now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    days = [(t0.date() + dt.timedelta(days=i)).strftime("%Y%m%d") for i in range((now.date() - t0.date()).days + 1)]

    sides = defaultdict(list)                          # (ticker, side) -> qualifying SIGNAL rows
    fu = defaultdict(dict)
    for d in days:
        for f in sorted(glob.glob(os.path.join(HERE, f"EvShadowTelemetry_{d}*.csv"))):
            for r in csv.DictReader(open(f, encoding="utf-8", newline="")):
                if (r.get("Decision") == pop["decision"] and r["Timestamp"][:19] >= start
                        and r["Ticker"].split("-")[0] in series
                        and r.get("InPlay") in ("1", "True", "true")
                        and pop["ask_min"] <= num(r, "KalshiRestAsk") <= pop["ask_max"]):
                    sides[(r["Ticker"], r["Side"])].append(r)
        for f in sorted(glob.glob(os.path.join(HERE, f"EvShadowFollowUp_{d}*.csv"))):
            for r in csv.DictReader(open(f, encoding="utf-8", newline="")):
                if r.get("Decision") == "SIGNAL":
                    # the follow-up stamps its entry a few microseconds off the telemetry row: match to the second
                    fu[(r["Ticker"], r["Side"], r["EntryUtc"][:19])][round(num(r, "AgeSec"))] = r

    def reading(r):
        rs = fu.get((r["Ticker"], r["Side"], r["Timestamp"][:19]), {})
        c = [a for a in rs if abs(a - 300) <= 30]
        if not c:
            return None
        b = rs[min(c, key=lambda a: abs(a - 300))]
        ok = b.get("PinState") == "open" and num(b, "NowPTrue") == num(b, "NowPTrue") and num(b, "EntryEvCents") == num(b, "EntryEvCents")
        return b if ok else None

    def score(keep):
        """One entry per side: its first deep signal that passes `keep`. -> (scored, n_sides)"""
        out, n_sides = [], 0
        for rows in sides.values():
            taken = next((r for r in sorted(rows, key=lambda r: r["Timestamp"])
                          if num(r, "WsDepthToLimit") >= pop["deep_min_contracts"] and keep(r)), None)
            if taken is None:
                continue
            n_sides += 1
            b = reading(taken)
            if b is None:
                continue
            ev, p0, p300 = num(b, "EntryEvCents"), num(b, "EntryPTrue"), num(b, "NowPTrue")
            out.append(dict(at=taken["Timestamp"], ticker=taken["Ticker"], side=taken["Side"], cushion=ev + (p300 - p0) * 100,
                            ev=ev, drift=(p300 - p0) * 100, p0=p0, p300=p300))
        out.sort(key=lambda x: x["at"])
        return out, n_sides

    led = lambda r: r.get("MoveRegime") == pop["regime"]
    obs, n_sides = score(led)
    n = len(obs)
    rate = n / max(1e-9, (now - t0).total_seconds() / 86400)
    first_n, ext_n = dec["first_n"], dec["extend_to_n"]

    verdict, m2 = "", None
    if n >= first_n:
        m1 = mean_se([x["cushion"] for x in obs[:first_n]])[0]
        if m1 >= dec["go_if_mean_at_or_above"]:
            verdict = "GO"
        elif m1 <= dec["no_go_if_mean_at_or_below"]:
            verdict = "NO-GO"
        elif n < ext_n:
            verdict = f"INCONCLUSIVE at {first_n} - extended to {ext_n} readings ({n} so far)"
        else:
            m2 = mean_se([x["cushion"] for x in obs[:ext_n]])[0]
            verdict = "GO" if m2 >= dec["go_at_extension_if_mean_at_or_above"] else "NO-GO"

    if "--line" in sys.argv:
        m, se = mean_se([x["cushion"] for x in obs])
        head = f"VERDICT: {verdict}" if verdict else f"{n}/{first_n} readings"
        print(f"{head} | big-jump cushion {m:+.2f}c +/-{se:.2f} (n={n})" if n >= 2 else f"{head} | too few readings yet")
        return 0

    print(f"KALSHI BASKETBALL FORWARD TEST - signals after a big Pinnacle jump ({pop['regime']})   "
          f"registered {rules['registered_at']}   rules sha256 {sha}")
    print(f"   sides with a deep {pop['regime']} signal since registration: {n_sides}; clean 5-minute readings: {n}")

    def block(sample, label):
        print(f"   {label}")
        print(f"      cushion   {fmt([x['cushion'] for x in sample])}   (bought {mean_se([x['ev'] for x in sample])[0]:+.2f}c, "
              f"Pinnacle drift by 5 min {mean_se([x['drift'] for x in sample])[0]:+.2f}c)")

    if n < first_n:
        eta = (t0 + dt.timedelta(days=first_n / rate)).strftime("%a %b %d") if rate > 0 else "unknown"
        print(f"   progress: {n} / {first_n} readings for the first look, ~{rate:.1f}/day -> around {eta}")
        if n >= 3:
            block(obs, "INTERIM (not a verdict - the rules decide only at the decision point):")
    else:
        block(obs[:first_n], f"DECISION POINT - the first {first_n} readings "
                             f"(GO >= {dec['go_if_mean_at_or_above']:+.1f}c, NO-GO <= {dec['no_go_if_mean_at_or_below']:+.1f}c):")
        if m2 is not None:
            block(obs[:ext_n], f"SECOND DECISION POINT - the first {ext_n} readings "
                               f"(GO >= {dec['go_at_extension_if_mean_at_or_above']:+.1f}c):")
    if verdict:
        print(f"   VERDICT: {verdict}")

    # BY DAY - display only; one day is noise, read the running total
    by_day = defaultdict(list)
    for x in obs:
        by_day[x["at"][:10]].append(x["cushion"])
    if by_day:
        print("   BY DAY (display only):")
        run = []
        for d in sorted(by_day):
            run += by_day[d]
            m, se = mean_se(by_day[d])
            rm, _ = mean_se(run)
            print(f"      {d[5:]}   cushion {m:+6.2f}" + (f" +/-{se:4.2f}" if se == se else " " * 8)
                  + f" (n={len(by_day[d]):2})   running {rm:+.2f} (n={len(run)})")

    # supporting only - none of these decides anything
    allx, _ = score(lambda r: True)
    rest, _ = score(lambda r: not led(r))
    print(f"   supporting: unfiltered (first deep signal per side, any regime) {fmt([x['cushion'] for x in allx])}; "
          f"the rest (first deep non-{pop['regime']} signal) {fmt([x['cushion'] for x in rest])}")
    res = settlements()
    st = [(x, 1.0 if (res.get(x["ticker"]) == "yes") == (x["side"] == "YES") else 0.0) for x in obs if res.get(x["ticker"])]
    if st:
        won = sum(w for _, w in st)
        print(f"   supporting: settled {len(st)} of the scored sides - won {won:.0f} vs {sum(x['p0'] for x, _ in st):.1f} at entry P_true; "
              f"realised {fmt([(w - x['p0']) * 100 + x['ev'] for x, w in st])} per contract; "
              f"outcome vs Pinnacle at T+300 {fmt([(w - x['p300']) * 100 for x, w in st])} (~0 = the 5-minute price was fair)")
    print("   Rules: bb_forward_test.json. Read-only; nothing here trades.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
