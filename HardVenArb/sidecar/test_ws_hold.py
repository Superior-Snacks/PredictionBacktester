"""The odds socket waits for the captured login to validate (2026-10-23).

At a window open the saved profile replays its last x-session, which after a dark gap is usually dead. Authed
REST already waited for the validation probe; the socket did not, and dialled the dead login four times (rc=5)
at the 14:49 open. Nothing reset the proof at a window open either, so a later window inherited the previous
window's. Offline: no venue, no network.

    python test_ws_hold.py              # exit 0 = all pass
"""
from __future__ import annotations
import asyncio
import io
import os
import sys
from contextlib import redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("PINNACLE_ODDS_MODE", "ws")
from pinnacle_adapter import PinnacleAdapter  # noqa: E402

PASS = FAIL = 0


def check(name, ok, got=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}" + (f"  (got {got})" if got != "" else ""))


def make(source="browser", proven=False):
    a = PinnacleAdapter()
    a._mode = "ws"
    a._session_source = source
    a._session_ready = True
    a._session_proven = proven
    starts = []

    async def seed(lid):
        pass

    a._refresh_league = seed
    a._reseed_league = seed

    def start():
        starts.append(1)
        a._ws_started = True              # what the real _start_ws does first

    a._start_ws = start
    return a, starts


def odds(a, n=1):
    buf = io.StringIO()
    with redirect_stdout(buf):
        for _ in range(n):
            asyncio.run(a.odds(["1000:1:home", "1000:1:away"]))
    return buf.getvalue()


print("browser session, captured but not yet validated")
a, starts = make()
out = odds(a, 3)
check("the socket is NOT dialled", starts == [], starts)
check("the hold is logged once, not per /odds", out.count("socket HELD") == 1, out)

print("the validation probe succeeds")
a._session_proven = True
out = odds(a, 3)
check("the next /odds dials it, once", starts == [1], starts)
check("...without logging the hold again", "socket HELD" not in out, out)

print("a non-browser session (creds from env) is not held")
b, starts_b = make(source="env")
odds(b)
check("dialled on the first /odds", starts_b == [1], starts_b)

print("a new window resets the proof")
a._proven_evt.set()
a._on_session_opening()
check("_session_proven is cleared", a._session_proven is False)
check("...and the event other waiters use", not a._proven_evt.is_set())
check("...and the socket is down for the new window", a._ws_started is False)
out = odds(a, 2)
check("the new window's capture is held again", starts == [1], starts)
check("...and the hold is logged again for this window", out.count("socket HELD") == 1, out)
a._session_proven = True
odds(a)
check("the new window dials once its probe succeeds", starts == [1, 1], starts)

print("a capture that is not ready serves the cache and dials nothing (unchanged)")
c, starts_c = make(proven=True)
c._session_ready = False
odds(c)
check("not dialled", starts_c == [], starts_c)

print(f"\n{'=' * 58}\n  {PASS} passed, {FAIL} failed\n{'=' * 58}")
sys.exit(1 if FAIL else 0)
