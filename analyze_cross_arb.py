"""
Cross-Platform Arb Telemetry Analyzer
Reads CrossArbTelemetry_*.csv (and optionally CrossArbBlended_*.csv) from the
KalshiPolyCross bot and produces:
  1. Session summary
  2. All pairs detected (clean and flagged)
  3. Fraud / sanity checks
  4. Duration analysis
  5. Profit analysis
  6. Arb type breakdown (K_YES_P_NO vs K_NO_P_YES)
  7. WS reliability
  8. REST verification
  9. Blended arb summary (if CrossArbBlended_*.csv found)
 10. Production sim (US server, competition-adjusted — LAST)

Usage:
  python analyze_cross_arb.py
  python analyze_cross_arb.py --file path/to/CrossArbTelemetry_xxx.csv
  python analyze_cross_arb.py --min-duration 50
  python analyze_cross_arb.py --exclude "TRUMP,NBA"
  python analyze_cross_arb.py --include "EPL,NFL"
  python analyze_cross_arb.py --clean
  python analyze_cross_arb.py --participation-rate 0.5
"""

import csv
import sys
import glob
import os
import re
import argparse
from collections import defaultdict
from datetime import datetime, timedelta

# ─── CONFIG ───────────────────────────────────────────────────────────────────
DEFAULT_MIN_DURATION_MS = 17     # US server: ~6ms one-way + ~5ms processing
DEFAULT_CAPITAL_PER_ARB = 50.0
DEFAULT_CAPTURE_RATE    = 0.60
THIN_DEPTH_THRESHOLD    = 2.0
STALE_BOOK_MS           = 30_000
REPEAT_SPAM_THRESHOLD   = 100    # >N windows for same pair = spam

PROD_LATENCY_MS         = 17     # US server min capturable window

# ─── FILE DISCOVERY ───────────────────────────────────────────────────────────

def find_latest_csv(pattern):
    candidates = (
        glob.glob(pattern) +
        glob.glob(f"KalshiPolyCross/{pattern}") +
        glob.glob(f"KalshiPolyCross/bin/**/{pattern}", recursive=True)
    )
    if not candidates:
        return None
    return max(candidates, key=os.path.getctime)


def find_blended_csv(binary_path):
    m = re.search(r'(\d{8}_\d{6})', os.path.basename(binary_path))
    if m:
        ts = m.group(1)
        candidates = (
            glob.glob(f"CrossArbBlended_{ts}.csv") +
            glob.glob(f"KalshiPolyCross/CrossArbBlended_{ts}.csv") +
            glob.glob(f"KalshiPolyCross/bin/**/CrossArbBlended_{ts}.csv", recursive=True)
        )
        if candidates:
            return candidates[0]
    return find_latest_csv("CrossArbBlended_*.csv")

# ─── DATETIME HELPERS ─────────────────────────────────────────────────────────

_DT_FORMATS = [
    "%Y-%m-%d %H:%M:%S.%f",
    "%Y-%m-%d %H:%M:%S",
]

def _parse_dt(s):
    for fmt in _DT_FORMATS:
        try:
            return datetime.strptime(s.strip(), fmt)
        except ValueError:
            pass
    return None

def compute_session_hours(rows, path):
    if len(rows) < 2:
        return 0.0
    dt_start = _parse_dt(rows[0]["start"])
    dt_end   = _parse_dt(rows[-1]["end"])
    if dt_start is not None and dt_end is not None:
        secs = (dt_end - dt_start).total_seconds()
        if secs <= 0:
            all_ends = [_parse_dt(r["end"]) for r in rows]
            all_ends = [d for d in all_ends if d is not None]
            if all_ends:
                secs = (max(all_ends) - dt_start).total_seconds()
        return max(secs / 3600.0, 1 / 3600.0)
    return 0.0

def _per_hr(amount, hours):
    if hours <= 0:
        return f"${amount:.4f}"
    return f"${amount:.4f}  (${amount/hours:.2f}/hr over {hours:.1f}h)"

def _hr(amount, hours):
    if hours <= 0:
        return f"${amount:+.2f}"
    return f"${amount/hours:+.2f}/hr"

# ─── SAFE FIELD PARSERS ───────────────────────────────────────────────────────

def _try_float(r, key, default=None):
    v = r.get(key, "").strip()
    if v in ("", "N/A", "n/a"):
        return default
    try:
        return float(v)
    except ValueError:
        return default

def _try_bool(r, key, default=None):
    v = r.get(key, "").strip().lower()
    if v in ("true", "1"):
        return True
    if v in ("false", "0"):
        return False
    return default

# ─── DATA LOADING ─────────────────────────────────────────────────────────────

def load_binary_csv(path):
    rows = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for r in reader:
            try:
                rows.append({
                    "start":               r["StartTime"].strip(),
                    "end":                 r["EndTime"].strip(),
                    "duration_ms":         float(r["DurationMs"]),
                    "pair_id":             r["PairId"].strip('"').strip(),
                    "label":               r["Label"].strip('"').strip(),
                    "arb_type":            r["ArbType"].strip(),
                    "entry_gross_cost":    float(r["EntryGrossCost"]),
                    "entry_net_cost":      float(r["EntryNetCost"]),
                    "best_gross_cost":     float(r["BestGrossCost"]),
                    "best_net_cost":       float(r["BestNetCost"]),
                    "total_fees":          float(r["TotalFees"]),
                    "kalshi_fees":         float(r["KalshiFees"]),
                    "poly_fees":           float(r["PolyFees"]),
                    "net_profit_per_share":float(r["NetProfitPerShare"]),
                    "kalshi_depth":        float(r["KalshiDepth"]),
                    "poly_depth":          float(r["PolyDepth"]),
                    "max_depth":           float(r["MaxDepth"]),
                    "total_capital_req":   float(r["TotalCapitalRequired"]),
                    "total_potential":     float(r["TotalPotentialProfit"]),
                    "kalshi_book_age_ms":  _try_float(r, "KalshiBookAgeMs", -1),
                    "poly_book_age_ms":    _try_float(r, "PolyBookAgeMs",   -1),
                    "kalshi_mid_sum":      _try_float(r, "KalshiMidSum"),
                    "poly_mid_sum":        _try_float(r, "PolyMidSum"),
                    "k_drops_at_open":     _try_float(r, "KalshiWsDropsAtOpen", 0),
                    "p_drops_at_open":     _try_float(r, "PolyWsDropsAtOpen",   0),
                    "drop_during":         _try_bool(r,  "DropDuringWindow", False),
                    "update_count":        int(r.get("UpdateCount", 0) or 0),
                    "closed_by":           r.get("ClosedBy", "").strip(),
                    "rest_checked":        _try_bool(r,  "RestChecked"),
                    "rest_confirmed":      _try_bool(r,  "RestConfirmed"),
                    "rest_kalshi_ask":     _try_float(r, "RestKalshiAsk"),
                    "rest_poly_ask":       _try_float(r, "RestPolyAsk"),
                    "rest_delay_ms":       _try_float(r, "RestDelayMs"),
                })
            except (KeyError, ValueError):
                continue
    return rows


def load_blended_csv(path):
    rows = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for r in reader:
            try:
                rows.append({
                    "start":             r["StartTime"].strip(),
                    "end":               r["EndTime"].strip(),
                    "duration_ms":       float(r["DurationMs"]),
                    "event_id":          r["EventId"].strip('"').strip(),
                    "num_legs":          int(r["NumLegs"]),
                    "leg_tickers":       r.get("LegTickers", "").strip('"').strip(),
                    "entry_net_cost":    float(r["EntryNetCost"]),
                    "entry_leg_choices": r.get("EntryLegChoices", "").strip('"').strip(),
                    "best_net_cost":     float(r["BestNetCost"]),
                    "best_leg_choices":  r.get("BestLegChoices", "").strip('"').strip(),
                    "best_leg_prices":   r.get("BestLegPrices", "").strip('"').strip(),
                    "min_depth":         float(r["MinDepth"]),
                    "total_capital_req": float(r["TotalCapitalRequired"]),
                    "total_potential":   float(r["TotalPotentialProfit"]),
                    "drop_during":       _try_bool(r, "DropDuringWindow", False),
                    "update_count":      int(r.get("UpdateCount", 0) or 0),
                    "closed_by":         r.get("ClosedBy", "").strip(),
                })
            except (KeyError, ValueError):
                continue
    return rows

# ─── FRAUD FLAGS ──────────────────────────────────────────────────────────────

def compute_flags(rows, spam_threshold=REPEAT_SPAM_THRESHOLD):
    pair_counts = defaultdict(int)
    for r in rows:
        pair_counts[r["pair_id"]] += 1

    for r in rows:
        flags = []
        if r["duration_ms"] < 10:
            flags.append("INSTANT_OPEN_CLOSE")
        if r["duration_ms"] > 3_600_000:
            flags.append("IMPLAUSIBLE_DURATION")
        if r["max_depth"] < THIN_DEPTH_THRESHOLD:
            flags.append("THIN_DEPTH")
        k_age = r.get("kalshi_book_age_ms", -1)
        p_age = r.get("poly_book_age_ms",   -1)
        if (k_age >= 0 and k_age > STALE_BOOK_MS) or (p_age >= 0 and p_age > STALE_BOOK_MS):
            flags.append("STALE_BOOK_OPEN")
        if r.get("drop_during"):
            flags.append("DROP_DURING_WINDOW")
        if r["net_profit_per_share"] <= 0:
            flags.append("ZERO_PROFIT")
        if pair_counts[r["pair_id"]] > spam_threshold:
            flags.append("REPEAT_SPAM")
        r["flags"] = flags

# ─── SECTION 1: SESSION SUMMARY ───────────────────────────────────────────────

def print_session_summary(rows, path, session_hours):
    print("=" * 80)
    print("  CROSS-PLATFORM ARB TELEMETRY ANALYSIS")
    print("=" * 80)
    print(f"  File     : {path}")
    print(f"  Rows     : {len(rows)}")
    if not rows:
        return
    unique_pairs    = len(set(r["pair_id"] for r in rows))
    total_potential = sum(r["total_potential"] for r in rows)
    starts = [r["start"] for r in rows]
    ends   = [r["end"]   for r in rows]
    print(f"  Pairs    : {unique_pairs} unique")
    print(f"  Duration : {session_hours:.1f}h  ({starts[0]} — {ends[-1]})")
    print(f"  Total potential profit (all windows): {_per_hr(total_potential, session_hours)}")
    print()

# ─── SECTION 2: ALL PAIRS DETECTED ───────────────────────────────────────────

def print_all_pairs(rows):
    print("=" * 80)
    print("  ALL PAIRS DETECTED")
    print("=" * 80)
    if not rows:
        print("  (no data)\n")
        return

    by_pair = defaultdict(list)
    for r in rows:
        by_pair[r["pair_id"]].append(r)

    summary = []
    for pair_id, pr in by_pair.items():
        all_flags = set()
        for r in pr:
            all_flags.update(r["flags"])
        summary.append({
            "pair_id":      pair_id,
            "label":        pr[0]["label"],
            "arb_types":    list({r["arb_type"] for r in pr}),
            "windows":      len(pr),
            "avg_duration": sum(r["duration_ms"]    for r in pr) / len(pr),
            "avg_net_cost": sum(r["best_net_cost"]   for r in pr) / len(pr),
            "avg_depth":    sum(r["max_depth"]        for r in pr) / len(pr),
            "total_pot":    sum(r["total_potential"]  for r in pr),
            "flags":        sorted(all_flags),
        })

    clean   = sorted([e for e in summary if not e["flags"]], key=lambda x: -x["total_pot"])
    flagged = sorted([e for e in summary if     e["flags"]], key=lambda x: -x["total_pot"])

    print(f"  {len(summary)} total pairs — {len(clean)} clean, {len(flagged)} flagged\n")
    hdr = f"  {'Label':<44} {'Win':>4} {'AvgMs':>7} {'AvgCost':>8} {'TotProfit':>10}  Flags"
    sep = f"  {'-'*44} {'-'*4} {'-'*7} {'-'*8} {'-'*10}  -----"

    def _row(e):
        flag_str = ",".join(e["flags"]) if e["flags"] else "OK"
        print(f"  {e['label'][:44]:<44} {e['windows']:>4} "
              f"{e['avg_duration']:>7.0f} {e['avg_net_cost']:>8.4f} "
              f"${e['total_pot']:>9.4f}  {flag_str}")

    if clean:
        print(f"  CLEAN ({len(clean)})")
        print(hdr); print(sep)
        for e in clean:
            _row(e)
        print()

    if flagged:
        print(f"  FLAGGED ({len(flagged)})")
        print(hdr); print(sep)
        for e in flagged:
            _row(e)
    print()

# ─── SECTION 3: FRAUD / SANITY CHECKS ────────────────────────────────────────

def print_fraud_checks(rows, spam_threshold=REPEAT_SPAM_THRESHOLD):
    print("=" * 80)
    print("  FRAUD / SANITY CHECKS")
    print("=" * 80)
    if not rows:
        print("  (no data)\n")
        return

    all_flag_names = [
        "INSTANT_OPEN_CLOSE", "IMPLAUSIBLE_DURATION", "THIN_DEPTH",
        "STALE_BOOK_OPEN", "DROP_DURING_WINDOW", "ZERO_PROFIT", "REPEAT_SPAM",
    ]
    descriptions = {
        "INSTANT_OPEN_CLOSE":   "Duration < 10ms — single-tick glitch",
        "IMPLAUSIBLE_DURATION": "Duration > 1 hour — likely stale book across reconnect",
        "THIN_DEPTH":           f"MaxDepth < {THIN_DEPTH_THRESHOLD} — resting 1-contract noise",
        "STALE_BOOK_OPEN":      f"K or P book age > {STALE_BOOK_MS//1000}s at window open — data may be stale",
        "DROP_DURING_WINDOW":   "WS reconnect happened while arb window was open — price may be ghost",
        "ZERO_PROFIT":          "NetProfitPerShare <= 0 — should not be in file",
        "REPEAT_SPAM":          f"Same pair appears > {spam_threshold}x — cycling open/close",
    }

    total = len(rows)
    any_flags = False
    for flag in all_flag_names:
        count = sum(1 for r in rows if flag in r["flags"])
        if count > 0:
            any_flags = True
            pct = count / total * 100
            print(f"  [{flag:<22}]  {count:4d} rows ({pct:5.1f}%)  — {descriptions[flag]}")

    if not any_flags:
        print("  No fraud flags found.")

    stale_rows = [r for r in rows if "STALE_BOOK_OPEN" in r["flags"]]
    if stale_rows:
        print(f"\n  STALE_BOOK_OPEN detail (first 5):")
        for r in stale_rows[:5]:
            k_age = r.get("kalshi_book_age_ms", -1)
            p_age = r.get("poly_book_age_ms",   -1)
            print(f"    {r['label'][:48]}  K_age={k_age:.0f}ms  P_age={p_age:.0f}ms")

    drop_rows = [r for r in rows if "DROP_DURING_WINDOW" in r["flags"]]
    if drop_rows:
        print(f"\n  DROP_DURING_WINDOW detail (first 5):")
        for r in drop_rows[:5]:
            print(f"    {r['label'][:48]}  {r['duration_ms']:.0f}ms  cost=${r['best_net_cost']:.4f}")
    print()

# ─── SECTION 4: DURATION ANALYSIS ────────────────────────────────────────────

def print_duration_analysis(rows, min_duration_ms):
    print("=" * 80)
    print("  DURATION ANALYSIS")
    print("=" * 80)
    if not rows:
        print("  (no data)\n")
        return

    buckets = [
        ("< 50ms",       0,       50),
        ("50-200ms",     50,      200),
        ("200-500ms",    200,     500),
        ("500ms-2s",     500,     2000),
        ("2s-10s",       2000,    10000),
        ("10s-60s",      10000,   60000),
        ("> 60s",        60000,   float("inf")),
    ]
    total     = len(rows)
    durations = sorted(r["duration_ms"] for r in rows)

    print(f"  {'Bucket':<16} {'Count':>6}  {'%':>6}")
    print(f"  {'-'*16} {'-'*6}  {'-'*6}")
    for label, lo, hi in buckets:
        count  = sum(1 for d in durations if lo <= d < hi)
        pct    = count / total * 100 if total else 0
        marker = " <-- capturable threshold" if lo == min_duration_ms or (lo < min_duration_ms <= hi) else ""
        print(f"  {label:<16} {count:>6}  {pct:>5.1f}%{marker}")

    capturable = sum(1 for r in rows if r["duration_ms"] >= min_duration_ms)
    median = durations[len(durations) // 2]
    p90    = durations[int(len(durations) * 0.90)]
    maxd   = durations[-1]
    print()
    print(f"  Capturable (>= {min_duration_ms}ms): {capturable} / {total}  ({capturable/total*100:.1f}%)")
    print(f"  Median: {median:.0f}ms   p90: {p90:.0f}ms   Max: {maxd:.0f}ms")
    print()

# ─── SECTION 5: PROFIT ANALYSIS ───────────────────────────────────────────────

def print_profit_analysis(rows, min_duration_ms, capital_per_arb, capture_rate, session_hours):
    print("=" * 80)
    print("  PROFIT ANALYSIS")
    print("=" * 80)
    if not rows:
        print("  (no data)\n")
        return

    total_potential  = sum(r["total_potential"] for r in rows)
    capturable_rows  = [r for r in rows if r["duration_ms"] >= min_duration_ms]
    cap_potential    = sum(r["total_potential"] for r in capturable_rows)

    realistic_profit = 0.0
    for r in capturable_rows:
        fill = min(1.0, capital_per_arb / r["total_capital_req"]) if r["total_capital_req"] > 0 else 1.0
        realistic_profit += r["total_potential"] * fill * capture_rate

    avg_per_window = total_potential / len(rows) if rows else 0
    avg_per_share  = sum(r["net_profit_per_share"] for r in rows) / len(rows) if rows else 0
    best_window    = max(rows, key=lambda r: r["total_potential"])

    print(f"  Total potential profit (ALL windows):        {_per_hr(total_potential, session_hours)}")
    print(f"  Total potential profit (capturable windows): {_per_hr(cap_potential,   session_hours)}")
    print(f"  Realistic (capped ${capital_per_arb:.0f}, {capture_rate*100:.0f}% capture):       {_per_hr(realistic_profit, session_hours)}")
    print()
    print(f"  Avg profit / window:  ${avg_per_window:.4f}")
    print(f"  Avg profit / share:   ${avg_per_share:.4f}")
    print(f"  Best single window:   ${best_window['total_potential']:.4f}  ({best_window['label']}  {best_window['duration_ms']:.0f}ms)")
    print()

    buckets = [
        ("< $0.01",     0,     0.01),
        ("$0.01-0.05",  0.01,  0.05),
        ("$0.05-0.25",  0.05,  0.25),
        ("$0.25-1.00",  0.25,  1.00),
        ("> $1.00",     1.00,  float("inf")),
    ]
    total = len(rows)
    print(f"  Potential profit distribution:")
    print(f"  {'Bucket':<16} {'Count':>6}  {'%':>6}")
    print(f"  {'-'*16} {'-'*6}  {'-'*6}")
    for label, lo, hi in buckets:
        count = sum(1 for r in rows if lo <= r["total_potential"] < hi)
        pct   = count / total * 100 if total else 0
        print(f"  {label:<16} {count:>6}  {pct:>5.1f}%")
    print()

# ─── SECTION 6: ARB TYPE BREAKDOWN ───────────────────────────────────────────

def print_arb_type_breakdown(rows):
    print("=" * 80)
    print("  ARB TYPE BREAKDOWN")
    print("=" * 80)
    if not rows:
        print("  (no data)\n")
        return

    types = ["K_YES_P_NO", "K_NO_P_YES"]
    print(f"  {'Type':<16} {'Count':>6}  {'AvgCost':>8}  {'AvgDepth':>9}  {'TotProfit':>10}  {'AvgFees':>8}")
    print(f"  {'-'*16} {'-'*6}  {'-'*8}  {'-'*9}  {'-'*10}  {'-'*8}")
    for t in types:
        subset = [r for r in rows if r["arb_type"] == t]
        if not subset:
            continue
        avg_cost  = sum(r["best_net_cost"]  for r in subset) / len(subset)
        avg_depth = sum(r["max_depth"]       for r in subset) / len(subset)
        tot_pot   = sum(r["total_potential"] for r in subset)
        avg_fees  = sum(r["total_fees"]      for r in subset) / len(subset)
        print(f"  {t:<16} {len(subset):>6}  {avg_cost:>8.4f}  {avg_depth:>9.1f}  ${tot_pot:>9.4f}  {avg_fees:>8.4f}")
    print()

# ─── SECTION 7: WS RELIABILITY ───────────────────────────────────────────────

def print_ws_reliability(rows):
    print("=" * 80)
    print("  WS RELIABILITY")
    print("=" * 80)
    if not rows:
        print("  (no data)\n")
        return

    total     = len(rows)
    drop_rows = [r for r in rows if r.get("drop_during")]
    print(f"  Windows with WS drop during window: {len(drop_rows)} / {total}  ({len(drop_rows)/total*100:.1f}%)")
    print()

    k_ages = [r["kalshi_book_age_ms"] for r in rows if r.get("kalshi_book_age_ms", -1) >= 0]
    p_ages = [r["poly_book_age_ms"]   for r in rows if r.get("poly_book_age_ms",   -1) >= 0]

    def _age_hist(ages, name):
        if not ages:
            return
        buckets = [
            ("Fresh  (< 1s)",    0,       1_000),
            ("OK     (1-5s)",    1_000,   5_000),
            ("Slow   (5-30s)",   5_000,   30_000),
            ("Stale  (> 30s)",   30_000,  float("inf")),
        ]
        n = len(ages)
        print(f"  {name} book age at window open  (n={n}):")
        for label, lo, hi in buckets:
            cnt = sum(1 for a in ages if lo <= a < hi)
            pct = cnt / n * 100
            print(f"    {label}  {cnt:>4}  ({pct:5.1f}%)")
        ages_s = sorted(ages)
        median = ages_s[len(ages_s)//2]
        p90    = ages_s[int(len(ages_s)*0.90)]
        print(f"    Median: {median:.0f}ms   p90: {p90:.0f}ms   Max: {ages_s[-1]:.0f}ms")
        print()

    _age_hist(k_ages, "Kalshi")
    _age_hist(p_ages, "Polymarket")

# ─── SECTION 8: REST VERIFICATION ────────────────────────────────────────────

def print_rest_verification(rows):
    print("=" * 80)
    print("  REST VERIFICATION")
    print("=" * 80)

    checked     = [r for r in rows if r.get("rest_checked") is True]
    not_checked = [r for r in rows if r.get("rest_checked") is False]
    no_data     = [r for r in rows if r.get("rest_checked") is None]

    total = len(rows)
    print(f"  Total arb windows:     {total}")
    print(f"  REST-checked:          {len(checked)}  ({len(checked)/total*100:.1f}%)")
    print(f"  REST not triggered:    {len(not_checked)}  ({len(not_checked)/total*100:.1f}%)")
    if no_data:
        print(f"  No REST column in CSV: {len(no_data)}  (older CSV)")
    print()

    if not checked:
        print("  No REST-checked windows to analyze.")
        print()
        return

    confirmed   = [r for r in checked if r.get("rest_confirmed") is True]
    unconfirmed = [r for r in checked if r.get("rest_confirmed") is False]
    print(f"  REST-confirmed:    {len(confirmed)}  ({len(confirmed)/len(checked)*100:.1f}% of checked)")
    print(f"  REST-unconfirmed:  {len(unconfirmed)}  ({len(unconfirmed)/len(checked)*100:.1f}% of checked)")
    print()

    if confirmed:
        deltas = []
        for r in confirmed:
            k = r.get("rest_kalshi_ask")
            p = r.get("rest_poly_ask")
            if k is not None and k >= 0 and p is not None and p >= 0:
                deltas.append(abs((k + p) - r["best_net_cost"]))
        if deltas:
            avg_d = sum(deltas) / len(deltas)
            max_d = max(deltas)
            close = sum(1 for d in deltas if d < 0.05)
            print(f"  WS vs REST cost delta (confirmed):")
            print(f"    Avg delta: ${avg_d:.4f}   Max: ${max_d:.4f}")
            print(f"    Close match (< $0.05): {close} / {len(deltas)}  ({close/len(deltas)*100:.1f}%)")
            print()

    delays = [r["rest_delay_ms"] for r in checked if r.get("rest_delay_ms") is not None and r["rest_delay_ms"] >= 0]
    if delays:
        delays.sort()
        print(f"  REST check delay:")
        print(f"    Median: {delays[len(delays)//2]:.0f}ms   p90: {delays[int(len(delays)*0.90)]:.0f}ms   Max: {delays[-1]:.0f}ms")

        buckets = [
            ("< 200ms",     0,      200),
            ("200-500ms",   200,    500),
            ("500ms-1s",    500,    1000),
            ("1s-3s",       1000,   3000),
            ("> 3s",        3000,   float("inf")),
        ]
        print(f"  {'Delay bucket':<16} {'Count':>6}  {'%':>6}")
        for label, lo, hi in buckets:
            cnt = sum(1 for d in delays if lo <= d < hi)
            pct = cnt / len(delays) * 100
            print(f"  {label:<16} {cnt:>6}  {pct:>5.1f}%")
        print()

    by_pair = defaultdict(list)
    for r in checked:
        by_pair[r["pair_id"]].append(r)

    if by_pair:
        print(f"  Per-pair REST results:")
        print(f"  {'Label':<46} {'Chk':>4} {'Conf':>4} {'AvgWSCost':>10} {'AvgDelay':>9}")
        print(f"  {'-'*46} {'-'*4} {'-'*4} {'-'*10} {'-'*9}")
        for pair_id, pr in sorted(by_pair.items(), key=lambda x: len(x[1]), reverse=True)[:15]:
            conf    = sum(1 for r in pr if r.get("rest_confirmed") is True)
            avg_ws  = sum(r["best_net_cost"] for r in pr) / len(pr)
            dlylist = [r["rest_delay_ms"] for r in pr if r.get("rest_delay_ms") is not None and r["rest_delay_ms"] >= 0]
            avg_dly = sum(dlylist) / len(dlylist) if dlylist else -1
            dly_str = f"{avg_dly:.0f}ms" if avg_dly >= 0 else "N/A"
            label   = pr[0]["label"][:46]
            print(f"  {label:<46} {len(pr):>4} {conf:>4} ${avg_ws:>8.4f} {dly_str:>9}")
    print()

# ─── SECTION 9: BLENDED ARB SUMMARY ─────────────────────────────────────────

def print_blended_summary(blended_rows, session_hours):
    print("=" * 80)
    print("  BLENDED CATEGORICAL ARB SUMMARY")
    print("=" * 80)

    if not blended_rows:
        print("  (no blended arb windows found in CrossArbBlended_*.csv)\n")
        return

    total           = len(blended_rows)
    unique_events   = len(set(r["event_id"] for r in blended_rows))
    total_potential = sum(r["total_potential"] for r in blended_rows)
    capturable      = [r for r in blended_rows if r["duration_ms"] >= DEFAULT_MIN_DURATION_MS]

    print(f"  Windows   : {total}  ({unique_events} unique events)")
    print(f"  Capturable: {len(capturable)} / {total}  (>= {DEFAULT_MIN_DURATION_MS}ms)")
    print(f"  Total potential profit: {_per_hr(total_potential, session_hours)}")
    print()

    durations = sorted(r["duration_ms"] for r in blended_rows)
    median = durations[len(durations)//2]
    p90    = durations[int(len(durations)*0.90)]
    print(f"  Duration — Median: {median:.0f}ms   p90: {p90:.0f}ms   Max: {durations[-1]:.0f}ms")
    print()

    by_event = defaultdict(list)
    for r in blended_rows:
        by_event[r["event_id"]].append(r)

    print(f"  {'EventId':<42} {'Legs':>4} {'Win':>4} {'AvgMs':>7} {'AvgCost':>8} {'TotProfit':>10}")
    print(f"  {'-'*42} {'-'*4} {'-'*4} {'-'*7} {'-'*8} {'-'*10}")
    for ev_id, evrows in sorted(by_event.items(), key=lambda x: -sum(r["total_potential"] for r in x[1])):
        legs     = evrows[0]["num_legs"]
        avg_ms   = sum(r["duration_ms"]    for r in evrows) / len(evrows)
        avg_cost = sum(r["best_net_cost"]  for r in evrows) / len(evrows)
        tot_pot  = sum(r["total_potential"] for r in evrows)
        print(f"  {ev_id:<42} {legs:>4} {len(evrows):>4} {avg_ms:>7.0f} {avg_cost:>8.4f} ${tot_pot:>9.4f}")
    print()

    all_choices = []
    for r in blended_rows:
        choices = r.get("best_leg_choices", "")
        if choices:
            all_choices.append(choices.split(","))

    if all_choices:
        max_legs = max(len(c) for c in all_choices)
        print(f"  Platform cheapest per leg position (across {len(all_choices)} windows):")
        print(f"  {'Leg':>4}  {'K-wins':>8}  {'P-wins':>8}  {'% K-cheap':>10}")
        print(f"  {'-'*4}  {'-'*8}  {'-'*8}  {'-'*10}")
        for leg_idx in range(max_legs):
            choices_for_leg = [c[leg_idx] for c in all_choices if leg_idx < len(c)]
            k_cnt = sum(1 for c in choices_for_leg if c.strip().upper() == "K")
            p_cnt = sum(1 for c in choices_for_leg if c.strip().upper() == "P")
            total_leg = k_cnt + p_cnt
            pct_k = k_cnt / total_leg * 100 if total_leg > 0 else 0
            print(f"  {leg_idx+1:>4}  {k_cnt:>8}  {p_cnt:>8}  {pct_k:>9.1f}%")
    print()

# ─── SECTION 10: PRODUCTION SIM ──────────────────────────────────────────────

def _duration_participation(duration_ms):
    """
    Duration-tiered participation rate.
    Short windows imply others are racing; long windows imply you're alone.
    """
    if duration_ms <   500: return 0.15
    if duration_ms <  2000: return 0.30
    if duration_ms < 60000: return 0.60
    return 0.85


def _sim_entries(by_pair, participation_rate):
    entries = []
    for pair_id, pr in by_pair.items():
        pool  = [r for r in pr if r["duration_ms"] >= PROD_LATENCY_MS] or pr
        best  = min(pool, key=lambda r: r["best_net_cost"])
        cap_w = len([r for r in pr if r["duration_ms"] >= PROD_LATENCY_MS])
        rate  = (participation_rate if participation_rate is not None
                 else _duration_participation(best["duration_ms"]))
        capital = best["total_capital_req"] * rate
        shares  = capital / best["best_net_cost"] if best["best_net_cost"] > 0 else 0
        entries.append({
            "pair_id":  pair_id,
            "label":    best["label"],
            "cap_wins": cap_w,
            "capital":  capital,
            "cost":     best["best_net_cost"],
            "rate":     rate,
            "win_pnl":  shares * best["net_profit_per_share"],
            "arb_type": best["arb_type"],
        })
    entries.sort(key=lambda x: x["win_pnl"], reverse=True)
    return entries


def print_production_sim(rows, session_hours, participation_rate=None):
    """
    Section 10 — Production Sim (always last).

    Models the bot running from a US server:
      • Min capturable window  : 17ms  (6ms one-way)
      • Participation rate     : duration-tiered by default, or flat via --participation-rate
      • Entry model            : one entry per pair at the best (lowest-cost) capturable window
      • Scope                  : clean rows only (no fraud flags)

    Note: cross-platform arbs have no external resolution API — P&L is projected only.
    """
    print("=" * 80)
    print("  PRODUCTION SIM  (US server · competition-adjusted · clean pairs only)")
    print("=" * 80)
    print(f"  Latency model : US server  (~6ms one-way, {PROD_LATENCY_MS}ms min capturable window)")
    if participation_rate is None:
        print(f"  Participation : duration-tiered  (<0.5s=15%  0.5-2s=30%  2-60s=60%  >60s=85%)")
        print(f"                  (long windows imply low competition; latency already filters fast closes)")
    else:
        print(f"  Participation : {participation_rate*100:.0f}% flat  (override via --participation-rate)")
    print(f"  Entry model   : one entry per pair at best (lowest-cost) capturable window")
    print(f"  Scope         : clean pairs — flagged rows excluded")
    print()

    clean_rows = [r for r in rows if not r["flags"]]
    flagged_n  = len(rows) - len(clean_rows)
    print(f"  Rows excluded : {flagged_n} flagged")
    print()

    if not clean_rows:
        print("  No clean rows remain after filters.")
        print()
        return

    by_pair = defaultdict(list)
    for r in clean_rows:
        by_pair[r["pair_id"]].append(r)

    entries      = _sim_entries(by_pair, participation_rate)
    total_capital = sum(e["capital"] for e in entries)
    total_proj    = sum(e["win_pnl"] for e in entries)

    def _multi(rate_or_none):
        total = 0.0
        for r in clean_rows:
            if r["duration_ms"] < PROD_LATENCY_MS:
                continue
            rate = (_duration_participation(r["duration_ms"]) if rate_or_none is None
                    else rate_or_none)
            cap = r["total_capital_req"] * rate
            if r["best_net_cost"] > 0:
                total += (cap / r["best_net_cost"]) * r["net_profit_per_share"]
        return total

    multi_proj = _multi(participation_rate)

    rate_hdr = "Part%" if participation_rate is None else f"{participation_rate*100:.0f}%"
    print(f"  {'Label':<44} {rate_hdr:>5} {'Capital':>8} {'ProjProfit':>10}  ArbType")
    print(f"  {'-'*44} {'-'*5} {'-'*8} {'-'*10}  -------")

    for e in entries:
        rate_str = f"{e['rate']*100:.0f}%" if participation_rate is None else ""
        print(f"  {e['label'][:44]:<44} {rate_str:>5} ${e['capital']:>7.2f} ${e['win_pnl']:>+9.2f}  {e['arb_type']}")

    print()
    rate_label = "duration-tiered" if participation_rate is None else f"{participation_rate*100:.0f}% flat"
    print(f"  Pairs:              {len(entries)}")
    print(f"  Total capital used: ${total_capital:.2f}  ({rate_label} participation)")
    print()
    print(f"  PROJECTED P&L   single entry  :  {_per_hr(total_proj,  session_hours)}")
    print(f"  PROJECTED P&L   multi-entry   :  {_per_hr(multi_proj,  session_hours)}")
    print()
    print(f"  Note: cross-platform arbs settle instantly (both legs pay $1 or both pay $0).")
    print(f"        No external resolution API — run the bot live to measure actual fill rate.")
    print()

    # ── Competition sensitivity table ─────────────────────────────────────────
    capturable_clean = [r for r in clean_rows if r["duration_ms"] >= PROD_LATENCY_MS]
    print(f"  COMPETITION SENSITIVITY  ({len(entries)} pairs · {len(capturable_clean)} capturable windows)")
    print(f"  {'Model':<16}  {'Assumption':<28}  {'1x Capital':>10}  {'1x /hr':>9}  {'Multi /hr':>10}")
    print(f"  {'-'*16}  {'-'*28}  {'-'*10}  {'-'*9}  {'-'*10}")

    def _row(rate_or_none, label, marker=""):
        sc_entries = _sim_entries(by_pair, rate_or_none)
        sc_capital = sum(e["capital"] for e in sc_entries)
        sc_1x      = sum(e["win_pnl"] for e in sc_entries)
        sc_multi   = _multi(rate_or_none)
        model_str  = "duration-tiered" if rate_or_none is None else f"flat {rate_or_none*100:.0f}%"
        print(f"  {model_str:<16}  {label:<28}  ${sc_capital:>9.2f}  {_hr(sc_1x, session_hours):>9}  "
              f"{_hr(sc_multi, session_hours):>10}{marker}")

    _row(None,  "tiered by duration (default)", " <--" if participation_rate is None else "")
    _row(1.00,  "sole actor / no competition",  " <--" if participation_rate == 1.0  else "")
    _row(0.50,  "1 competitor  (~2 desks)",     " <--" if participation_rate == 0.5  else "")
    _row(0.25,  "3 competitors (~4 desks)",     " <--" if participation_rate == 0.25 else "")
    _row(0.10,  "9 competitors (~10 desks)",    " <--" if participation_rate == 0.10 else "")

    print()
    print(f"  Note: multi-entry = every capturable re-entry per pair at the same participation rate.")
    print()

# ─── MAIN ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Analyze CrossArbTelemetry CSV")
    parser.add_argument("--file",         default=None,
                        help="Path to CrossArbTelemetry CSV (auto-discovers if omitted)")
    parser.add_argument("--min-duration", type=int, default=DEFAULT_MIN_DURATION_MS,
                        help=f"Min ms to count as capturable (default {DEFAULT_MIN_DURATION_MS})")
    parser.add_argument("--spam-threshold", type=int, default=REPEAT_SPAM_THRESHOLD,
                        help=f"Windows per pair above which REPEAT_SPAM fires (default {REPEAT_SPAM_THRESHOLD})")
    parser.add_argument("--exclude", default="",
                        help="Comma-separated terms; rows whose Label or PairId contains any term are removed")
    parser.add_argument("--include", default="",
                        help="Comma-separated terms; only rows whose Label or PairId contains at least one term are kept")
    parser.add_argument("--clean", action="store_true",
                        help="Only analyze rows with no fraud flags")
    parser.add_argument("--participation-rate", type=float, default=None,
                        help="Flat participation rate 0.0-1.0 (default: duration-tiered model)")
    args = parser.parse_args()

    path = args.file or find_latest_csv("CrossArbTelemetry_*.csv")
    if not path:
        print("ERROR: No CrossArbTelemetry_*.csv found. Run the KalshiPolyCross bot first.")
        sys.exit(1)

    rows = load_binary_csv(path)
    if not rows:
        print(f"ERROR: No valid rows loaded from {path}")
        sys.exit(1)

    # ── Apply --exclude / --include ───────────────────────────────────────────
    def _matches(r, term):
        t = term.lower()
        return t in r["label"].lower() or t in r["pair_id"].lower()

    exclude_terms = {t.strip() for t in args.exclude.split(",") if t.strip()}
    include_terms = {t.strip() for t in args.include.split(",") if t.strip()}

    if exclude_terms:
        before = len(rows)
        rows = [r for r in rows if not any(_matches(r, t) for t in exclude_terms)]
        print(f"[--exclude] Removed {before - len(rows)} rows  (terms: {', '.join(sorted(exclude_terms))})")

    if include_terms:
        before = len(rows)
        rows = [r for r in rows if any(_matches(r, t) for t in include_terms)]
        print(f"[--include] Kept {len(rows)} / {before} rows  (terms: {', '.join(sorted(include_terms))})")

    session_hours = compute_session_hours(rows, path)
    compute_flags(rows, spam_threshold=args.spam_threshold)

    # Fraud report uses full dataset; --clean filter applied for analysis sections
    analysis_rows = [r for r in rows if not r["flags"]] if args.clean else rows
    if args.clean:
        print(f"[--clean] Analyzing {len(analysis_rows)} / {len(rows)} rows (no fraud flags)\n")

    print_session_summary(analysis_rows, path, session_hours)
    print_all_pairs(rows)
    print_fraud_checks(rows, spam_threshold=args.spam_threshold)
    print_duration_analysis(analysis_rows, args.min_duration)
    print_profit_analysis(analysis_rows, args.min_duration, DEFAULT_CAPITAL_PER_ARB, DEFAULT_CAPTURE_RATE, session_hours)
    print_arb_type_breakdown(analysis_rows)
    print_ws_reliability(rows)
    print_rest_verification(rows)

    # Blended section (optional CSV)
    blended_path = find_blended_csv(path)
    if blended_path and os.path.exists(blended_path):
        blended_rows = load_blended_csv(blended_path)
        print(f"[BLENDED] {blended_path}  ({len(blended_rows)} rows)")
        print_blended_summary(blended_rows, session_hours)
    else:
        print("=" * 80)
        print("  BLENDED CATEGORICAL ARB SUMMARY")
        print("=" * 80)
        print("  (no CrossArbBlended_*.csv found — no blended arb windows recorded yet)\n")

    print_production_sim(rows, session_hours,          # LAST — most important
                         participation_rate=args.participation_rate)

if __name__ == "__main__":
    main()
