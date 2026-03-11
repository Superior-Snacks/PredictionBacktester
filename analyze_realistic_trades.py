import pandas as pd

def analyze_realistic_trades(csv_path, min_shares_required=5.0):
    print(f"Loading {csv_path}...")
    
    # Load the CSV and ensure it's sorted chronologically
    df = pd.read_csv(csv_path)
    df['Timestamp'] = pd.to_datetime(df['Timestamp'])
    df = df.sort_values('Timestamp').reset_index(drop=True)
    
    # State tracking
    open_positions = {}  # asset_id -> {'shares': 0.0, 'cost': 0.0}
    
    # Metrics
    metrics = {
        'valid_buys': 0,
        'valid_sells': 0,
        'illegal_trades_skipped': 0,
        'ghost_sells_prevented': 0,
        'winning_trades': 0,
        'losing_trades': 0,
        'realized_pnl': 0.0,
        'total_volume_traded': 0.0
    }
    
    print(f"Filtering trades using a minimum order size of {min_shares_required} shares...\n")
    
    for idx, row in df.iterrows():
        asset = row['AssetId']
        side = str(row['Side']).upper().strip()
        shares = float(row['Shares'])
        dollars = float(row['DollarValue'])
        price = float(row['ExecutionPrice'])
        
        # 1. THE EXCHANGE BOUNCER: Is the order size legal?
        if shares < min_shares_required:
            metrics['illegal_trades_skipped'] += 1
            continue
            
        # Initialize memory for new assets
        if asset not in open_positions:
            open_positions[asset] = {'shares': 0.0, 'cost': 0.0}
            
        if side == 'BUY':
            open_positions[asset]['shares'] += shares
            open_positions[asset]['cost'] += dollars
            metrics['valid_buys'] += 1
            metrics['total_volume_traded'] += dollars
            
        elif side == 'SELL':
            current_shares = open_positions[asset]['shares']
            
            # 2. THE GHOST SELL PREVENTION
            # If the original buy was illegal and skipped, we don't own these shares!
            if current_shares <= 0.0001:
                metrics['ghost_sells_prevented'] += 1
                metrics['illegal_trades_skipped'] += 1
                continue
                
            # If the paper bot tries to sell more than we legally acquired, clamp it
            actual_sell_shares = min(shares, current_shares)
            
            # Calculate the cost basis for the exact amount we are allowed to sell
            sell_ratio = actual_sell_shares / current_shares
            cost_basis = open_positions[asset]['cost'] * sell_ratio
            actual_revenue = actual_sell_shares * price
            
            # Calculate PnL
            trade_pnl = actual_revenue - cost_basis
            metrics['realized_pnl'] += trade_pnl
            metrics['total_volume_traded'] += actual_revenue
            
            if trade_pnl > 0:
                metrics['winning_trades'] += 1
            else:
                metrics['losing_trades'] += 1
                
            # Remove sold shares from inventory
            open_positions[asset]['shares'] -= actual_sell_shares
            open_positions[asset]['cost'] -= cost_basis
            metrics['valid_sells'] += 1

    # Print out the corrected realistic report
    print("="*50)
    print(" REALISTIC TRADING REPORT")
    print("="*50)
    print(f"Total Valid Buys:        {metrics['valid_buys']}")
    print(f"Total Valid Sells:       {metrics['valid_sells']}")
    
    total_valid_exits = metrics['winning_trades'] + metrics['losing_trades']
    win_rate = (metrics['winning_trades'] / total_valid_exits * 100) if total_valid_exits > 0 else 0
    
    print(f"\nWinning Sells:           {metrics['winning_trades']}")
    print(f"Losing Sells:            {metrics['losing_trades']}")
    print(f"Win Rate:                {win_rate:.2f}%")
    
    print(f"\nTotal Volume Traded:     ${metrics['total_volume_traded']:.2f}")
    
    # Color the PnL text based on profit/loss
    pnl_str = f"${metrics['realized_pnl']:.2f}"
    if metrics['realized_pnl'] >= 0:
        print(f"Realized PnL:            +{pnl_str}  ✅")
    else:
        print(f"Realized PnL:            {pnl_str}  ❌")
        
    print("\n" + "="*50)
    print(" ILLEGAL TRADE FILTER STATS")
    print("="*50)
    print(f"Total Illegal Trades Skipped: {metrics['illegal_trades_skipped']}")
    print(f"  -> Ghost Sells Prevented:   {metrics['ghost_sells_prevented']}")
    print("="*50)

if __name__ == "__main__":
    # You can change '1.0' to whatever you suspect the general minimum is
    analyze_realistic_trades('OLDPaperTrades_SNAPSHOT.csv', min_shares_required=1.0)