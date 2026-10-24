"""A socket whose subscriptions are all REFUSED is rebuilt at once (2026-10-24).

07:28: an rc=7 drop right after a re-mint reload; paho's same-client reconnect was accepted and then every
SUBSCRIBE refused (SUBACK 128) - 456 refusals over the silence watchdog's 180/360/475s ladder, ten minutes with
no prices, until its forced reconnect built a new client that was served at once. Now two leagues refused in a
row (nothing granted between) rebuild the socket immediately, and the subscribe pass pauses on it. Offline: a
fake paho client, no venue.

    python test_ws_refused.py           # exit 0 = all pass
"""
from __future__ import annotations
import asyncio
import io
import os
import sys
import threading
import time
from contextlib import redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("PINNACLE_ODDS_MODE", "ws")
os.environ.setdefault("PINNACLE_DEDICATED_WS", "1")
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


class FakeClient:
    def __init__(self, a):
        self.a = a
        self.topics = []
        self._mid = 0

    def subscribe(self, topic, qos):
        self._mid += 1
        self.topics.append(topic)
        return 0, self._mid

    def reconnect(self):
        self.a._on_connect(self, None, None, 0)       # paho calls this once the same client is back

    def disconnect(self):
        pass

    def loop_stop(self):
        pass


def make(n_leagues=6):
    a = PinnacleAdapter()
    a._session_source = "browser"
    a._session_ready = True
    made = []

    def new_client(mqtt=None):
        c = FakeClient(a)
        made.append(c)
        return c

    a._make_client = new_client
    a._connect_client = lambda cl: a._on_connect(cl, None, None, 0)
    a._client = new_client()
    a._on_connect(a._client, None, None, 0)
    for i in range(n_leagues):
        a._active_leagues[str(1000 + i)] = time.time()
    return a, made


def answer(a, refused):
    """The broker answers every outstanding SUBSCRIBE - until a rebuild detaches the old socket's callbacks."""
    cl = a._client
    for mid in list(a._sub_mids):
        if a._client is not cl:
            break
        a._on_subscribe(cl, None, mid, [128] if refused else [1])


def league(a, lid, refused):
    a._subscribe_league(lid)
    answer(a, refused)


print("the 2026-10-24 07:28 case: a reconnected socket refuses everything")
a, made = make()
buf = io.StringIO()
with redirect_stdout(buf):
    league(a, "1000", refused=True)
    one = (len(made), a._socket_refusing())
    league(a, "1001", refused=True)
check("one refused league is not enough", one == (1, False), one)
check("the second refused league rebuilds the socket", len(made) == 2 and a._ws_rebuilds == 1, (len(made), a._ws_rebuilds))
check("...after 8 refused SUBSCRIBEs, not 456", len(made[0].topics) == 8, len(made[0].topics))
check("the new socket starts with a clean streak", not a._socket_refusing() and not a._refusal_rebuild_asked)
check("the reason is printed once", buf.getvalue().count("in a row REFUSED") == 1, buf.getvalue())
with redirect_stdout(io.StringIO()):
    league(a, "1000", refused=False)
check("the new client is served", a._suback_ok > 0 and a._client is made[1])

print("a single bad league never triggers it")
b, made_b = make()
with redirect_stdout(io.StringIO()):
    league(b, "1000", refused=True)
    league(b, "1001", refused=False)
    league(b, "1002", refused=True)
    league(b, "1003", refused=False)
check("refusals separated by grants: no rebuild", len(made_b) == 1 and b._ws_rebuilds == 0, len(made_b))

print("a rebuild skipped by the rate limit pauses the subscribe pass")
c, made_c = make()
c._last_ws_rebuild = time.time()                    # a rebuild a moment ago: the 60s gap applies
with redirect_stdout(io.StringIO()):
    league(c, "1000", refused=True)
    league(c, "1001", refused=True)
check("the rebuild is skipped (rate limit)", len(made_c) == 1 and c._socket_refusing())
c._subscribe_gap_sec = 0.001
sent_before = len(made_c[0].topics)


async def run_reconciler(ad, secs=0.05):
    t = asyncio.create_task(ad._sub_reconciler())
    await asyncio.sleep(secs)
    t.cancel()


with redirect_stdout(io.StringIO()):
    asyncio.run(run_reconciler(c))
check("...and no more SUBSCRIBEs go to the refusing socket", len(made_c[0].topics) == sent_before,
      len(made_c[0].topics) - sent_before)
with redirect_stdout(io.StringIO()):
    c._client.reconnect()                           # the watchdog's fallback when a rebuild is skipped
    asyncio.run(run_reconciler(c))
check("a fresh connect resumes the pass", len(made_c[0].topics) > sent_before)

print("a socket that was served, then refuses after a re-subscribe (the 10-23 09:13 shape)")
d, made_d = make()
with redirect_stdout(io.StringIO()):
    league(d, "1000", refused=False)
    d._subscribed.clear()                           # the watchdog's drop-and-resubscribe
    league(d, "1000", refused=True)
    league(d, "1001", refused=True)
check("refusals after an earlier grant still rebuild", len(made_d) == 2 and d._ws_rebuilds == 1, len(made_d))

print("PINNACLE_WS_REFUSED_REBUILD_LEAGUES=0 turns it off")
e, made_e = make()
e._refusal_rebuild_leagues = 0
with redirect_stdout(io.StringIO()):
    for lid in ("1000", "1001", "1002"):
        league(e, lid, refused=True)
check("no rebuild, no pause", len(made_e) == 1 and not e._socket_refusing())

print("SUBACKs arrive on paho's thread; the rebuild runs on the event loop")
f, made_f = make()


async def from_paho_thread():
    f._loop = asyncio.get_running_loop()
    th = threading.Thread(target=lambda: (league(f, "1000", refused=True), league(f, "1001", refused=True)))
    th.start()
    th.join()
    queued = len(made_f)
    await asyncio.sleep(0.05)
    return queued, len(made_f)


with redirect_stdout(io.StringIO()):
    queued, after = asyncio.run(from_paho_thread())
check("not rebuilt on the paho thread itself", queued == 1, queued)
check("rebuilt by the loop right after", after == 2 and f._ws_rebuilds == 1, after)

print(f"\n{'=' * 58}\n  {PASS} passed, {FAIL} failed\n{'=' * 58}")
sys.exit(1 if FAIL else 0)
