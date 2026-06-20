#!/usr/bin/env python3
"""verify_arbs.py — POST-MORTEM audit: cross-check every logged arb against the raw bookmaker tape.

Run the sidecar with BOOKMAKER_AUDIT=1 (writes sidecar/quote_audit_*.jsonl) alongside the bot's
CrossArbTelemetry_*.csv, then run this. It joins each arb to the bookmaker game's RAW state at that moment
(exact join via the CSV's HardVenLegId → bookmaker idgm) and flags any arb that wasn't genuinely takeable:

  MISPAIR      bookmaker game's teams don't match the Kalshi market (paired to the wrong game)
  SUSPENDED    book was locked/suspended during the window (tradeable=False or the moneyline line gone)
  PRICE_DRIFT  the arb's book leg price isn't the bookmaker's implied price at that time (stale/derived)
  THIN         bookmaker max_stake below the floor (VERIFY_MIN_STAKE, default $50) — not takeable at size
  NO_AUDIT     no audit sample for this game/window (audit was off, or a pre-audit row) — can't verify

  python verify_arbs.py                          # newest CrossArbTelemetry_*.csv + newest quote_audit_*.jsonl
  python verify_arbs.py path/to.csv path/to.jsonl
"""
import csv
import glob
import json
import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone

try:
    from rapidfuzz import fuzz
except ImportError:
    fuzz = None

MIN_STAKE = float(os.environ.get("VERIFY_MIN_STAKE", "50"))   # bookmaker max_stake floor ($)
PRICE_TOL = float(os.environ.get("VERIFY_PRICE_TOL", "0.02"))  # implied-price match tolerance
WINDOW_PAD = 15.0                                              # seconds around the window to find samples


def _newest(pattern, *dirs):
    files = []
    for d in dirs:
        files += glob.glob(os.path.join(d, pattern))
    return max(files, key=os.path.getmtime) if files else None


def _epoch(s):
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(s.strip(), fmt).replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            continue
    return None


def _kalshi_teams(label):
    """'Cincinnati vs New York Y Winner?' -> ['cincinnati', 'new york y']."""
    s = re.sub(r"\b(winner|moneyline)\b", "", label, flags=re.I).strip().rstrip("?").strip()
    return [p.strip().lower() for p in re.split(r"\bvs\.?\b", s, flags=re.I) if p.strip()]


def _tmatch(a, b):
    if not a or not b:
        return 0
    if a in b or b in a:
        return 100
    return int(fuzz.token_set_ratio(a, b)) if fuzz else (100 if set(a.split()) & set(b.split()) else 0)


def _teams_match(kalshi_teams, htm, vtm):
    """Do the Kalshi market's two teams match this bookmaker game's two teams (either assignment)?"""
    if len(kalshi_teams) != 2:
        return True   # can't parse Kalshi teams — don't claim mispair
    bt = [htm.lower(), vtm.lower()]
    s1 = max(_tmatch(kalshi_teams[0], bt[0]), _tmatch(kalshi_teams[0], bt[1]))
    s2 = max(_tmatch(kalshi_teams[1], bt[0]), _tmatch(kalshi_teams[1], bt[1]))
    return min(s1, s2) >= 70


def main():
    args = sys.argv[1:]
    here = os.path.dirname(os.path.abspath(__file__))
    repo = os.path.dirname(os.path.dirname(here))      # sidecar -> HardVenArb -> repo root
    csv_path = args[0] if len(args) > 0 else _newest("CrossArbTelemetry_*.csv", repo, os.getcwd(), here)
    aud_path = args[1] if len(args) > 1 else _newest("quote_audit_*.jsonl", here, os.getcwd())
    if not csv_path:
        print("No CrossArbTelemetry_*.csv found.")
        return
    if not aud_path:
        print("No quote_audit_*.jsonl found — run the sidecar with BOOKMAKER_AUDIT=1.")
        return
    print(f"CSV   : {csv_path}\nAUDIT : {aud_path}")
    if fuzz is None:
        print("(rapidfuzz not installed — team checks are substring-only; pip install rapidfuzz to improve.)")
    print()

    by_idgm = defaultdict(list)
    for line in open(aud_path, encoding="utf-8"):
        if line.strip():
            r = json.loads(line)
            by_idgm[str(r.get("idgm"))].append(r)

    rows = list(csv.DictReader(open(csv_path, encoding="utf-8-sig")))   # utf-8-sig strips the CSV's BOM
    counts = defaultdict(int)
    for r in rows:
        label = r.get("Label", "")
        t0, t1 = _epoch(r.get("StartTime", "")), _epoch(r.get("EndTime", ""))
        leg = r.get("HardVenLegId", "") or ""
        idgm = leg.split(":")[0] if leg else ""
        side = leg.split(":")[2] if leg.count(":") >= 2 else ""
        try:
            pleg = float((r.get("BestLegPrices", "") or "|").split("|")[1])
        except (ValueError, IndexError):
            pleg = None

        flags = []
        samples = sorted(by_idgm.get(idgm, []), key=lambda a: a.get("ts", 0)) if idgm else []
        in_win = []
        if t0 is not None and t1 is not None and samples:
            during = [a for a in samples if t0 <= a.get("ts", 0) <= t1 + WINDOW_PAD]
            before = [a for a in samples if a.get("ts", 0) <= t0]
            # the audit logs ON CHANGE, so the book's state during the window is the last change at/before
            # the window start (carry-in), plus any changes within the window.
            in_win = ([before[-1]] if before else []) + during

        if not in_win:
            flags.append("NO_AUDIT")
        else:
            # MISPAIR — does the joined bookmaker game's teams match the Kalshi market?
            kt = _kalshi_teams(label)
            if not any(_teams_match(kt, a.get("htm", ""), a.get("vtm", "")) for a in in_win):
                flags.append("MISPAIR")
            # SUSPENDED — book not takeable at any point in the window
            if any((not a.get("tradeable")) or (not a.get("line_present")) for a in in_win):
                flags.append("SUSPENDED")
            # PRICE_DRIFT — the arb's book price must equal the bookmaker implied price for that side
            if pleg is not None:
                key = {"H": "implied_h", "V": "implied_v"}.get(side)
                def _ok(a):
                    if key and a.get(key) is not None:
                        return abs(a[key] - pleg) <= PRICE_TOL
                    # unknown side → accept if it matches EITHER implied
                    return any(a.get(k) is not None and abs(a[k] - pleg) <= PRICE_TOL
                               for k in ("implied_h", "implied_v"))
                if not any(_ok(a) for a in in_win):
                    flags.append("PRICE_DRIFT")
            # THIN — max stake floor
            stakes = [a.get("max_stake") for a in in_win if a.get("max_stake")]
            if stakes and max(stakes) < MIN_STAKE:
                flags.append("THIN")

        verdict = "OK" if not flags else ",".join(flags)
        for f in (flags or ["OK"]):
            counts[f] += 1
        tag = "OK  " if not flags else "FLAG"
        st = (r.get("StartTime", "") or "")[11:23]
        print(f"[{tag}] {st} {label[:40]:40} net={r.get('BestNetCost',''):>7} pLeg={pleg} -> {verdict}")

    print("\n=== SUMMARY ===")
    for k in ("OK", "MISPAIR", "SUSPENDED", "PRICE_DRIFT", "THIN", "NO_AUDIT"):
        if counts.get(k):
            print(f"  {k:12} {counts[k]}")
    print(f"  {'total':12} {len(rows)}")
    flagged = sum(v for k, v in counts.items() if k != "OK")
    print(f"\n{flagged} row(s) flagged for review." if flagged else "\nAll rows verified OK against the bookmaker tape.")


if __name__ == "__main__":
    main()
