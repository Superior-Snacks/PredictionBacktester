"""Betslip panel parsing: the selection label and the depth ladder.

Fixtures are VERBATIM panel text captured from the live venue on 2026-08-14 via slip_probe.ps1 across
three sports. Run as `python test_slip_ladder.py` (standalone, no pytest needed).
"""
import sys

sys.path.insert(0, __file__.rsplit("\\", 1)[0] if "\\" in __file__ else ".")

from betinasia_adapter import BetInAsiaAdapter as A

BASEBALL = ("Baseball Start Acca Cleveland Guardians Moneyline (Inc. Overtime) Stake $ Price Place $5 "
            "$10 $15 Stake At Price $9,852 Ex. Returns - 4casters BEST PRICE 1.776 $9,852 bf TOTAL "
            "$9,881 1.769 $29 sxbet TOTAL $10,388 1.769 $507 pin88 TOTAL $15,263 1.769 $4,876 bdaq "
            "TOTAL $15,364 1.763 $100 mbook TOTAL $15,386 1.763 $22 4casters TOTAL $15,781 1.763 $395 "
            "4casters TOTAL $17,689 1.761 $1,908 bdaq TOTAL $17,714 1.757 $26 bf TOTAL $18,592 1.757 "
            "$877 mbook TOTAL $19,072 1.754 $480 bdaq TOTAL $19,117 1.752 $45 bf TOTAL $23,056 1.746 "
            "$3,939 3et TOTAL $25,434 1.746 $2,377 mbook TOTAL $25,569 1.744 $136 polymarket TOTAL "
            "$80,953 1.742 $55,383 polymarket TOTAL $129,281 1.712 $48,328 polymarket TOTAL $185,782 "
            "1.683 $56,501")

TENNIS = ("Tennis Start Acca Rei Sakamoto Stake $ Price Place $5 $10 $15 Stake At Price $125 Ex. "
          "Returns - bf BEST PRICE 1.451 $125 schnitzel TOTAL $474 1.449 $350 bf TOTAL $639 1.440 $164 "
          "bdaq TOTAL $865 1.437 $226 sxbet TOTAL $999 1.433 $134 sharp TOTAL $1,340 1.431 $341 "
          "polymarket TOTAL $1,353 1.422 $13 bf TOTAL $1,531 1.421 $178 pin88 TOTAL $4,544 1.414 "
          "$3,013 bdaq TOTAL $4,849 1.408 $305 overtime TOTAL $5,569 1.406 $720 polymarket TOTAL "
          "$18,944 1.403 $13,374 polymarket TOTAL $32,353 1.384 $13,409 bdaq TOTAL $32,364 1.339 $12")

MMA = ("MMA Start Acca Gillian Robertson, Moneyline Stake $ Price Place $5 $10 $15 Stake At Price $4.36 "
       "Ex. Returns - bf BEST PRICE 2.865 $4 bf TOTAL $35 2.830 $31 bf TOTAL $230 2.796 $195 bdaq TOTAL "
       "$300 2.784 $70 overtime TOTAL $500 2.758 $200 betamapola TOTAL $14,622 2.756 $14,122 bdaq TOTAL "
       "$14,641 2.752 $20 4casters TOTAL $14,717 2.748 $76 pin88 TOTAL $18,468 2.740 $3,750 mbook TOTAL "
       "$18,611 2.668 $143 pmk TOTAL $48,560 2.651 $29,950")

bad = 0


def check(label, ok):
    global bad
    print(f"  {'PASS' if ok else 'FAIL'}  {label}")
    if not ok:
        bad += 1


print("[1] selection label")
check("baseball strips the market suffix",
      A._parse_slip_label(BASEBALL) == "Cleveland Guardians")
check("tennis is a bare name", A._parse_slip_label(TENNIS) == "Rei Sakamoto")
check("mma strips ', Moneyline'", A._parse_slip_label(MMA) == "Gillian Robertson")
for junk in ("", "<unreadable: TimeoutError>", "Tennis Start Acca", "no marker here"):
    check(f"never guesses on {junk!r:26}", A._parse_slip_label(junk) == "")

print("\n[2] ladder parses in document (descending-price) order")
lb = A._parse_slip_ladder(BASEBALL)
check(f"baseball rows = 18 (got {len(lb)})", len(lb) == 18)
check("first row is the BEST PRICE row",
      lb[0]["book"] == "4casters" and lb[0]["odds"] == 1.776 and lb[0]["stake"] == 9852.0)
check("commas stripped from sizes", lb[-1]["stake"] == 56501.0)
check("prices are non-increasing down the ladder",
      all(lb[i]["odds"] >= lb[i + 1]["odds"] for i in range(len(lb) - 1)))
# the cumulative TOTAL column is the arithmetic check that the rows were read correctly
check("best + next = the printed TOTAL (9852+29=9881)",
      abs((lb[0]["stake"] + lb[1]["stake"]) - 9881.0) < 1.0)

lt = A._parse_slip_ladder(TENNIS)
check(f"tennis rows = 14 (got {len(lt)})", len(lt) == 14)
check("tennis best = bf 1.451 $125",
      lt[0]["book"] == "bf" and lt[0]["odds"] == 1.451 and lt[0]["stake"] == 125.0)
lm = A._parse_slip_ladder(MMA)
check("mma reads a decimal 'Stake At Price' without corrupting the ladder",
      lm[0]["odds"] == 2.865 and lm[0]["stake"] == 4.0)
check("unreadable panel -> empty ladder", A._parse_slip_ladder("") == [])

print("\n[3] stake at the QUOTED price (not the cumulative sweep)")
# baseball quoted 1.769: bf 29 + sxbet 507 + pin88 4876 = 5412, and NOT the 9,852 sitting above it
# on excluded 4casters.
check("baseball 1.769 -> 5412", A._stake_at_price(lb, 1.769) == 5412.0)
check("excluded 4casters' 9852 at 1.776 is NOT counted",
      A._stake_at_price(lb, 1.769) < 9852.0)
check("tennis 1.414 -> 3013", A._stake_at_price(lt, 1.414) == 3013.0)
check("mma 2.740 -> 3750", A._stake_at_price(lm, 2.740) == 3750.0)
check("a price not on the ladder -> None", A._stake_at_price(lb, 1.500) is None)
check("empty ladder -> None", A._stake_at_price([], 1.769) is None)
check("zero odds -> None", A._stake_at_price(lb, 0.0) is None)

print("\nALL PASS" if bad == 0 else f"\nFAILURES: {bad}")
sys.exit(1 if bad else 0)
