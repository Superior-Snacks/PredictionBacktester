#!/usr/bin/env python3
"""Was this settlement normal? Retirements, walkovers, delays and abrupt endings, from what we logged.

WHY THIS EXISTS
---------------
Kalshi settles a tennis retirement exactly like a finished match - the player who advances wins, no note,
no flag - so a fill that lost to a retirement looks identical in the ledger to one that lost on the court.
The ONLY place the difference shows is the price path: a market at 0.42 that settles at 0.00 ten minutes
after our last look did not lose a set, it lost a player. `note` in ev_settlements.jsonl is empty on every
one of 10,224 records; Kalshi does not populate it.

Walkovers ARE distinguishable: "if the match does not occur (signaled by a ball being played) ... the market
will resolve to a fair price" - those come back with result=scalar rather than yes/no. Measured 2026-09-12:
45 such markets, none with a fill or a signal, and the report treats them as pending forever because its
IsFinal accepts only yes/no.

WHAT IT READS
-------------
  ev_settlements.jsonl   status / result / when we first saw it and when it finalized
  EvLive_*.csv           our fills on the ticker, and what they made
  EvTelemetry_*.csv      every evaluation: the Kalshi ask, our P_true, in-play flag - the price path
  Kalshi API (--api)     settlement_ts, close_time, settlement value, the rules' retirement clause

FLAGS
-----
  WALKOVER      result=scalar: no ball played, settled to a fair price (P&L is settlement_value - cost)
  SHORT_MATCH   first ball to Kalshi close under --short minutes (default 50): a retirement or a rout.
                Uses close_time from the API (on by default). With --no-api only our own coverage is
                known and the flag becomes SHORT_COVERAGE - which says how long WE watched, not the match
  ABRUPT_END    the winning side was still priced under 0.85 the last time we looked, and the market
                closed within 15 minutes of that look: it never converged, something ended it
  UPSET_PATH    the winning side was under 0.20 at our last look - the market had the OTHER player
  QUIET_END     no telemetry row for over 20 min before the close while the winner was still under 0.85:
                Pinnacle suspended and never reopened - the retirement signature
  BIG_JUMP      a 25c+ ask move between two IN-PLAY evaluations under 5 min apart. The pre-match ->
                first-ball step is excluded: a favourite can open in play far from its pre-match line,
                and that gap can span hours of no observation
  LATE_SETTLE   closed more than 6h after our last observation - delayed, postponed, or re-graded
  NOT_FINAL     not settled yet (or void/unknown result)

USAGE
-----
  python check_settlements.py XILLOG CANDEL SEKMAT        substrings are fine; matches any ticker containing them
  python check_settlements.py --losses                    every settled live fill that lost
  python check_settlements.py --losses --brief            one line per ticker
  python check_settlements.py --fills --flagged           every settled fill, only the ones that trip a flag
  python check_settlements.py --losses --no-api           skip the one Kalshi GET per ticker
"""
from __future__ import annotations
import argparse, csv, glob, io, json, math, os, sys, urllib.request, datetime as dt
from collections import defaultdict

KALSHI = "https://api.elections.kalshi.com/trade-api/v2/markets/"


def num(v):
    v = (v or "").strip()
    try:
        f = float(v)
        return f if math.isfinite(f) else None
    except Exception:
        return None


def ts(s):
    s = (s or "").strip().replace("Z", "+00:00")
    try:
        d = dt.datetime.fromisoformat(s)
        return d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)
    except Exception:
        return None


def fmt(d):
    return d.strftime("%m-%d %H:%M") if d else "-"


def mins(a, b):
    return (b - a).total_seconds() / 60 if a and b else None


# ── settlements: first sighting, last record ───────────────────────────────────────────────────
def load_settlements(path):
    first, last = {}, {}
    if not os.path.exists(path):
        return first, last
    for line in io.open(path, encoding="utf-8", errors="replace"):
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except Exception:
            continue
        t = d.get("ticker")
        if not t:
            continue
        first.setdefault(t, d)
        # the FINALIZING record is the one whose 'at' we want; later re-reads of a final market are noise
        if t not in last or last[t].get("status") not in ("finalized", "determined") \
                or d.get("status") in ("finalized", "determined") and last[t].get("result") != d.get("result"):
            last[t] = d
    return first, last


# ── fills ──────────────────────────────────────────────────────────────────────────────────────
def load_fills():
    out = defaultdict(list)
    for f in sorted(glob.glob("EvLive_*.csv")):
        for r in csv.DictReader(io.open(f, encoding="utf-8-sig", errors="replace")):
            fc = num(r.get("FillCount")) or 0
            if fc < 1:
                continue
            out[r["Ticker"]].append(dict(
                at=ts(r.get("At")), side=r.get("Side", ""), n=fc,
                px=num(r.get("AvgFillPrice")) or num(r.get("LimitPrice")) or 0,
                fee=num(r.get("FeeChargedUsd")) or 0))
    return out


# ── telemetry: the price path, only for the tickers we care about ──────────────────────────────
def load_paths(want):
    """ticker -> dict(rows=[(t, side, ask, ptrue, inplay)], title=...)"""
    paths = {t: dict(rows=[], title="") for t in want}
    for f in sorted(glob.glob("EvTelemetry_*.csv")):
        for r in csv.DictReader(io.open(f, encoding="utf-8-sig", errors="replace")):
            t = r.get("Ticker", "")
            if t not in paths:
                continue
            at = ts(r.get("Timestamp"))
            if at is None:
                continue
            p = paths[t]
            if not p["title"]:
                p["title"] = r.get("EventTitle", "")
            p["rows"].append((at, r.get("Side", ""), num(r.get("KalshiRestAsk")),
                              num(r.get("PTrueUsed")), (r.get("InPlay") or "").strip() == "1"))
    for p in paths.values():
        p["rows"].sort(key=lambda x: x[0])
    return paths


def kalshi_market(ticker):
    try:
        with urllib.request.urlopen(KALSHI + ticker, timeout=15) as resp:
            return json.load(resp).get("market") or {}
    except Exception as e:
        return {"_error": f"{type(e).__name__}: {e}"}


# ── the analysis for one ticker ────────────────────────────────────────────────────────────────
def analyse(ticker, first, last, fills, path, short_min, api):
    rec = last.get(ticker) or {}
    seen = first.get(ticker) or {}
    status, result = rec.get("status", "?"), rec.get("result", "")
    final = status in ("finalized", "determined") and result in ("yes", "no")
    flags = []
    lines = []

    title = path.get("title") or rec.get("title") or ""
    lines.append(f"{ticker}   {title}")

    # settlement record
    seen_at, fin_at = ts(seen.get("at")), ts(rec.get("at")) if status in ("finalized", "determined") else None
    s = f"  settlement   {status:<10} result={result or '-':<7} first seen {fmt(seen_at)}"
    if fin_at:
        s += f"   finalized {fmt(fin_at)}"
    lines.append(s)
    if result == "scalar":
        flags.append("WALKOVER")
    elif not final:
        flags.append("NOT_FINAL")

    # Kalshi, if asked
    km = kalshi_market(ticker) if api else {}
    close_at = None
    if km:
        if "_error" in km:
            lines.append(f"  kalshi       {km['_error']}")
        else:
            st, ct = ts(km.get("settlement_ts")), ts(km.get("close_time"))
            close_at = ct
            lines.append(f"  kalshi       close {fmt(ct)}   settlement_ts {fmt(st)}   "
                         f"value ${km.get('settlement_value_dollars', '?')}   "
                         f"expiration_value={km.get('expiration_value', '-')}")
            if km.get("early_close_condition"):
                lines.append(f"               {km['early_close_condition']}")
            if fin_at is None and st:
                fin_at = st

    # our position
    won_side = "YES" if result == "yes" else "NO" if result == "no" else None
    pos = fills.get(ticker) or []
    if pos:
        for x in pos:
            w = (x["side"] == won_side) if won_side else None
            pnl = (x["n"] if w else 0) - x["n"] * x["px"] - x["fee"] if w is not None else None
            lines.append(f"  our fill     {fmt(x['at'])}  {x['side']} x{x['n']:.0f} @ {x['px']:.2f}  -> "
                         + ("WON " if w else "lost" if w is not None else "open")
                         + (f" {pnl:+.2f}" if pnl is not None else ""))
    else:
        lines.append("  our fill     none (signals only)")

    # the price path
    rows = path.get("rows") or []
    ip = [r for r in rows if r[4]]
    if not rows:
        lines.append("  price path   no telemetry rows for this ticker")
    else:
        t0, t1 = rows[0][0], rows[-1][0]
        ip0, ip1 = (ip[0][0], ip[-1][0]) if ip else (None, None)
        span = mins(ip0, ip1)
        lines.append(f"  price path   {len(rows)} evaluation(s), {len(ip)} in-play   "
                     f"first {fmt(t0)}   first in-play {fmt(ip0)}   last {fmt(t1)}"
                     + (f"   -> {span:.0f} min observed in play" if span is not None else ""))

        # last seen per side
        last_ask, last_p = {}, {}
        for at, side, ask, p, _ in rows:
            if ask is not None:
                last_ask[side] = (ask, at)
            if p is not None:
                last_p[side] = p
        la = "   ".join(f"{sd} {v[0]:.2f}" for sd, v in sorted(last_ask.items()))
        lp = "   ".join(f"{sd} {v:.2f}" for sd, v in sorted(last_p.items()))
        lines.append(f"               last seen: kalshi ask {la}   pinnacle P_true {lp}")

        # largest single-step jump, per side, IN PLAY ONLY and between evaluations under 5 min apart.
        # The pre-match -> first-ball step is excluded on purpose: a favourite can open in-play far from
        # its pre-match line, and that "step" can span hours of no observation.
        jump = (0.0, None, None, None)
        prev = {}
        for at, side, ask, _, inplay in rows:
            if ask is None or not inplay:
                if not inplay:
                    prev.pop(side, None)
                continue
            if side in prev and mins(prev[side][1], at) <= 5 and abs(ask - prev[side][0]) > abs(jump[0]):
                jump = (ask - prev[side][0], at, side, (prev[side][0], ask))
            prev[side] = (ask, at)
        if jump[1] is not None and abs(jump[0]) >= 0.10:
            lines.append(f"               largest one-step move: {jump[2]} {jump[3][0]:.2f} -> {jump[3][1]:.2f} "
                         f"({jump[0]:+.2f}) at {fmt(jump[1])}")
        if abs(jump[0]) > 0.25:
            flags.append("BIG_JUMP")

        # flags from the path. Match length = first ball we saw -> Kalshi close (the real end). Without
        # the API only our own coverage is known, and that is a different, weaker statement.
        end = close_at or fin_at
        if ip0 and close_at:
            length = mins(ip0, close_at)
            lines.append(f"               first ball -> kalshi close: {length:.0f} min")
            if length < short_min:
                flags.append("SHORT_MATCH")
        elif span is not None and ip and span < short_min:
            flags.append("SHORT_COVERAGE")
        if won_side and won_side in last_ask:
            wa, wt = last_ask[won_side]
            gap_to_settle = mins(wt, end) if end else None
            if wa < 0.20:
                flags.append("UPSET_PATH")
            if wa < 0.85 and gap_to_settle is not None and gap_to_settle <= 15:
                flags.append("ABRUPT_END")
        if end and t1:
            quiet = mins(t1, end)
            if quiet is not None and quiet > 20 and ip and (won_side is None or last_ask.get(won_side, (1.0,))[0] < 0.85):
                flags.append("QUIET_END")
            if quiet is not None and quiet > 360:
                flags.append("LATE_SETTLE")
            lines.append(f"               last observation -> {'close' if close_at else 'finalized'}: {quiet:.0f} min")

    lines.append("  flags        " + (", ".join(flags) if flags else "none"))
    fill_s = "; ".join(f"{x['side']} x{x['n']:.0f}@{x['px']:.2f}" for x in pos) or "no fill"
    brief = f"{ticker:<40} {title[:26]:<26} {result or '-':<4} {fill_s:<26} {', '.join(flags) if flags else '-'}"
    return lines, flags, brief


def main():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("tickers", nargs="*", help="tickers or substrings of tickers")
    ap.add_argument("--fills", action="store_true", help="every settled live fill")
    ap.add_argument("--losses", action="store_true", help="every settled live fill that LOST")
    ap.add_argument("--flagged", action="store_true", help="print only tickers that trip at least one flag")
    ap.add_argument("--no-api", action="store_true", help="skip the Kalshi GET per ticker (loses close_time)")
    ap.add_argument("--brief", action="store_true", help="one line per ticker")
    ap.add_argument("--short", type=float, default=50.0, help="SHORT_MATCH threshold in minutes (default 50)")
    ap.add_argument("--settlements", default="ev_settlements.jsonl")
    a = ap.parse_args()

    first, last = load_settlements(a.settlements)
    fills = load_fills()

    want = set()
    if a.fills or a.losses:
        for t, fs in fills.items():
            rec = last.get(t) or {}
            res = rec.get("result", "")
            if res not in ("yes", "no"):
                continue
            won = "YES" if res == "yes" else "NO"
            if a.losses and not any(f["side"] != won for f in fs):
                continue
            want.add(t)
    for sub in a.tickers:
        sub = sub.strip().upper()
        hits = [t for t in set(last) | set(fills) if sub in t.upper()]
        if not hits:
            print(f"[?] nothing matches '{sub}'")
        want.update(hits)
    if not want:
        print("nothing to check - give tickers, or --fills / --losses")
        return 1

    paths = load_paths(want)
    flagged = 0
    order = sorted(want, key=lambda t: (paths[t]["rows"][0][0] if paths[t]["rows"] else dt.datetime.max.replace(tzinfo=dt.timezone.utc)))
    for t in order:
        lines, flags, brief = analyse(t, first, last, fills, paths[t], a.short, not a.no_api)
        if flags and flags != ["NOT_FINAL"]:
            flagged += 1
        if a.flagged and not flags:
            continue
        if a.brief:
            print(brief)
        else:
            print("\n".join(lines))
            print()
    print(f"{len(want)} ticker(s) checked, {flagged} flagged.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
