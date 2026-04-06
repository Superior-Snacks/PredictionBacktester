"""
Historical Arb Telemetry Verifier & Analyzer
Reads any version of ArbTelemetry_*.csv, intelligently maps the columns, 
queries the Polymarket API to filter out Fake Arbs/Traps, and computes PnL.

Usage: python analyze_historical_arbs.py [path_to_csv]
"""

import csv
import sys
import glob
import os
import argparse
import requests
import time
from collections import defaultdict

def load_csv_robust(path):
    rows = []
    skipped = 0
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        headers = reader.fieldnames
        print(f"-> Detected CSV Headers: {headers}")
        
        for i, r in enumerate(reader):
            try:
                # Intelligently find the ID column regardless of CSV version
                entity_id = r.get("EventId") or r.get("MarketId") or r.get("marketId")
                if not entity_id:
                    skipped += 1
                    continue
                
                entity_id = entity_id.strip('"').strip()
                
                # Robust extraction of numerical data (falling back to 0 if column is missing)
                dur_str = str(r.get("DurationMs", "0")).replace("ms", "").strip()
                duration = float(dur_str) if dur_str else 0.0
                
                pot_profit = float(r.get("TotalPotentialProfit", r.get("pot_profit", 0)))
                max_vol = float(r.get("MaxVolume", r.get("MaxVolumeAtBestSpread", 0)))
                profit_per = float(r.get("NetProfitPerShare", r.get("profitPerShare", 0)))
                legs = int(r.get("NumLegs", r.get("legs", 0)))
                
                rows.append({
                    "id": entity_id,
                    "duration_ms": duration,
                    "legs": legs,
                    "profit_per": profit_per,
                    "max_vol": max_vol,
                    "pot_profit": pot_profit
                })
            except Exception as e:
                skipped += 1
                
    return rows, skipped

def verify_with_api(entity_id, session):
    """
    Determines if the ID is an Event (numeric) or a Market (0x...) 
    and pings the appropriate Polymarket API to verify if it's a true arb.
    """
    clean_id = entity_id.replace("[EVT: ", "").replace("[MKT: ", "").replace("]", "").strip()
    is_market = clean_id.startswith("0x") or "MKT" in entity_id
    
    if is_market:
        # QUERY MARKET API (Used in older bot versions or 3-way soccer games)
        url = f"https://gamma-api.polymarket.com/markets?condition_id={clean_id}"
        try:
            resp = session.get(url, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data, list) and len(data) > 0:
                    mkt = data[0]
                    is_aug = mkt.get("negRiskAugmented", False)
                    title = mkt.get("question", clean_id)
                    tokens = mkt.get("clobTokenIds", [])
                    
                    if is_aug:
                        return False, "Trap (Augmented)", title
                    elif len(tokens) >= 3:
                        return True, "Valid Categorical", title
                    else:
                        return False, "Ignored (2-Leg Binary)", title
                else:
                    return False, "Not Found", clean_id
        except Exception:
            return False, "API Error", clean_id

    else:
        # QUERY EVENT API (Used for Elections/Grouped Markets)
        url = f"https://gamma-api.polymarket.com/events/{clean_id}"
        try:
            resp = session.get(url, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                is_mutex = data.get("mutuallyExclusive", False)
                is_aug = data.get("negRiskAugmented", False)
                title = data.get("title", clean_id)
                
                if is_mutex and not is_aug:
                    return True, "Valid Event", title
                elif not is_mutex:
                    return False, "Fake (Sports/Grouped)", title
                elif is_aug:
                    return False, "Trap (Augmented)", title
            else:
                return False, f"HTTP {resp.status_code}", clean_id
        except Exception:
            return False, "API Error", clean_id
            
    return False, "Unknown", clean_id

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

    print(f"\nLoading data from: {path} ...")
    rows, skipped = load_csv_robust(path)
    
    print(f"-> Successfully loaded {len(rows)} rows.")
    if skipped > 0:
        print(f"-> WARNING: Skipped {skipped} unreadable rows.")

    if not rows:
        print("No valid rows found in CSV. Exiting.")
        sys.exit(0)

    unique_ids = set(r["id"] for r in rows)
    print(f"\nFound {len(unique_ids)} unique entities. Verifying with Polymarket API...")
    
    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) QuantBot/1.0"})
    
    valid_entities = {}
    stats = {"fake": 0, "trap": 0, "binary": 0, "error": 0}

    # Verify each entity with the API
    for i, eid in enumerate(unique_ids):
        sys.stdout.write(f"\rVerifying {i+1}/{len(unique_ids)}...")
        sys.stdout.flush()
        
        is_valid, reason, title = verify_with_api(eid, session)
        
        if is_valid:
            valid_entities[eid] = title
        else:
            if "Fake" in reason:
                stats["fake"] += 1
            elif "Trap" in reason:
                stats["trap"] += 1
            elif "2-Leg" in reason:
                stats["binary"] += 1
            else:
                stats["error"] += 1
                
        time.sleep(0.1) # Be polite to Polymarket's API

    print("\n\n" + "=" * 75)
    print(" VERIFICATION SUMMARY")
    print("=" * 75)
    print(f" Total Unique Entities Checked : {len(unique_ids)}")
    print(f" ❌ Fake Arbs (Sports Groups)  : {stats['fake']}")
    print(f" ❌ Trap Arbs (Augmented)      : {stats['trap']}")
    print(f" ⚠️ Ignored (2-Leg Binary)     : {stats['binary']}")
    print(f" ❓ API Errors/Not Found       : {stats['error']}")
    print(f" ✅ TRUE 3+ Leg Arbs Verified  : {len(valid_entities)}")
    print("=" * 75 + "\n")

    if not valid_entities:
        print("No mathematically valid arbs remained after filtering. Exiting.")
        sys.exit(0)

    # Filter rows to only keep valid events
    valid_rows = [r for r in rows if r["id"] in valid_entities]
    
    # Group by Entity ID
    by_entity = defaultdict(list)
    for r in valid_rows:
        by_entity[r["id"]].append(r)

    results = []
    for eid, ev_rows in by_entity.items():
        max_legs = max(r["legs"] for r in ev_rows)
        max_profit_per = max(r["profit_per"] for r in ev_rows)
        max_pot = max(r["pot_profit"] for r in ev_rows)
        avg_dur = sum(r["duration_ms"] for r in ev_rows) / len(ev_rows)
        avg_vol = sum(r["max_vol"] for r in ev_rows) / len(ev_rows)
        title = valid_entities[eid]

        results.append({
            "id": eid,
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

    print(f"{'Market/Event Title':<45} {'Legs':>4} {'Windows':>7} {'Potential':>10} {'AvgProfit/sh':>13} {'AvgDuration':>12} {'AvgVol':>9}")
    print("-" * 105)
    
    total_potential = 0
    for res in results:
        # Determine leg count (older scripts might have logged '0', so show N/A if unknown)
        legs_str = str(res['legs']) if res['legs'] > 0 else "N/A"
        title_trunc = res['title'][:43] + ".." if len(res['title']) > 45 else res['title']
        
        print(f"{title_trunc:<45} {legs_str:>4} {res['windows']:>7} ${res['max_pot']:>9.2f} ${res['max_profit_per']:>12.4f} {res['avg_dur']:>10.0f}ms {res['avg_vol']:>9.1f}")
        total_potential += res["max_pot"]

    print("-" * 105)
    print(f"Total Theoretical Profit (assuming perfect fills): ${total_potential:,.2f}")
    print("=" * 105)

if __name__ == "__main__":
    main()