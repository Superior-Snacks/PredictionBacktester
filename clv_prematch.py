#!/usr/bin/env python3
"""Closing-line value of PRE-MATCH signals, against Pinnacle's last pre-match price.

WHY A SEPARATE SCRIPT
---------------------
Section 9 grades in-play signals at T+5..300s against Kalshi's ask, because in play there is no "close" -
the line moves until settlement. Pre-match has the real thing: the last price Pinnacle posted before first
ball is the sharpest number that exists for that match, and beating it is the textbook definition of CLV.
The bot logs SIGNAL_PREMATCH rows but never grades them; RequireInPlay keeps them out of the live path, so
nobody has asked. This answers it from data already on disk, with no venue request.

THREE NUMBERS PER SIGNAL
------------------------
  ORACLE CLV     P_true(close) - ask(entry)     Could we have bought below Pinnacle's closing fair value?
                                                The classic bettor's CLV. Positive = we beat the close.
  PINNACLE DRIFT P_true(close) - P_true(entry)  Did Pinnacle itself move toward or away from what we
                                                believed at entry? Negative = our entry fair value was
                                                too high and the book corrected it before the match.
  KALSHI DRIFT   ask(close) - ask(entry)        Did Kalshi's price rise toward ours by first ball?

"Close" = the LAST pre-match observation of this ticker+side across telemetry and the oracle snapshot log,
followed by the first in-play row if one exists (the opening in-play price is the closing pre-match price
plus one tick). Rows before the signal are ignored. A signal with no later pre-match observation and no
in-play row cannot be graded and is reported as such rather than silently dropped.

ONE SIGNAL PER TICKER+SIDE (the first), because a market that signals for six hours contributes one bet.

"PRE-MATCH" IS CHECKED AGAINST THE SCHEDULED START, NOT THE InPlay FLAG. Measured 2026-09-13: 437 of 749
rows tagged InPlay=0 were logged AFTER the match's scheduled start - the sidecar was serving a resurrected
pre-match parent at its frozen line while the match was in play (fixed the same day in pinnacle_adapter's
_read_cache). Those rows settle 5.8 points under their P_true (n=433, 2.4 sigma): a stale oracle against
a live market manufactures edges that lose. They are graded separately and EXCLUDED from the pre-match
figures. Start times come from HardVenArb/slate_observations.jsonl via pair_ledger.jsonl (ticker -> matchup
id); a candidate whose start is unknown is reported as such.

USAGE
-----
  python clv_prematch.py                 all pre-match signals
  python clv_prematch.py --min-hours 1   only signals at least 1h before close (drops "pre-match" rows
                                         logged seconds before first ball, which are in-play in all but name)
  python clv_prematch.py --list          one line per signal
  python clv_prematch.py --candidates    grade every pre-match row that passed EVERY filter except the
                                         kinetic one (NOT_RISING / NO_KINETIC_HISTORY / SIGNAL_PREMATCH).
                                         The kinetic filter needs Pinnacle to have moved in the last 5s,
                                         which pre-match it almost never does - 97% of pre-match quotes
                                         are flat - so SIGNAL_PREMATCH is a ~random 0.1% slice. Measured
                                         2026-09-13: 231,282 pre-match rows cleared EV; 156 got the label.
                                         --candidates is what a pre-match strategy would actually trade.
"""
from __future__ import annotations
import argparse, csv, glob, io, json, math, sys, datetime as dt
from collections import defaultdict


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


def tennis(ticker):
    s = ticker.split("-")[0]
    return any(x in s for x in ("ATP", "WTA", "ITF"))


def load_settlements(path="ev_settlements.jsonl"):
    last = {}
    try:
        for line in io.open(path, encoding="utf-8", errors="replace"):
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except Exception:
                continue
            if d.get("ticker"):
                last[d["ticker"]] = d
    except FileNotFoundError:
        pass
    return last


def won(last, tk, side):
    d = last.get(tk)
    if not d or d.get("status") not in ("finalized", "determined") or d.get("result") not in ("yes", "no"):
        return None
    return (d["result"] == "yes") == (side == "YES")


def mean_se(v):
    n = len(v)
    if n == 0:
        return 0, float("nan"), float("nan")
    m = sum(v) / n
    if n < 2:
        return n, m, float("nan")
    sd = math.sqrt(sum((x - m) ** 2 for x in v) / (n - 1))
    return n, m, sd / math.sqrt(n)


def main():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--min-hours", type=float, default=0.0, help="only signals at least this long before close")
    ap.add_argument("--list", action="store_true", help="one line per signal")
    ap.add_argument("--candidates", action="store_true",
                    help="include NOT_RISING / NO_KINETIC_HISTORY pre-match rows (kinetic filter off)")
    a = ap.parse_args()

    # ── every observation of every ticker+side: (time, ptrue, ask, inplay, source) ──────────────
    obs = defaultdict(list)
    sigs = {}
    for f in sorted(glob.glob("EvTelemetry_*.csv")):
        for r in csv.DictReader(io.open(f, encoding="utf-8-sig", errors="replace")):
            tk = r.get("Ticker", "")
            if not tennis(tk):
                continue
            t = ts(r.get("Timestamp"))
            p, ask = num(r.get("PTrueUsed")), num(r.get("KalshiRestAsk"))
            if t is None or p is None:
                continue
            k = tk + "|" + r.get("Side", "")
            live = (r.get("InPlay") or "").strip() == "1"
            obs[k].append((t, p, ask, live, "tel"))
            dec = r.get("Decision")
            take = dec == "SIGNAL_PREMATCH" or (a.candidates and not live and dec in ("NOT_RISING", "NO_KINETIC_HISTORY")
                                                and (num(r.get("Ev")) or 0) >= 0.01)
            if take and k not in sigs and ask is not None:
                sigs[k] = dict(tk=tk, side=r.get("Side", ""), t=t, p=p, ask=ask,
                               fee=num(r.get("FeePerContract")) or 0, ev=num(r.get("Ev")) or 0)
    for f in sorted(glob.glob("EvOracleSnap_*.csv")):
        for r in csv.DictReader(io.open(f, encoding="utf-8-sig", errors="replace")):
            tk = r.get("Ticker", "")
            if not tennis(tk):
                continue
            t = ts(r.get("Timestamp"))
            p = num(r.get("PTrueUsed"))
            if t is None or p is None:
                continue
            k = tk + "|" + r.get("Side", "")
            obs[k].append((t, p, num(r.get("KalshiWsAsk")), (r.get("InPlay") or "").strip() == "1", "snap"))
    for k in obs:
        obs[k].sort(key=lambda x: x[0])

    last = load_settlements()

    # ── scheduled start per ticker: ledger gives ticker -> Pinnacle matchup id, slate gives id -> start ──
    tk2mid, mid2start = {}, {}
    try:
        for line in io.open("pair_ledger.jsonl", encoding="utf-8", errors="replace"):
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except Exception:
                continue
            parts = (d.get("yes_token") or "").split(":")
            if len(parts) >= 2 and d.get("ticker"):
                tk2mid[d["ticker"]] = parts[1]
        for line in io.open("HardVenArb/slate_observations.jsonl", encoding="utf-8", errors="replace"):
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except Exception:
                continue
            if d.get("t") == "game" and d.get("mid") and d.get("start"):
                st = ts(d["start"])
                if st:
                    mid2start[str(d["mid"])] = st
    except FileNotFoundError:
        pass
    def sched_start(tk):
        return mid2start.get(tk2mid.get(tk, ""))

    # ── grade each signal against its close ─────────────────────────────────────────────────────
    graded, ungraded = [], []
    for k, s in sigs.items():
        later = [o for o in obs.get(k, []) if o[0] > s["t"]]
        pre = [o for o in later if not o[3]]
        live = [o for o in later if o[3]]
        if pre:
            close = pre[-1]
        elif live:
            close = live[0]                # opening in-play price = the close plus one tick
        else:
            ungraded.append(s)
            continue
        hours = (close[0] - s["t"]).total_seconds() / 3600
        if hours < a.min_hours:
            continue
        w = won(last, s["tk"], s["side"])
        st = sched_start(s["tk"])
        lead_min = (st - s["t"]).total_seconds() / 60 if st else None
        graded.append(dict(**s, close_t=close[0], close_p=close[1], close_ask=close[2], hours=hours,
                           went_live=bool(live), won=w, lead_min=lead_min,
                           oracle_clv=close[1] - s["ask"],
                           pin_drift=close[1] - s["p"],
                           kalshi_drift=(close[2] - s["ask"]) if close[2] is not None else None))

    print(f"pre-match tennis {'candidates (kinetic filter off)' if a.candidates else 'signals'}: {len(sigs)} distinct ticker+side   graded {len(graded)}   "
          f"ungraded {len(ungraded)} (no later observation)")

    # ── split off the rows that were NOT pre-match at all ───────────────────────────────────────
    stale = [g for g in graded if g["lead_min"] is not None and g["lead_min"] < -5]
    unknown = [g for g in graded if g["lead_min"] is None]
    graded = [g for g in graded if g["lead_min"] is not None and g["lead_min"] >= -5]
    if stale:
        st_ = [g for g in stale if g["won"] is not None]
        wr = sum(1 for g in st_ if g["won"]) / max(1, len(st_))
        pt = sum(g["p"] for g in st_) / max(1, len(st_))
        edge = [(1.0 if g["won"] else 0.0) - g["ask"] - g["fee"] for g in st_]
        n_, m_, se_ = mean_se(edge)
        print(f"  EXCLUDED {len(stale)} logged AFTER the scheduled start (InPlay=0 on a live match: the frozen-parent "
              f"bug) - settled {len(st_)}: won {100*wr:.1f}% vs P_true {100*pt:.1f}%, realised {100*m_:+.2f}c +/- {100*se_:.1f}")
    if unknown:
        print(f"  EXCLUDED {len(unknown)} with no scheduled start on record")
    print(f"  GENUINELY PRE-MATCH (before scheduled start): {len(graded)}")
    if not graded:
        return 1
    if a.min_hours:
        print(f"  (signals under {a.min_hours:g}h before close excluded)")
    if not graded:
        return 1
    hrs = sorted(g["hours"] for g in graded)
    print(f"  hours from signal to close: median {hrs[len(hrs)//2]:.1f}   p90 {hrs[int(.9*(len(hrs)-1))]:.1f}   "
          f"went in-play {sum(1 for g in graded if g['went_live'])}/{len(graded)}")
    print()

    def line(lbl, v, unit="c"):
        n, m, se = mean_se(v)
        t = m / se if se and se > 0 else float("nan")
        up = sum(1 for x in v if x > 1e-9)
        dn = sum(1 for x in v if x < -1e-9)
        print(f"  {lbl:<18} n={n:>3}  mean {100*m:+6.2f}{unit} +/- {100*se:4.2f}  (t={t:+5.1f})   "
              f"positive {100*up/max(1,up+dn):5.1f}%  of those that moved")

    print("ENTRY: quoted EV {:+.2f}c mean, ask {:.2f} mean, P_true {:.2f} mean".format(
        100 * sum(g["ev"] for g in graded) / len(graded),
        sum(g["ask"] for g in graded) / len(graded),
        sum(g["p"] for g in graded) / len(graded)))
    print()
    print("AT THE CLOSE (Pinnacle's last pre-match price):")
    line("ORACLE CLV", [g["oracle_clv"] for g in graded])
    print("      = P_true(close) - ask(entry). Positive: the price we could have bought at was BELOW Pinnacle's")
    print("        closing fair value. This is the number a pre-match strategy lives on.")
    line("PINNACLE DRIFT", [g["pin_drift"] for g in graded])
    print("      = P_true(close) - P_true(entry). Negative: Pinnacle corrected DOWN after we signalled - our")
    print("        entry fair value was the stale one, and the 'edge' was partly Pinnacle being early.")
    kd = [g["kalshi_drift"] for g in graded if g["kalshi_drift"] is not None]
    line("KALSHI DRIFT", kd)
    print("      = ask(close) - ask(entry). Positive: Kalshi's price rose toward ours by first ball.")

    # the honest edge: did the ENTRY price beat the close by more than the fee?
    net = [g["oracle_clv"] - g["fee"] for g in graded]
    n, m, se = mean_se(net)
    print()
    print(f"  ORACLE CLV net of fee: {100*m:+.2f}c +/- {100*se:.2f}   -> "
          + ("the entry beat the close after costs" if m > 0 else "the entry did NOT beat the close after costs"))

    # settlement, for the ones that have it
    st = [g for g in graded if g["won"] is not None]
    if st:
        wr = sum(1 for g in st if g["won"]) / len(st)
        pt = sum(g["p"] for g in st) / len(st)
        pc = sum(g["close_p"] for g in st) / len(st)
        edge = [(1.0 if g["won"] else 0.0) - g["ask"] - g["fee"] for g in st]
        n, m, se = mean_se(edge)
        print()
        print(f"SETTLED {len(st)}: won {100*wr:.1f}%   vs entry P_true {100*pt:.1f}%   vs close P_true {100*pc:.1f}%")
        print(f"  realised edge at the entry ask: {100*m:+.2f}c +/- {100*se:.2f} per contract")

    if a.list:
        print()
        print(f"  {'signal':<16} {'ticker':<40} {'side':<4} {'ask':>5} {'P_in':>5} {'P_cl':>5} {'hrs':>5} "
              f"{'oCLV':>6} {'pDrift':>7} {'kDrift':>7}  {'result'}")
        for g in sorted(graded, key=lambda g: g["t"]):
            kd = f"{100*g['kalshi_drift']:+6.1f}" if g["kalshi_drift"] is not None else "     -"
            res = "WON" if g["won"] else "lost" if g["won"] is not None else "-"
            print(f"  {g['t']:%m-%d %H:%M}      {g['tk']:<40} {g['side']:<4} {g['ask']:5.2f} {g['p']:5.2f} "
                  f"{g['close_p']:5.2f} {g['hours']:5.1f} {100*g['oracle_clv']:+6.1f} {100*g['pin_drift']:+7.1f} "
                  f"{kd}  {res}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
