import json
from collections import OrderedDict

J = "temp_crossarbjournal.jsonl"
HELD = {"FILLED", "DUST_ABSORBED_POLY", "FILLED_WITH_CLEANUP", "HEDGE_COMPLETED"}
open_pos = OrderedDict()   # pairId -> dict

with open(J, encoding="utf-8") as f:
    for line in f:
        line = line.strip()
        if not line: continue
        try: o = json.loads(line)
        except: continue
        ev = o.get("event")
        if ev == "EXECUTION_COMPLETE":
            pid = o.get("pairId"); pos = o.get("position", {}) or {}
            kH, pH = pos.get("kHeld", 0) or 0, pos.get("pHeld", 0) or 0
            outcome = o.get("outcome", "")
            if kH > 0 and pH > 0 and outcome in HELD:
                open_pos[pid] = {
                    "t": o.get("t","")[:19], "execId": o.get("execId",""),
                    "label": (o.get("label","") or "")[:50],
                    "proj": float(pos.get("projectedProfitUsd", 0) or 0),
                    "cost": float(pos.get("totalCostUsd", 0) or 0),
                    "net":  float(pos.get("actualNetPerSet", 0) or 0),
                    "qty":  kH,
                }
            else:
                open_pos.pop(pid, None)   # reversed / orphaned / naked -> not an open arb
        elif ev == "EARLY_EXIT_COMPLETE":
            open_pos.pop(o.get("pairId"), None)   # closed at break-even

tot_proj = sum(p["proj"] for p in open_pos.values())
tot_cost = sum(p["cost"] for p in open_pos.values())
print(f"OPEN POSITIONS: {len(open_pos)}")
print(f"EXPECTED SETTLEMENT P/L (sum projected): ${tot_proj:.4f}")
print(f"Capital deployed in open positions     : ${tot_cost:.2f}")
if tot_cost: print(f"Expected return on deployed capital     : {100*tot_proj/tot_cost:.2f}%")

# per-set edge = proj/qty; true arbs are pennies/set. Flag fat ones as possible mismatched (phantom) pairs.
print("\n-- open positions by projected profit --")
rows = sorted(open_pos.items(), key=lambda kv: -kv[1]["proj"])
for pid, p in rows:
    perset = p["proj"]/p["qty"] if p["qty"] else 0
    flag = "  <== FAT EDGE (verify pair)" if perset > 0.10 else ""
    print(f"  ${p['proj']:+.4f}  ({perset:+.4f}/set x{p['qty']:.0f})  net={p['net']:.4f}  {p['label']}{flag}")

fat = sum(p["proj"] for p in open_pos.values() if p["qty"] and p["proj"]/p["qty"] > 0.10)
if fat:
    print(f"\n  of which FAT-EDGE (per-set>$0.10, possible phantom): ${fat:.4f}")
    print(f"  EXPECTED SETTLEMENT P/L excluding fat-edge pairs   : ${tot_proj-fat:.4f}")
