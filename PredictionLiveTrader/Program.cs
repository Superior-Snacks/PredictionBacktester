using System;
using System.Net.WebSockets;
using System.Text;
using System.Text.Json;
using System.Threading;
using System.Threading.Tasks;
using PredictionBacktester.Strategies;
using PredictionBacktester.Core.Entities.Database;

namespace PredictionLiveTrader;

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
        var sniperBot = new FlashCrashSniperStrategy(0.15m, 300, 0.05m, 0.15m, 3, 0.05m);

        Console.WriteLine($"Strategy Loaded: {sniperBot.GetType().Name}");
        Console.WriteLine("Connecting to Polymarket Live Order Book...");

        // 3. Connect to the Live WebSocket
        using var ws = new ClientWebSocket();
        try
        {
            // Polymarket's live CLOB (Central Limit Order Book) WebSocket endpoint
            await ws.ConnectAsync(new Uri("wss://ws-subscriptions-clob.polymarket.com/ws/market"), CancellationToken.None);
            Console.WriteLine("[CONNECTED] Listening for live ticks... (Press CTRL+C to stop)\n");

            // 4. Send the Subscription Payload
            // NOTE: "0" is a placeholder. To trade a specific market, you'd replace this with the actual Asset/Condition ID.
            string subscribeMessage = "{\"assets\":[\"0\"],\"type\":\"market\"}";
            var bytes = Encoding.UTF8.GetBytes(subscribeMessage);
            await ws.SendAsync(new ArraySegment<byte>(bytes), WebSocketMessageType.Text, true, CancellationToken.None);

            // 5. The Infinite Listening Loop
            var receiveBuffer = new byte[4096];
            while (ws.State == WebSocketState.Open)
            {
                var result = await ws.ReceiveAsync(new ArraySegment<byte>(receiveBuffer), CancellationToken.None);

                if (result.MessageType == WebSocketMessageType.Close)
                {
                    await ws.CloseAsync(WebSocketCloseStatus.NormalClosure, string.Empty, CancellationToken.None);
                    Console.WriteLine("[DISCONNECTED] Server closed the connection.");
                    break;
                }

                // Decode the live JSON message from the exchange
                string message = Encoding.UTF8.GetString(receiveBuffer, 0, result.Count);

                // Try to parse the message into a Trade tick and feed it to the Bot!
                try
                {
                    // Basic check to see if it's a trade execution message
                    if (message.Contains("\"price\"") && message.Contains("\"size\""))
                    {
                        using var doc = JsonDocument.Parse(message);
                        var root = doc.RootElement;

                        // NOTE: Polymarket's actual live JSON structure might require tweaking these exact property names!
                        // This is a generic mapper to get you started.
                        if (root.TryGetProperty("price", out var priceEl) && root.TryGetProperty("size", out var sizeEl))
                        {
                            decimal livePrice = decimal.Parse(priceEl.GetString() ?? "0");
                            decimal liveSize = decimal.Parse(sizeEl.GetString() ?? "0");

                            // Create the Tick
                            var liveTick = new Trade
                            {
                                Price = livePrice,
                                Size = liveSize,
                                Timestamp = DateTimeOffset.UtcNow.ToUnixTimeSeconds()
                            };

                            // FEED THE BRAIN
                            sniperBot.OnTick(liveTick, paperBroker);
                        }
                    }
                }
                catch (Exception ex)
                {
                    // Ignore parse errors on random heartbeat messages or weird JSON structures
                    // Console.WriteLine($"Parse error: {ex.Message}");
                }
            }
        }
        catch (Exception ex)
        {
            Console.ForegroundColor = ConsoleColor.Red;
            Console.WriteLine($"[FATAL ERROR] WebSocket connection failed: {ex.Message}");
            Console.ResetColor();
        }
    }
}