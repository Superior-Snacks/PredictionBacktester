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
using System.IO;

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

            // --- 3. START LISTENING IMMEDIATELY (On a separate thread) ---
            var listenTask = Task.Run(async () =>
            {
                var receiveBuffer = new byte[8192];
                using var ms = new MemoryStream();

                while (ws.State == WebSocketState.Open)
                {
                    WebSocketReceiveResult result;
                    do
                    {
                        result = await ws.ReceiveAsync(new ArraySegment<byte>(receiveBuffer), CancellationToken.None);
                        if (result.MessageType == WebSocketMessageType.Close)
                        {
                            await ws.CloseAsync(WebSocketCloseStatus.NormalClosure, string.Empty, CancellationToken.None);
                            return;
                        }
                        ms.Write(receiveBuffer, 0, result.Count);
                    }
                    while (!result.EndOfMessage);

                    string message = Encoding.UTF8.GetString(ms.ToArray());
                    ms.SetLength(0); // Clear the memory stream for the next message!

                    try
                    {
                        using var doc = JsonDocument.Parse(message);
                        var root = doc.RootElement;

                        // ONLY look for Objects (ignores the massive initial arrays of book snapshots)
                        if (root.ValueKind == JsonValueKind.Object && root.TryGetProperty("event_type", out var eventTypeEl))
                        {
                            if (eventTypeEl.GetString() == "price_change" && root.TryGetProperty("price_changes", out var changesEl))
                            {
                                foreach (var change in changesEl.EnumerateArray())
                                {
                                    if (change.TryGetProperty("asset_id", out var assetIdEl) &&
                                        change.TryGetProperty("price", out var priceEl) &&
                                        change.TryGetProperty("size", out var sizeEl))
                                    {
                                        string assetId = assetIdEl.GetString();

                                        if (!string.IsNullOrEmpty(assetId))
                                        {
                                            // LAZY INITIALIZATION
                                            if (!sniperBots.ContainsKey(assetId))
                                            {
                                                paperBrokers[assetId] = new PaperBroker(1000m, assetId);
                                                sniperBots[assetId] = new LiveFlashCrashSniperStrategy(0.15m, 60, 0.05m, 0.15m, 0.05m);
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

                                            sniperBots[assetId].OnTick(liveTick, paperBrokers[assetId]);
                                        }
                                    }
                                }
                            }
                        }
                    }
                    catch { /* Ignore dirty JSON */ }
                }
            });

            // --- 4. SEND SUBSCRIPTIONS SLOWLY ---
            int chunkSize = 50;
            for (int i = 0; i < allTokens.Count; i += chunkSize)
            {
                var chunk = allTokens.Skip(i).Take(chunkSize);
                string assetListString = string.Join("\",\"", chunk);

                // THE REAL FIX: It absolutely MUST be "assets_ids"!
                string subscribeMessage = $"{{\"assets_ids\":[\"{assetListString}\"],\"type\":\"market\"}}";

                var bytes = Encoding.UTF8.GetBytes(subscribeMessage);
                await ws.SendAsync(new ArraySegment<byte>(bytes), WebSocketMessageType.Text, true, CancellationToken.None);

                // Wait half a second between chunks so we don't overwhelm the server
                await Task.Delay(500);
            }

            // --- 5. HEARTBEAT (Required by Polymarket) ---
            // Keep the connection alive by pinging every 10 seconds
            while (ws.State == WebSocketState.Open)
            {
                await Task.Delay(10000); // Wait 10 seconds
                var pingMessage = Encoding.UTF8.GetBytes("\"PING\"");
                await ws.SendAsync(new ArraySegment<byte>(pingMessage), WebSocketMessageType.Text, true, CancellationToken.None);
            }

            // Keep the main thread alive while the listener runs in the background
            await listenTask;
        }
        catch (Exception ex)
        {
            Console.ForegroundColor = ConsoleColor.Red;
            Console.WriteLine($"\n[FATAL ERROR] WebSocket connection failed: {ex.Message}");
            Console.ResetColor();
        }
    }
}