using System.Collections.Concurrent;
using System.Net.WebSockets;
using System.Text;
using System.Text.Json;
using KalshiPaperTrader;
using PredictionBacktester.Engine;
using PredictionBacktester.Engine.LiveExecution;
using PredictionBacktester.Strategies;

// ══════════════════════════════════════════════════════════════════════════════
//  CONFIGURATION
// ══════════════════════════════════════════════════════════════════════════════
const decimal STARTING_CAPITAL      = 1_000.00m;
const decimal MAX_INVESTMENT        = 50.00m;
const decimal MIN_PROFIT_PER_SET    = 0.02m;
const decimal DEPTH_FLOOR_SHARES    = 5m;
const decimal SLIPPAGE_CENTS        = 0.02m;
const double  FEE_RATE              = 0.0;   // Pending Kalshi fee schedule confirmation
const long    REQUIRED_SUSTAIN_MS   = 0;
const long    POST_BUY_COOLDOWN_MS  = 60_000;

// Subscription batch size — Kalshi accepts arrays of market_tickers
const int SUBSCRIBE_BATCH_SIZE = 100;

// ══════════════════════════════════════════════════════════════════════════════
//  STARTUP
// ══════════════════════════════════════════════════════════════════════════════
Console.WriteLine("═══════════════════════════════════════════════════════════");
Console.WriteLine("  KALSHI PAPER TRADER — Categorical Arb Strategy");
Console.WriteLine("═══════════════════════════════════════════════════════════");

var config = KalshiApiConfig.FromEnvironment();
if (string.IsNullOrEmpty(config.ApiKeyId) || string.IsNullOrEmpty(config.PrivateKeyPath))
{
    Console.WriteLine("[ERROR] Set KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH environment variables.");
    return;
}

using var orderClient = new KalshiOrderClient(config);

// Check balance to confirm auth works
try
{
    long balanceCents = await orderClient.GetBalanceCentsAsync();
    Console.WriteLine($"[AUTH OK] Kalshi balance: ${balanceCents / 100.0:0.00}");
}
catch (Exception ex)
{
    Console.WriteLine($"[AUTH FAIL] Could not fetch balance: {ex.Message}");
    Console.WriteLine("Check your API key ID and private key file, then retry.");
    return;
}

// Scan for arb events
var scanner = new KalshiMarketScanner(orderClient);
Dictionary<string, List<string>> arbEvents = await scanner.GetArbitrageEventsAsync();

if (arbEvents.Count == 0)
{
    Console.WriteLine("[SCANNER] No eligible arb events found. Exiting.");
    return;
}

int totalTickers = arbEvents.Values.Sum(v => v.Count);
Console.WriteLine($"[SCANNER] {arbEvents.Count} events, {totalTickers} market tickers to subscribe.");

// Create order books (one per ticker)
var orderBooks = new ConcurrentDictionary<string, LocalOrderBook>();
foreach (var ticker in arbEvents.Values.SelectMany(v => v))
    orderBooks[ticker] = new LocalOrderBook(ticker);

// Create broker and strategy
var broker = new KalshiPaperBroker("Kalshi_CategoricalArb", STARTING_CAPITAL, scanner.TokenNames);

var strategy = new PolymarketCategoricalArbStrategy(
    configuredEvents:       arbEvents,
    name:                   "Kalshi_CategoricalArb",
    maxInvestmentPerTrade:  MAX_INVESTMENT,
    slippageCents:          SLIPPAGE_CENTS,
    feeRate:                FEE_RATE,
    feeExponent:            1.0,
    requiredSustainMs:      REQUIRED_SUSTAIN_MS,
    minProfitPerSet:        MIN_PROFIT_PER_SET,
    depthFloorShares:       DEPTH_FLOOR_SHARES,
    postBuyCooldownMs:      POST_BUY_COOLDOWN_MS
);

// Per-ticker size tracking for delta accumulation
// Key: ticker → {price → currentSize}  (separate for yes and no sides)
var yesSizes = new ConcurrentDictionary<string, Dictionary<decimal, decimal>>();
var noSizes  = new ConcurrentDictionary<string, Dictionary<decimal, decimal>>();
foreach (var ticker in arbEvents.Values.SelectMany(v => v))
{
    yesSizes[ticker] = new Dictionary<decimal, decimal>();
    noSizes[ticker]  = new Dictionary<decimal, decimal>();
}

// CTS for clean shutdown
using var cts = new CancellationTokenSource();
Console.CancelKeyPress += (_, e) => { e.Cancel = true; cts.Cancel(); };

// P&L status ticker
_ = Task.Run(async () =>
{
    while (!cts.Token.IsCancellationRequested)
    {
        await Task.Delay(30_000, cts.Token).ContinueWith(_ => { });
        if (cts.Token.IsCancellationRequested) break;
        lock (GlobalSimulatedBroker.ConsoleLock)
        {
            Console.WriteLine($"\n[STATUS] Cash: ${broker.CashBalance:0.00} | " +
                              $"Realized P&L: ${strategy.TotalRealizedPnL:0.00} | " +
                              $"Sell-backs: {strategy.CompletedSellBacks}");
        }
    }
});

// ══════════════════════════════════════════════════════════════════════════════
//  WEBSOCKET LOOP (with reconnect)
// ══════════════════════════════════════════════════════════════════════════════
var allTickers = arbEvents.Values.SelectMany(v => v).ToList();

while (!cts.Token.IsCancellationRequested)
{
    try
    {
        using var ws = new ClientWebSocket();

        // Kalshi requires auth headers on the HTTP upgrade request
        var (key, ts, sig) = orderClient.CreateAuthHeaders("GET", "/trade-api/ws/v2");
        ws.Options.SetRequestHeader("KALSHI-ACCESS-KEY", key);
        ws.Options.SetRequestHeader("KALSHI-ACCESS-TIMESTAMP", ts);
        ws.Options.SetRequestHeader("KALSHI-ACCESS-SIGNATURE", sig);

        await ws.ConnectAsync(new Uri(config.BaseWsUrl), cts.Token);
        Console.WriteLine($"\n[WS] Connected to {config.BaseWsUrl}");

        // Subscribe to orderbook_delta for all tickers in batches
        int msgId = 1;
        for (int i = 0; i < allTickers.Count; i += SUBSCRIBE_BATCH_SIZE)
        {
            var batch = allTickers.Skip(i).Take(SUBSCRIBE_BATCH_SIZE).ToList();
            string tickerArray = string.Join(",", batch.Select(t => $"\"{t}\""));
            string subMsg = $"{{\"id\":{msgId++},\"cmd\":\"subscribe\",\"params\":{{\"channels\":[\"orderbook_delta\"],\"market_tickers\":[{tickerArray}]}}}}";

            byte[] subBytes = Encoding.UTF8.GetBytes(subMsg);
            await ws.SendAsync(new ArraySegment<byte>(subBytes), WebSocketMessageType.Text, true, cts.Token);
            await Task.Delay(100, cts.Token); // Polite pacing between batches
        }

        Console.WriteLine($"[WS] Subscribed to {allTickers.Count} tickers across {arbEvents.Count} events.");

        // Clear stale book state on reconnect
        foreach (var book in orderBooks.Values) book.ClearBook();
        foreach (var d in yesSizes.Values) d.Clear();
        foreach (var d in noSizes.Values)  d.Clear();
        strategy.OnReconnect();

        // Message receive loop
        var receiveBuffer = new byte[65536];
        using var ms = new MemoryStream();

        while (!cts.Token.IsCancellationRequested && ws.State == WebSocketState.Open)
        {
            ms.SetLength(0);
            WebSocketReceiveResult result;

            // Accumulate frames until end of message
            do
            {
                result = await ws.ReceiveAsync(new ArraySegment<byte>(receiveBuffer), cts.Token);

                if (result.MessageType == WebSocketMessageType.Close)
                {
                    await ws.CloseAsync(WebSocketCloseStatus.NormalClosure, "", CancellationToken.None);
                    goto reconnect;
                }

                // Kalshi sends Ping control frames with body "heartbeat" — .NET handles Pong automatically
                // but may also send a text "heartbeat" frame — skip it
                ms.Write(receiveBuffer, 0, result.Count);
            }
            while (!result.EndOfMessage);

            if (ms.Length == 0) continue;

            string message = Encoding.UTF8.GetString(ms.GetBuffer(), 0, (int)ms.Length);

            if (message is "heartbeat" or "PONG" or "pong") continue;

            ProcessMessage(message, orderBooks, yesSizes, noSizes, strategy, broker);
        }

        reconnect:;
    }
    catch (OperationCanceledException) { break; }
    catch (Exception ex)
    {
        Console.WriteLine($"[WS ERROR] {ex.Message} — reconnecting in 5s...");
    }

    if (!cts.Token.IsCancellationRequested)
        await Task.Delay(5_000, cts.Token).ContinueWith(_ => { });
}

Console.WriteLine("\n[SHUTDOWN] Final P&L summary:");
Console.WriteLine($"  Cash balance:   ${broker.CashBalance:0.00}");
Console.WriteLine($"  Realized P&L:   ${strategy.TotalRealizedPnL:0.00}");
Console.WriteLine($"  Completed arbs: {strategy.CompletedSellBacks}");
Console.WriteLine($"  Total trades:   {broker.TotalTradesExecuted}");

// ══════════════════════════════════════════════════════════════════════════════
//  MESSAGE PROCESSING
// ══════════════════════════════════════════════════════════════════════════════
static void ProcessMessage(
    string message,
    ConcurrentDictionary<string, LocalOrderBook> orderBooks,
    ConcurrentDictionary<string, Dictionary<decimal, decimal>> yesSizes,
    ConcurrentDictionary<string, Dictionary<decimal, decimal>> noSizes,
    PolymarketCategoricalArbStrategy strategy,
    KalshiPaperBroker broker)
{
    try
    {
        using var doc = JsonDocument.Parse(message);
        var root = doc.RootElement;

        if (!root.TryGetProperty("type", out var typeEl)) return;
        string msgType = typeEl.GetString() ?? "";

        if (!root.TryGetProperty("msg", out var msgEl)) return;
        if (!msgEl.TryGetProperty("market_ticker", out var tickerEl)) return;
        string ticker = tickerEl.GetString() ?? "";

        if (!orderBooks.TryGetValue(ticker, out var book)) return;

        if (msgType == "orderbook_snapshot")
        {
            ApplySnapshot(book, msgEl, yesSizes[ticker], noSizes[ticker]);
            strategy.OnBookUpdate(book, broker);
        }
        else if (msgType == "orderbook_delta")
        {
            ApplyDelta(book, msgEl, yesSizes[ticker], noSizes[ticker]);
            strategy.OnBookUpdate(book, broker);
        }
    }
    catch (JsonException)
    {
        // Malformed message — skip silently
    }
}

// Applies a full snapshot to the LocalOrderBook and size-tracking dicts
static void ApplySnapshot(
    LocalOrderBook book,
    JsonElement msg,
    Dictionary<decimal, decimal> yesSizeMap,
    Dictionary<decimal, decimal> noSizeMap)
{
    book.ClearBook();
    yesSizeMap.Clear();
    noSizeMap.Clear();

    // YES offers → ask levels (you can BUY YES at these prices)
    if (msg.TryGetProperty("yes_dollars_fp", out var yesEl) && yesEl.ValueKind == JsonValueKind.Array)
    {
        foreach (var level in yesEl.EnumerateArray())
        {
            if (!TryParseLevel(level, out decimal price, out decimal size)) continue;
            yesSizeMap[price] = size;
            book.UpdatePriceLevel("SELL", price, size);
        }
    }

    // NO offers → YES bid levels (if NO is offered at $0.46, that implies a YES bid at $0.54)
    if (msg.TryGetProperty("no_dollars_fp", out var noEl) && noEl.ValueKind == JsonValueKind.Array)
    {
        foreach (var level in noEl.EnumerateArray())
        {
            if (!TryParseLevel(level, out decimal noPrice, out decimal size)) continue;
            noSizeMap[noPrice] = size;
            decimal yesBidPrice = Math.Round(1m - noPrice, 4);
            book.UpdatePriceLevel("BUY", yesBidPrice, size);
        }
    }
}

// Applies an incremental delta to the LocalOrderBook
static void ApplyDelta(
    LocalOrderBook book,
    JsonElement msg,
    Dictionary<decimal, decimal> yesSizeMap,
    Dictionary<decimal, decimal> noSizeMap)
{
    if (!msg.TryGetProperty("price_dollars", out var priceEl)) return;
    if (!msg.TryGetProperty("delta_fp",      out var deltaEl)) return;
    if (!msg.TryGetProperty("side",          out var sideEl))  return;

    if (!decimal.TryParse(priceEl.GetString(), out decimal price)) return;
    if (!decimal.TryParse(deltaEl.GetString(), out decimal delta)) return;
    string side = sideEl.GetString() ?? "";

    if (side == "yes")
    {
        decimal current = yesSizeMap.GetValueOrDefault(price, 0m);
        decimal newSize = current + delta;
        if (newSize <= 0)
        {
            yesSizeMap.Remove(price);
            book.UpdatePriceLevel("SELL", price, 0m); // size=0 removes the level
        }
        else
        {
            yesSizeMap[price] = newSize;
            book.UpdatePriceLevel("SELL", price, newSize);
        }
    }
    else if (side == "no")
    {
        decimal current = noSizeMap.GetValueOrDefault(price, 0m);
        decimal newSize = current + delta;
        decimal yesBidPrice = Math.Round(1m - price, 4);

        if (newSize <= 0)
        {
            noSizeMap.Remove(price);
            book.UpdatePriceLevel("BUY", yesBidPrice, 0m);
        }
        else
        {
            noSizeMap[price] = newSize;
            book.UpdatePriceLevel("BUY", yesBidPrice, newSize);
        }
    }
}

// Parses a [price_string, size_string] array element
static bool TryParseLevel(JsonElement level, out decimal price, out decimal size)
{
    price = 0; size = 0;
    if (level.ValueKind != JsonValueKind.Array) return false;

    var arr = level.EnumerateArray().ToArray();
    if (arr.Length < 2) return false;

    return decimal.TryParse(arr[0].GetString(), out price) &&
           decimal.TryParse(arr[1].GetString(), out size);
}
