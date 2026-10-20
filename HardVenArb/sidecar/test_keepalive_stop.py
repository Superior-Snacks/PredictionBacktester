"""A league pass stops at the first venue 5xx (2026-10-19).

Pinnacle went down at ~11:01 and the next session-keepalive pass sent all 19 of its league GETs into the 503.
The first answer already says the venue is down. This pins: the pass stops after that one request, a healthy
pass still covers every league, a non-5xx failure (404, network error) does not stop it, a fetch that sends
nothing cannot inherit a stale 503, and the reader re-seed (guest source) stops the same way. Offline: no
venue, no network - the HTTP clients are fakes.

    python test_keepalive_stop.py          # exit 0 = all pass
"""
from __future__ import annotations
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("PINNACLE_ODDS_MODE", "ws")
from pinnacle_adapter import PinnacleAdapter  # noqa: E402
import maintenance  # noqa: E402

PASS = FAIL = 0


def check(name, ok, got=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}" + (f"  (got {got})" if got != "" else ""))


class Resp:
    def __init__(self, code):
        self.status_code = code
        self.headers = {}

    def json(self):
        return []


class FakeClient:
    """Answers with `codes` in turn (the last one repeats); an Exception instance is raised instead."""
    def __init__(self, codes):
        self.codes, self.calls = list(codes), []

    async def get(self, url):
        self.calls.append(url)
        c = self.codes[min(len(self.calls) - 1, len(self.codes) - 1)]
        if isinstance(c, Exception):
            raise c
        return Resp(c)


def adapter(codes):
    a = PinnacleAdapter()
    a._session_source = "browser"
    a._session_proven = True          # authed REST allowed
    a._jitter_ms = 0
    a._http = FakeClient(codes)
    return a


LEAGUES = [str(1000 + i) for i in range(19)]

a = adapter([503])
_, stopped = asyncio.run(a._league_pass(LEAGUES, a._refresh_league, "session-keepalive", stop_on_giveup=True))
check("venue down: ONE request, not 19", len(a._http.calls) == 1, len(a._http.calls))
check("...and the pass reports the code it stopped on", stopped == 503, stopped)

a = adapter([200])
_, stopped = asyncio.run(a._league_pass(LEAGUES, a._refresh_league, "session-keepalive", stop_on_giveup=True))
check("healthy venue: every league is fetched", len(a._http.calls) == 19 and stopped is None, len(a._http.calls))

a = adapter([200, 200, 502])
_, stopped = asyncio.run(a._league_pass(LEAGUES, a._refresh_league, "session-keepalive"))
check("a 5xx mid-pass stops it right there", len(a._http.calls) == 3 and stopped == 502, len(a._http.calls))

a = adapter([404, ConnectionError("blip"), 200])
_, stopped = asyncio.run(a._league_pass(LEAGUES, a._refresh_league, "session-keepalive"))
check("a 404 or a network error is not the venue down - the pass goes on",
      len(a._http.calls) == 19 and stopped is None, len(a._http.calls))

a = adapter([503])
a._last_http_status = 503             # left over from an earlier call (e.g. the balance check)
a._session_proven = False             # authed REST held -> the fetch sends nothing
_, stopped = asyncio.run(a._league_pass(LEAGUES, a._refresh_league, "session-keepalive"))
check("a fetch that sends nothing does not inherit a stale 503", stopped is None and a._http.calls == [],
      (stopped, len(a._http.calls)))

a = adapter([200])
a._reseed_source = "guest"
a._guest_http = FakeClient([503])
_, stopped = asyncio.run(a._league_pass(LEAGUES, a._reseed_league, "reader re-seed"))
check("the reader re-seed (guest) stops the same way", len(a._guest_http.calls) == 1 and stopped == 503,
      len(a._guest_http.calls))
check("...and its guest 503 still trips the maintenance latch as before", maintenance.active())

print(f"\n{'=' * 58}\n  {PASS} passed, {FAIL} failed\n{'=' * 58}")
sys.exit(1 if FAIL else 0)
