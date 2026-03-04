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

    // Strategy parameters — tune these based on paper trading results
    private const decimal CRASH_THRESHOLD = 0.25m;
    private const long TIME_WINDOW_SECONDS = 60;
    private const decimal TAKE_PROFIT = 0.05m;
    private const decimal STOP_LOSS = 0.15m;
    private const decimal RISK_PERCENTAGE = 0.05m;
    private const decimal ENTRY_SLIPPAGE = 0.03m;
    private const decimal EXIT_SLIPPAGE = 0.02m;

    // Market discovery filter
    private const decimal MIN_VOLUME = 50_000m;

    // --- CONTROLS ---
    private static volatile bool _isPaused = false;
    private static volatile bool _isMuted = false;
    private static volatile bool _quietMode = false;
    private static CancellationTokenSource _pauseCts = new();
    private static readonly string _sessionCsvFilename = $"LiveProduction_{DateTime.Now:yyyyMMdd_HHmmss}.csv";

    private static PolymarketLiveBroker _broker = null!;
    private static Dictionary<string, string> _tokenNames = new();
    private static readonly HashSet<string> _subscribedTokens = new();
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
        Console.ForegroundColor = ConsoleColor.Red;
        Console.WriteLine("╔══════════════════════════════════════════════════╗");
        Console.WriteLine("║     ⚠  LIVE PRODUCTION TRADING ENGINE  ⚠       ║");
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

        // ==========================================
        // 4. BROKER & STRATEGY INITIALIZATION
        // ==========================================
        Console.Clear();
        Log.Information("Initializing live broker...");

        _broker = await PolymarketLiveBroker.CreateAsync(STRATEGY_NAME, config, _tokenNames);
        Log.Information("Wallet balance: ${Balance:0.00}", _broker.CashBalance);

        var strategy = new LiveFlashCrashSniperStrategy(
            STRATEGY_NAME,
            CRASH_THRESHOLD,
            TIME_WINDOW_SECONDS,
            TAKE_PROFIT,
            STOP_LOSS,
            RISK_PERCENTAGE,
            ENTRY_SLIPPAGE,
            EXIT_SLIPPAGE
        );

        Console.WriteLine("=========================================");
        Console.WriteLine("  LIVE PRODUCTION ENGINE INITIALIZED");
        Console.WriteLine($"  Strategy: {STRATEGY_NAME}");
        Console.WriteLine($"  Wallet:   ${_broker.CashBalance:0.00} USDC");
        Console.WriteLine($"  Params:   Crash={CRASH_THRESHOLD} Window={TIME_WINDOW_SECONDS}s TP={TAKE_PROFIT} SL={STOP_LOSS}");
        Console.WriteLine("  Controls: P=Pause R=Resume M=Mute Q=Quiet S=Status");
        Console.WriteLine("=========================================");

        // ==========================================
        // 5. GRACEFUL SHUTDOWN (CTRL+C)
        // ==========================================
        Console.CancelKeyPress += (_, e) =>
        {
            e.Cancel = true;
            Log.Warning("Graceful shutdown initiated...");
            ExportLedgerToCsv(quietMode: false);
            PrintDashboard();
            Log.Information("Engine terminated.");
            Log.CloseAndFlush();
            Environment.Exit(0);
        };

        // ==========================================
        // 6. KEYBOARD LISTENER
        // ==========================================
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

                    case ConsoleKey.M:
                        _isMuted = !_isMuted;
                        _broker.IsMuted = _isMuted;
                        Log.Information("Trade logs {State}.", _isMuted ? "MUTED" : "UNMUTED");
                        break;

                    case ConsoleKey.Q:
                        _quietMode = !_quietMode;
                        Log.Information("Quiet mode: {State}", _quietMode ? "ON" : "OFF");
                        break;

                    case ConsoleKey.S:
                        PrintDashboard();
                        break;
                }
            }
        });

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
                    }
                }
                catch (Exception ex) { Log.Warning("Market discovery error: {Error}", ex.Message); }
            }
        });

        // ==========================================
        // 9. WEBSOCKET MAIN LOOP
        // ==========================================
        var orderBooks = new Dictionary<string, LocalOrderBook>();
        var strategies = new Dictionary<string, ILiveStrategy>();

        while (true)
        {
            if (_isPaused)
            {
                await Task.Delay(1000);
                continue;
            }

            // Wipe stale state on reconnect
            orderBooks.Clear();
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

                                if (!orderBooks.ContainsKey(assetId))
                                {
                                    orderBooks[assetId] = new LocalOrderBook(assetId);
                                    // Single strategy instance per asset — create a fresh one so each asset
                                    // gets its own sliding window of recent asks
                                    strategies[assetId] = new LiveFlashCrashSniperStrategy(
                                        STRATEGY_NAME, CRASH_THRESHOLD, TIME_WINDOW_SECONDS,
                                        TAKE_PROFIT, STOP_LOSS, RISK_PERCENTAGE,
                                        ENTRY_SLIPPAGE, EXIT_SLIPPAGE);
                                }

                                if (root.TryGetProperty("bids", out var bidsEl) && root.TryGetProperty("asks", out var asksEl))
                                    orderBooks[assetId].ProcessBookUpdate(bidsEl, asksEl);
                            }
                            else if (eventType == "price_change" && root.TryGetProperty("price_changes", out var changesEl))
                            {
                                foreach (var change in changesEl.EnumerateArray())
                                {
                                    if (!change.TryGetProperty("asset_id", out var idEl)) continue;
                                    string? assetId = idEl.GetString();
                                    if (string.IsNullOrEmpty(assetId) || !orderBooks.ContainsKey(assetId)) continue;

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

                                    if (!_isMuted && !_quietMode)
                                    {
                                        lock (GlobalSimulatedBroker.ConsoleLock)
                                        {
                                            Console.ForegroundColor = ConsoleColor.DarkGray;
                                            Console.Write(".");
                                            Console.ResetColor();
                                        }
                                    }

                                    _broker.ResetConsumedLiquidity(assetId);

                                    // Feed to the single strategy
                                    if (strategies.TryGetValue(assetId, out var strat))
                                        strat.OnBookUpdate(book, _broker);
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
                var tokenList = _subscribedTokens.ToList();

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
            ApiKey = Environment.GetEnvironmentVariable("POLY_API_KEY")!,
            ApiSecret = Environment.GetEnvironmentVariable("POLY_API_SECRET")!,
            ApiPassphrase = Environment.GetEnvironmentVariable("POLY_API_PASSPHRASE")!,
            PrivateKey = Environment.GetEnvironmentVariable("POLY_PRIVATE_KEY")!,
            ProxyAddress = Environment.GetEnvironmentVariable("POLY_PROXY_ADDRESS")!,
            RpcUrl = Environment.GetEnvironmentVariable("POLY_RPC_URL") ?? "https://polygon-rpc.com"
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
                    if (_subscribedTokens.Add(yesToken))
                    {
                        _tokenNames.TryAdd(yesToken, market.Question);
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
    // PERFORMANCE DASHBOARD
    // ==========================================
    private static void PrintDashboard()
    {
        decimal totalEquity = _broker.GetTotalPortfolioValue();
        decimal pnl = totalEquity - _broker.CashBalance; // unrealized
        decimal realizedPnl = _broker.CashBalance - STARTING_CAPITAL;

        lock (GlobalSimulatedBroker.ConsoleLock)
        {
            Console.WriteLine();
            Console.WriteLine("================= PRODUCTION DASHBOARD =================");
            Console.ForegroundColor = totalEquity >= STARTING_CAPITAL ? ConsoleColor.Green : ConsoleColor.Red;
            Console.WriteLine($"  Equity:     ${totalEquity:0.00}");
            Console.WriteLine($"  Cash:       ${_broker.CashBalance:0.00}");
            Console.WriteLine($"  MTM Value:  ${pnl:0.00}");
            Console.WriteLine($"  Actions:    {_broker.TotalActions}");
            Console.WriteLine($"  Exits:      {_broker.TotalTradesExecuted} (W:{_broker.WinningTrades} L:{_broker.LosingTrades})");
            Console.WriteLine($"  Rejected:   {_broker.RejectedOrders}");
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
