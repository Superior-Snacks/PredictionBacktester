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
    // --- PAUSE & RESUME CONTROLS ---
    private static volatile bool _isPaused = false;
    private static CancellationTokenSource _pauseCts = new CancellationTokenSource();

    private static Dictionary<string, PaperBroker> _strategyBrokers = new Dictionary<string, PaperBroker>();
    private static Dictionary<string, string> _tokenNames = new Dictionary<string, string>();

    static async Task Main(string[] args)
    {
        Console.Clear();
        Console.WriteLine("=========================================");
        Console.WriteLine("  LIVE PAPER TRADING ENGINE INITIALIZED  ");
        Console.WriteLine("  Controls: Press 'P' to Pause, 'R' to Resume");
        Console.WriteLine("=========================================");

        _strategyBrokers["Sniper_Strict"] = new PaperBroker("Sniper_Strict", 1000m, _tokenNames);
        _strategyBrokers["Sniper_Loose"] = new PaperBroker("Sniper_Loose", 1000m, _tokenNames); // Let's make a trigger-happy one!

        _strategyBrokers["Reverse_TrendFollowerRecc"] = new PaperBroker("Reverse_TrendFollowerRecc", 1000m, _tokenNames);
        _strategyBrokers["Reverse_TrendFollowerOrg"] = new PaperBroker("Reverse_TrendFollowerOrg", 1000m, _tokenNames);

        _strategyBrokers["StatArb_ZScore"] = new PaperBroker("StatArb_ZScore", 1000m, _tokenNames);


        _strategyBrokers["Imbalance_5x"] = new PaperBroker("Imbalance_5x", 1000m, _tokenNames);
        _strategyBrokers["Imbalance_1.5x very loose"] = new PaperBroker("Imbalance_1.5x very loose", 1000m, _tokenNames);


        // THE INTERCEPTOR: Catch CTRL+C before the console dies!
        Console.CancelKeyPress += (sender, e) =>
        {
            e.Cancel = true; // Tell the OS: "Wait, don't kill the app yet!"
            Console.ForegroundColor = ConsoleColor.Yellow;
            Console.WriteLine("\n\n[SYSTEM] Graceful shutdown initiated...");

            // Run our export script
            ExportLedgerToCsv();

            Console.WriteLine("[SYSTEM] Engine safely terminated. Goodbye.");
            Console.ResetColor();

            Environment.Exit(0); // Now we have permission to die peacefully.
        };

        var activeStrategies = new Dictionary<string, List<ILiveStrategy>>();
        var orderBooks = new Dictionary<string, LocalOrderBook>();

        // ==========================================
        // KEYBOARD LISTENER (Pause / Resume)
        // ==========================================
        _ = Task.Run(() =>
        {
            while (true)
            {
                var keyInfo = Console.ReadKey(intercept: true);
                if (keyInfo.Key == ConsoleKey.P && !_isPaused)
                {
                    _isPaused = true;
                    _pauseCts.Cancel(); // Instantly sever the WebSocket connection!
                    Console.ForegroundColor = ConsoleColor.Yellow;
                    Console.WriteLine("\n[SYSTEM] PAUSED by user. Disconnecting and wiping memory... Press 'R' to resume.");
                    Console.ResetColor();
                }
                else if (keyInfo.Key == ConsoleKey.R && _isPaused)
                {
                    _isPaused = false;
                    _pauseCts = new CancellationTokenSource(); // Reset the token for the new connection
                    Console.ForegroundColor = ConsoleColor.Green;
                    Console.WriteLine("\n[SYSTEM] RESUMED by user. Reconnecting to Polymarket...");
                    Console.ResetColor();
                }
            }
        });

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
        for (int offset = 0; ; offset += limit)
        {
            var events = await apiClient.GetActiveEventsAsync(limit, offset);
            if (events == null || events.Count == 0) break;

            foreach (var ev in events)
            {
                if (ev.Markets == null) continue;
                foreach (var market in ev.Markets)
                {
                    // 1. Skip if it hasn't started yet
                    if (market.StartDate.HasValue && market.StartDate.Value > DateTime.UtcNow) continue;

                    // 2. THE LIQUIDITY FILTER: Skip dead "Ghost Town" markets!
                    if (market.Volume < 50000m) continue;

                    if (market.ClobTokenIds != null && !market.IsClosed && market.ClobTokenIds.Length > 0)
                    {
                        // Only subscribe to the YES token (index 0) to avoid duplicate signals
                        // and incorrect BuyNo price derivations on NO token order books.
                        string yesToken = market.ClobTokenIds[0];
                        allTokens.Add(yesToken);
                        _tokenNames[yesToken] = market.Question;
                    }
                }
            }
        }

        allTokens = allTokens.Distinct().ToList();
        Console.WriteLine($"Found {allTokens.Count} active outcome tokens to monitor!");

        // ==========================================
        // SETTLEMENT SWEEPER 
        // ==========================================
        _ = Task.Run(async () =>
        {
            while (true)
            {
                await Task.Delay(TimeSpan.FromMinutes(15));
                if (_isPaused) continue; // Don't sweep if we are paused!

                // --- THE PERFORMANCE DASHBOARD ---
                Console.WriteLine("\n================= STRATEGY PERFORMANCE OVERVIEW =================");
                foreach (var kvp in _strategyBrokers)
                {
                    var name = kvp.Key;
                    var broker = kvp.Value;
                    decimal totalEquity = broker.GetTotalPortfolioValue();
                    decimal pnl = totalEquity - 1000m;
                    decimal activeBets = totalEquity - broker.CashBalance;

                    Console.ForegroundColor = pnl >= 0 ? ConsoleColor.Green : ConsoleColor.Red;
                    Console.WriteLine($"[{name.PadRight(15)}] Equity: ${totalEquity:0.00} | PnL: ${(pnl):0.00} | In Bets: ${activeBets:0.00} | Trades: {broker.TotalTradesExecuted} (W:{broker.WinningTrades} L:{broker.LosingTrades})");
                    Console.ResetColor();
                }
                Console.WriteLine("=================================================================\n");

                Console.WriteLine("\n[SYSTEM] Running background settlement sweep...");
                try
                {
                    int sweepLimit = 100;
                    for (int offset = 0; offset < 500; offset += sweepLimit)
                    {
                        var events = await apiClient.GetClosedEventsAsync(sweepLimit, offset);
                        if (events == null || events.Count == 0) break;

                        foreach (var ev in events)
                        {
                            if (ev.Markets == null) continue;
                            foreach (var market in ev.Markets)
                            {
                                if (market.IsClosed && market.ClobTokenIds != null && market.OutcomePrices != null)
                                {
                                    for (int i = 0; i < market.ClobTokenIds.Length; i++)
                                    {
                                        string tokenId = market.ClobTokenIds[i];
                                        foreach (var broker in _strategyBrokers.Values)
                                        {
                                            if (broker.GetPositionShares(tokenId) > 0 || broker.GetNoPositionShares(tokenId) > 0)
                                            {
                                                decimal finalPayoutPrice = 0.00m;
                                                if (i < market.OutcomePrices.Length && decimal.TryParse(market.OutcomePrices[i], out decimal price))
                                                {
                                                    finalPayoutPrice = price;
                                                }

                                                broker.ResolveMarket(tokenId, finalPayoutPrice);
                                            }
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
                catch { /* Ignore sweep errors */ }
            }
        });

        // ==========================================
        // WEBSOCKET LISTENER 
        // ==========================================
        while (true)
        {
            // 1. If the user paused the bot, just spin here in standby mode.
            if (_isPaused)
            {
                await Task.Delay(1000);
                continue;
            }

            // 2. AMNESIA: Wipe the shelves clean so we don't trade on stale Wi-Fi drop data!
            orderBooks.Clear();
            activeStrategies.Clear();

            try
            {
                using var ws = new ClientWebSocket();
                ws.Options.KeepAliveInterval = TimeSpan.FromSeconds(10);

                Console.WriteLine("\n[CONNECTING] Connecting to Polymarket WebSocket...");
                // Pass the CancellationToken so 'P' instantly aborts the connection
                await ws.ConnectAsync(new Uri("wss://ws-subscriptions-clob.polymarket.com/ws/market"), _pauseCts.Token);
                Console.WriteLine("[CONNECTED] Listening for live order book updates... (Press CTRL+C to stop)\n");

                var listenTask = Task.Run(async () =>
                {
                    var receiveBuffer = new byte[8192];
                    using var ms = new MemoryStream();

                    while (ws.State == WebSocketState.Open)
                    {
                        WebSocketReceiveResult result;
                        do
                        {
                            // Pass the CancellationToken here too!
                            result = await ws.ReceiveAsync(new ArraySegment<byte>(receiveBuffer), _pauseCts.Token);
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

                                            activeStrategies[assetId] = new List<ILiveStrategy>
                                            {
                                                // Strict bot: requires 15 cent drop in 60s
                                                new LiveFlashCrashSniperStrategy("Sniper_Strict", 0.15m, 60),
                
                                                // Loose bot: only requires a 2 cent drop in 60s (Will trade constantly!)
                                                new LiveFlashCrashSniperStrategy("Sniper_Loose", 0.02m, 60),

                                                //reverse bot: reccomended
                                                new LiveFlashCrashReverseStrategy("Reverse_TrendFollowerRecc", 0.10m, 60),

                                                //reverse: exact
                                                new LiveFlashCrashReverseStrategy("Reverse_TrendFollowerOrg", 0.15m, 60),

                                                new MeanReversionStatArbStrategy("StatArb_ZScore", 100, -2.0m, 0.0m),

                                                // Change the Take Profit and Stop Loss from 2 cents (0.02m) to 8 cents (0.08m)!
                                                new OrderBookImbalanceStrategy("Imbalance_5x", 5.0m, 3, 0.08m, 0.08m),

                                                //loose to see action
                                                //new OrderBookImbalanceStrategy("Imbalance_1.5x very loose", 1.5m, 3, 0.05m, 0.05m)
                                            };
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

                                                // THE MULTIPLEXER ROUTING: Feed the exact same book to every strategy simultaneously!
                                                foreach (var strategy in activeStrategies[assetId])
                                                {
                                                    strategy.OnBookUpdate(book, _strategyBrokers[strategy.StrategyName]);
                                                }
                                            }
                                        }
                                    }
                                }
                            }
                        }
                        catch { /* Ignore dirty JSON */ }
                    }
                }, _pauseCts.Token);

                int chunkSize = 50;
                bool isFirstChunk = true;

                for (int i = 0; i < allTokens.Count; i += chunkSize)
                {
                    // If the user hit 'P' while we were still subscribing, abort!
                    if (_pauseCts.IsCancellationRequested) break;

                    var chunk = allTokens.Skip(i).Take(chunkSize);
                    string assetListString = string.Join("\",\"", chunk);

                    string subscribeMessage;
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
                    await ws.SendAsync(new ArraySegment<byte>(bytes), WebSocketMessageType.Text, true, _pauseCts.Token);

                    await Task.Delay(500);
                }

                await listenTask;
            }
            catch (OperationCanceledException)
            {
                // We expect this! It means the user pressed 'P'. 
                // Do not print an error, just loop back around and wait in standby.
            }
            catch (Exception ex)
            {
                Console.ForegroundColor = ConsoleColor.Yellow;
                Console.WriteLine($"\n[CONNECTION LOST] WebSocket connection dropped: {ex.Message}");
                Console.WriteLine("Reconnecting in 5 seconds...");
                Console.ResetColor();

                await Task.Delay(5000);
            }
        }
    }

    // ==========================================
    // GRACEFUL SHUTDOWN & LEDGER EXPORT
    // ==========================================
    private static void ExportLedgerToCsv()
    {
        try
        {
            string filename = $"LivePaperTrades_{DateTime.Now:yyyyMMdd_HHmmss}.csv";
            using var writer = new StreamWriter(filename);

            // Added StrategyName column
            writer.WriteLine("Timestamp,StrategyName,MarketName,AssetId,Side,ExecutionPrice,Shares,DollarValue");

            int totalTrades = 0;
            foreach (var brokerKvp in _strategyBrokers)
            {
                string strategyName = brokerKvp.Key;
                var broker = brokerKvp.Value;

                // Snapshot the ledger under the broker's lock to avoid concurrent modification
                List<ExecutedTrade> ledgerSnapshot;
                lock (broker.BrokerLock)
                {
                    ledgerSnapshot = new List<ExecutedTrade>(broker.TradeLedger);
                }

                totalTrades += ledgerSnapshot.Count;

                foreach (var trade in ledgerSnapshot)
                {
                    string marketName = _tokenNames.GetValueOrDefault(trade.OutcomeId, "Unknown");
                    marketName = $"\"{marketName.Replace("\"", "\"\"")}\"";

                    writer.WriteLine($"{trade.Date:O},{strategyName},{marketName},{trade.OutcomeId},{trade.Side},{trade.Price},{trade.Shares},{trade.DollarValue}");
                }
            }

            Console.ForegroundColor = ConsoleColor.Green;
            Console.WriteLine($"\n[EXPORT SUCCESS] {totalTrades} trades saved to {filename}");
            Console.ResetColor();
        }
        catch (Exception ex)
        {
            Console.ForegroundColor = ConsoleColor.Red;
            Console.WriteLine($"\n[EXPORT FAILED] Could not save ledger: {ex.Message}");
            Console.ResetColor();
        }
    }
}