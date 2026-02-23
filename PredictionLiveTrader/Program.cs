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
using PredictionBacktester.Engine;

namespace PredictionLiveTrader;

class Program
{
    static async Task Main(string[] args)
    {
        Console.Clear();
        Console.WriteLine("=========================================");
        Console.WriteLine("  LIVE PAPER TRADING ENGINE INITIALIZED  ");
        Console.WriteLine("=========================================");

        var paperBrokers = new Dictionary<string, PaperBroker>();
        var sniperBots = new Dictionary<string, LiveFlashCrashSniperStrategy>();
        var orderBooks = new Dictionary<string, LocalOrderBook>();

        Console.WriteLine("Setting up API Client...");
        var services = new ServiceCollection();
        services.AddHttpClient("PolymarketGamma", client => { client.BaseAddress = new Uri("https://gamma-api.polymarket.com/"); });
        services.AddHttpClient("PolymarketClob", client => { client.BaseAddress = new Uri("https://clob.polymarket.com/"); });
        services.AddHttpClient("PolymarketData", client => { client.BaseAddress = new Uri("https://data-api.polymarket.com/"); });

        services.AddTransient<PolymarketClient>();

        var serviceProvider = services.BuildServiceProvider();
        var apiClient = serviceProvider.GetRequiredService<PolymarketClient>();

        Console.WriteLine("Fetching active markets from Polymarket API...");
        var allTokens = new List<string>();

        int limit = 100;
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

        allTokens = allTokens.Distinct().ToList();
        Console.WriteLine($"Found {allTokens.Count} active outcome tokens to monitor!");

        using var ws = new ClientWebSocket();

        // FIX 1: Set the Heartbeat ping BEFORE opening the connection
        ws.Options.KeepAliveInterval = TimeSpan.FromSeconds(10);

        try
        {
            // We only connect ONCE!
            await ws.ConnectAsync(new Uri("wss://ws-subscriptions-clob.polymarket.com/ws/market"), CancellationToken.None);
            Console.WriteLine("\n[CONNECTED] Listening for live order book updates... (Press CTRL+C to stop)\n");

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
                    ms.SetLength(0);

                    try
                    {
                        using var doc = JsonDocument.Parse(message);
                        var root = doc.RootElement;

                        if (root.ValueKind == JsonValueKind.Object && root.TryGetProperty("event_type", out var eventTypeEl))
                        {
                            string eventType = eventTypeEl.GetString();

                            if (eventType == "book" && root.TryGetProperty("asset_id", out var assetIdEl))
                            {
                                string assetId = assetIdEl.GetString();
                                if (!string.IsNullOrEmpty(assetId))
                                {
                                    if (!orderBooks.ContainsKey(assetId))
                                    {
                                        orderBooks[assetId] = new LocalOrderBook(assetId);
                                        paperBrokers[assetId] = new PaperBroker(1000m, assetId);
                                        sniperBots[assetId] = new LiveFlashCrashSniperStrategy(0.15m, 60, 0.05m, 0.15m, 0.05m);
                                    }

                                    if (root.TryGetProperty("bids", out var bidsEl) && root.TryGetProperty("asks", out var asksEl))
                                    {
                                        orderBooks[assetId].ProcessBookUpdate(bidsEl, asksEl);
                                    }
                                }
                            }
                            else if (eventType == "price_change" && root.TryGetProperty("price_changes", out var changesEl))
                            {
                                foreach (var change in changesEl.EnumerateArray())
                                {
                                    if (change.TryGetProperty("asset_id", out var idEl))
                                    {
                                        string assetId = idEl.GetString();

                                        if (!string.IsNullOrEmpty(assetId) && orderBooks.ContainsKey(assetId))
                                        {
                                            decimal price = decimal.Parse(change.GetProperty("price").GetString() ?? "0");
                                            decimal size = decimal.Parse(change.GetProperty("size").GetString() ?? "0");
                                            string side = change.GetProperty("side").GetString() ?? "";

                                            var book = orderBooks[assetId];

                                            if (side == "BUY")
                                            {
                                                if (size == 0) book.Bids.Remove(price);
                                                else book.Bids[price] = size;
                                            }
                                            else if (side == "SELL")
                                            {
                                                if (size == 0) book.Asks.Remove(price);
                                                else book.Asks[price] = size;
                                            }

                                            Console.ForegroundColor = ConsoleColor.DarkGray;
                                            Console.Write(".");
                                            Console.ResetColor();

                                            sniperBots[assetId].OnBookUpdate(book, paperBrokers[assetId]);
                                        }
                                    }
                                }
                            }
                        }
                    }
                    catch { /* Ignore dirty JSON */ }
                }
            });

            int chunkSize = 50;
            bool isFirstChunk = true;

            for (int i = 0; i < allTokens.Count; i += chunkSize)
            {
                var chunk = allTokens.Skip(i).Take(chunkSize);
                string assetListString = string.Join("\",\"", chunk);

                string subscribeMessage;

                // FIX 2: Only the very first chunk can use "type: market". 
                // The rest must use "operation: subscribe" to append to the channel.
                if (isFirstChunk)
                {
                    subscribeMessage = $"{{\"assets_ids\":[\"{assetListString}\"],\"type\":\"market\"}}";
                    isFirstChunk = false;
                }
                else
                {
                    subscribeMessage = $"{{\"assets_ids\":[\"{assetListString}\"],\"operation\":\"subscribe\"}}";
                }

                var bytes = Encoding.UTF8.GetBytes(subscribeMessage);
                await ws.SendAsync(new ArraySegment<byte>(bytes), WebSocketMessageType.Text, true, CancellationToken.None);

                await Task.Delay(500);
            }

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