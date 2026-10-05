"""A pre-match REST snapshot must never overwrite an IN-PLAY token (2026-10-05).

/leagues/{lid}/markets/straight lists a match that has gone in-play at its LAST PRE-MATCH LINE, status open.
The keepalive re-seed wrote that line over the live price (keeping only the in-play TAG), so the cache served
the frozen pre-match price as a live one until the next WS push - 83% of the EV bot's in-play "snaps" landed
exactly on the pre-match price. This pins the fix: in-play tokens keep their WS price and status; pre-match
tokens are still refreshed; the league still reports the tokens as covered.

    python test_reseed_inplay.py          # exit 0 = all pass
"""
from __future__ import annotations
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pinnacle_adapter import PinnacleAdapter  # noqa: E402
from book_adapter import Selection  # noqa: E402


def check(name, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}{('  ' + detail) if detail else ''}")
    return bool(ok)


def market(mid: int, home: int, away: int) -> dict:
    """A /markets/straight moneyline, American prices, as the REST endpoint returns it."""
    return {"matchupId": mid, "type": "moneyline", "period": 0, "status": "open",
            "prices": [{"designation": "home", "price": home}, {"designation": "away", "price": away}]}


def main() -> int:
    a = PinnacleAdapter()
    now = time.time()
    lid = "1000"
    # matchup 555 is IN-PLAY: the WS has it at 1.40 / 3.10, one leg suspended mid-point
    a._cache[f"{lid}:555:home"] = Selection(f"{lid}:555:home", 1.40, 100.0, status="open", ts=now, live=True)
    a._cache[f"{lid}:555:away"] = Selection(f"{lid}:555:away", 3.10, 100.0, status="suspended", ts=now, live=True)
    # matchup 777 is PRE-MATCH, cached from an earlier seed
    a._cache[f"{lid}:777:home"] = Selection(f"{lid}:777:home", 1.80, 100.0, status="open", ts=now - 90, live=False)

    # the keepalive snapshot: 555 at its old PRE-MATCH line (-300 / +250), 777 moved to -150 / +130
    n = a._apply_straight_markets(lid, [market(555, -300, 250), market(777, -150, 130)], now + 1)

    h, w = a._cache[f"{lid}:555:home"], a._cache[f"{lid}:555:away"]
    ok = True
    ok &= check("in-play price is KEPT, not replaced by the pre-match line", h.decimal_odds == 1.40,
                f"home {h.decimal_odds} (pre-match line would be {1 + 100 / 300:.3f})")
    ok &= check("in-play tag kept", h.live and w.live)
    ok &= check("a suspended in-play leg is NOT reopened by a pre-match snapshot",
                w.status == "suspended" and w.decimal_odds == 3.10, f"{w.status} @ {w.decimal_odds}")
    p = a._cache[f"{lid}:777:home"]
    ok &= check("pre-match tokens are still refreshed", abs(p.decimal_odds - (1 + 100 / 150)) < 1e-9 and not p.live,
                f"{p.decimal_odds:.4f}")
    ok &= check("new pre-match tokens are still added", f"{lid}:777:away" in a._cache)
    ok &= check("the league reports every token it COVERED (a refetch still reads as answered)", n == 4, f"n={n}")
    ok &= check("each refusal is counted for /debug/inplay", a._reseed_kept_live == 2, f"{a._reseed_kept_live}")
    ok &= check("and published there", a.inplay_diagnostics().get("reseed_kept_live") == 2)
    print("ALL PASS" if ok else "FAILURES ABOVE")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
