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

print("\n[5] the 2026-11-10 US college board (guest league 493): Kalshi school -> Pinnacle name, a clean 100")
NCAA = "KXNCAAMBGAME-26NOV10XXXYYY-XXX"
for k, b in [("Detroit Mercy", "Detroit"), ("Queens University", "Queens"), ("Ole Miss", "Mississippi"),
             ("Southeastern Louisiana", "SE Louisiana"), ("Southern University", "Southern"),
             ("Louisiana-Monroe", "UL - Monroe"), ("Maryland-Eastern Shore", "MD Eastern Shore"),
             ("UCF", "Central Florida"),
             # already fine by containment - must stay fine
             ("Florida St.", "Florida State"), ("Middle Tennessee", "Middle Tennessee State"),
             ("Kennesaw St.", "Kennesaw St"), ("Little Rock", "Arkansas Little Rock")]:
    folded = P._canon_team(_norm(k), NCAA)
    sim = _team_sim(folded, _book_name(b))
    check(f"{k!r} -> {b!r}", sim == 100, f"folded {folded!r}, score {sim}")

print("\n[6] one school inside the other: the EXACT name decides (basketball only)")
fl = [_book_name("Florida"), _book_name("Florida State")]
az = [_book_name("Northern Arizona"), _book_name("Arizona")]
check("the plain matcher refuses Florida vs Florida State (it is too close to call)", _pick_book_team("florida", fl) is None)
check("YES 'florida' -> Florida", P._pick_yes_team("florida", fl, NCAA) == "florida")
check("YES 'arizona' -> Arizona, not Northern Arizona", P._pick_yes_team("arizona", az, NCAA) == "arizona")
check("YES 'northern arizona' -> Northern Arizona", P._pick_yes_team("northern arizona", az, NCAA) == "northern arizona")
check("no exact name ('florida st') is still refused, not guessed", P._pick_yes_team("florida st", fl, NCAA) is None)
check("outside basketball the plain matcher's answer stands",
      P._pick_yes_team("florida", fl, "KXNFLGAME-X-Y") == _pick_book_team("florida", fl))
check("a clear case is unchanged", P._pick_yes_team("evansville", [_book_name("Evansville"),
      _book_name("Middle Tennessee State")], NCAA) == "evansville")

print("\n[7] the 2026-11-26/27 college boards: Kalshi school -> Pinnacle name, a clean 100")
for k, b in [("UTSA", "Texas San Antonio"), ("Saint Mary's", "Saint Marys CA"), ("UNC Wilmington", "NC Wilmington"),
             ("Penn", "Pennsylvania"), ("Oakland", "Oakland"),
             # aliased before Pinnacle listed them: BOTH plausible spellings must pair
             ("UNC Asheville", "NC Asheville"), ("UNC Asheville", "UNC Asheville"),
             ("UNC Greensboro", "NC Greensboro"), ("UNC Greensboro", "UNC Greensboro"),
             ("UMass Lowell", "Massachusetts Lowell"), ("UMass Lowell", "UMass Lowell"),
             ("Grambling St.", "Grambling State"), ("Grambling St.", "Grambling"),
             ("Central Connecticut St.", "Central Connecticut State"), ("Central Connecticut St.", "Central Connecticut"),
             ("USC Upstate", "South Carolina Upstate"), ("USC Upstate", "USC Upstate"),
             ("Purdue Fort Wayne", "Purdue Fort Wayne"), ("Purdue Fort Wayne", "Fort Wayne"),
             ("Texas A&M-Corpus Christi", "Texas A&M-Corpus Christi"), ("Texas A&M-Corpus Christi", "Texas A&M Corpus Christi"),
             ("Saint Joseph's", "Saint Josephs"), ("Saint Joseph's", "St. Joseph's"), ("Saint Joseph's", "Saint Joseph's"),
             # 11-27 15:56, caught by the unpaired-board log: Pinnacle's spelling and the long one
             ("UMBC", "MD Baltimore County"), ("UMBC", "Maryland Baltimore County"),
             ("East Tennessee St.", "East Tenn State"), ("East Tennessee St.", "East Tennessee State")]:
    folded = P._canon_team(_norm(k), NCAA)
    sim = _team_sim(folded, _book_name(b))
    check(f"{k!r} -> {b!r}", sim == 100, f"folded {folded!r}, score {sim}")
for k, b in [("Penn", "Penn State"), ("Saint Mary's", "Mount St. Mary's"), ("UTSA", "Texas"),
             ("UNC Wilmington", "North Carolina"), ("UMBC", "Maryland"), ("East Tennessee St.", "Tennessee"),
             ("East Tennessee St.", "Tennessee State")]:
    sim = _team_sim(P._canon_team(_norm(k), NCAA), _book_name(b))
    check(f"{k!r} is NOT {b!r}", sim < 100, f"score {sim}")
check("college 'oakland' stays oakland (the MLB alias skipped)", P._canon_team("oakland", NCAA) == "oakland")
check("NBA 'sacramento' stays sacramento", P._canon_team("sacramento", "KXNBAGAME-X-Y") == "sacramento")
check("MLB 'oakland' is still the Athletics", P._canon_team("oakland", "KXMLBGAME-X-Y") == "athletics")
check("MLB 'sacramento' is still the Athletics", P._canon_team("sacramento", "KXMLBGAME-X-Y") == "athletics")
check("YES 'pennsylvania' -> Pennsylvania, not Towson",
      P._pick_yes_team(P._canon_team("penn", NCAA), [_book_name("Pennsylvania"), _book_name("Towson")], NCAA)
      == "pennsylvania")

print("\n[8] the orientation audit reads a basketball alias (UCF/Penn were cleared 2026-11-27)")
agree = P._names_agree
check("'UCF' agrees with Central Florida", agree(NCAA, "UCF", "Central Florida"))
check("'UCF' does NOT agree with Georgia", not agree(NCAA, "UCF", "Georgia"))
check("'Penn' agrees with Pennsylvania", agree(NCAA, "Penn", "Pennsylvania"))
check("'Penn' does NOT agree with Towson", not agree(NCAA, "Penn", "Towson"))
check("'UTSA' agrees with Texas San Antonio", agree(NCAA, "UTSA", "Texas San Antonio"))
check("'UTSA' does NOT agree with Texas (the WHOLE target, not a shared token)", not agree(NCAA, "UTSA", "Texas"))
check("'UMass Lowell' agrees with Massachusetts Lowell", agree(NCAA, "UMass Lowell", "Massachusetts Lowell"))
check("'UMBC' agrees with MD Baltimore County", agree(NCAA, "UMBC", "MD Baltimore County"))
check("'UMBC' does NOT agree with George Washington", not agree(NCAA, "UMBC", "George Washington"))
check("YES 'umbc' -> MD Baltimore County, not George Washington",
      P._pick_yes_team(P._canon_team("umbc", NCAA), [_book_name("MD Baltimore County"), _book_name("George Washington")],
                       NCAA) == "md baltimore county")
check("a token match still agrees ('Oakland' / Oakland)", agree(NCAA, "Oakland", "Oakland"))
check("no alias, no shared token -> disagrees ('Towson' / Pennsylvania)", not agree(NCAA, "Towson", "Pennsylvania"))
check("outside basketball an alias does not count ('UCF', tennis)", not agree("KXATPMATCH-X-Y", "UCF", "Central Florida"))
check("outside basketball the token test is unchanged", agree("KXATPMATCH-X-Y", "Jasmin Ortenzi", "Jazmin Ortenzi")
      is bool(P._name_tok3("Jasmin Ortenzi") & P._name_tok3("Jazmin Ortenzi")))
check("an MLB-only alias is no evidence in basketball ('A's' / Athletics)", not agree(NCAA, "A's", "Athletics"))

print(f"\n{'=' * 58}\n  {PASS} passed, {FAIL} failed\n{'=' * 58}")
sys.exit(1 if FAIL else 0)
