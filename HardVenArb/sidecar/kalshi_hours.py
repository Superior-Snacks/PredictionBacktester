"""Kalshi's weekly maintenance in LOCAL time (2026-11-19): the hours the EV bot cannot trade.

Kalshi halts trading every Thursday 03:00-05:00 New York time: 08:00-10:00 UTC in US standard time, 07:00-09:00 UTC
in daylight time. Same rule as the bot's Calibration.InKalshiMaintenance, DST-aware on both ends here (New York's
zone and the local one the plans are written in).

Two users:
  * the block planner (build_blocks / block_scheduler): an open Pinnacle window in the halt buys nothing - 11-19's
    07:39-12:40 window spent two of its five hours there - so those minutes are worth zero and the day's budget
    goes to the next-best hours instead;
  * hour_value.py: minutes the bot watched while Kalshi could not fill must not count as covered, or they dilute
    the measured rate of the hours the halt falls in (for every day of the week, not just Thursdays).
"""
from __future__ import annotations

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

NEW_YORK = ZoneInfo("America/New_York")
WEEKDAY = 3                               # Thursday (datetime.weekday())
START, END = time(3, 0), time(5, 0)       # New York local


def _local(t: datetime, local_tz=None) -> datetime:
    """An aware datetime as naive LOCAL time (this machine's zone unless `local_tz` is given)."""
    return (t.astimezone(local_tz) if local_tz is not None else t.astimezone()).replace(tzinfo=None)


def windows(lo: datetime, hi: datetime, local_tz=None) -> list[tuple[datetime, datetime]]:
    """Every maintenance window overlapping [lo, hi), as naive LOCAL (start, end), not clipped.
    `lo`/`hi` are naive LOCAL; `local_tz` is the zone they are in (default: this machine's, like the sidecar)."""
    out = []
    d, last = (lo - timedelta(days=2)).date(), (hi + timedelta(days=2)).date()
    while d <= last:
        if d.weekday() == WEEKDAY:
            a = _local(datetime.combine(d, START, tzinfo=NEW_YORK), local_tz)
            b = _local(datetime.combine(d, END, tzinfo=NEW_YORK), local_tz)
            if a < hi and b > lo:
                out.append((a, b))
        d += timedelta(days=1)
    return out
