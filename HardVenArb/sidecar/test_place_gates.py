"""place_bet's refusal gates. Run: python test_place_gates.py

These run BEFORE anything is clicked, so they are the last line between a bad number and real money.
Driven against a stubbed adapter: no browser, no venue, no bet. Every case here must refuse, and the
one thing each refusal must never do is look like a clean rejection when the order might be live.
"""
import asyncio
import os
import sys
import types

os.environ.setdefault("BIA_TRANSPORT", "direct")   # no browser in the constructor

import betinasia_adapter as A

bad = 0


def check(label, ok):
    global bad
    print(f"  {'PASS' if ok else 'FAIL'}  {label}")
    if not ok:
        bad += 1


def make(bet_enabled=True, max_stake=100.0, quote=None):
    """An adapter with the gates set and everything past them stubbed out."""
    a = A.BetInAsiaAdapter.__new__(A.BetInAsiaAdapter)
    a._bet_enabled = bet_enabled
    a._max_stake = max_stake
    a._slip_lock = asyncio.Lock()
    a._slip_page = None
    a.observer = types.SimpleNamespace(pause_organic=lambda: None, resume_organic=lambda: None)
    a._slip_quote_outer = lambda sid: asyncio.sleep(0, result=(quote or {"ok": False, "error": "stub"}))
    return a


def place(a, sid="tennis:1:x~y~z:tennis_match~all:p1", stake=10.0, odds=2.0):
    return asyncio.run(a.place_bet(sid, stake, odds))


print("[1] the arming gate")
r = place(make(bet_enabled=False))
check("refuses with HARDVEN_BET_ENABLE unset", r.accepted is False)
check("and says why", "HARDVEN_BET_ENABLE" in (r.reason or ""))

print("\n[2] the sidecar stake ceiling — an INDEPENDENT check on the bot's own cap")
r = place(make(max_stake=5.0), stake=10.0)
check("refuses a stake over HARDVEN_MAX_STAKE", r.accepted is False)
check("names both numbers", "10.00" in (r.reason or "") and "5.00" in (r.reason or ""))
r = place(make(max_stake=0.0), stake=10.0)          # 0 = unset
check("0 means no ceiling, so it passes to the next gate",
      r.accepted is False and "MAX_STAKE" not in (r.reason or ""))

print("\n[3] the venue's odds-dependent minimum (~$5 return)")
r = place(make(), stake=1.0, odds=1.30)             # returns 1.30
check("refuses a stake whose RETURN is under the minimum", r.accepted is False)
check("explains it as a return, not a stake", "return" in (r.reason or "").lower())
r = place(make(), stake=4.0, odds=1.30)             # returns 5.20
check("allows one that clears it", "minimum return" not in (r.reason or ""))

print("\n[4] a slip that will not open")
r = place(make(quote={"ok": False, "error": "no board ROW for x"}))
check("refuses", r.accepted is False)
check("passes the underlying reason through", "no board ROW" in (r.reason or ""))

print("\n[5] the price floor is enforced on the SLIP's number")
r = place(make(quote={"ok": True, "decimal_odds": 1.90, "selection_label": "X"}), odds=2.10)
check("refuses when the slip offers less than required", r.accepted is False)
check("reports what was offered", r.actual_odds == 1.90)
check("names both prices", "1.9" in (r.reason or "") and "2.1" in (r.reason or ""))

print("\n[6] an unusable quote")
r = place(make(quote={"ok": True, "decimal_odds": 1.0}))
check("refuses odds <= 1.0", r.accepted is False and "no usable price" in (r.reason or ""))

print("\n[7] no refusal may read as 'definitely not placed' once the click could have landed")
# Every gate above fires BEFORE the Place click, so 'not placed' is honest for all of them.
for label, r in (("arming", place(make(bet_enabled=False))),
                 ("ceiling", place(make(max_stake=5.0), stake=10.0)),
                 ("price floor", place(make(quote={"ok": True, "decimal_odds": 1.5}), odds=2.0))):
    check(f"{label}: refused before any click, so no ambiguity", r.accepted is False and r.bet_id is None)

print("\nALL PASS" if bad == 0 else f"\nFAILURES: {bad}")
sys.exit(1 if bad else 0)
