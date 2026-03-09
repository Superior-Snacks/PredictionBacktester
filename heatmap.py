import pandas as pd
import seaborn as sns
import matplotlib.pyplot as plt

def analyze_strategies(csv_filename):
    print(f"Loading data from {csv_filename}...")
    
    try:
        df = pd.read_csv(csv_filename)
    except FileNotFoundError:
        print(f"Error: Could not find '{csv_filename}'. Make sure it's in the same folder.")
        return

    # 1. Convert timestamps
    df['Timestamp'] = pd.to_datetime(df['Timestamp'])

    # 2. Calculate Cash Flow (Sells are positive cash in, Buys are negative cash out)
    df['CashFlow'] = df.apply(lambda row: -row['DollarValue'] if row['Side'] == 'BUY' else row['DollarValue'], axis=1)

    # 3. Calculate overview metrics to rank the strategies
    print("\nCalculating strategy overview...")
    strategy_pnl = df.groupby('StrategyName')['CashFlow'].sum().sort_values(ascending=False)
    strategies = strategy_pnl.index.tolist()

    # 4. Print the interactive menu (Showing Top 50 so terminal doesn't get flooded)
    print("\n" + "="*70)
    print("🏆 STRATEGY HEATMAP SELECTION (Sorted by Net Cash Flow)")
    print("="*70)
    
    display_limit = 500
    for i, strat in enumerate(strategies[:display_limit]):
        print(f"[{i+1}] {strat}  -->  ${strategy_pnl[strat]:.2f}")
    
    if len(strategies) > display_limit:
        print(f"\n... and {len(strategies) - display_limit} other strategies hidden.")

    # 5. Prompt user for selection
    while True:
        try:
            choice_str = input(f"\nEnter the number of the strategy to plot (1-{min(len(strategies), display_limit)}): ")
            choice = int(choice_str)
            if 1 <= choice <= len(strategies):
                selected_strategy = strategies[choice - 1]
                break
            else:
                print("Invalid number. Please pick a number from the list above.")
        except ValueError:
            print("Please enter a valid integer.")

    # 6. Filter data for ONLY the chosen strategy
    print(f"\nFiltering data for: {selected_strategy}")
    strat_df = df[df['StrategyName'] == selected_strategy].copy()

    # 7. Extract Day and Hour for the heatmap
    strat_df['DayOfWeek'] = strat_df['Timestamp'].dt.day_name()
    strat_df['Hour'] = strat_df['Timestamp'].dt.hour

    days_order = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']
    strat_df['DayOfWeek'] = pd.Categorical(strat_df['DayOfWeek'], categories=days_order, ordered=True)

    heatmap_data = strat_df.groupby(['DayOfWeek', 'Hour'], observed=False)['CashFlow'].sum().unstack()

    # 8. Draw the isolated Heatmap
    plt.figure(figsize=(14, 6))
    sns.heatmap(
        heatmap_data, 
        cmap='RdYlGn', 
        annot=True,    
        fmt=".2f",     
        center=0,      
        linewidths=.5
    )

    plt.title(f"Profitability Heatmap: {selected_strategy}", fontsize=14)
    plt.xlabel("Hour of Day (UTC)", fontsize=12)
    plt.ylabel("Day of Week", fontsize=12)
    plt.tight_layout()
    
    print("Generating chart...")
    plt.show()

if __name__ == "__main__":
    # Ensure this matches the exact name of your CSV file
    csv_file = "LivePaperTrades_SNAPSHOT.csv" 
    analyze_strategies(csv_file)