"""Score the pre-registered tennis give-back forward test (rules: ev_forward_test.json, registered 2026-10-27).

Only signals entered AFTER the registration count, and every threshold comes from the JSON, so the test cannot be
tuned after the fact; the file's SHA-256 is printed so a later edit to the rules shows. Each verdict uses exactly the
first N clean readings in entry order (N = first_n, then extend_to_n), so running this every day cannot move it.
Read-only; nothing here changes what the bot trades.

    python ev_forward_test.py           # or `ev forward` in Discord
    python ev_forward_test.py --line    # one summary line (the end-of-day Discord drop posts it daily)
"""
from __future__ import annotations

import csv
import datetime as dt
import glob
import hashlib
import io
import json
import math
import os
import statistics
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
RULES = os.path.join(HERE, "ev_forward_test.json")
csv.field_size_limit(10 ** 9)


def num(r, k):
    try:
        return float(r.get(k) or "nan")
    except ValueError:
        return float("nan")


def is_tennis(t):
    s = t.split("-")[0]
    return any(x in s for x in ("ATP", "WTA", "ITF"))


def mean_se(v):
    if len(v) < 2:
        return (v[0] if v else float("nan")), float("nan")
    return sum(v) / len(v), statistics.stdev(v) / math.sqrt(len(v))


def fmt(v):
    m, se = mean_se(v)
    return "n/a" if m != m else (f"{m:+.2f}c" if se != se else f"{m:+.2f}c +/-{se:.2f} (n={len(v)})")


def slope(pts):
    n = len(pts)
    if n < 3:
        return float("nan"), float("nan")
    mx = sum(x for x, _ in pts) / n
    my = sum(y for _, y in pts) / n
    sxx = sum((x - mx) ** 2 for x, _ in pts)
    if sxx <= 0:
        return float("nan"), float("nan")
    b = sum((x - mx) * (y - my) for x, y in pts) / sxx
    res = [y - (my + b * (x - mx)) for x, y in pts]
    return b, math.sqrt(sum(e * e for e in res) / (n - 2) / sxx)


def main() -> int:
    import sys
    # --line: the full output is still produced (so the verdict logic runs exactly as always), just captured, and
    # one summary line is printed in its place.
    line_mode = "--line" in sys.argv
    real_stdout = sys.stdout
    if line_mode:
        sys.stdout = io.StringIO()
    path = RULES
    if "--rules" in sys.argv:                          # a scratch copy, to replay old data; never the registered file
        path = sys.argv[sys.argv.index("--rules") + 1]
    raw = open(path, "rb").read()
    rules = json.loads(raw)
    sha = hashlib.sha256(raw).hexdigest()[:12]
    start = rules["registered_at"].replace("Z", "")
    dec, grad, ponly, gap_rule = rules["decision"], rules["gradient"], rules["pinnacle_only"], rules["gap_rule"]
    t0 = dt.datetime.fromisoformat(start)
    today = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    days = [(t0.date() + dt.timedelta(days=i)).strftime("%Y%m%d") for i in range((today.date() - t0.date()).days + 1)]

    first, fu = {}, defaultdict(dict)
    for d in days:
        f = os.path.join(HERE, f"EvTelemetry_{d}.csv")
        if os.path.exists(f):
            for r in csv.DictReader(open(f, encoding="utf-8", newline="")):
                if (r.get("Decision") == "SIGNAL" and r["Timestamp"][:19] >= start and is_tennis(r["Ticker"])
                        and r.get("InPlay") in ("1", "True", "true") and 0.30 <= num(r, "KalshiRestAsk") <= 0.80
                        and (r.get("MarketType") or "moneyline") in ("", "moneyline")):
                    first.setdefault((r["Ticker"], r["Side"]), r)
        f = os.path.join(HERE, f"EvFollowUp_{d}.csv")
        if os.path.exists(f):
            for r in csv.DictReader(open(f, encoding="utf-8", newline="")):
                if r.get("Decision") == "SIGNAL":
                    fu[(r["Ticker"], r["Side"], r["EntryUtc"][:19])][round(float(r["AgeSec"]))] = r

    def at(r, age, tol):
        rs = fu.get((r["Ticker"], r["Side"], r["Timestamp"][:19]), {})
        c = [a for a in rs if abs(a - age) <= tol]
        return rs[min(c, key=lambda a: abs(a - age))] if c else None

    def clean(f):
        return f is not None and f.get("PinState") == "open" and num(f, "NowPTrue") == num(f, "NowPTrue")

    obs = []                                           # (entry time, gap, give-back, pinnacle-only or nan)
    for r in sorted(first.values(), key=lambda r: r["Timestamp"]):
        b = at(r, 300, 30)
        if not clean(b):
            continue
        gap = (num(r, "PTrueUsed") - num(r, "KalshiRestAsk") - num(r, "FeePerContract")) * 100
        gb = (num(b, "NowPTrue") - num(b, "EntryPTrue")) * 100
        po = float("nan")
        a = at(r, 60, 10)
        if clean(a):
            m60 = (num(a, "NowAsk") + num(a, "NowBid")) / 2
            m300 = (num(b, "NowAsk") + num(b, "NowBid")) / 2
            if m60 == m60 and m300 == m300:
                po = ((num(b, "NowPTrue") - num(a, "NowPTrue")) - (m300 - m60)) * 100
        obs.append((r["Timestamp"], gap, gb, po))

    n = len(obs)
    elapsed = max(1e-9, (today - t0).total_seconds() / 86400)
    rate = n / elapsed
    print(f"FORWARD TEST - tennis give-back   registered {rules['registered_at']}   rules sha256 {sha}")
    print(f"   new tennis signals since registration: {len(first)} sides; clean 5-minute readings {n}")

    def block(sample, label):
        gb = [x[2] for x in sample]
        g = [x[1] for x in sample]
        b, se = slope([(x[1], x[2]) for x in sample])
        po = [x[3] for x in sample if x[3] == x[3]]
        lo = [x for x in sample if x[1] <= gap_rule["skip_gap_above_cents"]]
        hi = [x for x in sample if x[1] > gap_rule["skip_gap_above_cents"]]

        def cushion(grp):
            if len(grp) < 2:
                return float("nan"), float("nan"), len(grp)
            vals = [x[1] + x[2] for x in grp]              # gap bought + give-back, per signal
            m, s = mean_se(vals)
            return m, s, len(grp)
        print(f"   {label}")
        print(f"      give-back at 5 min   {fmt(gb)}   (normal {rules['hypotheses']['normal_cents']:+.1f}c, "
              f"streak {rules['hypotheses']['streak_cents']:+.1f}c); mean gap bought {mean_se(g)[0]:+.2f}c")
        # DISPLAY ONLY (added 2026-10-28; no rule reads it): the number the user follows day to day.
        print(f"      cushion (gap + give-back) {fmt([x[1] + x[2] for x in sample])}   "
              f"(before 10-20 about +2c, the 10-20 streak about 0c)")
        tt = b / se if se == se and se > 0 else float("nan")
        print(f"      gap gradient         {b:+.2f} +/-{se:.2f} per cent of gap (t={tt:+.1f})   "
              f"(before 10-20 {grad['before_slope']:+.2f}, streak {grad['streak_slope']:+.2f})")
        print(f"      Pinnacle-only 60s->5m {fmt(po)}   (normal {ponly['normal_cents']:+.1f}c)")
        # THE TWO GAP BANDS - DISPLAY ONLY (added 2026-10-27, after registration; no rule reads it). Wide gaps
        # carry a bigger bought edge AND a bigger give-back, so pooled they can mask the core band. Cut at the
        # registered 3c line, under vs at-or-above (a gap of exactly 3.00c does not occur: P_true is continuous).
        cut = gap_rule["skip_gap_above_cents"]
        for i, (lab, grp) in enumerate(((f"gap <{cut:g}c ", [x for x in sample if x[1] < cut]),
                                        (f"gap >={cut:g}c", [x for x in sample if x[1] >= cut]))):
            head = "by gap band" if i == 0 else ""
            if len(grp) < 2:
                print(f"      {head:<21}{lab}  n={len(grp)}")
                continue
            gm, gs = mean_se([x[2] for x in grp])
            cm, cs = mean_se([x[1] + x[2] for x in grp])
            print(f"      {head:<21}{lab}  give-back {gm:+.2f} +/-{gs:.2f}   bought {mean_se([x[1] for x in grp])[0]:+.2f}c"
                  f" -> cushion {cm:+.2f} +/-{cs:.2f} (n={len(grp)})")
        cl, sl, nl = cushion(lo)
        ch, sh, nh = cushion(hi)
        print(f"      gap rule (skip > {gap_rule['skip_gap_above_cents']:.0f}c): cushion gap<=3c {cl:+.2f} +/-{sl:.2f} (n={nl})   "
              f"gap>3c {ch:+.2f} +/-{sh:.2f} (n={nh})")
        return mean_se(gb)[0], b, tt, mean_se(po)[0], (cl, ch, sh)

    first_n, ext_n = dec["first_n"], dec["extend_to_n"]
    if n < first_n:
        eta = (t0 + dt.timedelta(days=first_n / rate)).strftime("%a %b %d") if rate > 0 else "unknown"
        print(f"   progress: {n} / {first_n} readings for the first decision, ~{rate:.1f}/day -> around {eta}")
        if n >= 3:
            block(obs, "INTERIM (not a verdict - the rules decide only at the first decision point):")
    else:
        m1, b1, t1, po1, cush1 = block(obs[:first_n], f"FIRST DECISION POINT - the first {first_n} readings:")
        if m1 <= dec["shift_if_mean_at_or_below"]:
            verdict, used = "SHIFT CONFIRMED - the bigger give-back is real, not luck", (b1, t1, po1, cush1)
        elif m1 >= dec["normal_if_mean_at_or_above"]:
            verdict, used = "NORMAL - the streak was luck; the give-back is back at the usual level", (b1, t1, po1, cush1)
        elif n < ext_n:
            eta = (t0 + dt.timedelta(days=ext_n / rate)).strftime("%a %b %d") if rate > 0 else "unknown"
            verdict, used = (f"INCONCLUSIVE at {first_n} - extended to {ext_n} readings ({n} so far, around {eta})", None)
        else:
            m2, b2, t2, po2, cush2 = block(obs[:ext_n], f"SECOND DECISION POINT - the first {ext_n} readings:")
            verdict = ("SHIFT CONFIRMED - the bigger give-back is real" if m2 <= dec["midpoint_at_extension"]
                       else "NORMAL - the streak was luck")
            used = (b2, t2, po2, cush2)
        print(f"   VERDICT: {verdict}")
        if used:
            b, tt, po, (cl, ch, sh) = used
            gv = ("CONFIRMED - bigger gaps give back more" if b <= grad["confirmed_if_slope_at_or_below"]
                  and tt <= grad["and_t_at_or_below"] else
                  "ABSENT - no link to the gap, as before 10-20" if b >= grad["absent_if_slope_at_or_above"]
                  else "inconclusive")
            print(f"   gap gradient: {gv}")
            print("   Pinnacle itself: " + ("WORSE - its own price now over-moves more"
                                          if po <= ponly["pinnacle_itself_worse_if_at_or_below"] else "as before"))
            worth = ch == ch and sh == sh and sh > 0 and ch < 0 and ch / sh <= -1.5 and cl > 0
            print("   gap rule: " + ("worth a live test" if worth else "not supported by this data"))

    # BY DAY - DISPLAY ONLY (added 2026-10-28 for the daily check; no rule reads it). Days are UTC, by entry time.
    by_day = defaultdict(list)
    for x in obs:
        by_day[x[0][:10]].append(x[1] + x[2])
    if by_day:
        print("   BY DAY (display only; one day is about +/-2c of noise, so read the running total):")
        run = []
        for d in sorted(by_day):
            v = by_day[d]
            run += v
            m, se = mean_se(v)
            rm, rse = mean_se(run)
            tail = " (from 16:30)" if d == start[:10] else ""
            print(f"      {d[5:]}{tail:<13} cushion {m:+6.2f}" + (f" +/-{se:4.2f}" if se == se else " " * 8)
                  + f" (n={len(v):2})   running {rm:+.2f}" + (f" +/-{rse:.2f}" if rse == rse else "") + f" (n={len(run)})")

    # settlement on new fills - supporting only
    res = {}
    try:
        for line in open(os.path.join(HERE, "ev_settlements.jsonl"), encoding="utf-8"):
            try:
                j = json.loads(line)
            except ValueError:
                continue
            if j.get("result") in ("yes", "no"):
                res[j["ticker"]] = j["result"]
    except OSError:
        pass
    fills = {}
    for f in sorted(glob.glob(os.path.join(HERE, "EvLive_*.csv"))):
        for r in csv.DictReader(open(f, encoding="utf-8", newline="")):
            if num(r, "FillCount") >= 1 and r["At"].replace(" ", "T")[:19] >= start:
                k = (r["Ticker"], r["Side"])
                if k not in fills or r["At"] < fills[k]["At"]:
                    fills[k] = r
    won = exp = var = pnl = ctr = 0.0
    settled = pend = 0
    for (tk, sd), r in fills.items():
        if tk not in res:
            pend += 1
            continue
        w = 1.0 if (res[tk] == "yes") == (sd == "YES") else 0.0
        p, q, px, fee = num(r, "PTrue"), num(r, "FillCount"), num(r, "AvgFillPrice"), num(r, "FeeChargedUsd")
        if px != px or px <= 0:
            px = num(r, "LimitPrice")
        settled += 1
        won += w
        exp += p
        var += p * (1 - p)
        pnl += q * (w - px) - (0.0 if fee != fee else fee)
        ctr += q
    if settled:
        print(f"   settled live fills since registration: {settled} ({pend} pending), won {won:.0f} vs {exp:.1f} expected "
              f"({(won - exp) / math.sqrt(var):+.1f} SD), realised {100 * pnl / ctr:+.2f}c/contract - supporting only")
    else:
        print(f"   settled live fills since registration: none yet ({pend} pending)")
    print("   Rules: ev_forward_test.json. Nothing here changes what the bot trades.")

    if line_mode:
        full = sys.stdout.getvalue()
        sys.stdout = real_stdout
        cm, cs = mean_se([x[1] + x[2] for x in obs])
        tv = [x[1] + x[2] for x in obs if x[0][:10] == today.strftime("%Y-%m-%d")]
        tm, ts = mean_se(tv)
        verdict = next((ln.strip() for ln in full.splitlines() if ln.strip().startswith("VERDICT:")), "")
        when = ""
        if not verdict and rate > 0:
            target = dec["first_n"] if n < dec["first_n"] else dec["extend_to_n"]
            when = f" (decision ~{(t0 + dt.timedelta(days=target / rate)):%a %b %d})"
        head = verdict if verdict else f"forward test {n}/{dec['first_n'] if n < dec['first_n'] else dec['extend_to_n']}{when}"
        cush = "cushion n/a" if cm != cm else (f"cushion since registration {cm:+.2f}c" + (f" +/-{cs:.2f}" if cs == cs else "")
                                                + f" (n={n}; before 10-20 about +2c, streak about 0c)")
        day = f"today {tm:+.2f}c (n={len(tv)})" if tv else "today: no readings yet"
        print(f"{head} | {cush} | {day}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
