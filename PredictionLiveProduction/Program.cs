using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Net.WebSockets;
using System.Text;
using System.Text.Json;
using System.Threading;
using System.Threading.Tasks;
using Microsoft.Extensions.DependencyInjection;
using PredictionBacktester.Data.ApiClients;
using PredictionBacktester.Engine;
using PredictionBacktester.Engine.LiveExecution;
using PredictionBacktester.Strategies;
using Serilog;

namespace PredictionLiveProduction;

class Program
{
    // ==========================================
    // STRATEGY CONFIGURATION (Single Optimized Strategy)
    // ==========================================
    private const string STRATEGY_NAME = "FlashCrashSniper_Production";
    private const decimal STARTING_CAPITAL = 100m; // Will be overridden by real wallet balance

    // Strategy parameters — tuned from paper trading results
    private const decimal CRASH_THRESHOLD = 0.25m;
    private const long TIME_WINDOW_SECONDS = 60;
    private const decimal TAKE_PROFIT = 0.05m;
    private const decimal STOP_LOSS = 0.10m;
    private const decimal RISK_PERCENTAGE = 0.03m;
    private const decimal ENTRY_SLIPPAGE = 0.03m;
    private const decimal EXIT_SLIPPAGE = 0.03m;
    private const long SUSTAIN_TIMER_MS = 1000;
    private const long SETTLEMENT_LOCK_MS = 5000;

    // Risk controls (editable at runtime via keyboard)
    private static decimal _maxBetSize = 100.00m;       // Hard cap on dollars per single trade
    private static decimal _dailyLossLimit = 50.00m;   // Auto-pause buying if daily losses exceed this

    // Market discovery filter
    private const decimal MIN_VOLUME = 50_000m;

    // --- CONTROLS ---
    private static volatile bool _isPaused = false;
    private static volatile bool _isMuted = false;      // M: mute trade logs only
    private static volatile bool _quietMode = false;     // Q: silence everything
    private static volatile bool _verboseMode = false;   // V: show full book update details instead of dots
    private static volatile bool _tradeMuted = false;    // T: mute/unmute trade-related logs only
    private static volatile bool _debugGap = false;      // G: show crash gap calculations
    private static volatile bool _buyingPaused = false;
    private static volatile bool _dailyLossTriggered = false;
    private static CancellationTokenSource _pauseCts = new();
    private static readonly string _sessionCsvFilename = $"LiveProduction_{DateTime.Now:yyyyMMdd_HHmmss}.csv";

    // Daily loss tracking
    private static decimal _dayStartEquity;
    private static DateTime _currentDay = DateTime.UtcNow.Date;

    private static ProductionBroker _broker = null!;
    private static System.Collections.Concurrent.ConcurrentDictionary<string, string> _tokenNames = new();
    private static System.Collections.Concurrent.ConcurrentDictionary<string, bool> _tokenNegRisk = new();
    private static System.Collections.Concurrent.ConcurrentDictionary<string, string> _tokenTickSize = new();
    private static System.Collections.Concurrent.ConcurrentDictionary<string, decimal> _tokenMinSize = new();
    private static System.Collections.Concurrent.ConcurrentDictionary<string, bool> _subscribedTokens = new();
    private static System.Collections.Concurrent.ConcurrentDictionary<string, string> _tokenConditionIds = new(); // tokenId -> conditionId
    private static System.Collections.Concurrent.ConcurrentDictionary<string, LocalOrderBook> _orderBooks = new();
    private static PolymarketUserStreamClient? _userStream;
    private static ClientWebSocket? _activeWs;
    private static readonly SemaphoreSlim _wsSendSemaphore = new(1, 1);

    static async Task Main(string[] args)
    {
        // ==========================================
        // 1. LOGGING SETUP
        // ==========================================
        Log.Logger = new LoggerConfiguration()
            .MinimumLevel.Information()
            .WriteTo.Console(outputTemplate: "[{Timestamp:HH:mm:ss.fff}] [{Level:u3}] {Message:lj}{NewLine}{Exception}")
            .WriteTo.File($"logs/production_{DateTime.Now:yyyyMMdd}.log", rollingInterval: RollingInterval.Day)
            .CreateLogger();

        // ==========================================
        // 2. CREDENTIAL LOADING (Environment Variables)
        // ==========================================
        var config = LoadApiConfig();
        if (config == null) return;

        // ==========================================
        // 3. SAFETY CONFIRMATION
        // ==========================================
        bool headless = args.Contains("--no-confirm");

        if (!headless)
        {
            Console.ForegroundColor = ConsoleColor.Red;
            Console.WriteLine("╔══════════════════════════════════════════════════╗");
            Console.WriteLine("║    ⚠  LIVE PRODUCTION TRADING ENGINE  ⚠        ║");
            Console.WriteLine("║                                                  ║");
            Console.WriteLine("║  This will place REAL orders with REAL money     ║");
            Console.WriteLine("║  on Polymarket using your connected wallet.      ║");
            Console.WriteLine("╚══════════════════════════════════════════════════╝");
            Console.ResetColor();
            Console.Write("\nType 'YES' to confirm: ");
            string? confirmation = Console.ReadLine()?.Trim();
            if (confirmation != "YES")
            {
                Console.WriteLine("Aborted. No trades will be placed.");
                return;
            }
        }
        else
        {
            Log.Information("Headless mode (--no-confirm). Skipping confirmation prompt.");
        }

        // ==========================================
        // 4. BROKER & STRATEGY INITIALIZATION
        // ==========================================
        if (!headless) Console.Clear();
        Log.Information("Initializing live broker...");

        _broker = await ProductionBroker.CreateAsync(STRATEGY_NAME, config, _tokenNames, _tokenNegRisk, _tokenTickSize, _tokenMinSize, _maxBetSize);
        _dayStartEquity = _broker.CashBalance;
        Log.Information("Wallet balance: ${Balance:0.00} | Max bet: ${MaxBet:0.00} | Daily loss limit: ${DailyLimit:0.00}",
            _broker.CashBalance, _maxBetSize, _dailyLossLimit);

        var strategy = new LiveFlashCrashSniperStrategy(
            STRATEGY_NAME,
            CRASH_THRESHOLD,
            TIME_WINDOW_SECONDS,
            TAKE_PROFIT,
            STOP_LOSS,
            RISK_PERCENTAGE,
            ENTRY_SLIPPAGE,
            EXIT_SLIPPAGE,
            SUSTAIN_TIMER_MS,
            SETTLEMENT_LOCK_MS
        );

        Console.WriteLine("=========================================");
        Console.WriteLine("  LIVE PRODUCTION ENGINE INITIALIZED");
        Console.WriteLine($"  Strategy: {STRATEGY_NAME}");
        Console.WriteLine($"  Wallet:   ${_broker.CashBalance:0.00} USDC");
        Console.WriteLine($"  Params:   Crash={CRASH_THRESHOLD} Window={TIME_WINDOW_SECONDS}s TP={TAKE_PROFIT} SL={STOP_LOSS} Sustain={SUSTAIN_TIMER_MS}ms Lock={SETTLEMENT_LOCK_MS}ms");
        Console.WriteLine($"  Limits:   MaxBet=${_maxBetSize} DailyLoss=${_dailyLossLimit}");
        Console.WriteLine("  Controls: P=Pause R=Resume N=PauseBuying X=SellAll M=Mute T=Trades Q=Quiet V=Verbose F=Debug S=Status");
        Console.WriteLine("  Settings: 1=MaxBet 2=DailyLossLimit");
        Console.WriteLine("=========================================");

        // ==========================================
        // 5. GRACEFUL SHUTDOWN (CTRL+C + SIGTERM)
        // ==========================================
        void Shutdown()
        {
            Log.Warning("Graceful shutdown initiated...");
            _userStream?.Dispose();
            ExportLedgerToCsv(quietMode: false);
            _broker.SaveState(_subscribedTokens.Keys);
            PrintDashboard();
            Log.Information("Engine terminated.");
            Log.CloseAndFlush();
        }

        Console.CancelKeyPress += (_, e) =>
        {
            e.Cancel = true;
            Shutdown();
            Environment.Exit(0);
        };

        // SIGTERM from systemd — export ledger before dying
        AppDomain.CurrentDomain.ProcessExit += (_, _) =>
        {
            Shutdown();
        };

        // ==========================================
        // 6. KEYBOARD LISTENER (skipped in headless mode)
        // ==========================================
        if (!headless)
        {
            _ = Task.Run(() =>
            {
                while (true)
                {
                    var key = Console.ReadKey(intercept: true).Key;

                    switch (key)
                    {
                        case ConsoleKey.P when !_isPaused:
                            _isPaused = true;
                            _pauseCts.Cancel();
                            Log.Warning("PAUSED by user. Press R to resume.");
                            break;

                        case ConsoleKey.R when _isPaused:
                            _isPaused = false;
                            _pauseCts = new CancellationTokenSource();
                            Log.Information("RESUMED by user. Reconnecting...");
                            break;

                        case ConsoleKey.N:
                            _buyingPaused = !_buyingPaused;
                            Log.Warning("Buying {State}. Exits for open positions remain active.",
                                _buyingPaused ? "PAUSED" : "RESUMED");
                            break;

                        case ConsoleKey.X:
                            SellAllPositions();
                            break;

                        case ConsoleKey.M:
                            _isMuted = !_isMuted;
                            Log.Information("Book dots {State}.", _isMuted ? "MUTED" : "UNMUTED");
                            break;

                        case ConsoleKey.Q:
                            _quietMode = !_quietMode;
                            Log.Information("Quiet mode: {State}", _quietMode ? "ON (all output silenced)" : "OFF");
                            break;

                        case ConsoleKey.V:
                            _verboseMode = !_verboseMode;
                            Log.Information("Verbose mode: {State}", _verboseMode ? "ON (full book updates)" : "OFF (dots)");
                            break;

                        case ConsoleKey.T:
                            _tradeMuted = !_tradeMuted;
                            _broker.IsMuted = _tradeMuted;
                            Log.Information("Trade logs: {State}", _tradeMuted ? "MUTED" : "VISIBLE");
                            break;

                        case ConsoleKey.G:
                            _debugGap = !_debugGap;
                            Log.Information("Gap debug: {State}", _debugGap ? "ON" : "OFF");
                            break;

                        case ConsoleKey.F:
                            _broker.OrderDebugMode = !_broker.OrderDebugMode;
                            Log.Information("Order debug (payload + EIP712): {State}", _broker.OrderDebugMode ? "ON" : "OFF");
                            break;

                        case ConsoleKey.S:
                            PrintDashboard();
                            break;

                        case ConsoleKey.D1: // '1' key — edit max bet size
                            Console.Write("\n[SETTINGS] New max bet size (current: $" + _maxBetSize.ToString("0.00") + "): $");
                            if (decimal.TryParse(Console.ReadLine()?.Trim(), out decimal newBet) && newBet > 0)
                            {
                                _maxBetSize = newBet;
                                _broker.MaxBetSize = newBet;
                                Log.Information("Max bet size updated to ${MaxBet:0.00}", newBet);
                            }
                            else Log.Warning("Invalid input. Max bet size unchanged.");
                            break;

                        case ConsoleKey.D2: // '2' key — edit daily loss limit
                            Console.Write("\n[SETTINGS] New daily loss limit (current: $" + _dailyLossLimit.ToString("0.00") + "): $");
                            if (decimal.TryParse(Console.ReadLine()?.Trim(), out decimal newLimit) && newLimit > 0)
                            {
                                _dailyLossLimit = newLimit;
                                // If we were locked out but the new limit is higher, re-check
                                if (_dailyLossTriggered)
                                {
                                    decimal dailyPnl = _broker.GetTotalPortfolioValue() - _dayStartEquity;
                                    if (dailyPnl > -newLimit)
                                    {
                                        _dailyLossTriggered = false;
                                        Log.Information("Daily loss no longer exceeded with new limit. Buying re-enabled.");
                                    }
                                }
                                Log.Information("Daily loss limit updated to ${Limit:0.00}", newLimit);
                            }
                            else Log.Warning("Invalid input. Daily loss limit unchanged.");
                            break;
                    }
                }
            });
        }

        // ==========================================
        // 7. MARKET DISCOVERY
        // ==========================================
        Log.Information("Setting up API client...");
        var services = new ServiceCollection();
        services.AddHttpClient("PolymarketGamma", client => { client.BaseAddress = new Uri("https://gamma-api.polymarket.com/"); });
        services.AddHttpClient("PolymarketClob", client => { client.BaseAddress = new Uri("https://clob.polymarket.com/"); });
        services.AddHttpClient("PolymarketData", client => { client.BaseAddress = new Uri("https://data-api.polymarket.com/"); });
        services.AddTransient<PolymarketClient>();

        var serviceProvider = services.BuildServiceProvider();
        var apiClient = serviceProvider.GetRequiredService<PolymarketClient>();

        Log.Information("Fetching active markets...");
        await DiscoverNewMarkets(apiClient);
        Log.Information("Monitoring {Count} outcome tokens.", _subscribedTokens.Count);

        // ==========================================
        // 7b. ON-CHAIN STATE SYNC (Startup)
        // ==========================================
        Log.Information("Loading local state and running startup on-chain sync...");
        
        // 1. Restore entry prices from memory
        _broker.LoadState();

        // THE FIX: Removed tokenIds and fullDiscovery params!
        // 2. Validate actual share counts against the blockchain
        await _broker.RunFullSyncAsync(_tokenNames);
        
        _dayStartEquity = _broker.GetTotalPortfolioValue();

        // ==========================================
        // 7c. USER STREAM — Real-time fill detection
        // ==========================================
        var uniqueConditionIds = _tokenConditionIds.Values.Distinct().ToList();
        _userStream = new PolymarketUserStreamClient(config, uniqueConditionIds);
        _userStream.OnTradeMatched += (fill) =>
        {
            // FAST PATH: If a polling loop is waiting on this asset, wake it up instantly
            // The polling loop will confirm via API and handle all the bookkeeping
            _broker.SignalFill(fill.TokenId);

            // FALLBACK: If polling already timed out (ghost order), reconcile directly
            bool reconciled = _broker.ReconcileGhostFill(fill);
            if (reconciled)
            {
                // Ghost was caught — save state immediately so we don't lose it on crash
                _broker.SaveState(_subscribedTokens.Keys);
            }
        };
        await _userStream.StartAsync();
        Log.Information("User stream started. Monitoring {Count} condition IDs for ghost fills.", uniqueConditionIds.Count);

        // ==========================================
        // 8. SETTLEMENT SWEEPER (Background)
        // ==========================================
        _ = Task.Run(async () =>
        {
            while (true)
            {
                await Task.Delay(TimeSpan.FromMinutes(15));
                if (_isPaused) continue;

                if (!_quietMode) PrintDashboard();
                ExportLedgerToCsv(quietMode: true);

                _broker.SaveState(_subscribedTokens.Keys);

                // Check for settled markets
                try
                {
                    for (int offset = 0; offset < 500; offset += 100)
                    {
                        var events = await apiClient.GetClosedEventsAsync(100, offset);
                        if (events == null || events.Count == 0) break;

                        foreach (var ev in events)
                        {
                            if (ev.Markets == null) continue;
                            foreach (var market in ev.Markets)
                            {
                                if (!market.IsClosed || market.ClobTokenIds == null || market.OutcomePrices == null) continue;
                                for (int i = 0; i < market.ClobTokenIds.Length; i++)
                                {
                                    string tokenId = market.ClobTokenIds[i];
                                    if (_broker.GetPositionShares(tokenId) > 0 || _broker.GetNoPositionShares(tokenId) > 0)
                                    {
                                        decimal finalPrice = 0m;
                                        if (i < market.OutcomePrices.Length && decimal.TryParse(market.OutcomePrices[i], out decimal price))
                                            finalPrice = price;

                                        _broker.ResolveMarket(tokenId, finalPrice);
                                        Log.Information("Market resolved: {Name} @ {Price}", _tokenNames.GetValueOrDefault(tokenId, tokenId), finalPrice);
                                    }
                                }
                            }
                        }
                    }
                }
                catch (Exception ex) { Log.Warning("Settlement sweep error: {Error}", ex.Message); }

                // Discover new markets
                try
                {
                    var newTokens = await DiscoverNewMarkets(apiClient);
                    if (newTokens.Count > 0)
                    {
                        Log.Information("Discovered {Count} new market(s). Subscribing...", newTokens.Count);
                        await SubscribeNewTokens(newTokens);

                        // Also subscribe new condition IDs to the user stream
                        var newConditionIds = newTokens
                            .Where(t => _tokenConditionIds.ContainsKey(t))
                            .Select(t => _tokenConditionIds[t])
                            .Distinct()
                            .ToList();
                        if (newConditionIds.Count > 0 && _userStream != null)
                            await _userStream.SubscribeNewMarketsAsync(newConditionIds);
                    }
                }
                catch (Exception ex) { Log.Warning("Market discovery error: {Error}", ex.Message); }

                // Purge ghost orders older than 15 minutes (they're truly dead)
                int purged = _broker.PurgeStaleGhostOrders(TimeSpan.FromMinutes(15));
                if (purged > 0) Log.Information("[GHOST] Purged {Count} stale ghost order(s).", purged);

                // Periodic on-chain state reconciliation
                try
                {
                    await _broker.RunFullSyncAsync(_tokenNames);
                }
                catch (Exception ex) { Log.Warning("State sync error: {Error}", ex.Message); }
            }
        });

        // ==========================================
        // 9. WEBSOCKET MAIN LOOP
        // ==========================================
        var strategies = new Dictionary<string, ILiveStrategy>();

        while (true)
        {
            if (_isPaused)
            {
                await Task.Delay(1000);
                continue;
            }

            // Wipe stale state on reconnect
            _orderBooks.Clear();
            strategies.Clear();

            try
            {
                using var ws = new ClientWebSocket();
                ws.Options.KeepAliveInterval = TimeSpan.FromSeconds(10);

                Log.Information("Connecting to Polymarket WebSocket...");
                await ws.ConnectAsync(new Uri("wss://ws-subscriptions-clob.polymarket.com/ws/market"), _pauseCts.Token);
                _activeWs = ws;
                Log.Information("Connected. Listening for live order book updates...");

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
                            using var timeoutCts = new CancellationTokenSource(staleTimeout);
                            using var linkedCts = CancellationTokenSource.CreateLinkedTokenSource(_pauseCts.Token, timeoutCts.Token);
                            try
                            {
                                result = await ws.ReceiveAsync(new ArraySegment<byte>(receiveBuffer), linkedCts.Token);
                            }
                            catch (OperationCanceledException) when (!_pauseCts.IsCancellationRequested)
                            {
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

                        string message = Encoding.UTF8.GetString(ms.GetBuffer(), 0, (int)ms.Length);
                        ms.SetLength(0);

                        try
                        {
                            using var doc = JsonDocument.Parse(message);
                            var root = doc.RootElement;

                            if (root.ValueKind != JsonValueKind.Object || !root.TryGetProperty("event_type", out var eventTypeEl))
                                continue;

                            string? eventType = eventTypeEl.GetString();

                            if (eventType == "book" && root.TryGetProperty("asset_id", out var assetIdEl))
                            {
                                string? assetId = assetIdEl.GetString();
                                if (string.IsNullOrEmpty(assetId)) continue;

                                if (!_orderBooks.ContainsKey(assetId))
                                {
                                    _orderBooks[assetId] = new LocalOrderBook(assetId);
                                    // Single strategy instance per asset — create a fresh one so each asset
                                    // gets its own sliding window of recent asks
                                    strategies[assetId] = new LiveFlashCrashSniperStrategy(
                                        STRATEGY_NAME, CRASH_THRESHOLD, TIME_WINDOW_SECONDS,
                                        TAKE_PROFIT, STOP_LOSS, RISK_PERCENTAGE,
                                        ENTRY_SLIPPAGE, EXIT_SLIPPAGE,
                                        SUSTAIN_TIMER_MS, SETTLEMENT_LOCK_MS);
                                }

                                if (root.TryGetProperty("bids", out var bidsEl) && root.TryGetProperty("asks", out var asksEl))
                                    _orderBooks[assetId].ProcessBookUpdate(bidsEl, asksEl);
                            }
                            else if (eventType == "price_change" && root.TryGetProperty("price_changes", out var changesEl))
                            {
                                foreach (var change in changesEl.EnumerateArray())
                                {
                                    if (!change.TryGetProperty("asset_id", out var idEl)) continue;
                                    string? assetId = idEl.GetString();
                                    if (string.IsNullOrEmpty(assetId) || !_orderBooks.ContainsKey(assetId)) continue;

                                    decimal price = decimal.Parse(change.GetProperty("price").GetString() ?? "0");
                                    decimal size = decimal.Parse(change.GetProperty("size").GetString() ?? "0");
                                    string side = change.GetProperty("side").GetString() ?? "";

                                    var book = _orderBooks[assetId];

                                    book.UpdatePriceLevel(side, price, size);

                                    if (!_quietMode)
                                    {
                                        if (_verboseMode)
                                        {
                                            lock (GlobalSimulatedBroker.ConsoleLock)
                                            {
                                                string name = _tokenNames.GetValueOrDefault(assetId, assetId[..Math.Min(8, assetId.Length)] + "...");
                                                Console.ForegroundColor = ConsoleColor.DarkGray;
                                                Console.WriteLine($"[{DateTime.Now:HH:mm:ss.fff}] [BOOK] {side} {size:0.00} @ {price:0.0000} | {name}");
                                                Console.ResetColor();
                                            }
                                        }
                                        else if (!_isMuted)
                                        {
                                            lock (GlobalSimulatedBroker.ConsoleLock)
                                            {
                                                Console.ForegroundColor = ConsoleColor.DarkGray;
                                                Console.Write(".");
                                                Console.ResetColor();
                                            }
                                        }
                                    }

                                    _broker.ResetConsumedLiquidity(assetId);

                                    // Daily loss check — reset at midnight UTC
                                    CheckDailyLossLimit();

                                    // Feed to the single strategy
                                    // When buying is paused (manually or by daily loss), only run the
                                    // strategy for assets where we hold a position (exits still fire)
                                    if (strategies.TryGetValue(assetId, out var strat))
                                    {
                                        bool hasPosition = _broker.GetPositionShares(assetId) > 0
                                                        || _broker.GetNoPositionShares(assetId) > 0;

                                        if ((!_buyingPaused && !_dailyLossTriggered) || hasPosition)
                                            strat.OnBookUpdate(book, _broker);

                                        if (_debugGap && !hasPosition && strat is LiveFlashCrashSniperStrategy sniper)
                                        {
                                            decimal bestAsk = book.GetBestAskPrice();
                                            decimal bestBid = book.GetBestBidPrice();
                                            decimal spread = bestAsk - bestBid;
                                            decimal askSize = book.GetBestAskSize();
                                            decimal bidSize = book.GetBestBidSize();
                                            decimal gap = sniper.GetMaxGap();
                                            string mktName = _tokenNames.GetValueOrDefault(assetId, assetId[..Math.Min(8, assetId.Length)] + "...");

                                            if (gap > 0.005m) // Only show when there's a meaningful gap
                                            {
                                                lock (GlobalSimulatedBroker.ConsoleLock)
                                                {
                                                    Console.ForegroundColor = gap >= CRASH_THRESHOLD ? ConsoleColor.Yellow : ConsoleColor.DarkGray;
                                                    Console.WriteLine($"[{DateTime.Now:HH:mm:ss.fff}] [GAP] {gap:0.0000} / {CRASH_THRESHOLD} | Ask:{bestAsk:0.00} Bid:{bestBid:0.00} Spread:{spread:0.00} AskSz:{askSize:0.0} BidSz:{bidSize:0.0} | {mktName}");
                                                    Console.ResetColor();
                                                }
                                            }
                                        }
                                    }
                                }
                            }
                        }
                        catch (Exception ex)
                        {
                            Log.Debug("WebSocket parse error: {Error}", ex.Message);
                        }
                    }
                }, _pauseCts.Token);

                // Subscribe in chunks
                int chunkSize = 50;
                bool isFirstChunk = true;
                var tokenList = _subscribedTokens.Keys.ToList();

                for (int i = 0; i < tokenList.Count; i += chunkSize)
                {
                    if (_pauseCts.IsCancellationRequested) break;

                    var chunk = tokenList.Skip(i).Take(chunkSize);
                    string assetListString = string.Join("\",\"", chunk);

                    string subscribeMessage = isFirstChunk
                        ? $"{{\"assets_ids\":[\"{assetListString}\"],\"type\":\"market\"}}"
                        : $"{{\"assets_ids\":[\"{assetListString}\"],\"operation\":\"subscribe\"}}";

                    isFirstChunk = false;

                    var bytes = Encoding.UTF8.GetBytes(subscribeMessage);
                    await ws.SendAsync(new ArraySegment<byte>(bytes), WebSocketMessageType.Text, true, _pauseCts.Token);
                    await Task.Delay(500);
                }

                await listenTask;
            }
            catch (OperationCanceledException)
            {
                _activeWs = null; // User paused
            }
            catch (Exception ex)
            {
                _activeWs = null;
                Log.Warning("Connection lost: {Error}. Reconnecting in 5s...", ex.Message);
                await Task.Delay(5000);
            }
        }
    }

    // ==========================================
    // CREDENTIAL LOADING
    // ==========================================
    private static PolymarketApiConfig? LoadApiConfig()
    {
        string[] required = ["POLY_API_KEY", "POLY_API_SECRET", "POLY_API_PASSPHRASE", "POLY_PRIVATE_KEY", "POLY_PROXY_ADDRESS"];
        var missing = required.Where(k => string.IsNullOrEmpty(Environment.GetEnvironmentVariable(k))).ToList();

        if (missing.Count > 0)
        {
            Console.ForegroundColor = ConsoleColor.Red;
            Console.WriteLine("Missing required environment variables:");
            foreach (var m in missing)
                Console.WriteLine($"  - {m}");
            Console.ResetColor();
            return null;
        }

        return new PolymarketApiConfig
        {
            ApiKey = Environment.GetEnvironmentVariable("POLY_API_KEY")!.Trim(),
            ApiSecret = Environment.GetEnvironmentVariable("POLY_API_SECRET")!.Trim(),
            ApiPassphrase = Environment.GetEnvironmentVariable("POLY_API_PASSPHRASE")!.Trim(),
            PrivateKey = Environment.GetEnvironmentVariable("POLY_PRIVATE_KEY")!.Trim(),
            ProxyAddress = Environment.GetEnvironmentVariable("POLY_PROXY_ADDRESS")!.Trim(),
            RpcUrl = (Environment.GetEnvironmentVariable("POLY_RPC_URL") ?? "https://polygon-rpc.com").Trim()
        };
    }

    // ==========================================
    // MARKET DISCOVERY
    // ==========================================
    private static async Task<List<string>> DiscoverNewMarkets(PolymarketClient apiClient)
    {
        var newlyDiscovered = new List<string>();

        for (int offset = 0; ; offset += 100)
        {
            var events = await apiClient.GetActiveEventsAsync(100, offset);
            if (events == null || events.Count == 0) break;

            foreach (var ev in events)
            {
                if (ev.Markets == null) continue;
                foreach (var market in ev.Markets)
                {
                    if (market.StartDate.HasValue && market.StartDate.Value > DateTime.UtcNow) continue;
                    if (market.Volume < MIN_VOLUME) continue;
                    if (market.IsClosed || market.ClobTokenIds == null || market.ClobTokenIds.Length == 0) continue;

                    string yesToken = market.ClobTokenIds[0];
                    if (_subscribedTokens.TryAdd(yesToken, true))
                    {
                        _tokenNames.TryAdd(yesToken, market.Question);
                        _tokenNegRisk.TryAdd(yesToken, ev.NegRisk);
                        string tickSize = market.OrderPriceMinTickSize > 0
                            ? market.OrderPriceMinTickSize.ToString("G")
                            : "0.01";
                        _tokenTickSize.TryAdd(yesToken, tickSize);
                        _tokenMinSize.TryAdd(yesToken, market.OrderMinSize > 0 ? market.OrderMinSize : 1.00m);
                        if (!string.IsNullOrEmpty(market.ConditionId))
                            _tokenConditionIds.TryAdd(yesToken, market.ConditionId);

                        // Fetch fees upfront, but strictly throttle to 10 requests/sec to prevent IP bans
                        if (_broker != null)
                        {
                            try
                            {
                                int feeRate = await _broker.OrderClient.GetTakerFeeAsync(yesToken);
                                _broker.SetTokenFeeRate(yesToken, feeRate);
                                
                                // Sleep for 100ms. If there are 1,000 markets, startup takes ~1.5 minutes.
                                await Task.Delay(100); 
                            }
                            catch (Exception ex)
                            {
                                Log.Warning("Failed to fetch fee upfront for {Token}: {Error}", yesToken, ex.Message);
                            }
                        }

                        newlyDiscovered.Add(yesToken);
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

        for (int i = 0; i < newTokens.Count; i += 50)
        {
            var chunk = newTokens.Skip(i).Take(50);
            string assetListString = string.Join("\",\"", chunk);
            string msg = $"{{\"assets_ids\":[\"{assetListString}\"],\"operation\":\"subscribe\"}}";

            var bytes = Encoding.UTF8.GetBytes(msg);
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
    // DAILY LOSS LIMIT
    // ==========================================
    private static void CheckDailyLossLimit()
    {
        // Reset at midnight UTC — new trading day
        DateTime today = DateTime.UtcNow.Date;
        if (today != _currentDay)
        {
            _currentDay = today;
            _dayStartEquity = _broker.GetTotalPortfolioValue();
            if (_dailyLossTriggered)
            {
                _dailyLossTriggered = false;
                Log.Information("New trading day. Daily loss limit reset. Buying re-enabled.");
            }
            return;
        }

        if (_dailyLossTriggered) return; // Already triggered today

        decimal currentEquity = _broker.GetTotalPortfolioValue();
        decimal dailyPnl = currentEquity - _dayStartEquity;

        if (dailyPnl <= -_dailyLossLimit)
        {
            _dailyLossTriggered = true;
            Log.Error("DAILY LOSS LIMIT HIT: ${DailyPnl:0.00} (limit: -${Limit:0.00}). Buying auto-paused until midnight UTC. Exits remain active.",
                dailyPnl, _dailyLossLimit);
        }
    }

    // ==========================================
    // SELL ALL OPEN POSITIONS
    // ==========================================
    private static void SellAllPositions()
    {
        // Pause buying first so no new entries while we liquidate
        _buyingPaused = true;
        Log.Warning("SELL ALL triggered. Buying paused. Liquidating all open positions...");

        int sold = 0;
        foreach (var kvp in _orderBooks)
        {
            string assetId = kvp.Key;
            var book = kvp.Value;

            decimal yesShares = _broker.GetPositionShares(assetId);
            if (yesShares > 0)
            {
                decimal bestBid = book.GetBestBidPrice();
                if (bestBid > 0.01m && bestBid < 0.99m)
                {
                    decimal limitPrice = Math.Max(bestBid - 0.05m, 0.001m);
                    _broker.SubmitSellAllOrder(assetId, limitPrice, book);
                    sold++;
                }
            }

            // NO positions would use SellAllNo on the base broker, but PolymarketLiveBroker
            // only overrides SubmitSellAllOrder (YES side). If you hold NO positions, they'll
            // be resolved at settlement. For now, log them.
            decimal noShares = _broker.GetNoPositionShares(assetId);
            if (noShares > 0)
            {
                string name = _tokenNames.GetValueOrDefault(assetId, assetId[..8] + "...");
                Log.Warning("Cannot sell NO position via CLOB: {Name} ({Shares} shares). Will resolve at settlement.", name, noShares);
            }
        }

        Log.Warning("Submitted {Count} SELL orders. Buying remains paused — press B to resume.", sold);
    }

    // ==========================================
    // PERFORMANCE DASHBOARD
    // ==========================================
    private static void PrintDashboard()
    {
        decimal cash = _broker.CashBalance;
        decimal mtmValue = 0m;

        // Calculate explicit MTM by checking the live order book's best bid!
        foreach (var assetId in _subscribedTokens.Keys)
        {
            decimal shares = _broker.GetPositionShares(assetId);
            if (shares > 0 && _orderBooks.TryGetValue(assetId, out var book))
            {
                decimal bestBid = book.GetBestBidPrice();
                
                // If the book is completely empty, fallback to our entry price to prevent MTM from flashing $0
                if (bestBid <= 0.00m) bestBid = _broker.GetAverageEntryPrice(assetId); 
                
                mtmValue += (shares * bestBid);
            }
        }

        decimal totalEquity = cash + mtmValue;
        decimal dailyPnl = totalEquity - _dayStartEquity;

        lock (GlobalSimulatedBroker.ConsoleLock)
        {
            Console.WriteLine();
            Console.WriteLine("================= PRODUCTION DASHBOARD =================");
            Console.ForegroundColor = totalEquity >= _dayStartEquity ? ConsoleColor.Green : ConsoleColor.Red;
            Console.WriteLine($"  Equity:     ${totalEquity:0.00}");
            Console.WriteLine($"  Cash:       ${cash:0.00}");
            Console.WriteLine($"  MTM Value:  ${mtmValue:0.00}");
            Console.WriteLine($"  Daily PnL:  ${dailyPnl:0.00} (limit: -${_dailyLossLimit:0.00})");
            Console.WriteLine($"  Actions:    {_broker.TotalActions}");
            Console.WriteLine($"  Exits:      {_broker.TotalTradesExecuted} (W:{_broker.WinningTrades} L:{_broker.LosingTrades})");
            Console.WriteLine($"  Rejected:   {_broker.RejectedOrders}");
            Console.WriteLine($"  Missed:     {_broker.MissedBuys} buys / {_broker.MissedSells} sells (FAK killed)");
            Console.WriteLine($"  Ghosts:     {_broker.GhostOrderCount} pending (watching via UserStream)");
            Console.ResetColor();

            // Status flags
            string status = _dailyLossTriggered ? "DAILY LOSS LIMIT" : _buyingPaused ? "BUYING PAUSED" : "ACTIVE";
            Console.ForegroundColor = status == "ACTIVE" ? ConsoleColor.Green : ConsoleColor.Yellow;
            Console.WriteLine($"  Status:     {status}");
            Console.ResetColor();
            Console.WriteLine("=========================================================");
        }
    }

    // ==========================================
    // LEDGER EXPORT
    // ==========================================
    private static void ExportLedgerToCsv(bool quietMode = false)
    {
        try
        {
            List<ExecutedTrade> ledgerSnapshot;
            lock (_broker.BrokerLock)
            {
                ledgerSnapshot = new List<ExecutedTrade>(_broker.TradeLedger);
            }

            using var writer = new StreamWriter(_sessionCsvFilename);
            writer.WriteLine("Timestamp,StrategyName,MarketName,AssetId,Side,ExecutionPrice,Shares,DollarValue");

            foreach (var trade in ledgerSnapshot)
            {
                string marketName = _tokenNames.GetValueOrDefault(trade.OutcomeId, "Unknown");
                marketName = $"\"{marketName.Replace("\"", "\"\"")}\"";
                writer.WriteLine($"{trade.Date:O},{STRATEGY_NAME},{marketName},{trade.OutcomeId},{trade.Side},{trade.Price},{trade.Shares},{trade.DollarValue}");
            }

            if (!quietMode)
                Log.Information("{Count} trades exported to {File}", ledgerSnapshot.Count, _sessionCsvFilename);
        }
        catch (Exception ex)
        {
            Log.Error("Failed to export ledger: {Error}", ex.Message);
        }
    }
}