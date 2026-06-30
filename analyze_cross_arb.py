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

NET EV (§6) — the failed-HardVen-leg HEDGE model. Models Kalshi-first execution: you commit the fast,
reversible Kalshi leg at OPEN, then fire the slow, irreversible HardVen leg. If HardVen held within the arb
long enough (HardVenLegWithinMs > --hardven-secs) you complete → WIN; if it left first you're naked on Kalshi
and must UNWIND it (sell the held leg back at the bid) at --hedge-secs. The unwind is priced from the
CrossArbHedgeMonitor_*.csv tape (per-arb post-open Kalshi bid trajectory). NET EV = Σ(wins) − Σ(miss hedge
cost) — the per-attempt expected value, the number that says whether the strategy survives its miss rate.
Needs the within-columns AND a hedge tape (both produced by the updated bot); older CSVs fall back gracefully.

  python analyze_cross_arb.py                       # latest CrossArbTelemetry_*.csv in CWD
  python analyze_cross_arb.py --file path.csv --fx 1.08 --pinnacle-bankroll 50 --kalshi-bankroll 422
  python analyze_cross_arb.py --hedge-secs 6 --max-bet 300   # net-EV w/ hedge tape (auto-discovered)
"""
from __future__ import annotations

import argparse
import csv
import glob
import os
import statistics as st
import sys
from collections import Counter, defaultdict
from datetime import datetime

# The report uses unicode (→ × € ─ ≥ ¢ ⚠); force UTF-8 stdout so the Windows cp1252 console doesn't crash.
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass


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


def capturable(r: dict, hardven_ms: float, kalshi_ms: float, use_within: bool):
    """WITHIN model (newer CSVs with per-leg within-times — the accurate one): capturable iff EACH leg stayed
    'within the arb' (ask ≤ its open price) long enough to place it — HardVen > hardven_ms AND Kalshi >
    kalshi_ms. Both windows start at open, so you place the fast Kalshi leg first then the slow HardVen leg.
    LEGACY model (older CSVs): window open > hardven_ms, OR Kalshi-closed + dur > kalshi_ms + the HardVen leg's
    FROZEN-price age > hardven_ms (conservative — any wiggle resets it)."""
    if use_within:
        hw, kw = _f(r.get("HardVenLegWithinMs")), _f(r.get("KalshiLegWithinMs"))
        if hw <= hardven_ms:
            return False, "HardVen left-within <%gs" % (hardven_ms / 1000)
        if kw <= kalshi_ms:
            return False, "Kalshi left-within <%gs" % (kalshi_ms / 1000)
        return True, "both legs held within"
    dur = _f(r.get("DurationMs"))
    if dur > hardven_ms:
        return True, "open >%gs" % (hardven_ms / 1000)
    if (r.get("ClosedBySide") == "KALSHI" and dur > kalshi_ms
            and _f(r.get("HardVenLegAgeMsAtClose")) > hardven_ms):
        return True, "Kalshi-closed, HardVen frozen >%gs" % (hardven_ms / 1000)
    return False, "too fast / HardVen leg not held"


def kalshi_fee(p: float) -> float:
    """Kalshi per-contract fee: 0.07 × p × (1−p). Charged on BOTH the entry and the unwind."""
    return 0.07 * p * (1.0 - p)


def group_hedge(samples: list[dict]) -> dict:
    """CrossArbHedgeMonitor rows → {(PairId, OpenTime): [samples sorted by OffsetMs]}. The key joins to a main
    telemetry window on (PairId, StartTime) — OpenTime == that window's StartTime."""
    g: dict = defaultdict(list)
    for s in samples:
        g[(s.get("PairId", ""), s.get("OpenTime", ""))].append(s)
    for k in g:
        g[k].sort(key=lambda s: _f(s.get("OffsetMs")))
    return g


def sample_at(samples: list[dict], target_ms: float):
    """Nearest sample at-or-after target_ms (the realization instant); if the tape ends before target, the last
    sample. Returns (sample, actual_offset_ms) or (None, None)."""
    if not samples:
        return None, None
    after = [s for s in samples if _f(s.get("OffsetMs")) >= target_ms]
    s = after[0] if after else samples[-1]
    return s, _f(s.get("OffsetMs"))


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
    ap.add_argument("--metric", choices=("auto", "within", "legacy"), default="auto",
                    help="capturability model: 'within' = per-leg held-within-arb times (newer CSVs, accurate); "
                         "'legacy' = duration + HardVen frozen-age; 'auto' = within if the columns exist (default)")
    ap.add_argument("--hedge-file", help="CrossArbHedgeMonitor CSV (default: newest in CWD)")
    ap.add_argument("--hedge-secs", type=float, default=6.0,
                    help="SECONDS after open at which you realize the HardVen leg missed and unwind the Kalshi "
                         "leg — the realization delay the hedge is priced at (default 6)")
    ap.add_argument("--max-bet", type=float, default=300.0,
                    help="max $ capital deployed per single arb (per-bet cap, separate from total bankroll; default 300)")
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

    has_within = bool(rows[0].get("HardVenLegWithinMs") is not None and rows[0].get("KalshiLegWithinMs") is not None)
    use_within = a.metric == "within" or (a.metric == "auto" and has_within)
    if a.metric == "within" and not has_within:
        print("[WARN] --metric within but the CSV lacks HardVenLegWithinMs/KalshiLegWithinMs → using legacy.")
        use_within = False
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
        ok, why = capturable(r, hardven_ms, kalshi_ms, use_within)
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
    if use_within:
        print(f"\n1. CAPTURABILITY  [within model]  (HardVen held-within >{a.hardven_secs:g}s  AND  "
              f"Kalshi held-within >{a.kalshi_secs:g}s — each leg placeable)")
    else:
        print(f"\n1. CAPTURABILITY  [legacy model]  (dur>{a.hardven_secs:g}s  OR  Kalshi-closed + "
              f"dur>{a.kalshi_secs:g}s + HardVen-frozen>{a.hardven_secs:g}s){'  — no within-times in this CSV' if not has_within else ''}")
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

    # ── 6. NET EV — failed-HardVen-leg hedge model ───────────────────────────────
    # Kalshi-first execution: you commit the fast, reversible Kalshi leg at OPEN, then fire the slow HardVen
    # leg. If HardVen HELD within the arb long enough (HardVenLegWithinMs > hardven-secs) you complete → WIN.
    # If it left first, you're naked on Kalshi and must UNWIND it (sell the held leg back at the bid) at
    # --hedge-secs. The hedge tape (CrossArbHedgeMonitor) prices that unwind. NET EV = wins − miss hedge cost.
    print("\n6. NET EV  —  FAILED-HARDVEN-LEG HEDGE MODEL  (Kalshi-first: commit Kalshi, unwind it if HardVen misses)")
    if not has_within:
        print("   ⚠ this CSV has no per-leg within-times → can't split win vs miss. Run the updated bot.")
        print("=" * 78)
        return
    hpath = a.hedge_file or (sorted(glob.glob("CrossArbHedgeMonitor_*.csv")) or [None])[-1]
    if not hpath or not os.path.exists(hpath):
        print("   ⚠ no CrossArbHedgeMonitor_*.csv found (pass --hedge-file) → can't price the unwind. Run the updated bot.")
        print("=" * 78)
        return
    hedge = group_hedge(load(hpath))
    target_ms = a.hedge_secs * 1000
    print(f"   hedge tape: {os.path.basename(hpath)}  |  realize HardVen miss at {a.hedge_secs:g}s  |  per-bet cap ${a.max_bet:g}")

    wins, misses = [], []
    no_entry = no_hedge_data = recovered = 0
    for r in rows:
        edge = _f(r.get("NetProfitPerShare"))
        if edge < a.min_edge:
            continue
        hw, kw = _f(r.get("HardVenLegWithinMs")), _f(r.get("KalshiLegWithinMs"))
        # Kalshi-first: you commit Kalshi at open. If it didn't even hold kalshi-secs, the entry price is
        # unreliable (Kalshi snapped away instantly) → treat as no clean entry, neither win nor hedge.
        if kw <= kalshi_ms:
            no_entry += 1
            continue
        # sizing — shared by win & miss: market depth ∩ both bankrolls ∩ per-bet cap
        kdepth = _f(r.get("KalshiDepth"))
        hdepth_usd = _f(r.get("HardVenDepth")) * a.fx
        exec_depth = min(kdepth, hdepth_usd)
        net = _f(r.get("BestNetCost"), 1.0)
        kp, hp = _legs(r.get("BestLegPrices"))
        k_aff = a.kalshi_bankroll / kp if kp > 0 else exec_depth
        p_aff = a.pinnacle_bankroll / hp if hp > 0 else exec_depth
        b_aff = a.max_bet / net if net > 0 else exec_depth
        contracts = max(0.0, min(exec_depth, k_aff, p_aff, b_aff))
        day = r.get("StartTime", "")[:10]

        if hw > hardven_ms:                                          # HardVen held → complete the arb → WIN
            wins.append({"label": r.get("Label", "")[:40], "contracts": contracts,
                         "profit": edge * contracts, "capital": net * contracts, "date": day})
            continue

        # MISS — unwind the committed Kalshi leg at the realization instant
        samples = hedge.get((r.get("PairId", ""), r.get("StartTime", "")))
        if not samples:
            no_hedge_data += 1
            continue
        s, actual_ms = sample_at(samples, target_ms)
        entry = _f(s.get("EntryKalshiAsk")) or kp
        unwind = _f(s.get("KalshiUnwindBid"))
        hv_now = _f(s.get("HardVenLegNow"))
        # if the HardVen leg came back INTO the arb by now, you'd COMPLETE late instead of hedging
        if hv_now > 0 and entry + hv_now + kalshi_fee(entry) < 1.0:
            recovered += 1
            late_edge = 1.0 - (entry + hv_now + kalshi_fee(entry))
            wins.append({"label": r.get("Label", "")[:40], "contracts": contracts,
                         "profit": late_edge * contracts, "capital": net * contracts, "date": day})
            continue
        fees = kalshi_fee(entry) + kalshi_fee(unwind)               # Kalshi charges on entry AND unwind
        pnl_share = unwind - entry - fees                           # negative = realized loss on the miss
        # break-even FIX: does the unwind bid later recover to ≥ break-even (within the monitored tape)?
        fix_ms = None
        for s2 in samples:
            off = _f(s2.get("OffsetMs"))
            if off < actual_ms:
                continue
            ub = _f(s2.get("KalshiUnwindBid"))
            if ub - entry - (kalshi_fee(entry) + kalshi_fee(ub)) >= 0:
                fix_ms = off
                break
        misses.append({"label": r.get("Label", "")[:40], "contracts": contracts, "pnl_share": pnl_share,
                       "pnl": pnl_share * contracts, "at_ms": actual_ms, "fix_ms": fix_ms,
                       "capital": entry * contracts, "date": day})

    n_win, n_miss = len(wins), len(misses)
    n_att = n_win + n_miss
    if not n_att:
        print(f"   No attemptable windows (skipped: {no_entry} no-entry, {no_hedge_data} no hedge-tape match).")
        print("=" * 78)
        return

    win_profit = sum(w["profit"] for w in wins)
    hedge_pnl = sum(m["pnl"] for m in misses)
    net_ev = win_profit + hedge_pnl
    miss_loss = sum(-m["pnl"] for m in misses if m["pnl"] < 0)
    miss_gain = sum(m["pnl"] for m in misses if m["pnl"] > 0)
    cap_spent = sum(w["capital"] for w in wins) + sum(m["capital"] for m in misses)

    print(f"   attempts {n_att}  →  {n_win} wins ({100*n_win/n_att:.0f}%)  |  {n_miss} misses ({100*n_miss/n_att:.0f}%)"
          + (f"   [{recovered} HardVen recovered → completed late]" if recovered else ""))
    if no_entry or no_hedge_data:
        print(f"   (excluded: {no_entry} no-clean-Kalshi-entry, {no_hedge_data} miss w/ no hedge-tape match)")
    print(f"   win profit       +${win_profit:9,.2f}   ({n_win} × avg ${win_profit/max(n_win,1):.2f})")
    print(f"   miss hedge P/L    ${hedge_pnl:+9,.2f}   (realized loss ${miss_loss:,.2f}"
          + (f", favorable-unwind +${miss_gain:,.2f}" if miss_gain else "") + ")")
    print(f"   {'─'*44}")
    print(f"   NET EV            ${net_ev:+9,.2f}   over {n_att} attempts  =  ${net_ev/n_att:+.4f}/attempt")
    if cap_spent:
        print(f"   capital deployed  ${cap_spent:9,.0f}   →  net return {100*net_ev/cap_spent:+.2f}%")

    if misses:
        pnls = [m["pnl_share"] for m in misses]
        print(f"\n   UNWIND @ {a.hedge_secs:g}s (per share):  avg {st.mean(pnls):+.4f}   median {st.median(pnls):+.4f}"
              f"   best {max(pnls):+.4f}   worst {min(pnls):+.4f}")
        fixable = [m for m in misses if m["fix_ms"] is not None and m["pnl"] < 0]
        losers = [m for m in misses if m["pnl"] < 0]
        if losers and fixable:
            fix_secs = sorted((m["fix_ms"] - m["at_ms"]) / 1000 for m in fixable)
            print(f"   BREAK-EVEN FIX:  {len(fixable)}/{len(losers)} loss-positions later recovered to ≥ break-even "
                  f"(median +{fix_secs[len(fix_secs)//2]:.1f}s after unwind) → for these, WAITING beats unwinding at {a.hedge_secs:g}s.")
        elif losers:
            print(f"   BREAK-EVEN FIX:  none of the {len(losers)} loss-positions recovered within the tape "
                  f"→ the {a.hedge_secs:g}s unwind was the floor (hold-to-settlement is the only 'fix').")

    by_day = defaultdict(lambda: [0, 0, 0.0, 0.0])    # date → [n_win, n_miss, win_profit, hedge_pnl]
    for w in wins:
        d = by_day[w["date"]]; d[0] += 1; d[2] += w["profit"]
    for m in misses:
        d = by_day[m["date"]]; d[1] += 1; d[3] += m["pnl"]
    if len(by_day) > 1:
        print("\n   DAILY (net = win profit + miss hedge P/L):")
        for day in sorted(by_day):
            nw, nm, wp, hpl = by_day[day]
            print(f"     {day}:  {nw}W / {nm}M    net ${wp+hpl:+,.2f}")

    print(f"\n   NOTE: NET EV is the per-ATTEMPT expected value (decision metric), NOT realized $ — capital locks "
          f"until settlement, so concurrent throughput is bankroll-limited (see §3B).")
    print("=" * 78)


if __name__ == "__main__":
    main()
