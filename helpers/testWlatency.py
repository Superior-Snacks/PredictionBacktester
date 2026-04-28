"""
Hold-time reality check: flags trades that settled faster than physically possible.
Uses the same CSV discovery as analyze_trades.py.
Usage: python testWlatency.py [min_hold_seconds]
"""
import pandas as pd
import glob
import os
import sys
import shutil
from pathlib import Path

_ROOT = Path(__file__).parent.parent  # PredictionBacktester/

MIN_HOLD_SECONDS = int(sys.argv[1]) if len(sys.argv) > 1 else 5

def main():
    # Same file discovery as analyze_trades.py
    csv_files = (glob.glob(str(_ROOT / "PredictionLiveTrader/LivePaperTrades_*.csv"))
               + glob.glob(str(_ROOT / "LivePaperTrades_*.csv"))
               + glob.glob(str(_ROOT / "PredictionLiveTrader/bin/Release/**/LivePaperTrades_*.csv"), recursive=True))
    valid_files = [f for f in csv_files if "SNAPSHOT" not in f and "_summary" not in f]

    if valid_files:
        latest_file = max(valid_files, key=os.path.getctime)
        snapshot_file = str(_ROOT / "LivePaperTrades_SNAPSHOT.csv")
        try:
            shutil.copy2(latest_file, snapshot_file)
            print(f"\nSnapshot: {os.path.basename(latest_file)}")
        except Exception as e:
            print(f"Failed to create snapshot: {e}")
            return
    else:
        # Fallback: use existing snapshot files directly (check both locations)
        snapshot_candidates = glob.glob(str(_ROOT / "LivePaperTrades_SNAPSHOT*.csv")) + [f for f in csv_files if "SNAPSHOT" in f and "_summary" not in f]
        if not snapshot_candidates:
            print("No CSV files found!")
            return
        snapshot_file = max(snapshot_candidates, key=os.path.getctime)
        latest_file = snapshot_file
        print(f"\nUsing existing snapshot: {os.path.basename(snapshot_file)}")

    df = pd.read_csv(snapshot_file)
    if df.empty:
        print("CSV is empty.")
        return

    df['Timestamp'] = pd.to_datetime(df['Timestamp'])

    buy_sides = {'BUY', 'BUY NO'}
    sell_sides = {'SELL', 'SELL NO', 'RESOLVE YES', 'RESOLVE NO'}
    threshold = pd.Timedelta(seconds=MIN_HOLD_SECONDS)

    print("=" * 100)
    print(f"  HOLD-TIME REALITY CHECK (threshold: {MIN_HOLD_SECONDS}s)")
    print("=" * 100)

    results = []

    for strat_name, strat_df in df.groupby('StrategyName'):
        strat_df = strat_df.sort_values('Timestamp')

        # FIFO pairing per asset
        positions = {}  # asset -> list of buy rows
        matched = []

        for _, row in strat_df.iterrows():
            asset = row['AssetId']
            side = row['Side']

            if side in buy_sides:
                positions.setdefault(asset, []).append(row)
            elif side in sell_sides:
                buys = positions.get(asset, [])
                if buys:
                    buy = buys.pop(0)
                    hold_time = row['Timestamp'] - buy['Timestamp']
                    pnl = row['DollarValue'] - buy['DollarValue']
                    matched.append({
                        'hold_time': hold_time,
                        'pnl': pnl,
                        'buy_dollars': buy['DollarValue'],
                        'sell_dollars': row['DollarValue'],
                        'asset': asset,
                        'exit_type': side,
                    })

        if not matched:
            continue

        total = len(matched)
        impossible = [t for t in matched if t['hold_time'] < threshold]
        realistic = [t for t in matched if t['hold_time'] >= threshold]

        # Fair pessimistic: losing impossible trades = total loss, winning = $0 (strip profit, return capital)
        impossible_losers = [t for t in impossible if t['pnl'] <= 0]
        impossible_penalty = sum(-t['buy_dollars'] for t in impossible_losers)  # total wipeout
        # winners (pnl > 0): strip the profit but don't destroy capital (pnl contribution = $0)

        realistic_pnl = sum(t['pnl'] for t in realistic)
        pessimistic_pnl = realistic_pnl + impossible_penalty

        avg_hold = pd.Timedelta(seconds=sum(t['hold_time'].total_seconds() for t in matched) / total) if total else pd.Timedelta(0)
        min_hold = min(t['hold_time'] for t in matched)

        capital = float(strat_df.iloc[0].get('StartingCapital', 1000))

        results.append({
            'strategy': strat_name,
            'total': total,
            'realistic': len(realistic),
            'impossible': len(impossible),
            'pct_impossible': len(impossible) / total * 100,
            'realistic_pnl': realistic_pnl,
            'impossible_penalty': impossible_penalty,
            'pessimistic_pnl': pessimistic_pnl,
            'capital': capital,
            'pessimistic_roi': pessimistic_pnl / capital * 100,
            'avg_hold': avg_hold,
            'min_hold': min_hold,
        })

    if not results:
        print("  No matched round-trip trades found.")
        return

    # Sort by pessimistic PnL (best strategies survive the stress test)
    results.sort(key=lambda r: r['pessimistic_pnl'], reverse=True)

    # Summary table
    print(f"\n  Fair pessimistic: impossible losers (<{MIN_HOLD_SECONDS}s) = total loss, impossible winners = profit stripped ($0)\n")
    print(f"  {'Strategy':<55} {'Total':>5} {'Real':>5} {'Fake':>5}  {'Real PnL':>10} {'Fake Loss':>10} {'Net PnL':>10} {'ROI%':>7}  {'Avg Hold':>10}")
    print(f"  {'-'*55} {'-'*5} {'-'*5} {'-'*5}  {'-'*10} {'-'*10} {'-'*10} {'-'*7}  {'-'*10}")

    totals = {'total': 0, 'realistic': 0, 'impossible': 0, 'realistic_pnl': 0, 'impossible_penalty': 0, 'pessimistic_pnl': 0}

    for r in results:
        name = r['strategy'][:55]
        avg_str = format_timedelta(r['avg_hold'])

        color = "\033[32m" if r['pessimistic_pnl'] > 0 else "\033[31m"
        reset = "\033[0m"

        print(f"  {name:<55} {r['total']:>5} {r['realistic']:>5} {r['impossible']:>5}  ${r['realistic_pnl']:>+9.2f} ${r['impossible_penalty']:>+9.2f} {color}${r['pessimistic_pnl']:>+9.2f}{reset} {color}{r['pessimistic_roi']:>+6.1f}%{reset}  {avg_str:>10}")

        totals['total'] += r['total']
        totals['realistic'] += r['realistic']
        totals['impossible'] += r['impossible']
        totals['realistic_pnl'] += r['realistic_pnl']
        totals['impossible_penalty'] += r['impossible_penalty']
        totals['pessimistic_pnl'] += r['pessimistic_pnl']

    # Grand totals
    print(f"\n  {'TOTAL':<55} {totals['total']:>5} {totals['realistic']:>5} {totals['impossible']:>5}  ${totals['realistic_pnl']:>+9.2f} ${totals['impossible_penalty']:>+9.2f} ${totals['pessimistic_pnl']:>+9.2f}")

    # Count profitable strategies
    profitable = sum(1 for r in results if r['pessimistic_pnl'] > 0)
    print(f"\n  {profitable}/{len(results)} strategies remain profitable after stress test")


def format_timedelta(td):
    total_secs = td.total_seconds()
    if total_secs < 60:
        return f"{total_secs:.1f}s"
    elif total_secs < 3600:
        return f"{total_secs/60:.1f}m"
    elif total_secs < 86400:
        return f"{total_secs/3600:.1f}h"
    else:
        return f"{total_secs/86400:.1f}d"


if __name__ == "__main__":
    main()
