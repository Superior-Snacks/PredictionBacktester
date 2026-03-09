import pandas as pd
import seaborn as sns
import matplotlib.pyplot as plt

def generate_heatmap(csv_filename):
    print(f"Loading data from {csv_filename}...")
    
    # 1. Load the CSV
    df = pd.read_csv(csv_filename)
    
    # 2. Convert timestamps to proper datetime objects
    df['Timestamp'] = pd.to_datetime(df['Timestamp'])

    # 3. Calculate Cash Flow
    # If we BUY, money leaves our account (Negative)
    # If we SELL, money enters our account (Positive)
    df['CashFlow'] = df.apply(lambda row: -row['DollarValue'] if row['Side'] == 'BUY' else row['DollarValue'], axis=1)

    # 4. Extract the Day of Week and the Hour of the Day
    df['DayOfWeek'] = df['Timestamp'].dt.day_name()
    df['Hour'] = df['Timestamp'].dt.hour

    # Ensure days are sorted chronologically
    days_order = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']
    df['DayOfWeek'] = pd.Categorical(df['DayOfWeek'], categories=days_order, ordered=True)

    # 5. Group the data to calculate the net profit/loss for each Day/Hour combo
    heatmap_data = df.groupby(['DayOfWeek', 'Hour'], observed=False)['CashFlow'].sum().unstack()

    # 6. Draw the Heatmap
    plt.figure(figsize=(14, 6))
    sns.heatmap(
        heatmap_data, 
        cmap='RdYlGn', # Red for losses, Yellow for neutral, Green for profits
        annot=True,    # Show the actual dollar amounts
        fmt=".2f",     # Format to 2 decimal places
        center=0,      
        linewidths=.5
    )

    plt.title("Bot Profitability Heatmap (Realized Net Cash Flow)", fontsize=16)
    plt.xlabel("Hour of Day (UTC)", fontsize=12)
    plt.ylabel("Day of Week", fontsize=12)
    plt.tight_layout()
    
    print("Generating chart...")
    plt.show()

if __name__ == "__main__":
    # Change this to the actual name of your CSV file
    csv_file = "LivePaperTrades_SNAPSHOT.csv" 
    generate_heatmap(csv_file)