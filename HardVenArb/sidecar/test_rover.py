"""Pins the two-tab split: the roving tab, and per-socket betslip subscriptions.

Two things are under test.

1. SUBSCRIPTIONS DIE WITH THEIR SOCKET. `watch_acca_hcaps` is sent by a page over one socket; the venue
   forgets it when that socket goes. Tracking them in one global set made slip_quote refuse to click
   ("already subscribed") against a socket that had never subscribed anything — a permanent blind spot
   for that event. A roving tab navigates constantly, so it would mint one on every hop.

2. THE ROVER ADDRESSES LEAGUES DIRECTLY. country + competition_id are on 100% of captured event frames,
   so the rover can go straight to the league page instead of loading a sport board and expanding it.

Run: python test_rover.py       (from HardVenArb/sidecar)
"""
import asyncio
import json
import sys

import betinasia_adapter as A
from betinasia_observer import BetInAsiaObserver

FAIL = 0


def check(label, cond, detail=""):
    global FAIL
    print(f"  {'PASS' if cond else 'FAIL'}  {label}" + (f"   [{detail}]" if detail and not cond else ""))
    if not cond:
        FAIL += 1


class _FakeWS:
    """Stands in for a Playwright WebSocket: identity is all the subscription bookkeeping uses."""
    def __init__(self, name):
        self.name = name


def _watch(obs, ws, sport, ekey, kind="watch_acca_hcaps"):
    obs._on_sent(json.dumps([kind, [[1, sport, ekey]]]), ws)


print("\n[1] betslip subscriptions are tracked PER SOCKET")
obs = BetInAsiaObserver()
a, b = _FakeWS("a"), _FakeWS("b")
_watch(obs, a, "tennis", "E1")
_watch(obs, b, "baseball", "E2")
check("both sockets' subscriptions are visible", obs._acca_subs == {("tennis", "E1"), ("baseball", "E2")},
      str(obs._acca_subs))

print("\n[2] closing a socket releases ONLY its subscriptions")
obs._on_ws_close(a)
check("socket a's subscription is gone", ("tennis", "E1") not in obs._acca_subs, str(obs._acca_subs))
check("socket b's subscription survives", ("baseball", "E2") in obs._acca_subs, str(obs._acca_subs))

print("\n[3] REGRESSION: a re-opened tab is not reported as still subscribed")
# The bug: tab closes, venue drops the sub, but the global set still claimed it -> slip_quote refused to
# click forever after. The user hit exactly this ("I closed the page and re-opened it").
obs2 = BetInAsiaObserver()
old = _FakeWS("old")
_watch(obs2, old, "tennis", "MATCH")
obs2._on_ws_close(old)
check("event is quotable again after the socket died", ("tennis", "MATCH") not in obs2._acca_subs)
new = _FakeWS("new")
_watch(obs2, new, "tennis", "MATCH")
check("the fresh socket's subscription registers", ("tennis", "MATCH") in obs2._acca_subs)

print("\n[4] unwatch removes only from the socket that sent it")
obs3 = BetInAsiaObserver()
c, d = _FakeWS("c"), _FakeWS("d")
_watch(obs3, c, "fb", "G")
_watch(obs3, d, "fb", "G")
_watch(obs3, c, "fb", "G", kind="unwatch_acca_hcaps")
check("still subscribed via the other socket", ("fb", "G") in obs3._acca_subs, str(obs3._acca_subs))
obs3._on_ws_close(d)
check("gone once every holder is released", ("fb", "G") not in obs3._acca_subs, str(obs3._acca_subs))

print("\n[5] board subscription accounting still works (no regression from the ws arg)")
obs4 = BetInAsiaObserver()
obs4._on_sent(json.dumps(["watch_hcaps", [[7, "tennis", "B1"]]]), _FakeWS("x"))
check("watch_hcaps recorded", ("tennis", "B1") in obs4._sub_order, str(obs4._sub_order))
obs4._on_sent(json.dumps(["watch_hcaps", [[7, "tennis", "B2"]]]))   # legacy call, no socket
check("a socket-less call does not raise", ("tennis", "B2") in obs4._sub_order)


print("\n[6] league URL is built from country + comp_id")


class _FeedStub:
    def __init__(self, ev):
        self._ev = ev

    def get_event(self, sport, ekey):
        return self._ev


ad = A.BetInAsiaAdapter.__new__(A.BetInAsiaAdapter)          # no browser/session needed for URL building
ad.feed = _FeedStub({"country": "AR", "competition_id": 24805})
url = ad._league_url("fb", "2026-08-05,31470,529", 24805)
check("matches the captured shape /sportsbook/football/AR/24805",
      url.endswith("/sportsbook/football/AR/24805"), url)

ad.feed = _FeedStub({})                                       # country unknown
check("no country -> no URL (never guess a 404)", ad._league_url("fb", "E", 24805) == "")
ad.feed = _FeedStub({"country": "AR"})
check("no comp_id -> no URL", ad._league_url("fb", "E", None) == "")
ad.feed = _FeedStub({"country": "GB"})
check("unverified sport slug -> no URL", ad._league_url("darts", "E", 99) == "")

print("\n[7] the rover is a SECOND tab, reused across quotes")


class _FakePage:
    def __init__(self, ctx):
        self.ctx, self.url, self.closed, self.visited = ctx, "about:blank", False, []

    def is_closed(self):
        return self.closed

    async def goto(self, url, **kw):
        self.url = url
        self.visited.append(url)


class _FakeCtx:
    def __init__(self):
        self.pages_made = 0

    async def new_page(self):
        self.pages_made += 1
        return _FakePage(self)


async def _rover_checks():
    o = BetInAsiaObserver()
    ctx = _FakeCtx()
    o._ctx = ctx
    board = object()
    o._page = board                                  # the observing tab
    p1 = await o.rover()
    check("a tab is created on first use", ctx.pages_made == 1, f"made {ctx.pages_made}")
    check("the rover is NOT the observing tab", p1 is not board)
    p2 = await o.rover()
    check("the same tab is reused (no tab per quote)", p2 is p1 and ctx.pages_made == 1,
          f"made {ctx.pages_made}")
    p3 = await o.rover("https://x/sportsbook/tennis/GB/1")
    check("navigating goes to the rover", p3.url.endswith("/tennis/GB/1"), p3.url)
    check("the observing tab was never navigated", o._page is board)
    p1.closed = True                                 # user closed it
    p4 = await o.rover()
    check("a closed rover is replaced", p4 is not p1 and ctx.pages_made == 2, f"made {ctx.pages_made}")

asyncio.run(_rover_checks())

print("\n" + ("FAILURES: %d" % FAIL if FAIL else "ALL PASS"))
sys.exit(1 if FAIL else 0)
