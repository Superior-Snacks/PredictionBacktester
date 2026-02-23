using System;
using System.Threading.Tasks;
using PredictionBacktester.Strategies;
using PredictionBacktester.Core.Entities.Database;
using PredictionLiveTrader;

class Program
{
    static async Task Main(string[] args)
    {
        Console.Clear();
        Console.WriteLine("=========================================");
        Console.WriteLine("  LIVE PAPER TRADING ENGINE INITIALIZED  ");
        Console.WriteLine("=========================================");

        // 1. Initialize the Paper Broker with $1,000 fake dollars
        var paperBroker = new PaperBroker(1000m);

        // 2. Load the best HFT Strategy from your Leaderboard!
        // Params: [0.15 crash, 300s window, 0.05 profit, 0.15 stop, 3s delay, 0.05 risk]
        var sniperBot = new FlashCrashSniperStrategy(0.15m, 300, 0.05m, 0.15m, 3, 0.05m);

        Console.WriteLine($"Strategy Loaded: {sniperBot.GetType().Name}");
        Console.WriteLine("Listening for live Polymarket ticks... (Press CTRL+C to stop)\n");

        // 3. The Live Loop 
        // In the next step, we will wire this to the actual "wss://ws-subscriptions-clob.polymarket.com" WebSocket.
        // For now, let's build the loop that feeds data to the bot.

        while (true)
        {
            // TODO: In our next prompt, we will drop the ClientWebSocket code here!
            // For now, this is where the bot waits for the live market data.

            await Task.Delay(1000);
        }
    }
}