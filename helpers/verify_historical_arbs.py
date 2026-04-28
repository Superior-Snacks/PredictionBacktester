"""
Historical Arb Telemetry Verifier & Analyzer
Reads ArbTelemetry_*.csv, queries the Polymarket API to filter out
fake arbs (sports/grouped events, augmented neg-risk traps), and reports
only verified mutually-exclusive categorical arb opportunities.

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

# Fix Windows console encoding for unicode characters
if sys.platform == "win32":
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

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
                
                capital_req = float(r.get("TotalCapitalRequired", 0))

                rows.append({
                    "id": entity_id,
                    "start": r.get("StartTime", ""),
                    "end": r.get("EndTime", ""),
                    "duration_ms": duration,
                    "legs": legs,
                    "profit_per": profit_per,
                    "max_vol": max_vol,
                    "pot_profit": pot_profit,
                    "capital_req": capital_req,
                })
            except Exception as e:
                skipped += 1
                
    return rows, skipped

def verify_with_api(entity_id, session):
    """
    Fetches event from Gamma API and checks if it's a true categorical arb.

    The key signal is negRisk:
    - negRisk=True  → mutually exclusive outcomes (elections, winner markets, Fed decisions)
    - negRisk=False → independent markets bundled together (sports props, timelines)

    Only negRisk=True events have the "exactly one outcome pays $1.00" guarantee
    that makes categorical arbitrage work.
    """
    clean_id = entity_id.strip()

    url = f"https://gamma-api.polymarket.com/events/{clean_id}"
    try:
        resp = session.get(url, timeout=10)
        if resp.status_code != 200:
            return False, f"HTTP {resp.status_code}", clean_id

        data = resp.json()
        title = data.get("title", clean_id)
        neg_risk = data.get("negRisk", False)
        markets = data.get("markets", [])

        # Check for augmented neg-risk on any child market (placeholder trap)
        has_augmented = any(m.get("negRiskAugmented", False) for m in markets)

        if has_augmented:
            return False, "Trap (Augmented NegRisk)", title
        elif neg_risk:
            return True, "Valid (negRisk categorical)", title
        elif len(markets) < 3:
            return False, "Ignored (< 3 legs)", title
        else:
            return False, "Fake (not negRisk — sports/timeline)", title

    except Exception:
        return False, "API Error", clean_id

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
            "PredictionLiveTrader/bin/Release/**/ArbTelemetry_*.csv",
            "PredictionLiveProduction/ArbTelemetry_*.csv",
            "PredictionLiveProduction/bin/Release/**/ArbTelemetry_*.csv",
            "../ArbTelemetry_*.csv",
        ]
        files = []
        for pattern in search_paths:
            files.extend(glob.glob(pattern, recursive=True))
        # Prefer the largest file (most data) rather than just most recent
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
    invalid_entities = {}  # id -> (reason, title)
    stats = {"fake": 0, "trap": 0, "small": 0, "error": 0}

    # Verify each entity with the API
    for i, eid in enumerate(unique_ids):
        sys.stdout.write(f"\rVerifying {i+1}/{len(unique_ids)}...")
        sys.stdout.flush()

        is_valid, reason, title = verify_with_api(eid, session)

        if is_valid:
            valid_entities[eid] = title
        else:
            invalid_entities[eid] = (reason, title)
            if "Fake" in reason:
                stats["fake"] += 1
            elif "Trap" in reason:
                stats["trap"] += 1
            elif "< 3" in reason:
                stats["small"] += 1
            else:
                stats["error"] += 1

        time.sleep(0.1)

    print("\n\n" + "=" * 75)
    print("  VERIFICATION SUMMARY")
    print("=" * 75)
    print(f"  Total unique events checked:    {len(unique_ids)}")
    print(f"  Fake (sports/timeline/grouped): {stats['fake']}")
    print(f"  Trap (augmented neg-risk):       {stats['trap']}")
    print(f"  Ignored (< 3 legs):             {stats['small']}")
    print(f"  API errors / not found:         {stats['error']}")
    print(f"  VALID categorical arbs:         {len(valid_entities)}")
    print("=" * 75)

    # Show rejected events for transparency
    if invalid_entities:
        print(f"\n  Rejected events:")
        for eid, (reason, title) in sorted(invalid_entities.items(), key=lambda x: x[1][0]):
            print(f"    {eid:>8} | {reason:<35} | {title[:50]}")
        print()

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
        legs = ev_rows[0]["legs"]
        avg_profit = sum(r["profit_per"] for r in ev_rows) / len(ev_rows)
        total_pot = sum(r["pot_profit"] for r in ev_rows)
        avg_dur = sum(r["duration_ms"] for r in ev_rows) / len(ev_rows)
        avg_vol = sum(r["max_vol"] for r in ev_rows) / len(ev_rows)
        title = valid_entities[eid]

        results.append({
            "id": eid,
            "title": title,
            "legs": legs,
            "windows": len(ev_rows),
            "total_pot": total_pot,
            "avg_profit": avg_profit,
            "avg_dur": avg_dur,
            "avg_vol": avg_vol,
        })

    results.sort(key=lambda x: x["total_pot"], reverse=True)

    # ── Per-event breakdown ──
    print("=" * 115)
    print("  VERIFIED ARB EVENTS (mutually exclusive categorical only)")
    print("=" * 115)
    print(f"  {'Title':<40} {'ID':>8} {'Legs':>4} {'Windows':>7} {'Potential':>10} {'AvgProfit/sh':>13} {'AvgDuration':>12} {'AvgVol':>8}")
    print("  " + "-" * 105)

    total_potential = 0
    for res in results:
        title_trunc = res['title'][:38] + ".." if len(res['title']) > 40 else res['title']
        print(f"  {title_trunc:<40} {res['id']:>8} {res['legs']:>4} {res['windows']:>7} ${res['total_pot']:>8.2f} ${res['avg_profit']:>11.4f} {res['avg_dur']:>10.0f}ms {res['avg_vol']:>7.1f}")
        total_potential += res["total_pot"]

    print("  " + "-" * 105)

    # ── Realistic PnL estimate (non-sports only: >= 1s window, 500ms match latency) ──
    max_deploy = 50.0
    realistic_profit = 0
    realistic_count = 0
    for r in valid_rows:
        if r["duration_ms"] < 500:
            continue
        realistic_count += 1
        deploy_ratio = min(1.0, max_deploy / r["capital_req"]) if r["capital_req"] > 0 else 0
        realistic_profit += r["pot_profit"] * deploy_ratio * 0.70

    # Session duration from first start to last end
    try:
        from datetime import datetime, timedelta
        t_start = datetime.strptime(valid_rows[0]["start"], "%H:%M:%S.%f")
        end_times = [datetime.strptime(r["end"], "%H:%M:%S.%f") for r in valid_rows]
        days_offset = 0
        prev = t_start
        for et in end_times:
            if et < prev and (prev - et).total_seconds() > 3600:
                days_offset += 1
            prev = et
        t_end_adjusted = end_times[-1] + timedelta(days=days_offset)
        session_hours = (t_end_adjusted - t_start).total_seconds() / 3600
    except (ValueError, IndexError):
        session_hours = 0

    print()
    print("  REALISTIC PnL (verified events only)")
    print(f"  Assumptions: >= 500ms windows, 70% capture, ${max_deploy:.0f} max per arb")
    print(f"  Total potential (all windows): ${total_potential:,.2f}")
    print(f"  Eligible windows (>= 500ms):   {realistic_count}/{len(valid_rows)}")
    print(f"  Estimated profit:              ${realistic_profit:,.2f}")
    if session_hours > 0:
        print(f"  Session duration:              {session_hours:.1f} hrs")
        print(f"  Profit per hour:               ${realistic_profit / session_hours:,.2f}/hr")
    print("=" * 115)

if __name__ == "__main__":
    main()