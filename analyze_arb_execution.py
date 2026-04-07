"""
Arb Execution Analyzer
Reads LivePaperTrades_*.csv and breaks down each arb attempt:
which legs filled, at what price, whether the set is complete,
and the implied P&L per attempt.

Usage: python analyze_arb_execution.py [path_to_csv]
       If no path given, uses the most recent LivePaperTrades_*.csv (excludes *_summary.csv).
"""

import csv
import sys
import glob
import os
import argparse
from datetime import datetime, timezone
from collections import defaultdict

# Trades within this window = same arb attempt
GROUP_WINDOW_SECONDS = 5

# Warn if cost per set exceeds this
OVERPAID_THRESHOLD = 0.995


def find_file(pattern, exclude_suffix="_summary.csv"):
    search_paths = [
        pattern,
        f"PredictionLiveTrader/{pattern}",
        f"PredictionLiveTrader/bin/Release/**/{pattern}",
        f"PredictionLiveProduction/{pattern}",
        f"PredictionLiveProduction/bin/Release/**/{pattern}",
        f"../{pattern}",
    ]
    files = []
    for p in search_paths:
        files.extend(glob.glob(p, recursive=True))
    files = [f for f in files if not f.endswith(exclude_suffix)]
    if not files:
        return None
    files.sort(key=os.path.getmtime, reverse=True)
    return files[0]


def parse_ts(ts_str):
    """Parse ISO 8601 timestamp to datetime."""
    ts_str = ts_str.strip()
    # Handle offset-aware timestamps like 2026-03-30T23:41:53.2738867+00:00
    try:
        # Python 3.11+ handles this natively
        return datetime.fromisoformat(ts_str)
    except ValueError:
        # Truncate sub-microsecond digits (7 fractional digits -> 6)
        import re
        ts_str = re.sub(r'(\.\d{6})\d+', r'\1', ts_str)
        return datetime.fromisoformat(ts_str)


def load_trades(path):
    trades = []
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for r in reader:
            try:
                trades.append({
                    "ts":       parse_ts(r["Timestamp"]),
                    "strategy": r["StrategyName"].strip(),
                    "capital":  float(r["StartingCapital"]),
                    "market":   r["MarketName"].strip().strip('"'),
                    "asset":    r["AssetId"].strip(),
                    "side":     r["Side"].strip().upper(),
                    "price":    float(r["ExecutionPrice"]),
                    "shares":   float(r["Shares"]),
                    "dollars":  float(r["DollarValue"]),
                })
            except (ValueError, KeyError):
                continue
    return trades


def load_telemetry(trades_path):
    """Try to find a matching ArbTelemetry CSV from the same session."""
    # Extract timestamp suffix from trades filename e.g. _20260330_234124
    import re
    m = re.search(r'_(\d{8}_\d{6})', os.path.basename(trades_path))
    if not m:
        return []
    suffix = m.group(1)
    telem_path = find_file(f"ArbTelemetry_{suffix}.csv")
    if not telem_path or not os.path.exists(telem_path):
        return []

    rows = []
    with open(telem_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for r in reader:
            try:
                rows.append({
                    "start":    r["StartTime"].strip(),
                    "end":      r["EndTime"].strip(),
                    "event":    r["EventId"].strip().strip('"'),
                    "best_net": float(r["BestNetCost"]),
                    "legs":     int(r["NumLegs"]),
                })
            except (ValueError, KeyError):
                continue
    return telem_path, rows


def load_summary(trades_path):
    summary_path = trades_path.replace(".csv", "_summary.csv")
    if not os.path.exists(summary_path):
        return None
    with open(summary_path, newline="", encoding="utf-8") as f:
        return f.read()


def group_buys(buy_trades):
    """Group BUY trades into arb attempts by time proximity."""
    if not buy_trades:
        return []
    sorted_buys = sorted(buy_trades, key=lambda t: t["ts"])
    groups = []
    current = [sorted_buys[0]]
    for trade in sorted_buys[1:]:
        gap = (trade["ts"] - current[-1]["ts"]).total_seconds()
        if gap <= GROUP_WINDOW_SECONDS:
            current.append(trade)
        else:
            groups.append(current)
            current = [trade]
    groups.append(current)
    return groups


def analyze(trades_path):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    trades = load_trades(trades_path)
    if not trades:
        print(f"No trades found in {trades_path}")
        return

    buy_trades  = [t for t in trades if t["side"] == "BUY"]
    sell_trades = [t for t in trades if t["side"] in ("SELL", "SELL NO")]
    resolve_yes = [t for t in trades if t["side"] == "RESOLVE YES"]
    resolve_no  = [t for t in trades if t["side"] == "RESOLVE NO"]

    ts_all = [t["ts"] for t in trades]
    session_start = min(ts_all)
    session_end   = max(ts_all)
    duration_hrs  = (session_end - session_start).total_seconds() / 3600

    print("=" * 68)
    print(f"  ARB EXECUTION ANALYSIS")
    print(f"  {os.path.basename(trades_path)}")
    print("=" * 68)
    print(f"  Trades loaded:  {len(trades)}")
    print(f"  Session:        {session_start.strftime('%H:%M:%S')} - {session_end.strftime('%H:%M:%S')}  ({duration_hrs:.1f} hrs)")
    print(f"  BUY:  {len(buy_trades)}  |  SELL: {len(sell_trades)}  |  Resolve YES: {len(resolve_yes)}  |  Resolve NO: {len(resolve_no)}")

    # Load optional data
    telem_result = load_telemetry(trades_path)
    telem_rows = []
    telem_path = None
    if telem_result:
        telem_path, telem_rows = telem_result
        print(f"  Telemetry:      {os.path.basename(telem_path)} ({len(telem_rows)} windows)")

    summary_text = load_summary(trades_path)
    if summary_text:
        # Extract rejection count from summary if present
        import re
        m = re.search(r'Rejected[^\d]*(\d+)', summary_text, re.IGNORECASE)
        if m:
            print(f"  Rejected orders (broker): {m.group(1)}")

    print()

    # ── ARB ATTEMPTS ─────────────────────────────────────────────────────
    groups = group_buys(buy_trades)
    print(f"── ARBS ATTEMPTED ({len(groups)} attempts) " + "─" * 36)
    print()

    # Build asset → arb attempt map for sell/resolve matching
    asset_to_attempt = {}

    total_cost      = 0.0
    total_payout    = 0.0
    n_complete      = 0
    n_partial       = 0
    n_overpaid      = 0
    n_single_leg    = 0

    for i, group in enumerate(groups, 1):
        ts       = group[0]["ts"]
        num_legs = len(group)
        shares   = [t["shares"] for t in group]
        complete_sets = min(shares)
        total_grp_cost = sum(t["dollars"] for t in group)
        cost_per_set   = total_grp_cost / complete_sets if complete_sets > 0 else 999
        guaranteed     = complete_sets * 1.00
        implied_pnl    = guaranteed - total_grp_cost

        is_partial    = (max(shares) - min(shares)) > 0.01
        is_overpaid   = cost_per_set >= OVERPAID_THRESHOLD
        is_single_leg = num_legs == 1

        flags = []
        if is_single_leg: flags.append("SINGLE LEG")
        if is_partial:    flags.append("PARTIAL FILL")
        if is_overpaid:   flags.append("OVERPAID")
        flag_str = "  [" + " | ".join(flags) + "]" if flags else ""

        # Accumulate totals
        total_cost   += total_grp_cost
        total_payout += guaranteed
        if is_partial or is_single_leg: n_partial += 1
        else: n_complete += 1
        if is_overpaid:   n_overpaid += 1
        if is_single_leg: n_single_leg += 1

        # Print attempt header
        event_name = group[0]["market"]  # Use first leg's market as label (trimmed)
        if len(event_name) > 50: event_name = event_name[:47] + "..."
        print(f"  #{i}  {ts.strftime('%Y-%m-%d %H:%M:%S')}  [{event_name}]  {num_legs} leg(s){flag_str}")

        # Per-leg rows
        for t in group:
            mkt = t["market"]
            if len(mkt) > 36: mkt = mkt[:33] + "..."
            share_flag = " *" if is_partial and abs(t["shares"] - complete_sets) > 0.01 else "  "
            print(f"    {share_flag} {mkt:<36}  ${t['price']:.4f} x {t['shares']:>10.4f} sh  = ${t['dollars']:>8.4f}")

        # Partial fill note
        if is_partial:
            print(f"    WARNING: share counts differ — complete sets limited to {complete_sets:.4f}")

        print(f"    {'─'*60}")
        print(f"    Complete sets: {complete_sets:.4f}  |  Total cost: ${total_grp_cost:.4f}  |  Payout: ${guaranteed:.4f}")
        pnl_sign = "+" if implied_pnl >= 0 else ""
        overpaid_note = "  *** OVERPAID — entered at loss ***" if is_overpaid else ""
        print(f"    Cost/set: ${cost_per_set:.4f}  |  Implied P&L: {pnl_sign}${implied_pnl:.4f}{overpaid_note}")

        # Telemetry slippage (if available — match nearest window before this trade)
        if telem_rows:
            ts_hms = ts.strftime("%H:%M:%S")
            best_match = None
            for w in telem_rows:
                if w["start"] <= ts_hms:
                    best_match = w
            if best_match:
                expected = best_match["best_net"]
                slippage = cost_per_set - expected
                slip_sign = "+" if slippage >= 0 else ""
                print(f"    Telemetry:  Expected ${expected:.4f}/set  →  Actual ${cost_per_set:.4f}/set  (slippage: {slip_sign}${slippage:.4f})")

        print()

        # Register assets for sell/resolve matching
        for t in group:
            asset_to_attempt[t["asset"]] = i

    # ── SELLS & RESOLVES ─────────────────────────────────────────────────
    exit_trades = sell_trades + resolve_yes + resolve_no
    if exit_trades:
        print("── EXITS (sells / resolves) " + "─" * 41)
        print()
        exit_by_attempt = defaultdict(list)
        for t in exit_trades:
            attempt_id = asset_to_attempt.get(t["asset"], "?")
            exit_by_attempt[attempt_id].append(t)

        for attempt_id, exits in sorted(exit_by_attempt.items(), key=lambda x: str(x[0])):
            print(f"  Attempt #{attempt_id}:")
            total_exit = 0.0
            for t in exits:
                mkt = t["market"]
                if len(mkt) > 36: mkt = mkt[:33] + "..."
                print(f"    {t['side']:<12}  {mkt:<36}  ${t['price']:.4f} x {t['shares']:>10.4f} sh  = ${t['dollars']:>8.4f}")
                total_exit += t["dollars"]
            print(f"    Total proceeds: ${total_exit:.4f}")
            print()

    # ── SUMMARY ──────────────────────────────────────────────────────────
    print("── SUMMARY " + "─" * 57)
    print()
    print(f"  Total arbs attempted:          {len(groups)}")
    print(f"  Complete fills (all legs same): {n_complete}")
    print(f"  Partial / single-leg:           {n_partial}")
    print(f"  Overpaid (cost/set >= {OVERPAID_THRESHOLD}):   {n_overpaid}")
    print()
    print(f"  Total invested:     ${total_cost:.2f}")
    print(f"  Guaranteed payout:  ${total_payout:.2f}")
    implied_total = total_payout - total_cost
    pnl_sign = "+" if implied_total >= 0 else ""
    pct = (implied_total / total_cost * 100) if total_cost > 0 else 0
    print(f"  Implied P&L:        {pnl_sign}${implied_total:.2f}  ({pnl_sign}{pct:.1f}%)")
    if len(groups) > 0:
        avg_cost_per_set = total_cost / total_payout if total_payout > 0 else 0
        print(f"  Avg cost per set:   ${avg_cost_per_set:.4f}  (target: < $0.980)")
    print()

    if n_overpaid > 0:
        print("  DIAGNOSIS: Bot entered arbs at cost/set >= $0.995.")
        print("  Likely cause: 3500ms latency — books moved significantly before fills.")
        print("  The arb looked profitable at detection but was gone by execution time.")
        print()


def main():
    parser = argparse.ArgumentParser(description="Arb Execution CSV Analyzer")
    parser.add_argument("path", nargs="?", help="Path to LivePaperTrades CSV file")
    args = parser.parse_args()

    if args.path:
        path = args.path
    else:
        path = find_file("LivePaperTrades_*.csv")
        if not path:
            print("No LivePaperTrades_*.csv found. Pass a path as argument.")
            sys.exit(1)

    print(f"Loading: {path}\n")
    analyze(path)


if __name__ == "__main__":
    main()
