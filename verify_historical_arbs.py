"""
Historical Arb Telemetry Verifier & Analyzer
Reads old ArbTelemetry_*.csv files, queries the Polymarket API to filter out 
"Fake Arbs" (non-mutually exclusive sports games) and "Trap Arbs" (Augmented Placeholder markets),
and computes realistic PnL projections for the true arbs that remain.

Usage: python verify_historical_arbs.py [path_to_csv]
"""

import csv
import sys
import glob
import os
import argparse
import requests
import time
from collections import defaultdict

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
                    "pot_profit":  float(r["TotalPotentialProfit"])
                })
            except ValueError:
                continue
    return rows

def verify_event_with_api(event_id, session):
    """
    Queries Polymarket to check if the event is a true categorical arb.
    """
    url = f"https://gamma-api.polymarket.com/events/{event_id}"
    try:
        resp = session.get(url, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            is_mutex = data.get("mutuallyExclusive", False)
            is_aug = data.get("negRiskAugmented", False)
            title = data.get("title", "Unknown")
            
            if is_mutex and not is_aug:
                return True, "Valid", title
            elif not is_mutex:
                return False, "Fake", title
            elif is_aug:
                return False, "Trap", title
        elif resp.status_code == 404:
            return False, "Not Found", "Unknown"
        else:
            return False, f"HTTP {resp.status_code}", "Unknown"
    except Exception as e:
        return False, f"Error", "Unknown"

def main():
    parser = argparse.ArgumentParser(description="Historical Arb Telemetry Verifier")
    parser.add_argument("path", nargs="?", help="Path to ArbTelemetry CSV file")
    args = parser.parse_args()

    if args.path:
        path = args.path
    else:
        search_paths = [
            "ArbTelemetry_*.csv",
            "PredictionLiveTrader/ArbTelemetry_*.csv",
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

    print(f"Loading data from: {path} ...")
    rows = load_csv(path)
    if not rows:
        print("No valid rows found in CSV.")
        sys.exit(0)

    unique_event_ids = set(r["event"] for r in rows)
    print(f"Found {len(unique_event_ids)} unique events. Verifying with Polymarket API...")
    
    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) QuantBot/1.0"})
    
    valid_events = {}
    stats = {
        "fake": 0,
        "trap": 0,
        "error": 0
    }

    # Verify each event
    for i, eid in enumerate(unique_event_ids):
        sys.stdout.write(f"\rVerifying event {i+1}/{len(unique_event_ids)}...")
        sys.stdout.flush()
        
        is_valid, reason, title = verify_event_with_api(eid, session)
        
        if is_valid:
            valid_events[eid] = title
        else:
            if reason == "Fake":
                stats["fake"] += 1
            elif reason == "Trap":
                stats["trap"] += 1
            else:
                stats["error"] += 1
                
        time.sleep(0.1) # Small delay to respect rate limits

    print("\n\n" + "=" * 70)
    print(" VERIFICATION SUMMARY")
    print("=" * 70)
    print(f" Total Unique Events Found : {len(unique_event_ids)}")
    print(f" ❌ Fake Arbs (Sports)     : {stats['fake']}")
    print(f" ❌ Trap Arbs (Augmented)  : {stats['trap']}")
    print(f" ❓ API Errors/Not Found   : {stats['error']}")
    print(f" ✅ TRUE Arbs Verified     : {len(valid_events)}")
    print("=" * 70 + "\n")

    if not valid_events:
        print("No valid arbs remained after filtering. Exiting.")
        sys.exit(0)

    # Filter rows to only keep valid events
    valid_rows = [r for r in rows if r["event"] in valid_events]
    
    # Group by Event
    by_event = defaultdict(list)
    for r in valid_rows:
        by_event[r["event"]].append(r)

    results = []
    for eid, ev_rows in by_event.items():
        max_legs = max(r["legs"] for r in ev_rows)
        max_profit_per = max(r["profit_per"] for r in ev_rows)
        max_pot = max(r["pot_profit"] for r in ev_rows)
        avg_dur = sum(r["duration_ms"] for r in ev_rows) / len(ev_rows)
        avg_vol = sum(r["max_vol"] for r in ev_rows) / len(ev_rows)
        title = valid_events[eid]

        results.append({
            "event": eid,
            "title": title,
            "legs": max_legs,
            "windows": len(ev_rows),
            "max_pot": max_pot,
            "max_profit_per": max_profit_per,
            "avg_dur": avg_dur,
            "avg_vol": avg_vol
        })

    # Sort by total potential profit
    results.sort(key=lambda x: x["max_pot"], reverse=True)

    print(f"{'Event Title':<45} {'Legs':>4} {'Windows':>7} {'Potential':>10} {'AvgProfit/sh':>13} {'AvgDuration':>12} {'AvgVol':>9}")
    print("-" * 105)
    
    total_potential = 0
    for res in results:
        title_trunc = res['title'][:43] + ".." if len(res['title']) > 45 else res['title']
        print(f"{title_trunc:<45} {res['legs']:>4} {res['windows']:>7} ${res['max_pot']:>9.2f} ${res['max_profit_per']:>12.4f} {res['avg_dur']:>10.0f}ms {res['avg_vol']:>9.1f}")
        total_potential += res["max_pot"]

    print("-" * 105)
    print(f"Total Theoretical Profit (assuming perfect fills): ${total_potential:,.2f}")
    print("=" * 105)

if __name__ == "__main__":
    main()