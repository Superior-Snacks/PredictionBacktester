#!/usr/bin/env python3
"""live_curve.py - when is a tennis match ACTUALLY live, relative to its LISTED start? Writes live_curve.json,
which the block scheduler reads under PINNACLE_BLOCK_OBJECTIVE=inplay.

    python live_curve.py                         # all history -> sidecar/live_curve.json
    python live_curve.py --until 2026-09-27      # train on an earlier slice (for an honest replay)
    python live_curve.py --out other.json --length-scale 1.1

WHY. The EV bot trades IN-PLAY only, and measured over 1,793 matches its signal rate per watched hour is flat
across the whole match (0.43/h in the first 15 min, 0.6-0.7/h after) - 63% of fills come an hour or more after
the listed start. Listed tennis starts are also nominal ("followed by"): the median match ENDS 150 min after its
listed start. A scheduler that covers listed STARTS is therefore aimed at the wrong time; this curve is what
it should cover instead.

HOW, AND WHY NOT FROM OUR OWN TELEMETRY. Our in-play rows exist only inside our own windows, so any start delay
measured from them is biased early (the latest starters fall outside the windows). Kalshi's settlement
`close_time` is the real END of the match (median 2.3 min after our last in-play row on 1,415 matches watched
to the finish) and exists for EVERY settled market, watched or not. So:
    END  - exact and schedule-independent: close_time - listed start, every settled tennis market.
    LENGTH - the only estimated part: last - first in-play row on matches the bot watched from just before they
             went live to the close, scaled up (`--length-scale`, default 1.1) because a sample that had to fit
             inside a window runs short.
    L(m) = P(live at m min after the listed start) = mean over matches of P(LENGTH > end - m), for m < end.
Inputs (all local, no network): ev_settlements.jsonl, pair_ledger.jsonl and EvTelemetry_*.csv at the repo root,
HardVenArb/slate_observations.jsonl.
"""
from __future__ import annotations

import argparse
import bisect
import csv
import glob
import json
import os
from datetime import datetime, timedelta

SIDECAR = os.path.dirname(os.path.abspath(__file__))
HARDVEN = os.path.dirname(SIDECAR)
REPO = os.path.dirname(HARDVEN)
OUT = os.path.join(SIDECAR, "live_curve.json")


def _ts(s: str) -> datetime:
    return datetime.fromisoformat(s[:19])


def _tennis(ticker: str) -> bool:
    series = (ticker or "").split("-")[0].upper()
    return "MATCH" in series and any(t in series for t in ("ATP", "WTA", "ITF"))


def match_ends(repo: str, hardven: str, since: str, until: str) -> dict:
    """matchup id -> (listed start, real end) for settled tennis markets whose listed start is in [since, until]."""
    mid_of = {}
    for line in open(os.path.join(repo, "pair_ledger.jsonl"), encoding="utf-8"):
        try:
            j = json.loads(line)
        except ValueError:
            continue
        p = (j.get("yes_token") or "").split(":")
        if len(p) >= 2 and j.get("ticker"):
            mid_of[j["ticker"]] = p[1]
    start_of = {}
    for line in open(os.path.join(hardven, "slate_observations.jsonl"), encoding="utf-8"):
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if r.get("t") == "game" and (r.get("sport") or "").lower() == "tennis":
            try:
                start_of.setdefault(r["mid"], _ts(r["start"]))
            except (KeyError, ValueError):
                pass
    out = {}
    for line in open(os.path.join(repo, "ev_settlements.jsonl"), encoding="utf-8"):
        try:
            j = json.loads(line)
        except ValueError:
            continue
        tk = j.get("ticker", "")
        if j.get("result") not in ("yes", "no") or not j.get("close_time") or not _tennis(tk):
            continue
        mid = mid_of.get(tk)
        st = start_of.get(mid or "")
        if st is None or mid in out or not (since <= st.date().isoformat() <= until):
            continue
        end = _ts(j["close_time"])
        if timedelta(0) < end - st < timedelta(hours=12):
            out[mid] = (st, end, tk)
    return out


def match_lengths(repo: str, ends: dict, since: str, until: str) -> list[float]:
    """Minutes live, from matches the bot watched from just before they went live to the close."""
    tick_mid = {v[2]: k for k, v in ends.items()}
    active, first, last = set(), {}, {}
    for fn in sorted(glob.glob(os.path.join(repo, "EvTelemetry_2026*.csv"))):
        day = os.path.basename(fn)[12:20]
        d = f"{day[:4]}-{day[4:6]}-{day[6:8]}"
        if not (since <= d <= until):
            continue
        with open(fn, newline="", encoding="utf-8") as fh:
            for x in csv.DictReader(fh):
                t = x["Timestamp"][:19]
                active.add(t[:15])                                 # 10-minute bucket
                tk = x["Ticker"]
                if x.get("InPlay") != "1" or tk not in tick_mid:
                    continue
                first.setdefault(tk, t)
                last[tk] = max(last.get(tk, t), t)
    key = lambda dt_: dt_.strftime("%Y-%m-%dT%H:%M")[:15]
    out = []
    for tk, f in first.items():
        st, end, _ = ends[tick_mid[tk]]
        f, l = _ts(f), _ts(last[tk])
        if key(f - timedelta(minutes=10)) not in active:          # not watching just before it went live
            continue
        if not (-2 <= (end - l).total_seconds() / 60 <= 10):       # not watched to the close
            continue
        m = (end - f).total_seconds() / 60
        if 20 <= m <= 300:
            out.append(m)
    return out


def build(since: str = "2000-01-01", until: str = "2999-12-31", length_scale: float = 1.1,
          step: int = 5, max_min: int = 720, repo: str = REPO, hardven: str = HARDVEN) -> dict:
    ends = match_ends(repo, hardven, since, until)
    lengths = sorted(x * length_scale for x in match_lengths(repo, ends, since, until))
    if len(ends) < 50 or len(lengths) < 50:
        raise SystemExit(f"[CURVE] too little data: {len(ends)} matches with an end, {len(lengths)} lengths")
    offs = [(e - s).total_seconds() / 60 for s, e, _ in ends.values()]
    n_len = len(lengths)
    surv = lambda x: (n_len - bisect.bisect_right(lengths, x)) / n_len      # P(LENGTH > x)
    curve = []
    for m in range(0, max_min, step):
        mid_m = m + step / 2
        curve.append(round(sum(surv(e - mid_m) for e in offs if e > mid_m) / len(offs), 5))
    q = lambda v, p: sorted(v)[min(len(v) - 1, int(len(v) * p))]
    peak = max(range(len(curve)), key=curve.__getitem__)
    return {
        "built": datetime.now().isoformat(timespec="seconds"), "since": since, "until": until,
        "step_min": step, "curve": curve,
        "n_matches": len(offs), "n_lengths": n_len, "length_scale": length_scale,
        "end_after_listed_min": {"p25": round(q(offs, .25)), "median": round(q(offs, .5)), "p75": round(q(offs, .75))},
        "length_min": {"p25": round(q(lengths, .25)), "median": round(q(lengths, .5)), "p75": round(q(lengths, .75))},
        "expected_live_min_per_match": round(sum(curve) * step, 1),
        "peak": {"min_after_listed": peak * step, "p_live": curve[peak]},
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--since", default="2000-01-01")
    ap.add_argument("--until", default="2999-12-31")
    ap.add_argument("--length-scale", type=float, default=1.1)
    ap.add_argument("--out", default=OUT)
    a = ap.parse_args()
    c = build(a.since, a.until, a.length_scale)
    with open(a.out, "w", encoding="utf-8") as fh:
        json.dump(c, fh, indent=1)
    print(f"[CURVE] {c['n_matches']} matches ({c['n_lengths']} lengths) -> {a.out}")
    print(f"[CURVE] real end after listed start: {c['end_after_listed_min']}; length (x{a.length_scale}): {c['length_min']}")
    print(f"[CURVE] peak P(live) {c['peak']['p_live']:.2f} at +{c['peak']['min_after_listed']} min; "
          f"expected live minutes per match {c['expected_live_min_per_match']}")
    for m in (0, 15, 30, 60, 90, 120, 180, 240, 300, 360):
        print(f"         +{m:3d} min  P(live) {c['curve'][m // c['step_min']]:.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
