"""Side resolution — the check that would have stopped 2026-08-19.

Two live fires bought Kalshi-NO on Reyniak alongside Pinnacle-Izquierdo: both legs on the SAME outcome,
booked as two locked arbs. The cause was `_pick_book_team` answering with the first key that matched any
tier, so an ambiguous two-player field produced a confident wrong answer and the yes/no tokens came back
inverted. Nothing downstream could see it — each row is individually plausible, only the PAIR is wrong.

So this pins two properties:
  1. the resolver picks the right player, and REFUSES when the field genuinely cannot say
  2. the two markets of one event always land on DIFFERENT sides of the SAME matchup, or neither is written
"""
import sys
sys.path.insert(0, ".")
from pair_auto import _pick_book_team as pick

PASS = FAIL = 0
def check(name, cond, got=""):
    global PASS, FAIL
    if cond: PASS += 1; print(f"  PASS  {name}")
    else:    FAIL += 1; print(f"  FAIL  {name}" + (f"  (got {got})" if got else ""))

print("[1] resolves the obvious cases")
check("exact name",            pick("harold mayot", {"max purcell", "harold mayot"}) == "harold mayot")
check("surname only",          pick("rinky hijikata", {"hijikata", "lehecka"}) == "hijikata")
check("city -> full team",     pick("boston", {"boston red sox", "new york mets"}) == "boston red sox")
check("word order",            pick("congo dr", {"dr congo", "angola"}) == "dr congo")
check("exact beats near-miss", pick("john smith", {"john smith", "john smyth"}) == "john smith")

print("\n[2] the shapes that used to invert")
# A shared GIVEN name scored as highly as a full containment, so the field looked like a tie and the
# first-listed key won — which is how two markets of one event both landed on ':home'.
check("shared first name does not beat containment",
      pick("maria fernanda lopes", {"maria sousa salazar", "fernanda lopes"}) == "fernanda lopes",
      pick("maria fernanda lopes", {"maria sousa salazar", "fernanda lopes"}))
check("the live failure resolves correctly",
      pick("matias reyniak", {"rafael izquierdo luque", "matias reyniak"}) == "matias reyniak")
check("its sibling resolves the other way",
      pick("rafael izquierdo luque", {"rafael izquierdo luque", "matias reyniak"}) == "rafael izquierdo luque")

print("\n[3] refuses rather than guessing")
# A refused pair costs one market for a day. An inverted one costs the stake and looks like a win until
# it settles, so a genuine tie must return None.
check("same surname, no distinguishing token", pick("m garcia", {"maria garcia", "manuel garcia"}) is None,
      pick("m garcia", {"maria garcia", "manuel garcia"}))
check("matches neither side",  pick("someone else", {"alpha player", "beta player"}) is None)
check("empty field",           pick("anyone", set()) is None)

print("\n[4] MIRROR PROPERTY — the invariant that actually protects the money")
for a, b in [("harold mayot", "max purcell"),
             ("matias reyniak", "rafael izquierdo luque"),
             ("maria fernanda lopes", "natalia sousa salazar"),
             ("rodrigo alujas", "martin antonio vergara del puerto")]:
    ka, kb = pick(a, {a, b}), pick(b, {a, b})
    check(f"{a[:26]!r} / {b[:26]!r} take different sides",
          ka is not None and kb is not None and ka != kb, f"{ka} / {kb}")

print(f"\n{'='*58}\n  {PASS} passed, {FAIL} failed\n{'='*58}")
sys.exit(1 if FAIL else 0)
