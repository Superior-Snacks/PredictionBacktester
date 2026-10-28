#!/usr/bin/env python
"""Polymarket shadow report - the tennis radar's lines, scored on what poly_shadow.py logged.

Unit = one Polymarket outcome (first SIGNAL per market+outcome), as the bot's reports use the first signal per
ticker+side; a "deep" side is graded at its first signal with >= 15 shares at the break-even limit. Prices are the
REST ask plus Polymarket's taker fee (rate x p(1-p), from each market's own fee schedule).

  COVERAGE      paired markets watched, in-play hours (5-min snapshots), signalled sides per in-play hour
  DECISIONS     what the gates did with every candidate that cleared EV_MIN at REST
  EDGE          bought edge at REST, and how far the WS book read from REST
  TAKER DELAY   the book re-read one `secondsDelay` (1s) after the decision, when a taker order would really
                match: did a POLY_ORDER_SHARES order still fill (at the seen price / worse / partly / not at all),
                and the edge at the price it filled. "filled" lines grade each side at the first signal whose
                order would have filled - an order retried after every miss.
  CONVERGENCE   the bot's section 9 FILLABLE lines: T+20/T+60/T+300 ask move toward Pinnacle's price, came-to-us %
  ORACLE DRIFT  Pinnacle's own P_true change by T+300 (clean 'open' readings); CUSHION = edge + drift
  MATCH-NEUTRAL T+60 -> T+300: Pinnacle's move minus Polymarket's mid move (match events cancel)
  SETTLEMENT    edge after fees at settlement, and wins vs the sum of P_true
  ARB WINDOWS   Kalshi + Polymarket on the same match under $1 after both fees: how often, HOW LONG OPEN, margin,
                size, whether the Polymarket leg survived its taker delay, which venue was the cheap one

GAP BANDS (as the bot's resolve, sections 9/11/4f): CONVERGENCE, ORACLE DRIFT/CUSHION and the deep/filled settlement
rows are printed pooled and then split at EV_GAP_BAND_CENTS (3c) - under it the core band, at or above it the wide
gaps - so a few wide gaps cannot mask the core band. The gap is the edge bought after Polymarket's fee.

Settlement comes from Polymarket itself (Gamma /markets/{id}: outcomePrices once the market is closed), cached in
PolyShadow/data/poly_resolutions.json. A tennis walkover resolves 50-50 there and is graded at 0.5.

Usage: python PolyShadow/poly_shadow_report.py [--since 2026-10-27] [--sport tennis] [--no-api]
"""
from __future__ import annotations

import argparse
import csv
import glob
import json
import math
import os
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

import httpx

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from poly_shadow import GAMMA, Cfg, read_env  # noqa: E402  (same folder, same config)

csv.field_size_limit(10 ** 9)
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
DEEP = 15.0
FILLED = ("FULL_AT_SEEN", "FULL_WORSE", "PARTIAL")      # AfterDelay outcomes where the order got something


def D(r, k):
    try:
        v = float(r.get(k) or "nan")
        return v
    except ValueError:
        return float("nan")


def load(data: Path, prefix: str, since: str) -> list[dict]:
    out = []
    for f in sorted(glob.glob(str(data / f"{prefix}_*.csv"))):
        day = Path(f).stem.split("_")[1]
        if since and day < since.replace("-", ""):
            continue
        with open(f, encoding="utf-8", newline="") as fh:
            out += list(csv.DictReader(fh))
    return out


def stat(v):
    v = [x for x in v if x == x]
    n = len(v)
    if n == 0:
        return 0, float("nan"), float("nan")
    m = sum(v) / n
    se = math.sqrt(sum((x - m) ** 2 for x in v) / (n - 1)) / math.sqrt(n) if n > 1 else float("nan")
    return n, m, se


def show(v, unit="c") -> str:
    n, m, se = stat(v)
    if n == 0:
        return "n=0"
    t = m / se if se and se == se and se > 0 else float("nan")
    return (f"{m:+6.2f}{unit} +/-{se:4.2f} (t={t:+.1f}, n={n})" if se == se else f"{m:+6.2f}{unit} (n={n})")


def resolutions(markets: dict[str, list[str]], data: Path, use_api: bool) -> dict[str, float]:
    """asset id -> settlement value (1, 0 or 0.5). Only FINAL prices are kept; an open market is asked again."""
    path = data / "poly_resolutions.json"
    cache = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    todo = [m for m in markets if m not in cache]
    if use_api and todo:
        with httpx.Client(headers={"User-Agent": "poly-shadow-report/1.0"}, timeout=20.0) as http:
            for i, mid in enumerate(todo):
                try:
                    mk = http.get(f"{GAMMA}/markets/{mid}").json()
                except Exception:
                    continue
                if not mk.get("closed"):
                    continue
                try:
                    prices = [float(x) for x in json.loads(mk.get("outcomePrices") or "[]")]
                    ids = ([str(x) for x in mk.get("positionIds") or []] if (mk.get("version") or "v1") == "v2"
                           else [str(x) for x in json.loads(mk.get("clobTokenIds") or "[]")])
                except (ValueError, TypeError):
                    continue
                final = len(prices) == len(ids) == 2 and (sorted(prices) in ([0.0, 1.0], [0.5, 0.5]))
                if final:
                    cache[mid] = dict(zip(ids, prices))
                if i % 20 == 19:
                    time.sleep(0.5)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(cache), encoding="utf-8")
        os.replace(tmp, path)
    out = {}
    for mid, prices in cache.items():
        out.update(prices)
    return out


def main():
    ap = argparse.ArgumentParser(description="Polymarket shadow report")
    ap.add_argument("--since", default="", help="first UTC day to include, e.g. 2026-10-27")
    ap.add_argument("--sport", default="", help="only this sport (tennis, basketball, ...)")
    ap.add_argument("--no-api", action="store_true", help="do not ask Polymarket for settlements")
    args = ap.parse_args()
    cfg = Cfg(read_env())
    data = cfg.data
    cut = cfg.gap_band

    def bands(gap):
        """(label, keep) for pooled, under the gap-band cut, and at or above it - the bot's resolve split (sections 9,
        11, 4f). `gap` maps a row to its gap bought in cents; a row with no gap on record is in the pooled line only."""
        return (("all", lambda r: True),
                (f"gap <{cut:g}c", lambda r: gap(r) == gap(r) and gap(r) < cut),
                (f"gap >={cut:g}c", lambda r: gap(r) == gap(r) and gap(r) >= cut))

    tel = load(data, "PolyShadowTelemetry", args.since)
    fu = load(data, "PolyShadowFollowUp", args.since)
    snap = load(data, "PolyShadowSnap", args.since)
    if args.sport:
        tel = [r for r in tel if r["Sport"] == args.sport]
        fu = [r for r in fu if r["Sport"] == args.sport]
        snap = [r for r in snap if r["Sport"] == args.sport]
    if not tel and not snap:
        print(f"No shadow data in {data} yet. Is poly_shadow.py running?")
        arb_windows(data, args)
        return
    days = sorted({r["Timestamp"][:10] for r in tel + snap})
    print(f"POLYMARKET SHADOW  {days[0]} .. {days[-1]}   {len(tel)} candidate row(s), {len(fu)} follow-up row(s), "
          f"{len(snap)} snapshot row(s)   gates: ev>={cfg.ev_min * 100:.2f}c band {cfg.min_price:.2f}-{cfg.max_price:.2f}")

    # ── COVERAGE ──
    print("\nCOVERAGE")
    live_rows = Counter()
    watched = defaultdict(set)
    for r in snap:
        watched[r["Sport"]].add(r["Ticker"])
        if r["InPlay"] in ("1", "True", "true"):
            live_rows[r["Sport"]] += 1
    sig = [r for r in tel if r["Decision"] == "SIGNAL"]
    sides = defaultdict(list)
    for r in sorted(sig, key=lambda r: r["Timestamp"]):
        sides[(r["Ticker"], r["Side"])].append(r)
    for sp in sorted(set(watched) | {r["Sport"] for r in tel}):
        # one snapshot row per OUTCOME every EV_SNAPSHOT_MIN, two outcomes per market
        hours = live_rows[sp] / 2 * cfg.snapshot_min / 60
        ns = sum(1 for k, v in sides.items() if v[0]["Sport"] == sp)
        deep = sum(1 for k, v in sides.items() if v[0]["Sport"] == sp and any(D(x, "DepthToLimit") >= DEEP for x in v))
        rate = f"{ns / hours:.2f}/in-play h" if hours > 0 else "n/a"
        print(f"   {sp:11} {len(watched[sp]):4} market(s) watched, ~{hours:5.1f} in-play market-hours, "
              f"{ns:4} signalled side(s) ({rate}), {deep} deep (>= {DEEP:.0f} shares at the limit)")

    # ── DECISIONS ──
    print("\nDECISIONS (every REST-checked candidate)")
    dec = Counter((r["Sport"], r["Decision"]) for r in tel)
    for sp in sorted({k[0] for k in dec}):
        parts = sorted(((n, d) for (s, d), n in dec.items() if s == sp), reverse=True)
        print(f"   {sp:11} " + ", ".join(f"{d} {n}" for n, d in parts))

    # grade each side at its first deep signal, else its first signal (the bot's 4f convention)
    grade = {k: next((r for r in v if D(r, "DepthToLimit") >= DEEP), v[0]) for k, v in sides.items()}
    # ...and, Polymarket-real, at the first signal whose would-be order still FILLED one taker delay later - what an
    # order retried after each miss would have bought, at the price it would have paid
    taken = {k: next((r for r in v if r.get("AfterDelay") in FILLED), None) for k, v in sides.items()}
    deep_k = {k for k, v in sides.items() if any(D(r, "DepthToLimit") >= DEEP for r in v)}
    fu_by = defaultdict(dict)
    for r in fu:
        fu_by[(r["Ticker"], r["Side"], r["EntryUtc"][:19])][round(D(r, "AgeSec"))] = r

    def at(r, age, tol):
        rs = fu_by.get((r["Ticker"], r["Side"], r["Timestamp"][:19]), {})
        c = [a for a in rs if abs(a - age) <= tol]
        return rs[min(c, key=lambda a: abs(a - age))] if c else None

    for sp in sorted({v[0]["Sport"] for v in sides.values()}):
        ks = [k for k in sides if sides[k][0]["Sport"] == sp]
        print(f"\n{sp.upper()} - {len(ks)} signalled side(s), {len([k for k in ks if k in deep_k])} deep")
        # ── EDGE ──
        print(f"   EDGE          bought (all)  {show([D(grade[k], 'Ev') * 100 for k in ks])}")
        print(f"                 bought (deep) {show([D(grade[k], 'Ev') * 100 for k in ks if k in deep_k])}")
        print(f"                 REST - WS ask {show([D(grade[k], 'WsRestGapCents') for k in ks])}   "
              "(+ = the WS book read LOWER than REST, i.e. optimistic)")
        # ── TAKER DELAY ── each side at the signal it is graded on, then: would a retrying order have filled?
        ad = [grade[k] for k in ks if grade[k].get("AfterDelay") not in (None, "", "RECHECK_FAILED")]
        if ad:
            dist = Counter(r["AfterDelay"] for r in ad)
            print(f"   TAKER DELAY   book re-read {D(ad[0], 'TakerDelaySec'):g}s after the decision: "
                  + ", ".join(f"{s} {100 * dist[s] / len(ad):.0f}%" for s in
                              ("FULL_AT_SEEN", "FULL_WORSE", "PARTIAL", "GONE", "NO_DELAY") if dist[s])
                  + f"  (n={len(ad)})")
            print(f"                 ask move in the delay   {show([D(r, 'DelayAskMoveCents') for r in ad])}")
            fn, fd = stat([D(r, "FillSharesNow") for r in ad])[1], stat([D(r, "DelayedFillShares") for r in ad])[1]
            print(f"                 {D(ad[0], 'OrderShares'):.0f}-share order: {fn:.1f} sh fillable with no delay "
                  f"-> {fd:.1f} after it")
            print(f"                 edge at the fill        {show([D(r, 'DelayedEvAtFillP') * 100 for r in ad if r['AfterDelay'] in FILLED])}"
                  f"   (Pinnacle at fill time; as decided {show([D(r, 'Ev') * 100 for r in ad if r['AfterDelay'] in FILLED])})")
        n_taken = sum(1 for k in ks if taken[k] is not None)
        measured = [k for k in ks if any(r.get("AfterDelay") not in (None, "", "RECHECK_FAILED") for r in sides[k])]
        print(f"                 sides that would have FILLED (an order retried at each later signal): "
              f"{n_taken} of {len(measured)}"
              + (f"   ({len(ks) - len(measured)} signalled before the delay re-check existed)" if len(measured) < len(ks) else ""))
        # ── CONVERGENCE ── the bot's section 9 FILLABLE lines: first SIGNAL observation per side at each checkpoint,
        # the book showing depth at our limit at entry; ask move toward P_true; pooled and per gap band
        print("   CONVERGENCE   fillable signals, ask move toward P_true (first per side per checkpoint)")
        for cp in (20, 60, 300):
            firsts = {}
            for r in fu:
                if (r["Sport"] != sp or r["Decision"] != "SIGNAL" or round(D(r, "AgeSec")) != cp
                        or not D(r, "EntryDepth") > 0 or D(r, "NowAsk") != D(r, "NowAsk")):
                    continue
                k = (r["Ticker"], r["Side"])
                if k not in firsts or r["EntryUtc"] < firsts[k]["EntryUtc"]:
                    firsts[k] = r
            for lab, keep in bands(lambda r: D(r, "EntryEvCents")):
                mv = [(D(r, "NowAsk") - D(r, "EntryAsk")) * 100 for r in firsts.values() if keep(r)]
                moved = [x for x in mv if abs(x) > 1e-9]
                if mv:
                    print(f"                 T+{cp:<4}{lab:10}{show(mv)}"
                          + (f"   came to us {100 * sum(x > 0 for x in moved) / len(moved):.0f}%" if moved else ""))
        # ── ORACLE DRIFT + CUSHION ── per side at its graded signal; "as filled" = the price paid after the taker
        # delay, plus the same drift from the decision's P_true. Pooled and per gap band (the side's own gap).
        print("   ORACLE DRIFT  Pinnacle's own move by T+300; CUSHION = edge + drift")
        for lab, keep in bands(lambda r: D(r, "Ev") * 100):
            drift, cush, cush_f = [], [], []
            for k in ks:
                f = at(grade[k], 300, 30) if keep(grade[k]) else None
                if f and f["PinState"] == "open" and D(f, "NowPTrue") == D(f, "NowPTrue"):
                    d = (D(f, "NowPTrue") - D(f, "EntryPTrue")) * 100
                    drift.append(d)
                    cush.append(D(grade[k], "Ev") * 100 + d)
                r = taken[k]
                f = at(r, 300, 30) if r and keep(r) else None
                if f and f["PinState"] == "open" and D(f, "NowPTrue") == D(f, "NowPTrue"):
                    cush_f.append(D(r, "DelayedEvAtEntryP") * 100 + (D(f, "NowPTrue") - D(f, "EntryPTrue")) * 100)
            print(f"                 {lab:10}drift {show(drift)}   cushion {show(cush)}   as filled {show(cush_f)}")
        # ── MATCH-NEUTRAL ──
        dp, dk, dd = [], [], []
        for k in ks:
            a, b = at(grade[k], 60, 10), at(grade[k], 300, 30)
            if not a or not b or a["PinState"] != "open" or b["PinState"] != "open":
                continue
            p0, p1 = D(a, "NowPTrue"), D(b, "NowPTrue")
            m0 = (D(a, "NowAsk") + D(a, "NowBid")) / 2
            m1 = (D(b, "NowAsk") + D(b, "NowBid")) / 2
            if all(x == x for x in (p0, p1, m0, m1)):
                dp.append((p1 - p0) * 100)
                dk.append((m1 - m0) * 100)
                dd.append(((p1 - p0) - (m1 - m0)) * 100)
        print(f"   MATCH-NEUTRAL T+60->300 Pinnacle {show(dp)}")
        print(f"                 Polymarket mid  {show(dk)}")
        print(f"                 Pinnacle-only   {show(dd)}")

    # ── SETTLEMENT ──
    mk_assets = defaultdict(set)
    for r in tel:
        mk_assets[r["MarketId"]].add(r["AssetId"])
    res = resolutions(mk_assets, data, not args.no_api)
    print("\nSETTLEMENT (Polymarket's own resolution; walkovers 50-50 = 0.5)")
    def settle_line(sp, label, rows, cost_of, note=""):
        """One settlement row: edge after fees per share, wins vs P_true, and how many are still open."""
        pnl, won, exp, var, n = [], 0.0, 0.0, 0.0, 0
        for r in rows:
            v = res.get(r["AssetId"])
            if v is None:
                continue
            pnl.append((v - cost_of(r)) * 100)
            p = D(r, "PTrueUsed")
            won += v
            exp += p
            var += p * (1 - p)
            n += 1
        pending = len(rows) - n
        line = f"   {sp:11} {label:18} edge after fees {show(pnl)}"
        if n and var > 0:
            line += f"   won {won:.1f} vs P_true {exp:.1f} ({(won - exp) / math.sqrt(var):+.1f} SD)"
        print(line + note + (f"   [{pending} not settled yet]" if pending else ""))

    at_ask = lambda r: D(r, "RestAsk") + D(r, "FeePerShare")
    at_fill = lambda r: D(r, "DelayedVwap") + D(r, "DelayedFeePerShare")
    for sp in sorted({v[0]["Sport"] for v in sides.values()}):
        ks = [k for k in sides if sides[k][0]["Sport"] == sp]
        settle_line(sp, "all", [grade[k] for k in ks], at_ask)
        # DEEP and FILLED per gap band (the bot's 4f split): have the wide gaps paid what they quoted?
        for lab, keep in bands(lambda r: D(r, "Ev") * 100):
            settle_line(sp, "deep" if lab == "all" else f"  deep, {lab}",
                        [grade[k] for k in ks if k in deep_k and keep(grade[k])], at_ask)
        # the Polymarket-real lines: only sides an order would have FILLED after the delay, at the price it paid
        for lab, keep in bands(lambda r: D(r, "Ev") * 100):
            settle_line(sp, "filled" if lab == "all" else f"  filled, {lab}",
                        [taken[k] for k in ks if taken[k] is not None and keep(taken[k])], at_fill,
                        "   (at the post-delay fill price)" if lab == "all" else "")

    arb_windows(data, args)


def arb_windows(data: Path, args):
    """CROSS-VENUE ARB WINDOWS: one Kalshi side + the Polymarket outcome that completes it, under $1 after both taker
    fees. How often they open, how long they stay open, what they were worth, and whether the Polymarket leg survived
    its taker delay - the facts that decide whether there is anything to build here."""
    arb = load(data, "PolyArbWindows", args.since)
    if args.sport:
        arb = [r for r in arb if r["Sport"] == args.sport]
    print("\nCROSS-VENUE ARB WINDOWS (Kalshi + Polymarket on the same match, under $1 after both venues' taker fees)")
    if not arb:
        print("   none logged yet - the status line's 'arb ... closest' shows how near the pairs came")
        return
    for sp in sorted({r["Sport"] for r in arb}):
        ws = [r for r in arb if r["Sport"] == sp]
        durs = sorted(D(r, "DurationMs") / 1000 for r in ws)

        def q(p):
            return durs[min(len(durs) - 1, int(p * len(durs)))]

        def share(s):
            return 100 * sum(d >= s for d in durs) / len(durs)
        print(f"   {sp.upper()}  {len(ws)} window(s) on {len({(r['KalshiTicker'], r['PolyAssetId']) for r in ws})} pair(s)"
              f" in {len({r['EventTitle'] for r in ws})} match(es)")
        print(f"      open for       median {q(.5):.1f}s  p75 {q(.75):.1f}s  p90 {q(.9):.1f}s  longest {durs[-1]:.1f}s   "
              f"lasted >=1s {share(1):.0f}%  >=5s {share(5):.0f}%  >=30s {share(30):.0f}%")
        print(f"      margin         at open {show([D(r, 'MarginOpenCents') for r in ws])}   "
              f"best while open {show([D(r, 'MaxMarginCents') for r in ws])}")
        shares = sorted(D(r, "ArbShares") for r in ws)
        print(f"      size           median {shares[len(shares) // 2]:.0f} shares on both tops; "
              f"${sum(D(r, 'ProfitOpenUsd') for r in ws if D(r, 'ProfitOpenUsd') == D(r, 'ProfitOpenUsd')):.2f} on the table at open, all windows")
        print("      Polymarket leg after its taker delay: "
              + ", ".join(f"{s} {n}" for s, n in Counter(r["PolyAfterDelay"] or "-" for r in ws).most_common()))
        conf = [r for r in ws if r.get("KalshiConfirmed") in ("1", "True", "true")]
        print(f"      CONFIRMED by a fresh Kalshi read: {len(conf)} of {len(ws)}   (the rest opened on a bot row up to 3s old"
              " and never survived a fresh read - mostly a stale Kalshi price, not an arb)")
        took = [r for r in conf if D(r, "DurationMs") >= 1000 and r["PolyAfterDelay"] == "FULL_AT_SEEN"]
        print(f"      TAKEABLE (confirmed, open >= 1s, Polymarket leg still there after its delay): {len(took)} window(s), "
              f"${sum(D(r, 'ProfitOpenUsd') for r in took):.2f} at open")
        print(f"      who was off vs Pinnacle:  Kalshi leg {show([D(r, 'KalshiEdgeVsPinCents') for r in ws])}   "
              f"Polymarket leg {show([D(r, 'PolyEdgeVsPinCents') for r in ws])}   (+ = that leg was the cheap one)")
        ages = sorted(D(r, "KalshiAgeMsOpen") for r in ws)
        print("      found by: " + ", ".join(f"{t} {n}" for t, n in Counter(r["Trigger"] for r in ws).most_common())
              + f"   Kalshi price at open: " + ", ".join(f"{s} {n}" for s, n in Counter(r["KalshiSourceOpen"] for r in ws).most_common())
              + f" (median {ages[len(ages) // 2]:.0f}ms old)")
        flick = sum(1 for r in ws if r["ReopenGapMs"] and D(r, "ReopenGapMs") < 2000)
        print("      closed by: " + ", ".join(f"{s} {n}" for s, n in Counter(r["CloseReason"] for r in ws).most_common())
              + (f"   ({flick} re-opened within 2s of a close: one window flickering)" if flick else ""))
        # WHAT MADE THE GAP: does a gap with no game event behind it (Pinnacle still for 10s+) stay open longer?
        print("      by origin (Pinnacle's last price change at the open; quiet = none for 10s+):")
        for org in ("PINNACLE_QUIET", "PINNACLE_MOVING", "UNKNOWN"):
            g = [r for r in ws if r.get("Origin") == org]
            if not g:
                continue
            dd = sorted(D(r, "DurationMs") / 1000 for r in g)
            cf = sum(1 for r in g if r.get("KalshiConfirmed") in ("1", "True", "true"))
            print(f"         {org:16} n={len(g):4}  open median {dd[len(dd) // 2]:.1f}s  >=1s "
                  f"{100 * sum(d >= 1 for d in dd) / len(dd):.0f}%  >=5s {100 * sum(d >= 5 for d in dd) / len(dd):.0f}%  "
                  f"confirmed {cf}  margin at open {show([D(r, 'MarginOpenCents') for r in g])}")
        # LEG RISK: each order of execution, priced one taker delay after the open
        kf = [D(r, "KalshiFirstMarginCents") for r in ws if r.get("KalshiFirstMarginCents")]
        pf_n = sum(1 for r in ws if r.get("PolyAfterDelay") in ("FULL_AT_SEEN", "PARTIAL", "GONE"))
        pf = [D(r, "PolyFirstMarginCents") for r in ws if r.get("PolyFirstMarginCents")]
        if kf or pf:
            print("      leg risk, priced 1 taker delay after the open:")
            if kf:
                print(f"         Kalshi first (instant), Polymarket a delay later:  {show(kf)}   locked in "
                      f"{100 * sum(x > 0 for x in kf) / len(kf):.0f}%, a loss in {100 * sum(x < 0 for x in kf) / len(kf):.0f}%")
            if pf_n:
                print(f"         Polymarket first (a miss is free), then Kalshi:    filled {len(pf)} of {pf_n}"
                      + (f"; when filled {show(pf)}, locked in {100 * sum(x > 0 for x in pf) / len(pf):.0f}%" if pf else ""))


if __name__ == "__main__":
    main()
