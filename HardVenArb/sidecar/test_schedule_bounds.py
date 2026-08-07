"""
test_schedule_bounds.py — the HARD bounds on scheduled uptime: guaranteed downtime + a daily hour ceiling.

Motivation (2026-08-07): nothing in the scheduler limited a window's LENGTH or the day's TOTAL. Windows are
shaped around games, and `merge_windows` unions on ANY overlap, so adjacent sessions plus an operator pin chain
into one span — a live plan showed a single 12:37->19:45 window (7h08m). A dense enough slate can therefore
plan the bot an all-day session with no browser downtime at all.

    python test_schedule_bounds.py            # 14/14 expected
"""
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import schedule as sched

B = datetime(2026, 8, 7, 6, 0)          # UTC base; this box runs UTC so local == UTC


def w(oh, om, ch, cm, games=1, day=7):
    return (datetime(2026, 8, day, oh, om), datetime(2026, 8, day, ch, cm), games)


def hours(ws):
    return round(sum((c - o for o, c, _ in ws), timedelta()).total_seconds() / 3600, 2)


def check(name, got, want):
    ok = got == want
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}: {got} (expected {want})")
    return ok


def main() -> int:
    r = []

    # ── enforce_downtime ──────────────────────────────────────────────────────
    back_to_back = [w(8, 0, 12, 0), w(12, 0, 16, 0)]          # zero gap
    out = sched.enforce_downtime(back_to_back, 45)
    r.append(check("back-to-back gets a 45m gap", (out[1][0] - out[0][1]).total_seconds() / 60, 45.0))
    r.append(check("  ...by trimming the EARLIER close", out[0][1].strftime("%H:%M"), "11:15"))
    r.append(check("  ...later open untouched (lead protected)", out[1][0].strftime("%H:%M"), "12:00"))

    already = [w(8, 0, 10, 0), w(14, 0, 16, 0)]                # 4h gap already
    r.append(check("ample gap is left alone", sched.enforce_downtime(already, 45), already))

    tiny = [w(8, 0, 8, 40), w(9, 0, 12, 0)]                    # trimming would leave 20m... exactly the floor
    out = sched.enforce_downtime(tiny, 45, min_window_min=30)
    r.append(check("window trimmed below floor is dropped", len(out), 1))
    r.append(check("  ...and it's the later one that survives", out[0][0].strftime("%H:%M"), "09:00"))

    r.append(check("disabled (0) is a no-op", sched.enforce_downtime(back_to_back, 0), back_to_back))
    r.append(check("single window unaffected", sched.enforce_downtime([w(8, 0, 20, 0)], 45),
                   [w(8, 0, 20, 0)]))

    # ── cap_daily_hours (T0 = midnight, so every test window is entirely future) ──
    T0 = datetime(2026, 8, 7, 0, 0)
    dense = [w(6, 0, 10, 0, games=2), w(11, 0, 15, 0, games=9), w(16, 0, 20, 0, games=4)]   # 12h total
    out = sched.cap_daily_hours(dense, 8, now=T0)
    r.append(check("12h of blocks capped to 8h", hours(out), 8.0))
    kept = [(o.strftime("%H:%M"), c.strftime("%H:%M")) for o, c, _ in out]
    # densest (9 games) first, then 4 games, then the 2-game block gets the 0h remainder -> dropped
    r.append(check("  ...densest blocks kept", kept, [("11:00", "15:00"), ("16:00", "20:00")]))

    part = [w(11, 0, 15, 0, games=9), w(16, 0, 20, 0, games=4)]
    out = sched.cap_daily_hours(part, 6, now=T0)                # 4h + 2h of the second
    r.append(check("partial fit is trimmed, not dropped", hours(out), 6.0))
    r.append(check("  ...trimmed from the front", out[1][1].strftime("%H:%M"), "18:00"))

    pin = w(6, 0, 8, 0, games=0)
    out = sched.cap_daily_hours([pin, w(11, 0, 21, 0, games=9)], 6, protected=[pin], now=T0)
    r.append(check("pin survives the cap despite 0 games", pin in out, True))
    r.append(check("  ...and still consumes budget (2h pin + 4h left)", hours(out), 6.0))

    # ── the real 2026-08-07 case: one 7h08m block, no downtime all day ────────
    real = [(datetime(2026, 8, 7, 12, 37), datetime(2026, 8, 7, 19, 45), 53)]
    bounded = sched.cap_daily_hours(sched.enforce_downtime(real, 45), 5, now=T0)
    r.append(check("the live 7h08m block is capped to 5h", hours(bounded), 5.0))

    # ── THE DAILY-vs-PER-PLAN BUG: an hourly rebuild must not refill the budget ──
    # Morning: 5h cap, one big block -> trimmed to 5h.
    day = [w(6, 0, 20, 0, games=9)]
    morning = sched.cap_daily_hours(day, 5, now=T0)
    r.append(check("morning plan trimmed to the 5h cap", hours(morning), 5.0))
    # Noon rebuild after 4h already burned: only 1h may remain, NOT another full 5h.
    spent = {datetime(2026, 8, 7).date(): timedelta(hours=4)}
    noon = sched.cap_daily_hours([w(10, 0, 20, 0, games=9)], 5,
                                 spent=spent, now=datetime(2026, 8, 7, 10, 0))
    r.append(check("rebuild honours hours already burned", hours(noon), 1.0))
    # Budget fully spent -> nothing more today.
    spent5 = {datetime(2026, 8, 7).date(): timedelta(hours=5)}
    r.append(check("exhausted budget plans nothing", sched.cap_daily_hours(
        [w(10, 0, 20, 0, games=9)], 5, spent=spent5, now=datetime(2026, 8, 7, 10, 0)), []))
    # An in-progress window is charged only its REMAINING time (no double-count with `spent`).
    live = [(datetime(2026, 8, 7, 8, 0), datetime(2026, 8, 7, 20, 0), 9)]
    out = sched.cap_daily_hours(live, 6, spent={datetime(2026, 8, 7).date(): timedelta(hours=2)},
                                now=datetime(2026, 8, 7, 10, 0))
    r.append(check("live window charged only its remaining time", out[0][1].strftime("%H:%M"), "14:00"))
    # A fully-past window is history: kept, charged nothing.
    past = [w(1, 0, 3, 0, games=2), w(12, 0, 20, 0, games=9)]
    out = sched.cap_daily_hours(past, 4, now=datetime(2026, 8, 7, 10, 0))
    r.append(check("past window kept and not charged", (out[0][0].hour, out[1][1].strftime("%H:%M")),
                   (1, "16:00")))

    # ── WIRING: the lifecycle must actually MEASURE uptime, not just accept a `spent` dict ────────
    import asyncio
    from lifecycle import PinnacleLifecycle

    class FakeBrowser:
        def __init__(self): self.started = 0
        async def start(self): self.started += 1
        async def stop(self): pass

    lc = PinnacleLifecycle(FakeBrowser(), [33], max_daily_hours=5, min_downtime_min=45)
    lc._windows = [w(8, 0, 12, 0, games=3)]
    asyncio.run(lc.tick(now=datetime(2026, 8, 7, 9, 0)))       # inside -> open
    r.append(check("lifecycle opens inside a window", lc._open, True))
    snap = lc._spent_snapshot(datetime(2026, 8, 7, 10, 0))
    r.append(check("  live session counts toward today while OPEN",
                   snap[datetime(2026, 8, 7).date()], timedelta(hours=1)))
    asyncio.run(lc.tick(now=datetime(2026, 8, 7, 12, 30)))     # outside -> close, bank 3h30m
    r.append(check("lifecycle closes and banks the session", lc._open, False))
    r.append(check("  banked uptime is the real open->close span",
                   lc._spent_by_day[datetime(2026, 8, 7).date()], timedelta(hours=3, minutes=30)))

    # ── fill_daily_hours: the cap as a TARGET, not just a ceiling ─────────────
    thin = [w(12, 0, 14, 0, games=3)]                       # 2h planned, 8h budget
    out = sched.fill_daily_hours(thin, 8, now=T0)
    r.append(check("thin plan grows toward the cap", hours(out), 8.0))
    r.append(check("  ...earlier open preferred (pre-live lead)", out[0][0] < thin[0][0], True))

    r.append(check("already at the cap -> unchanged",
                   sched.fill_daily_hours([w(6, 0, 14, 0, games=3)], 8, now=T0),
                   [w(6, 0, 14, 0, games=3)]))
    r.append(check("over the cap -> fill does nothing",
                   sched.fill_daily_hours([w(6, 0, 20, 0, games=3)], 8, now=T0),
                   [w(6, 0, 20, 0, games=3)]))

    two = [w(8, 0, 10, 0, games=3), w(14, 0, 16, 0, games=3)]
    out = sched.fill_daily_hours(two, 24, now=T0, min_downtime_min=60)
    gap = (out[1][0] - out[0][1]).total_seconds() / 60
    r.append(check("filling never eats the required downtime", gap >= 60, True))
    r.append(check("  ...and stays inside the local day",
                   out[0][0] >= datetime(2026, 8, 7) and out[-1][1] <= datetime(2026, 8, 8), True))

    burned = {datetime(2026, 8, 7).date(): timedelta(hours=6)}
    out = sched.fill_daily_hours(thin, 8, spent=burned, now=T0)
    r.append(check("fill respects hours already burned", hours(out), 2.0))

    # A FUTURE day holds only pins (its games aren't fetched under today_only) — inflating it against a slate
    # we can't see turned tomorrow's 2h morning pin into a 10h midnight block on the live bot (2026-08-07).
    tomorrow_pin = [w(6, 0, 8, 0, games=0, day=8)]
    r.append(check("future day is NOT filled (games unknown yet)",
                   sched.fill_daily_hours(tomorrow_pin, 10, now=T0), tomorrow_pin))
    mixed = sched.fill_daily_hours(thin + tomorrow_pin, 8, now=T0)
    r.append(check("  ...while today still fills", (hours([mixed[0]]), hours([mixed[1]])), (8.0, 2.0)))

    # ── banking window ────────────────────────────────────────────────────────
    lc2 = PinnacleLifecycle(FakeBrowser(), [33])
    asyncio.run(lc2.halt("low balance: pinnacle wallet 0.00 < floor 5"))
    r.append(check("halt closes the site", (lc2._override, lc2._open), ("halted", False)))
    asyncio.run(lc2.banking(30))
    r.append(check("banking OPENS the site through a halt", (lc2.state, lc2._open), ("banking", True)))

    # the halt underneath must survive: banking is deliberately not persisted
    class FakeControl:
        def __init__(self): self.saved = []
        def set_override(self, mode, reason, until): self.saved.append(mode)
    lc3 = PinnacleLifecycle(FakeBrowser(), [33])
    lc3._control = FakeControl()
    asyncio.run(lc3.halt("empty"))
    asyncio.run(lc3.banking(30))
    r.append(check("banking is NOT persisted (halt survives a restart)", lc3._control.saved, ["halted"]))
    r.append(check("  ...and a persisted banking can't be restored",
                   (lc3.restore_override("banking", "x", None), lc3._override)[1], "banking"))

    # expiry reverts to the halt, it does not clear it
    lc3._override_until = datetime(2026, 8, 7, 0, 0)
    asyncio.run(lc3.tick(now=datetime(2026, 8, 7, 1, 0)))
    r.append(check("banking expires back INTO the halt", lc3._override, "halted"))

    n = sum(r)
    print(f"\n{n}/{len(r)} passed")
    return 0 if n == len(r) else 1


if __name__ == "__main__":
    sys.exit(main())
