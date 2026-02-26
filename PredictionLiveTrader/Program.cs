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

record StrategyConfig(string Name, decimal StartingCapital, Func<ILiveStrategy> Factory);

class Program
{
    // ==========================================
    // STRATEGY CONFIGURATION (Grid Search / Cartesian Product)
    // ==========================================
    private static readonly List<StrategyConfig> _strategyConfigs = GenerateStrategyGrid();

    private static List<StrategyConfig> GenerateStrategyGrid()
    {
        var configs = new List<StrategyConfig>();

        // ---------------------------------------------------------
        // normal hard test
        // ---------------------------------------------------------
        configs.Add(new StrategyConfig(
            "Sniper_Ultra_Strict", 
            1000m, 
            () => new LiveFlashCrashSniperStrategy("Sniper_Ultra_Strict", 0.25m, 60)
        ));


        // ---------------------------------------------------------
        // GRID 1: Live Flash Crash Sniper
        // ---------------------------------------------------------
        decimal[] sniperThresholds = { 0.02m, 0.05m, 0.15m, 0.30m}; // 4 options
        long[] sniperWindows = {20, 30, 60 };                    // 3 options

        int sniperVersion = 1;
        
        // This LINQ query creates the Cartesian Product automatically!
        var sniperGrid = from threshold in sniperThresholds
                         from window in sniperWindows
                         select new { threshold, window };

        foreach (var param in sniperGrid)
        {
            // NEW: Inject Threshold (T) and Window (W) into the name
            string name = $"Sniper_v{sniperVersion++}_T{param.threshold}_W{param.window}";
            configs.Add(new StrategyConfig(
                name, 
                1000m, 
                () => new LiveFlashCrashSniperStrategy(name, param.threshold, param.window)
            ));
        }
        /*
        // ---------------------------------------------------------
        // GRID 2: Mean Reversion Stat Arb
        // ---------------------------------------------------------
        int[] rollingWindows = { 50, 100 };
        decimal[] entryZScores = { -2.0m, -2.5m, -3.0m };
        // Total Combinations: 2 x 3 = 6 bots

        int statArbVersion = 1;

        var statArbGrid = from window in rollingWindows
                          from entryZ in entryZScores
                          select new { window, entryZ };

        foreach (var param in statArbGrid)
        {
            string name = $"StatArb_v{statArbVersion++}";
            configs.Add(new StrategyConfig(
                name, 
                1000m, 
                () => new MeanReversionStatArbStrategy(name, param.window, param.entryZ)
            ));
        }

        // ---------------------------------------------------------
        // GRID 3: Order Book Imbalance
        // ---------------------------------------------------------
        decimal[] imbalanceRatios = { 3.0m, 5.0m };
        int[] depths = { 3, 5 };
        
        int imbVersion = 1;

        var imbGrid = from ratio in imbalanceRatios
                      from depth in depths
                      select new { ratio, depth };

        foreach (var param in imbGrid)
        {
            string name = $"Imbalance_v{imbVersion++}";
            configs.Add(new StrategyConfig(
                name, 
                1000m, 
                // Using 0.08m as the take profit / stop loss margins for this example
                () => new OrderBookImbalanceStrategy(name, param.ratio, param.depth, 0.08m, 0.08m)
            ));
        }*/

        // ---------------------------------------------------------
        // GRID 4: Reverse Flash Crash (Trend Follower)
        // ---------------------------------------------------------
        // Parameters to test:
        decimal[] reverseThresholds = { 0.10m, 0.15m };        // How big of a crash triggers us?
        long[] reverseWindows = { 30, 60 };                    // How fast does it need to drop?
        decimal[] takeProfitMargins = { 0.10m, 0.20m };        // How far do we ride the trend down?
        
        // Total Combinations: 2 x 2 x 2 = 8 unique bots

        int reverseVersion = 1;

        // A 3D Cartesian Product!
        var reverseGrid = from threshold in reverseThresholds
                          from window in reverseWindows
                          from tpMargin in takeProfitMargins
                          select new { threshold, window, tpMargin };

        foreach (var param in reverseGrid)
        {
            // NEW: Inject Threshold (T) and TakeProfit (P) into the name
            string name = $"RevArb_v{reverseVersion++}_T{param.threshold}_W{param.window}_P{param.tpMargin}";
            configs.Add(new StrategyConfig(
                name, 
                1000m, 
                () => new LiveFlashCrashReverseStrategy(
                    name, 
                    param.threshold, 
                    param.window, 
                    param.tpMargin, 
                    0.05m
                )
            ));
        }

        return configs;
    }

    // --- PAUSE & RESUME CONTROLS ---
    private static volatile bool _isPaused = false;
    private static volatile bool _isMuted = false;
    private static CancellationTokenSource _pauseCts = new CancellationTokenSource();
    private static readonly string _sessionCsvFilename = $"LivePaperTrades_{DateTime.Now:yyyyMMdd_HHmmss}.csv";

    private static readonly int _maxNameLength = _strategyConfigs.Max(c => c.Name.Length);
    private static Dictionary<string, PaperBroker> _strategyBrokers = new Dictionary<string, PaperBroker>();
    private static Dictionary<string, string> _tokenNames = new Dictionary<string, string>();
    private static readonly HashSet<string> _subscribedTokens = new();
    private static ClientWebSocket? _activeWs;
private static readonly SemaphoreSlim _wsSendSemaphore = new SemaphoreSlim(1, 1);
    static async Task Main(string[] args)
    {
        Console.Clear();
        Console.WriteLine("=========================================");
        Console.WriteLine("  LIVE PAPER TRADING ENGINE INITIALIZED  ");
        Console.WriteLine("  Controls: 'P' = Pause | 'R' = Resume | 'M' = Mute Logs");
        Console.WriteLine("=========================================");

        foreach (var config in _strategyConfigs)
            _strategyBrokers[config.Name] = new PaperBroker(config.Name, config.StartingCapital, _tokenNames);

        Console.WriteLine($"Loaded {_strategyConfigs.Count} strategy configurations.");


        // THE INTERCEPTOR: Catch CTRL+C before the console dies!
        Console.CancelKeyPress += (sender, e) =>
        {
            e.Cancel = true; // Tell the OS: "Wait, don't kill the app yet!"
            Console.ForegroundColor = ConsoleColor.Yellow;
            Console.WriteLine("\n\n[SYSTEM] Graceful shutdown initiated...");

            // Run our export script
            ExportLedgerToCsv(quietMode: true);

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
                else if (keyInfo.Key == ConsoleKey.M)
                {
                    _isMuted = !_isMuted;
                    
                    // Tell every active broker to update its mute status
                    foreach (var broker in _strategyBrokers.Values)
                    {
                        broker.IsMuted = _isMuted;
                    }

                    Console.ForegroundColor = ConsoleColor.Cyan;
                    Console.WriteLine($"\n[SYSTEM] Trade logs are now {(_isMuted ? "MUTED" : "UNMUTED")}.");
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
        await DiscoverNewMarkets(apiClient);
        Console.WriteLine($"Found {_subscribedTokens.Count} active outcome tokens to monitor!");

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
                foreach (var config in _strategyConfigs)
                {
                    var name = config.Name;
                    var broker = _strategyBrokers[name];
                    decimal totalEquity = broker.GetTotalPortfolioValue();
                    decimal pnl = totalEquity - config.StartingCapital;
                    decimal activeBets = totalEquity - broker.CashBalance;

                    Console.ForegroundColor = pnl >= 0 ? ConsoleColor.Green : ConsoleColor.Red;
                    Console.WriteLine($"[{name.PadRight(_maxNameLength)}] Equity: ${totalEquity:0.00} | PnL: ${(pnl):0.00} | In Bets: ${activeBets:0.00} | Trades: {broker.TotalTradesExecuted} (W:{broker.WinningTrades} L:{broker.LosingTrades})");
                    Console.ResetColor();
                }
                Console.WriteLine("=================================================================\n");
                
                ExportLedgerToCsv(quietMode: true);

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

                // --- NEW MARKET DISCOVERY ---
                try
                {
                    List<string> newTokens = await DiscoverNewMarkets(apiClient);

                    if (newTokens.Count > 0)
                    {
                        Console.ForegroundColor = ConsoleColor.Green;
                        Console.WriteLine($"[DISCOVERY] Found {newTokens.Count} new market(s) crossing the $50k threshold! Subscribing...");
                        Console.ResetColor();

                        await SubscribeNewTokens(newTokens);
                    }
                }
                catch { /* Ignore discovery errors */ }
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
                await ws.ConnectAsync(new Uri("wss://ws-subscriptions-clob.polymarket.com/ws/market"), _pauseCts.Token);
                _activeWs = ws;
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

                        //string message = Encoding.UTF8.GetString(ms.ToArray());
                        string message = Encoding.UTF8.GetString(ms.GetBuffer(), 0, (int)ms.Length);
                        ms.SetLength(0);

                        try
                        {
                            using var doc = JsonDocument.Parse(message);
                            var root = doc.RootElement;

                            if (root.ValueKind == JsonValueKind.Object && root.TryGetProperty("event_type", out var eventTypeEl))
                            {
                                string? eventType = eventTypeEl.GetString();

                                if (eventType == "book" && root.TryGetProperty("asset_id", out var assetIdEl))
                                {
                                    string? assetId = assetIdEl.GetString();
                                    if (!string.IsNullOrEmpty(assetId))
                                    {
                                        if (!orderBooks.ContainsKey(assetId))
                                        {
                                            orderBooks[assetId] = new LocalOrderBook(assetId);

                                            activeStrategies[assetId] = _strategyConfigs
                                                .Select(c => c.Factory())
                                                .ToList();
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
                                            string? assetId = idEl.GetString();

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

                                                if (!_isMuted)
                                                {
                                                Console.ForegroundColor = ConsoleColor.DarkGray;
                                                Console.Write(".");
                                                Console.ResetColor();   
                                                }

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
                var tokenList = _subscribedTokens.ToList();

                for (int i = 0; i < tokenList.Count; i += chunkSize)
                {
                    if (_pauseCts.IsCancellationRequested) break;

                    var chunk = tokenList.Skip(i).Take(chunkSize);
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
                _activeWs = null;
            }
            catch (Exception ex)
            {
                _activeWs = null;
                Console.ForegroundColor = ConsoleColor.Yellow;
                Console.WriteLine($"\n[CONNECTION LOST] WebSocket connection dropped: {ex.Message}");
                Console.WriteLine("Reconnecting in 5 seconds...");
                Console.ResetColor();

                await Task.Delay(5000);
            }
        }
    }

    // ==========================================
    // NEW MARKET DISCOVERY
    // ==========================================
    private static async Task<List<string>> DiscoverNewMarkets(PolymarketClient apiClient)
    {
        var newlyDiscovered = new List<string>();
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
                    if (market.StartDate.HasValue && market.StartDate.Value > DateTime.UtcNow) continue;
                    if (market.Volume < 50000m) continue;

                    if (market.ClobTokenIds != null && !market.IsClosed && market.ClobTokenIds.Length > 0)
                    {
                        string yesToken = market.ClobTokenIds[0];
                        
                        // Add returns 'true' if the item didn't exist before.
                        // This safely guarantees we only collect truly new tokens!
                        if (_subscribedTokens.Add(yesToken))
                        {
                            _tokenNames.TryAdd(yesToken, market.Question);
                            newlyDiscovered.Add(yesToken);
                        }
                    }
                }
            }
        }
        return newlyDiscovered;
    }

    private static async Task SubscribeNewTokens(List<string> newTokens)
    {
        var ws = _activeWs;
        if (ws == null || ws.State != WebSocketState.Open || newTokens.Count == 0) return;

        int chunkSize = 50;
        for (int i = 0; i < newTokens.Count; i += chunkSize)
        {
            var chunk = newTokens.Skip(i).Take(chunkSize);
            string assetListString = string.Join("\",\"", chunk);
            string msg = $"{{\"assets_ids\":[\"{assetListString}\"],\"operation\":\"subscribe\"}}";

            var bytes = Encoding.UTF8.GetBytes(msg);

            // Async-safe lock! Wait for the line to be clear, send, then release.
            await _wsSendSemaphore.WaitAsync();
            try
            {
                await ws.SendAsync(new ArraySegment<byte>(bytes), WebSocketMessageType.Text, true, CancellationToken.None);
            }
            finally
            {
                _wsSendSemaphore.Release();
            }

            await Task.Delay(500);
        }
    }

    // ==========================================
    // GRACEFUL SHUTDOWN & LEDGER EXPORT
    // ==========================================
    private static void ExportLedgerToCsv(bool quietMode = false)
    {
        try
        {
            using var writer = new StreamWriter(_sessionCsvFilename);

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

            // Only print the success message if we are shutting down (not quiet mode)
            if (!quietMode)
            {
                Console.ForegroundColor = ConsoleColor.Green;
                Console.WriteLine($"\n[EXPORT SUCCESS] {totalTrades} trades saved to {_sessionCsvFilename}");
                Console.ResetColor();
            }
        }
        catch (Exception ex)
        {
            Console.ForegroundColor = ConsoleColor.Red;
            Console.WriteLine($"\n[EXPORT FAILED] Could not save ledger: {ex.Message}");
            Console.ResetColor();
        }
    }
}