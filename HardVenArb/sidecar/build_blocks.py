#!/usr/bin/env python3
"""Author the day's work windows from the live slate, and push them to the running sidecar.

WHY THIS EXISTS
---------------
The lifecycle's own density planner answers "when are games dense?", which is not the question that matters.
It carves fixed-length sessions around clusters and knows nothing about which games are PAIRED, how many sit
on a round-hour start, or how much of the day is left. This script asks the real question: given today's
paired slate and a time budget, which windows cover the most matches?

It is deliberately DETERMINISTIC. The same slate yields the same blocks, so when coverage moves you can tell
whether the schedule changed or the slate did. That property is worth more here than flexible phrasing, and
it is the reason this is a script rather than a prompt.

THE ONE RULE THAT MATTERS: OPEN ON :15 OR :45
---------------------------------------------
Measured 2026-08-25: 69% of a day's tennis starts land exactly on :00 or :30, and the big tournament rounds
are pure round-hour blocks (the 15:00 slot held 17 matches, ALL starting at 15:00 sharp; 18:00 held 16, all
at 18:00). A window opening AT the hour therefore misses the entire cluster it was aimed at - the lifecycle's
+/-7m jitter turned a 15:00 open into 15:06 and lost all 17. Opening at :45/:15 puts even a late-jittered
open at :52/:22, still ahead of the cluster. Every candidate open below is on that grid. Nothing else in
this file matters as much.

RESTARTS
--------
Everything is planned forward from `now`, so a sidecar started at 16:00 simply gets the blocks still to come
- there is no "day plan" that a restart can lose. `--keep-before` additionally freezes blocks that already
opened, so the midday re-run can move LATER blocks without disturbing one that is currently running.

NOTE ON THE DAILY CAP: the lifecycle's spent-hours ledger (`_spent_by_day`) is in memory only, so a restart
forgets what today already burned. Keep `--budget-hours` at or below PINNACLE_MAX_DAILY_HOURS and the cap
never becomes the binding constraint, which is what makes that gap harmless.
"""
from __future__ import annotations
import argparse, datetime as dt, json, os, sys
from typing import NamedTuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import schedule as sched                                   # noqa: E402  (path set immediately above)


class Block(NamedTuple):
    open_min: int          # minutes from local midnight
    close_min: int

    def __str__(self) -> str:
        return (f"{self.open_min // 60:02d}:{self.open_min % 60:02d}-"
                f"{self.close_min // 60:02d}:{self.close_min % 60:02d}")


def hm(s: str) -> int:
    h, m = s.split(":")
    return int(h) * 60 + int(m)


# -- slate ------------------------------------------------------------------------------------------------
def load_starts(sport_ids, horizon_h, pairs_path, paired_only, back_hours=0):
    """Today's game starts, optionally restricted to games we can actually bet.

    back_hours=0 for the STARTS objective: a match that has already started cannot be covered by a window we
    are about to author, so counting it would inflate every candidate equally while changing no decision.
    The IN-PLAY objective passes a few hours: a match LISTED at 10:00 is often still to be played at noon
    ("followed by"), and the board keeps listing it until it goes live.
    """
    starts = sched.fetch_starts(sport_ids, horizon_hours=horizon_h, back_hours=back_hours)
    starts = sched.filter_to_local_day(starts)
    if paired_only:
        mids = sched.paired_mids(pairs_path)
        if mids:
            starts = [g for g in starts if len(g) < 3 or str(g[2]) in mids]
    return starts


# -- the in-play objective (2026-10-12) ---------------------------------------------------------------------
# PINNACLE_BLOCK_OBJECTIVE=inplay scores a window by the tennis it will see LIVE rather than by the listed starts
# it contains. The EV bot trades in-play only and its signal rate per watched hour is flat across a match (63%
# of fills come 1h+ after the listed start), while listed starts are nominal - the median match ENDS 150 min
# after its listed start. A window that "covers" a 10:00 start and closes at 11:15 has mostly missed it.
# live_curve.json (built by live_curve.py from Kalshi's real match ends) gives P(live) per minute after the
# listed start; a window's score is the expected live minutes it holds, summed over matches.
CURVE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "live_curve.json")


def load_curve(path=None):
    """(step_min, [P(live)...]) from live_curve.json, or None - the caller then falls back to starts."""
    try:
        with open(path or CURVE_PATH, encoding="utf-8") as fh:
            j = json.load(fh)
        step, curve = int(j["step_min"]), [float(x) for x in j["curve"]]
        return (step, curve) if step > 0 and curve else None
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _cum_live(curve):
    """C[m] = expected live minutes in the first m minutes after a listed start (1-minute resolution)."""
    step, p = curve
    out = [0.0]
    for m in range(len(p) * step):
        out.append(out[-1] + p[m // step])
    return out


def expected_live(times, blocks, curve, jitter_min=0):
    """Expected live minutes `blocks` hold for matches LISTED at `times` (minutes from local midnight)."""
    c = _cum_live(curve)
    top = len(c) - 1
    cm = lambda x: c[0 if x <= 0 else min(x, top)]
    return sum(cm(b.close_min - jitter_min - t) - cm(b.open_min + jitter_min - t) for t in times for b in blocks)


# -- the optimiser ------------------------------------------------------------------------------------------
def optimise(starts, budget_min, min_gap_min, jitter_min, earliest_min, latest_min, now_min,
             min_len=60, max_len=300, objective="starts", curve=None):
    """Pick non-overlapping windows maximising coverage, subject to a total-minutes budget.

    Coverage is covered STARTS by default, or expected LIVE minutes with objective="inplay" and a curve (see
    above; without a curve it silently stays on starts). Everything else - the grid, gaps, jitter, lengths,
    budget and the DP - is shared, so the two objectives differ in what they count and nothing else.

    Weighted interval scheduling with a budget - a DP over candidates sorted by close time, NOT a search over
    combinations: at ~288 candidates the exhaustive form is C(288,4) ~ 283M and does not finish.

    Coverage is scored under WORST-CASE jitter (open slips `jitter_min` late, close slips `jitter_min` early)
    so a block is only credited with matches it would still catch on the unluckiest day. Because the chosen
    windows are non-overlapping and separated by `min_gap_min`, no start - and no live minute - can fall inside
    two of them, so coverage is additive and the DP is exact rather than a heuristic.
    """
    times = sorted(t.hour * 60 + t.minute for t in (g[0] for g in starts))
    inplay = objective == "inplay" and curve is not None
    if inplay:
        cum = _cum_live(curve)
        top = len(cum) - 1
        cm = lambda x: cum[0 if x <= 0 else min(x, top)]
    # ALIGN THE SCAN TO THE QUARTER-HOUR GRID FIRST. `earliest_min` is usually `now`, an arbitrary minute,
    # and range(908, ..., 15) only ever visits minutes congruent to 908 mod 15 - so the `o % 60 in (15, 45)`
    # test below could never fire and the optimiser silently returned "nothing worth opening" at any wall
    # clock not already on the grid.
    earliest_min = ((earliest_min + 14) // 15) * 15
    cands = []                                             # (close, open, minutes, covered)
    for o in range(earliest_min, latest_min, 15):
        if o % 60 not in (15, 45):                         # the round-hour rule; see module docstring
            continue
        if o < now_min:                                    # never author a window that already opened
            continue
        for length in range(min_len, max_len + 1, 15):
            c = o + length
            if c > latest_min:
                break
            lo, hi = o + jitter_min, c - jitter_min
            if inplay:
                n = sum(cm(hi - t) - cm(lo - t) for t in times)
                if n < 0.5:                                # under half an expected live minute: not a candidate
                    continue
            else:
                n = sum(1 for t in times if lo <= t < hi)
            if n:
                cands.append((c, o, length, n))
    if not cands:
        return []
    cands.sort()
    prev = []                                              # latest candidate ending early enough to precede j
    for j in range(len(cands)):
        o_j = cands[j][1]
        p = -1
        for i in range(j - 1, -1, -1):
            if cands[i][0] <= o_j - min_gap_min:
                p = i
                break
        prev.append(p)
    units = budget_min // 15
    dp = [[(-1, None)] * (units + 1) for _ in range(len(cands) + 1)]
    for j in range(len(cands) + 1):
        dp[j][0] = (0, None)
    for j in range(len(cands)):
        c, o, length, n = cands[j]
        u = length // 15
        for b in range(units + 1):
            best = dp[j][b]
            if u <= b:
                base = dp[prev[j] + 1][b - u]
                if base[0] >= 0 and base[0] + n > best[0]:
                    best = (base[0] + n, (j, b - u))
            dp[j + 1][b] = best
    b = max(range(units + 1), key=lambda x: dp[len(cands)][x][0])
    out = []
    j = len(cands)
    while j > 0:
        val, back = dp[j][b]
        if back is None:
            j -= 1
            continue
        k, nb = back
        if dp[j - 1][b][0] >= val and dp[j - 1][b][1] != back:
            j -= 1
            continue
        out.append(Block(cands[k][1], cands[k][0]))
        j, b = prev[k] + 1, nb
    return sorted(out)


# -- output -----------------------------------------------------------------------------------------------
def push(base_url, pins, jitter, min_games):
    """Hand the blocks to the running sidecar. No .env edit and no restart - set_pins() replans immediately."""
    import httpx
    base = base_url.rstrip("/")
    with httpx.Client(timeout=20.0) as c:
        r = c.post(base + "/control/pins", json={"pins": pins, "reason": "build_blocks"})
        r.raise_for_status()
        print("[BLOCKS] pushed pins -> HTTP %s" % r.status_code)
        body = {"reason": "build_blocks"}
        if jitter is not None:
            body["jitter_min"] = jitter
        if min_games is not None:
            body["min_games"] = min_games
        if len(body) > 1:
            r2 = c.post(base + "/control/schedule", json=body)
            print("[BLOCKS] schedule knobs %s -> HTTP %s" % (body, r2.status_code))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--budget-hours", type=float, default=10.0,
                    help="total open hours to spend (default 10; keep <= PINNACLE_MAX_DAILY_HOURS)")
    ap.add_argument("--min-gap", type=int, default=50,
                    help="minutes between blocks; must clear PINNACLE_MIN_DOWNTIME_MIN (default 50)")
    # DEFAULT None, NOT 0. This flag means "worst-case jitter to PLAN AGAINST", but push() also writes it
    # to the lifecycle as the jitter to APPLY - two different quantities sharing one value. With a default
    # of 0, every --push silently set the live jitter to zero AND persisted it to control_state.json, where
    # it was re-applied on every start and permanently overrode PINNACLE_JITTER_MIN. Diagnosed 2026-08-31:
    # .env said 7, the running plan said 0.0, and every window opened on an exact :15/:45 for days.
    # Unset now means "use whatever the lifecycle is configured with", and push() leaves it alone.
    ap.add_argument("--jitter", type=int, default=None,
                    help="worst-case jitter to plan against (default: PINNACLE_JITTER_MIN, and NOT pushed)")
    ap.add_argument("--earliest", default="06:00")
    ap.add_argument("--latest", default="23:00")
    ap.add_argument("--keep-before", default="",
                    help="HH:MM - freeze blocks opening before this, for the midday re-run")
    ap.add_argument("--keep", default="", help="existing pin spec whose early part --keep-before preserves")
    ap.add_argument("--horizon", type=int, default=30)
    ap.add_argument("--sports", default="", help="Pinnacle sport ids (default: sports.py / HARDVEN_SPORTS)")
    ap.add_argument("--pairs", default="")
    ap.add_argument("--all-games", action="store_true", help="do not restrict to paired games")
    ap.add_argument("--push", default="", help="sidecar base URL, e.g. http://127.0.0.1:8787")
    ap.add_argument("--set-min-games", type=int, default=None,
                    help="also push min_games; a large value suppresses the density planner entirely so the "
                         "authored blocks ARE the plan (see README note)")
    ap.add_argument("--objective", default=os.environ.get("PINNACLE_BLOCK_OBJECTIVE", "starts"),
                    choices=("starts", "inplay"), help="what a window is scored by (default: env or 'starts')")
    ap.add_argument("--curve", default="", help="live_curve.json for --objective inplay (default: beside this file)")
    a = ap.parse_args()
    curve = load_curve(a.curve or None) if a.objective == "inplay" else None
    if a.objective == "inplay" and curve is None:
        print("[BLOCKS] --objective inplay but no usable live_curve.json - scoring by STARTS. Run live_curve.py.")

    pairs_path = a.pairs or str(sched.Path(__file__).resolve().parent.parent / "cross_pairs.json")
    import sports as sports_cfg
    ids = [int(x) for x in a.sports.split(",") if x.strip()] or sports_cfg.pinnacle_ids()
    starts = load_starts(ids, a.horizon, pairs_path, not a.all_games, back_hours=6 if curve else 0)
    now = dt.datetime.now()
    now_min = now.hour * 60 + now.minute
    print("[BLOCKS] %d %sstart(s) today; planning forward from %s"
          % (len(starts), "" if a.all_games else "paired ", now.strftime("%H:%M")))

    frozen = []
    earliest = max(hm(a.earliest), now_min)
    keep = a.keep
    if a.keep_before and not keep and a.push:
        # ASK THE SIDECAR WHAT IT IS CURRENTLY RUNNING rather than making the operator paste it in. A midday
        # re-run that is handed the wrong --keep silently moves a block that is already open, which is the
        # one thing this flag exists to prevent - so the safest source is the live plan itself.
        try:
            import httpx
            with httpx.Client(timeout=15.0) as c:
                keep = ",".join(c.get(a.push.rstrip("/") + "/debug/schedule").json().get("pins") or [])
            print("[BLOCKS] current pins read from the sidecar: %s" % (keep or "(none)"))
        except Exception as ex:
            print("[BLOCKS] could not read current pins (%s: %s) - nothing will be frozen."
                  % (type(ex).__name__, ex))
    if a.keep_before and keep:
        cut = hm(a.keep_before)
        for part in keep.split(","):
            part = part.strip()
            if not part:
                continue
            o, c = (hm(x) for x in part.split("-"))
            if o < cut:                                    # already open or already finished - never move it
                frozen.append(Block(o, c))
        if frozen:
            earliest = max(earliest, max(b.close_min for b in frozen) + a.min_gap)
            print("[BLOCKS] frozen (opened before %s): %s"
                  % (a.keep_before, ", ".join(str(b) for b in frozen)))

    spent = sum(b.close_min - b.open_min for b in frozen)
    budget = max(int(a.budget_hours * 60) - spent, 0)
    # Plan against the jitter the lifecycle will ACTUALLY apply, or coverage is scored against a schedule
    # that will not happen.
    plan_jitter = a.jitter if a.jitter is not None else int(float(os.environ.get("PINNACLE_JITTER_MIN", "7")))
    blocks = optimise(starts, budget, a.min_gap, plan_jitter, earliest, hm(a.latest), now_min,
                      objective="inplay" if curve else "starts", curve=curve)
    allb = sorted(frozen + blocks)
    if not allb:
        print("[BLOCKS] nothing worth opening - leaving the schedule untouched.")
        return 1

    pins = ",".join(str(b) for b in allb)
    times = sorted(t.hour * 60 + t.minute for t in (g[0] for g in starts))
    cov = sum(1 for t in times
              if any(b.open_min + plan_jitter <= t < b.close_min - plan_jitter for b in allb))
    total = sum(b.close_min - b.open_min for b in allb)
    print("[BLOCKS] %s" % pins)
    print("[BLOCKS] %d block(s), %.2fh, covering %d/%d start(s) (%.0f%%)"
          % (len(allb), total / 60.0, cov, len(times), 100.0 * cov / max(len(times), 1)))
    if curve:
        got, full = expected_live(times, allb, curve, plan_jitter), expected_live(times, [Block(0, 2880)], curve)
        print("[BLOCKS] in-play objective: %.1f of %.1f expected live match-hours covered (%.0f%%)"
              % (got / 60, full / 60, 100.0 * got / max(full, 1e-9)))
    if a.push:
        push(a.push, pins, a.jitter, a.set_min_games)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
