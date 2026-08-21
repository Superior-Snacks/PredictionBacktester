#!/usr/bin/env python3
"""slate_log.py - an append-only record of WHEN games are actually live, built up over weeks.

    (written automatically by pair_pinnacle on every pairing run)
    python slate_log.py --report          # live-density by hour, from the accumulated record
    python slate_log.py --report --days 14
    python slate_log.py --lead            # how far ahead of their start games APPEAR on the board

WHY A LOG AND NOT A LOOK. Scheduling off today's board is biased in a way that cannot be seen from today's
board: a game visible at 14:00 was often not there at 09:00. Tennis fixtures - ITF and challenger especially
- surface a couple of hours before they start, so a plan made in the morning is made against a fraction of
the day it is planning for. Only repeated observation separates "a quiet day" from "quiet at the moment I
looked", and that is exactly the distinction a work schedule needs.

TWO KINDS OF ROW, because there are two different questions:
  "slate"  one per pairing run - the in-play concurrency profile the board implied AT THAT MOMENT. Replayed
           across weeks this gives the real shape of a day, and a disagreement between an early and a late
           observation of the SAME day is itself the finding.
  "game"   one the first time a matchup is seen, carrying `lead_min` = how long before its start it
           appeared. That is the number that says how early a schedule can honestly be made.

Append-only and dependency-free: an analysis file that needs the bot running in order to be read is no use
months later. Rows are self-describing, so an old file stays readable when the fields change.
"""
from __future__ import annotations

import argparse
import collections
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

LOG = Path(os.environ.get("HARDVEN_SLATE_LOG",
                          str(Path(__file__).resolve().parent.parent / "slate_observations.jsonl")))
SEEN = LOG.with_suffix(".seen.json")
DEFAULT_DURATION_MIN = 105          # a tennis match; only used to turn a start into a live INTERVAL


def _utc(s):
    """ISO string -> naive UTC datetime, or None. Tolerant: a row this cannot parse is skipped, never fatal."""
    try:
        d = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        return d.astimezone(timezone.utc).replace(tzinfo=None) if d.tzinfo else d
    except Exception:
        return None


def record(games, path=LOG, duration_min=DEFAULT_DURATION_MIN):
    """games: [{matchup_id, start_time, league, sport, paired}]. Appends the rows. Returns a summary.

    Never raises. This runs inside the pairing job, and a logging failure must not cost a pairing run.
    """
    now = datetime.utcnow().replace(microsecond=0)
    rows, iv = [], []
    try:
        seen = set(json.loads(SEEN.read_text(encoding="utf-8")))
    except Exception:
        seen = set()
    fresh = 0
    for g in games:
        st = _utc(g.get("start_time"))
        mid = str(g.get("matchup_id") or "")
        if st is None or not mid:
            continue
        iv.append((st, st + timedelta(minutes=duration_min), bool(g.get("paired"))))
        if mid in seen:
            continue
        seen.add(mid)
        fresh += 1
        rows.append({"t": "game", "at": now.isoformat(), "mid": mid, "start": st.isoformat(),
                     "lead_min": round((st - now).total_seconds() / 60),
                     "league": g.get("league", ""), "sport": g.get("sport", ""),
                     "paired": bool(g.get("paired"))})

    # In-play concurrency by hour from NOW forward - what THIS observation believes the day looks like.
    prof, prof_paired = {}, {}
    base = now.replace(minute=0, second=0, microsecond=0)
    for h in range(24):
        u = base + timedelta(hours=h)
        prof[u.isoformat()] = sum(1 for a, b, _ in iv if a <= u < b)
        prof_paired[u.isoformat()] = sum(1 for a, b, p in iv if p and a <= u < b)
    rows.append({"t": "slate", "at": now.isoformat(), "n_games": len(iv),
                 "n_paired": sum(1 for _, _, p in iv if p), "n_new": fresh,
                 "live_by_hour": prof, "live_by_hour_paired": prof_paired})

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            for r in rows:
                fh.write(json.dumps(r) + "\n")
        # Bounded so the seen-set cannot grow without limit; matchup ids are monotonic enough that the
        # newest 20k always covers everything currently on a board.
        SEEN.write_text(json.dumps(sorted(seen)[-20000:]), encoding="utf-8")
    except Exception as ex:
        return {"ok": False, "error": "%s: %s" % (type(ex).__name__, ex)}
    return {"ok": True, "new_games": fresh, "games": len(iv)}


def _load(path, days):
    cut = datetime.utcnow() - timedelta(days=days)
    out = []
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return out
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            r = json.loads(line)
        except Exception:
            continue                      # a truncated tail must not lose the whole history
        at = _utc(r.get("at"))
        if at and at >= cut:
            out.append(r)
    return out


def report(days):
    rows = _load(LOG, days)
    slates = [r for r in rows if r.get("t") == "slate"]
    if not slates:
        print("[slate] nothing recorded in the last %d day(s) - %s" % (days, LOG))
        return
    byday = {}
    for r in slates:
        at = _utc(r["at"])
        byday.setdefault(at.date().isoformat(), {})[at.isoformat()] = r
    hours, hours_p = collections.defaultdict(list), collections.defaultdict(list)
    for _day, obs in byday.items():
        # The LAST observation of a day is the least biased one: an early look under-counts its own
        # afternoon, because the afternoon's games were not on the board yet.
        last = obs[max(obs)]
        for iso, n in (last.get("live_by_hour") or {}).items():
            h = _utc(iso)
            if h:
                hours[h.hour].append(n)
        for iso, n in (last.get("live_by_hour_paired") or {}).items():
            h = _utc(iso)
            if h:
                hours_p[h.hour].append(n)
    print("[slate] %d observation(s) across %d day(s) - %s" % (len(slates), len(byday), LOG.name))
    print("  UTC hour   avg live   avg paired")
    for h in sorted(hours):
        a = sum(hours[h]) / len(hours[h])
        b = (sum(hours_p[h]) / len(hours_p[h])) if hours_p.get(h) else 0.0
        print("    %02d:00     %6.1f   %6.1f   %s" % (h, a, b, "#" * int(min(a, 40))))
    if len(byday) < 5:
        print("  NOTE: %d day(s) of history - too few to schedule from. This becomes useful over weeks."
              % len(byday))


def lead(days):
    rows = [r for r in _load(LOG, days) if r.get("t") == "game" and r.get("lead_min") is not None]
    if not rows:
        print("[slate] no first-sightings recorded in the last %d day(s)." % days)
        return
    v = sorted(r["lead_min"] for r in rows)

    def pct(p):
        return v[min(len(v) - 1, int(len(v) * p))]

    print("[slate] %d first-sighting(s) over %d day(s) - how far AHEAD of its start a game appears:" %
          (len(v), days))
    print("    p10 %5.1fh   p25 %5.1fh   median %5.1fh   p75 %5.1fh   p90 %5.1fh" %
          (pct(.10) / 60, pct(.25) / 60, pct(.50) / 60, pct(.75) / 60, pct(.90) / 60))
    late = sum(1 for x in v if x < 180)
    print("    %d/%d (%.0f%%) appeared LESS THAN 3 HOURS before starting - a plan made earlier than that "
          "cannot have seen them." % (late, len(v), late / len(v) * 100))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", action="store_true", help="live-density by hour of day")
    ap.add_argument("--lead", action="store_true", help="how far ahead games appear on the board")
    ap.add_argument("--days", type=int, default=30)
    a = ap.parse_args()
    if a.lead:
        lead(a.days)
    elif a.report:
        report(a.days)
    else:
        print("[slate] log: %s" % LOG)
        print("[slate] use --report (live-density by hour) or --lead (how early games appear)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
