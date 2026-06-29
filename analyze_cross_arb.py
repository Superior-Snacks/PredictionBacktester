#!/usr/bin/env python3
"""
analyze_cross_arb.py — read CrossArbTelemetry_*.csv (Kalshi ↔ HardVen/Pinnacle cross-arb) and estimate, per
the capturability framework: how many windows were CAPTURABLE, at what PRICE/edge, and the estimated PROFIT
(both at full market depth and constrained by a real bankroll).

CAPTURABILITY — one knob: --hardven-secs (default 6) = the slow HardVen leg's capture time; --kalshi-secs
(default 0.2) = the fast Kalshi leg's. A window is capturable iff BOTH legs could be on before it closed:
    DurationMs > hardven-secs                                       (window open long enough to place HardVen)
  OR (ClosedBySide == KALSHI and DurationMs > kalshi-secs           (the fast Kalshi side had its time)
      and HardVenLegAgeMsAtClose > hardven-secs)                    (HardVen leg already held that long)

CURRENCY: HardVen depth is in the Pinnacle ACCOUNT currency (EUR); Kalshi is USD. The arb PRICE/edge is
unitless (probabilities), so detection is unaffected — but SIZE/$ are not. We convert HardVen depth to USD
(× --fx) before combining. NOTE: CSVs written BEFORE the C# HARDVEN_FX_TO_USD fix have raw-EUR depth → use
--fx 1.08; CSVs written AFTER it are already USD → use --fx 1.0.

PROFIT is recomputed from raw (KalshiDepth, HardVenDepth, BestNetCost, edge) so it's FX-correct regardless of
the CSV's pre-computed columns. Reported two ways:
  A) FULL DEPTH (unlimited capital) — the size of the OPPORTUNITY.
  B) BANKROLL-CONSTRAINED — what your actual Pinnacle (EUR) + Kalshi (USD) bankrolls could capture per window.

  python analyze_cross_arb.py                       # latest CrossArbTelemetry_*.csv in CWD
  python analyze_cross_arb.py --file path.csv --fx 1.08 --pinnacle-bankroll 50 --kalshi-bankroll 422
"""
from __future__ import annotations

import argparse
import csv
import glob
import os
import statistics as st
from collections import Counter, defaultdict
from datetime import datetime


def _f(v, d=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


def _legs(s: str):
    """BestLegPrices 'kalshi|hardven' → (kalshi_price, hardven_price). These are the per-$1-payout costs."""
    p = (s or "").split("|")
    return (_f(p[0]), _f(p[1])) if len(p) == 2 else (0.0, 0.0)


def _span_min(times: list[str]) -> float:
    """Minutes between the first and last window StartTime ('2026-06-26 13:50:03.805')."""
    if len(times) < 2:
        return 0.0
    try:
        fmt = "%Y-%m-%d %H:%M:%S.%f"
        return (datetime.strptime(times[-1], fmt) - datetime.strptime(times[0], fmt)).total_seconds() / 60
    except ValueError:
        return 0.0


def load(path: str) -> list[dict]:
    with open(path, encoding="utf-8-sig", newline="") as fh:        # utf-8-sig: strip the BOM
        return list(csv.DictReader(fh))


def capturable(r: dict, hardven_ms: float, kalshi_ms: float):
    """HARDVEN_MS = the slow leg's capture time, drives BOTH branches (window open long enough OR HardVen leg
    already held long enough). KALSHI_MS = the fast leg's minimum when Kalshi closes the window."""
    dur = _f(r.get("DurationMs"))
    if dur > hardven_ms:                                              # window open long enough to place HardVen
        return True, "open >%gs" % (hardven_ms / 1000)
    if (r.get("ClosedBySide") == "KALSHI" and dur > kalshi_ms
            and _f(r.get("HardVenLegAgeMsAtClose")) > hardven_ms):    # HardVen leg already held that long
        return True, "Kalshi-closed, HardVen held >%gs" % (hardven_ms / 1000)
    return False, "too fast / HardVen leg not held"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", help="CrossArbTelemetry CSV (default: newest in CWD)")
    ap.add_argument("--fx", type=float, default=1.08,
                    help="USD per HardVen-currency unit (EUR). PRE-C#-fix CSVs: 1.08; POST-fix (already USD): 1.0")
    ap.add_argument("--pinnacle-bankroll", type=float, default=50.0, help="HardVen/Pinnacle bankroll (EUR)")
    ap.add_argument("--kalshi-bankroll", type=float, default=422.0, help="Kalshi bankroll (USD)")
    ap.add_argument("--min-edge", type=float, default=0.0, help="ignore windows with edge below this (e.g. 0.005)")
    ap.add_argument("--hardven-secs", type=float, default=6.0,
                    help="SECONDS the slow HardVen leg needs to capture — the window must be open this long, OR "
                         "the HardVen leg already held this long when Kalshi closes it (default 6)")
    ap.add_argument("--kalshi-secs", type=float, default=0.2,
                    help="SECONDS the fast Kalshi leg needs (min window duration when Kalshi closes it; default 0.2)")
    a = ap.parse_args()
    hardven_ms, kalshi_ms = a.hardven_secs * 1000, a.kalshi_secs * 1000

    path = a.file or (sorted(glob.glob("CrossArbTelemetry_*.csv")) or [None])[-1]
    if not path or not os.path.exists(path):
        print("No CrossArbTelemetry_*.csv found (pass --file).")
        return
    rows = load(path)
    if not rows:
        print(f"{path}: empty.")
        return

    times = [r.get("StartTime", "") for r in rows if r.get("StartTime")]
    span = f"{times[0][:19]} → {times[-1][11:19]}" if times else "?"
    print("=" * 78)
    print(f"CROSS-PLATFORM ARB TELEMETRY  —  {os.path.basename(path)}")
    print(f"{len(rows)} windows  |  {span}  |  FX HardVen(EUR)→USD ×{a.fx:g}  |  "
          f"bankroll: Pinnacle €{a.pinnacle_bankroll:g} + Kalshi ${a.kalshi_bankroll:g}")
    print("=" * 78)

    # ── per-window enrichment ────────────────────────────────────────────────────
    cap_rows, reasons = [], Counter()
    for r in rows:
        edge = _f(r.get("NetProfitPerShare"))
        if edge < a.min_edge:
            continue
        ok, why = capturable(r, hardven_ms, kalshi_ms)
        reasons[("capturable: " + why) if ok else "not capturable"] += 1
        if not ok:
            continue
        kdepth = _f(r.get("KalshiDepth"))
        hdepth_usd = _f(r.get("HardVenDepth")) * a.fx                 # EUR-payout units → USD-equivalent
        exec_depth = min(kdepth, hdepth_usd)                          # market-limited matched pairs
        net = _f(r.get("BestNetCost"), 1.0)
        kp, hp = _legs(r.get("BestLegPrices"))                        # per-pair leg costs (Kalshi $, HardVen €)
        # bankroll caps: each matched pair stakes kp on Kalshi (USD) and hp on Pinnacle (EUR)
        k_aff = a.kalshi_bankroll / kp if kp > 0 else exec_depth
        p_aff = a.pinnacle_bankroll / hp if hp > 0 else exec_depth
        capped_depth = min(exec_depth, k_aff, p_aff)
        bind = ("market depth" if capped_depth == exec_depth else
                "Pinnacle €" if capped_depth == p_aff else "Kalshi $")
        cap_rows.append({
            "label": r.get("Label", "")[:48], "pair": r.get("PairId", ""), "edge": edge, "net": net,
            "exec_depth": exec_depth, "capped_depth": capped_depth, "bind": bind,
            "profit_full": edge * exec_depth, "profit_capped": edge * capped_depth,
            "capital_full": net * exec_depth, "dur": _f(r.get("DurationMs")),
            "held": r.get("HardVenLegHeld") == "1", "closed": r.get("ClosedBySide", ""),
        })

    n_cap = len(cap_rows)
    print(f"\n1. CAPTURABILITY  (dur>{a.hardven_secs:g}s  OR  Kalshi-closed + dur>{a.kalshi_secs:g}s + "
          f"HardVen-held>{a.hardven_secs:g}s)")
    for k, v in reasons.most_common():
        print(f"   {v:>4}  {k}")
    print(f"   ----")
    print(f"   {n_cap:>4}  CAPTURABLE  ({100*n_cap/len(rows):.0f}% of {len(rows)})")
    if not n_cap:
        print("\n   No capturable windows under these thresholds.")
        return

    # ── 2. price / edge ──────────────────────────────────────────────────────────
    edges = [c["edge"] for c in cap_rows]
    nets = [c["net"] for c in cap_rows]
    print("\n2. PRICE / EDGE  (capturable windows)")
    print(f"   net cost / pair (what you pay):  mean {st.mean(nets):.4f}  median {st.median(nets):.4f}  "
          f"min {min(nets):.4f}")
    print(f"   edge / contract (1 − net):       mean {st.mean(edges):.4f}  median {st.median(edges):.4f}  "
          f"max {max(edges):.4f}")
    print(f"   → thin edges ({100*st.mean(edges):.1f}¢/contract avg); profit scales with SIZE, not price.")

    # ── 3. profit ────────────────────────────────────────────────────────────────
    pf_full = sum(c["profit_full"] for c in cap_rows)
    cap_full = sum(c["capital_full"] for c in cap_rows)
    capped = [c["profit_capped"] for c in cap_rows]
    binds = Counter(c["bind"] for c in cap_rows)
    print("\n3. PROFIT ESTIMATE  (capturable windows, FX-corrected)")
    print(f"   A) FULL DEPTH (unlimited capital) — the OPPORTUNITY:")
    print(f"        total ${pf_full:,.2f} over {n_cap} windows (avg ${pf_full/n_cap:,.2f})  on ${cap_full:,.0f} "
          f"capital → {100*pf_full/cap_full:.2f}% return")
    print(f"   B) BANKROLL-CONSTRAINED (€{a.pinnacle_bankroll:g} Pinnacle / ${a.kalshi_bankroll:g} Kalshi) — what YOU capture:")
    print(f"        PER WINDOW:  avg ${st.mean(capped):.2f}   median ${st.median(capped):.2f}   max ${max(capped):.2f}")
    print(f"        binding constraint: " + ", ".join(f"{k} {v}×" for k, v in binds.most_common()))
    print(f"        → your €{a.pinnacle_bankroll:g} caps each arb to ~{a.pinnacle_bankroll/st.mean([_legs(r.get('BestLegPrices'))[1] for r in rows if _legs(r.get('BestLegPrices'))[1]>0]):.0f} "
          f"contract-pairs, so profit/arb is small and scales with BANKROLL, not arb COUNT.")
    print(f"        ⚠ do NOT sum these {n_cap} windows (${sum(capped):,.0f}): that assumes capital RECYCLES instantly.")
    print(f"          A position locks until the match settles (hours), so one ~{_span_min(times):.0f}-min session "
          f"realistically does only a few CONCURRENT arbs ≈ ${st.median(capped)*3:.0f}–${st.mean(capped)*5:.0f}.")

    # ── 4. by pair ───────────────────────────────────────────────────────────────
    by_pair = defaultdict(lambda: [0, 0.0, 0.0])
    for c in cap_rows:
        b = by_pair[c["label"] or c["pair"]]
        b[0] += 1; b[1] += c["profit_full"]; b[2] += c["profit_capped"]
    print("\n4. TOP PAIRS (by full-depth profit)")
    for label, (n, pf, pc) in sorted(by_pair.items(), key=lambda x: -x[1][1])[:10]:
        print(f"   {n:>2}×  ${pf:>9,.2f} full / ${pc:>6.2f} capped   {label}")

    # ── 5. caveats ───────────────────────────────────────────────────────────────
    held = sum(1 for c in cap_rows if c["held"])
    print("\n5. CAVEATS")
    print(f"   • Settlement risk: tennis-heavy → retirements (Pinnacle VOIDS / Kalshi SETTLES on result). "
          f"{held}/{n_cap} capturable windows had the HardVen leg HELD pre-window — those are the ones whose")
    print(f"     irreversible leg is most exposed to a void. This is THE thing to confirm on the live run.")
    print(f"   • Depth = Pinnacle max-risk LIMIT, not guaranteed fill; real captures are partial (competition).")
    print(f"   • FX ×{a.fx:g} applied to HardVen(EUR) depth — correct for PRE-C#-fix CSVs; use --fx 1.0 for newer ones.")
    print(f"   • Edge is already net of Kalshi fees; HardVen vig is in the odds (no separate fee).")


if __name__ == "__main__":
    main()
