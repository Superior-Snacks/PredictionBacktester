"""Basketball name folding in pair_pinnacle (2026-10-22): the club-affix strip and the aliases, on the real names
Kalshi and Pinnacle used that day (guest /leagues/{id}/matchups). Each Kalshi team, folded, must score a clean
100 against Pinnacle's name with the matcher's own _team_sim (containment or token subset), the YES side must
resolve to the right club, and nothing outside basketball may change.

    python test_pair_names.py          # exit 0 = all pass
"""
from __future__ import annotations
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.argv = sys.argv[:1]
import pair_pinnacle as P  # noqa: E402
from pair_auto import _norm, _book_name, _team_sim, _pick_book_team  # noqa: E402

PASS = FAIL = 0


def check(name, ok, got=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}" + (f"  (got {got})" if got != "" else ""))


print("[1] the affix strip")
check("prefix", P._bb_core("bc aek athens") == "aek athens")
check("suffix", P._bb_core("hapoel holon bc") == "hapoel holon")
check("suffix + number", P._bb_core("poitiers basket 86") == "poitiers")
check("both ends", P._bb_core("alm evreux basket") == "evreux")
check("never strips to nothing", P._bb_core("basket") == "basket" and P._bb_core("bc") == "bc")
check("a name with no affix is untouched", P._bb_core("real madrid") == "real madrid")

print("\n[2] the 2026-10-22 board: Kalshi team (as titled) -> Pinnacle name, a clean 100")
T = "KXEUROLEAGUEGAME-26OCT221445CZVKPB-CZV"
CASES = [  # (series, Kalshi team, Pinnacle team)
    ("KXEUROLEAGUEGAME", "Dubai Basketball", "BC Dubai"),
    ("KXEUROLEAGUEGAME", "KK Crvena zvezda Belgrade", "Crvena Zvezda"),
    ("KXEUROLEAGUEGAME", "KK Partizan Belgrade", "KK Partizan Belgrade"),
    ("KXGBLGAME", "BC Aris Thessaloniki", "Aris Thessaloniki BC"),
    ("KXGBLGAME", "AO Mykonoy", "AO Mykonos"),
    ("KXGBLGAME", "BC AEK Athens", "AEK Athens"),
    ("KXGBLGAME", "BC Olympiakos Piraeus", "Olympiakos BC"),
    ("KXGBLGAME", "Promitheas Patras BC", "Promitheas Patras"),
    ("KXISLGAME", "Hapoel Holon BC", "Hapoel Holon"),
    ("KXLNBELITE2GAME", "ALM Evreux Basket", "Evreux"),
    ("KXLNBELITE2GAME", "Stade Rochelais Basket", "Stade Rochelais"),
    ("KXLNBELITE2GAME", "Alliance Sport Alsace", "AS Alsace Gries-Souffel"),
    ("KXLNBELITE2GAME", "Hermine Nantes Basket", "Nantes Atlantique"),
    ("KXLNBELITE2GAME", "Poitiers Basket 86", "Poitiers"),
    ("KXLNBELITE2GAME", "Rouen Metropole Basket", "Rouen"),
    ("KXLNBELITE2GAME", "Aix Maurienne Savoie Basket", "Aix Maurienne Savoie"),
    ("KXLNBELITE2GAME", "JA Vichy-Clermont", "Vichy-Clermont"),
    ("KXNBBGAME", "SC Corinthians Paulista", "Corinthians Paulista"),
    ("KXVTBGAME", "BC Uniks Kazan", "UNICS Kazan"),
]
for series, k, b in CASES:
    folded = P._canon_team(_norm(k), f"{series}-X-Y")
    sim = _team_sim(folded, _book_name(b))
    check(f"{k!r} -> {b!r}", sim == 100, f"folded {folded!r}, score {sim}")

print("\n[3] the YES side resolves to the right club (the outcome is folded like its team)")
for series, k, b, other in [("KXGBLGAME", "BC AEK Athens", "AEK Athens", "PAOK BC"),
                            ("KXGBLGAME", "BC Olympiakos Piraeus", "Olympiakos BC", "Promitheas Patras"),
                            ("KXEUROLEAGUEGAME", "KK Crvena zvezda Belgrade", "Crvena Zvezda", "KK Partizan Belgrade"),
                            ("KXLNBELITE2GAME", "Alliance Sport Alsace", "AS Alsace Gries-Souffel", "Nantes Atlantique")]:
    pick = _pick_book_team(P._canon_team(_norm(k), f"{series}-X-Y"), {_book_name(b), _book_name(other)})
    check(f"YES {k!r} -> {b!r}", pick == _book_name(b), pick)

print("\n[4] nothing outside basketball changes")
check("tennis key untouched (no strip)", P._canon_team("bc smith", "KXATPMATCH-X-Y") == "bc smith")
check("NFL code expansion unchanged", P._canon_team("chi bears", "KXNFLGAME-X-Y") == "chicago bears")
check("MLB alias unchanged", P._canon_team("a s", "KXMLBGAME-X-Y") == "athletics")
check("hockey alias unchanged", P._canon_team("red bull munich", "KXDELGAME-X-Y") == "munchen")
check("a basketball ALIAS wins over the strip", P._canon_team("cb coruna", "KXACBGAME-X-Y") == "coruna")

print(f"\n{'=' * 58}\n  {PASS} passed, {FAIL} failed\n{'=' * 58}")
sys.exit(1 if FAIL else 0)
