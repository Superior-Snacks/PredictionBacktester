using System;
using System.Linq;
using System.Net.WebSockets;
using System.Text;
using System.Text.Json;
using System.Threading;
using System.Threading.Tasks;
using PredictionBacktester.Strategies;
using PredictionBacktester.Core.Entities.Database;
using PredictionBacktester.Data.Database; // NEW: Added database access

namespace PredictionLiveTrader;

class Program
{
    static async Task Main(string[] args)
    {
        Console.Clear();
        Console.WriteLine("=========================================");
        Console.WriteLine("  LIVE PAPER TRADING ENGINE INITIALIZED  ");
        Console.WriteLine("=========================================");

        var paperBroker = new PaperBroker(1000m);
        var sniperBot = new FlashCrashSniperStrategy(0.15m, 300, 0.05m, 0.15m, 3, 0.05m);
        Console.WriteLine($"Strategy Loaded: {sniperBot.GetType().Name}\n");

        // --- NEW: FETCH THE MOST VOLATILE MARKETS FROM DB ---
        Console.WriteLine("Hunting database for top 10 most active markets...");
        string assetListString = "";

        using (var dbContext = new PolymarketDbContext())
        {
            // Find the top 10 Outcome IDs (Tokens) with the absolute highest number of historical trades
            var topTokens = dbContext.Trades
                .GroupBy(t => t.OutcomeId)
                .OrderByDescending(g => g.Count())
                .Select(g => g.Key)
                .Take(10)
                .ToList();

            // Format them for the JSON array: "0x123","0x456","0x789"
            assetListString = string.Join("\",\"", topTokens);
            Console.WriteLine($"Found {topTokens.Count} massive markets to monitor!");
        }
        // ----------------------------------------------------

        using var ws = new ClientWebSocket();
        try
        {
            await ws.ConnectAsync(new Uri("wss://ws-subscriptions-clob.polymarket.com/ws/market"), CancellationToken.None);
            Console.WriteLine("\n[CONNECTED] Listening for live flash crashes... (Press CTRL+C to stop)\n");

            // --- NEW: INJECT THE REAL TOKENS INTO THE SUBSCRIPTION ---
            string subscribeMessage = $"{{\"assets\":[\"{assetListString}\"],\"type\":\"market\"}}";
            var bytes = Encoding.UTF8.GetBytes(subscribeMessage);
            await ws.SendAsync(new ArraySegment<byte>(bytes), WebSocketMessageType.Text, true, CancellationToken.None);

            var receiveBuffer = new byte[8192]; // Increased buffer size for heavy traffic

            while (ws.State == WebSocketState.Open)
            {
                var result = await ws.ReceiveAsync(new ArraySegment<byte>(receiveBuffer), CancellationToken.None);

                if (result.MessageType == WebSocketMessageType.Close)
                {
                    await ws.CloseAsync(WebSocketCloseStatus.NormalClosure, string.Empty, CancellationToken.None);
                    break;
                }

                string message = Encoding.UTF8.GetString(receiveBuffer, 0, result.Count);

                try
                {
                    // Polymarket WebSocket sends arrays of events. Let's do a quick and dirty parse.
                    if (message.Contains("\"price\"") && message.Contains("\"size\""))
                    {
                        using var doc = JsonDocument.Parse(message);
                        var root = doc.RootElement;

                        // Check if it's an array of events
                        if (root.ValueKind == JsonValueKind.Array)
                        {
                            foreach (var element in root.EnumerateArray())
                            {
                                if (element.TryGetProperty("price", out var priceEl) && element.TryGetProperty("size", out var sizeEl))
                                {
                                    decimal livePrice = decimal.Parse(priceEl.GetString() ?? "0");
                                    decimal liveSize = decimal.Parse(sizeEl.GetString() ?? "0");

                                    var liveTick = new Trade
                                    {
                                        Price = livePrice,
                                        Size = liveSize,
                                        Timestamp = DateTimeOffset.UtcNow.ToUnixTimeSeconds()
                                    };

                                    // Print a tiny gray dot so you know data is actively flowing!
                                    Console.ForegroundColor = ConsoleColor.DarkGray;
                                    Console.Write(".");
                                    Console.ResetColor();

                                    sniperBot.OnTick(liveTick, paperBroker);
                                }
                            }
                        }
                    }
                }
                catch { /* Ignore dirty JSON ticks */ }
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