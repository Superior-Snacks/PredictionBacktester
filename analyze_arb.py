"""
Arb Telemetry CSV Analyzer
Reads ArbTelemetry_*.csv files and computes realistic PnL projections.
Usage: python analyze_arb.py [path_to_csv] [--exclude EVENT_ID ...] [--top N]
       If no path given, uses the most recent ArbTelemetry_*.csv in the current directory.
       --exclude EVENT_ID   Exclude specific event IDs (can repeat)
       --top N              Auto-exclude top N events by potential profit
"""

import csv
import sys
import glob
import os
import argparse
from collections import defaultdict
from datetime import datetime, timedelta

def load_csv(path):
    rows = []
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        for r in reader:
            try:
                rows.append({
                    "start":       r["StartTime"],
                    "end":         r["EndTime"],
                    "duration_ms": float(r["DurationMs"]),
                    "event":       r["EventId"].strip('"'),
                    "legs":        int(r["NumLegs"]),
                    "leg_prices":  r["LegPrices"].strip('"'),
                    "entry_net":   float(r["EntryNetCost"]),
                    "best_gross":  float(r["BestGrossCost"]),
                    "total_fees":  float(r["TotalFees"]),
                    "best_net":    float(r["BestNetCost"]),
                    "profit_per":  float(r["NetProfitPerShare"]),
                    "max_vol":     float(r["MaxVolume"]),
                    "capital_req": float(r["TotalCapitalRequired"]),
                    "potential":   float(r["TotalPotentialProfit"]),
                })
            except (ValueError, KeyError) as e:
                continue
    return rows


def analyze(rows, exclude_events=None, exclude_top_n=0):
    if not rows:
        print("No arb records found.")
        return

    excluded = set(exclude_events or [])

    # Auto-exclude top N events by potential profit
    if exclude_top_n > 0:
        events_pot = defaultdict(float)
        for r in rows:
            events_pot[r["event"]] += r["potential"]
        top_events = sorted(events_pot, key=events_pot.get, reverse=True)[:exclude_top_n]
        excluded.update(top_events)

    if excluded:
        before = len(rows)
        rows = [r for r in rows if r["event"] not in excluded]
        print(f"  Excluded {len(excluded)} event(s): {', '.join(sorted(excluded))}")
        print(f"  Rows: {before} → {len(rows)}")
        print()
        if not rows:
            print("No arb records remaining after exclusion.")
            return

    # ── Overall Summary ──
    total_potential = sum(r["potential"] for r in rows)
    total_capital   = sum(r["capital_req"] for r in rows)
    durations       = [r["duration_ms"] for r in rows]
    profits_per     = [r["profit_per"] for r in rows]
    volumes         = [r["max_vol"] for r in rows]

    print("=" * 70)
    print("  ARB TELEMETRY ANALYSIS")
    print("=" * 70)
    print(f"  Total arb windows detected:  {len(rows)}")
    print(f"  Unique events with arbs:     {len(set(r['event'] for r in rows))}")
    print(f"  Time span:                   {rows[0]['start']} - {rows[-1]['end']}")
    print()

    # ── Profit Summary ──
    print("── PROFIT SUMMARY (if every arb was captured at peak) ──")
    print(f"  Total potential profit:      ${total_potential:,.2f}")
    print(f"  Total capital deployed:      ${total_capital:,.2f}")
    print(f"  Avg profit per window:       ${total_potential / len(rows):,.4f}")
    print(f"  Avg profit/share:            ${sum(profits_per) / len(profits_per):,.4f}")
    print(f"  Best single window:          ${max(r['potential'] for r in rows):,.2f}")
    print()

    # ── Duration Analysis ──
    print("── WINDOW DURATION ──")
    durations_sorted = sorted(durations)
    p50 = durations_sorted[len(durations_sorted) // 2]
    p90 = durations_sorted[int(len(durations_sorted) * 0.9)]
    executable = [d for d in durations if d >= 3500]  # >= 3.5s (conservative: accounts for sports 3s delay + 500ms match)
    print(f"  Min:                         {min(durations):,.0f} ms")
    print(f"  Median (p50):                {p50:,.0f} ms")
    print(f"  p90:                         {p90:,.0f} ms")
    print(f"  Max:                         {max(durations):,.0f} ms")
    print(f"  Windows >= 3.5s (executable): {len(executable)}/{len(rows)} ({100*len(executable)/len(rows):.0f}%)")
    print(f"  Windows >= 5s:               {len([d for d in durations if d >= 5000])}/{len(rows)}")
    print()

    # ── Volume Analysis ──
    print("── VOLUME / CAPITAL ──")
    print(f"  Avg max volume (shares):     {sum(volumes) / len(volumes):,.1f}")
    print(f"  Median volume:               {sorted(volumes)[len(volumes)//2]:,.1f}")
    print(f"  Avg capital per window:      ${total_capital / len(rows):,.2f}")
    print(f"  Max capital in single arb:   ${max(r['capital_req'] for r in rows):,.2f}")
    print()

    # ── Realistic PnL Estimate ──
    # Assume: only capture arbs lasting >= 3.5s (sports 3s delay + 500ms match),
    # capture 70% of peak profit, and can only deploy up to $50 per arb window
    max_deploy = 50.0
    realistic_profit = 0
    realistic_capital = 0
    realistic_count = 0
    for r in rows:
        if r["duration_ms"] < 3500:
            continue
        realistic_count += 1
        deploy_ratio = min(1.0, max_deploy / r["capital_req"]) if r["capital_req"] > 0 else 0
        realistic_capital += r["capital_req"] * deploy_ratio
        realistic_profit += r["potential"] * deploy_ratio * 0.70

    # Calculate session duration in hours (handles multi-day / midnight crossings)
    # End times are chronologically ordered (rows logged when arb closes).
    # Walk end times to detect midnight crossings, then measure first start → last end.
    try:
        t_start = datetime.strptime(rows[0]["start"], "%H:%M:%S.%f")
        end_times = [datetime.strptime(r["end"], "%H:%M:%S.%f") for r in rows]

        days_offset = 0
        prev = t_start
        for et in end_times:
            if et < prev and (prev - et).total_seconds() > 3600:
                days_offset += 1
            prev = et

        t_end_adjusted = end_times[-1] + timedelta(days=days_offset)
        session_hours = (t_end_adjusted - t_start).total_seconds() / 3600
        if session_hours <= 0:
            session_hours = 0
    except ValueError:
        session_hours = 0

    print("── REALISTIC PnL ESTIMATE ──")
    print(f"  Assumptions: >= 3.5s windows only, 70% capture rate, ${max_deploy:.0f} max per arb")
    print(f"  Eligible windows:            {realistic_count}/{len(rows)}")
    print(f"  Capital deployed (total):    ${realistic_capital:,.2f}")
    print(f"  Estimated profit:            ${realistic_profit:,.2f}")
    if session_hours > 0:
        print(f"  Session duration:            {session_hours:.1f} hrs")
        print(f"  Profit per hour:             ${realistic_profit / session_hours:,.2f}/hr")
    print()

    # ── Per-Event Breakdown ──
    events = defaultdict(list)
    for r in rows:
        events[r["event"]].append(r)

    print("── PER-EVENT BREAKDOWN ──")
    print(f"  {'Event':<12} {'Legs':>4} {'Windows':>7} {'Potential':>10} {'AvgProfit/sh':>13} {'AvgDuration':>12} {'AvgVol':>8}")
    print("  " + "-" * 68)

    event_summary = []
    for eid, recs in events.items():
        avg_profit = sum(r["profit_per"] for r in recs) / len(recs)
        avg_dur    = sum(r["duration_ms"] for r in recs) / len(recs)
        avg_vol    = sum(r["max_vol"] for r in recs) / len(recs)
        total_pot  = sum(r["potential"] for r in recs)
        legs       = recs[0]["legs"]
        event_summary.append((total_pot, eid, legs, len(recs), avg_profit, avg_dur, avg_vol))

    for total_pot, eid, legs, count, avg_profit, avg_dur, avg_vol in sorted(event_summary, reverse=True):
        print(f"  {eid:<12} {legs:>4} {count:>7} ${total_pot:>8.2f} ${avg_profit:>11.4f} {avg_dur:>10.0f}ms {avg_vol:>7.1f}")

    print()

    # ── Profit Buckets ──
    print("── PROFIT/SHARE DISTRIBUTION ──")
    buckets = [
        ("< $0.001  (dust)",      0, 0.001),
        ("$0.001-0.01 (micro)",   0.001, 0.01),
        ("$0.01-0.05 (small)",    0.01, 0.05),
        ("$0.05-0.10 (medium)",   0.05, 0.10),
        ("> $0.10   (large)",     0.10, 999),
    ]
    for label, lo, hi in buckets:
        matching = [r for r in rows if lo <= r["profit_per"] < hi]
        pot = sum(r["potential"] for r in matching)
        print(f"  {label:<25} {len(matching):>4} windows  ${pot:>8.2f} potential")

    print()
    print("=" * 70)


def main():
    parser = argparse.ArgumentParser(description="Arb Telemetry CSV Analyzer")
    parser.add_argument("path", nargs="?", help="Path to ArbTelemetry CSV file")
    parser.add_argument("--exclude", nargs="+", default=[], metavar="EVENT_ID",
                        help="Exclude specific event IDs")
    parser.add_argument("--top", type=int, default=0, metavar="N",
                        help="Auto-exclude top N events by potential profit")
    args = parser.parse_args()

    if args.path:
        path = args.path
    else:
        search_paths = [
            "ArbTelemetry_*.csv",
            "PredictionLiveTrader/ArbTelemetry_*.csv",
            "PredictionLiveTrader/bin/Release/**/ArbTelemetry_*.csv",
            "PredictionLiveProduction/ArbTelemetry_*.csv",
            "PredictionLiveProduction/bin/Release/**/ArbTelemetry_*.csv",
            "../ArbTelemetry_*.csv",
        ]
        files = []
        for pattern in search_paths:
            files.extend(glob.glob(pattern, recursive=True))
        if not files:
            print("No ArbTelemetry_*.csv found. Pass a path as argument.")
            sys.exit(1)
        files.sort(key=os.path.getmtime, reverse=True)
        path = files[0]

    print(f"Analyzing: {path}")
    print()
    rows = load_csv(path)
    analyze(rows, exclude_events=args.exclude, exclude_top_n=args.top)


if __name__ == "__main__":
    main()
