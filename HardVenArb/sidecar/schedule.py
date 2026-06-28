#!/usr/bin/env python3
"""
schedule.py — compute the bot's human-like WORK WINDOWS from the day's game slate.

A 24/7 session is the single biggest bot tell. This reads game START times for the scoped sports from
Pinnacle's GUEST API (account-free: public key, no session, no login) and clusters them into WINDOWS —
the bot opens shortly BEFORE a block of games, works THROUGH it, closes AFTER the last one ends, and goes
dark in the gaps / overnight. Real punter rhythm, not a server that never sleeps.

Standalone for planning/preview (`python schedule.py`) AND importable by the bot:
    from schedule import fetch_starts, compute_windows, status
    windows = compute_windows(fetch_starts([3, 33], 36))
    state, secs = status(windows)        # ("OPEN", secs_to_close) | ("CLOSED", secs_to_next_open)

WINDOW MODEL: each game contributes an interval [start - LEAD, start + DURATION + TRAIL]; intervals that
overlap OR sit within MIN_GAP of each other merge into one window (so the bot never closes for a pointless
short gap). DURATION is per-sport (a baseball game runs longer than a best-of-3). All knobs are CLI flags.

Times are computed in UTC (Pinnacle startTime is ISO-UTC) and DISPLAYED in the machine's local timezone.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from pair_pinnacle import _pin_dt   # ISO-UTC start -> naive UTC datetime (reused so parsing can't drift)

GUEST_BASE = os.environ.get("PINNACLE_GUEST_BASE", "https://guest.api.arcadia.pinnacle.com/0.1")
GUEST_KEY = os.environ.get("PINNACLE_API_KEY", "CmX2KcMrXuFmNg6YFbmTxE0y9CIrOi0R")
OUT = Path(__file__).resolve().parent.parent / "work_windows.json"

SPORT_NAME = {3: "baseball", 33: "tennis"}
# per-sport game length (minutes) used for the window's tail; over-estimating just keeps the bot open a touch
# longer (safer for live arbs) than closing mid-game.
DURATION = {"baseball": 210, "tennis": 180}
DEFAULT_DURATION = 180


def _utcnow() -> datetime:
    """Naive UTC 'now' — matches _pin_dt's naive-UTC starts (mixing naive+aware datetimes would raise)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ── window math (PURE — unit-testable, no network) ───────────────────────────────────────────────────────
def compute_windows(starts: list[tuple[datetime, str]], lead_min: int = 25, trail_min: int = 45,
                    min_gap_min: int = 60, duration: dict | None = None) -> list[tuple[datetime, datetime]]:
    """starts = [(utc_start, sport), ...] -> merged [(open, close), ...] in UTC. Each game spans
    [start-lead, start+dur+trail]; intervals overlapping or within min_gap merge into one window."""
    duration = duration or DURATION
    intervals = sorted(
        (s - timedelta(minutes=lead_min), s + timedelta(minutes=duration.get(sport, DEFAULT_DURATION) + trail_min))
        for s, sport in starts)
    merged: list[list[datetime]] = []
    for o, c in intervals:
        if merged and o <= merged[-1][1] + timedelta(minutes=min_gap_min):
            merged[-1][1] = max(merged[-1][1], c)
        else:
            merged.append([o, c])
    return [(o, c) for o, c in merged]


def active_window(windows, now: datetime | None = None):
    """The window containing `now` (UTC naive), or None."""
    now = now or _utcnow()
    return next(((o, c) for o, c in windows if o <= now <= c), None)


def status(windows, now: datetime | None = None) -> tuple[str, float | None]:
    """('OPEN', seconds_until_close) if inside a window; else ('CLOSED', seconds_until_next_open) or
    ('CLOSED', None) if no upcoming window. The bot polls this to decide open vs dark."""
    now = now or _utcnow()
    cur = active_window(windows, now)
    if cur:
        return "OPEN", (cur[1] - now).total_seconds()
    upcoming = [o for o, _ in windows if o > now]
    return "CLOSED", ((min(upcoming) - now).total_seconds() if upcoming else None)


# ── slate (GUEST API — account-free) ─────────────────────────────────────────────────────────────────────
def _guest(client: httpx.Client, path: str):
    try:
        r = client.get(GUEST_BASE + path)
        return r.json() if r.status_code == 200 else None
    except Exception:
        return None


def fetch_starts(sports: list[int], horizon_hours: int = 36, back_hours: int = 4) -> list[tuple[datetime, str]]:
    """Game start times (UTC) for the scoped sports from the guest board, within [now-back, now+horizon].
    Only the MAIN matchup per game (parentId is None, type 'matchup') so each game counts once; doubles and
    derivative '(Games)' children are skipped. `back_hours` keeps already-started games so live ones still
    fall inside their window."""
    client = httpx.Client(headers={"accept": "application/json", "x-api-key": GUEST_KEY,
                                   "origin": "https://www.pinnacle.bet", "user-agent": "Mozilla/5.0"},
                          timeout=20.0, follow_redirects=True)
    now = _utcnow()
    lo, hi = now - timedelta(hours=back_hours), now + timedelta(hours=horizon_hours)
    out: list[tuple[datetime, str]] = []
    for sid in sports:
        sport = SPORT_NAME.get(sid, str(sid))
        for lg in (_guest(client, f"/sports/{sid}/leagues") or []):
            if (lg.get("matchupCount") or 0) <= 0 or "doubles" in (lg.get("name", "") or "").lower():
                continue
            for m in (_guest(client, f"/leagues/{lg['id']}/matchups") or []):
                if m.get("parentId") is not None or m.get("type") != "matchup":
                    continue   # skip "(Games)" derivative children + tournament specials
                st = _pin_dt(m.get("startTime", ""))
                if st and lo <= st <= hi:
                    out.append((st, sport))
            time.sleep(0.15)
    client.close()
    return out


# ── display helpers ──────────────────────────────────────────────────────────────────────────────────────
def _local(dt_utc: datetime) -> datetime:
    return dt_utc.replace(tzinfo=timezone.utc).astimezone()


def _hm(secs: float | None) -> str:
    if secs is None:
        return "—"
    secs = int(secs)
    sign = "-" if secs < 0 else ""
    secs = abs(secs)
    return f"{sign}{secs // 3600}h{(secs % 3600) // 60:02d}m"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sports", default="3,33", help="Pinnacle sport ids (default 3=baseball,33=tennis)")
    ap.add_argument("--horizon", type=int, default=36, help="plan this many hours ahead (default 36)")
    ap.add_argument("--lead", type=int, default=25, help="open this many min before a block's first game")
    ap.add_argument("--trail", type=int, default=45, help="close this many min after the last game's end")
    ap.add_argument("--min-gap", type=int, default=60, help="merge blocks less than this many min apart")
    ap.add_argument("--write", action="store_true", help="also write work_windows.json for the bot")
    args = ap.parse_args()

    sports = [int(s) for s in args.sports.split(",") if s.strip()]
    print(f"[SCHED] fetching slate (sports={sports}, horizon={args.horizon}h) from the guest board …")
    starts = fetch_starts(sports, args.horizon)
    bysport = {}
    for _, sp in starts:
        bysport[sp] = bysport.get(sp, 0) + 1
    print(f"[SCHED] {len(starts)} games: " + ", ".join(f"{k}={v}" for k, v in sorted(bysport.items())))

    windows = compute_windows(starts, args.lead, args.trail, args.min_gap)
    print(f"\n[SCHED] {len(windows)} work window(s) (local time):")
    now = _utcnow()
    for o, c in windows:
        live = "  <== NOW" if o <= now <= c else ""
        dur = _hm((c - o).total_seconds())
        print(f"   {_local(o):%a %d %b %H:%M} -> {_local(c):%H:%M}  ({dur}){live}")

    state, secs = status(windows, now)
    if state == "OPEN":
        print(f"\n[SCHED] NOW: OPEN — work for another {_hm(secs)} (then close).")
    else:
        print(f"\n[SCHED] NOW: CLOSED (dark) — next open in {_hm(secs)}." if secs is not None
              else "\n[SCHED] NOW: CLOSED — no upcoming games in the horizon.")

    if args.write:
        OUT.write_text(json.dumps([{"open": o.isoformat() + "Z", "close": c.isoformat() + "Z"}
                                   for o, c in windows], indent=2), encoding="utf-8")
        print(f"[SCHED] wrote {len(windows)} window(s) -> {OUT}")


if __name__ == "__main__":
    main()
