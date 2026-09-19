"""Offline test of the WS silence watchdog after the 2026-09-19 fixes. No venue, no network.

What is being checked, and why each matters:

  1. A fresh socket does not inherit the previous window's silence. Before, the first judgement after a dark
     window read "SILENT 12865s" and forced a reconnect mid-subscribe-pass.
  2. A subscribe pass draining exactly on schedule is not declared "not draining". Before, the hold bound was
     recomputed from the REMAINING backlog and shrank under the pass.
  3. A genuinely dead feed escalates drop -> drop -> reconnect twice and then BACKS OFF: socket down, loops
     stopped, quotes no longer stamped fresh, one retry scheduled. Before, that loop ran forever - 5,275 league
     re-subscribes and 38 reconnects in one window.
  4. A frame clears every bit of silence state.
  5. The retry restarts the feed only if the session is up; a window open resets the backoff.
  6. Leagues the bot stopped asking about fall out of the active set.
  7. SUBACK refusals are counted.

Fake time: `time.time` is replaced by a clock the fake `asyncio.sleep` advances, and the reconciler's work
(one league per PINNACLE_SUBSCRIBE_GAP_SEC) is done by the fake sleep on that same clock, so the pass drains
exactly on schedule - the case the old bound got wrong.
"""
from __future__ import annotations
import asyncio
import math
import os
import sys
import time

os.environ.setdefault("PINNACLE_ODDS_MODE", "ws")
os.environ.setdefault("PINNACLE_DEDICATED_WS", "1")

import pinnacle_adapter  # noqa: E402
from pinnacle_adapter import PinnacleAdapter  # noqa: E402

REAL_SLEEP = asyncio.sleep
REAL_TIME = time.time


class Clock:
    def __init__(self, t0: float = 1_000_000.0):
        self.now = t0
        self.on_advance = None

    def time(self) -> float:
        return self.now

    async def sleep(self, d: float):
        self.now += d
        if self.on_advance:
            self.on_advance()
        await REAL_SLEEP(0)


class FakeClient:
    def __init__(self, adapter: PinnacleAdapter):
        self.a = adapter
        self.subs = 0
        self.reconnects = 0
        self.disconnects = 0
        self.loop_stops = 0
        self._mid = 0

    def subscribe(self, topic, qos):
        self.subs += 1
        self._mid += 1
        return 0, self._mid

    def reconnect(self):
        self.reconnects += 1
        self.a._on_connect(self, None, None, 0)      # paho would call this once the socket is back

    def disconnect(self):
        self.disconnects += 1

    def loop_stop(self):
        self.loop_stops += 1


class FakeMsg:
    def __init__(self, topic="matchups/reg/lg/1/live/ld", payload=b"{}"):
        self.topic = topic
        self.payload = payload


def check(name, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}{('  ' + detail) if detail else ''}")
    return bool(ok)


def make_adapter(clock: Clock, n_leagues: int) -> tuple[PinnacleAdapter, FakeClient]:
    a = PinnacleAdapter()
    a._session_source = "browser"
    a._session_ready = True
    a._session_expired = False
    c = FakeClient(a)
    a._client = c
    for i in range(n_leagues):
        a._active_leagues[str(1000 + i)] = clock.now
    return a, c


def on_schedule_reconciler(a: PinnacleAdapter, clock: Clock, start_holder: dict):
    """Subscribe as many leagues as the real reconciler would have by now: one per gap since the pass began."""
    def _tick():
        for l in list(a._active_leagues):          # the C# bot asks about every paired league each 250ms
            a._active_leagues[l] = clock.now
        if not a._connected or a._ws_gave_up:
            return
        pending = [l for l in a._active_leagues if l not in a._subscribed]
        if not pending:
            start_holder.pop("t0", None)
            return
        t0 = start_holder.setdefault("t0", clock.now)
        due = int(math.floor((clock.now - t0) / a._subscribe_gap_sec))
        done = len(a._subscribed) - start_holder.setdefault("base", len(a._subscribed))
        while pending and done < due:
            a._subscribe_league(pending.pop(0))
            done += 1
    return _tick


async def run_watchdog_for(a: PinnacleAdapter, clock: Clock, seconds: float):
    t_end = clock.now + seconds
    task = asyncio.create_task(a._ws_watchdog())
    while clock.now < t_end and not task.done():
        await REAL_SLEEP(0)
    if not task.done():
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass


async def main() -> int:
    results = []
    clock = Clock()
    time.time = clock.time
    asyncio.sleep = clock.sleep
    pinnacle_adapter.time.time = clock.time
    pinnacle_adapter.asyncio.sleep = clock.sleep

    # ── 1 + 2: fresh socket after a dark window, pass drains on schedule, feed dead ─────────────────
    a, c = make_adapter(clock, 94)
    a._ws_last_msg_ts = clock.now - 12865          # the previous window's last frame, as on 2026-09-18 21:49
    a._sub_pass_done_ts = clock.now - 12900        # ...and its last completed pass
    holder = {}
    clock.on_advance = on_schedule_reconciler(a, clock, holder)
    a._on_connect(c, None, None, 0)                # socket up -> baseline reset
    pass_len = 94 * a._subscribe_gap_sec           # 282s
    await run_watchdog_for(a, clock, pass_len + 20)
    results.append(check("no reconnect during an on-schedule pass (old code: at 235s)",
                         c.reconnects == 0 and len(a._subscribed) == 94,
                         f"reconnects={c.reconnects} subscribed={len(a._subscribed)}"))

    # ── 3: keep it dead -> two cycles -> backoff ───────────────────────────────────────────────────
    subs_before = c.subs
    resumed = {}

    async def _record_resume(delay):               # under fake time a 900s sleep returns at once; hold the retry
        resumed["delay"] = delay
    a._silence_resume = _record_resume
    await run_watchdog_for(a, clock, 3600)
    await REAL_SLEEP(0.3)                          # the hard-stop runs on a helper thread
    results.append(check("dead feed gives up after 2 reconnect cycles",
                         a._ws_gave_up and not a._connected and c.reconnects == 1,
                         f"gave_up={a._ws_gave_up} connected={a._connected} reconnects={c.reconnects}"))
    results.append(check("socket actually stood down (disconnect + loop_stop)",
                         c.disconnects >= 1 and c.loop_stops >= 1))
    results.append(check("backoff armed at 15 min, retry scheduled",
                         a._silence_backoff_sec == 900 and resumed.get("delay") == 900))
    results.append(check("quotes no longer stamped fresh while suspect", a._feed_live() is False))
    results.append(check("subscribe traffic bounded in the hour (old code: ~1,200 league subs)",
                         (c.subs - subs_before) / 4 < 400, f"league subs this hour={(c.subs - subs_before) // 4}"))

    # ── 4: a frame clears the silence state ─────────────────────────────────────────────────────────
    a._connected = True                            # as a resumed socket would be
    a._on_message(c, None, FakeMsg())
    results.append(check("a frame clears suspect/cycles/backoff",
                         not a._silence_suspect and a._silence_cycles == 0 and a._silence_backoff_sec == 0.0))
    results.append(check("...and quotes stamp fresh again", a._feed_live() is True))

    # ── 5: the retry and the window-open reset ──────────────────────────────────────────────────────
    a2, c2 = make_adapter(clock, 5)
    a2._ws_gave_up = True
    a2._ws_started = True
    await a2._silence_resume(1)
    results.append(check("retry re-arms the feed when the session is up",
                         a2._ws_gave_up is False and a2._ws_started is False))
    a3, c3 = make_adapter(clock, 5)
    a3._ws_gave_up = True
    a3._ws_started = True
    a3._session_ready = False
    await a3._silence_resume(1)
    results.append(check("retry does NOTHING while dark (no session)", a3._ws_gave_up is True))
    a3._silence_backoff_sec = 1800
    a3._silence_cycles = 1
    a3._silence_suspect = True
    a3._on_session_opening()
    results.append(check("window open resets the backoff",
                         a3._silence_backoff_sec == 0.0 and a3._silence_cycles == 0 and not a3._silence_suspect))

    # ── 6: active-league pruning ─────────────────────────────────────────────────────────────────────
    a4, _ = make_adapter(clock, 3)
    a4._active_leagues["old-1"] = clock.now - 601
    a4._active_leagues["old-2"] = clock.now - 90000
    kept = a4._active_league_ids()
    results.append(check("leagues not requested for >TTL are pruned",
                         set(kept) == {"1000", "1001", "1002"} and "old-1" not in a4._active_leagues))

    # ── 7: SUBACK accounting ─────────────────────────────────────────────────────────────────────────
    a5, c5 = make_adapter(clock, 1)
    a5._connected = True
    a5._subscribe_league("1000")                   # 4 topics -> 4 mids
    mids = list(a5._sub_mids.keys())
    a5._on_subscribe(c5, None, mids[0], (0,))
    a5._on_subscribe(c5, None, mids[1], (128,))
    a5._on_subscribe(c5, None, mids[2], [0])
    results.append(check("SUBACK granted/refused counted",
                         a5._suback_ok == 2 and a5._suback_refused == 1 and len(a5._sub_mids) == 1,
                         f"ok={a5._suback_ok} refused={a5._suback_refused} unanswered={len(a5._sub_mids)}"))

    # ── 8: a healthy feed never trips the watchdog ───────────────────────────────────────────────────
    a6, c6 = make_adapter(clock, 20)
    holder6 = {}
    recon6 = on_schedule_reconciler(a6, clock, holder6)

    last = {"t": clock.now}

    def healthy_tick():
        recon6()
        if a6._connected and clock.now - last["t"] >= 30:
            last["t"] = clock.now
            a6._on_message(c6, None, FakeMsg())    # a frame every ~30s
    clock.on_advance = healthy_tick
    a6._on_connect(c6, None, None, 0)
    await run_watchdog_for(a6, clock, 1800)
    results.append(check("healthy feed: no drops, no reconnects, not suspect",
                         c6.reconnects == 0 and not a6._silence_suspect and not a6._ws_gave_up
                         and len(a6._subscribed) == 20))

    time.time = REAL_TIME
    asyncio.sleep = REAL_SLEEP
    n_ok = sum(results)
    print(f"\n{n_ok}/{len(results)} passed.")
    return 0 if n_ok == len(results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
