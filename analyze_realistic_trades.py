import pandas as pd
import os
import re

# ==========================================
# SIMULATION SETTINGS
# ==========================================
TARGET_CSV_FILE = "OLDLivePaperTrades_SNAPSHOT.csv"  # <--- Type your exact filename here!
MIN_SHARES_REQUIRED = 5.0  # Change this if you want to test stricter limits

def analyze_specific_run():
    print("="*80)
    print(f"📊 ANALYZING REALISTIC DATA: {TARGET_CSV_FILE} (Min Size: {MIN_SHARES_REQUIRED})")
    print("="*80)

    if not os.path.exists(TARGET_CSV_FILE):
        print(f"❌ Error: Could not find '{TARGET_CSV_FILE}' in the current folder.")
        return

    # Load the data
    df = pd.read_csv(TARGET_CSV_FILE)

    if df.empty:
        print("CSV is empty — no trades to analyze yet.")
        return

    df['Timestamp'] = pd.to_datetime(df['Timestamp'])
    df = df.sort_values('Timestamp').reset_index(drop=True)

    # ==========================================
    # THE REALISTIC EXCHANGE SIMULATOR
    # ==========================================
    valid_flags, illegal_buy_flags, ghost_sell_flags = [], [], []
    real_shares, real_dollars = [], []
    
    # State tracking: (Strategy, Asset, YES/NO) -> {'shares': 0.0, 'cost': 0.0}
    inventory = {} 

    for idx, row in df.iterrows():
        strat = row['StrategyName']
        asset = row['AssetId']
        side = str(row['Side']).upper().strip()
        shares = float(row['Shares'])
        dollars = float(row['DollarValue'])
        price = float(row['ExecutionPrice'])

        is_yes = side in ['BUY', 'SELL', 'RESOLVE YES']
        tok_type = 'YES' if is_yes else 'NO'
        inv_key = (strat, asset, tok_type)

        if inv_key not in inventory:
            inventory[inv_key] = {'shares': 0.0, 'cost': 0.0}

        if side in ['BUY', 'BUY NO']:
            if shares < MIN_SHARES_REQUIRED:
                # Illegal buy - blocked by exchange limits
                valid_flags.append(False)
                illegal_buy_flags.append(True)
                ghost_sell_flags.append(False)
                real_shares.append(0)
                real_dollars.append(0)
            else:
                # Valid buy
                valid_flags.append(True)
                illegal_buy_flags.append(False)
                ghost_sell_flags.append(False)
                real_shares.append(shares)
                real_dollars.append(dollars)
                inventory[inv_key]['shares'] += shares
                inventory[inv_key]['cost'] += dollars
        else:
            # SELLs and RESOLVEs
            curr_shares = inventory[inv_key]['shares']
            if curr_shares <= 0.0001:
                # Ghost sell - we don't own these shares because the buy was blocked
                valid_flags.append(False)
                illegal_buy_flags.append(False)
                ghost_sell_flags.append(True)
                real_shares.append(0)
                real_dollars.append(0)
            else:
                # Valid sell - clamp to what we actually own
                valid_flags.append(True)
                illegal_buy_flags.append(False)
                ghost_sell_flags.append(False)

                actual_sell = min(shares, curr_shares)
                sell_ratio = actual_sell / curr_shares
                cost_basis = inventory[inv_key]['cost'] * sell_ratio
                actual_revenue = actual_sell * price

                real_shares.append(actual_sell)
                real_dollars.append(actual_revenue)

                inventory[inv_key]['shares'] -= actual_sell
                inventory[inv_key]['cost'] -= cost_basis

    # Apply the realistic filters back to the dataframe
    df['Illegal_Buy'] = illegal_buy_flags
    df['Ghost_Sell'] = ghost_sell_flags
    df['Valid_Trade'] = valid_flags
    
    # Save the error counts per strategy before filtering
    error_counts = df.groupby('StrategyName').agg(
        Ill_Buys=('Illegal_Buy', 'sum'),
        Gh_Sells=('Ghost_Sell', 'sum')
    ).reset_index()

    # Overwrite the original columns with the clamped realistic amounts
    df['Shares'] = real_shares
    df['DollarValue'] = real_dollars

    # DROPPING ALL ILLEGAL TRADES from the rest of the analysis!
    df = df[df['Valid_Trade'] == True].copy()

    # Handle all trade sides correctly
    buy_sides = {'BUY', 'BUY NO'}
    sell_sides = {'SELL', 'SELL NO', 'RESOLVE YES', 'RESOLVE NO'}
    
    df['CashFlow'] = df.apply(
        lambda row: -row['DollarValue'] if row['Side'] in buy_sides else row['DollarValue'],
        axis=1
    )

    # Load reject counts from summary CSV if it exists
    summary_file = TARGET_CSV_FILE.replace(".csv", "_summary.csv")
    reject_counts = {}
    if os.path.exists(summary_file):
        try:
            summary_df = pd.read_csv(summary_file)
            reject_counts = dict(zip(summary_df['StrategyName'], summary_df['RejectedOrders']))
        except Exception:
            pass

    if 'StartingCapital' in df.columns:
        starting_caps = df.drop_duplicates('StrategyName').set_index('StrategyName')['StartingCapital'].to_dict()
    else:
        starting_caps = {}

    df['StrategyType'] = df['StrategyName'].apply(lambda x: x.split('_v')[0].split('_Ultra')[0])

    def extract_params(name):
        params = re.findall(r'_([A-Z]+)([0-9.]+)', name)
        return {k: float(v) for k, v in params}

    df['Params'] = df['StrategyName'].apply(extract_params)

    # ==========================================
    # DASHBOARD 1: STRATEGY LEADERBOARD
    # ==========================================
    leaderboard = df.groupby('StrategyName').agg(
        Total_PnL=('CashFlow', 'sum'),
        Buys=('Side', lambda x: x.isin(buy_sides).sum()),
        Sells=('Side', lambda x: x.isin(sell_sides).sum()),
    ).reset_index()
    
    leaderboard['Rejects'] = leaderboard['StrategyName'].map(reject_counts).fillna(0).astype(int)

    # Merge our new error counts
    leaderboard = leaderboard.merge(error_counts, on='StrategyName', how='left')
    leaderboard['Ill_Buys'] = leaderboard['Ill_Buys'].fillna(0).astype(int)
    leaderboard['Gh_Sells'] = leaderboard['Gh_Sells'].fillna(0).astype(int)

    # --- Mark-to-Market calculations ---
    last_prices = df.sort_values('Timestamp').groupby('AssetId')['ExecutionPrice'].last().to_dict()

    df['Yes_Share_Change'] = df.apply(lambda r: r['Shares'] if r['Side'] == 'BUY' else (-r['Shares'] if r['Side'] in ('SELL', 'RESOLVE YES') else 0), axis=1)
    df['No_Share_Change'] = df.apply(lambda r: r['Shares'] if r['Side'] == 'BUY NO' else (-r['Shares'] if r['Side'] in ('SELL NO', 'RESOLVE NO') else 0), axis=1)

    inventory_df = df.groupby('StrategyName').agg(
        Net_Yes_Shares=('Yes_Share_Change', 'sum'),
        Net_No_Shares=('No_Share_Change', 'sum')
    ).reset_index()

    LIQUIDITY_HAIRCUT = 0.07
    inventory_df['MarkToMarket_Value'] = 0.0

    for idx, row in inventory_df.iterrows():
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
        inventory_df.at[idx, 'MarkToMarket_Value'] = strat_mtm * (1 - LIQUIDITY_HAIRCUT)

    leaderboard = leaderboard.merge(inventory_df[['StrategyName', 'MarkToMarket_Value']], on='StrategyName', how='left')
    leaderboard['MarkToMarket_Value'] = leaderboard['MarkToMarket_Value'].fillna(0.0)

    leaderboard['StartingCapital'] = leaderboard['StrategyName'].map(starting_caps).fillna(1000.0)
    leaderboard['True_Total_Equity'] = leaderboard['StartingCapital'] + leaderboard['Total_PnL'] + leaderboard['MarkToMarket_Value']
    leaderboard['True_PnL'] = leaderboard['True_Total_Equity'] - leaderboard['StartingCapital']

    strat_time_span = df.groupby('StrategyName')['Timestamp'].agg(['min', 'max'])
    strat_time_span['Hours'] = (strat_time_span['max'] - strat_time_span['min']).dt.total_seconds() / 3600.0
    strat_time_span['Hours'] = strat_time_span['Hours'].clip(lower=0.1)
    leaderboard = leaderboard.merge(strat_time_span[['Hours']], left_on='StrategyName', right_index=True, how='left')
    leaderboard['Hourly_PnL'] = leaderboard['True_PnL'] / leaderboard['Hours']

    # Sort and format
    leaderboard = leaderboard.sort_values('True_Total_Equity', ascending=False)
    leaderboard['WinRate%'] = (leaderboard['Sells'] / (leaderboard['Buys'] + leaderboard['Sells']) * 100).fillna(0).apply(lambda x: f"{x:.1f}%")
    leaderboard['MTM_Value'] = leaderboard['MarkToMarket_Value'].apply(lambda x: f"${x:,.2f}")
    leaderboard['Equity'] = leaderboard['True_Total_Equity'].apply(lambda x: f"${x:,.2f}")
    leaderboard['True_PnL_fmt'] = leaderboard['True_PnL'].apply(lambda x: f"${x:,.2f}")
    leaderboard['PnL_hr'] = leaderboard['Hourly_PnL'].apply(lambda x: f"${x:,.2f}/hr")

    print("\n🏆 STRATEGY LEADERBOARD (Filtered for Reality)")
    print("-" * 130)
    print(leaderboard[['StrategyName', 'Equity', 'True_PnL_fmt', 'PnL_hr', 'MTM_Value', 'Buys', 'Sells', 'Ill_Buys', 'Gh_Sells', 'WinRate%']].to_string(index=False))

    # ==========================================
    # DASHBOARD 2 & 3
    # ==========================================
    print("\n🔬 PARAMETER IMPACT (Which settings work best?)")
    print("-" * 80)

    all_params = df['Params'].apply(pd.Series).stack().reset_index(level=1)
    all_params.columns = ['Param', 'Value']
    all_params['CashFlow'] = df.loc[all_params.index, 'CashFlow']
    all_params['StrategyName'] = df.loc[all_params.index, 'StrategyName']

    strategy_pnl = df.groupby('StrategyName')['CashFlow'].sum().reset_index()
    strategy_pnl.columns = ['StrategyName', 'StrategyPnL']

    param_names = {'T': 'Threshold', 'W': 'Window', 'P': 'TakeProfit', 'S': 'StopLoss', 'ES': 'EntrySlippage', 'XS': 'ExitSlippage'}

    for param_letter in sorted(df['Params'].apply(lambda d: list(d.keys())).explode().dropna().unique()):
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

    print("⏰ HOURLY REGIME ANALYSIS (Top 5 Winners & Losers per Hour)")
    print("-" * 120)

    df['Hour'] = df['Timestamp'].dt.floor('h')
    hourly_strat = df.groupby(['Hour', 'StrategyName'])['CashFlow'].sum().reset_index()

    for hour in sorted(hourly_strat['Hour'].unique()):
        hour_data = hourly_strat[hourly_strat['Hour'] == hour].sort_values('CashFlow', ascending=False)
        hour_total = hour_data['CashFlow'].sum()
        top5 = hour_data.head(5)
        bot5 = hour_data.tail(5).sort_values('CashFlow', ascending=True)

        print(f"\n  [{pd.Timestamp(hour).strftime('%Y-%m-%d %H:%M')}] Net: ${hour_total:,.2f}")
        for _, r in top5.iterrows():
            print(f"    + {r['StrategyName']:<50} ${r['CashFlow']:>10,.2f}")
        for _, r in bot5.iterrows():
            print(f"    - {r['StrategyName']:<50} ${r['CashFlow']:>10,.2f}")

if __name__ == "__main__":
    analyze_specific_run()