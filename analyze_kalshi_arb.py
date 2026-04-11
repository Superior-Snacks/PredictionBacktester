"""
Kalshi Arb Telemetry Analyzer
Reads ArbTelemetry_*.csv from the Kalshi paper trader and produces:
  1. Session summary
  2. Fraud / sanity checks (flags bad rows)
  3. Duration histogram
  4. Profit analysis
  5. Per-event breakdown (top 20)
  6. Spread/correlated market detection
  7. Realistic PnL estimate (Iceland -> US latency model)

Usage:
  python analyze_kalshi_arb.py
  python analyze_kalshi_arb.py --file path/to/ArbTelemetry_xxx.csv
  python analyze_kalshi_arb.py --min-duration 200
  python analyze_kalshi_arb.py --exclude KXEPLSPREAD-26APR11BREEVE,KXHIGHTSATX-26APR11
  python analyze_kalshi_arb.py --clean   # only non-flagged rows
"""

import csv
import sys
import glob
import os
import argparse
from collections import defaultdict

# ─── CONFIG ───────────────────────────────────────────────────────────────────
DEFAULT_MIN_DURATION_MS = 500
DEFAULT_LATENCY_MS      = 180   # Iceland -> US one-way estimate
DEFAULT_CAPITAL_PER_ARB = 50.0  # max $ per arb attempt
DEFAULT_CAPTURE_RATE    = 0.60  # realistic fill rate

PRICE_SUM_LOW_THRESHOLD  = 0.70   # below this = likely NOT mutually exclusive
PRICE_SUM_HIGH_THRESHOLD = 1.20   # above this = likely missing legs
REPEAT_SPAM_THRESHOLD    = 100    # >N windows for same event = spam (100 = ~1 window/5min over 9h)
THIN_DEPTH_THRESHOLD     = 2.0    # below this = 1-contract resting order noise

# ─── CSV DISCOVERY ────────────────────────────────────────────────────────────

def find_latest_csv():
    candidates = (
        glob.glob("ArbTelemetry_*.csv") +
        glob.glob("KalshiPaperTrader/ArbTelemetry_*.csv")
    )
    if not candidates:
        return None
    return max(candidates, key=os.path.getctime)

# ─── DATA LOADING ─────────────────────────────────────────────────────────────

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

def load_csv(path):
    rows = []
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for r in reader:
            try:
                raw_prices = r["LegPrices"].strip('"')
                leg_prices = [float(p) for p in raw_prices.split("|") if p.strip()]
                rows.append({
                    "start":               r["StartTime"].strip(),
                    "end":                 r["EndTime"].strip(),
                    "duration_ms":         float(r["DurationMs"]),
                    "event":               r["EventId"].strip('"').strip(),
                    "num_legs":            int(r["NumLegs"]),
                    "leg_prices":          leg_prices,
                    "leg_sum":             sum(leg_prices),
                    "entry_net_cost":      float(r["EntryNetCost"]),
                    "best_gross_cost":     float(r["BestGrossCost"]),
                    "total_fees":          float(r["TotalFees"]),
                    "best_net_cost":       float(r["BestNetCost"]),
                    "net_profit_per_share":float(r["NetProfitPerShare"]),
                    "max_volume":          float(r["MaxVolume"]),
                    "total_capital_req":   float(r["TotalCapitalRequired"]),
                    "total_potential":     float(r["TotalPotentialProfit"]),
                    # REST verification columns (None if not in file / not yet checked)
                    "rest_checked":        _try_bool(r,  "RestChecked"),
                    "rest_confirmed":      _try_bool(r,  "RestConfirmed"),
                    "rest_yes_ask_sum":    _try_float(r, "RestYesAskSum"),
                    "rest_min_depth":      _try_float(r, "RestMinDepth"),
                    "rest_delay_ms":       _try_float(r, "RestCheckDelayMs"),
                })
            except (KeyError, ValueError):
                continue
    return rows

# ─── FRAUD FLAGS ──────────────────────────────────────────────────────────────

def compute_flags(rows, spam_threshold=REPEAT_SPAM_THRESHOLD):
    # First pass: per-row flags
    event_counts = defaultdict(int)
    for r in rows:
        event_counts[r["event"]] += 1

    for r in rows:
        flags = []
        if r["leg_sum"] < PRICE_SUM_LOW_THRESHOLD:
            flags.append("PRICE_SUM_LOW")
        if r["leg_sum"] > PRICE_SUM_HIGH_THRESHOLD:
            flags.append("PRICE_SUM_HIGH")
        if r["net_profit_per_share"] <= 0:
            flags.append("ZERO_PROFIT")
        if r["duration_ms"] < 10:
            flags.append("INSTANT_OPEN_CLOSE")
        if r["duration_ms"] > 3_600_000:
            flags.append("IMPLAUSIBLE_DURATION")
        if r["max_volume"] < THIN_DEPTH_THRESHOLD:
            flags.append("THIN_DEPTH")
        if r["best_net_cost"] > 1.00:
            flags.append("COST_EXCEEDS_1")
        if event_counts[r["event"]] > spam_threshold:
            flags.append("REPEAT_SPAM")
        r["flags"] = flags

# ─── SECTION 1: SESSION SUMMARY ───────────────────────────────────────────────

def print_session_summary(rows, path):
    print("=" * 80)
    print("  KALSHI ARB TELEMETRY ANALYSIS")
    print("=" * 80)
    print(f"  File     : {path}")
    print(f"  Rows     : {len(rows)}")
    if not rows:
        return

    unique_events = len(set(r["event"] for r in rows))
    avg_legs = sum(r["num_legs"] for r in rows) / len(rows)
    total_potential = sum(r["total_potential"] for r in rows)

    starts = [r["start"] for r in rows]
    ends   = [r["end"]   for r in rows]
    print(f"  Events   : {unique_events} unique")
    print(f"  Avg legs : {avg_legs:.1f}")
    print(f"  Time span: {starts[0]} — {ends[-1]}")
    print(f"  Total potential profit (all windows): ${total_potential:.2f}")
    print()

# ─── SECTION 2: FRAUD / SANITY CHECKS ────────────────────────────────────────

def print_fraud_checks(rows, spam_threshold=REPEAT_SPAM_THRESHOLD):
    print("=" * 80)
    print("  FRAUD / SANITY CHECKS")
    print("=" * 80)
    if not rows:
        print("  (no data)")
        print()
        return

    all_flag_names = [
        "PRICE_SUM_LOW", "PRICE_SUM_HIGH", "ZERO_PROFIT",
        "INSTANT_OPEN_CLOSE", "IMPLAUSIBLE_DURATION",
        "THIN_DEPTH", "REPEAT_SPAM", "COST_EXCEEDS_1",
    ]

    flag_descriptions = {
        "PRICE_SUM_LOW":        f"Leg prices sum < {PRICE_SUM_LOW_THRESHOLD} — likely NOT mutually exclusive (spread/correlated)",
        "PRICE_SUM_HIGH":       f"Leg prices sum > {PRICE_SUM_HIGH_THRESHOLD} — missing legs or bad data",
        "ZERO_PROFIT":          "NetProfitPerShare <= 0 — should not be in file",
        "INSTANT_OPEN_CLOSE":   "Duration < 10ms — single-tick glitch",
        "IMPLAUSIBLE_DURATION": "Duration > 1 hour — likely stale book across reconnect",
        "THIN_DEPTH":           f"MaxVolume < {THIN_DEPTH_THRESHOLD} — resting 1-contract order noise",
        "REPEAT_SPAM":          f"Same EventId appears > {spam_threshold}x — cycling open/close",
        "COST_EXCEEDS_1":       "BestNetCost > $1.00 — should have been filtered by strategy",
    }

    total = len(rows)
    any_flags = False
    for flag in all_flag_names:
        count = sum(1 for r in rows if flag in r["flags"])
        if count > 0:
            any_flags = True
            pct = count / total * 100
            print(f"  [{flag:<22}]  {count:4d} rows ({pct:5.1f}%)  — {flag_descriptions[flag]}")

    if not any_flags:
        print("  No fraud flags found.")

    # Detail rows for PRICE_SUM_LOW
    low_sum_rows = [r for r in rows if "PRICE_SUM_LOW" in r["flags"]]
    if low_sum_rows:
        print(f"\n  PRICE_SUM_LOW detail (first 10 unique events):")
        seen = set()
        printed = 0
        for r in low_sum_rows:
            if r["event"] not in seen:
                seen.add(r["event"])
                prices_str = " | ".join(f"${p:.2f}" for p in r["leg_prices"])
                print(f"    {r['event']:<40} sum=${r['leg_sum']:.4f}  legs=[{prices_str}]")
                printed += 1
                if printed >= 10:
                    break

    # Detail rows for INSTANT_OPEN_CLOSE
    instant_rows = [r for r in rows if "INSTANT_OPEN_CLOSE" in r["flags"]]
    if instant_rows:
        print(f"\n  INSTANT_OPEN_CLOSE detail ({len(instant_rows)} rows):")
        for r in instant_rows[:10]:
            print(f"    {r['event']:<40} {r['duration_ms']:.0f}ms  cost=${r['best_net_cost']:.4f}")

    print()

# ─── SECTION 3: DURATION ANALYSIS ────────────────────────────────────────────

def print_duration_analysis(rows, min_duration_ms):
    print("=" * 80)
    print("  DURATION ANALYSIS")
    print("=" * 80)
    if not rows:
        print("  (no data)")
        print()
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

    total = len(rows)
    durations = sorted(r["duration_ms"] for r in rows)

    print(f"  {'Bucket':<16} {'Count':>6}  {'%':>6}")
    print(f"  {'-'*16} {'-'*6}  {'-'*6}")
    for label, lo, hi in buckets:
        count = sum(1 for d in durations if lo <= d < hi)
        pct = count / total * 100 if total else 0
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

# ─── SECTION 4: PROFIT ANALYSIS ───────────────────────────────────────────────

def print_profit_analysis(rows, min_duration_ms, capital_per_arb, capture_rate):
    print("=" * 80)
    print("  PROFIT ANALYSIS")
    print("=" * 80)
    if not rows:
        print("  (no data)")
        print()
        return

    total_potential = sum(r["total_potential"] for r in rows)
    capturable_rows = [r for r in rows if r["duration_ms"] >= min_duration_ms]

    # Realistic profit: capped capital, capture rate applied
    realistic_profit = 0.0
    for r in capturable_rows:
        if r["total_capital_req"] > 0:
            fill_fraction = min(1.0, capital_per_arb / r["total_capital_req"])
        else:
            fill_fraction = 1.0
        realistic_profit += r["total_potential"] * fill_fraction * capture_rate

    avg_profit_per_window = total_potential / len(rows) if rows else 0
    avg_profit_per_share  = sum(r["net_profit_per_share"] for r in rows) / len(rows) if rows else 0
    best_window = max(rows, key=lambda r: r["total_potential"])

    print(f"  Total potential profit (ALL windows):         ${total_potential:.4f}")
    print(f"  Total potential profit (capturable windows):  ${sum(r['total_potential'] for r in capturable_rows):.4f}")
    print(f"  Realistic profit (capped ${capital_per_arb:.0f}, {capture_rate*100:.0f}% capture): ${realistic_profit:.4f}")
    print()
    print(f"  Avg profit / window:  ${avg_profit_per_window:.4f}")
    print(f"  Avg profit / share:   ${avg_profit_per_share:.4f}")
    print(f"  Best single window:   ${best_window['total_potential']:.4f}  ({best_window['event']}  {best_window['duration_ms']:.0f}ms)")
    print()

    # Distribution buckets
    buckets = [
        ("< $0.01",      0,     0.01),
        ("$0.01-0.05",   0.01,  0.05),
        ("$0.05-0.25",   0.05,  0.25),
        ("$0.25-1.00",   0.25,  1.00),
        ("> $1.00",      1.00,  float("inf")),
    ]
    total = len(rows)
    print(f"  Potential profit distribution:")
    print(f"  {'Bucket':<16} {'Count':>6}  {'%':>6}")
    print(f"  {'-'*16} {'-'*6}  {'-'*6}")
    for label, lo, hi in buckets:
        count = sum(1 for r in rows if lo <= r["total_potential"] < hi)
        pct = count / total * 100 if total else 0
        print(f"  {label:<16} {count:>6}  {pct:>5.1f}%")
    print()

# ─── SECTION 5: PER-EVENT BREAKDOWN ──────────────────────────────────────────

def print_per_event_breakdown(rows):
    print("=" * 80)
    print("  PER-EVENT BREAKDOWN  (top 20 by total potential profit)")
    print("=" * 80)
    if not rows:
        print("  (no data)")
        print()
        return

    by_event = defaultdict(list)
    for r in rows:
        by_event[r["event"]].append(r)

    summary = []
    for event, evrows in by_event.items():
        all_flags = set()
        for r in evrows:
            all_flags.update(r["flags"])
        summary.append({
            "event":        event,
            "legs":         evrows[0]["num_legs"],
            "windows":      len(evrows),
            "avg_duration": sum(r["duration_ms"] for r in evrows) / len(evrows),
            "avg_net_cost": sum(r["best_net_cost"] for r in evrows) / len(evrows),
            "avg_depth":    sum(r["max_volume"] for r in evrows) / len(evrows),
            "total_pot":    sum(r["total_potential"] for r in evrows),
            "flags":        ",".join(sorted(all_flags)) if all_flags else "OK",
        })

    summary.sort(key=lambda x: x["total_pot"], reverse=True)
    top = summary[:20]

    hdr = f"  {'EventId':<42} {'L':>2} {'Win':>4} {'AvgMs':>7} {'AvgCost':>8} {'AvgDepth':>9} {'TotProfit':>10}  Flags"
    print(hdr)
    print(f"  {'-'*42} {'-'*2} {'-'*4} {'-'*7} {'-'*8} {'-'*9} {'-'*10}  -----")
    for e in top:
        print(f"  {e['event']:<42} {e['legs']:>2} {e['windows']:>4} "
              f"{e['avg_duration']:>7.0f} {e['avg_net_cost']:>8.4f} "
              f"{e['avg_depth']:>9.1f} ${e['total_pot']:>9.4f}  {e['flags']}")
    print()

# ─── SECTION 6: SPREAD/CORRELATED MARKET DETECTION ───────────────────────────

def print_spread_detection(rows):
    print("=" * 80)
    print("  SPREAD / CORRELATED MARKET DETECTION")
    print("=" * 80)
    if not rows:
        print("  (no data)")
        print()
        return

    by_event = defaultdict(list)
    for r in rows:
        by_event[r["event"]].append(r)

    suspicious = []
    for event, evrows in by_event.items():
        low_count = sum(1 for r in evrows if "PRICE_SUM_LOW" in r["flags"])
        if low_count / len(evrows) > 0.50:
            avg_sum = sum(r["leg_sum"] for r in evrows) / len(evrows)
            sample  = evrows[0]["leg_prices"]
            suspicious.append((event, len(evrows), avg_sum, sample))

    if not suspicious:
        print("  No spread/correlated events detected.")
    else:
        print(f"  {len(suspicious)} event(s) flagged as likely NOT mutually exclusive:\n")
        for event, count, avg_sum, sample in sorted(suspicious, key=lambda x: x[2]):
            prices_str = " | ".join(f"${p:.2f}" for p in sample)
            print(f"  *** {event}")
            print(f"      Windows: {count}  AvgLegSum: ${avg_sum:.4f}  Sample legs: [{prices_str}]")
            print(f"      LIKELY NOT MUTUALLY EXCLUSIVE — spread/correlated bets (all can resolve NO)")
    print()

# ─── SECTION 7: REALISTIC PNL ESTIMATE ───────────────────────────────────────

def print_realistic_pnl(rows, min_duration_ms, latency_ms, capital_per_arb, capture_rate):
    print("=" * 80)
    print("  REALISTIC PnL ESTIMATE")
    print("=" * 80)
    print(f"  Assumptions:")
    print(f"    One-way latency (Iceland -> US):  ~{latency_ms}ms")
    print(f"    Min capturable window:             {min_duration_ms}ms")
    print(f"    Capital per arb:                  ${capital_per_arb:.0f}")
    print(f"    Capture rate (slippage/partial):   {capture_rate*100:.0f}%")
    print()

    if not rows:
        print("  (no data)")
        print()
        return

    # Clean rows only (no fraud flags)
    clean = [r for r in rows if not r["flags"]]
    capturable_clean = [r for r in clean if r["duration_ms"] >= min_duration_ms]

    realistic = 0.0
    for r in capturable_clean:
        if r["total_capital_req"] > 0:
            fill_fraction = min(1.0, capital_per_arb / r["total_capital_req"])
        else:
            fill_fraction = 1.0
        realistic += r["total_potential"] * fill_fraction * capture_rate

    total = len(rows)
    print(f"  Total windows:             {total}")
    print(f"  Clean windows (no flags):  {len(clean)}  ({len(clean)/total*100:.1f}%)")
    print(f"  Capturable clean windows:  {len(capturable_clean)}")
    print()
    print(f"  Expected profit (realistic): ${realistic:.4f}")

    if capturable_clean:
        avg_dur = sum(r["duration_ms"] for r in capturable_clean) / len(capturable_clean)
        avg_profit = sum(r["total_potential"] for r in capturable_clean) / len(capturable_clean)
        print(f"  Avg window duration:         {avg_dur:.0f}ms")
        print(f"  Avg potential per window:    ${avg_profit:.4f}")

    print()
    print("  Note: Running from Iceland adds ~180ms to detection latency.")
    print("  A US server would expose all windows >= ~50ms (vs ~500ms now).")
    print()

# ─── SECTION 8: REST VERIFICATION ANALYSIS ───────────────────────────────────

def print_rest_verification(rows):
    print("=" * 80)
    print("  REST VERIFICATION ANALYSIS")
    print("=" * 80)

    # Only rows where REST check was attempted
    checked = [r for r in rows if r.get("rest_checked") is True]
    not_checked = [r for r in rows if r.get("rest_checked") is False]
    no_data     = [r for r in rows if r.get("rest_checked") is None]

    total = len(rows)
    print(f"  Total arb windows:          {total}")
    print(f"  REST-checked:               {len(checked)}  ({len(checked)/total*100:.1f}%)")
    print(f"  REST not triggered:         {len(not_checked)}  ({len(not_checked)/total*100:.1f}%)")
    if no_data:
        print(f"  No REST column in CSV:      {len(no_data)}  (older CSV, run bot again)")
    print()

    if not checked:
        print("  No REST-checked windows to analyze.")
        print("  (REST verification fires when a new arb OPENS — if the arb was already open")
        print("   from a previous window, it may not fire again.)")
        print()
        return

    confirmed   = [r for r in checked if r.get("rest_confirmed") is True]
    unconfirmed = [r for r in checked if r.get("rest_confirmed") is False]
    print(f"  REST-confirmed (sum < $1.00): {len(confirmed)}  ({len(confirmed)/len(checked)*100:.1f}% of checked)")
    print(f"  REST-unconfirmed:             {len(unconfirmed)}  ({len(unconfirmed)/len(checked)*100:.1f}% of checked)")
    print()

    # WS vs REST cost comparison for confirmed arbs
    if confirmed:
        deltas = []
        for r in confirmed:
            if r["rest_yes_ask_sum"] is not None and r["rest_yes_ask_sum"] >= 0:
                deltas.append(abs(r["rest_yes_ask_sum"] - r["best_net_cost"]))

        if deltas:
            avg_delta = sum(deltas) / len(deltas)
            max_delta = max(deltas)
            close = sum(1 for d in deltas if d < 0.05)
            print(f"  WS vs REST cost delta (confirmed arbs):")
            print(f"    Avg delta:   ${avg_delta:.4f}")
            print(f"    Max delta:   ${max_delta:.4f}")
            print(f"    Close match (< $0.05):  {close} / {len(deltas)}  ({close/len(deltas)*100:.1f}%)")
            print()

    # REST check delay distribution
    delays = [r["rest_delay_ms"] for r in checked if r.get("rest_delay_ms") is not None and r["rest_delay_ms"] >= 0]
    if delays:
        delays.sort()
        median_d = delays[len(delays) // 2]
        p90_d    = delays[int(len(delays) * 0.90)]
        max_d    = delays[-1]
        print(f"  REST check delay (ms from arb open to verification):")
        print(f"    Median: {median_d:.0f}ms   p90: {p90_d:.0f}ms   Max: {max_d:.0f}ms")

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

    # Per-event REST confirmation breakdown
    by_event = defaultdict(list)
    for r in checked:
        by_event[r["event"]].append(r)

    if by_event:
        print(f"  Per-event REST results (events with >= 1 checked window):")
        print(f"  {'EventId':<42} {'Chk':>4} {'Conf':>4} {'AvgWSCost':>10} {'AvgRESTSum':>11} {'AvgDelay':>9}")
        print(f"  {'-'*42} {'-'*4} {'-'*4} {'-'*10} {'-'*11} {'-'*9}")
        rows_by_pot = sorted(by_event.items(),
                             key=lambda x: sum(r["total_potential"] for r in x[1]), reverse=True)
        for event, evrows in rows_by_pot[:20]:
            conf_count = sum(1 for r in evrows if r.get("rest_confirmed") is True)
            rest_sums  = [r["rest_yes_ask_sum"] for r in evrows
                          if r["rest_yes_ask_sum"] is not None and r["rest_yes_ask_sum"] >= 0]
            delays_ev  = [r["rest_delay_ms"] for r in evrows
                          if r.get("rest_delay_ms") is not None and r["rest_delay_ms"] >= 0]
            avg_ws   = sum(r["best_net_cost"] for r in evrows) / len(evrows)
            avg_rest = sum(rest_sums) / len(rest_sums) if rest_sums else -1
            avg_dly  = sum(delays_ev) / len(delays_ev) if delays_ev else -1
            rest_str = f"${avg_rest:.4f}" if avg_rest >= 0 else "  N/A  "
            dly_str  = f"{avg_dly:.0f}ms" if avg_dly >= 0 else "  N/A"
            print(f"  {event:<42} {len(evrows):>4} {conf_count:>4} ${avg_ws:>8.4f} {rest_str:>11} {dly_str:>9}")
    print()


# ─── MAIN ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Analyze Kalshi ArbTelemetry CSV")
    parser.add_argument("--file", default=None, help="Path to CSV (auto-discovers if omitted)")
    parser.add_argument("--min-duration", type=int, default=DEFAULT_MIN_DURATION_MS,
                        help=f"Min ms to count as capturable (default {DEFAULT_MIN_DURATION_MS})")
    parser.add_argument("--spam-threshold", type=int, default=REPEAT_SPAM_THRESHOLD,
                        help=f"Windows per event above which REPEAT_SPAM fires (default {REPEAT_SPAM_THRESHOLD})")
    parser.add_argument("--exclude", default="", help="Comma-separated EventIds to exclude")
    parser.add_argument("--clean", action="store_true", help="Only analyze rows with no fraud flags")
    args = parser.parse_args()

    path = args.file or find_latest_csv()
    if not path:
        print("ERROR: No ArbTelemetry_*.csv found. Run the Kalshi paper trader first.")
        sys.exit(1)

    rows = load_csv(path)
    if not rows:
        print(f"ERROR: No valid rows loaded from {path}")
        sys.exit(1)

    # Apply exclusions
    excluded = {e.strip() for e in args.exclude.split(",") if e.strip()}
    if excluded:
        rows = [r for r in rows if r["event"] not in excluded]
        print(f"Excluded {len(excluded)} event(s): {', '.join(excluded)}\n")

    # Compute flags on full dataset first (REPEAT_SPAM needs full counts)
    compute_flags(rows, spam_threshold=args.spam_threshold)

    # Apply --clean filter after flagging
    analysis_rows = [r for r in rows if not r["flags"]] if args.clean else rows
    if args.clean:
        print(f"[--clean] Analyzing {len(analysis_rows)} / {len(rows)} rows (no fraud flags)\n")

    print_session_summary(analysis_rows, path)
    print_fraud_checks(rows, spam_threshold=args.spam_threshold)  # always show fraud report on full dataset
    print_duration_analysis(analysis_rows, args.min_duration)
    print_profit_analysis(analysis_rows, args.min_duration, DEFAULT_CAPITAL_PER_ARB, DEFAULT_CAPTURE_RATE)
    print_per_event_breakdown(analysis_rows)
    print_spread_detection(rows)  # always on full dataset
    print_realistic_pnl(rows, args.min_duration, DEFAULT_LATENCY_MS, DEFAULT_CAPITAL_PER_ARB, DEFAULT_CAPTURE_RATE)
    print_rest_verification(rows)  # always on full dataset (REST fields may be None for old CSVs)

if __name__ == "__main__":
    main()
