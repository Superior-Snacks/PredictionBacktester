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
const decimal DEPTH_FLOOR_SHARES    = 50m;
const decimal SLIPPAGE_CENTS        = 0.02m;
const double  FEE_RATE              = 0.0;   // Pending Kalshi fee schedule confirmation
const double  FEE_EXPONENT          = 1.0;
const long    REQUIRED_SUSTAIN_MS   = 0;
const long    POST_BUY_COOLDOWN_MS  = 60_000;

const int  SUBSCRIBE_BATCH_SIZE     = 100;
const decimal MIN_VOLUME_24H        = 10m;   // Min contracts/day to include in binary scan
const decimal MIN_BOOK_PRICE        = 0.03m; // Reject phantom/stale levels below this price

// "categorical" — only multi-leg categorical arbs
// "binary"      — only single-market YES/NO arbs (top MAX_BINARY_MARKETS by 24h volume)
// "both"        — categorical + binary together
const string ARB_MODE          = "binary";
const int    MAX_BINARY_MARKETS = 10000;

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
var scanner = new KalshiMarketScanner(orderClient, MIN_VOLUME_24H, maxBinaryMarkets: MAX_BINARY_MARKETS);
KalshiScanResult scan = await scanner.ScanAllAsync();

int catCount = scan.CategoricalEvents.Count;
int binCount = scan.BinaryMarkets.Count;

if (catCount + binCount == 0)
{
    Console.WriteLine("[SCANNER] No eligible arb events found. Exiting.");
    return;
}

Console.WriteLine($"[SCANNER] Categorical: {catCount} events | Binary: {binCount} markets");

var allEvents = new Dictionary<string, List<string>>();
if (ARB_MODE is "categorical" or "both")
    foreach (var kv in scan.CategoricalEvents) allEvents[kv.Key] = kv.Value;
if (ARB_MODE is "binary" or "both")
    foreach (var kv in scan.BinaryMarkets)     allEvents[kv.Key] = kv.Value;

// Real tickers to subscribe to (strip _NO virtual IDs — they share a WS stream)
// ConcurrentDictionary<string,byte> used as a thread-safe set so the rescan task
// can add new tickers without racing with the WS receive loop.
var subscribedTickers = new ConcurrentDictionary<string, byte>(
    allEvents.Values
        .SelectMany(v => v)
        .Where(t => !t.EndsWith("_NO", StringComparison.Ordinal))
        .Select(t => new KeyValuePair<string, byte>(t, 0)));
var realTickers = subscribedTickers.Keys.ToList();

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
// Telemetry uses a floor of 1 contract so it logs ALL observable arbs,
// including thin ones. DEPTH_FLOOR_SHARES only gates execution, not observation.
var telemetry = new FastMergeArbTelemetryStrategy(
    configuredEvents:       allEvents,
    maxInvestmentPerTrade:  MAX_INVESTMENT,
    slippageCents:          SLIPPAGE_CENTS,
    minProfitPerSet:        MIN_PROFIT_PER_SET,
    depthFloorShares:       1m,
    feeRate:                FEE_RATE,
    feeExponent:            FEE_EXPONENT
);

// Outbound WS messages queued by background tasks (rescan subscriptions).
// Drained by the WS receive loop after each inbound message.
var wsSendQueue = new ConcurrentQueue<string>();

// When a new arb opens, fire a background REST check to verify live depth.
telemetry.OnArbOpened += (eventId, netCost, legs, wsDepth) =>
{
    _ = Task.Run(async () =>
    {
        var openedAt = DateTime.UtcNow;
        decimal restYesAskSum = -1m;
        decimal restMinDepth  = -1m;
        try
        {
            await Task.Delay(500); // small delay so WS log prints first
            Console.WriteLine($"[REST CHECK] Verifying {eventId} via REST...");

            using var evDoc = await orderClient.GetEventAsync(eventId);
            var evRoot = evDoc.RootElement;
            if (!evRoot.TryGetProperty("event", out var evEl) ||
                !evEl.TryGetProperty("markets", out var mktsEl))
            {
                Console.WriteLine($"[REST CHECK] {eventId}: could not parse event response");
                return;
            }

            var markets = mktsEl.EnumerateArray()
                .Where(m => string.Equals(
                    m.TryGetProperty("status", out var s) ? s.GetString() : "",
                    "active", StringComparison.OrdinalIgnoreCase))
                .ToList();

            if (markets.Count == 0)
            {
                Console.WriteLine($"[REST CHECK] {eventId}: NO ACTIVE MARKETS — already resolved!");
                return;
            }

            // Partial-leg detection: if REST sees more active markets than we subscribed
            // to for this event, we are only tracking a subset of legs. The "arb" is
            // because the unmonitored legs are not priced into our sum — it's NOT real.
            bool partialLegs = markets.Count > legs + 1;

            // Sum YES asks from REST snapshot (no_bid_dollars → implied yes_ask)
            restYesAskSum = 0m;
            restMinDepth  = decimal.MaxValue;
            var legLines = new List<string>();
            foreach (var mkt in markets)
            {
                string ticker = mkt.TryGetProperty("ticker", out var t) ? t.GetString() ?? "" : "";

                // Try known price field names
                static decimal ParseDollarsField(JsonElement el, params string[] keys)
                {
                    foreach (var k in keys)
                        if (el.TryGetProperty(k, out var v))
                        {
                            string? s = v.ValueKind == JsonValueKind.String ? v.GetString() : v.ToString();
                            if (decimal.TryParse(s, System.Globalization.NumberStyles.Any,
                                System.Globalization.CultureInfo.InvariantCulture, out decimal d)) return d;
                        }
                    return 0m;
                }
                decimal yesAsk = ParseDollarsField(mkt, "yes_ask_dollars", "yes_ask_price");
                decimal noBid  = ParseDollarsField(mkt, "no_bid_dollars",  "no_bid_price");
                decimal impliedAsk = noBid > 0.01m ? Math.Round(1m - noBid, 4) : yesAsk;

                if (impliedAsk > 0.01m && impliedAsk < 1.0m) restYesAskSum += impliedAsk;

                // Fetch real orderbook depth
                try
                {
                    using var bookDoc = await orderClient.GetMarketOrderBookAsync(ticker);
                    var book = bookDoc.RootElement.TryGetProperty("orderbook", out var ob) ? ob : bookDoc.RootElement;

                    decimal noDepth  = 0m;
                    decimal yesDepth = 0m;
                    decimal bestNoAsk = 0m;

                    if (book.TryGetProperty("no", out var noEl))
                        foreach (var lvl in noEl.EnumerateArray())
                        {
                            var arr = lvl.EnumerateArray().ToArray();
                            if (arr.Length >= 2)
                            {
                                decimal sz = decimal.Parse(arr[1].GetString() ?? "0",
                                    System.Globalization.CultureInfo.InvariantCulture);
                                decimal pr = decimal.Parse(arr[0].GetString() ?? "0",
                                    System.Globalization.CultureInfo.InvariantCulture) / 100m;
                                noDepth  += sz;
                                if (pr > bestNoAsk) bestNoAsk = pr;
                            }
                        }
                    if (book.TryGetProperty("yes", out var yesEl2))
                        foreach (var lvl in yesEl2.EnumerateArray())
                        {
                            var arr = lvl.EnumerateArray().ToArray();
                            if (arr.Length >= 2)
                                yesDepth += decimal.Parse(arr[1].GetString() ?? "0",
                                    System.Globalization.CultureInfo.InvariantCulture);
                        }

                    decimal impliedRestAsk = bestNoAsk > 0 ? Math.Round(1m - bestNoAsk, 4) : impliedAsk;
                    if (noDepth > 0) restMinDepth = Math.Min(restMinDepth, noDepth);
                    legLines.Add($"  {ticker[..Math.Min(45, ticker.Length)],-45} REST ask=${impliedRestAsk:0.0000}  noDepth={noDepth:0.0}  yesDepth={yesDepth:0.0}");
                    await Task.Delay(150); // polite rate limiting
                }
                catch (Exception ex)
                {
                    legLines.Add($"  {ticker[..Math.Min(45, ticker.Length)],-45} [book fetch failed: {ex.Message}]");
                }
            }

            bool confirmed = !partialLegs && restYesAskSum > 0 && restYesAskSum < 1.0m;
            string arbStatus = partialLegs
                ? $"*** PARTIAL LEGS *** REST={markets.Count} active, WS={legs} subscribed — unregistering"
                : confirmed
                    ? $"*** REST CONFIRMS ARB *** (sum=${restYesAskSum:0.0000})"
                    : restYesAskSum == 0 ? "book empty / prices unavailable"
                    : $"no arb at REST time (sum=${restYesAskSum:0.0000})";

            Console.WriteLine($"[REST CHECK] {eventId}: {markets.Count} REST legs / {legs} WS legs | {arbStatus}");
            Console.WriteLine($"[REST CHECK] WS avg cost=${netCost:0.0000} | WS depth={wsDepth:0.0}");
            foreach (var line in legLines) Console.WriteLine(line);

            // Partial-leg events produce structurally invalid arbs — remove immediately
            // so they never fire again for the rest of this session.
            if (partialLegs)
                telemetry.UnregisterEvent(eventId);

            // Feed results back into strategy so they appear in the closed-arb CSV row
            if (restMinDepth == decimal.MaxValue) restMinDepth = 0m;
            long delayMs = (long)(DateTime.UtcNow - openedAt).TotalMilliseconds;
            telemetry.UpdateRestVerification(eventId, confirmed, restYesAskSum, restMinDepth, delayMs);
        }
        catch (Exception ex)
        {
            Console.WriteLine($"[REST CHECK] {eventId}: error — {ex.Message}");
            telemetry.UpdateRestVerification(eventId, false, -1m, -1m,
                (long)(DateTime.UtcNow - openedAt).TotalMilliseconds);
        }
    });
};

// ── SHUTDOWN + STATUS ─────────────────────────────────────────────────────────
using var cts = new CancellationTokenSource();
Console.CancelKeyPress += (_, e) => { e.Cancel = true; cts.Cancel(); };

// Status ticker
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

// ── PERIODIC RESCAN ───────────────────────────────────────────────────────────
// Every 15 minutes: re-fetch all open events and reconcile against our current
// subscription set.
//
//  • New events / new legs  → create books, queue WS subscription, register
//  • Events that disappeared or gained too many legs → unregister from strategy
//
// This handles two cases:
//   1. Kalshi activates new markets for an existing event after session start
//      (e.g. player-specific HR markets that go live when a game starts).
//   2. Entirely new events that become scannable mid-session.
_ = Task.Run(async () =>
{
    const int RESCAN_INTERVAL_MS = 15 * 60 * 1000;
    int subMsgId = 10_000; // avoid colliding with startup msg IDs

    while (!cts.Token.IsCancellationRequested)
    {
        await Task.Delay(RESCAN_INTERVAL_MS, cts.Token).ContinueWith(_ => { });
        if (cts.Token.IsCancellationRequested) break;

        try
        {
            Console.WriteLine("\n[RESCAN] Re-fetching open events...");
            var newScan = await scanner.ScanAllAsync();

            // ── Leg additions / event removal ─────────────────────────────
            // For every event we currently track, check whether the new scan
            // still includes it.  If it was dropped (gained too many legs,
            // became partially resolved, or closed) — unregister it.
            var currentEventIds = allEvents.Keys.ToList();
            foreach (var eventId in currentEventIds)
            {
                if (!newScan.CategoricalEvents.ContainsKey(eventId))
                {
                    Console.WriteLine($"[RESCAN] Event no longer valid — unregistering: {eventId}");
                    telemetry.UnregisterEvent(eventId);
                    allEvents.Remove(eventId);
                }
            }

            // ── New events ────────────────────────────────────────────────
            int newEventCount = 0;
            int newTickerCount = 0;
            foreach (var (eventId, legs) in newScan.CategoricalEvents)
            {
                if (allEvents.ContainsKey(eventId)) continue; // already tracked

                // Create order books for any legs we haven't seen before
                var newLegs = new List<string>();
                foreach (var ticker in legs)
                {
                    // Register both YES book and _NO virtual book
                    foreach (var bookId in new[] { ticker, ticker + "_NO" })
                        orderBooks.TryAdd(bookId, new LocalOrderBook(bookId));

                    // Add to size maps if it's a real ticker
                    if (!ticker.EndsWith("_NO", StringComparison.Ordinal))
                    {
                        yesSizes.TryAdd(ticker, new Dictionary<decimal, decimal>());
                        noSizes.TryAdd(ticker,  new Dictionary<decimal, decimal>());

                        // Queue a WS subscription if we haven't already subscribed
                        if (subscribedTickers.TryAdd(ticker, 0))
                        {
                            newLegs.Add(ticker);
                            newTickerCount++;
                        }
                    }
                }

                allEvents[eventId] = legs;
                telemetry.RegisterEvent(eventId, legs);
                newEventCount++;

                // Subscribe new tickers in batches of 100
                for (int i = 0; i < newLegs.Count; i += SUBSCRIBE_BATCH_SIZE)
                {
                    var batch = newLegs.Skip(i).Take(SUBSCRIBE_BATCH_SIZE).ToList();
                    string tickerArray = string.Join(",", batch.Select(t => $"\"{t}\""));
                    string subMsg = $"{{\"id\":{subMsgId++},\"cmd\":\"subscribe\",\"params\":{{\"channels\":[\"orderbook_delta\"],\"market_tickers\":[{tickerArray}]}}}}";
                    wsSendQueue.Enqueue(subMsg);
                }

                // Update display names
                foreach (var (k, v) in newScan.TokenNames)
                    scan.TokenNames.TryAdd(k, v);
            }

            if (newEventCount > 0 || currentEventIds.Count != allEvents.Count)
                Console.WriteLine($"[RESCAN] +{newEventCount} new events, +{newTickerCount} new tickers | " +
                                  $"Total: {allEvents.Count} events");
            else
                Console.WriteLine($"[RESCAN] No changes. {allEvents.Count} events active.");
        }
        catch (Exception ex)
        {
            Console.WriteLine($"[RESCAN ERROR] {ex.Message}");
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

            // Drain any outbound messages queued by background tasks (new subscriptions).
            // WS send is safe here because we are the only sender and we're between receives.
            while (wsSendQueue.TryDequeue(out var outMsg))
            {
                try { await ws.SendAsync(Encoding.UTF8.GetBytes(outMsg), WebSocketMessageType.Text, true, cts.Token); }
                catch { /* reconnect will resubscribe everything */ }
            }
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

    // Apply same price floor as snapshots — reject sub-floor levels from deltas too
    if (price < MIN_BOOK_PRICE || price > (1m - MIN_BOOK_PRICE)) return false;

    if (side == "yes")
    {
        // YES bid delta → BUY side of YES book
        decimal newSize = yesSizeMap.GetValueOrDefault(price, 0m) + delta;
        decimal impliedNoAsk = Math.Round(1m - price, 4);
        if (newSize <= 0)
        {
            yesSizeMap.Remove(price);
            yesBook.UpdatePriceLevel("BUY", price, 0m);
            noBook?.UpdatePriceLevel("SELL", impliedNoAsk, 0m);
        }
        else
        {
            yesSizeMap[price] = newSize;
            yesBook.UpdatePriceLevel("BUY", price, newSize);
            noBook?.UpdatePriceLevel("SELL", impliedNoAsk, newSize);
        }
        yesBook.MarkDeltaReceived();
        return false;
    }

    if (side == "no")
    {
        // NO bid delta → implied YES ask at (1 - price) on SELL side of YES book
        decimal newSize = noSizeMap.GetValueOrDefault(price, 0m) + delta;
        decimal impliedYesAsk = Math.Round(1m - price, 4);
        if (newSize <= 0)
        {
            noSizeMap.Remove(price);
            noBook?.UpdatePriceLevel("BUY", price, 0m);
            yesBook.UpdatePriceLevel("SELL", impliedYesAsk, 0m);
        }
        else
        {
            noSizeMap[price] = newSize;
            noBook?.UpdatePriceLevel("BUY", price, newSize);
            yesBook.UpdatePriceLevel("SELL", impliedYesAsk, newSize);
        }
        noBook?.MarkDeltaReceived();
        yesBook.MarkDeltaReceived();
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

    // yes_dollars_fp = YES bids (orders to BUY YES at this price)
    // → BUY side of the YES book
    // Binary arb: YES bid at X implies NO ask at (1-X) on the NO virtual book
    if (msg.TryGetProperty("yes_dollars_fp", out var yesEl) && yesEl.ValueKind == JsonValueKind.Array)
    {
        foreach (var level in yesEl.EnumerateArray())
        {
            if (!TryParseLevel(level, out decimal price, out decimal size)) continue;
            yesSizeMap[price] = size;
            yesBook.UpdatePriceLevel("BUY", price, size);
            noBook?.UpdatePriceLevel("SELL", Math.Round(1m - price, 4), size);
        }
    }

    // no_dollars_fp = NO bids (orders to BUY NO at this price)
    // → implies YES ask at (1 - noPrice): a NO buyer is a functional YES seller
    // → SELL side of the YES book
    // Binary arb: also populate NO book BUY side directly
    if (msg.TryGetProperty("no_dollars_fp", out var noEl) && noEl.ValueKind == JsonValueKind.Array)
    {
        foreach (var level in noEl.EnumerateArray())
        {
            if (!TryParseLevel(level, out decimal noPrice, out decimal size)) continue;
            noSizeMap[noPrice] = size;
            noBook?.UpdatePriceLevel("BUY", noPrice, size);
            decimal impliedYesAsk = Math.Round(1m - noPrice, 4);
            yesBook.UpdatePriceLevel("SELL", impliedYesAsk, size);
        }
    }
}

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
