"""
Offline tests for the BetInAsia feed + adapter.

No network, no account. Two sources of truth:
  1. Synthetic frames for the edge cases we must get right (withdrawal, batching, id round-trip).
  2. REPLAY of the real recon capture (betinasia_recon_20260805_*.jsonl) when it is present, so the
     parser is checked against frames the site actually sent rather than against my reading of them.

CAVEAT ON THE REPLAY CORPUS: the recon writer truncates every frame at MAX_FRAME_CHARS=4000, so only
~58% of captured frames are valid JSON and the long `event` (catalog) frames are almost all cut off.
The replay therefore exercises the ODDS path well and the CATALOG path barely. Raise MAX_FRAME_CHARS
before the next recon run.

Run: python test_betinasia.py
"""
from __future__ import annotations

import glob
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from betinasia_ws import BetInAsiaFeed, iter_messages
from betinasia_adapter import (
    is_moneyline, is_three_way, make_selection_id, parse_selection_id,
    make_bet_type, BET_TYPE_INFIX,
    _sides, _selection_name, _start_ts_epoch,
)

PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {detail}")


def feed() -> BetInAsiaFeed:
    return BetInAsiaFeed(username="x", password="y", on_log=lambda m: None)


# ── 1. envelope normalisation ─────────────────────────────────────────────────
print("\n[1] envelope normalisation")
check("batch of messages", len(iter_messages([["ok"], ["pong", 1]])) == 2)
check("bare single message", len(iter_messages(["offers_hcap", [1, "tennis", "k"], {}])) == 1)
check("empty frame", iter_messages([]) == [])
check("non-list frame", iter_messages({"a": 1}) == [])
check("bare message keeps shape",
      iter_messages(["error", "event_already_subscribed"])[0][0] == "error")

# ── 2. offers parsing ─────────────────────────────────────────────────────────
print("\n[2] offers_hcap parsing")
f = feed()
f.handle_frame([["offers_hcap", [338, "tennis", "2026-08-05,73551,87843"], {
    "tennis_match,all": [None, [["p1", 1.80], ["p2", 2.05]]],
    "tennis_ah,all,game": [16, [["p1", 1.794], ["p2", 2.13]]],
}]])
got = f.get_market("tennis", "2026-08-05,73551,87843", "tennis_match,all")
check("moneyline cached", got is not None and got[1]["p1"] == 1.80)
check("moneyline line is None", got is not None and got[0] is None)
check("handicap line preserved (quarter-units)",
      f.get_market("tennis", "2026-08-05,73551,87843", "tennis_ah,all,game")[0] == 16)
check("comp_id captured", f._books[("tennis", "2026-08-05,73551,87843")]["comp_id"] == 338)

# a null market value means WITHDRAWN -> must be removed, not held stale
f.handle_frame([["offers_hcap", [338, "tennis", "2026-08-05,73551,87843"],
                 {"tennis_match,all": None}]])
check("null market is withdrawn (not stale)",
      f.get_market("tennis", "2026-08-05,73551,87843", "tennis_match,all") is None)
check("sibling market survives withdrawal",
      f.get_market("tennis", "2026-08-05,73551,87843", "tennis_ah,all,game") is not None)

f2 = feed()
f2.handle_frame([["offers_hcap", [1, "fb", "k"], {"m": [None, []]}]])
check("empty selection list is not cached", f2.get_market("fb", "k", "m") is None)
f2.handle_frame([["offers_hcap", [1, "fb", "k"], {"m": [None, [["h", "notanumber"], ["a", 2.0]]]}]])
got2 = f2.get_market("fb", "k", "m")
check("non-numeric odds dropped, good ones kept",
      got2 is not None and "h" not in got2[1] and got2[1]["a"] == 2.0)

# The real feed emits 0.0 for listed-but-unavailable markets (tennis_game_win in the capture).
# 0.0 is not a price; it must be rejected at ingest so catalog() never publishes an unpriceable leg.
f2.handle_frame([["offers_hcap", [1, "fb", "z"], {"m": [None, [["h", 0.0], ["a", 1.0]]]}]])
check("zero/unit odds rejected at ingest", f2.get_market("fb", "z", "m") is None)
f2.handle_frame([["offers_hcap", [1, "fb", "z2"], {"m": [None, [["h", 0.0], ["a", 2.5]]]}]])
g = f2.get_market("fb", "z2", "m")
check("partial: unpriced side dropped, priced side kept",
      g is not None and "h" not in g[1] and g[1]["a"] == 2.5)

# correct-score markets carry a LIST line ([2,0]) - must not crash
f2.handle_frame([["offers_hcap", [1, "tennis", "k2"], {"tennis_cs,all,set": [[2, 0], [["", 4.5]]]}]])
check("list-valued line tolerated", f2.get_market("tennis", "k2", "tennis_cs,all,set")[0] == [2, 0])

# ── 3. event frames / in-play ─────────────────────────────────────────────────
print("\n[3] event frames + in-play flag")
f3 = feed()
f3.handle_frame([["event", ["fb", "2026-08-05,1,2"], {
    "event_type": "normal", "start_ts": "2026-08-05T18:30:00Z",
    "competition_name": "Club Friendly", "home": "CD Cieza", "away": "Real Murcia CF",
    "event_name": "CD Cieza vs. Real Murcia CF",
    "ir_status": {"time": ["2h", 21], "score": [0, 4], "rc": [0, 0]},
}]])
ev = f3.get_event("fb", "2026-08-05,1,2")
check("event cached", ev is not None)
check("ir_status present => in-play", bool(ev.get("ir_status")))
check("sides from home/away", _sides(ev) == ("CD Cieza", "Real Murcia CF"))

pre = {"start_ts": "2026-08-05T18:30:00Z", "teams": [{"team_id": 1, "name": "A"},
                                                     {"team_id": 2, "name": "B"}]}
check("sides from teams[] (multirunner shape)", _sides(pre) == ("A", "B"))
check("no ir_status => pre-match", not pre.get("ir_status"))
check("start_ts parsed to epoch", _start_ts_epoch("2026-08-05T18:30:00Z") > 1_700_000_000)
check("missing start_ts = 0 (unknown)", _start_ts_epoch(None) == 0.0)
check("garbage start_ts = 0 (unknown)", _start_ts_epoch("not-a-date") == 0.0)

# ── 4. market taxonomy ────────────────────────────────────────────────────────
print("\n[4] market taxonomy")
for k in ("tennis_match,all", "ml", "time_win,tp,all,ml"):
    check(f"moneyline: {k}", is_moneyline(k))
for k in ("wdw", "time_win,tp,reg,wdw"):
    check(f"three-way: {k}", is_three_way(k))
for k in ("tennis_ah,all,game", "tennis_ahou,all,set", "tennis_cs,all,set", "tennis_match,1",
          "tennis_match,2", "time_ah,tp,all", "tahou,a", "dc", "gr", "tennis_game_win,1,5"):
    check(f"derivative: {k}", not is_moneyline(k) and not is_three_way(k))
# regulation-only is NOT the same market as Kalshi's final result
check("time_win,tp,reg,ml excluded (regulation != final)", not is_moneyline("time_win,tp,reg,ml"))

# ── 5. selection ids ──────────────────────────────────────────────────────────
print("\n[5] selection id round-trip")
sid = make_selection_id("tennis", 338, "2026-08-05,73551,87843", "tennis_match,all", "p1")
check("id shape", sid == "tennis:338:2026-08-05,73551,87843:tennis_match,all:p1")
check("round-trips", parse_selection_id(sid) ==
      ("tennis", "338", "2026-08-05,73551,87843", "tennis_match,all", "p1"))
check("commas in event+market keys survive", parse_selection_id(sid)[2].count(",") == 2)
check("multirunner key round-trips",
      parse_selection_id(make_selection_id("fb", 209, "2026-01-28,multirunner,100340373", "ml", "h"))
      [2] == "2026-01-28,multirunner,100340373")
check("empty selection (correct-score) round-trips",
      parse_selection_id(make_selection_id("tennis", 1, "k", "tennis_cs,all,set", ""))[4] == "")
check("malformed id rejected", parse_selection_id("nope") is None)
check("selection name maps to team", _selection_name("p1", "Alice", "Bob") == "Alice")
check("away token maps to away", _selection_name("a", "Home FC", "Away FC") == "Away FC")
check("unknown token falls back to raw", _selection_name("over", "H", "A") == "over")

# ── 5b. feed market_key -> order bet_type ─────────────────────────────────────
# All four cases are VERBATIM from real POST /v1/betslips/ requests (2026-08-09 captures).
print("\n[5b] bet_type mapping (observed slips)")
check("tennis moneyline",
      make_bet_type("tennis_match,all", "p2") == "for,tset,all,vwhatever,p2")
check("basket moneyline", make_bet_type("ml", "h") == "for,ml,h")
check("baseball moneyline", make_bet_type("time_win,tp,all,ml", "a") == "for,tp,all,ml,a")
check("soccer 1X2 has NO infix", make_bet_type("wdw", "h") == "for,h")
check("soccer draw follows the same shape", make_bet_type("wdw", "d") == "for,d")
# Guessing a bet_type is worse than refusing: a wrong one is either rejected or silently accepted
# as a DIFFERENT market than the one we priced.
check("unobserved market yields None", make_bet_type("tennis_ah,all,game", "p1") is None)
check("regulation-only variant yields None", make_bet_type("time_win,tp,reg,ml", "a") is None)
check("every moneyline/3-way key is mappable",
      all(k in BET_TYPE_INFIX for k in ("tennis_match,all", "ml", "time_win,tp,all,ml", "wdw")))

# ── 6. replay the real recon capture ──────────────────────────────────────────
print("\n[6] replay of real recon frames")
files = sorted(glob.glob(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                      "betinasia_recon_*.jsonl")))
if not files:
    print("  SKIP  no betinasia_recon_*.jsonl present")
else:
    rf = feed()
    total = parsed = 0
    for path in files:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("kind") != "ws_frame" or rec.get("dir") != "in":
                    continue
                total += 1
                body = rec.get("body")
                if isinstance(body, str):
                    try:
                        body = json.loads(body)      # truncated frames fail here, as expected
                    except json.JSONDecodeError:
                        continue
                parsed += 1
                rf.handle_frame(body)               # must never raise on real data

    st = rf.stats()
    print(f"  replayed {parsed}/{total} in-frames ({100*parsed//max(total,1)}% -- "
          f"rest truncated at MAX_FRAME_CHARS)")
    check("replay produced books", st["books"] > 0, str(st))
    check("no crash on real frames", True)

    ml_found = {}
    for (sport, ekey), book in rf._books.items():
        for mk in book["markets"]:
            if is_moneyline(mk):
                ml_found.setdefault(sport, 0)
                ml_found[sport] += 1
    check("moneylines recognised in real data", len(ml_found) >= 3, str(ml_found))
    print(f"  moneyline markets by sport: {ml_found}")

    # every cached price must be a plausible decimal odd
    bad = [(s, e, mk, sel, o)
           for (s, e), b in rf._books.items()
           for mk, (_l, sels) in b["markets"].items()
           for sel, o in sels.items() if not (1.0 < o < 1000.0)]
    check("all cached odds are plausible decimals", not bad, str(bad[:3]))

    # ids built from real data must round-trip
    broke = []
    for (sport, ekey), b in rf._books.items():
        for mk, (_l, sels) in b["markets"].items():
            for sel in sels:
                s = make_selection_id(sport, b["comp_id"], ekey, mk, sel)
                if parse_selection_id(s) != (sport, str(b["comp_id"]), ekey, mk, sel):
                    broke.append(s)
    check("all real-data ids round-trip", not broke, f"{len(broke)} broke e.g. {broke[:2]}")

print(f"\n{'='*58}\n  {PASS} passed, {FAIL} failed\n{'='*58}")
sys.exit(1 if FAIL else 0)
