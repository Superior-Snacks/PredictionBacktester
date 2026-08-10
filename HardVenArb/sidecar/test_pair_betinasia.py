"""
Tests for the BetInAsia pairer's name matching.

THE BUG THIS PINS (found 2026-08-10). `_name_score` returned 95 for ANY surname equality, so
'Alexander Zverev' matched 'Mischa Zverev' and 'Andy Murray' matched 'Jamie Murray' -- different
people, both comfortably above the default --threshold of 90. That is not a near miss: the two legs
would back OPPOSITE players, turning the "hedge" into a doubled directional bet that loses on both
branches. Tennis is unusually exposed to it (siblings, plus common surnames throughout the Challenger
tour, which is where most of the Kalshi tennis tickers live).

The fix must fail closed WITHOUT breaking the variation we actually have to survive: initials
('T. Tirante' == 'Thiago Agustin Tirante'), surname-only listings, and accents.

Run: python test_pair_betinasia.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pair_betinasia as P

PASS = 0
FAIL = 0
THRESHOLD = 90.0     # the pairer's default


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {detail}")


print("\n[1] same surname, DIFFERENT person -> must not pair")
for a, b in [("Alexander Zverev", "Mischa Zverev"),
             ("Andy Murray", "Jamie Murray"),
             ("Serena Williams", "Venus Williams"),
             ("Bob Bryan", "Mike Bryan")]:
    s = P._name_score(a, b)
    check(f"{a} != {b}  (score {s:.0f})", s < THRESHOLD, f"scored {s} >= {THRESHOLD}")

print("\n[2] same person, different spelling -> must still pair")
for a, b in [("T. Tirante", "Thiago Agustin Tirante"),
             ("Thiago Agustin Tirante", "T. Tirante"),
             ("Tallon Griekspoor", "Griekspoor"),
             ("Griekspoor", "Tallon Griekspoor"),
             ("Jaume Munar", "Jaume Muñar"),
             ("Alexander Zverev", "A. Zverev"),
             ("Learner Tien", "learner  tien")]:
    s = P._name_score(a, b)
    check(f"{a} == {b}  (score {s:.0f})", s >= THRESHOLD, f"scored {s} < {THRESHOLD}")

print("\n[2b] annotated names — both venues decorate them, both broke matching")
# Kalshi disambiguates duplicates; BetInAsia adds a status marker. The square-bracket case is worse:
# 'f' lands as the LAST token so _surname() returned "f" and compared the wrong thing entirely.
for a, b in [("Cezar Cretu (b. 2001)", "Cezar Cretu"),
             ("Ekaterina Alexandrova [f]", "Ekaterina Alexandrova"),
             ("Alexandrova", "Ekaterina Alexandrova [f]"),
             ("Elina Svitolina", "Svitolina")]:
    s = P._name_score(a, b)
    check(f"{a} == {b}  (score {s:.0f})", s >= THRESHOLD, f"scored {s} < {THRESHOLD}")
check("bracket marker no longer hijacks the surname",
      P._surname("Ekaterina Alexandrova [f]") == "alexandrova",
      P._surname("Ekaterina Alexandrova [f]"))
# ...and stripping annotations must not resurrect the sibling hole
check("annotated sibling still rejected",
      P._name_score("Alexander Zverev [f]", "Mischa Zverev") < THRESHOLD)

print("\n[2c] compound surnames — only ONE venue spells the second surname")
# Real ties lost to this, found by reading the unpaired lists by hand. `_surname()` takes the LAST
# token, so a Spanish double surname makes the two venues disagree on the surname entirely.
for a, b in [("Murkel Dellien", "Murkel Alejandro Dellien Velasco"),
             ("Daniel Elahi Galan", "Daniel Elahi Galan Riveros"),
             ("Daniel Merida", "Daniel Merida Aguilar"),
             ("Learner Tien", "Learner Tien"),
             ("Juan Martin", "Juan Martin Del Potro")]:
    s = P._name_score(a, b)
    check(f"{a} == {b}  (score {s:.0f})", s >= THRESHOLD, f"scored {s} < {THRESHOLD}")
# ...and the loosening must not start matching merely-similar surnames
for a, b in [("Juan Martin", "Juan Martinez"),
             ("Li Na", "Li Wang"),
             ("Andres Martin", "Andres Molteni")]:
    s = P._name_score(a, b)
    check(f"{a} != {b}  (score {s:.0f})", s < THRESHOLD, f"scored {s} >= {THRESHOLD}")

print("\n[3] unrelated players")
for a, b in [("Learner Tien", "Thiago Agustin Tirante"),
             ("Andre Ilagan", "Andres Martin"),
             ("Jakub Mensik", "Botic Van de Zandschulp")]:
    s = P._name_score(a, b)
    check(f"{a} != {b}  (score {s:.0f})", s < THRESHOLD, f"scored {s} >= {THRESHOLD}")
# a shared FIRST name must not carry a pair on fuzzy alone
s = P._name_score("Andres Martin", "Andres Molteni")
check(f"shared first name is not enough (score {s:.0f})", s < THRESHOLD)

print("\n[4] given-name compatibility helper")
check("initial matches full name", P._given_compatible(["t"], ["thiago", "agustin"]))
check("full names equal", P._given_compatible(["andy"], ["andy"]))
check("conflicting given names rejected", not P._given_compatible(["alexander"], ["mischa"]))
check("empty side is permissive", P._given_compatible([], ["tallon"]))
check("wrong initial rejected", not P._given_compatible(["m"], ["alexander"]))
# COMPOUND SURNAMES: _surname() keeps only the last token, so the rest of the compound lands in the
# given list and lines up at a different POSITION on each side. These are real pairs from the tape
# that a positional comparison threw away.
check("compound surname, offset position", P._given_compatible(["jorda"], ["david", "jorda"]))
check("compound surname (Alcala Gurri)", P._given_compatible(["alcala"], ["max", "alcala"]))
check("apostrophe surname (D'Agostino)", P._given_compatible(["d"], ["stefano", "d"]))
check("still rejects siblings after the loosening",
      not P._given_compatible(["alexander"], ["mischa"]))

print("\n[4b] the three real pairs a positional comparison lost")
for a, b in [("Jorda Sanchis", "David Jorda Sanchis"),
             ("Alcala Gurri", "Max Alcala Gurri"),
             ("D`Agostino", "Stefano D'Agostino"),
             ("Kravchenko", "Georgii Kravchenko"),
             ("Pinnington Jones", "Jack Pinnington Jones")]:
    s = P._name_score(a, b)
    check(f"{a} == {b}  (score {s:.0f})", s >= THRESHOLD, f"scored {s} < {THRESHOLD}")

print("\n[5] end-to-end: a sibling tie must not pair to the wrong brother")
games = {
    "2026-08-10,111,222": {
        "sport": "tennis", "league": "ATP", "start": "2026-08-10T12:00:00Z", "three_way": False,
        "players": {"p1": ("Mischa Zverev", "tennis:1:2026-08-10,111,222:tennis_match,all:p1"),
                    "p2": ("Andres Martin", "tennis:1:2026-08-10,111,222:tennis_match,all:p2")},
    }
}
entry = {"kalshi_ticker": "KXATPMATCH-26AUG10ZVEMAR-ZVE",
         "event_title": "Zverev vs Martin", "kalshi_outcome": "Alexander Zverev",
         "settlement_date": "2026-08-10"}
hit = P._match_game(entry, games, {}, THRESHOLD)
# "Zverev" alone is surname-only and legitimately matches Mischa; the ORIENTATION step is the
# safety net -- kalshi_outcome is the full "Alexander Zverev", which must not resolve to Mischa.
if hit:
    yes = entry["kalshi_outcome"]
    scored = sorted(((P._name_score(yes, nm), tok) for tok, (nm, _s) in hit[1]["players"].items()),
                    reverse=True)
    check("orientation refuses the wrong Zverev", scored[0][0] < THRESHOLD,
          f"best side scored {scored[0][0]}")
else:
    check("orientation refuses the wrong Zverev", True)

print("\n[6] date guard")
check("same day passes", P._date_close("2026-08-10", "2026-08-10T12:00:00Z"))
check("+1 day tolerated (venue TZ slop)", P._date_close("2026-08-10", "2026-08-11T01:00:00Z"))
check("3 days apart rejected", not P._date_close("2026-08-10", "2026-08-13T12:00:00Z"))
check("unparseable does not block", P._date_close("", "garbage"))

print("\n[7] event-key helpers")
check("player ids parsed", P._event_players("2026-08-09,10047664,90384") == ("10047664", "90384"))
check("outright yields no players", P._event_players("2026-08-01,multirunner,100447481") == ("", ""))
check("date parsed", P._event_date("2026-08-09,1,2") == "2026-08-09")

print(f"\n{'='*58}\n  {PASS} passed, {FAIL} failed\n{'='*58}")
sys.exit(1 if FAIL else 0)
