"""
test_overnight_blocks.py — plans that run past midnight and value-weighted hours (2026-11-09): the evening pays
~3x the morning per live match, so the scheduler can now plan a service day 06:00 -> 02:00 and weight each live
minute by its clock hour (hour_value.json). The defaults stay exactly as they were.

    python test_overnight_blocks.py            # all PASS expected
"""
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import build_blocks as bb
import block_scheduler as bsch
import schedule as sched


def check(name, got, want):
    ok = got == want
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}: {got} (expected {want})")
    return ok


def utc(local_naive):
    """A naive LOCAL time as the naive-UTC the board hands out."""
    return local_naive.astimezone(timezone.utc).replace(tzinfo=None)


class FakeLifecycle:
    def __init__(self, pins):
        self.pins = pins

    def status(self):
        return {"pins": list(self.pins)}


def scheduler(pins, latest="02:00", budget=16.0, weights=None):
    return bsch.BlockScheduler(FakeLifecycle(pins), [33], "unused.json", hours="06,12,18", budget_h=budget,
                               min_gap=50, jitter=7, earliest="06:00", latest=latest, objective="inplay",
                               weights_path=weights)


def main() -> int:
    r = []
    curve = (5, [1.0] * 18)                                     # live for 90 minutes from the listed start

    # ── a block prints as clock time, also past midnight ────────────────────────
    r.append(check("a block past midnight prints as clock time", str(bb.Block(1245, 1545)), "20:45-01:45"))
    r.append(check("a same-day block prints as before", str(bb.Block(405, 705)), "06:45-11:45"))

    # ── the default path is untouched ────────────────────────────────────────────
    day = [(datetime(2026, 11, 10, 10),)] * 12 + [(datetime(2026, 11, 10, 18),)] * 4
    old = bb.optimise(day, 300, 50, 7, 360, 1380, 0, objective="inplay", curve=curve)
    same = bb.optimise(day, 300, 50, 7, 360, 1380, 0, objective="inplay", curve=curve, times=[600] * 12 + [1080] * 4)
    r.append(check("passing the same times explicitly changes nothing", same, old))
    r.append(check("weights=None is the unweighted optimiser",
                   bb.optimise(day, 300, 50, 7, 360, 1380, 0, objective="inplay", curve=curve, weights=None), old))

    # ── a plan can run past midnight ─────────────────────────────────────────────
    late = [1380] * 10                                          # 10 matches listed 23:00
    blocks = bb.optimise([], 300, 50, 7, 360, 1560, 0, objective="inplay", curve=curve, times=late)
    r.append(check("an overnight window is found for 23:00 matches", len(blocks) >= 1, True))
    r.append(check("  ...and it closes after midnight", any(b.close_min > 1440 for b in blocks), True))
    r.append(check("  ...never past LATEST (02:00 = 1560)", all(b.close_min <= 1560 for b in blocks), True))
    r.append(check("  ...still on the :15/:45 grid", all(b.open_min % 60 in (15, 45) for b in blocks), True))
    capped = bb.optimise([], 300, 50, 7, 360, 1380, 0, objective="inplay", curve=curve, times=late)
    r.append(check("the same matches with LATEST 23:00 get nothing (they start at 23:00)", capped, []))

    # ── weights move the budget to the hour that pays ────────────────────────────
    two = [540] * 12 + [1260] * 10                             # 12 matches at 09:00, 10 at 21:00
    flat = bb.optimise([], 120, 50, 7, 360, 1560, 0, objective="inplay", curve=curve, times=two)
    w = [1.0] * 24
    w[21] = w[22] = 3.0
    rich = bb.optimise([], 120, 50, 7, 360, 1560, 0, objective="inplay", curve=curve, times=two, weights=w)
    r.append(check("unweighted, a 2h budget goes to the bigger 09:00 cluster",
                   all(b.open_min < 720 for b in flat) and bool(flat), True))
    r.append(check("weighted 3x at 21-23h, the same budget goes to the 21:00 cluster",
                   all(b.open_min >= 1200 for b in rich) and bool(rich), True))
    ones = [1.0] * 24
    a = bb.expected_live(two, [bb.Block(525, 645), bb.Block(1245, 1365)], curve, 7)
    b = bb.expected_live(two, [bb.Block(525, 645), bb.Block(1245, 1365)], curve, 7, weights=ones)
    r.append(check("weights of 1.0 give exactly the unweighted live minutes", round(a, 6), round(b, 6)))
    r.append(check("weighted expected_live counts the 21h cluster at 3x",
                   round(bb.expected_live([1260], [bb.Block(1260, 1350)], curve, 0, weights=w)), 270))

    # ── load_weights ─────────────────────────────────────────────────────────────
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "w.json")
        with open(p, "w", encoding="utf-8") as fh:
            json.dump({"weights": [1.0] * 23 + [2.5]}, fh)
        r.append(check("load_weights reads 24 hours", bb.load_weights(p), [1.0] * 23 + [2.5]))
        r.append(check("load_weights on a missing file -> None", bb.load_weights(os.path.join(d, "x.json")), None))
        for bad, label in (([1.0] * 23, "23 hours"), ([1.0] * 23 + [-1.0], "a negative hour"),
                           ([0.0] * 24, "all zero")):
            with open(p, "w", encoding="utf-8") as fh:
                json.dump({"weights": bad}, fh)
            r.append(check(f"load_weights rejects {label}", bb.load_weights(p), None))
        with open(p, "w", encoding="utf-8") as fh:
            fh.write("{broken")
        r.append(check("load_weights on a broken file -> None", bb.load_weights(p), None))

    # ── the service day ──────────────────────────────────────────────────────────
    s = scheduler([])
    md = lambda t: (s.service_day(t)[0].date().isoformat(), s.service_day(t)[1])
    r.append(check("00:30 belongs to YESTERDAY's plan, minute 1470",
                   md(datetime(2026, 11, 11, 0, 30)), ("2026-11-10", 1470)))
    r.append(check("03:00 (after LATEST) starts today's plan", md(datetime(2026, 11, 11, 3, 0)), ("2026-11-11", 180)))
    r.append(check("02:00 exactly is past LATEST -> today", md(datetime(2026, 11, 11, 2, 0)), ("2026-11-11", 120)))
    r.append(check("06:00 is today", md(datetime(2026, 11, 11, 6, 0)), ("2026-11-11", 360)))
    r.append(check("the service day ends at minute 1560", s.service_day(datetime(2026, 11, 11, 12))[3], 1560))
    plain = scheduler([], latest="23:00")
    r.append(check("without an overnight LATEST, 00:30 is just today",
                   (plain.service_day(datetime(2026, 11, 11, 0, 30))[0].day, plain.service_day(datetime(2026, 11, 11, 0, 30))[1]),
                   (11, 30)))
    r.append(check("pin 20:45-01:45 -> (1245, 1545)", s.pin_minutes("20:45-01:45"), (1245, 1545)))
    r.append(check("pin 00:15-01:45 (after midnight) -> (1455, 1545)", s.pin_minutes("00:15-01:45"), (1455, 1545)))
    r.append(check("pin 06:45-11:45 -> (405, 705)", s.pin_minutes("06:45-11:45"), (405, 705)))
    r.append(check("same-day config: 06:45-11:45 unchanged", plain.pin_minutes("06:45-11:45"), (405, 705)))

    # ── a re-plan after midnight keeps the running overnight block ───────────────
    real_load = bb.load_starts
    try:
        base = datetime(2026, 11, 10)
        board = [sched.Game(utc(base + timedelta(hours=h)), "Tennis", str(i))
                 for i, h in enumerate([9, 9, 13, 13, 19, 20, 21, 22, 23, 33, 33, 37, 37, 43, 45, 47])]
        bb.load_starts = lambda *a, **k: [g for g in board
                                          if k["window"][0] <= bb.local_naive(g[0]) < k["window"][1]]
        running = ["06:45-11:45", "12:45-17:45", "20:45-01:45"]
        night = scheduler(running)
        spec = night._author("test", now=datetime(2026, 11, 11, 0, 30))
        r.append(check("at 00:30 the running 20:45-01:45 block is frozen and kept",
                       spec is not None and "20:45-01:45" in spec.split(","), True))
        r.append(check("  ...and so are the day's earlier blocks", spec is not None and "06:45-11:45" in spec.split(","), True))
        fresh = scheduler(running)
        spec6 = fresh._author("test", now=datetime(2026, 11, 11, 6, 0))
        r.append(check("at 06:00 nothing from yesterday is frozen: today's plan gets the whole 16h budget",
                       spec6 is not None and sum(c - o for o, c in map(fresh.pin_minutes, spec6.split(","))) > 10 * 60,
                       True))
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "hv.json")
            wv = [1.0] * 24
            wv[20] = wv[21] = wv[22] = wv[23] = 3.0
            with open(p, "w", encoding="utf-8") as fh:
                json.dump({"weights": wv}, fh)
            weighted = scheduler(["06:45-11:45", "12:45-17:45"], weights=p)
            spec_w = weighted._author("test", now=datetime(2026, 11, 10, 18, 0))
            r.append(check("a weights file is read and the plan still comes back", spec_w is not None, True))
            gone = scheduler(["06:45-11:45"], weights=os.path.join(d, "missing.json"))
            r.append(check("a missing weights file falls back to unweighted, not to nothing",
                           gone._author("test", now=datetime(2026, 11, 10, 18, 0)) is not None, True))
        eve = scheduler(["06:45-11:45", "12:45-17:45"])
        spec18 = eve._author("test", now=datetime(2026, 11, 10, 18, 0))
        r.append(check("at 18:00 the plan can reach past midnight", spec18 is not None and any(
            eve.pin_minutes(x)[1] > 1440 for x in spec18.split(",")), True))
        r.append(check("  ...within budget (16h incl. the frozen 10h)",
                       sum(c - o for o, c in map(eve.pin_minutes, spec18.split(","))) <= 16 * 60, True))
    finally:
        bb.load_starts = real_load

    # ── the lifecycle keeps a pin that opened yesterday ─────────────────────────
    loc = lambda y, m, d, h, mi: datetime(y, m, d, h, mi).astimezone(timezone.utc).replace(tzinfo=None)
    now = loc(2026, 11, 11, 0, 30)
    wins = sched.pinned_windows(sched.parse_pin_hours("20:45-01:45"), now=now)
    r.append(check("pinned_windows at 00:30 still holds the 20:45-01:45 that opened yesterday",
                   any(o <= now < c for o, c, _ in wins), True))
    day_wins = sched.pinned_windows(sched.parse_pin_hours("06:45-11:45"), now=now)
    r.append(check("a daytime pin at 00:30: only today's and tomorrow's (yesterday's is past)",
                   [o for o, c, _ in day_wins], [loc(2026, 11, 11, 6, 45), loc(2026, 11, 12, 6, 45)]))

    n = sum(r)
    print(f"\n{n}/{len(r)} passed")
    return 0 if n == len(r) else 1


if __name__ == "__main__":
    sys.exit(main())
