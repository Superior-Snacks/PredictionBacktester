"""Window open must not REST-seed leagues the bot stopped asking about (2026-10-17).

Pruning of the active-league set runs in the socket loops, which are off between windows, so a dark period
accumulates every league the bot asked about and then stopped (finished tennis rounds, shadow games leaving
their scope): 36 against 16 live after one evening. The first /odds of the window seeded all of them, one REST
GET each, in sequence, inside that request - the wanted tennis leagues waited behind the stale ones. Offline:
no venue, no network.

    python test_seed_active.py          # exit 0 = all pass
"""
from __future__ import annotations
import asyncio
import os
import sys
import time

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


def make():
    a = PinnacleAdapter()
    a._mode = "ws"
    a._session_source = "browser"
    a._session_ready = True
    seeded = []

    async def seed(lid):
        seeded.append(lid)

    a._refresh_league = seed          # whichever seeding path the reader mode picks, it is counted
    a._reseed_league = seed
    a._start_ws = lambda: None        # never connect
    return a, seeded


a, seeded = make()
now = time.time()
a._active_leagues["9001"] = now - 3600      # a tennis round that finished an hour ago
a._active_leagues["1419"] = now - 700       # a shadow game that left its scope
a._active_leagues["559"] = now - 60         # asked for a minute ago (inside the 600s TTL), not in this call
asyncio.run(a.odds(["1000:1:home", "1000:1:away", "2000:5:spread:-1.5:home"]))
check("the leagues of this call are seeded", {"1000", "2000"} <= set(seeded), seeded)
check("a league asked for within the TTL is still seeded", "559" in seeded, seeded)
check("leagues not asked for in >TTL are NOT seeded at window open",
      "9001" not in seeded and "1419" not in seeded, seeded)
check("...and leave the active set", "9001" not in a._active_leagues and "1419" not in a._active_leagues)
check("each league is seeded once", len(seeded) == len(set(seeded)), seeded)
n = len(seeded)
asyncio.run(a.odds(["1000:1:home"]))
check("a later /odds does not seed again", len(seeded) == n, seeded)

b, seeded_b = make()
b._session_ready = False                    # dark: the session is closed
b._active_leagues["9001"] = time.time() - 3600
asyncio.run(b.odds(["1000:1:home"]))
check("while dark nothing is seeded (the cache is served as before)", seeded_b == [], seeded_b)

print(f"\n{'=' * 58}\n  {PASS} passed, {FAIL} failed\n{'=' * 58}")
sys.exit(1 if FAIL else 0)
