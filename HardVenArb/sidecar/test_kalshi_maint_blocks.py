"""
test_kalshi_maint_blocks.py - Kalshi's weekly halt in the block planner (2026-11-19). Thursdays 03:00-05:00 New York
the EV bot cannot trade, so the planner values those minutes at zero and spends the day's budget elsewhere; and
hour_value.py no longer counts them as watched. 11-19's real plan opened 07:39-12:40 and spent two hours in the halt.

    python test_kalshi_maint_blocks.py            # all PASS expected
"""
import contextlib
import io
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import block_scheduler as bsch                              # noqa: E402
import build_blocks as bb                                   # noqa: E402
import hour_value as hv                                     # noqa: E402
import kalshi_hours as kh                                   # noqa: E402
import schedule as sched                                    # noqa: E402

UTC = timezone.utc


def check(name, got, want):
    ok = got == want
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}: {got} (expected {want})")
    return ok


def overlap(blocks, dead):
    return sum(max(0, min(b.close_min, hi) - max(b.open_min, lo)) for b in blocks for lo, hi in dead)


def skips(blocks, dead):
    """No block spans the halt: each touches it at most by one 15-minute grid step at an edge (an open at 09:45
    for a 10:00 reopen is even useful - the session is up and subscribed when trading resumes)."""
    return all(overlap([b], dead) <= 15 for b in blocks)


class FakeLifecycle:
    def __init__(self, pins):
        self.pins = pins

    def status(self):
        return {"pins": list(self.pins)}


def main() -> int:
    r = []
    D = lambda *a: datetime(*a)

    # ── the halt in local time, DST on both ends ────────────────────────────────
    r.append(check("Thu 11-19 (US winter) = 08:00-10:00 UTC",
                   kh.windows(D(2026, 11, 19), D(2026, 11, 20), UTC), [(D(2026, 11, 19, 8), D(2026, 11, 19, 10))]))
    r.append(check("Thu 07-16 (US summer) = 07:00-09:00 UTC",
                   kh.windows(D(2026, 7, 16), D(2026, 7, 17), UTC), [(D(2026, 7, 16, 7), D(2026, 7, 16, 9))]))
    r.append(check("the Thursday before the Nov 1 switch is still summer (07-09)",
                   kh.windows(D(2026, 10, 29), D(2026, 10, 30), UTC)[0][0], D(2026, 10, 29, 7)))
    r.append(check("the first Thursday after it is winter (08-10)",
                   kh.windows(D(2026, 11, 5), D(2026, 11, 6), UTC)[0][0], D(2026, 11, 5, 8)))
    r.append(check("March: the Thursday after the Mar 8 switch is summer again (07-09)",
                   kh.windows(D(2026, 3, 12), D(2026, 3, 13), UTC)[0][0], D(2026, 3, 12, 7)))
    r.append(check("Mon..Wed holds no halt", kh.windows(D(2026, 11, 16), D(2026, 11, 19), UTC), []))
    r.append(check("a range starting inside the halt still returns it, unclipped",
                   kh.windows(D(2026, 11, 19, 9), D(2026, 11, 19, 12), UTC), [(D(2026, 11, 19, 8), D(2026, 11, 19, 10))]))
    r.append(check("in another local zone (Berlin) the same halt is 09:00-11:00",
                   kh.windows(D(2026, 11, 19), D(2026, 11, 20), ZoneInfo("Europe/Berlin")),
                   [(D(2026, 11, 19, 9), D(2026, 11, 19, 11))]))

    # ── on the plan's minute scale ───────────────────────────────────────────────
    r.append(check("Thursday's service day (to 02:00 = 1560): minutes 480-600",
                   bb.kalshi_dead_minutes(D(2026, 11, 19), 1560, UTC), [(480, 600)]))
    r.append(check("Wednesday's service day ends Thu 02:00, before the halt: nothing",
                   bb.kalshi_dead_minutes(D(2026, 11, 18), 1560, UTC), []))
    r.append(check("clipped to the plan's span", bb.kalshi_dead_minutes(D(2026, 11, 19), 540, UTC), [(480, 540)]))

    # ── the optimiser: halt minutes are worth zero, the budget goes elsewhere ────
    curve = (5, [1.0] * 18)                                     # live for 90 minutes from the listed start
    times = [450 + 15 * k for k in range(12)] * 3 + list(range(360, 1500, 30))   # a peak 07:30-10:15 + all day
    dead = [(480, 600)]
    base = bb.optimise([], 600, 50, 7, 360, 1560, 0, objective="inplay", curve=curve, times=times)
    skip = bb.optimise([], 600, 50, 7, 360, 1560, 0, objective="inplay", curve=curve, times=times, dead=dead)
    r.append(check("without the rule the plan sits on the 08-10 peak (the 11-19 shape)", overlap(base, dead) >= 90, True))
    print(f"         plans: without {[str(b) for b in base]}  with {[str(b) for b in skip]}")
    r.append(check("with it, no block spans the halt (15-min grid edges only)", skips(skip, dead), True))
    spent = lambda bl: sum(b.close_min - b.open_min for b in bl)
    r.append(check("  ...and the same 10h budget is still spent (re-allocated, not dropped)", spent(skip), spent(base)))
    r.append(check("  ...for more tradeable value than the old plan had",
                   bb.expected_live(times, skip, curve, 7, dead=dead) > bb.expected_live(times, base, curve, 7, dead=dead),
                   True))
    r.append(check("  ...on the :15/:45 grid as always", all(b.open_min % 60 in (15, 45) for b in skip), True))
    r.append(check("dead=[] is exactly the old plan",
                   bb.optimise([], 600, 50, 7, 360, 1560, 0, objective="inplay", curve=curve, times=times, dead=[]), base))
    w = [1.0] * 24
    w[20] = w[21] = 2.0
    wb = bb.optimise([], 600, 50, 7, 360, 1560, 0, objective="inplay", curve=curve, times=times, weights=w, dead=dead)
    r.append(check("with hour weights too, the halt stays out", skips(wb, dead), True))
    r.append(check("a block wholly inside the halt is worth nothing",
                   bb.expected_live([480], [bb.Block(480, 600)], curve, 0, dead=dead), 0.0))
    r.append(check("expected_live counts only the minutes after the reopen",
                   bb.expected_live([540], [bb.Block(540, 660)], curve, 0, dead=dead), 30.0))
    r.append(check("  ...and without dead, the whole 90",
                   bb.expected_live([540], [bb.Block(540, 660)], curve, 0), 90.0))
    r.append(check("the starts objective is untouched by dead (in-play only)",
                   bb.optimise([(D(2026, 11, 19, 9),)] * 5, 300, 50, 7, 360, 1380, 0, dead=dead),
                   bb.optimise([(D(2026, 11, 19, 9),)] * 5, 300, 50, 7, 360, 1380, 0)))

    # ── the scheduler on a real-shaped Thursday (machine-local clock, like the sidecar) ──
    real_load = bb.load_starts
    try:
        thu, wed = datetime(2026, 11, 19), datetime(2026, 11, 18)
        utc = lambda local_naive: local_naive.astimezone(UTC).replace(tzinfo=None)
        hours = [7.5, 7.75, 8, 8, 8.25, 8.5, 8.5, 9, 9, 9.25, 9.5, 10, 10.5, 11, 12, 13, 14, 15, 16, 17, 18,
                 19, 20, 21, 22, 23]
        board = [sched.Game(utc(day + timedelta(hours=h)), "Tennis", f"{day.day}-{i}")
                 for day in (wed, thu) for i, h in enumerate(hours)]
        bb.load_starts = lambda *a, **k: [g for g in board if k["window"][0] <= bb.local_naive(g[0]) < k["window"][1]]

        def plan(skip, day=thu):
            s = bsch.BlockScheduler(FakeLifecycle([]), [33], "unused.json", hours="06,12,18", budget_h=16.0,
                                    min_gap=50, jitter=7, earliest="06:00", latest="02:00", objective="inplay",
                                    skip_kalshi_maint=skip)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                spec = s._author("test", now=day.replace(hour=6))
            return s, spec, out.getvalue()

        s_on, spec_on, log_on = plan(True)
        s_off, spec_off, _ = plan(False)
        local_dead = bb.kalshi_dead_minutes(thu, 1560)            # this machine's zone, as the scheduler uses
        blocks_on = [bb.Block(*s_on.pin_minutes(x)) for x in spec_on.split(",")]
        blocks_off = [bb.Block(*s_off.pin_minutes(x)) for x in spec_off.split(",")]
        print(f"         Thursday plans: rule off {spec_off}  |  rule on {spec_on}")
        r.append(check("Thursday, rule off: the plan opens across the halt (what 11-19 did)",
                       overlap(blocks_off, local_dead) >= 90, True))
        r.append(check("Thursday, rule on: no block spans the halt", skips(blocks_on, local_dead), True))
        r.append(check("  ...and its hours are re-allocated, not dropped (at least as many open hours)",
                       spent(blocks_on) >= spent(blocks_off), True))
        r.append(check("  ...said so in the log", "Kalshi's weekly maintenance" in log_on and "counts as zero" in log_on, True))
        r.append(check("status() reports the rule", s_on.status()["skip_kalshi_maint"], True))
        _, spec_wed_on, log_wed = plan(True, wed)
        _, spec_wed_off, _ = plan(False, wed)
        r.append(check("a Wednesday plan exists", spec_wed_on is not None, True))
        r.append(check("  ...and is identical with or without the rule", spec_wed_on, spec_wed_off))
        r.append(check("  ...and says nothing about the halt", "maintenance" in log_wed, False))
    finally:
        bb.load_starts = real_load

    # ── hour_value: halt minutes are not 'warm' ───────────────────────────────────
    with tempfile.TemporaryDirectory() as d:
        lo = datetime(2026, 11, 19, 6, 0)
        lines = ["Timestamp,Ticker\n"] + [
            (lo + timedelta(minutes=m)).replace(tzinfo=None).isoformat(timespec="seconds") + "Z,KXITFMATCH-X\n"
            for m in range(0, 300)]                               # telemetry every minute 06:00-10:59 UTC
        with open(os.path.join(d, "EvTelemetry_20261119.csv"), "w", encoding="utf-8") as fh:
            fh.writelines(lines)
        warm = hv.warm_buckets(d, "2026-11-19", "2026-11-19")
        to_local = lambda t_utc: t_utc.replace(tzinfo=UTC).astimezone().replace(tzinfo=None)
        halt = kh.windows(to_local(lo), to_local(lo + timedelta(hours=5)))
        r.append(check("a halt is in range of the fixture", len(halt), 1))
        a, b = halt[0]
        r.append(check("no warm bucket inside the halt", any(a <= x < b for x in warm), False))
        r.append(check("the bucket just before it is warm", (a - timedelta(minutes=5)) in warm, True))
        r.append(check("the reopen bucket is warm at once (the bot never stopped)", b in warm, True))

    n = sum(r)
    print(f"\n{n}/{len(r)} passed")
    return 0 if n == len(r) else 1


if __name__ == "__main__":
    sys.exit(main())
