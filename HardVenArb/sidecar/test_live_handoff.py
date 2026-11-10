"""
test_live_handoff.py — a paired PRE-MATCH token must follow its fixture in-play even when Pinnacle's live matchup
carries no parentId (2026-11-10).

When a match goes live Pinnacle issues a NEW matchup; the pair still holds the old one. `_read_cache` follows it by
the parentId link, or by the players. In dedicated-WS mode the players link was only tried on a cache MISS, and the
retired parent is usually still cached (suspended, or re-seeded at its pre-match line), so a child without a
parentId stranded the pair until the 90-minute pairing run: 90 of 336 tennis matches Pinnacle offered live
(11-03..11-10), a median 70 minutes of play each. Exercises the REAL `_apply` and `_read_cache`.

    python test_live_handoff.py            # all PASS expected
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pinnacle_adapter import PinnacleAdapter  # noqa: E402
from book_adapter import Selection  # noqa: E402

LID, PARENT, CHILD, GAMES = "9417", "1640000001", "1640000777", "1640000778"
P_HOME, P_AWAY = f"{LID}:{PARENT}:home", f"{LID}:{PARENT}:away"
START = "2026-11-10T10:00:00Z"


def iso(sec_from_now):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + sec_from_now))


def push(mid, home, away, price_home, price_away, start=START, units="Sets", parent=None):
    rec = {"id": int(mid), "league": {"id": int(LID)}, "startTime": start, "units": units,
           "participants": [{"alignment": "home", "name": f"{home} ({units})"},
                            {"alignment": "away", "name": f"{away} ({units})"}],
           "markets": [{"type": "moneyline", "period": 0, "status": "open", "cutoffAt": iso(3600),
                        "limits": [{"amount": 500}],
                        "prices": [{"designation": "home", "price": price_home},
                                   {"designation": "away", "price": price_away}]}]}
    if parent:
        rec["parentId"] = int(parent)
    return {"op": "upd", "rec": rec}


def adapter(parent_status="suspended", pair_start=START):
    """The state the bug lived in: the paired parent cached, NOT live, and the pairing's names on record."""
    a = PinnacleAdapter()
    now = time.time()
    a._cache[P_HOME] = Selection(P_HOME, 1.80, 100.0, status=parent_status, ts=now, live=False)
    a._cache[P_AWAY] = Selection(P_AWAY, 2.05, 100.0, status=parent_status, ts=now, live=False)
    a._pair_names = {P_HOME: ("anna alpha", "bea beta"), P_AWAY: ("bea beta", "anna alpha")}
    a._pair_starts = {P_HOME: pair_start, P_AWAY: pair_start} if pair_start else {}
    a._pair_names_ts = now + 3600                      # no reload from the real pair file during the test
    return a


def check(name, got, want):
    ok = got == want
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}: {got} (expected {want})")
    return ok


def main() -> int:
    r = []

    # 1. THE BUG: live child, same players, NO parentId -> the cached parent is now followed into play
    a = adapter()
    a._apply(push(CHILD, "Anna Alpha", "Bea Beta", -150, +130), live=True)
    out = a._read_cache([P_HOME, P_AWAY], time.time())
    r.append(check("parent home served from the live child", (out[P_HOME].live, round(out[P_HOME].decimal_odds, 3)),
                   (True, round(1 + 100 / 150, 3))))
    r.append(check("parent away served from the live child", (out[P_AWAY].live, round(out[P_AWAY].decimal_odds, 3)),
                   (True, 2.3)))
    r.append(check("counted as a redirect found by the players", (a._parent_redirects, a._name_redirects), (2, 2)))

    # 2. side by NAME: the child lists the players the other way round
    b = adapter()
    b._apply(push(CHILD, "Bea Beta", "Anna Alpha", +130, -150), live=True)
    out = b._read_cache([P_HOME], time.time())
    r.append(check("Anna is still Anna when the child swaps home/away", round(out[P_HOME].decimal_odds, 3),
                   round(1 + 100 / 150, 3)))

    # 3. before go-live nothing changes
    c = adapter(parent_status="open")
    c._apply(push(PARENT, "Anna Alpha", "Bea Beta", -120, +100), live=False)    # the parent's own pre-match push
    out = c._read_cache([P_HOME], time.time())
    r.append(check("pre-match: the parent is served as itself", (out[P_HOME].live, out[P_HOME].selection_id),
                   (False, P_HOME)))

    # 4. the rematch guard: same two teams, a live game 3 days from the paired one -> not this fixture
    d = adapter(pair_start="2026-11-13T10:00:00Z")
    d._apply(push(CHILD, "Anna Alpha", "Bea Beta", -150, +130, start=START), live=True)
    out = d._read_cache([P_HOME], time.time())
    r.append(check("a live game 3 days off is NOT followed", (out[P_HOME].live, out[P_HOME].selection_id),
                   (False, P_HOME)))
    e = adapter(pair_start=None)                                                 # no start on record
    e._apply(push(CHILD, "Anna Alpha", "Bea Beta", -150, +130), live=True)
    out = e._read_cache([P_HOME], time.time())
    r.append(check("unknown start: the names are trusted, as before", out[P_HOME].live, True))

    # 5. the "(Games)" twin pushing AFTER the Sets child must not take the players' slot
    f = adapter()
    f._apply(push(CHILD, "Anna Alpha", "Bea Beta", -150, +130), live=True)
    f._apply(push(GAMES, "Anna Alpha", "Bea Beta", -400, +300, units="Games"), live=True)
    out = f._read_cache([P_HOME], time.time())
    r.append(check("the Games twin cannot hijack the moneyline", round(out[P_HOME].decimal_odds, 3),
                   round(1 + 100 / 150, 3)))
    r.append(check("...and the Games matchup is still in the live catalog (derivative pairing)",
                   f"{LID}:{GAMES}" in f._live_meta, True))

    # 6. the parentId path is unchanged
    g = adapter()
    g._apply(push(CHILD, "Anna Alpha", "Bea Beta", -150, +130, parent=PARENT), live=True)
    out = g._read_cache([P_HOME], time.time())
    r.append(check("parentId link still followed", out[P_HOME].live, True))
    r.append(check("...and not counted as a players redirect", (g._parent_redirects, g._name_redirects), (1, 0)))

    # 7. never redirect toward something that is not live
    h = adapter()
    h._apply(push(CHILD, "Anna Alpha", "Bea Beta", -150, +130), live=False)      # pushed on a /pre topic only
    out = h._read_cache([P_HOME], time.time())
    r.append(check("a non-live child is not followed", (out[P_HOME].live, out[P_HOME].selection_id), (False, P_HOME)))

    # 8. a parent with no names on record is never redirected by the players (unknown is safe, wrong is not)
    k = adapter()
    k._pair_names = {}
    k._apply(push(CHILD, "Anna Alpha", "Bea Beta", -150, +130), live=True)
    out = k._read_cache([P_HOME], time.time())
    r.append(check("no recorded names -> no players redirect", out[P_HOME].live, False))

    # 9. the cache-miss path gets the same guard (the parent was del'd)
    m = adapter(pair_start="2026-11-13T10:00:00Z")
    del m._cache[P_HOME]
    m._apply(push(CHILD, "Anna Alpha", "Bea Beta", -150, +130), live=True)
    out = m._read_cache([P_HOME], time.time())
    r.append(check("cache miss + a rematch 3 days off -> nothing served", P_HOME in out, False))

    n = sum(r)
    print(f"\n{n}/{len(r)} passed")
    return 0 if n == len(r) else 1


if __name__ == "__main__":
    sys.exit(main())
