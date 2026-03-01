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
            "Sniper_Ultra_Strict_100$", 
            100m, 
            () => new LiveFlashCrashSniperStrategy("Sniper_Ultra_Strict_100$", 0.25m, 60)
        ));

        configs.Add(new StrategyConfig(
            "Sniper_Ultra_Strict_500$",
            500m,
            () => new LiveFlashCrashSniperStrategy("Sniper_Ultra_Strict_500$", 0.25m, 60)
        ));


        // ---------------------------------------------------------
        // GRID 1: Live Flash Crash Sniper
        // ---------------------------------------------------------
        decimal[] sniperThresholds = {0.15m, 0.20m, 0.25m, 0.30m };
        long[] sniperWindows = { 10, 20, 30, 60, 120 };
        decimal[] sniperTakeProfit = { 0.03m, 0.05m, 0.10m };
        decimal[] sniperStopLoss = { 0.10m, 0.15m, 0.25m };
        decimal[] sniperSlippage = { 0.01m, 0.03m, 0.05m };

        int sniperVersion = 1;

        // This LINQ query creates the Cartesian Product automatically!
        var sniperGrid = from threshold in sniperThresholds
                         from window in sniperWindows
                         from Profit in sniperTakeProfit
                         from stop in sniperStopLoss
                         from slip in sniperSlippage
                         select new { threshold, window, Profit, stop, slip };

        foreach (var param in sniperGrid)
        {
            string name = $"Sniper_v{sniperVersion++}_T{param.threshold}_W{param.window}_P{param.Profit}_S{param.stop}_SL{param.slip}";
            configs.Add(new StrategyConfig(
                name,
                1000m,
                () => new LiveFlashCrashSniperStrategy(name, param.threshold, param.window, param.Profit, param.stop, slippageTolerance: param.slip)
            ));
        }
        return configs;
    }

    // --- LATENCY SIMULATION (based on ping to Polymarket CLOB API) ---
    private const int REALISTIC_LATENCY_MS = 250;
    private static volatile bool _latencyEnabled = true;

    // --- PAUSE & RESUME CONTROLS ---
    private static volatile bool _isPaused = false;
    private static volatile bool _isMuted = false;
    private static CancellationTokenSource _pauseCts = new CancellationTokenSource();
    private static readonly string _sessionCsvFilename = $"LivePaperTrades_{DateTime.Now:yyyyMMdd_HHmmss}.csv";

    private static readonly int _maxNameLength = _strategyConfigs.Max(c => c.Name.Length);
    private static Dictionary<string, PaperBroker> _strategyBrokers = new Dictionary<string, PaperBroker>();
    private static Dictionary<string, string> _tokenNames = new Dictionary<string, string>();
    private static readonly HashSet<string> _subscribedTokens = new();
    private static readonly HashSet<string> _droppedStrategies = new();
    private static ClientWebSocket? _activeWs;
    private static readonly SemaphoreSlim _wsSendSemaphore = new SemaphoreSlim(1, 1);

    static async Task Main(string[] args)
    {
        Console.Clear();
        Console.WriteLine("=========================================");
        Console.WriteLine("  LIVE PAPER TRADING ENGINE INITIALIZED  ");
        Console.WriteLine("  Controls: 'P' = Pause | 'R' = Resume | 'M' = Mute | 'L' = Latency | 'D' = Drop");
        Console.WriteLine($"  Latency starst at {REALISTIC_LATENCY_MS}");
        Console.WriteLine("=========================================");

        foreach (var config in _strategyConfigs)
        {
            var broker = new PaperBroker(config.Name, config.StartingCapital, _tokenNames);
            broker.LatencyMs = _latencyEnabled ? REALISTIC_LATENCY_MS : 0;
            _strategyBrokers[config.Name] = broker;
        }

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
                else if (keyInfo.Key == ConsoleKey.L)
                {
                    _latencyEnabled = !_latencyEnabled;
                    int newLatency = _latencyEnabled ? REALISTIC_LATENCY_MS : 0;

                    foreach (var broker in _strategyBrokers.Values)
                    {
                        broker.LatencyMs = newLatency;
                    }

                    Console.ForegroundColor = ConsoleColor.Cyan;
                    Console.WriteLine($"\n[SYSTEM] Latency simulation: {(_latencyEnabled ? $"ON ({REALISTIC_LATENCY_MS}ms)" : "OFF (instant)")}");
                    Console.ResetColor();
                }
                else if (keyInfo.Key == ConsoleKey.D)
                {
                    Console.ForegroundColor = ConsoleColor.Yellow;
                    Console.WriteLine("\n[DROP MODE] Type strategy name (or partial match) and press Enter:");
                    Console.ResetColor();

                    string? input = Console.ReadLine()?.Trim();
                    if (string.IsNullOrEmpty(input))
                    {
                        Console.WriteLine("[DROP MODE] Cancelled — no input provided.");
                        continue;
                    }

                    var matches = _strategyConfigs
                        .Where(c => c.Name.Contains(input, StringComparison.OrdinalIgnoreCase) && !_droppedStrategies.Contains(c.Name))
                        .ToList();

                    if (matches.Count == 0)
                    {
                        Console.ForegroundColor = ConsoleColor.Red;
                        Console.WriteLine($"[DROP MODE] No active strategy matches '{input}'.");
                        Console.ResetColor();
                    }
                    else if (matches.Count == 1)
                    {
                        string name = matches[0].Name;
                        _droppedStrategies.Add(name);
                        Console.ForegroundColor = ConsoleColor.Red;
                        Console.WriteLine($"[DROPPED] '{name}' has been removed from live trading. ({_strategyConfigs.Count - _droppedStrategies.Count} strategies remaining)");
                        Console.ResetColor();
                    }
                    else
                    {
                        Console.ForegroundColor = ConsoleColor.Yellow;
                        Console.WriteLine($"[DROP MODE] {matches.Count} strategies match '{input}'. Be more specific:");
                        foreach (var m in matches.Take(20))
                            Console.WriteLine($"  - {m.Name}");
                        if (matches.Count > 20)
                            Console.WriteLine($"  ... and {matches.Count - 20} more");
                        Console.ResetColor();
                    }
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
                    if (_droppedStrategies.Contains(config.Name)) continue;
                    var name = config.Name;
                    var broker = _strategyBrokers[name];
                    decimal totalEquity = broker.GetTotalPortfolioValue();
                    decimal pnl = totalEquity - config.StartingCapital;
                    decimal mtmValue = totalEquity - broker.CashBalance;
                    decimal realizedPnl = broker.CashBalance - config.StartingCapital;

                    Console.ForegroundColor = pnl >= 0 ? ConsoleColor.Green : ConsoleColor.Red;
                    Console.WriteLine($"[{name.PadRight(_maxNameLength)}] Equity: ${totalEquity:0.00} | PnL: ${pnl:0.00} (Real: ${realizedPnl:0.00} + MTM: ${mtmValue:0.00}) | Actions: {broker.TotalActions} Exits: {broker.TotalTradesExecuted} (W:{broker.WinningTrades} L:{broker.LosingTrades})");
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
                                                .Where(c => !_droppedStrategies.Contains(c.Name))
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
                                                    if (_droppedStrategies.Contains(strategy.StrategyName)) continue;
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

            writer.WriteLine("Timestamp,StrategyName,StartingCapital,MarketName,AssetId,Side,ExecutionPrice,Shares,DollarValue");

            int totalTrades = 0;
            foreach (var brokerKvp in _strategyBrokers)
            {
                string strategyName = brokerKvp.Key;
                var broker = brokerKvp.Value;
                var config = _strategyConfigs.FirstOrDefault(c => c.Name == strategyName);
                decimal startingCapital = config?.StartingCapital ?? 1000m;

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

                    writer.WriteLine($"{trade.Date:O},{strategyName},{startingCapital},{marketName},{trade.OutcomeId},{trade.Side},{trade.Price},{trade.Shares},{trade.DollarValue}");
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