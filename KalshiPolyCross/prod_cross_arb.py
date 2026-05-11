"""
prod_cross_arb.py — Cross-Arb Live Execution Analyzer
Reads CrossArbJournal_*.jsonl written by CrossArbExecutor and produces:
  1. Session summary
  2. P&L analysis
  3. Fill & execution quality
  4. Cleanup cost analysis
  5. Fee model tracking
  6. Reconciliation & alerts
  7. Settlement & post-mortem  (Kalshi + Poly API resolution, cached)
  8. Pair health & position limits

Usage:
  python prod_cross_arb.py
  python prod_cross_arb.py --file CrossArbJournal_20260509.jsonl
  python prod_cross_arb.py --dry-run          # only dry-run executions
  python prod_cross_arb.py --live             # only live executions
  python prod_cross_arb.py --pair KXSENATEWVR-26-SMOO
  python prod_cross_arb.py --no-api           # skip settlement API calls
  python prod_cross_arb.py --since 2026-05-01
"""

import json
import sys
import glob
import os
import argparse
import time
import urllib.request
import urllib.error
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

# Ensure UTF-8 output on Windows (box-drawing chars in separators)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

_HERE = Path(__file__).parent   # KalshiPolyCross/

KALSHI_BASE    = "https://api.elections.kalshi.com/trade-api/v2"
POLY_GAMMA     = "https://gamma-api.polymarket.com"
CACHE_FILE     = _HERE / "cross_arb_resolution_cache.json"
BLOCKLIST_FILE = _HERE / "cross_pair_blocklist.json"
PAIRS_FILE     = _HERE / "cross_pairs.json"

SEP  = "─" * 72
SEP2 = "═" * 72

# ─── File Discovery ───────────────────────────────────────────────────────────

def find_latest_journal():
    candidates = (
        glob.glob(str(_HERE / "CrossArbJournal_*.jsonl")) +
        glob.glob(str(_HERE / "bin" / "**" / "CrossArbJournal_*.jsonl"), recursive=True)
    )
    if not candidates:
        return None
    return max(candidates, key=os.path.getmtime)

# ─── Data Loading ─────────────────────────────────────────────────────────────

def load_journal(path):
    events, skipped = [], 0
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                skipped += 1
    if skipped:
        print(f"  [warn] {skipped} malformed line(s) skipped")
    return sorted(events, key=lambda e: e.get("t", ""))

def load_pairs():
    if not PAIRS_FILE.exists():
        return {}
    with open(PAIRS_FILE, encoding="utf-8") as f:
        pairs = json.load(f)
    return {p["kalshi_ticker"]: p for p in pairs}

def extract_ticker(pair_id):
    """MANUAL_{kTicker}__{yesToken[0:8]} → kTicker"""
    if not pair_id:
        return ""
    if "__" in pair_id:
        return pair_id.split("__")[0].replace("MANUAL_", "")
    return pair_id

def parse_dt(t_str):
    if not t_str:
        return None
    try:
        dt = datetime.fromisoformat(t_str.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None

def fmt_dur(seconds):
    if seconds < 60:   return f"{seconds:.0f}s"
    if seconds < 3600: return f"{seconds/60:.0f}m"
    return f"{seconds/3600:.1f}h"

def fmt_pct(n, d):
    return f"{n/d:.0%}" if d else "n/a"

def hdr(title):
    print(f"\n{SEP}\n  {title}\n{SEP}")

# ─── Resolution Cache ─────────────────────────────────────────────────────────

def load_cache():
    if CACHE_FILE.exists():
        with open(CACHE_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_cache(cache):
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(cache, f, indent=2)

def _get_json(url):
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())

def fetch_kalshi_resolution(ticker, cache):
    """Returns 'yes', 'no', or None (unresolved/error). Caches resolved results."""
    key = f"K:{ticker}"
    if key in cache and cache[key] is not None:
        return cache[key]
    try:
        data   = _get_json(f"{KALSHI_BASE}/markets/{ticker}")
        market = data.get("market", data)
        result = market.get("result")       # "yes" | "no" | null
        if result:
            cache[key] = result
        return result
    except Exception as e:
        print(f"  [warn] Kalshi resolution fetch failed for {ticker}: {e}")
        return None

def fetch_poly_token_wins(token_id, cache):
    """Returns True if the token won, False if it lost, None if unresolved/error."""
    key = f"P:{token_id}"
    if key in cache and cache[key] is not None:
        return cache[key]
    try:
        data    = _get_json(f"{POLY_GAMMA}/markets?clob_token_ids={token_id}")
        markets = data if isinstance(data, list) else [data]
        for market in markets:
            if not market.get("closed", False):
                continue
            for tok in market.get("tokens", []):
                if str(tok.get("token_id", "")) == str(token_id):
                    # Prefer explicit winner field; fall back to price==1.0
                    if "winner" in tok:
                        result = bool(tok["winner"])
                    else:
                        price  = float(tok.get("price", -1))
                        result = price >= 0.99
                    cache[key] = result
                    return result
        return None
    except Exception as e:
        print(f"  [warn] Poly resolution fetch failed for token {str(token_id)[:12]}...: {e}")
        return None

def categorize_settlement(exec_ev, k_result, k_resolved, p_token_wins, p_resolved):
    if not k_resolved or not p_resolved:
        return "PENDING"
    arb_type = exec_ev.get("arbType", "")
    # K_YES_P_NO: we hold Kalshi YES + Poly NO token
    # K_NO_P_YES: we hold Kalshi NO  + Poly YES token
    k_pays = (arb_type == "K_YES_P_NO" and k_result == "yes") or \
             (arb_type == "K_NO_P_YES" and k_result == "no")
    p_pays = bool(p_token_wins)

    if k_pays and p_pays:         return "PAIR_MISMATCH_BOTH_WON"
    if not k_pays and not p_pays: return "PAIR_MISMATCH_BOTH_LOST"
    # Exactly one pays — correct hedge; determine P&L quality
    pos         = exec_ev.get("position") or {}
    actual_net  = float(pos.get("actualNetPerSet",  1.0))
    modeled_net = float(pos.get("modeledNetPerSet", actual_net))
    if actual_net >= 1.0:
        base = "FEE_MODEL_LOSS" if (actual_net - modeled_net) > 0.005 else "EXECUTION_LOSS"
    else:
        base = "CLEAN_WIN"
    # Positions that required cleanup/recovery at entry get a RECOVERED_ prefix so
    # they can be tracked separately from clean fills with the same settlement outcome.
    if exec_ev.get("outcome") == "FILLED_WITH_CLEANUP":
        return f"RECOVERED_{base}"
    return base

# ─── Section 1: Session Summary ───────────────────────────────────────────────

def section_summary(events, journal_path, mode_filter):
    hdr("1. Session Summary")

    evs_by_type = defaultdict(list)
    for e in events:
        evs_by_type[e.get("event", "")].append(e)

    ts_list = [parse_dt(e.get("t")) for e in events]
    ts_list = [t for t in ts_list if t]
    t_start  = min(ts_list) if ts_list else None
    t_end    = max(ts_list) if ts_list else None
    dur_str  = fmt_dur((t_end - t_start).total_seconds()) if t_start and t_end else "?"

    all_pairs = set(
        extract_ticker(e.get("pairId") or e.get("pair") or "")
        for e in events
        if e.get("pairId") or e.get("pair")
    )
    all_pairs.discard("")

    mode_str = {"dry_run": "dry-run only", "live": "live only"}.get(mode_filter, "all")
    print(f"  Journal:  {Path(journal_path).name}")
    if t_start:
        print(f"  Period:   {t_start.strftime('%Y-%m-%d %H:%M:%S')} → "
              f"{t_end.strftime('%Y-%m-%d %H:%M:%S')}  ({dur_str})")
    print(f"  Mode:     {mode_str}")
    print(f"  Pairs:    {len(all_pairs)} unique ticker(s)")

    intents = evs_by_type["INTENT"]
    execs   = evs_by_type["EXECUTION_COMPLETE"]
    halts   = evs_by_type["HALT_TRIPWIRE"]
    cleanups= evs_by_type["CLEANUP_REVERSED"] + evs_by_type["CLEANUP_DUST"]
    mismatches = evs_by_type["RECONCILE_MISMATCH"]

    print(f"  Intents:  {len(intents)}")

    if execs:
        fills  = [e for e in execs if e.get("outcome") in ("FILLED", "FILLED_WITH_CLEANUP")]
        misses = [e for e in execs if e.get("outcome") == "MISS"]
        haltd  = [e for e in execs if e.get("outcome") == "HALTED"]
        print(f"  Fills:    {len(fills)}  ({fmt_pct(len(fills), len(execs))} fill rate)")
        if misses: print(f"  Misses:   {len(misses)}")
        if haltd:  print(f"  Halted:   {len(haltd)}")
    else:
        fills_legacy = evs_by_type["FILLED"]
        misses_legacy= evs_by_type["MISS"]
        print(f"  Fills:    {len(fills_legacy)}  (legacy FILLED events; no EXECUTION_COMPLETE yet)")
        if misses_legacy: print(f"  Misses:   {len(misses_legacy)}")

    if halts:
        reasons = ", ".join(sorted(set(h.get("reason", "?") for h in halts)))
        print(f"  Halts:    {len(halts)}  ({reasons})")
    if cleanups:
        cleanup_cost = sum(e.get("loss", e.get("absorbedUsd", 0.0)) for e in cleanups)
        print(f"  Cleanup:  {len(cleanups)} event(s)  (${cleanup_cost:.4f} cost)")
    if mismatches:
        print(f"  Reconcile mismatches: {len(mismatches)}")

# ─── Section 2: P&L Analysis ──────────────────────────────────────────────────

def section_pnl(events):
    hdr("2. P&L Analysis")

    execs = [e for e in events
             if e.get("event") == "EXECUTION_COMPLETE"
             and e.get("outcome") in ("FILLED", "FILLED_WITH_CLEANUP")]

    if not execs:
        # Fall back to legacy FILLED events
        filled = [e for e in events if e.get("event") == "FILLED" and e.get("balanced", 0) > 0]
        if not filled:
            print("  No filled executions recorded.")
            return {}
        total_proj = sum(
            float(e.get("balanced", 0)) * (1.0 - float(e.get("actualNet") or 1.0))
            for e in filled
            if e.get("actualNet") is not None
        )
        print(f"  Legacy FILLED events: {len(filled)}  (limited data — no EXECUTION_COMPLETE)")
        print(f"  Projected profit:     ${total_proj:.4f}  (based on actualNet per set)")
        return {"total_proj": total_proj}

    pair_stats = defaultdict(lambda: {"fills": 0, "balanced": 0.0, "cost": 0.0, "proj": 0.0})
    total_cost = 0.0
    total_proj = 0.0

    for e in execs:
        pos    = e.get("position") or {}
        k_held = float(pos.get("kHeld", 0))
        cost   = float(pos.get("totalCostUsd", 0))
        proj   = float(pos.get("projectedProfitUsd", 0))
        ticker = extract_ticker(e.get("pairId", ""))
        pair_stats[ticker]["fills"]    += 1
        pair_stats[ticker]["balanced"] += k_held
        pair_stats[ticker]["cost"]     += cost
        pair_stats[ticker]["proj"]     += proj
        total_cost += cost
        total_proj += proj

    cleanups     = [e for e in events if e.get("event") in ("CLEANUP_REVERSED", "CLEANUP_DUST")]
    cleanup_loss = sum(float(e.get("loss", e.get("absorbedUsd", 0))) for e in cleanups)

    daily_reports = [e for e in events if e.get("event") == "DAILY_REPORT"]
    total_modeled = sum(float(r.get("modeledFeesUsd", 0)) for r in daily_reports)
    total_net_var = sum(float(r.get("netVarUsd", 0))      for r in daily_reports)

    print(f"  Filled executions:    {len(execs)}")
    print(f"  Total cost deployed:  ${total_cost:.2f}")
    print(f"  Projected profit:    +${total_proj:.4f}")
    print(f"  Cleanup losses:      −${cleanup_loss:.4f}")
    print(f"  Net projected:        ${total_proj - cleanup_loss:.4f}")

    if total_modeled > 0:
        drift     = abs(total_net_var) / total_modeled
        drift_tag = "  ⚠ DRIFT" if drift > 0.10 else "  ✓"
        print(f"\n  Fee tracking:  modeled ${total_modeled:.2f}  "
              f"var {total_net_var:+.2f}  drift {drift:.1%}{drift_tag}")

    sorted_pairs = sorted(pair_stats.items(), key=lambda x: -x[1]["proj"])
    if sorted_pairs:
        print(f"\n  Per-pair breakdown:")
        for ticker, s in sorted_pairs[:10]:
            print(f"    {ticker:<38}  {s['fills']:>2} fills  "
                  f"bal={s['balanced']:.1f}  cost=${s['cost']:.2f}  proj={s['proj']:+.4f}")

    return {"total_cost": total_cost, "total_proj": total_proj, "cleanup_loss": cleanup_loss}

# ─── Section 3: Fill & Execution Quality ─────────────────────────────────────

def section_execution(events):
    hdr("3. Fill & Execution Quality")

    execs = [e for e in events if e.get("event") == "EXECUTION_COMPLETE"]
    if not execs:
        print("  No EXECUTION_COMPLETE events — run bot with latest version to collect.")
        return

    fills   = [e for e in execs if e.get("outcome") in ("FILLED", "FILLED_WITH_CLEANUP")]
    misses  = [e for e in execs if e.get("outcome") == "MISS"]
    cleanup = [e for e in execs if e.get("outcome") == "FILLED_WITH_CLEANUP"]

    print(f"  Overall fill rate:     {fmt_pct(len(fills), len(execs))}  ({len(fills)}/{len(execs)})")
    if cleanup:
        print(f"  Fills with cleanup:    {len(cleanup)}  ({fmt_pct(len(cleanup), len(fills))} of fills)")

    if fills:
        slippages, profitable, losses = [], 0, 0
        poly_slips, durations         = [], []

        for e in fills:
            pos         = e.get("position") or {}
            actual_net  = float(pos.get("actualNetPerSet",  0))
            modeled_net = float(pos.get("modeledNetPerSet", actual_net))
            slippages.append(actual_net - modeled_net)
            if actual_net < 1.0: profitable += 1
            else:                losses     += 1

            pf = (e.get("fills") or {}).get("poly") or {}
            sp = pf.get("slippagePct")
            if sp is not None:
                poly_slips.append(float(sp))

            dur = e.get("durationMs")
            if dur:
                durations.append(int(dur))

        avg_slip = sum(slippages) / len(slippages) if slippages else 0
        print(f"  Avg net cost drift:    {avg_slip:+.4f}  (modeled→actual per set)")
        print(f"  Profitable fills:      {profitable}/{len(fills)}  "
              f"({fmt_pct(profitable, len(fills))})  — actualNet < 1.00")
        if losses:
            print(f"  Slippage losses:       {losses}/{len(fills)}  "
                  f"({fmt_pct(losses, len(fills))})  — actualNet ≥ 1.00")

        if poly_slips:
            print(f"  Poly slippage:         avg {sum(poly_slips)/len(poly_slips):+.3f}%  "
                  f"max {max(poly_slips):+.3f}%")

        if durations:
            avg_d = sum(durations) / len(durations)
            print(f"  Exec duration:         avg {avg_d:.0f}ms  "
                  f"max {max(durations)}ms  min {min(durations)}ms")

        # Recovery breakdown
        rec_counts = defaultdict(int)
        for e in fills:
            rec = (e.get("hedge") or {}).get("recovery")
            if isinstance(rec, dict):
                rec_counts[rec.get("outcome", "?")] += 1
        if rec_counts:
            print(f"\n  Recovery outcomes:")
            for outcome, count in sorted(rec_counts.items(), key=lambda x: -x[1]):
                print(f"    {outcome:<32}  {count}")

    # Per-pair fill rates
    pair_i, pair_f = defaultdict(int), defaultdict(int)
    for e in execs:
        t = extract_ticker(e.get("pairId", ""))
        pair_i[t] += 1
        if e.get("outcome") in ("FILLED", "FILLED_WITH_CLEANUP"):
            pair_f[t] += 1

    low_fill = [(t, pair_f[t], pair_i[t]) for t in pair_i
                if pair_i[t] > 0 and pair_f[t] / pair_i[t] < 0.30]
    if low_fill:
        print(f"\n  Low fill rate pairs (<30%):")
        for t, f, i in sorted(low_fill, key=lambda x: x[1] / x[2]):
            print(f"    {t:<40}  {f}/{i}  ({fmt_pct(f, i)})")

# ─── Section 4: Cleanup Cost Analysis ────────────────────────────────────────

def section_cleanup(events):
    hdr("4. Cleanup Cost Analysis")

    reversed_ev = [e for e in events if e.get("event") == "CLEANUP_REVERSED"]
    dust_ev     = [e for e in events if e.get("event") == "CLEANUP_DUST"]

    if not reversed_ev and not dust_ev:
        print("  No cleanup events recorded.")
        return

    k_rev = [e for e in reversed_ev if e.get("leg") == "kalshi"]
    p_rev = [e for e in reversed_ev if e.get("leg") == "poly"]
    k_rev_loss = sum(float(e.get("loss", 0)) for e in k_rev)
    p_rev_loss = sum(float(e.get("loss", 0)) for e in p_rev)
    dust_loss  = sum(float(e.get("absorbedUsd", 0)) for e in dust_ev)
    total      = k_rev_loss + p_rev_loss + dust_loss

    print(f"  Cleanup events:  {len(reversed_ev) + len(dust_ev)}  "
          f"({len(reversed_ev)} reversed, {len(dust_ev)} dust absorbed)")
    print(f"  Total cost:      ${total:.4f}")
    if k_rev:    print(f"    Kalshi reversals:  ${k_rev_loss:.4f}  ({len(k_rev)} events)")
    if p_rev:    print(f"    Poly reversals:    ${p_rev_loss:.4f}  ({len(p_rev)} events)")
    if dust_ev:  print(f"    Dust absorbed:     ${dust_loss:.4f}  ({len(dust_ev)} events)")

    # Hedge completions from EXECUTION_COMPLETE recovery field
    hedge_comps = [
        e for e in events
        if e.get("event") == "EXECUTION_COMPLETE"
        and isinstance((e.get("hedge") or {}).get("recovery"), dict)
        and e["hedge"]["recovery"].get("outcome") == "HEDGE_COMPLETED"
    ]
    if hedge_comps:
        total_qty = sum(float((e["hedge"]["recovery"]).get("qty", 0)) for e in hedge_comps)
        print(f"\n  Hedge completions (imbalance filled via retry): "
              f"{len(hedge_comps)} events  +{total_qty:.1f} contracts recovered at no loss")

# ─── Section 5: Fee Model Tracking ───────────────────────────────────────────

def section_fees(events):
    hdr("5. Fee Model Tracking")

    daily_reports = [e for e in events if e.get("event") == "DAILY_REPORT"]

    if not daily_reports:
        # Per-execution fallback
        execs = [e for e in events
                 if e.get("event") == "EXECUTION_COMPLETE"
                 and (e.get("position") or {}).get("actualNetPerSet") is not None]
        if not execs:
            print("  No fee model data available yet.")
            return
        var_total = sum(
            (float(e["position"]["actualNetPerSet"]) - float(e["position"]["modeledNetPerSet"])) *
            float(e["position"].get("kHeld", 0))
            for e in execs
            if e["position"].get("modeledNetPerSet") is not None
        )
        print(f"  No DAILY_REPORT events yet (emitted on day rollover).")
        print(f"  Session net cost variance (all fills): {var_total:+.4f}")
        return

    print(f"  Daily reports: {len(daily_reports)}")
    any_drift = False
    for r in sorted(daily_reports, key=lambda x: x.get("t", "")):
        date_str = r.get("t", "?")[:10]
        n        = int(r.get("trades", 0))
        modeled  = float(r.get("modeledFeesUsd", 0))
        var      = float(r.get("netVarUsd",      0))
        drift    = float(r.get("driftPct",        0))
        tag      = "  ⚠ DRIFT" if drift > 10 else "  ✓"
        print(f"    {date_str}:  {n:>3} trades  "
              f"modeled=${modeled:.2f}  var={var:+.2f}  drift={drift:.1f}%{tag}")
        if drift > 10:
            any_drift = True

    if any_drift:
        print(f"\n  [ACTION REQUIRED] Fee model drift >10% — audit KalshiFee() / PolyFee() formulas.")

# ─── Section 6: Reconciliation & Alerts ──────────────────────────────────────

def section_reconcile(events):
    hdr("6. Reconciliation & Alerts")

    mismatches = [e for e in events if e.get("event") == "RECONCILE_MISMATCH"]
    halts      = [e for e in events if e.get("event") == "HALT_TRIPWIRE"]

    if not mismatches and not halts:
        print("  No reconciliation mismatches or halt events recorded.  ✓")
        return

    if mismatches:
        print(f"  Reconcile mismatches: {len(mismatches)}")
        for m in mismatches:
            t_str = m.get("t", "?")[:19]
            pair  = extract_ticker(m.get("pair", "?"))
            k_exp = m.get("kExpected"); k_ven = m.get("kVenue")
            p_exp = m.get("pExpected"); p_ven = m.get("pVenue")
            k_tag = "✓" if k_exp == k_ven else f"⚠ local={k_exp}  venue={k_ven}"
            try:
                p_tag = "✓" if abs(float(p_exp or 0) - float(p_ven or 0)) < 0.01 \
                        else f"⚠ local={p_exp}  venue={p_ven}"
            except (TypeError, ValueError):
                p_tag = f"? local={p_exp}  venue={p_ven}"
            print(f"\n    {t_str}  {pair}")
            print(f"      Kalshi:  {k_tag}")
            print(f"      Poly:    {p_tag}")

    if halts:
        print(f"\n  Halt events: {len(halts)}")
        for h in sorted(halts, key=lambda x: x.get("t", "")):
            t_str  = h.get("t", "?")[:19]
            reason = h.get("reason", "?")
            pair   = extract_ticker(h.get("pairId", "?"))
            if reason == "per_trade_loss":
                detail = (f"  actualNet={h.get('actualNet', '?')}  "
                          f"maxAllowed={h.get('maxAllowed', '?')}")
            elif reason == "per_day_loss":
                detail = (f"  dayLoss=${float(h.get('dayLoss', 0)):.2f}  "
                          f"max=${float(h.get('maxDayLoss', 0)):.2f}")
            else:
                detail = ""
            print(f"    {t_str}  {reason:<22}  {pair}{detail}")

# ─── Section 7: Settlement & Post-Mortem ─────────────────────────────────────

def section_settlement(events, pairs_by_ticker, no_api):
    hdr("7. Settlement & Post-Mortem")

    # Use EXECUTION_COMPLETE with a real position as the source of truth
    filled = [e for e in events
              if e.get("event") == "EXECUTION_COMPLETE"
              and e.get("outcome") in ("FILLED", "FILLED_WITH_CLEANUP")
              and float((e.get("position") or {}).get("kHeld", 0)) > 0]

    if not filled:
        print("  No filled positions to settle.")
        return []

    if no_api:
        print(f"  {len(filled)} position(s) found.  (--no-api: skipping settlement API calls)")
        print(f"  Remove --no-api to query Kalshi + Poly for resolution status.")
        return []

    cache            = load_cache()
    categories       = defaultdict(list)
    mismatch_tickers = []

    print(f"  Checking settlement for {len(filled)} position(s) ...")
    for i, e in enumerate(filled, 1):
        pair_id  = e.get("pairId", "")
        arb_type = e.get("arbType", "")
        ticker   = extract_ticker(pair_id)
        pair_rec = pairs_by_ticker.get(ticker)

        poly_token = None
        if pair_rec:
            poly_token = (pair_rec.get("poly_no_token")  if arb_type == "K_YES_P_NO"
                          else pair_rec.get("poly_yes_token"))

        k_result     = fetch_kalshi_resolution(ticker, cache)
        k_resolved   = k_result is not None
        p_token_wins = fetch_poly_token_wins(poly_token, cache) if poly_token else None
        p_resolved   = p_token_wins is not None

        category = categorize_settlement(e, k_result, k_resolved, p_token_wins, p_resolved)
        categories[category].append(e)

        if category in ("PAIR_MISMATCH_BOTH_LOST", "PAIR_MISMATCH_BOTH_WON"):
            if ticker not in mismatch_tickers:
                mismatch_tickers.append(ticker)

        if i % 5 == 0:
            save_cache(cache)
        time.sleep(0.15)

    save_cache(cache)

    total_settled = sum(len(v) for k, v in categories.items() if k != "PENDING")
    total_pending = len(categories.get("PENDING", []))
    print(f"\n  Settled: {total_settled} of {len(filled)} positions  |  Pending: {total_pending}")
    print()

    base_cats = [
        "CLEAN_WIN",
        "EXECUTION_LOSS",
        "FEE_MODEL_LOSS",
        "PAIR_MISMATCH_BOTH_WON",
        "PAIR_MISMATCH_BOTH_LOST",
        "PENDING",
    ]
    # Append any RECOVERED_* categories that actually appeared in this journal
    recovered_cats = sorted(c for c in categories if c.startswith("RECOVERED_"))
    cat_order = base_cats + recovered_cats

    realized_pnl  = 0.0
    action_needed = []

    for cat in cat_order:
        evs = categories.get(cat, [])
        if not evs:
            continue
        cat_pnl = sum(
            float(pos.get("kHeld", 0)) * (1.0 - float(pos.get("actualNetPerSet", 1.0)))
            for e in evs
            for pos in [(e.get("position") or {})]
        ) if cat != "PENDING" else 0.0
        realized_pnl += cat_pnl

        pnl_str    = f"  P&L {cat_pnl:+.4f}" if cat != "PENDING" else ""
        flag_parts = []
        base_cat   = cat.replace("RECOVERED_", "")
        if base_cat == "EXECUTION_LOSS" and evs:
            flag_parts.append("[ACTION REQUIRED] review execution code")
            action_needed.append("execution")
        if base_cat == "FEE_MODEL_LOSS" and evs:
            flag_parts.append("[ACTION REQUIRED] update fee model")
            action_needed.append("fee_model")
        if base_cat.startswith("PAIR_MISMATCH") and evs:
            flag_parts.append("[ACTION REQUIRED] BLOCKLISTING")
            action_needed.append("blocklist")
        if cat.startswith("RECOVERED_"):
            flag_parts.append("cleanup required at entry")
        flag_str = ("  ← " + " + ".join(flag_parts)) if flag_parts else ""

        print(f"  {cat:<32}  {len(evs):>3}{pnl_str}{flag_str}")

    if total_settled > 0:
        print(f"\n  Net realized P&L (settled): {'+' if realized_pnl >= 0 else ''}${realized_pnl:.4f}")

    # Per-pair breakdown
    pair_settled = defaultdict(lambda: defaultdict(int))
    pair_pnl     = defaultdict(float)
    for cat, evs in categories.items():
        if cat == "PENDING":
            continue
        for e in evs:
            t = extract_ticker(e.get("pairId", ""))
            pair_settled[t][cat] += 1
            pos = e.get("position") or {}
            pair_pnl[t] += float(pos.get("kHeld", 0)) * (1.0 - float(pos.get("actualNetPerSet", 1.0)))

    if pair_settled:
        print(f"\n  Per-pair settled:")
        for t in sorted(pair_settled, key=lambda x: -abs(pair_pnl[x])):
            cats_str = "  ".join(f"{c}×{n}" for c, n in pair_settled[t].items())
            pnl_v    = pair_pnl[t]
            print(f"    {t:<40}  {cats_str}  {'+' if pnl_v >= 0 else ''}${pnl_v:.4f}")

    if mismatch_tickers:
        print(f"\n  [ACTION REQUIRED] Pair mismatch — blocklisting {len(mismatch_tickers)} ticker(s):")
        for t in mismatch_tickers:
            print(f"    {t}")
        update_blocklist(mismatch_tickers)
    elif "execution" in action_needed:
        print(f"\n  [ACTION REQUIRED] Review execution code for slippage causes.")
    elif "fee_model" in action_needed:
        print(f"\n  [ACTION REQUIRED] Update fee model — actual fees exceed modeled.")

    return mismatch_tickers

# ─── Section 8: Pair Health & Position Limits ─────────────────────────────────

def section_pair_health(events, pairs_by_ticker):
    hdr("8. Pair Health & Position Limits")

    execs = [e for e in events
             if e.get("event") == "EXECUTION_COMPLETE"
             and e.get("outcome") in ("FILLED", "FILLED_WITH_CLEANUP")]

    if execs:
        # Latest execution per pair (approximates open position state)
        by_pair = defaultdict(list)
        for e in execs:
            by_pair[extract_ticker(e.get("pairId", ""))].append(e)

        total_exposure = 0.0
        rows = []
        now  = datetime.now(timezone.utc)

        for ticker, evs in by_pair.items():
            latest  = evs[-1]
            pos     = latest.get("position") or {}
            k_held  = float(pos.get("kHeld", 0))
            cost    = float(pos.get("totalCostUsd", 0))
            proj    = float(pos.get("projectedProfitUsd", 0))
            entry_t = parse_dt(latest.get("t", ""))
            age_str = fmt_dur((now - entry_t).total_seconds()) if entry_t else "?"
            label   = latest.get("label", ticker)[:45]
            total_exposure += cost
            rows.append((ticker, k_held, cost, proj, age_str, label,
                         len(evs), sum(1 for e in evs
                                       if e.get("outcome") == "FILLED_WITH_CLEANUP")))

        if rows:
            print(f"  Latest execution per pair ({len(rows)} pairs):")
            for (ticker, k, cost, proj, age, label, n_fills, n_cleanup) in \
                    sorted(rows, key=lambda x: -x[2]):
                conc     = cost / total_exposure * 100 if total_exposure else 0
                conc_tag = "  ⚠ HIGH CONC" if conc > 30 else ""
                clean_tag= f"  {n_cleanup} cleanup(s)" if n_cleanup else ""
                print(f"    {ticker:<38}  K={k:.1f}  "
                      f"cost=${cost:.2f} ({conc:.0f}%)  "
                      f"proj={proj:+.4f}  age={age}"
                      f"{conc_tag}{clean_tag}")

            print(f"\n  Total open exposure:  ${total_exposure:.2f}")
            high = [(t, c / total_exposure * 100)
                    for (t, _, c, _, _, _, _, _) in rows
                    if total_exposure and c / total_exposure > 0.30]
            if high:
                print(f"  ⚠ High concentration pairs (>30%):")
                for t, pct in high:
                    print(f"    {t}: {pct:.0f}%")
            else:
                print(f"  Concentration: no pair >30% of exposure  ✓")
    else:
        print("  No filled executions.")

    # Blocklist
    blocklist = []
    if BLOCKLIST_FILE.exists():
        with open(BLOCKLIST_FILE, encoding="utf-8") as f:
            blocklist = json.load(f)
    status = f"{len(blocklist)} ticker(s)" if blocklist else "empty  ✓"
    print(f"\n  Blocklist ({BLOCKLIST_FILE.name}): {status}")
    for t in blocklist:
        print(f"    {t}")

# ─── Blocklist Update ─────────────────────────────────────────────────────────

def update_blocklist(mismatch_tickers):
    existing = []
    if BLOCKLIST_FILE.exists():
        with open(BLOCKLIST_FILE, encoding="utf-8") as f:
            existing = json.load(f)
    new_entries = [t for t in mismatch_tickers if t not in existing]
    if new_entries:
        existing.extend(new_entries)
        with open(BLOCKLIST_FILE, "w", encoding="utf-8") as f:
            json.dump(existing, f, indent=2)
        print(f"  [BLOCKLIST] Added {len(new_entries)} ticker(s) → {BLOCKLIST_FILE.name}")
    else:
        print(f"  [BLOCKLIST] All mismatch tickers already in blocklist.")

# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Cross-arb live execution analyzer",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--file",    metavar="PATH", help="Explicit journal .jsonl path")
    parser.add_argument("--dry-run", action="store_true", help="Show only dry-run executions")
    parser.add_argument("--live",    action="store_true", help="Show only live executions")
    parser.add_argument("--pair",    metavar="TICKER",    help="Filter to one kalshi_ticker")
    parser.add_argument("--no-api",  action="store_true", help="Skip settlement API calls")
    parser.add_argument("--since",   metavar="DATE",      help="Only events after date (ISO)")
    args = parser.parse_args()

    if args.dry_run and args.live:
        print("ERROR: --dry-run and --live are mutually exclusive.")
        sys.exit(1)

    journal_path = args.file or find_latest_journal()
    if not journal_path:
        print("ERROR: No CrossArbJournal_*.jsonl found in KalshiPolyCross/ or bin/.")
        print("  Use --file to specify the path explicitly.")
        sys.exit(1)
    if not os.path.exists(journal_path):
        print(f"ERROR: File not found: {journal_path}")
        sys.exit(1)

    print(f"\nLoading {journal_path} ...")
    events = load_journal(journal_path)
    if not events:
        print("No events found in journal.")
        sys.exit(0)

    # ── Filters ───────────────────────────────────────────────────────────────
    mode_filter = None
    if args.dry_run or args.live:
        mode_filter = "dry_run" if args.dry_run else "live"
        want_dry    = args.dry_run
        def keep_mode(e):
            if e.get("event") == "EXECUTION_COMPLETE":
                return bool(e.get("dryRun", False)) == want_dry
            return True
        events = [e for e in events if keep_mode(e)]

    if args.pair:
        events = [e for e in events
                  if args.pair in (e.get("pairId") or e.get("pair") or
                                   e.get("execId") or "")]

    if args.since:
        try:
            since_dt = datetime.fromisoformat(args.since)
            if since_dt.tzinfo is None:
                since_dt = since_dt.replace(tzinfo=timezone.utc)
            events = [e for e in events
                      if (parse_dt(e.get("t")) or
                          datetime.min.replace(tzinfo=timezone.utc)) >= since_dt]
        except ValueError:
            print(f"ERROR: Invalid --since date '{args.since}' — use ISO format e.g. 2026-05-01")
            sys.exit(1)

    print(f"  {len(events)} event(s) after filters\n")

    pairs_by_ticker = load_pairs()
    if not pairs_by_ticker and not args.no_api:
        print("  [warn] cross_pairs.json not found — settlement section will skip token lookup")

    # ── Sections ──────────────────────────────────────────────────────────────
    section_summary(events, journal_path, mode_filter)
    section_pnl(events)
    section_execution(events)
    section_cleanup(events)
    section_fees(events)
    section_reconcile(events)
    mismatch_tickers = section_settlement(events, pairs_by_ticker, args.no_api)
    section_pair_health(events, pairs_by_ticker)

    print(f"\n{SEP2}")
    print(f"  Analysis complete.")
    if mismatch_tickers:
        print(f"  ⚠  {len(mismatch_tickers)} pair(s) blocklisted — restart bot to take effect.")
    print(SEP2)


if __name__ == "__main__":
    main()
