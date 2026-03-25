"""
Analyze replay backtester CSV trades — leaderboard + interactive strategy drill-down.
Reads ReplayTrades_*.csv files produced by the binary replay bot.
Usage: python analyze_replay.py
"""
import pandas as pd
import glob
import os
import re
import csv
import sys


def pair_trades(trades):
    """Pair BUY/SELL trades per asset to calculate per-round-trip P&L.
    Handles partial fills: accumulates sells until the position is fully closed."""
    buy_sides = {'BUY', 'BUY NO'}
    roundtrips = []

    # Group trades by asset, sorted by time
    by_asset = {}
    for t in sorted(trades, key=lambda x: x["ReplayTime"]):
        by_asset.setdefault(t["AssetId"], []).append(t)

    for asset, asset_trades in by_asset.items():
        buy_shares = 0.0
        buy_cost = 0.0
        buy_price = 0.0
        buy_time = None
        sell_shares = 0.0
        sell_proceeds = 0.0
        market = ""
        last_sell_time = None
        last_exit_side = ""

        for t in asset_trades:
            side = t["Side"]
            price = float(t["ExecutionPrice"])
            shares = float(t["Shares"])
            dollars = float(t["DollarValue"])
            market = t["MarketName"]

            if side in buy_sides:
                # Close previous incomplete trip if any
                if buy_shares > 0.01:
                    pnl = sell_proceeds - buy_cost
                    roundtrips.append({
                        "market": market,
                        "buy_price": buy_price,
                        "sell_price": (sell_proceeds / sell_shares) if sell_shares > 0 else 0,
                        "shares": buy_shares,
                        "buy_dollars": buy_cost,
                        "sell_dollars": sell_proceeds,
                        "pnl": pnl,
                        "pnl_pct": (pnl / buy_cost * 100) if buy_cost > 0 else 0,
                        "buy_time": buy_time,
                        "sell_time": last_sell_time or "OPEN",
                        "side": last_exit_side or "OPEN",
                    })

                buy_shares = shares
                buy_cost = dollars
                buy_price = price
                buy_time = t["ReplayTime"]
                sell_shares = 0.0
                sell_proceeds = 0.0
                last_sell_time = None
                last_exit_side = ""

            elif "SELL" in side or "RESOLVE" in side:
                if buy_shares <= 0.01:
                    continue  # Orphan sell with no matching buy — skip

                sell_shares += shares
                sell_proceeds += dollars
                last_sell_time = t["ReplayTime"]
                last_exit_side = side

                if sell_shares >= buy_shares - 0.01:
                    # Round trip complete
                    pnl = sell_proceeds - buy_cost
                    roundtrips.append({
                        "market": market,
                        "buy_price": buy_price,
                        "sell_price": sell_proceeds / sell_shares if sell_shares > 0 else 0,
                        "shares": buy_shares,
                        "buy_dollars": buy_cost,
                        "sell_dollars": sell_proceeds,
                        "pnl": pnl,
                        "pnl_pct": (pnl / buy_cost * 100) if buy_cost > 0 else 0,
                        "buy_time": buy_time,
                        "sell_time": last_sell_time,
                        "side": last_exit_side,
                    })
                    buy_shares = 0.0
                    buy_cost = 0.0
                    sell_shares = 0.0
                    sell_proceeds = 0.0

        # Open position at end of asset
        if buy_shares > 0.01:
            pnl = sell_proceeds - buy_cost if sell_shares > 0 else 0
            roundtrips.append({
                "market": market,
                "buy_price": buy_price,
                "sell_price": (sell_proceeds / sell_shares) if sell_shares > 0 else 0,
                "shares": buy_shares,
                "buy_dollars": buy_cost,
                "sell_dollars": sell_proceeds,
                "pnl": pnl,
                "pnl_pct": (pnl / buy_cost * 100) if buy_cost > 0 else 0,
                "buy_time": buy_time,
                "sell_time": last_sell_time or "OPEN",
                "side": "OPEN",
            })

    return roundtrips


def show_strategy_detail(df_strat, strat_name, capital):
    """Show per-trade round-trip breakdown for a single strategy."""
    trades = df_strat.to_dict('records')
    roundtrips = pair_trades(trades)
    roundtrips.sort(key=lambda x: x["pnl"], reverse=True)

    total_pnl = sum(r["pnl"] for r in roundtrips if r["side"] != "OPEN")
    wins = sum(1 for r in roundtrips if r["pnl"] > 0 and r["side"] != "OPEN")
    losses = sum(1 for r in roundtrips if r["pnl"] <= 0 and r["side"] != "OPEN")
    closed = wins + losses
    open_count = sum(1 for r in roundtrips if r["side"] == "OPEN")

    print(f"\n{'='*100}")
    print(f"  Strategy: {strat_name}")
    print(f"  Starting Capital: ${capital:.2f}")
    print(f"  Total P&L: ${total_pnl:+.2f} ({total_pnl/capital*100:+.1f}%)")
    print(f"  Trades: {closed} closed ({wins}W / {losses}L)", end="")
    if closed > 0:
        print(f" | Win Rate: {wins/closed*100:.0f}%", end="")
    if open_count > 0:
        print(f" | {open_count} open", end="")
    print(f"\n{'='*100}")

    if not roundtrips:
        print("  No round-trip trades.")
        return

    print(f"  {'P&L':>9}  {'P&L%':>7}  {'Buy@':>6}  {'Sell@':>6}  {'Shares':>8}  {'$In':>8}  {'$Out':>8}  {'Exit':>7}  Market")
    print(f"  {'-'*9}  {'-'*7}  {'-'*6}  {'-'*6}  {'-'*8}  {'-'*8}  {'-'*8}  {'-'*7}  {'-'*30}")

    for r in roundtrips:
        pnl_color = "\033[32m" if r["pnl"] > 0 else "\033[31m" if r["pnl"] < 0 else "\033[90m"
        reset = "\033[0m"
        market = r["market"][:35] if len(r["market"]) > 35 else r["market"]
        exit_type = r["side"].replace("RESOLVE YES", "SETTLE").replace("RESOLVE NO", "SETTLE")

        print(f"  {pnl_color}${r['pnl']:>+8.2f}{reset}  {r['pnl_pct']:>+6.1f}%  ${r['buy_price']:>5.2f}  ${r['sell_price']:>5.2f}  {r['shares']:>8.2f}  ${r['buy_dollars']:>7.2f}  ${r['sell_dollars']:>7.2f}  {exit_type:>7}  {market}")


def analyze():
    # Find the most recent ReplayTrades CSV
    csv_files = glob.glob("ReplayTrades_*.csv") + glob.glob("PredictionLiveTrader/ReplayTrades_*.csv")
    csv_files = [f for f in csv_files if os.path.getsize(f) > 0]

    if not csv_files:
        print("No ReplayTrades_*.csv files found!")
        return

    latest_file = max(csv_files, key=os.path.getmtime)
    print(f"\n  Reading: {latest_file}")

    df = pd.read_csv(latest_file)
    if df.empty:
        print("  CSV is empty — no trades yet.")
        return

    # Use ReplayTime for time-based analysis (market time)
    df['ReplayTime'] = pd.to_datetime(df['ReplayTime'])

    buy_sides = {'BUY', 'BUY NO'}
    sell_sides = {'SELL', 'SELL NO', 'RESOLVE YES', 'RESOLVE NO'}

    df['CashFlow'] = df.apply(
        lambda row: -row['DollarValue'] if row['Side'] in buy_sides else row['DollarValue'],
        axis=1
    )

    # Starting capital per strategy
    if 'StartingCapital' in df.columns:
        starting_caps = df.drop_duplicates('StrategyName').set_index('StrategyName')['StartingCapital'].to_dict()
    else:
        starting_caps = {}

    # Extract strategy parameters
    df['StrategyType'] = df['StrategyName'].apply(lambda x: x.split('_v')[0].split('_Ultra')[0])

    def extract_params(name):
        params = re.findall(r'_([A-Z]+)([0-9.]+)', name)
        return {k: float(v) for k, v in params}

    df['Params'] = df['StrategyName'].apply(extract_params)

    # ==========================================
    # LEADERBOARD
    # ==========================================
    leaderboard = df.groupby('StrategyName').agg(
        Total_PnL=('CashFlow', 'sum'),
        Buys=('Side', lambda x: x.isin(buy_sides).sum()),
        Sells=('Side', lambda x: x.isin(sell_sides).sum()),
    ).reset_index()

    # Round-trip win/loss tracking
    round_trip_stats = {}
    for strat, strat_df in df.sort_values('ReplayTime').groupby('StrategyName'):
        wins = 0
        losses = 0
        open_trips = 0
        for asset, asset_df in strat_df.groupby('AssetId'):
            trip_pnl = 0.0
            in_trip = False
            for _, row in asset_df.iterrows():
                if row['Side'] in buy_sides:
                    if in_trip:
                        if trip_pnl >= 0:
                            wins += 1
                        else:
                            losses += 1
                    in_trip = True
                    trip_pnl = row['CashFlow']
                elif row['Side'] in sell_sides:
                    trip_pnl += row['CashFlow']
            if in_trip:
                yes_held = asset_df[asset_df['Side'] == 'BUY']['Shares'].sum() - asset_df[asset_df['Side'].isin(['SELL', 'RESOLVE YES'])]['Shares'].sum()
                no_held = asset_df[asset_df['Side'] == 'BUY NO']['Shares'].sum() - asset_df[asset_df['Side'].isin(['SELL NO', 'RESOLVE NO'])]['Shares'].sum()
                if abs(yes_held) < 0.01 and abs(no_held) < 0.01:
                    if trip_pnl >= 0:
                        wins += 1
                    else:
                        losses += 1
                else:
                    open_trips += 1
        round_trip_stats[strat] = {'Wins': wins, 'Losses': losses, 'Open': open_trips}

    rt_df = pd.DataFrame(round_trip_stats).T.reset_index().rename(columns={'index': 'StrategyName'})
    leaderboard = leaderboard.merge(rt_df, on='StrategyName', how='left')
    leaderboard[['Wins', 'Losses', 'Open']] = leaderboard[['Wins', 'Losses', 'Open']].fillna(0).astype(int)

    # Mark-to-Market
    last_prices = df.sort_values('ReplayTime').groupby('AssetId')['ExecutionPrice'].last().to_dict()

    df['Yes_Share_Change'] = df.apply(lambda r: r['Shares'] if r['Side'] == 'BUY' else (-r['Shares'] if r['Side'] in ('SELL', 'RESOLVE YES') else 0), axis=1)
    df['No_Share_Change'] = df.apply(lambda r: r['Shares'] if r['Side'] == 'BUY NO' else (-r['Shares'] if r['Side'] in ('SELL NO', 'RESOLVE NO') else 0), axis=1)

    inventory = df.groupby('StrategyName').agg(
        Net_Yes_Shares=('Yes_Share_Change', 'sum'),
        Net_No_Shares=('No_Share_Change', 'sum')
    ).reset_index()

    LIQUIDITY_HAIRCUT = 0.07
    inventory['MarkToMarket_Value'] = 0.0

    for idx, row in inventory.iterrows():
        strat = row['StrategyName']
        strat_trades = df[df['StrategyName'] == strat]
        open_assets = strat_trades['AssetId'].unique()
        strat_mtm = 0.0
        for asset in open_assets:
            asset_trades = strat_trades[strat_trades['AssetId'] == asset]
            yes_held = asset_trades[asset_trades['Side'] == 'BUY']['Shares'].sum() - asset_trades[asset_trades['Side'].isin(['SELL', 'RESOLVE YES'])]['Shares'].sum()
            no_held = asset_trades[asset_trades['Side'] == 'BUY NO']['Shares'].sum() - asset_trades[asset_trades['Side'].isin(['SELL NO', 'RESOLVE NO'])]['Shares'].sum()
            last_price = last_prices.get(asset, 0)
            if yes_held > 0:
                strat_mtm += (yes_held * last_price)
            if no_held > 0:
                strat_mtm += (no_held * (1 - last_price))
        inventory.at[idx, 'MarkToMarket_Value'] = strat_mtm * (1 - LIQUIDITY_HAIRCUT)

    leaderboard = leaderboard.merge(inventory[['StrategyName', 'MarkToMarket_Value']], on='StrategyName', how='left')
    leaderboard['MarkToMarket_Value'] = leaderboard['MarkToMarket_Value'].fillna(0.0)

    leaderboard['StartingCapital'] = leaderboard['StrategyName'].map(starting_caps).fillna(1000.0)
    leaderboard['True_Total_Equity'] = leaderboard['StartingCapital'] + leaderboard['Total_PnL'] + leaderboard['MarkToMarket_Value']
    leaderboard['True_PnL'] = leaderboard['True_Total_Equity'] - leaderboard['StartingCapital']
    leaderboard['Worst_Case_Equity'] = leaderboard['StartingCapital'] + leaderboard['Total_PnL']
    leaderboard['Worst_Case_PnL'] = leaderboard['Worst_Case_Equity'] - leaderboard['StartingCapital']

    strat_time_span = df.groupby('StrategyName')['ReplayTime'].agg(['min', 'max'])
    strat_time_span['Hours'] = (strat_time_span['max'] - strat_time_span['min']).dt.total_seconds() / 3600.0
    strat_time_span['Hours'] = strat_time_span['Hours'].clip(lower=0.1)
    leaderboard = leaderboard.merge(strat_time_span[['Hours']], left_on='StrategyName', right_index=True, how='left')
    leaderboard['Hourly_PnL'] = leaderboard['True_PnL'] / leaderboard['Hours']

    # Format and display
    leaderboard = leaderboard.sort_values('True_Total_Equity', ascending=False)
    closed_trips = leaderboard['Wins'] + leaderboard['Losses']
    total_trips = closed_trips + leaderboard['Open']
    leaderboard['WinRate%'] = (leaderboard['Wins'] / closed_trips.replace(0, 1) * 100).apply(lambda x: f"{x:.0f}%")
    leaderboard['Closure%'] = (closed_trips / total_trips.replace(0, 1) * 100).apply(lambda x: f"{x:.0f}%")
    leaderboard['W/L'] = leaderboard.apply(lambda r: f"{r['Wins']}W/{r['Losses']}L/{r['Open']}O", axis=1)
    leaderboard['Equity'] = leaderboard['True_Total_Equity'].apply(lambda x: f"${x:,.2f}")
    leaderboard['True_PnL_fmt'] = leaderboard['True_PnL'].apply(lambda x: f"${x:,.2f}")
    leaderboard['Cash_Left'] = leaderboard['Worst_Case_Equity'].apply(lambda x: f"${x:,.2f}")
    leaderboard['MTM_Value'] = leaderboard['MarkToMarket_Value'].apply(lambda x: f"${x:,.2f}")
    leaderboard['Worst_PnL'] = leaderboard['Worst_Case_PnL'].apply(lambda x: f"${x:,.2f}")
    leaderboard['Hours_fmt'] = leaderboard['Hours'].apply(lambda x: f"{x:.1f}h")
    leaderboard['PnL_hr'] = leaderboard['Hourly_PnL'].apply(lambda x: f"${x:,.2f}/hr")

    print("\n  STRATEGY LEADERBOARD (Sorted by Equity)")
    print("  " + "-" * 120)
    print(leaderboard[['StrategyName', 'Equity', 'True_PnL_fmt', 'PnL_hr', 'Cash_Left', 'MTM_Value', 'Worst_PnL', 'W/L', 'WinRate%', 'Closure%', 'Hours_fmt']].to_string(index=False))

    # ==========================================
    # PARAMETER IMPACT
    # ==========================================
    param_names = {'T': 'Threshold', 'W': 'Window', 'P': 'TakeProfit', 'S': 'StopLoss', 'MS': 'SustainMs', 'ES': 'EntrySlippage', 'XS': 'ExitSlippage'}

    strategy_pnl = df.groupby('StrategyName')['CashFlow'].sum().reset_index()
    strategy_pnl.columns = ['StrategyName', 'StrategyPnL']

    all_param_keys = df['Params'].apply(lambda d: list(d.keys())).explode().dropna().unique()
    if len(all_param_keys) > 0:
        print(f"\n  PARAMETER IMPACT (Which settings work best?)")
        print("  " + "-" * 80)

        for param_letter in sorted(all_param_keys):
            param_label = param_names.get(param_letter, param_letter)
            param_vals = df.drop_duplicates('StrategyName')[['StrategyName', 'Params']].copy()
            param_vals['ParamValue'] = param_vals['Params'].apply(lambda d: d.get(param_letter))
            param_vals = param_vals.dropna(subset=['ParamValue'])
            if param_vals.empty:
                continue

            param_vals = param_vals.merge(strategy_pnl, on='StrategyName')
            summary = param_vals.groupby('ParamValue').agg(
                Avg_PnL=('StrategyPnL', 'mean'),
                Strategies=('StrategyName', 'count')
            ).reset_index()
            summary = summary.sort_values('Avg_PnL', ascending=False)
            summary['Avg_PnL'] = summary['Avg_PnL'].apply(lambda x: f"${x:,.2f}")

            print(f"  [{param_label}] (param '{param_letter}')")
            print(summary.to_string(index=False))
            print()

    # ==========================================
    # HOURLY REGIME (using market time)
    # ==========================================
    print("  HOURLY REGIME ANALYSIS (Market Time)")
    print("  " + "-" * 120)

    df['Hour'] = df['ReplayTime'].dt.floor('h')
    hourly_strat = df.groupby(['Hour', 'StrategyName'])['CashFlow'].sum().reset_index()

    for hour in sorted(hourly_strat['Hour'].unique()):
        hour_data = hourly_strat[hourly_strat['Hour'] == hour].sort_values('CashFlow', ascending=False)
        hour_total = hour_data['CashFlow'].sum()
        top3 = hour_data.head(3)
        bot3 = hour_data.tail(3).sort_values('CashFlow', ascending=True)

        print(f"\n  [{pd.Timestamp(hour).strftime('%Y-%m-%d %H:%M')}] Net: ${hour_total:,.2f}")
        for _, r in top3.iterrows():
            print(f"    + {r['StrategyName']:<50} ${r['CashFlow']:>10,.2f}")
        for _, r in bot3.iterrows():
            if r['CashFlow'] < 0:
                print(f"    - {r['StrategyName']:<50} ${r['CashFlow']:>10,.2f}")

    # ==========================================
    # INTERACTIVE DRILL-DOWN
    # ==========================================
    strat_names = sorted(df['StrategyName'].unique())

    while True:
        print(f"\n  Available strategies ({len(strat_names)}):")
        for i, s in enumerate(strat_names):
            cap = starting_caps.get(s, 1000)
            row = leaderboard[leaderboard['StrategyName'] == s]
            if not row.empty:
                print(f"    {i+1:>3}. {s:<50} {row.iloc[0]['Equity']:>12}  {row.iloc[0]['W/L']}")
            else:
                print(f"    {i+1:>3}. {s}")

        query = input("\n  Enter strategy name/number (or 'q' to quit): ").strip()
        if query.lower() in ('q', 'quit', ''):
            break

        # Support number selection
        if query.isdigit():
            idx = int(query) - 1
            if 0 <= idx < len(strat_names):
                query = strat_names[idx]

        matched = [s for s in strat_names if query.lower() in s.lower()]
        if not matched:
            print(f"  No strategies matching '{query}'.")
            continue

        for strat_name in matched:
            strat_df = df[df['StrategyName'] == strat_name].copy()
            capital = starting_caps.get(strat_name, 1000.0)
            show_strategy_detail(strat_df, strat_name, capital)


if __name__ == "__main__":
    analyze()
