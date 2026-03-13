"""
Filter paper trading CSV logs by strategy name, show trades sorted by profitability.
Usage: python view.py [strategy_filter]
"""
import csv, sys, os, glob

def load_trades(filepath):
    trades = []
    with open(filepath, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            trades.append(row)
    return trades

def pair_trades(trades):
    """Pair BUY/SELL trades per asset to calculate per-round-trip P&L."""
    positions = {}  # asset -> list of open buys
    roundtrips = []

    for t in sorted(trades, key=lambda x: x["Timestamp"]):
        asset = t["AssetId"]
        side = t["Side"]
        price = float(t["ExecutionPrice"])
        shares = float(t["Shares"])
        dollars = float(t["DollarValue"])
        market = t["MarketName"]

        if "BUY" in side:
            positions.setdefault(asset, []).append(t)
        elif "SELL" in side or "RESOLVE" in side:
            buys = positions.get(asset, [])
            if buys:
                buy = buys.pop(0)
                buy_dollars = float(buy["DollarValue"])
                pnl = dollars - buy_dollars
                roundtrips.append({
                    "market": market,
                    "buy_price": float(buy["ExecutionPrice"]),
                    "sell_price": price,
                    "shares": float(buy["Shares"]),
                    "buy_dollars": buy_dollars,
                    "sell_dollars": dollars,
                    "pnl": pnl,
                    "pnl_pct": (pnl / buy_dollars * 100) if buy_dollars > 0 else 0,
                    "buy_time": buy["Timestamp"],
                    "sell_time": t["Timestamp"],
                    "side": side,
                })
            else:
                # Sell without a matching buy (orphan)
                roundtrips.append({
                    "market": market,
                    "buy_price": 0,
                    "sell_price": price,
                    "shares": shares,
                    "buy_dollars": 0,
                    "sell_dollars": dollars,
                    "pnl": dollars,
                    "pnl_pct": 0,
                    "buy_time": "N/A",
                    "sell_time": t["Timestamp"],
                    "side": side,
                })

    # Open positions (bought but not sold yet)
    for asset, buys in positions.items():
        for buy in buys:
            roundtrips.append({
                "market": buy["MarketName"],
                "buy_price": float(buy["ExecutionPrice"]),
                "sell_price": 0,
                "shares": float(buy["Shares"]),
                "buy_dollars": float(buy["DollarValue"]),
                "sell_dollars": 0,
                "pnl": 0,
                "pnl_pct": 0,
                "buy_time": buy["Timestamp"],
                "sell_time": "OPEN",
                "side": "OPEN",
            })

    return roundtrips

def main():
    # Find the most recent LivePaperTrades CSV (excluding summary files)
    csvs = sorted(glob.glob("LivePaperTrades*.csv"), key=os.path.getmtime, reverse=True)
    csvs = [f for f in csvs if "_summary" not in f.lower()]

    if not csvs:
        print("No LivePaperTrades CSV files found.")
        return

    csv_file = csvs[0]
    print(f"Reading: {csv_file}")

    # Get filter
    if len(sys.argv) > 1:
        query = " ".join(sys.argv[1:])
    else:
        query = input("Enter strategy name (or partial match): ").strip()

    if not query:
        print("No filter provided.")
        return

    # Load trades from the single file
    all_trades = load_trades(csv_file)

    filtered = [t for t in all_trades if query.lower() in t.get("StrategyName", "").lower()]

    if not filtered:
        print(f"No trades found matching '{query}'.")
        # Show available strategies
        strats = sorted(set(t.get("StrategyName", "") for t in all_trades))
        if strats:
            print(f"\nAvailable strategies ({len(strats)}):")
            for s in strats[:30]:
                print(f"  {s}")
            if len(strats) > 30:
                print(f"  ... and {len(strats) - 30} more")
        return

    # Get unique strategies matching
    matched_strats = sorted(set(t["StrategyName"] for t in filtered))

    for strat_name in matched_strats:
        strat_trades = [t for t in filtered if t["StrategyName"] == strat_name]
        capital = float(strat_trades[0].get("StartingCapital", 1000))

        roundtrips = pair_trades(strat_trades)
        roundtrips.sort(key=lambda x: x["pnl"], reverse=True)

        total_pnl = sum(r["pnl"] for r in roundtrips if r["side"] != "OPEN")
        wins = sum(1 for r in roundtrips if r["pnl"] > 0 and r["side"] != "OPEN")
        losses = sum(1 for r in roundtrips if r["pnl"] <= 0 and r["side"] != "OPEN")
        closed = wins + losses
        open_count = sum(1 for r in roundtrips if r["side"] == "OPEN")

        print(f"\n{'='*80}")
        print(f"  Strategy: {strat_name}")
        print(f"  Starting Capital: ${capital:.2f}")
        print(f"  Total P&L: ${total_pnl:+.2f} ({total_pnl/capital*100:+.1f}%)")
        print(f"  Trades: {closed} closed ({wins}W / {losses}L)", end="")
        if closed > 0:
            print(f" | Win Rate: {wins/closed*100:.0f}%", end="")
        if open_count > 0:
            print(f" | {open_count} open", end="")
        print(f"\n{'='*80}")

        if not roundtrips:
            print("  No round-trip trades.")
            continue

        print(f"  {'P&L':>9}  {'P&L%':>7}  {'Buy@':>6}  {'Sell@':>6}  {'Shares':>8}  {'$In':>8}  {'$Out':>8}  {'Exit':>7}  Market")
        print(f"  {'-'*9}  {'-'*7}  {'-'*6}  {'-'*6}  {'-'*8}  {'-'*8}  {'-'*8}  {'-'*7}  {'-'*30}")

        for r in roundtrips:
            pnl_color = "\033[32m" if r["pnl"] > 0 else "\033[31m" if r["pnl"] < 0 else "\033[90m"
            reset = "\033[0m"
            market = r["market"][:35] if len(r["market"]) > 35 else r["market"]
            exit_type = r["side"].replace("SELL", "SELL").replace("RESOLVE YES", "SETTLE").replace("RESOLVE NO", "SETTLE")

            print(f"  {pnl_color}${r['pnl']:>+8.2f}{reset}  {r['pnl_pct']:>+6.1f}%  ${r['buy_price']:>5.2f}  ${r['sell_price']:>5.2f}  {r['shares']:>8.2f}  ${r['buy_dollars']:>7.2f}  ${r['sell_dollars']:>7.2f}  {exit_type:>7}  {market}")

if __name__ == "__main__":
    main()
