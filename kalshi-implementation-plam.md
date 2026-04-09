# Kalshi Paper Trading Client

## Context
We have a working Polymarket categorical arb paper trader. The goal is to build a parallel Kalshi client so the same `PolymarketCategoricalArbStrategy` (which is exchange-agnostic — it only calls `OnBookUpdate(LocalOrderBook, GlobalSimulatedBroker)`) can run against Kalshi real-time orderbook data. Fee rate is set to 0 for now.

Key Kalshi differences vs Polymarket:
- **Auth**: RSA-PSS (not EIP-712 + HMAC). Message = `{timestampMs}{METHOD}{/trade-api/v2/path}`, SHA-256, PSS padding, DIGEST_LENGTH salt, base64-encoded.
- **Order book**: `yes_dollars_fp` / `no_dollars_fp` arrays of `[price_cents_string, size_string]`. Prices are integer cents (54 = $0.54).
- **WebSocket**: `wss://demo-api.kalshi.co/trade-api/ws/v2`, subscribe via `{id, cmd: "subscribe", params: {channels: ["orderbook_delta"], market_ticker: "..."}}`. Auth headers on HTTP upgrade. Server pings "heartbeat" every 10s — respond with pong frame.
- **Multi-outcome arb**: Kalshi groups related binary markets under an event. Exactly one market in a group will resolve YES. Sum of YES ask prices < $1.00 = arb opportunity. Same math as negRisk on Polymarket.
- **Contracts**: Fractional contracts supported (fee docs reference 0.30 contracts), price range 1–99 cents.
- **Balance**: API returns cents (integer). Divide by 100 for dollars.

---

## New Files (6 files + 1 project file)

### 1. `PredictionBacktester.Engine/LiveExecution/KalshiApiConfig.cs`
```csharp
public class KalshiApiConfig {
    public string ApiKeyId { get; set; }       // UUID: a952bcbe-...
    public string PrivateKeyPath { get; set; } // Path to .key PEM file
    public string BaseRestUrl { get; set; } = "https://demo-api.kalshi.co/trade-api/v2";
    public string BaseWsUrl  { get; set; } = "wss://demo-api.kalshi.co/trade-api/ws/v2";
}
```

Env vars: `KALSHI_API_KEY_ID`, `KALSHI_PRIVATE_KEY_PATH`

---

### 2. `PredictionBacktester.Engine/LiveExecution/KalshiOrderClient.cs`
RSA-PSS auth wrapper + REST calls for scanner and balance.

**Signing:**
```csharp
private (string key, string ts, string sig) CreateAuthHeaders(string method, string path) {
    string ts = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds().ToString();
    string pathNoQuery = path.Split('?')[0];
    string message = ts + method + pathNoQuery;
    byte[] sig = _rsa.SignData(Encoding.UTF8.GetBytes(message), HashAlgorithmName.SHA256, RSASignaturePadding.Pss);
    return (_config.ApiKeyId, ts, Convert.ToBase64String(sig));
}
```

**Key methods:**
- `LoadPrivateKey()` — `RSA.Create()` + `ImportFromPem(File.ReadAllText(keyPath))`
- `GetAsync(string path)` — sets KALSHI-ACCESS-KEY/TIMESTAMP/SIGNATURE headers
- `GetBalanceCentsAsync()` → `GET /portfolio/balance` → returns `balance` field (cents int)
- `GetEventsAsync(int offset, int limit)` → `GET /events?status=open&limit={limit}&cursor={cursor}`
- `GetMarketOrderBookAsync(string ticker)` → `GET /markets/{ticker}/orderbook`

Note: All REST calls use the full path from root for signing (e.g. `/trade-api/v2/portfolio/balance`), not just the relative endpoint.

---

### 3. `PredictionBacktester.Engine/KalshiMarketScanner.cs`
Mirrors `PolymarketMarketScanner`. Returns `Dictionary<string, List<string>>` (eventId → ordered list of market tickers).

**Logic:**
1. `GET /events?status=open&limit=200` — paginate with cursor
2. For each event: get its markets (`event.markets` array in response)
3. Filter: must have 3+ active markets (`status == "active"`)
4. Skip sports events — same keyword set as Polymarket scanner (sports tag slug/label check)
5. Build map: `eventId → [ticker1, ticker2, ticker3, ...]`
6. Also populate `TokenNames[ticker]` = market title (for display)

No live arb-cost filter at scan time (strategy handles that once books are live).

---

### 4. `KalshiPaperTrader/KalshiPaperBroker.cs`
Extends `GlobalSimulatedBroker`. Minimal for paper trading — no real API calls.

```csharp
public class KalshiPaperBroker : GlobalSimulatedBroker {
    public override decimal GetMinSize(string assetId) => 1.0m; // 1 contract minimum
}
```

Constructor mirrors `PaperBroker`: takes `name`, `startingCapital`, `tokenNames`.

---

### 5. `KalshiPaperTrader/Program.cs`
Main loop. Mirrors `PredictionLiveTrader/Program.cs` pattern.

**Startup:**
1. Load `KalshiApiConfig` from env vars
2. Create `KalshiOrderClient`
3. Run `KalshiMarketScanner.GetArbitrageEventsAsync()` → event→ticker map
4. Create one `LocalOrderBook` per ticker
5. Create one shared `KalshiPaperBroker` (starting capital $1000)
6. Create one `PolymarketCategoricalArbStrategy` with the event→ticker map, `feeRate=0`

**WebSocket connection:**
```csharp
var ws = new ClientWebSocket();
// Auth headers on HTTP upgrade (timestamp+sig computed at connect time)
var (key, ts, sig) = orderClient.CreateAuthHeaders("GET", "/trade-api/ws/v2");
ws.Options.SetRequestHeader("KALSHI-ACCESS-KEY", key);
ws.Options.SetRequestHeader("KALSHI-ACCESS-TIMESTAMP", ts);
ws.Options.SetRequestHeader("KALSHI-ACCESS-SIGNATURE", sig);
await ws.ConnectAsync(new Uri(config.BaseWsUrl), cts.Token);
```

**Subscription:** send one subscribe message per ticker (or batch into single message with array of tickers if Kalshi supports it):
```json
{"id": 1, "cmd": "subscribe", "params": {"channels": ["orderbook_delta"], "market_ticker": "KXPRESIDENTBIDEN"}}
```

**Message loop:**
- Buffer frames until `EndOfMessage` (same 8KB pattern as Polymarket)
- Parse JSON: check `type` field
- `orderbook_snapshot` → parse `yes_dollars_fp` array → populate LocalOrderBook:
  - `book.ClearBook()`
  - For each `[price_str, size_str]` in `yes_dollars_fp`: `book.UpdatePriceLevel("SELL", int.Parse(price_str)/100m, decimal.Parse(size_str))` (YES asks = ask side)
  - For each `[price_str, size_str]` in `no_dollars_fp`: `book.UpdatePriceLevel("BUY", 1m - int.Parse(price_str)/100m, decimal.Parse(size_str))` (NO ask at 46c → YES bid at 54c)
- `orderbook_delta` → parse `price_dollars`, `delta_fp`, `side` → apply delta:
  - If `side=="yes"`: `book.UpdatePriceLevel("SELL", price, currentSize + delta)`
  - If `side=="no"`: `book.UpdatePriceLevel("BUY", 1m - price, currentSize + delta)` 
  - Need to track current size per level for delta accumulation (snapshot seeds it)
- After each book update: `strategy.OnBookUpdate(book, broker)`
- Heartbeat: respond to server Ping (0x9) with Pong (0xA); also send application-level pong to "heartbeat" text frames

**Reconnect:** on disconnect, clear all books + call `strategy.OnReconnect()`, retry with 5s delay.

**Console output:** Print P&L summary every 30s: cash balance, realized P&L, completed trade count.

---

### 6. `KalshiPaperTrader/KalshiPaperTrader.csproj`
```xml
<Project Sdk="Microsoft.NET.Sdk">
  <PropertyGroup>
    <OutputType>Exe</OutputType>
    <TargetFramework>net10.0</TargetFramework>
  </PropertyGroup>
  <ItemGroup>
    <ProjectReference Include="..\PredictionBacktester.Engine\PredictionBacktester.Engine.csproj" />
    <ProjectReference Include="..\PredictionBacktester.Strategies\PredictionBacktester.Strategies.csproj" />
  </ItemGroup>
</Project>
```

Add to `PredictionBacktester.sln` via `dotnet sln add`.

---

## Delta handling detail
Kalshi sends delta_fp (integer cents * 100 i.e. fixed point). Need to accumulate per-level sizes. Track `Dictionary<string, decimal> _yesSizes` and `_noSizes` keyed on ticker+price. On snapshot, seed these maps. On delta, apply `currentSize + delta_fp/100m` and pass to `UpdatePriceLevel`.

Actually `delta_fp` = change in size (positive = more depth, negative = depth consumed). Apply: `newSize = trackedSize + delta; if newSize <= 0: remove level; else: update`. The fixed-point `_fp` suffix means values are integers representing `actual * 100`.

---

## Files to Modify
- `PredictionBacktester.sln` — add KalshiPaperTrader project (`dotnet sln add`)

## Files NOT Modified
- `PolymarketCategoricalArbStrategy.cs` — already exchange-agnostic, used as-is
- `LocalOrderBook.cs` — used as-is via `UpdatePriceLevel`
- `GlobalSimulatedBroker.cs` — base class, no changes needed

---

## Verification
1. `dotnet build` — entire solution compiles
2. Set env vars: `KALSHI_API_KEY_ID`, `KALSHI_PRIVATE_KEY_PATH`
3. `dotnet run --project KalshiPaperTrader` — should connect to WSS, print "Subscribed to N tickers across M events", start receiving book updates
4. Watch for `[ARB DETECTED]` console output when arb windows appear
5. Confirm balance decrements on `[ARB FIRED]` and increments on `[ARB SELL-BACK COMPLETE]`
