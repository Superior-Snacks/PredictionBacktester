"""The WS fill observer, driven by the REAL frames from order 2013069548 (2026-08-14).

The venue pushes the whole order lifecycle, so a fill needs no polling and no request. What the executor
takes from it is two numbers the request does NOT contain: the price actually routed (1.88 asked, 1.90
filled) and the stake actually routed (4.00 asked, 3.9917 filled). Sizing the Kalshi leg off the request
would leave that residual naked on every single trade.

Run: python test_fill_observer.py
"""
import sys

from betinasia_ws import BetInAsiaFeed

OID = 2013069548

# Verbatim sequence, trimmed to the fields the observer reads.
FRAMES = [
    ["api", {"ts": 1786733123.7, "data": [
        ["order", {"order_id": OID, "status": "open", "price": None, "stake": None,
                   "want_price": 1.88, "want_stake": ["USD", 4.0],
                   "closed": False, "close_reason": None}]]}],
    ["api", {"ts": 1786733123.8, "data": [
        ["bet", {"order_id": OID, "bet_id": 14425092231, "bookie": "overtime",
                 "status": {"code": "placing"}, "want_price": 1.9,
                 "want_stake": ["USD", 3.9917]}]]}],
    ["api", {"ts": 1786733137.3, "data": [
        ["order", {"order_id": OID, "status": "open", "price": 1.9, "stake": ["USD", 3.9917],
                   "want_price": 1.88, "want_stake": ["USD", 4.0], "closed": False}]]}],
    ["api", {"ts": 1786733137.3, "data": [
        ["bet", {"order_id": OID, "bet_id": 14425092231, "bookie": "overtime",
                 "status": {"code": "done"}, "want_price": 1.9,
                 "want_stake": ["USD", 3.9917]}]]}],
    ["api", {"ts": 1786733137.4, "data": [
        ["order", {"order_id": OID, "status": "done", "price": 1.9, "stake": ["USD", 3.9917],
                   "want_price": 1.88, "want_stake": ["USD", 4.0],
                   "closed": True, "close_reason": "order_filled"}]]}],
]

bad = 0


def check(label, ok):
    global bad
    print(f"  {'PASS' if ok else 'FAIL'}  {label}")
    if not ok:
        bad += 1


feed = BetInAsiaFeed(passive=True)

print("[1] before anything is pushed")
f = feed.order_fill(OID)
check("unknown order is not 'done'", f["known"] is False and f["done"] is False)
check("and reports no fill rather than a zero one", f["filled_stake"] == 0.0 and f["avg_price"] is None)

print("\n[2] order accepted — nothing filled yet")
feed.handle_frame([FRAMES[0]])
f = feed.order_fill(OID)
check("known now", f["known"] is True)
check("NOT done while the venue says open", f["done"] is False)
check("null stake is not read as a zero fill", f["filled_stake"] == 0.0)
check("the request is retained", f["want_price"] == 1.88 and f["want_stake"] == 4.0)

print("\n[3] routed to a bookie — the haircut is already applied")
feed.handle_frame([FRAMES[1]])
f = feed.order_fill(OID)
check("bookie captured", f["bookies"] == ["overtime"])
check(f"routed stake 3.9917, not the 4.00 requested (got {f['filled_stake']})",
      abs(f["filled_stake"] - 3.9917) < 1e-9)
check(f"routed price 1.90, not the 1.88 requested (got {f['avg_price']})", f["avg_price"] == 1.9)
check("STILL not done — 'placing' is not a fill", f["done"] is False)

print("\n[4] filled")
for fr in FRAMES[2:]:
    feed.handle_frame([fr])
f = feed.order_fill(OID)
check("done", f["done"] is True)
check("close_reason", f["close_reason"] == "order_filled")
check("status", f["status"] == "done")
check(f"final stake {f['filled_stake']}", abs(f["filled_stake"] - 3.9917) < 1e-9)
check(f"final price {f['avg_price']}", f["avg_price"] == 1.9)

print("\n[5] the numbers the Kalshi leg must be sized against")
short = f["want_stake"] - f["filled_stake"]
check(f"short-filled by {short:.4f} — sizing off want_stake leaves that naked", short > 0)
check("price moved in OUR favour, so the arb is better than modelled",
      f["avg_price"] > f["want_price"])

print("\n[6] a partial: stake present, still open")
feed2 = BetInAsiaFeed(passive=True)
feed2.handle_frame([["api", {"data": [
    ["order", {"order_id": 999, "status": "open", "price": 2.0, "stake": ["USD", 1.5],
               "want_price": 2.0, "want_stake": ["USD", 5.0], "closed": False}]]}]])
p = feed2.order_fill(999)
check("reports the partial stake", p["filled_stake"] == 1.5)
check("but is NOT done — an order can hold a stake and still be open", p["done"] is False)

print("\n[7] merging: a later frame must not erase an earlier field")
feed2.handle_frame([["api", {"data": [["order", {"order_id": 999, "status": "done",
                                                 "closed": True, "close_reason": "order_filled"}]]}]])
p = feed2.order_fill(999)
check("stake survived a record that omitted it", p["filled_stake"] == 1.5)
check("and the close came through", p["done"] is True and p["close_reason"] == "order_filled")

print("\n[8] two bookies -> stake-weighted average price")
feed3 = BetInAsiaFeed(passive=True)
for bid, bk, px, st in ((1, "bf", 2.0, 3.0), (2, "pin88", 3.0, 1.0)):
    feed3.handle_frame([["api", {"data": [["bet", {"order_id": 7, "bet_id": bid, "bookie": bk,
                                                   "status": {"code": "done"}, "want_price": px,
                                                   "want_stake": ["USD", st]}]]}]])
f3 = feed3.order_fill(7)
check(f"stake 4.0 (got {f3['filled_stake']})", f3["filled_stake"] == 4.0)
check(f"weighted 2.25, not the 2.5 midpoint (got {f3['avg_price']})", abs(f3["avg_price"] - 2.25) < 1e-9)
check("both bookies listed", f3["bookies"] == ["bf", "pin88"])

print("\n[9] junk cannot break it")
for junk in (None, {}, {"data": None}, {"data": [["order", None]]}, {"data": [["bet", {}]]},
             {"data": [["nonsense", {"order_id": 1}]]}):
    feed3.handle_frame([["api", junk]])
check("survives malformed api frames", feed3.order_fill(7)["filled_stake"] == 4.0)

print("\nALL PASS" if bad == 0 else f"\nFAILURES: {bad}")
sys.exit(1 if bad else 0)
