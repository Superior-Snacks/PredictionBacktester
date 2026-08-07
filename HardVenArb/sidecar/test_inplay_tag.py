"""
test_inplay_tag.py — the IN-PLAY (`live`) tag must never be downgraded to pre-match.

Regression for the 2026-08-07 audit. `HardVenInPlay` in CrossArbTelemetry is fail-open: `live` is set True only
by a `.../live/*` WS topic, so ANY path that loses the tag silently refiles an in-play window as PRE-LIVE — the
favourable regime (~1s placement in the analyzer, and past the executor's pre-live-only gate). Two leaks were
found in `_apply`, both fixed here:

  1. the reconcile/suspend branch rebuilt the Selection without `live`/`cutoff` (in-play tennis suspends between
     points, so live tokens pass through it constantly);
  2. the final `cache.update()` let a `/pre`-topic push overwrite a token the `/live/*` topic had flagged.

Only `del` (game over) may clear the tag. Exercises the REAL `_apply` / `_apply_straight_markets`.

    python test_inplay_tag.py            # 6/6 expected
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pinnacle_adapter import PinnacleAdapter

LID, MID = "9417", "1633450401"
TOKEN = f"{LID}:{MID}:home"


def rec(price_home=-120, price_away=+110, cutoff_in=3600.0):
    """One matchup push with an open period-0 moneyline, cutoff safely in the future."""
    cut = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + cutoff_in))
    return {"op": "upd", "rec": {
        "id": int(MID), "league": {"id": int(LID)},
        "markets": [{"type": "moneyline", "period": 0, "status": "open", "cutoffAt": cut,
                     "limits": [{"amount": 500}],
                     "prices": [{"designation": "home", "price": price_home},
                                {"designation": "away", "price": price_away}]}]}}


def marketless():
    """A score/clock heartbeat: no markets -> ambiguous, must not reconcile."""
    return {"op": "upd", "rec": {"id": int(MID), "league": {"id": int(LID)}, "markets": []}}


def pulled():
    """The moneyline is gone from the push -> reconcile marks the cached token suspended."""
    return {"op": "upd", "rec": {"id": int(MID), "league": {"id": int(LID)},
                                 "markets": [{"type": "total", "period": 0, "status": "open",
                                              "cutoffAt": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                                                        time.gmtime(time.time() + 3600)),
                                              "limits": [{"amount": 500}],
                                              "prices": [{"designation": "over", "points": 21.5, "price": -110},
                                                         {"designation": "under", "points": 21.5, "price": -110}]}]}}


def straight_snapshot():
    """A REST /markets/straight row — the pre-match-blind seed path."""
    return [{"matchupId": int(MID), "type": "moneyline", "period": 0, "status": "open",
             "cutoffAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 3600)),
             "limits": [{"amount": 500}],
             "prices": [{"designation": "home", "price": -125},
                        {"designation": "away", "price": +115}]}]


def check(name, got, want):
    ok = got == want
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}: live={got} (expected {want})")
    return ok


def main() -> int:
    results = []

    # 1. a /live/* push tags the token in-play
    a = PinnacleAdapter()
    a._apply(rec(), live=True)
    results.append(check("live push tags in-play", a._cache[TOKEN].live, True))

    # 2. THE BUG: a /pre push for the same matchup must not downgrade it
    a._apply(rec(price_home=-130), live=False)
    results.append(check("/pre push does NOT downgrade", a._cache[TOKEN].live, True))

    # 3. THE OTHER BUG: suspend/reconcile must carry the tag through
    a._apply(pulled(), live=False)
    assert a._cache[TOKEN].status == "suspended", "precondition: token should be suspended"
    results.append(check("suspend keeps the tag", a._cache[TOKEN].live, True))

    # 4. ...and a REST re-seed after that suspension must still see it as live
    a._apply_straight_markets(LID, straight_snapshot(), time.time())
    results.append(check("REST re-seed after suspend keeps it", a._cache[TOKEN].live, True))

    # 5. a marketless heartbeat is ambiguous — must not reconcile or downgrade
    a._apply(marketless(), live=False)
    results.append(check("marketless heartbeat is inert", a._cache[TOKEN].live, True))

    # 6. a genuinely pre-match token stays pre-match (the guard must not tag everything live)
    b = PinnacleAdapter()
    b._apply(rec(), live=False)
    b._apply(rec(price_home=-130), live=False)
    results.append(check("pre-match stays pre-match", b._cache[TOKEN].live, False))

    # `del` (game over) is the only thing that clears the tag — it drops the token entirely.
    a._apply({"op": "del", "rec": {"id": int(MID), "league": {"id": int(LID)}}})
    gone = TOKEN not in a._cache
    print(f"  [{'PASS' if gone else 'FAIL'}] del clears the matchup: cached={not gone} (expected False)")
    results.append(gone)

    n = sum(results)
    print(f"\n{n}/{len(results)} passed")
    return 0 if n == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
