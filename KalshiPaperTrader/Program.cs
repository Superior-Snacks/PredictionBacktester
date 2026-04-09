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
const double  FEE_EXPONENT          = 1.0;
const long    REQUIRED_SUSTAIN_MS   = 0;
const long    POST_BUY_COOLDOWN_MS  = 60_000;

const int  SUBSCRIBE_BATCH_SIZE     = 100;
const decimal MIN_VOLUME_24H        = 10m;   // Min contracts/day to include in binary scan

// ══════════════════════════════════════════════════════════════════════════════
//  STARTUP
// ══════════════════════════════════════════════════════════════════════════════
Console.WriteLine("═══════════════════════════════════════════════════════════");
Console.WriteLine("  KALSHI PAPER TRADER — Categorical + Binary Arb + Telemetry");
Console.WriteLine("═══════════════════════════════════════════════════════════");

var config = KalshiApiConfig.FromEnvironment();
if (string.IsNullOrEmpty(config.ApiKeyId) || string.IsNullOrEmpty(config.PrivateKeyPath))
{
    Console.WriteLine("[ERROR] Set KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH environment variables.");
    return;
}

using var orderClient = new KalshiOrderClient(config);

try
{
    long balanceCents = await orderClient.GetBalanceCentsAsync();
    Console.WriteLine($"[AUTH OK] Kalshi balance: ${balanceCents / 100.0:0.00}");
}
catch (Exception ex)
{
    Console.WriteLine($"[AUTH FAIL] {ex.Message}");
    Console.WriteLine("Check your API key ID and private key file, then retry.");
    return;
}

// ── SCAN ──────────────────────────────────────────────────────────────────────
var scanner = new KalshiMarketScanner(orderClient, MIN_VOLUME_24H);
KalshiScanResult scan = await scanner.ScanAllAsync();

int catCount = scan.CategoricalEvents.Count;
int binCount = scan.BinaryMarkets.Count;

if (catCount + binCount == 0)
{
    Console.WriteLine("[SCANNER] No eligible arb events found. Exiting.");
    return;
}

Console.WriteLine($"[SCANNER] Categorical: {catCount} events | Binary: {binCount} markets");

// Binary arb disabled: the implied-bid construction produces unrealistic
// sell-back prices until the book population logic is validated.
// TODO: re-enable once binary arb book symmetry is verified.
var allEvents = new Dictionary<string, List<string>>(scan.CategoricalEvents);

// Real tickers to subscribe to (strip _NO virtual IDs — they share a WS stream)
var realTickers = allEvents.Values
    .SelectMany(v => v)
    .Where(t => !t.EndsWith("_NO", StringComparison.Ordinal))
    .Distinct()
    .ToList();

Console.WriteLine($"[SCANNER] {allEvents.Count} total arb events | {realTickers.Count} WS subscriptions");

// ── ORDER BOOKS ───────────────────────────────────────────────────────────────
// One LocalOrderBook per real ticker AND one per _NO virtual ticker.
// Real ticker book  → YES asks on SELL side, implied YES bids on BUY side
// _NO virtual book  → NO asks on SELL side only (used for binary arb entry cost)
var orderBooks = new ConcurrentDictionary<string, LocalOrderBook>();
foreach (var ticker in allEvents.Values.SelectMany(v => v))
    orderBooks[ticker] = new LocalOrderBook(ticker);

// Per-ticker size maps for delta accumulation (keyed on real ticker only)
var yesSizes = new ConcurrentDictionary<string, Dictionary<decimal, decimal>>();
var noSizes  = new ConcurrentDictionary<string, Dictionary<decimal, decimal>>();
foreach (var ticker in realTickers)
{
    yesSizes[ticker] = new Dictionary<decimal, decimal>();
    noSizes[ticker]  = new Dictionary<decimal, decimal>();
}

// ── BROKER + STRATEGY ─────────────────────────────────────────────────────────
var broker = new KalshiPaperBroker("Kalshi_Arb", STARTING_CAPITAL, scan.TokenNames);

var strategy = new PolymarketCategoricalArbStrategy(
    configuredEvents:       allEvents,
    name:                   "Kalshi_Arb",
    maxInvestmentPerTrade:  MAX_INVESTMENT,
    slippageCents:          SLIPPAGE_CENTS,
    feeRate:                FEE_RATE,
    feeExponent:            FEE_EXPONENT,
    requiredSustainMs:      REQUIRED_SUSTAIN_MS,
    minProfitPerSet:        MIN_PROFIT_PER_SET,
    depthFloorShares:       DEPTH_FLOOR_SHARES,
    postBuyCooldownMs:      POST_BUY_COOLDOWN_MS
);

// Telemetry strategy — same event map, logs arb windows to ArbTelemetry_*.csv
// Uses a dummy broker (no real orders) — only reads books, writes CSV
var telemetryBroker = new KalshiPaperBroker("Kalshi_Telemetry", 0m, scan.TokenNames);
var telemetry = new FastMergeArbTelemetryStrategy(
    configuredEvents:       allEvents,
    maxInvestmentPerTrade:  MAX_INVESTMENT,
    slippageCents:          SLIPPAGE_CENTS,
    minProfitPerSet:        MIN_PROFIT_PER_SET,
    depthFloorShares:       DEPTH_FLOOR_SHARES,
    feeRate:                FEE_RATE,
    feeExponent:            FEE_EXPONENT
);

// ── SHUTDOWN + STATUS ─────────────────────────────────────────────────────────
using var cts = new CancellationTokenSource();
Console.CancelKeyPress += (_, e) => { e.Cancel = true; cts.Cancel(); };

_ = Task.Run(async () =>
{
    while (!cts.Token.IsCancellationRequested)
    {
        await Task.Delay(30_000, cts.Token).ContinueWith(_ => { });
        if (cts.Token.IsCancellationRequested) break;
        lock (GlobalSimulatedBroker.ConsoleLock)
        {
            Console.WriteLine($"\n[STATUS] Cash: ${broker.CashBalance:0.00} | " +
                              $"P&L: ${strategy.TotalRealizedPnL:0.00} | " +
                              $"Sell-backs: {strategy.CompletedSellBacks}");
        }
    }
});

// ══════════════════════════════════════════════════════════════════════════════
//  WEBSOCKET LOOP (with reconnect)
// ══════════════════════════════════════════════════════════════════════════════
while (!cts.Token.IsCancellationRequested)
{
    try
    {
        using var ws = new ClientWebSocket();

        var (key, ts, sig) = orderClient.CreateAuthHeaders("GET", "/trade-api/ws/v2");
        ws.Options.SetRequestHeader("KALSHI-ACCESS-KEY", key);
        ws.Options.SetRequestHeader("KALSHI-ACCESS-TIMESTAMP", ts);
        ws.Options.SetRequestHeader("KALSHI-ACCESS-SIGNATURE", sig);

        await ws.ConnectAsync(new Uri(config.BaseWsUrl), cts.Token);
        Console.WriteLine($"\n[WS] Connected to {config.BaseWsUrl}");

        // Subscribe to real tickers in batches
        int msgId = 1;
        for (int i = 0; i < realTickers.Count; i += SUBSCRIBE_BATCH_SIZE)
        {
            var batch = realTickers.Skip(i).Take(SUBSCRIBE_BATCH_SIZE).ToList();
            string tickerArray = string.Join(",", batch.Select(t => $"\"{t}\""));
            string subMsg = $"{{\"id\":{msgId++},\"cmd\":\"subscribe\",\"params\":{{\"channels\":[\"orderbook_delta\"],\"market_tickers\":[{tickerArray}]}}}}";

            await ws.SendAsync(Encoding.UTF8.GetBytes(subMsg), WebSocketMessageType.Text, true, cts.Token);
            await Task.Delay(100, cts.Token);
        }

        Console.WriteLine($"[WS] Subscribed to {realTickers.Count} tickers.");

        // Clear stale state on reconnect
        foreach (var book in orderBooks.Values) book.ClearBook();
        foreach (var d in yesSizes.Values) d.Clear();
        foreach (var d in noSizes.Values)  d.Clear();
        strategy.OnReconnect();
        telemetry.OnReconnect();

        var receiveBuffer = new byte[65536];
        using var ms = new MemoryStream();

        while (!cts.Token.IsCancellationRequested && ws.State == WebSocketState.Open)
        {
            ms.SetLength(0);
            WebSocketReceiveResult result;

            do
            {
                result = await ws.ReceiveAsync(new ArraySegment<byte>(receiveBuffer), cts.Token);

                if (result.MessageType == WebSocketMessageType.Close)
                {
                    await ws.CloseAsync(WebSocketCloseStatus.NormalClosure, "", CancellationToken.None);
                    goto reconnect;
                }

                ms.Write(receiveBuffer, 0, result.Count);
            }
            while (!result.EndOfMessage);

            if (ms.Length == 0) continue;

            string message = Encoding.UTF8.GetString(ms.GetBuffer(), 0, (int)ms.Length);
            if (message is "heartbeat" or "PONG" or "pong") continue;

            ProcessMessage(message, orderBooks, yesSizes, noSizes, strategy, broker, telemetry, telemetryBroker);
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

Console.WriteLine("\n[SHUTDOWN]");
Console.WriteLine($"  Cash:         ${broker.CashBalance:0.00}");
Console.WriteLine($"  Realized P&L: ${strategy.TotalRealizedPnL:0.00}");
Console.WriteLine($"  Arbs closed:  {strategy.CompletedSellBacks}");
Console.WriteLine($"  Total trades: {broker.TotalTradesExecuted}");

// ══════════════════════════════════════════════════════════════════════════════
//  MESSAGE PROCESSING
// ══════════════════════════════════════════════════════════════════════════════
static void ProcessMessage(
    string message,
    ConcurrentDictionary<string, LocalOrderBook> orderBooks,
    ConcurrentDictionary<string, Dictionary<decimal, decimal>> yesSizes,
    ConcurrentDictionary<string, Dictionary<decimal, decimal>> noSizes,
    PolymarketCategoricalArbStrategy strategy,
    KalshiPaperBroker broker,
    FastMergeArbTelemetryStrategy telemetry,
    KalshiPaperBroker telemetryBroker)
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

        if (!orderBooks.TryGetValue(ticker, out var yesBook)) return;

        // _NO virtual book: exists only for binary arb markets
        orderBooks.TryGetValue(ticker + "_NO", out var noBook);

        if (msgType == "orderbook_snapshot")
        {
            ApplySnapshot(yesBook, noBook, msgEl, yesSizes[ticker], noSizes[ticker]);
            // strategy.OnBookUpdate(yesBook, broker); // disabled: telemetry-only mode
            telemetry.OnBookUpdate(yesBook, telemetryBroker);
            if (noBook != null)
            {
                // strategy.OnBookUpdate(noBook, broker);
                telemetry.OnBookUpdate(noBook, telemetryBroker);
            }
        }
        else if (msgType == "orderbook_delta")
        {
            bool noSideChanged = ApplyDelta(yesBook, noBook, msgEl, yesSizes[ticker], noSizes[ticker]);
            // strategy.OnBookUpdate(yesBook, broker);
            telemetry.OnBookUpdate(yesBook, telemetryBroker);
            if (noBook != null && noSideChanged)
            {
                // strategy.OnBookUpdate(noBook, broker);
                telemetry.OnBookUpdate(noBook, telemetryBroker);
            }
        }
    }
    catch (JsonException) { }
}

// Returns true if the NO side (and therefore the _NO book) was updated.
static bool ApplyDelta(
    LocalOrderBook yesBook,
    LocalOrderBook? noBook,
    JsonElement msg,
    Dictionary<decimal, decimal> yesSizeMap,
    Dictionary<decimal, decimal> noSizeMap)
{
    if (!msg.TryGetProperty("price_dollars", out var priceEl)) return false;
    if (!msg.TryGetProperty("delta_fp",      out var deltaEl)) return false;
    if (!msg.TryGetProperty("side",          out var sideEl))  return false;

    if (!decimal.TryParse(priceEl.GetString(), System.Globalization.NumberStyles.Any,
            System.Globalization.CultureInfo.InvariantCulture, out decimal price)) return false;
    if (!decimal.TryParse(deltaEl.GetString(), System.Globalization.NumberStyles.Any,
            System.Globalization.CultureInfo.InvariantCulture, out decimal delta)) return false;
    string side = sideEl.GetString() ?? "";

    if (side == "yes")
    {
        decimal newSize = yesSizeMap.GetValueOrDefault(price, 0m) + delta;
        decimal impliedNoBid = Math.Round(1m - price, 4);
        if (newSize <= 0)
        {
            yesSizeMap.Remove(price);
            yesBook.UpdatePriceLevel("SELL", price, 0m);
            noBook?.UpdatePriceLevel("BUY", impliedNoBid, 0m);
        }
        else
        {
            yesSizeMap[price] = newSize;
            yesBook.UpdatePriceLevel("SELL", price, newSize);
            noBook?.UpdatePriceLevel("BUY", impliedNoBid, newSize);
        }
        return false;
    }

    if (side == "no")
    {
        decimal newSize = noSizeMap.GetValueOrDefault(price, 0m) + delta;

        if (newSize <= 0)
        {
            noSizeMap.Remove(price);
            noBook?.UpdatePriceLevel("SELL", price, 0m);
            if (noBook != null)
                yesBook.UpdatePriceLevel("BUY", Math.Round(1m - price, 4), 0m);
        }
        else
        {
            noSizeMap[price] = newSize;
            noBook?.UpdatePriceLevel("SELL", price, newSize);
            if (noBook != null)
                yesBook.UpdatePriceLevel("BUY", Math.Round(1m - price, 4), newSize);
        }
        return true;
    }

    return false;
}

static void ApplySnapshot(
    LocalOrderBook yesBook,
    LocalOrderBook? noBook,
    JsonElement msg,
    Dictionary<decimal, decimal> yesSizeMap,
    Dictionary<decimal, decimal> noSizeMap)
{
    yesBook.ClearBook();
    noBook?.ClearBook();
    yesSizeMap.Clear();
    noSizeMap.Clear();

    // YES asks → SELL levels on the real book
    //           + implied NO BID on the _NO virtual book (YES ask $0.54 → NO bid $0.46)
    if (msg.TryGetProperty("yes_dollars_fp", out var yesEl) && yesEl.ValueKind == JsonValueKind.Array)
    {
        foreach (var level in yesEl.EnumerateArray())
        {
            if (!TryParseLevel(level, out decimal price, out decimal size)) continue;
            yesSizeMap[price] = size;
            yesBook.UpdatePriceLevel("SELL", price, size);
            decimal impliedNoBid = Math.Round(1m - price, 4);
            noBook?.UpdatePriceLevel("BUY", impliedNoBid, size);
        }
    }

    // NO asks → SELL level on _NO virtual book + implied YES BID on real book.
    // The implied YES bid is only valid for binary arb (noBook exists).
    // For categorical markets (noBook == null), we have no real YES bid data —
    // adding fake implied bids causes wildly inflated sell-back prices.
    if (msg.TryGetProperty("no_dollars_fp", out var noEl) && noEl.ValueKind == JsonValueKind.Array)
    {
        foreach (var level in noEl.EnumerateArray())
        {
            if (!TryParseLevel(level, out decimal noPrice, out decimal size)) continue;
            noSizeMap[noPrice] = size;
            noBook?.UpdatePriceLevel("SELL", noPrice, size);
            if (noBook != null)
            {
                decimal impliedYesBid = Math.Round(1m - noPrice, 4);
                yesBook.UpdatePriceLevel("BUY", impliedYesBid, size);
            }
        }
    }
}

// Minimum ask price to accept as a valid book level.
// Levels below this are phantom/stale orders on dead or settled markets.
// A $0.01 YES ask + $0.01 NO ask is not a real arb — it's an orphan order.
const decimal MIN_BOOK_PRICE = 0.03m;

static bool TryParseLevel(JsonElement level, out decimal price, out decimal size)
{
    price = 0; size = 0;
    if (level.ValueKind != JsonValueKind.Array) return false;
    var arr = level.EnumerateArray().ToArray();
    if (arr.Length < 2) return false;
    if (!decimal.TryParse(arr[0].GetString(), System.Globalization.NumberStyles.Any,
               System.Globalization.CultureInfo.InvariantCulture, out price)) return false;
    if (!decimal.TryParse(arr[1].GetString(), System.Globalization.NumberStyles.Any,
               System.Globalization.CultureInfo.InvariantCulture, out size)) return false;
    return price >= MIN_BOOK_PRICE && price <= (1m - MIN_BOOK_PRICE);
}
