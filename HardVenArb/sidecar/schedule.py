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
short gap). DURATION is per-sport (a baseball game runs longer than a best-of-3). Then SELECT the blocks
worth a session: drop any with fewer than MIN_GAMES matches, and keep the densest MAX_BLOCKS (the "3-4 blocks
where the most matches happen"). All knobs are CLI flags.

Times are computed in UTC (Pinnacle startTime is ISO-UTC) and DISPLAYED in the machine's local timezone.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import NamedTuple

import httpx

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from pair_pinnacle import _pin_dt   # ISO-UTC start -> naive UTC datetime (reused so parsing can't drift)
import sports as sports_cfg         # unified sport catalog (ids, names, durations, series)

GUEST_BASE = os.environ.get("PINNACLE_GUEST_BASE", "https://guest.api.arcadia.pinnacle.com/0.1")
GUEST_KEY = os.environ.get("PINNACLE_API_KEY", "CmX2KcMrXuFmNg6YFbmTxE0y9CIrOi0R")
OUT = Path(__file__).resolve().parent.parent / "work_windows.json"

# Pinnacle id -> name and name -> game-length (window tail). From the unified catalog (sports.py) so the sport
# set lives in ONE place; over-estimating the duration just keeps the bot open a touch longer (safer for live
# arbs) than closing mid-game.
SPORT_NAME = sports_cfg.name_by_id()
DURATION = sports_cfg.duration_by_name()
DEFAULT_DURATION = 180


class Game(NamedTuple):
    """One board game. A NamedTuple (not a dataclass) so it stays INDEXABLE — every window function reads
    item[0]/item[1], and the older plain (start, sport) tuples still work unchanged."""
    start: datetime
    sport: str
    mid: str = ""            # Pinnacle matchupId — the key the paired filter and cross_pairs.json share
    label: str = ""          # "Player A vs Player B" for human-readable alerts
    league: str = ""


def _utcnow() -> datetime:
    """Naive UTC 'now' — matches _pin_dt's naive-UTC starts (mixing naive+aware datetimes would raise)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _g(item, i: int, default=""):
    """Field `i` of a start item, tolerating the legacy 2-tuple shape."""
    return item[i] if len(item) > i else default


# ── window math (PURE — unit-testable, no network) ───────────────────────────────────────────────────────
def compute_windows(starts: list[tuple[datetime, str]], lead_min: int = 25, trail_min: int = 45,
                    min_gap_min: int = 60, duration: dict | None = None,
                    min_games: int = 1, max_blocks: int | None = None
                    ) -> list[tuple[datetime, datetime, int]]:
    """starts = [(utc_start, sport), ...] -> selected [(open, close, games), ...] in UTC. Each game spans
    [start-lead, start+dur+trail]; intervals overlapping or within min_gap merge into one window (games = how
    many matches landed in it). Then SELECT the blocks worth a session: drop any with fewer than `min_games`
    matches (not worth a login + warm-up for one isolated game), and if more than `max_blocks` remain keep the
    DENSEST `max_blocks` (most matches; ties → earlier first), restored to chronological order. Defaults
    (min_games=1, max_blocks=None) keep every merged block — selection is opt-in."""
    duration = duration or DURATION
    # `starts` items are (start, sport) or the richer (start, sport, matchup_id) from fetch_starts — index
    # rather than unpack so both shapes work and the paired filter can carry ids through.
    intervals = sorted(
        (it[0] - timedelta(minutes=lead_min),
         it[0] + timedelta(minutes=duration.get(it[1], DEFAULT_DURATION) + trail_min))
        for it in starts)
    merged: list[list] = []
    for o, c in intervals:
        if merged and o <= merged[-1][1] + timedelta(minutes=min_gap_min):
            merged[-1][1] = max(merged[-1][1], c)
            merged[-1][2] += 1
        else:
            merged.append([o, c, 1])
    kept = [w for w in merged if w[2] >= min_games]
    if max_blocks is not None and len(kept) > max_blocks:
        # rank by match count (densest first; ties → earlier), take the top N, restore chronological order
        kept = sorted(sorted(kept, key=lambda w: (-w[2], w[0]))[:max_blocks], key=lambda w: w[0])
    return [(o, c, g) for o, c, g in kept]


def compute_sessions(starts: list[tuple[datetime, str]], session_hours: float = 2.0, lead_min: int = 15,
                     trail_min: int = 45, min_games: int = 2, max_blocks: int = 4
                     ) -> list[tuple[datetime, datetime, int]]:
    """DISCRETE ~session_hours sessions at PEAK game-START density — the right model for CONTINUOUS sports
    (tennis runs worldwide all day, so gap-merging collapses the whole slate into ONE window). Greedily take the
    session_hours span containing the MOST game starts, claim those games, repeat up to max_blocks; drop clusters
    with fewer than min_games. Each session = [first_start - lead, last_start + trail]. Chronological order."""
    win = timedelta(hours=session_hours)
    remaining = sorted(it[0] for it in starts)          # (start, sport[, matchup_id]) → just the starts
    sessions: list[tuple[datetime, datetime, int]] = []
    while remaining and len(sessions) < max_blocks:
        best_i, best_n = 0, 0
        for i, t in enumerate(remaining):                 # densest window always starts at a game (count changes there)
            n = 0
            for u in remaining[i:]:
                if u <= t + win:
                    n += 1
                else:
                    break
            if n > best_n:
                best_n, best_i = n, i
        if best_n < min_games:
            break
        t0 = remaining[best_i]
        claimed = [u for u in remaining if t0 <= u <= t0 + win]
        open_t = min(claimed) - timedelta(minutes=lead_min)
        close_t = max(claimed) + timedelta(minutes=trail_min)
        sessions.append((open_t, close_t, len(claimed)))
        remaining = [u for u in remaining if not (open_t <= u <= close_t)]   # non-overlapping: drop everything in this span
    return sorted(sessions, key=lambda w: w[0])


def _resolve_path(path: str) -> Path:
    """Accept a GIT-BASH style POSIX path on Windows. The sidecar is launched from PowerShell but the operator
    copies paths out of a bash shell, so `/c/Users/...` arrives and Windows Python reads it as `\\c\\Users\\...`
    — file not found, and (before the louder error below) that read as "correctly dark" while actually meaning
    the lifecycle could never open. Translate `/<drive>/rest` → `<DRIVE>:\\rest`; leave everything else alone.
    Also resolves a relative path against this file's directory, not the CWD."""
    p = Path(path)
    if not p.exists():
        m = re.match(r"^[/\\]([A-Za-z])[/\\](.*)$", str(path))
        if m:
            win = Path(f"{m.group(1).upper()}:\\{m.group(2).replace('/', chr(92))}")
            if win.exists():
                return win
        if not p.is_absolute():
            local = Path(__file__).resolve().parent / path
            if local.exists():
                return local
    return p


def load_manual_plan(path: str, base: datetime | None = None) -> list[tuple[datetime, datetime, int]]:
    """Load a MANUAL test plan that OVERRIDES the game slate — for quickly verifying the lifecycle's
    open→wait→close→wait→open cycle WITHOUT waiting for real games. Each entry is either RELATIVE (easy for
    testing): {"open_in": <min from base>, "close_in": <min from base>} — base defaults to now (plan-load time);
    or ABSOLUTE: {"open": <ISO-UTC>, "close": <ISO-UTC>}. Returns [(open_utc, close_utc, games), ...] sorted."""
    base = base or _utcnow()
    data = json.loads(_resolve_path(path).read_text(encoding="utf-8"))
    out: list[tuple[datetime, datetime, int]] = []
    for e in data:
        if "open_in" in e or "close_in" in e:
            o = base + timedelta(minutes=float(e["open_in"]))
            c = base + timedelta(minutes=float(e["close_in"]))
        else:
            o, c = _pin_dt(e.get("open", "")), _pin_dt(e.get("close", ""))
        if o and c and c > o:
            out.append((o, c, int(e.get("games", 1))))
    return sorted(out, key=lambda w: w[0])


def filter_to_local_day(starts: list[tuple[datetime, str]], day=None) -> list[tuple[datetime, str]]:
    """Keep only games whose LOCAL start date == `day` (default: today, local). For per-DAY block planning:
    tomorrow's slate is still filling in, so it shouldn't compete for today's block budget or pull the densest-N
    selection into an incomplete next day. The lifecycle recomputes hourly, so this rolls to the new day on its
    own. `starts` are naive-UTC (from _pin_dt); compared in local time via _local()."""
    day = day or _local(_utcnow()).date()
    return [it for it in starts if _local(it[0]).date() == day]   # keeps the item shape (may carry a matchup id)


# ── schedule around what we can actually BET (paired games), not the whole board ──────────────────────────
def paired_mids(pairs_path: str | None = None) -> set:
    """Pinnacle matchupIds that are PAIRED with a Kalshi market (from cross_pairs.json tokens
    '{lid}:{mid}:{...}'). These are the only games the bot can arb — everything else on the board is
    scenery, and letting scenery drive the schedule buys sessions with nothing to trade."""
    p = Path(pairs_path) if pairs_path else Path(__file__).resolve().parent.parent / "cross_pairs.json"
    try:
        data = json.loads(p.read_text(encoding="utf-8-sig"))
    except Exception:
        return set()
    out = set()
    for e in data:
        for k in ("hardven_yes_token", "hardven_no_token"):
            tok = e.get(k) or ""
            if tok.count(":") >= 2:
                out.add(tok.split(":")[1])
    return out


def filter_to_paired(starts, pairs_path: str | None = None, warn: bool = True):
    """Keep only games whose matchupId is paired with a Kalshi market. Needs `starts` items carrying the id
    (the 3-tuples fetch_starts returns); 2-tuple items have no id and are kept (can't judge them).

    SAFETY: if the pairing file is missing/empty, or the filter would leave NOTHING, return the input
    unchanged — a stale pairing file must never black out the whole schedule."""
    mids = paired_mids(pairs_path)
    if not mids:
        if warn:
            print("[SCHED] no paired matchups found (cross_pairs.json missing/empty) - scheduling on the FULL board.")
        return starts
    kept = [it for it in starts if len(it) < 3 or str(it[2]) in mids]
    if not kept:
        if warn:
            print(f"[SCHED] paired filter removed ALL {len(starts)} game(s) - falling back to the full board "
                  "(is the pairing stale? re-run pairHard.py + pair_pinnacle.py).")
        return starts
    return kept


# ── jitter: a human doesn't clock in at exactly T-15:00 every single day ──────────────────────────────────
def _jitter(anchor: datetime, salt: str, spread_min: float) -> timedelta:
    """DETERMINISTIC offset in [-spread, +spread], derived from the window's own anchor time.

    Deterministic is the whole point: the lifecycle recomputes hourly, and a fresh random() each pass would
    keep sliding the boundary — the bot would flap open/closed around a transition and the "human" rhythm
    would look like a machine twitching. Hashing the anchor gives the same offset every recompute while
    still differing per window and per day."""
    if spread_min <= 0:
        return timedelta(0)
    h = hashlib.sha256(f"{anchor.isoformat()}|{salt}".encode()).digest()
    frac = int.from_bytes(h[:4], "big") / 0xFFFFFFFF          # 0.0 .. 1.0
    return timedelta(minutes=(frac * 2.0 - 1.0) * spread_min)  # -spread .. +spread


# ── operator-pinned hours: "I find more arbs in the morning — be open then" ──────────────────────────────
def parse_pin_hours(spec: str) -> list[tuple[int, int, int, int]]:
    """'09:00-12:00' or '08:30-11:00,20:00-23:30' (LOCAL time) -> [(h1,m1,h2,m2), ...]. An end at or before
    its start means the range crosses midnight ('22:00-02:00'). Bad chunks are skipped with a warning."""
    out = []
    for chunk in (spec or "").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            a, b = chunk.split("-")
            h1, m1 = (list(map(int, a.split(":"))) + [0])[:2]
            h2, m2 = (list(map(int, b.split(":"))) + [0])[:2]
            if not (0 <= h1 < 24 and 0 <= h2 < 24 and 0 <= m1 < 60 and 0 <= m2 < 60):
                raise ValueError(chunk)
            out.append((h1, m1, h2, m2))
        except (ValueError, IndexError):
            print(f"[SCHED] bad pin range {chunk!r} (want 'HH:MM-HH:MM' local) - skipped")
    return out


def pinned_windows(ranges: list[tuple[int, int, int, int]], now: datetime | None = None,
                   days: int = 2) -> list[tuple[datetime, datetime, int]]:
    """The next occurrences (today + tomorrow) of each pinned LOCAL range as UTC-naive windows, past ones
    dropped. games=0 — a pin exists to WATCH LINES (pre-live arbs surface hours before a game starts), not
    because games start inside it; the caller recounts after merging."""
    now = now or _utcnow()
    tz = datetime.now(timezone.utc).astimezone().tzinfo
    today = _local(now).date()
    out = []
    for h1, m1, h2, m2 in ranges:
        for d in range(days):
            day = today + timedelta(days=d)
            o_loc = datetime(day.year, day.month, day.day, h1, m1, tzinfo=tz)
            c_loc = datetime(day.year, day.month, day.day, h2, m2, tzinfo=tz)
            if c_loc <= o_loc:
                c_loc += timedelta(days=1)                  # overnight range crosses midnight
            o = o_loc.astimezone(timezone.utc).replace(tzinfo=None)
            c = c_loc.astimezone(timezone.utc).replace(tzinfo=None)
            if c > now:                                     # keep a pin we are currently inside; drop fully-past
                out.append((o, c, 0))
    return sorted(out, key=lambda w: w[0])


def merge_windows(a: list, b: list) -> list:
    """Union-merge two window lists into disjoint chronological windows (game counts summed; the lifecycle
    recounts them via assign_games afterwards anyway). Used to fold operator pins into the computed plan —
    an overlapping pin EXTENDS a computed window rather than duplicating it."""
    ivs = sorted(list(a) + list(b), key=lambda w: w[0])
    out: list[list] = []
    for o, c, g in ivs:
        if out and o <= out[-1][1]:
            out[-1][1] = max(out[-1][1], c)
            out[-1][2] += g
        else:
            out.append([o, c, g])
    return [(o, c, g) for o, c, g in out]


def carve_out_pins(computed, pins, min_window_min: float = 20.0):
    """Keep operator PINS as their own windows instead of gluing them onto computed sessions.

    `merge_windows` unions on ANY overlap, so a session that ran up to a pin's start became ONE block spanning
    both — observed 2026-08-07/08 as 18:21->02:58 (8h37m), 02:39->14:51 (12h12m) and 05:45->18:48 (13h03m) with
    `PINNACLE_SESSION_HOURS=3`. Nothing downstream caught it: enforce_downtime only spaces windows APART,
    cap_daily_hours bounds the DAY, and max_window_hours is only consulted when FILLING. The session-shape
    setting was therefore silently void whenever a pin happened to abut a session.

    Both intents survive here: the pin keeps its exact stated span (operator instruction), the computed session
    keeps its shape, and `enforce_downtime` then carves a real gap between them. A session overlapped in the
    MIDDLE by a pin splits into two pieces; pieces below `min_window_min` are dropped as not worth a login.
    """
    spans = merge_windows(list(pins), [])          # normalise overlapping pins among themselves
    out = []
    for o, c, g in computed:
        pieces = [(o, c)]
        for po, pc, *_ in spans:
            nxt = []
            for a, b in pieces:
                if pc <= a or po >= b:
                    nxt.append((a, b))             # no overlap
                    continue
                if a < po:
                    nxt.append((a, po))            # keep the head before the pin
                if b > pc:
                    nxt.append((pc, b))            # keep the tail after the pin
            pieces = nxt
        for a, b in pieces:
            if (b - a) >= timedelta(minutes=min_window_min):
                out.append((a, b, g))
    return sorted(out + [tuple(s) for s in spans], key=lambda w: w[0])


def allowed_intervals(ranges: list[tuple[int, int, int, int]], now: datetime | None = None,
                      days: int = 3) -> list[tuple[datetime, datetime]]:
    """Local HH:MM-HH:MM ranges materialised as UTC-naive intervals, from YESTERDAY through `days` ahead.

    Starts a day EARLY on purpose: an overnight range ('22:00-02:00') that opened yesterday is still running
    now, and generating only from today would clip the plan against an interval that had not started yet."""
    now = now or _utcnow()
    tz = datetime.now(timezone.utc).astimezone().tzinfo
    today = _local(now).date()
    out = []
    for h1, m1, h2, m2 in ranges:
        for d in range(-1, days):
            day = today + timedelta(days=d)
            o_loc = datetime(day.year, day.month, day.day, h1, m1, tzinfo=tz)
            c_loc = datetime(day.year, day.month, day.day, h2, m2, tzinfo=tz)
            if c_loc <= o_loc:
                c_loc += timedelta(days=1)              # overnight range crosses midnight
            out.append((o_loc.astimezone(timezone.utc).replace(tzinfo=None),
                        c_loc.astimezone(timezone.utc).replace(tzinfo=None)))
    return sorted(out)


def clip_to_allowed(windows, ranges: list[tuple[int, int, int, int]], now: datetime | None = None,
                    min_window_min: float = 20.0, days: int = 3):
    """Restrict a plan to operator ALLOWED hours — the INVERSE of a pin, and the only rule here that can
    say "never be up then".

    Everything else in this module ADDS or CAPS: `pinned_windows` adds hours, `merge_windows` unions them,
    `fill_daily_hours` spends slack, and the caps bound DURATION ("at most 10h/day", "3h per session") with
    no opinion about WHICH hours. So "only run 05:00-08:00" was previously unexpressible, and reaching for
    PINNACLE_PIN_HOURS did the opposite of what the name suggests to an operator.

    Intersects each planned window with the allowed set; fragments shorter than `min_window_min` are dropped
    (not worth a login), and adjacent fragments are re-merged so contiguous ranges like
    '10:00-12:00,12:00-15:00' behave as one 10:00-15:00 block rather than two windows with a phantom gap.

    PINS ARE NOT EXEMPT. Every other bound protects them because a pin is an explicit operator instruction —
    but so is this, and it is the stricter one. "Only these hours" that a pin could escape would be a lie.

    An EMPTY `ranges` disables the restriction entirely (returns the plan untouched), so an unset or
    unparseable env can never silently ground the bot.
    """
    if not ranges or not windows:
        return list(windows)
    allowed = allowed_intervals(ranges, now=now, days=days)
    floor = timedelta(minutes=min_window_min)
    out = []
    for o, c, g in windows:
        first = True
        for ao, ac in allowed:
            s, e = max(o, ao), min(c, ac)
            if e - s < floor:
                continue
            # Games ride the FIRST surviving fragment only. A split would otherwise report the window's whole
            # game count on each piece; the lifecycle re-attributes afterwards, but inflating a count that
            # feeds "is this window worth opening" is the wrong direction to be wrong in.
            out.append((s, e, g if first else 0))
            first = False
    return merge_windows(out, [])


def cap_window_length(windows, max_hours: float, protected=None, min_window_min: float = 20.0):
    """Hard ceiling on ANY SINGLE window — the backstop `max_window_hours` never was.

    Truncates from the CLOSE (the open carries the lead time that finds pre-live edges). Operator pins are
    exempt: a pinned span is an explicit instruction about which hours to be up, not a computed guess, so a 4h
    pin stays 4h even under a 3h session shape.
    """
    if max_hours <= 0 or not windows:
        return list(windows)
    lim = timedelta(hours=max_hours)
    floor = timedelta(minutes=min_window_min)
    out = []
    for o, c, g in windows:
        if _is_protected(o, c, protected) or (c - o) <= lim:
            out.append((o, c, g))
        elif lim >= floor:
            out.append((o, o + lim, g))
    return out


def enforce_downtime(windows, min_downtime_min: float, min_window_min: float = 20.0):
    """Guarantee at least `min_downtime_min` of browser-DOWN time between consecutive windows.

    Why this is needed: `merge_windows` unions on ANY overlap, so a chain of adjacent sessions plus an operator
    pin collapses into one very long block — observed 2026-08-07 as a single 12:37->19:45 window (7h08m). No
    other rule bounds a window's LENGTH, so without this the bot can legitimately plan itself an all-day session
    and never stand the browser down.

    Policy: pull the EARLIER window's close back; never delay the later window's open. Pre-live edges surface
    hours before a start, so the later block's LEAD time is worth more than the earlier block's TRAIL (which
    only buys settlement/void reads). A window trimmed below `min_window_min` isn't worth a login and is dropped.
    Expects chronological, disjoint windows — i.e. run this AFTER merge_windows and AFTER apply_jitter, since
    jitter can itself close a gap.
    """
    if min_downtime_min <= 0 or len(windows) < 2:
        return list(windows)
    gap = timedelta(minutes=min_downtime_min)
    floor = timedelta(minutes=min_window_min)
    ws = sorted(windows, key=lambda w: w[0])
    out = []
    for i, (o, c, g) in enumerate(ws):
        if i + 1 < len(ws):
            latest_close = ws[i + 1][0] - gap
            if c > latest_close:
                c = latest_close
        if c - o >= floor:
            out.append((o, c, g))
    return out


def cap_daily_hours(windows, max_hours: float, protected=None, min_window_min: float = 20.0,
                    spent=None, now: datetime | None = None):
    """Bound the TOTAL open time per LOCAL day — the ceiling that stops a dense slate from planning a 24/7
    session.

    THE BUDGET IS THE DAY'S, NOT THE PLAN'S. The hourly recompute rebuilds windows from scratch and
    `fetch_starts(back_hours=4)` drops older blocks off the front, so a plan-local cap would hand out a fresh
    full allowance every rebuild: 5h this morning + a noon rebuild = another 5h, against a "5h" cap. Two things
    make it a real daily ceiling:
      * `spent` = {local_date: timedelta} of uptime ALREADY BURNED today (the lifecycle measures actual
        open->close time), subtracted from that day's budget;
      * each window is charged only its REMAINING time (from `now`), so the in-progress window isn't
        double-counted against the elapsed time that produced it.

    Spends what's left in priority order: `protected` windows first, then most games, then earliest. The window
    that only PARTIALLY fits is trimmed to the remainder (dropped if that is under `min_window_min`); the rest
    are dropped. Fully-past windows are kept untouched and charged nothing — they are history, not plan.

    Operator pins are `protected`: they still CONSUME budget (a cap pins can silently blow through is not a
    cap) but are never trimmed or dropped — a pin is an explicit instruction, not a guess. If pins alone exceed
    the budget they all survive and everything else sleeps.

    A window is attributed to the local day it OPENS on and charged there: simpler and more predictable than
    splitting an overnight block across two budgets.
    """
    if max_hours <= 0 or not windows:
        return list(windows)
    now = now or _utcnow()
    spent = spent or {}
    floor = timedelta(minutes=min_window_min)
    by_day: dict = {}
    for w in windows:
        by_day.setdefault(_local(w[0]).date(), []).append(w)
    out = []
    for day, ws in by_day.items():
        budget = timedelta(hours=max_hours) - spent.get(day, timedelta())
        order = sorted(ws, key=lambda w: (0 if _is_protected(w[0], w[1], protected) else 1, -w[2], w[0]))
        used = timedelta()
        for o, c, g in order:
            anchor = max(o, now)                 # only the FUTURE part of a window costs budget
            remaining = c - anchor
            if remaining <= timedelta():
                out.append((o, c, g))            # already over — history, charge nothing
                continue
            if _is_protected(o, c, protected):
                out.append((o, c, g))
                used += remaining
                continue
            left = budget - used
            if left <= timedelta():
                continue                         # budget gone — this block sleeps
            if remaining <= left:
                out.append((o, c, g))
                used += remaining
            elif left >= floor:
                out.append((o, anchor + left, g))   # partial fit: take the front of what's left
                used += left
    return sorted(out, key=lambda w: w[0])


def _is_protected(o: datetime, c: datetime, spans) -> bool:
    """Does this window OVERLAP an operator-pinned span? Overlap, not tuple equality: by the time the bounds run,
    merge_windows and apply_jitter have both rewritten the tuples, so an exact-match test silently never fires
    and pins lose their protection. Mirrors PinnacleLifecycle._is_pinned so both answer the same question."""
    return any(po < c and o < pc for po, pc, *_ in (spans or []))


def _midnight_utc(day, plus_days: int = 0) -> datetime:
    """Local midnight of `day` (+plus_days) as a UTC-naive datetime — the edge a window may not grow past, so
    stretching today's plan can't quietly spend tomorrow's budget."""
    tz = datetime.now(timezone.utc).astimezone().tzinfo
    d = day + timedelta(days=plus_days)
    return datetime(d.year, d.month, d.day, tzinfo=tz).astimezone(timezone.utc).replace(tzinfo=None)


def fill_daily_hours(windows, max_hours: float, spent=None, now: datetime | None = None,
                     min_downtime_min: float = 0.0, max_window_hours: float = 0.0, protected=None):
    """Grow the plan TOWARD the daily ceiling — the cap as a TARGET, not just a limit.

    `cap_daily_hours` only ever removes time, so on a thin slate the bot sits dark with budget unspent. Watching
    is what finds pre-live edges (they surface hours before a start), so unused budget is unused opportunity.

    Expansion is EARLIER-OPEN FIRST, then later-close: more lead time is worth more than more trail, for the
    same reason `enforce_downtime` trims closes rather than delaying opens. Room is bounded by the neighbouring
    window plus `min_downtime_min` (so filling can never eat the downtime the operator asked for), by `now` (no
    growing into the past), and by local midnight (so today can't spend tomorrow's budget). When there isn't
    enough budget for everyone, each window gets a share proportional to the room it has.

    TWO THINGS FILL MUST NOT DO, both found on the live plan 2026-08-07:
      * `max_window_hours` — never stretch a window past the SHAPE the operator configured. Under
        `PINNACLE_SESSION_HOURS=2` a session is ~2h+lead+trail; filling had grown them to 5.2h and 3.5h, which
        silently repeals the setting. On a thin slate the day now simply lands under the cap — the lever for
        more hours is `max_blocks`/`min_games` (more sessions), not longer ones.
      * `protected` — never stretch an operator PIN. A pin is a stated span ("06:00-08:00"), not a seed to grow
        from; filling had turned a 2h morning pin into a 10h block opening at midnight.

    Run AFTER cap_daily_hours: cap enforces the ceiling, fill takes up the slack under it.
    """
    if max_hours <= 0 or not windows:
        return list(windows)
    now = now or _utcnow()
    spent = spent or {}
    gap = timedelta(minutes=min_downtime_min)
    by_day: dict = {}
    for w in windows:
        by_day.setdefault(_local(w[0]).date(), []).append(w)
    out = []
    today = _local(now).date()
    for day, ws in by_day.items():
        ws = sorted(ws, key=lambda x: x[0])
        # ONLY fill the current day. With `today_only` planning, a future day holds nothing but the operator's
        # pins — its games have not been fetched yet — so filling it inflates a placeholder against a slate we
        # cannot see. Observed 2026-08-07: tomorrow's 06:00-08:00 pin was stretched to a 10h block starting at
        # MIDNIGHT. That day gets replanned (and filled) once its games exist.
        if day != today:
            out.extend(ws)
            continue
        planned = sum(((c - max(o, now)) for o, c, _ in ws if c > max(o, now)), timedelta())
        leftover = timedelta(hours=max_hours) - spent.get(day, timedelta()) - planned
        if leftover <= timedelta():
            out.extend(ws)
            continue
        lo_edge, hi_edge = _midnight_utc(day), _midnight_utc(day, 1)
        opens = [o for o, _, _ in ws]
        closes = [c for _, c, _ in ws]
        # TWO PHASES, because the space between two windows belongs to BOTH of them and can only be spent once.
        # Sizing each window's room against its ORIGINAL neighbours double-counts that gap and lets the pair
        # expand straight through the downtime (caught by test: a 4h gap handed 3h to each side).
        # Phase 1 moves OPENS earlier (the preferred direction); phase 2 then moves CLOSES later against the
        # opens as they now stand, so what phase 1 consumed is no longer on offer.
        # How much each window is ALLOWED to grow in total, before we look at where there is room.
        cap_w = timedelta(hours=max_window_hours) if max_window_hours > 0 else None
        headroom = []
        for o, c, _g in ws:
            if _is_protected(o, c, protected):
                headroom.append(timedelta())                       # a pin is a stated span, not a seed
            elif cap_w is not None:
                headroom.append(max(timedelta(), cap_w - (c - o)))  # never exceed the configured session shape
            else:
                headroom.append(None)                              # unbounded
        early = []
        for i in range(len(ws)):
            lower = max(closes[i - 1] + gap, now, lo_edge) if i else max(now, lo_edge)
            room = max(timedelta(), opens[i] - lower)
            if headroom[i] is not None:
                room = min(room, headroom[i])
            early.append(room)
        tot_e = sum(early, timedelta())
        if tot_e > timedelta():
            scale = min(1.0, leftover / tot_e)
            for i in range(len(ws)):
                opens[i] -= early[i] * scale
                if headroom[i] is not None:
                    headroom[i] -= early[i] * scale      # phase 1 spends the window's own allowance
            leftover -= tot_e * scale
        if leftover > timedelta():
            late = []
            for i in range(len(ws)):
                upper = (opens[i + 1] - gap) if i + 1 < len(ws) else hi_edge
                room = max(timedelta(), upper - closes[i])
                if headroom[i] is not None:
                    room = min(room, headroom[i])
                late.append(room)
            tot_l = sum(late, timedelta())
            if tot_l > timedelta():
                scale = min(1.0, leftover / tot_l)
                for i in range(len(ws)):
                    closes[i] += late[i] * scale
        out.extend((opens[i], closes[i], ws[i][2]) for i in range(len(ws)))
    return sorted(out, key=lambda w: w[0])


def assign_games(windows, starts):
    """Attribute every game to the window that will cover it → (per_window, left_behind).

    `per_window` is a list parallel to `windows` of the games inside each one; `left_behind` is every game
    that falls in NO selected window (a block dropped by min_games/max_blocks, or a gap the bot sleeps
    through). This is what turns "3 windows, 27 games" into "these are the matches we're up for, and these
    are the ones we're skipping" — the answer an operator actually wants from an alert."""
    per = [[] for _ in windows]
    left: list = []
    for it in starts:
        s = it[0]
        for i, (o, c, _g_) in enumerate(windows):
            if o <= s <= c:
                per[i].append(it)
                break
        else:
            left.append(it)
    return per, left


def describe_games(games, limit: int = 6) -> str:
    """Compact human list for an alert: 'Kym vs Houkes 14:30, Dodig vs Giustino 15:00 (+3 more)'."""
    if not games:
        return "none"
    out = []
    for it in sorted(games, key=lambda x: x[0])[:limit]:
        lbl = _g(it, 3) or _g(it, 2) or _g(it, 1)
        out.append(f"{lbl} {_local(it[0]):%H:%M}")
    extra = len(games) - len(out)
    return ", ".join(out) + (f" (+{extra} more)" if extra > 0 else "")


def apply_jitter(windows, spread_min: float = 0.0, mode: str = "ends"):
    """Nudge each window's open/close by a stable pseudo-random amount so the session rhythm isn't
    machine-precise. Applied AFTER block selection (never changes WHICH blocks are chosen), keeps each
    window at least 10 minutes long, and trims any overlap jitter introduces so windows stay disjoint.

    TWO MODES, because at large spreads they mean very different things:

      "ends"  (default) jitters open and close INDEPENDENTLY. Right for small nudges (the original ~7min
              use): a session starts a few minutes late and runs a few minutes over, as people do.

      "shift" moves the window as a UNIT, preserving its length. Right for a BIG spread. At +/-60min on a
              2h window "ends" can draw open +60 and close -60 and produce a 10-minute session, or the
              reverse and produce a 4-hour one — so the length becomes the random variable, which is not
              what "be open for two hours, starting somewhere around 8" means. A person's session drifts
              in START time; it does not randomly double or vanish.
    """
    if spread_min <= 0 or not windows:
        return windows
    out = []
    for o, c, g in windows:
        if mode == "shift":
            d = _jitter(o, "shift", spread_min)
            o2, c2 = o + d, c + d
        else:
            o2 = o + _jitter(o, "open", spread_min)
            c2 = c + _jitter(o, "close", spread_min)
        if c2 - o2 < timedelta(minutes=10):                   # jitter must never invert/erase a window
            o2, c2 = o, c
        if out and o2 < out[-1][1]:                           # keep windows disjoint after the nudge
            o2 = out[-1][1] + timedelta(minutes=1)
        if c2 > o2:
            out.append((o2, c2, g))
    return out


def active_window(windows, now: datetime | None = None):
    """The window (open, close, games) containing `now` (UTC naive), or None."""
    now = now or _utcnow()
    return next((w for w in windows if w[0] <= now <= w[1]), None)


def status(windows, now: datetime | None = None) -> tuple[str, float | None]:
    """('OPEN', seconds_until_close) if inside a window; else ('CLOSED', seconds_until_next_open) or
    ('CLOSED', None) if no upcoming window. The bot polls this to decide open vs dark."""
    now = now or _utcnow()
    cur = active_window(windows, now)
    if cur:
        return "OPEN", (cur[1] - now).total_seconds()
    upcoming = [w[0] for w in windows if w[0] > now]
    return "CLOSED", ((min(upcoming) - now).total_seconds() if upcoming else None)


# ── slate (GUEST API — account-free) ─────────────────────────────────────────────────────────────────────
def _guest(client: httpx.Client, path: str):
    try:
        r = client.get(GUEST_BASE + path)
        return r.json() if r.status_code == 200 else None
    except Exception:
        return None


def fetch_starts(sports: list[int], horizon_hours: int = 36, back_hours: int = 4) -> list[tuple]:
    """(start_utc, sport, matchup_id) for the scoped sports from the guest board, within [now-back,
    now+horizon]. Only the MAIN matchup per game (parentId is None, type 'matchup') so each game counts once;
    doubles and derivative '(Games)' children are skipped. `back_hours` keeps already-started games so live
    ones still fall inside their window. The matchup id lets filter_to_paired() drop games we can't bet."""
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
                    parts = m.get("participants") or []
                    home = next((p.get("name", "") for p in parts if p.get("alignment") == "home"), "")
                    away = next((p.get("name", "") for p in parts if p.get("alignment") == "away"), "")
                    label = f"{home} vs {away}" if (home and away) else (lg.get("name") or sport)
                    out.append(Game(st, sport, str(m.get("id")), label, lg.get("name") or ""))
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
    ap.add_argument("--sports", default=",".join(str(i) for i in sports_cfg.pinnacle_ids()),
                    help="Pinnacle sport ids (default = active sports from sports.py / HARDVEN_SPORTS)")
    ap.add_argument("--horizon", type=int, default=36, help="plan this many hours ahead (default 36)")
    ap.add_argument("--lead", type=int, default=15, help="open this many min before a block's first game (default 15)")
    ap.add_argument("--trail", type=int, default=45, help="close this many min after the last game's end")
    ap.add_argument("--min-gap", type=int, default=60, help="merge blocks less than this many min apart")
    ap.add_argument("--min-games", type=int, default=1,
                    help="drop blocks with fewer than this many matches (default 1 = keep all)")
    ap.add_argument("--max-blocks", type=int, default=4,
                    help="keep at most this many blocks, the densest by match count (default 4; 0 = unlimited)")
    ap.add_argument("--session-hours", type=float, default=0.0,
                    help="SESSION mode: carve the densest N fixed ~this-many-hour sessions by game-START density "
                         "(for continuous sports like tennis where gap-merge collapses the day into one block). "
                         "0 = OFF (use the gap-merge window model). Try 2.")
    ap.add_argument("--today-only", action="store_true",
                    help="plan blocks for the CURRENT LOCAL DAY only (drop tomorrow's games so an incomplete "
                         "next-day slate can't skew the densest-N selection). Matches the bot's default.")
    ap.add_argument("--all-games", action="store_true",
                    help="schedule on the WHOLE board instead of only Kalshi-PAIRED games (default: paired "
                         "only — unpaired games can't be arbed, so they shouldn't buy a session)")
    ap.add_argument("--jitter", type=float, default=0.0,
                    help="randomize each window by up to +/- this many minutes (deterministic per window, so "
                         "it doesn't drift across recomputes). Try 7 with --jitter-mode ends, 60 with shift.")
    ap.add_argument("--jitter-mode", choices=("ends", "shift"), default="ends",
                    help="'ends' jitters open/close independently (small nudges); 'shift' moves the whole "
                         "window and KEEPS ITS LENGTH (use for a big spread — see apply_jitter)")
    ap.add_argument("--pin-only", action="store_true",
                    help="plan ONLY the --pin hours: no density blocks at all. For a fixed two-block day "
                         "where the slate decides nothing.")
    ap.add_argument("--pin", default="",
                    help="operator-pinned LOCAL hours always included in the plan, e.g. '09:00-12:00' or "
                         "'08:30-11:00,20:00-23:00' (immune to --min-games/--max-blocks; matches "
                         "PINNACLE_PIN_HOURS in the bot)")
    ap.add_argument("--write", action="store_true", help="also write work_windows.json for the bot")
    args = ap.parse_args()

    sports = [int(s) for s in args.sports.split(",") if s.strip()]
    print(f"[SCHED] fetching slate (sports={sports}, horizon={args.horizon}h) from the guest board …")
    starts = fetch_starts(sports, args.horizon)
    if args.today_only:
        starts = filter_to_local_day(starts)
        print(f"[SCHED] today-only: {len(starts)} game(s) remain on the current local day.")
    if not args.all_games:
        n_before = len(starts)
        starts = filter_to_paired(starts)
        print(f"[SCHED] paired-only: {len(starts)}/{n_before} game(s) are paired with a Kalshi market.")
    bysport = {}
    for it in starts:
        sp = it[1]
        bysport[sp] = bysport.get(sp, 0) + 1
    print(f"[SCHED] {len(starts)} games: " + ", ".join(f"{k}={v}" for k, v in sorted(bysport.items())))

    max_blocks = args.max_blocks or None      # 0 = unlimited
    if args.session_hours > 0:                 # SESSION mode: discrete density sessions (continuous-sport friendly)
        windows = compute_sessions(starts, args.session_hours, args.lead, args.trail,
                                   min_games=args.min_games, max_blocks=args.max_blocks or 4)
        sel = f" (densest {len(windows)} × ~{args.session_hours:g}h sessions by game-start density)"
    else:                                      # WINDOW mode: gap-merged blocks (good for clustered sports)
        all_merged = compute_windows(starts, args.lead, args.trail, args.min_gap)
        windows = compute_windows(starts, args.lead, args.trail, args.min_gap,
                                  min_games=args.min_games, max_blocks=max_blocks)
        dropped = len(all_merged) - len(windows)
        sel = f" (selected the densest {len(windows)} of {len(all_merged)}; dropped {dropped})" if dropped else ""
    pins = pinned_windows(parse_pin_hours(args.pin)) if args.pin else []
    if pins and args.pin_only:
        # The slate decides nothing here: these two blocks ARE the day. Density selection is skipped
        # entirely rather than merged, so a busy afternoon cannot bolt a third session onto the plan.
        windows = pins
        sel = f" (PIN-ONLY: {len(pins)} operator block(s); the slate was not consulted)"
    elif pins:
        windows = merge_windows(windows, pins)
        sel += f" [+{len(pins)} pinned]"
    if args.jitter > 0:
        windows = apply_jitter(windows, args.jitter, mode=args.jitter_mode)
        sel += f" [jitter +/-{args.jitter:g}m {args.jitter_mode}]"
    per, _left = assign_games(windows, starts)
    windows = [(o, c, len(per[i])) for i, (o, c, _n) in enumerate(windows)]
    print(f"\n[SCHED] {len(windows)} work window(s){sel} (local time):")
    now = _utcnow()
    for i, (o, c, g) in enumerate(windows):
        live = "  <== NOW" if o <= now <= c else ""
        pin = "  [PINNED]" if any(po < c and o < pc for po, pc, _g2 in pins) else ""
        dur = _hm((c - o).total_seconds())
        lo, lc = _local(o), _local(c)
        c_fmt = f"{lc:%H:%M}" if lc.date() == lo.date() else f"{lc:%a %d %b %H:%M}"   # show date if it spills to another day
        print(f"   {lo:%a %d %b %H:%M} -> {c_fmt}  ({dur}, {g} match{'es' if g != 1 else ''}){pin}{live}")

    state, secs = status(windows, now)
    if state == "OPEN":
        print(f"\n[SCHED] NOW: OPEN — work for another {_hm(secs)} (then close).")
    else:
        print(f"\n[SCHED] NOW: CLOSED (dark) — next open in {_hm(secs)}." if secs is not None
              else "\n[SCHED] NOW: CLOSED — no upcoming games in the horizon.")

    if args.write:
        OUT.write_text(json.dumps([{"open": o.isoformat() + "Z", "close": c.isoformat() + "Z", "games": g}
                                   for o, c, g in windows], indent=2), encoding="utf-8")
        print(f"[SCHED] wrote {len(windows)} window(s) -> {OUT}")


if __name__ == "__main__":
    main()
