#!/usr/bin/env python3
"""hour_value.py - what is a live tennis minute worth at each LOCAL clock hour? Writes hour_value.json, which the
block scheduler reads under PINNACLE_BLOCK_WEIGHTS (in-play objective only).

    python hour_value.py                          # the last 42 days -> sidecar/hour_value.json
    python hour_value.py --days 28 --out other.json

WHY. The in-play objective counts every live match-minute the same, so the budget goes where the most matches
are - the morning. Measured 09-28..11-08 on the bot's own covered tennis, fills per in-play match-hour were
0.047 at 07-09 local, 0.056 at 09-13, 0.064 at 13-17, 0.078 at 17-20 and 0.155 at 20-23: the evening (events
in the Americas) pays about three times the morning per match. Not load - the busiest hours (09-13) pay like
the morning.

HOW. Per local clock hour, over the bot's WARM 5-minute buckets (telemetry in the bucket and in the 15 minutes
before it, so a session's first minutes don't count):
    covered match-hours = every settled tennis market's REAL end (Kalshi close_time), live for the measured
        length distribution (live_curve.match_lengths, x1.1 as there), counted where the bot was warm.
    fills = EvLive rows with FillCount > 0 in a warm bucket.
    rate = (fills + ALPHA x all-hours rate) / (covered + ALPHA): shrunk toward the average by ALPHA match-hours.
    An hour with under MIN_COVER covered match-hours has not really been watched. It BORROWS the nearest watched
        hour's rate (both neighbours averaged on a tie) and the JSON names it; rebuild once the bot has run there
        for a couple of weeks and it is measured instead.
    weight = rate / the all-hours rate, so 1.0 is an average hour.
Inputs (all local, no network): ev_settlements.jsonl, pair_ledger.jsonl, EvTelemetry_*.csv and EvLive_*.csv at the
repo root, HardVenArb/slate_observations.jsonl.
"""
from __future__ import annotations

import argparse
import bisect
import csv
import glob
import json
import os
from datetime import date, datetime, timedelta, timezone

import live_curve as lc

SIDECAR = os.path.dirname(os.path.abspath(__file__))
HARDVEN = os.path.dirname(SIDECAR)
REPO = os.path.dirname(HARDVEN)
OUT = os.path.join(SIDECAR, "hour_value.json")


def _local(s: str) -> datetime:
    """An ISO UTC stamp ('...Z' or naive) as naive LOCAL time - the clock every plan is written in."""
    return datetime.fromisoformat(s[:19]).replace(tzinfo=timezone.utc).astimezone().replace(tzinfo=None)


def _bucket(t: datetime) -> datetime:
    return t.replace(minute=t.minute // 5 * 5, second=0, microsecond=0)


def _in(fn: str, prefix: str, since: str, until: str) -> bool:
    day = os.path.basename(fn)[len(prefix):len(prefix) + 8]
    return since <= f"{day[:4]}-{day[4:6]}-{day[6:8]}" <= until


def warm_buckets(repo: str, since: str, until: str) -> set:
    """LOCAL 5-minute buckets where the bot was up, and had been for the 15 minutes before."""
    minutes = set()
    for fn in sorted(glob.glob(os.path.join(repo, "EvTelemetry_2026*.csv"))):
        if not _in(fn, "EvTelemetry_", since, until):
            continue
        with open(fn, encoding="utf-8") as fh:
            next(fh, None)
            for line in fh:
                minutes.add(line[:16])                     # 'YYYY-MM-DDTHH:MM' - parse each distinct minute once
    up = set()
    for m in minutes:
        try:
            up.add(_bucket(_local(m + ":00")))
        except ValueError:
            pass
    return {b for b in up if all(b - timedelta(minutes=5 * k) in up for k in (1, 2, 3))}


def build(days: int = 42, until: str | None = None, alpha: float = 30.0, min_cover: float = 20.0,
          repo: str = REPO, hardven: str = HARDVEN) -> dict:
    until = until or date.today().isoformat()
    since = (date.fromisoformat(until) - timedelta(days=days - 1)).isoformat()
    warm = warm_buckets(repo, since, until)
    lengths = sorted(x * 1.1 for x in lc.match_lengths(repo, lc.match_ends(repo, hardven, since, until), since, until))
    if len(lengths) < 50:
        raise SystemExit(f"[HOURS] too few match lengths in {since}..{until} ({len(lengths)}); widen --days")
    surv = lambda x: (len(lengths) - bisect.bisect_right(lengths, x)) / len(lengths)

    ends = {}                                              # event -> real end, LOCAL
    for line in open(os.path.join(repo, "ev_settlements.jsonl"), encoding="utf-8"):
        try:
            j = json.loads(line)
        except ValueError:
            continue
        tk = j.get("ticker", "")
        if j.get("result") in ("yes", "no") and j.get("close_time") and lc._tennis(tk):
            ends.setdefault(j.get("event_ticker") or tk.rsplit("-", 1)[0], _local(j["close_time"]))
    cover = [0.0] * 24                                     # covered in-play match-MINUTES per hour
    for end in ends.values():
        if not (since <= end.date().isoformat() <= until):
            continue
        for back in range(1, 400):
            p = surv(back - 0.5)
            if p <= 0:
                break
            t = end - timedelta(minutes=back)
            if _bucket(t) in warm:
                cover[t.hour] += p

    fills = [0] * 24
    for fn in glob.glob(os.path.join(repo, "EvLive_2026*.csv")):
        if not _in(fn, "EvLive_", since, until):
            continue
        with open(fn, newline="", encoding="utf-8") as fh:
            for x in csv.DictReader(fh):
                try:
                    if float(x.get("FillCount") or 0) <= 0 or not lc._tennis(x.get("Ticker", "")):
                        continue
                    t = _local(x["At"])
                except (ValueError, KeyError):
                    continue
                if _bucket(t) in warm:
                    fills[t.hour] += 1

    cov_h = [c / 60 for c in cover]
    if sum(cov_h) <= 0 or sum(fills) == 0:
        raise SystemExit(f"[HOURS] nothing to measure in {since}..{until}: {sum(cov_h):.0f} covered match-hours, "
                         f"{sum(fills)} fills")
    avg = sum(fills) / sum(cov_h)
    rate = [(fills[h] + alpha * avg) / (cov_h[h] + alpha) for h in range(24)]
    watched = [h for h in range(24) if cov_h[h] >= min_cover]
    borrowed = {}
    for h in range(24):
        if h in watched or not watched:
            continue
        dist = lambda w: min((h - w) % 24, (w - h) % 24)
        best = min(dist(w) for w in watched)
        near = [w for w in watched if dist(w) == best]
        borrowed[h] = near
        rate[h] = sum(rate[w] for w in near) / len(near)
    weights = [round(r / avg, 3) for r in rate]
    return {
        "built": datetime.now().isoformat(timespec="seconds"), "since": since, "until": until,
        "measure": "fills per covered in-play tennis match-hour, warm buckets, by LOCAL clock hour",
        "alpha_match_hours": alpha, "min_cover_match_hours": min_cover,
        "all_hours_rate": round(avg, 4), "covered_match_hours": round(sum(cov_h), 1), "fills": sum(fills),
        "hours": [{"hour": h, "covered_match_h": round(cov_h[h], 1), "fills": fills[h], "rate": round(rate[h], 4),
                   "borrowed_from": borrowed.get(h), "weight": weights[h]} for h in range(24)],
        "weights": weights,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=int, default=42)
    ap.add_argument("--until", default=None, help="last day included (default today)")
    ap.add_argument("--alpha", type=float, default=30.0, help="match-hours of prior pulling each hour to the average")
    ap.add_argument("--min-cover", type=float, default=20.0, help="covered match-hours below which an hour borrows")
    ap.add_argument("--out", default=OUT)
    a = ap.parse_args()
    v = build(a.days, a.until, a.alpha, a.min_cover)
    with open(a.out, "w", encoding="utf-8") as fh:
        json.dump(v, fh, indent=1)
    print(f"[HOURS] {v['since']}..{v['until']}: {v['fills']} fills over {v['covered_match_hours']:.0f} covered match-hours "
          f"= {v['all_hours_rate']:.3f}/match-h on average -> {a.out}")
    print("[HOURS] hour  covered  fills   rate    weight")
    for x in v["hours"]:
        note = f"  borrowed from {','.join(f'{w:02d}h' for w in x['borrowed_from'])}" if x["borrowed_from"] else ""
        print(f"[HOURS]  {x['hour']:02d}h  {x['covered_match_h']:7.1f}  {x['fills']:5d}  {x['rate']:.4f}  {x['weight']:6.2f}{note}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
