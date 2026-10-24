"""The organic nav-click must not walk the board off its home (2026-10-23).

The click re-clicks the sport's nav row, which links to the PRE-MATCH list. With the board's home on the LIVE
list every click left home and the drift watchdog navigated back 180s later - two page loads, and two fresh
logins on the page's odds socket, per click (26 of the page's 42 socket re-opens in 2h47m). Offline: a fake
page, no browser.

    python test_navclick_home.py        # exit 0 = all pass
"""
from __future__ import annotations
import asyncio
import io
import os
import sys
from contextlib import redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")   # the nav-click line prints an arrow
import organic  # noqa: E402
from organic import OrganicActivity  # noqa: E402

PASS = FAIL = 0
LIVE = "https://www.pinnacle.bet/en/tennis/matchups/live/"
PRE = "https://www.pinnacle.bet/en/tennis/matchups/"


def check(name, ok, got=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}" + (f"  (got {got})" if got != "" else ""))


class Keyboard:
    def __init__(self):
        self.keys = []

    async def press(self, key):
        self.keys.append(key)


class Page:
    def __init__(self, url):
        self.url = url
        self.keyboard = Keyboard()


def make(home_fn, browse=LIVE):
    page = Page(LIVE)
    a = OrganicActivity(page, browse_urls=[browse], home_fn=home_fn)
    clicks = []

    async def fake_click():
        clicks.append(1)
        return "tennis"

    a._nav_click_random = fake_click
    return a, page, clicks


def nav_tick(a):
    """One tick with the roll forced into the nav-click band (>= 0.85)."""
    real = organic.random.random
    organic.random.random = lambda: 0.9
    try:
        return asyncio.run(a.tick())
    finally:
        organic.random.random = real


for k in ("HARDVEN_ORGANIC_SPORT_FLIP", "HARDVEN_ORGANIC_NAVCLICK_LEAVE_HOME"):
    os.environ.pop(k, None)

print("home = the LIVE list (the 2026-10-23 setup)")
a, page, clicks = make(lambda: LIVE)
buf = io.StringIO()
with redirect_stdout(buf):
    n1, n2 = nav_tick(a), nav_tick(a)
check("the click would leave home", a._navclick_leaves_home())
check("the nav-click band does a keyboard scroll instead", (n1, n2) == ("keyscroll", "keyscroll"), (n1, n2))
check("...the sport row is never clicked", clicks == [], clicks)
check("...and the scroll really pressed keys", len(page.keyboard.keys) > 0, page.keyboard.keys)
check("the reason is printed once, not per tick", buf.getvalue().count("nav-click OFF") == 1, buf.getvalue())

print("home = the sport's own pre-match list (the click lands on home)")
a, page, clicks = make(lambda: PRE)
check("the click stays home", not a._navclick_leaves_home())
check("...so the nav-click still fires", nav_tick(a) == "navclick" and clicks == [1], clicks)
a, page, clicks = make(lambda: PRE + "#period:0")
check("a fragment on home is not a different page", not a._navclick_leaves_home())

print("no home known / opt-outs keep the old behaviour")
a, _, clicks = make(None)
check("no home_fn: the click fires as before", nav_tick(a) == "navclick" and clicks == [1], clicks)
a, _, _ = make(lambda: "")
check("an empty home: the click is allowed", not a._navclick_leaves_home())


def boom():
    raise RuntimeError("session gone")


a, _, _ = make(boom)
check("a failing home_fn: the click is allowed (never crashes the loop)", not a._navclick_leaves_home())
os.environ["HARDVEN_ORGANIC_SPORT_FLIP"] = "1"
a, _, _ = make(lambda: LIVE)
check("sport-flip on: leaving home is the point, so the click is allowed", not a._navclick_leaves_home())
os.environ.pop("HARDVEN_ORGANIC_SPORT_FLIP")
os.environ["HARDVEN_ORGANIC_NAVCLICK_LEAVE_HOME"] = "1"
a, _, clicks = make(lambda: LIVE)
check("HARDVEN_ORGANIC_NAVCLICK_LEAVE_HOME=1 restores the click",
      nav_tick(a) == "navclick" and clicks == [1], clicks)
os.environ.pop("HARDVEN_ORGANIC_NAVCLICK_LEAVE_HOME")

print("home follows the session (in-play re-points it)")
home = {"url": PRE}
a, _, _ = make(lambda: home["url"])
before = a._navclick_leaves_home()
home["url"] = LIVE
check("the check reads the current home every tick", (before, a._navclick_leaves_home()) == (False, True))

print(f"\n{'=' * 58}\n  {PASS} passed, {FAIL} failed\n{'=' * 58}")
sys.exit(1 if FAIL else 0)
