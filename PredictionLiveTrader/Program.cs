using System;
using System.Collections.Generic;
using System.Linq;
using System.Net.WebSockets;
using System.Text;
using System.Text.Json;
using System.Threading;
using System.Threading.Tasks;
using Microsoft.Extensions.DependencyInjection;
using PredictionBacktester.Strategies;
using PredictionBacktester.Core.Entities.Database;
using PredictionBacktester.Data.ApiClients;

namespace PredictionLiveTrader;

class Program
{
    static async Task Main(string[] args)
    {
        Console.Clear();
        Console.WriteLine("=========================================");
        Console.WriteLine("  LIVE PAPER TRADING ENGINE INITIALIZED  ");
        Console.WriteLine("=========================================");

        // Dictionaries to hold a unique bot/broker for each market
        var paperBrokers = new Dictionary<string, PaperBroker>();
        var sniperBots = new Dictionary<string, LiveFlashCrashSniperStrategy>();

        // --- 1. SETUP API CLIENT TO BYPASS DATABASE ---
        Console.WriteLine("Setting up API Client...");
        var services = new ServiceCollection();
        services.AddHttpClient("PolymarketGamma", client => { client.BaseAddress = new Uri("https://gamma-api.polymarket.com/"); });
        services.AddHttpClient("PolymarketClob", client => { client.BaseAddress = new Uri("https://clob.polymarket.com/"); });
        services.AddHttpClient("PolymarketData", client => { client.BaseAddress = new Uri("https://data-api.polymarket.com/"); });

        services.AddTransient<PolymarketClient>();

        var serviceProvider = services.BuildServiceProvider();
        var apiClient = serviceProvider.GetRequiredService<PolymarketClient>();

        // --- 2. FETCH ALL ACTIVE MARKETS FROM API ---
        Console.WriteLine("Fetching active markets from Polymarket API...");
        var allTokens = new List<string>();

        int limit = 100;
        // Fetch the 500 most recent events (this usually yields thousands of outcomes)
        // Increase the 500 limit if you want literally every obscure market on the platform
        for (int offset = 0; offset < 500; offset += limit)
        {
            var events = await apiClient.GetActiveEventsAsync(limit, offset);
            if (events == null || events.Count == 0) break;

            foreach (var ev in events)
            {
                if (ev.Markets == null) continue;
                foreach (var market in ev.Markets)
                {
                    if (market.ClobTokenIds != null && !market.IsClosed)
                    {
                        allTokens.AddRange(market.ClobTokenIds);
                    }
                }
            }
        }

        allTokens = allTokens.Distinct().ToList(); // Remove duplicates
        Console.WriteLine($"Found {allTokens.Count} active outcome tokens to monitor!");

        using var ws = new ClientWebSocket();
        try
        {
            await ws.ConnectAsync(new Uri("wss://ws-subscriptions-clob.polymarket.com/ws/market"), CancellationToken.None);
            Console.WriteLine("\n[CONNECTED] Listening for live flash crashes... (Press CTRL+C to stop)\n");

            // --- 3. CHUNK THE SUBSCRIPTIONS ---
            // If we send 5000 tokens in one JSON string, the WS will reject it. We send them in batches.
            int chunkSize = 300;
            for (int i = 0; i < allTokens.Count; i += chunkSize)
            {
                var chunk = allTokens.Skip(i).Take(chunkSize);
                string assetListString = string.Join("\",\"", chunk);
                string subscribeMessage = $"{{\"assets\":[\"{assetListString}\"],\"type\":\"market\"}}";

                var bytes = Encoding.UTF8.GetBytes(subscribeMessage);
                await ws.SendAsync(new ArraySegment<byte>(bytes), WebSocketMessageType.Text, true, CancellationToken.None);
                await Task.Delay(100); // Give the WebSocket a tiny breather to process
            }

            var receiveBuffer = new byte[16384]; // Increased buffer for high volume traffic

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
                    if (message.Contains("\"price\"") && message.Contains("\"size\""))
                    {
                        using var doc = JsonDocument.Parse(message);
                        var root = doc.RootElement;

                        if (root.ValueKind == JsonValueKind.Array)
                        {
                            foreach (var element in root.EnumerateArray())
                            {
                                if (element.TryGetProperty("price", out var priceEl) &&
                                    element.TryGetProperty("size", out var sizeEl) &&
                                    element.TryGetProperty("asset_id", out var assetIdEl))
                                {
                                    string assetId = assetIdEl.GetString();

                                    if (!string.IsNullOrEmpty(assetId))
                                    {
                                        // --- 4. DYNAMIC LAZY INITIALIZATION ---
                                        // Only create the strategy for this market if someone actually trades it.
                                        // This saves massive amounts of RAM and CPU.
                                        if (!sniperBots.ContainsKey(assetId))
                                        {
                                            paperBrokers[assetId] = new PaperBroker(1000m, assetId);
                                            sniperBots[assetId] = new LiveFlashCrashSniperStrategy(0.15m, 300, 0.05m, 0.15m, 0.05m);
                                        }

                                        decimal livePrice = decimal.Parse(priceEl.GetString() ?? "0");
                                        decimal liveSize = decimal.Parse(sizeEl.GetString() ?? "0");

                                        var liveTick = new Trade
                                        {
                                            OutcomeId = assetId,
                                            Price = livePrice,
                                            Size = liveSize,
                                            Timestamp = DateTimeOffset.UtcNow.ToUnixTimeSeconds()
                                        };

                                        Console.ForegroundColor = ConsoleColor.DarkGray;
                                        Console.Write(".");
                                        Console.ResetColor();

                                        // Route the tick to the specific market's isolated bot
                                        sniperBots[assetId].OnTick(liveTick, paperBrokers[assetId]);
                                    }
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
            Console.WriteLine($"\n[FATAL ERROR] WebSocket connection failed: {ex.Message}");
            Console.ResetColor();
        }
    }
}