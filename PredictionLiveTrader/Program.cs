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
using System.Diagnostics;

namespace PredictionLiveTrader;

record StrategyConfig(string Name, decimal StartingCapital, Func<ILiveStrategy> Factory, bool IsShared = false);

class Program
{
    // ==========================================
    // STRATEGY CONFIGURATION (Grid Search / Cartesian Product)
    // ==========================================
    private static Dictionary<string, List<string>> _arbEvents = new();
    private static Dictionary<string, string> _arbTokenNames = new();
    private static readonly List<StrategyConfig> _strategyConfigs = GenerateStrategyGrid();
    private static List<StrategyConfig> GenerateStrategyGrid()
    {
        var configs = new List<StrategyConfig>();
        
        // ---------------------------------------------------------
        // 1. Fetch Arb Markets via API
        // Because this runs in static initialization, we must block synchronously
        // ---------------------------------------------------------
        Console.WriteLine("[SYSTEM] Fetching Top Liquid 3+ Leg Events for Arbitrage...");
        var scanner = new PolymarketMarketScanner();

        // Fetch top liquid 3+ leg events (120s timeout — pagination is slow)
        var scanTask = scanner.GetTopLiquidEventsAsync(200);
        if (scanTask.Wait(TimeSpan.FromSeconds(120)))
        {
            _arbEvents = scanTask.Result;
            _arbTokenNames = scanner.TokenNames;
        }
        else
        {
            Console.WriteLine("[SYSTEM] WARNING: Scanner timed out after 120s — starting with 0 arb events.");
            _arbEvents = new Dictionary<string, List<string>>();
        }
        
        // ---------------------------------------------------------
        // the Telemetry Strategy (Logs to CSV)
        // ---------------------------------------------------------
        configs.Add(new StrategyConfig(
            Name: "Fast_Merge_Arb_Telemetry",
            StartingCapital: 5000m,
            Factory: () => new FastMergeArbTelemetryStrategy(_arbEvents),
            IsShared: true
        ));

        // ---------------------------------------------------------
        // Execution Strategy (Actually buys the YES tokens)
        // ---------------------------------------------------------
        configs.Add(new StrategyConfig(
            Name: "Categorical_Merge_Arb_Execution",
            StartingCapital: 5000m,
            Factory: () => new PolymarketCategoricalArbStrategy(_arbEvents, name: "Categorical_Merge_Arb_Execution")
            {
                LockEventAfterBuy = true
            },
            IsShared: true
        ));
        return configs;
    }

    // --- LATENCY SIMULATION (based on ping to Polymarket CLOB API) ---
    private const int REALISTIC_LATENCY_MS = 500; // Measured from production: ~500ms per order round-trip
    private static volatile bool _latencyEnabled = true;

    // --- PAUSE & RESUME CONTROLS ---
    private static volatile bool _isPaused = false;
    private static volatile bool _isMuted = false;      // M: mute book dots only
    private static volatile bool _tradeMuted = false;    // T: mute trade logs only
    private static volatile bool _verboseDots = false;
    private static volatile bool _quietMode = false;
    private static CancellationTokenSource _pauseCts = new CancellationTokenSource();
    private static readonly string _sessionCsvFilename = $"LivePaperTrades_{DateTime.Now:yyyyMMdd_HHmmss}.csv";

    private const decimal MAX_BET_SIZE = 1000.00m;
    private static readonly int _maxNameLength = _strategyConfigs.Max(c => c.Name.Length);
    private static Dictionary<string, PaperBroker> _strategyBrokers = new Dictionary<string, PaperBroker>();
    private static Dictionary<string, string> _tokenNames = new Dictionary<string, string>();
    private static Dictionary<string, decimal> _tokenMinSizes = new Dictionary<string, decimal>();
    private static Dictionary<string, int> _tokenFeeRates = new Dictionary<string, int>();
    private static readonly HashSet<string> _subscribedTokens = new();
    private static readonly HashSet<string> _droppedStrategies = new();
    // Tracks the last time each token received a book update (for staleness detection)
    private static readonly System.Collections.Concurrent.ConcurrentDictionary<string, DateTime> _lastBookUpdate = new();
    // Markets already force-settled (don't re-check)
    private static readonly HashSet<string> _forceSettled = new();
    private static ClientWebSocket? _activeWs;
    private static readonly SemaphoreSlim _wsSendSemaphore = new SemaphoreSlim(1, 1);
    private static readonly HttpClient _clobHttpClient = new() { BaseAddress = new Uri("https://clob.polymarket.com/") };
    private static readonly bool _recordMarketData = false;
    private static readonly MarketReplayLogger? _replayLogger = _recordMarketData ? new MarketReplayLogger("MarketData") : null;

    static async Task Main(string[] args)
    {
        // --- PREPROCESS MODE: convert .gz files to compact binary ---
        int preprocessIdx = Array.IndexOf(args, "--preprocess");
        if (preprocessIdx >= 0)
        {
            string preprocessDir = preprocessIdx + 1 < args.Length ? args[preprocessIdx + 1] : "MarketData1week";
            ReplayPreprocessor.Run(preprocessDir);
            return;
        }

        // --- REPLAY MODE: offline backtest from recorded .gz data ---
        int replayIdx = Array.IndexOf(args, "--replay");
        if (replayIdx >= 0)
        {
            string replayDir = replayIdx + 1 < args.Length ? args[replayIdx + 1] : "MarketData1week";
            ReplayRunner.Run(replayDir, _strategyConfigs);
            return;
        }

        Console.Clear();
        Console.WriteLine("=========================================");
        Console.WriteLine("  LIVE PAPER TRADING ENGINE INITIALIZED  ");
        Console.WriteLine("  Controls: P=Pause R=Resume M=Mute(dots) T=Mute(trades) Q=Quiet V=Verbose L=Latency D=Drop K=Cull");
        Console.WriteLine($"  Latency starst at {REALISTIC_LATENCY_MS}");
        Console.WriteLine("=========================================");

        foreach (var config in _strategyConfigs)
        {
            var broker = new PaperBroker(config.Name, config.StartingCapital, _tokenNames, _tokenMinSizes, MAX_BET_SIZE, _tokenFeeRates);
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

            // Flush all buffered market data to disk
            if (_replayLogger != null)
            {
                _replayLogger.StopAsync().GetAwaiter().GetResult();
                Console.WriteLine("[SYSTEM] Market replay data flushed.");
            }

            // Run our export script
            ExportLedgerToCsv(quietMode: true);

            Console.WriteLine("[SYSTEM] Engine safely terminated. Goodbye.");
            Console.ResetColor();

            Environment.Exit(0); // Now we have permission to die peacefully.
        };

        var activeStrategies = new Dictionary<string, List<ILiveStrategy>>();
        var orderBooks = new Dictionary<string, LocalOrderBook>();

        // Shared strategy instances: multi-leg strategies (arb) need ONE instance
        // that accumulates books across all assets, not per-asset copies.
        var sharedInstances = new Dictionary<string, ILiveStrategy>();
        foreach (var config in _strategyConfigs.Where(c => c.IsShared))
            sharedInstances[config.Name] = config.Factory();

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
                    Console.ForegroundColor = ConsoleColor.Cyan;
                    Console.WriteLine($"\n[SYSTEM] Book dots {(_isMuted ? "MUTED" : "UNMUTED")}.");
                    Console.ResetColor();
                }
                else if (keyInfo.Key == ConsoleKey.T)
                {
                    _tradeMuted = !_tradeMuted;
                    foreach (var broker in _strategyBrokers.Values)
                        broker.IsMuted = _tradeMuted;

                    Console.ForegroundColor = ConsoleColor.Cyan;
                    Console.WriteLine($"\n[SYSTEM] Trade logs {(_tradeMuted ? "MUTED" : "VISIBLE")}.");
                    Console.ResetColor();
                }
                else if (keyInfo.Key == ConsoleKey.Q)
                {
                    _quietMode = !_quietMode;
                    // Quiet mode still prints this one message so you know it toggled
                    Console.ForegroundColor = ConsoleColor.Cyan;
                    Console.WriteLine($"\n[SYSTEM] Quiet mode: {(_quietMode ? "ON (all output silenced)" : "OFF")}");
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
                else if (keyInfo.Key == ConsoleKey.V)
                {
                    _verboseDots = !_verboseDots;
                    Console.ForegroundColor = ConsoleColor.Cyan;
                    Console.WriteLine($"\n[SYSTEM] Book updates: {(_verboseDots ? "VERBOSE (full details)" : "DOTS (compact)")}");
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
                else if (keyInfo.Key == ConsoleKey.K)
                {
                    Console.ForegroundColor = ConsoleColor.Yellow;
                    Console.Write("\n[CULL MODE] How many worst performers to drop? ");
                    Console.ResetColor();

                    string? countInput = Console.ReadLine()?.Trim();
                    if (!int.TryParse(countInput, out int cullCount) || cullCount <= 0)
                    {
                        Console.WriteLine("[CULL MODE] Cancelled — invalid number.");
                        continue;
                    }

                    var activeConfigs = _strategyConfigs
                        .Where(c => !_droppedStrategies.Contains(c.Name))
                        .ToList();

                    var ranked = activeConfigs
                        .Select(c => new { c.Name, PnL = _strategyBrokers[c.Name].GetTotalPortfolioValue() - c.StartingCapital })
                        .OrderBy(x => x.PnL)
                        .Take(cullCount)
                        .ToList();

                    Console.ForegroundColor = ConsoleColor.Red;
                    Console.WriteLine($"\n[CULL MODE] Bottom {ranked.Count} strategies:");
                    foreach (var r in ranked)
                        Console.WriteLine($"  {r.Name}  (PnL: ${r.PnL:0.00})");

                    Console.ForegroundColor = ConsoleColor.Yellow;
                    Console.Write($"\nConfirm dropping these {ranked.Count} strategies? (Y/N): ");
                    Console.ResetColor();

                    var confirmKey = Console.ReadKey(intercept: true);
                    Console.WriteLine(confirmKey.KeyChar.ToString());

                    if (confirmKey.Key == ConsoleKey.Y)
                    {
                        foreach (var r in ranked)
                            _droppedStrategies.Add(r.Name);

                        Console.ForegroundColor = ConsoleColor.Red;
                        Console.WriteLine($"[CULLED] Dropped {ranked.Count} strategies. ({_strategyConfigs.Count - _droppedStrategies.Count} remaining)");
                        Console.ResetColor();
                    }
                    else
                    {
                        Console.WriteLine("[CULL MODE] Cancelled.");
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

        // Ensure all arb scanner tokens are subscribed to the WebSocket
        int arbTokenCount = 0;
        foreach (var evt in _arbEvents)
            foreach (var token in evt.Value)
            {
                if (_subscribedTokens.Add(token)) arbTokenCount++;
                if (_arbTokenNames.TryGetValue(token, out var name))
                    _tokenNames.TryAdd(token, name);
            }

        Console.WriteLine($"[SYSTEM] Registered {arbTokenCount} arb tokens from {_arbEvents.Count} events for WS subscription.");

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
                if (!_quietMode)
                {
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

                        lock (GlobalSimulatedBroker.ConsoleLock)
                        {
                            Console.ForegroundColor = pnl >= 0 ? ConsoleColor.Green : ConsoleColor.Red;
                            Console.WriteLine($"[{name.PadRight(_maxNameLength)}] Equity: ${totalEquity:0.00} | PnL: ${pnl:0.00} (Real: ${realizedPnl:0.00} + MTM: ${mtmValue:0.00}) | Actions: {broker.TotalActions} Exits: {broker.TotalTradesExecuted} (W:{broker.WinningTrades} L:{broker.LosingTrades}) Rej: {broker.RejectedOrders}");
                            Console.ResetColor();
                        }
                    }
                    Console.WriteLine("=================================================================\n");

                    // Print near-miss report from telemetry strategy
                    if (sharedInstances.TryGetValue("Fast_Merge_Arb_Telemetry", out var telemetryInstance)
                        && telemetryInstance is FastMergeArbTelemetryStrategy telemetry)
                    {
                        telemetry.PrintNearMissReport();
                    }
                }

                ExportLedgerToCsv(quietMode: true);

                if (!_quietMode) Console.WriteLine("\n[SYSTEM] Running background settlement sweep...");
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

                // --- STALENESS SWEEP ---
                // Markets with no book updates for 1 hour are likely settled.
                // Query the API to confirm and resolve at the correct price.
                try
                {
                    var staleThreshold = DateTime.UtcNow.AddHours(-1);
                    var staleTokens = _lastBookUpdate
                        .Where(kvp => kvp.Value < staleThreshold && !_forceSettled.Contains(kvp.Key))
                        .Select(kvp => kvp.Key)
                        .ToList();

                    int settled = 0;
                    foreach (var tokenId in staleTokens)
                    {
                        // Only bother checking if any strategy holds a position
                        bool anyPosition = _strategyBrokers.Values.Any(b => b.GetPositionShares(tokenId) > 0 || b.GetNoPositionShares(tokenId) > 0);
                        if (!anyPosition)
                        {
                            _forceSettled.Add(tokenId);
                            continue;
                        }

                        var market = await apiClient.GetMarketByTokenIdAsync(tokenId);
                        if (market == null) continue;

                        if (market.IsClosed && market.ClobTokenIds != null && market.OutcomePrices != null)
                        {
                            // Find our token's index to get the correct payout price
                            for (int i = 0; i < market.ClobTokenIds.Length; i++)
                            {
                                if (market.ClobTokenIds[i] == tokenId)
                                {
                                    decimal payoutPrice = 0m;
                                    if (i < market.OutcomePrices.Length)
                                        decimal.TryParse(market.OutcomePrices[i], System.Globalization.NumberStyles.Any, System.Globalization.CultureInfo.InvariantCulture, out payoutPrice);

                                    foreach (var broker in _strategyBrokers.Values)
                                    {
                                        if (broker.GetPositionShares(tokenId) > 0 || broker.GetNoPositionShares(tokenId) > 0)
                                            broker.ResolveMarket(tokenId, payoutPrice);
                                    }

                                    string name = _tokenNames.GetValueOrDefault(tokenId, tokenId[..Math.Min(8, tokenId.Length)] + "...");
                                    if (!_quietMode) lock (GlobalSimulatedBroker.ConsoleLock)
                                    {
                                        Console.ForegroundColor = ConsoleColor.Yellow;
                                        Console.WriteLine($"[STALE SETTLE] {name} — no updates for 1h, API confirmed closed. Settled @ ${payoutPrice:0.00}");
                                        Console.ResetColor();
                                    }
                                    settled++;
                                    break;
                                }
                            }
                        }
                        _forceSettled.Add(tokenId);

                        await Task.Delay(100); // Rate limit API calls
                    }

                    if (settled > 0 && !_quietMode)
                        Console.WriteLine($"[STALE SETTLE] Force-settled {settled} stale market(s).");
                }
                catch { /* Ignore staleness sweep errors */ }

                // --- NEW MARKET DISCOVERY ---
                try
                {
                    List<string> newTokens = await DiscoverNewMarkets(apiClient);

                    if (newTokens.Count > 0)
                    {
                        if (!_quietMode) lock (GlobalSimulatedBroker.ConsoleLock)
                        {
                            Console.ForegroundColor = ConsoleColor.Green;
                            Console.WriteLine($"[DISCOVERY] Found {newTokens.Count} new market(s) crossing the $50k threshold! Subscribing...");
                            Console.ResetColor();
                        }

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

                if (!_quietMode) Console.WriteLine("\n[CONNECTING] Connecting to Polymarket WebSocket...");
                await ws.ConnectAsync(new Uri("wss://ws-subscriptions-clob.polymarket.com/ws/market"), _pauseCts.Token);
                _activeWs = ws;
                if (!_quietMode) Console.WriteLine("[CONNECTED] Listening for live order book updates... (Press CTRL+C to stop)\n");

                var listenTask = Task.Run(async () =>
                {
                    var receiveBuffer = new byte[8192];
                    using var ms = new MemoryStream();
                    var staleTimeout = TimeSpan.FromSeconds(30);

                    while (ws.State == WebSocketState.Open)
                    {
                        WebSocketReceiveResult result;
                        do
                        {
                            // Combine user pause token with a stale-connection timeout
                            using var timeoutCts = new CancellationTokenSource(staleTimeout);
                            using var linkedCts = CancellationTokenSource.CreateLinkedTokenSource(_pauseCts.Token, timeoutCts.Token);
                            try
                            {
                                result = await ws.ReceiveAsync(new ArraySegment<byte>(receiveBuffer), linkedCts.Token);
                            }
                            catch (OperationCanceledException) when (!_pauseCts.IsCancellationRequested)
                            {
                                // Timeout fired, not user pause — connection is stale
                                throw new TimeoutException("No data received for 30 seconds — forcing reconnect.");
                            }
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

                        _replayLogger?.EnqueueTick(message);

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
                                                .Select(c => c.IsShared ? sharedInstances[c.Name] : c.Factory())
                                                .ToList();
                                        }

                                        if (root.TryGetProperty("bids", out var bidsEl) && root.TryGetProperty("asks", out var asksEl))
                                        {
                                            orderBooks[assetId].ProcessBookUpdate(bidsEl, asksEl);

                                            // Dispatch initial snapshot to strategies so multi-leg
                                            // strategies register this token's book immediately
                                            var bookStrategies = activeStrategies[assetId];
                                            foreach (var strategy in bookStrategies)
                                            {
                                                if (_droppedStrategies.Contains(strategy.StrategyName)) continue;
                                                strategy.OnBookUpdate(orderBooks[assetId], _strategyBrokers[strategy.StrategyName]);
                                            }
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
                                                _lastBookUpdate[assetId] = DateTime.UtcNow;

                                                book.UpdatePriceLevel(side, price, size);

                                                if (!_isMuted && !_quietMode) lock (GlobalSimulatedBroker.ConsoleLock)
                                                {
                                                    if (_verboseDots)
                                                    {
                                                        string name = _tokenNames.GetValueOrDefault(assetId, assetId.Substring(0, 8) + "...");
                                                        if (name.Length > 45) name = name.Substring(0, 42) + "...";
                                                        Console.ForegroundColor = ConsoleColor.DarkGray;
                                                        Console.WriteLine($"[{DateTime.Now:HH:mm:ss.fff}] [BOOK] {side} ${price:0.00} x{size:0} | {name}");
                                                        Console.ResetColor();
                                                    }
                                                    else
                                                    {
                                                        Console.ForegroundColor = ConsoleColor.DarkGray;
                                                        Console.Write(".");
                                                        Console.ResetColor();
                                                    }
                                                }

                                                // Reset per-broker consumed liquidity for this asset (fresh book data arrived)
                                                foreach (var broker in _strategyBrokers.Values)
                                                    broker.ResetConsumedLiquidity(assetId);

                                                // THE MULTIPLEXER ROUTING: Feed the exact same book to every strategy in parallel!
                                                var strategies = activeStrategies[assetId];
                                                var parallelOptions = new ParallelOptions
                                                {
                                                    MaxDegreeOfParallelism = Math.Max(Environment.ProcessorCount - 1, 1)
                                                };
                                                var sw = Stopwatch.StartNew();
                                                Parallel.ForEach(strategies, parallelOptions, strategy =>
                                                {
                                                    if (_droppedStrategies.Contains(strategy.StrategyName)) return;
                                                    strategy.OnBookUpdate(book, _strategyBrokers[strategy.StrategyName]);
                                                });
                                                sw.Stop();
                                                if (sw.ElapsedMilliseconds > 80)
                                                {
                                                    Console.WriteLine($"[WARNING] CPU Bottleneck! Tick processing took {sw.ElapsedMilliseconds}ms");
                                                }
                                            }
                                        }
                                    }
                                }
                            }
                        }
                        catch (Exception ex)
                        {
                            if (_verboseDots && !_quietMode) lock (GlobalSimulatedBroker.ConsoleLock)
                            {
                                Console.ForegroundColor = ConsoleColor.DarkYellow;
                                Console.WriteLine($"[{DateTime.Now:HH:mm:ss.fff}] [WS ERROR] {ex.GetType().Name}: {ex.Message}");
                                Console.ResetColor();
                            }
                        }
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
                if (!_quietMode) lock (GlobalSimulatedBroker.ConsoleLock)
                {
                    Console.ForegroundColor = ConsoleColor.Yellow;
                    Console.WriteLine($"\n[CONNECTION LOST] WebSocket connection dropped: {ex.Message}");
                    Console.WriteLine("Reconnecting in 5 seconds...");
                    Console.ResetColor();
                }

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
                            _tokenMinSizes.TryAdd(yesToken, market.OrderMinSize > 0 ? market.OrderMinSize : 1.00m);
                            newlyDiscovered.Add(yesToken);

                            // Fetch fee rate from CLOB API (non-blocking best-effort)
                            try
                            {
                                var feeResp = await _clobHttpClient.GetStringAsync($"/fee-rate?token_id={yesToken}");
                                using var doc = System.Text.Json.JsonDocument.Parse(feeResp);
                                var root = doc.RootElement;
                                if ((root.TryGetProperty("fee_rate_bps", out var feeEl) ||
                                     root.TryGetProperty("feeRateBps", out feeEl)) &&
                                    feeEl.TryGetInt32(out int bps) && bps > 0)
                                {
                                    _tokenFeeRates[yesToken] = bps;
                                }
                            }
                            catch { /* Non-critical — fees default to 0 if fetch fails */ }
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

            // Write summary CSV with per-strategy reject counts
            string summaryFilename = _sessionCsvFilename.Replace(".csv", "_summary.csv");
            using var summaryWriter = new StreamWriter(summaryFilename);
            summaryWriter.WriteLine("StrategyName,RejectedOrders");
            foreach (var brokerKvp in _strategyBrokers)
            {
                summaryWriter.WriteLine($"{brokerKvp.Key},{brokerKvp.Value.RejectedOrders}");
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