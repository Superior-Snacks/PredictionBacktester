# PredictionBacktester

Prediction market trading system spanning Polymarket and Kalshi: backtesting engine, paper traders, live production executor, and cross-platform arbitrage monitor.

## Architecture

Nine C# projects (.NET 10.0) in a layered architecture:

```
PredictionBacktester.Core          → Shared models, DTOs, database entities
PredictionBacktester.Data          → SQLite DB (EF Core), Polymarket API clients, repositories
PredictionBacktester.Engine        → Backtesting runner, brokers (simulated + live), order books
PredictionBacktester.Strategies    → Trading strategy implementations
PredictionBacktester.ConsoleApp    → Interactive CLI (ingestion, backtesting, optimization)
PredictionLiveTrader               → Polymarket paper trading via WebSocket (live data, simulated orders)
PredictionLiveProduction           → Polymarket real money live trading via CLOB API
KalshiPaperTrader                  → Kalshi categorical + binary arb paper trader with telemetry
KalshiPolyCross                    → Cross-platform Kalshi ↔ Polymarket binary arb monitor
```

Dependencies: Core ← Data ← Engine ← Strategies ← {ConsoleApp, PredictionLiveTrader, PredictionLiveProduction, KalshiPaperTrader, KalshiPolyCross}
KalshiPaperTrader and KalshiPolyCross depend only on Engine + Strategies (no Data layer).

## Build & Run

```bash
dotnet build                                            # Build entire solution
dotnet run --project PredictionBacktester.ConsoleApp    # Backtester CLI
dotnet run --project PredictionLiveTrader               # Polymarket paper trader
dotnet run --project PredictionLiveProduction           # Polymarket production trader
dotnet run --project KalshiPaperTrader                  # Kalshi arb paper trader
dotnet run --project KalshiPolyCross                    # Cross-platform arb monitor
```

Database auto-migrates on startup (`dbContext.Database.MigrateAsync()`). SQLite file: `polymarket_backtest.db`.

## Key Files

| File | Purpose |
|------|---------|
| `Engine/BacktestRunner.cs` | Orchestrates single-market and portfolio simulations |
| `Engine/SimulatedBroker.cs` | Single-market order execution with spread penalties |
| `Engine/GlobalSimulatedBroker.cs` | Thread-safe multi-asset broker with per-broker liquidity tracking |
| `Engine/LiveExecution/PolymarketLiveBroker.cs` | Real order execution via CLOB API |
| `Engine/LiveExecution/PolymarketOrderClient.cs` | Polymarket CLOB API wrapper (auth + EIP-712 signing) |
| `Engine/IStrategy.cs` | Strategy interfaces: ITickStrategy, ICandleStrategy, ILiveStrategy |
| `Engine/LocalOrderBook.cs` | Real-time order book from WebSocket deltas |
| `Data/ApiClients/PolymarketClient.cs` | REST client for Gamma/CLOB/Data APIs |
| `Data/Repositories/PolymarketRepository.cs` | Data access layer (queries, upserts, cleanup) |
| `Data/Database/PolymarketDbContext.cs` | EF Core DbContext (Markets, Outcomes, Trades) |
| `ConsoleApp/Program.cs` | CLI with 10 menu options (ingest, backtest, optimize, etc.) |
| `PredictionLiveTrader/Program.cs` | Paper trading with strategy grid search over WebSocket |
| `PredictionLiveTrader/PaperBroker.cs` | Simulated broker with production constraints |
| `PredictionLiveTrader/MarketReplayLogger.cs` | GZip-compressed WebSocket data recorder for replay |
| `PredictionLiveProduction/Program.cs` | Production live trader with Serilog logging |
| `PredictionLiveProduction/ProductionBroker.cs` | Real CLOB execution with risk controls |
| `KalshiPaperTrader/Program.cs` | Kalshi arb paper trader: scan → subscribe → detect → telemetry |
| `KalshiPaperTrader/KalshiPaperBroker.cs` | Extends GlobalSimulatedBroker (min 1 contract, no spread penalty) |
| `KalshiPolyCross/Program.cs` | Cross-platform market matcher + dual-WS arb monitor |
| `KalshiPolyCross/CrossPlatformArbTelemetryStrategy.cs` | K↔P arb window detection and CSV telemetry |
| `Strategies/FastMergeArbTelemetryStrategy.cs` | Kalshi-specific: logs every arb window to ArbTelemetry_*.csv |
| `Strategies/PolymarketCategoricalArbStrategy.cs` | Multi-leg categorical arb execution (used by both Kalshi and Polymarket) |

## Strategy System

Three interfaces, all extending `IStrategy`:

- **ITickStrategy** — `OnTick(Trade, SimulatedBroker)` — processes every raw trade
- **ICandleStrategy** — `OnCandle(Candle, SimulatedBroker)` + `Timeframe` — processes OHLCV candles (auto-aggregated from ticks)
- **ILiveStrategy** — `OnBookUpdate(LocalOrderBook, GlobalSimulatedBroker)` — processes real-time order book snapshots

Implemented strategies: RsiReversion, FlashCrashSniper, LiveFlashCrashSniper, LiveFlashCrashReverse, CandleSmaCrossover, BollingerBreakout, HybridConfluence, ThetaDecay, MeanReversionStatArb, OrderBookImbalance, VolumeAnomaly, PureBuyNo, DipBuying, PolymarketCategoricalArb, FastMergeArbTelemetry.

### Key Strategy Details

- **LiveFlashCrashSniper**: Anti-spoofing stopwatch (`requiredSustainMs`) delays entry until crash sustains for N ms. Settlement lock (`settlementLockMs`) blocks sells after buy to simulate blockchain settlement.
- **PolymarketCategoricalArbStrategy**: Multi-leg arbitrage. Buys all YES legs when net cost (gross + fees) < $1.00. Cash balance check prevents partial fills. Also used in KalshiPaperTrader for Kalshi execution.
- **FastMergeArbTelemetryStrategy**: Telemetry-only (no execution). Monitors all arb windows and logs them to `ArbTelemetry_*.csv`. Uses Kalshi fee formula: `0.07 × P × (1 - P)` per contract. Arb condition: `1.07 × Σ(P_i) − 0.07 × Σ(P_i²) < 1.00`.

## Broker Architecture

- **GlobalSimulatedBroker** (base): Thread-safe multi-asset broker with per-broker liquidity tracking (`GetAvailableAskSize`/`GetAvailableBidSize`), latency simulation, and `virtual GetMinSize()` for market-specific minimum trade sizes.
- **PaperBroker** (Polymarket paper): Overrides `SubmitBuyOrder`/`SubmitSellAllOrder` to enforce production constraints: max bet cap, price boundary checks, share rounding, min size rejection, dust detection with one-time logging.
- **KalshiPaperBroker** (Kalshi paper): GetMinSize() returns 1.0 (Kalshi min: 1 contract). SpreadPenalty = 0 (fees handled in strategy).
- **ProductionBroker** (live): Routes orders through `PolymarketLiveBroker` → `PolymarketOrderClient` for real CLOB execution with EIP-712 signing (POLY_GNOSIS_SAFE, signature_type=2).

## Kalshi Paper Trader

**ARB_MODE options:** `"categorical"` | `"binary"` | `"both"` (currently `"categorical"`)

**Market scanning** (`KalshiMarketScanner`):
- Fetches all open Kalshi events
- Keeps only `mutually_exclusive: true` events
- Applies exhaustive check: hardcoded blocklist + auto-learned `event_blocklist.json` for events where all legs can resolve NO (spreads, conditionals)
- Binary mode scans all markets for YES/NO single-market arbs

**Arb detection** (`FastMergeArbTelemetryStrategy`):
- Walks ask side of each leg (slippage-adjusted), sums VWAP prices + Kalshi fees
- Categorical sanity: `sum(midpoints) ≥ 0.80` to reject correlated/spread markets
- Stale guard: books silent >120s are skipped
- Near-miss report printed every 30s

**Telemetry CSV columns:** StartTime, EndTime, DurationMs, EventId, NumLegs, LegTickers, LegPrices, EntryNetCost, BestGrossCost, TotalFees, BestNetCost, NetProfitPerShare, MaxVolume, TotalCapitalRequired, TotalPotentialProfit, RestChecked, RestConfirmed, RestYesAskSum, RestMinDepth, RestCheckDelayMs

**Key configuration constants (KalshiPaperTrader/Program.cs):**
```
FEE_RATE              = 0.07    // Kalshi: 0.07 × P × (1-P) per contract
MIN_PROFIT_PER_SET    = 0.02    // require $0.02 net profit after fees
DEPTH_FLOOR_SHARES    = 50      // require 50+ contracts depth (execution only)
MAX_INVESTMENT        = 50.00   // max $ per arb trade
SLIPPAGE_CENTS        = 0.02    // add 2¢ to ask when walking
POST_BUY_COOLDOWN_MS  = 60000   // block re-entry 60s after buy
```

**`event_blocklist.json`**: Auto-updated by `analyze_kalshi_arb.py` when all-NO (non-exhaustive) resolutions are detected. Also includes hardcoded entries for known structural non-exhaustive series (KXNBA2D, KXTRUMPNUMSTATES, etc.).

## KalshiPolyCross

Cross-platform binary arb monitor comparing Kalshi and Polymarket binary markets in real time.

**Market matching (`Program.cs`):**
- Fetches Kalshi via `GET /markets?status=open` and Polymarket via `/events`
- Sports-only filter on both sides (Kalshi: `category == "Sports"`; Polymarket: `tags` contains "sport")
- Two-pass title matching: Pass 1 = full title keywords; Pass 2 = YES/NO subtitles
- Auto-saves unambiguous perfect-match pairs to `cross_pairs.json`

**Arb detection (`CrossPlatformArbTelemetryStrategy.cs`):**
- Arb types: `K_YES_P_NO` or `K_NO_P_YES`
- Threshold: `sum(both legs) < 0.995`
- Logs windows to `CrossArbTelemetry_*.csv`
- Cross-platform fee model: TBD (currently 0)

**`cross_pairs.json`**: Manual verified or auto-saved pairs; empty `[]` by default.

## Analyzer Script

`analyze_kalshi_arb.py` reads `ArbTelemetry_*.csv` and produces 9 analysis sections.

**CLI flags:**
```bash
python analyze_kalshi_arb.py                          # auto-discover latest CSV
python analyze_kalshi_arb.py --file path/to/file.csv
python analyze_kalshi_arb.py --3                      # filter events settling >3 months out
python analyze_kalshi_arb.py --include EPL,NBA        # only events matching these terms
python analyze_kalshi_arb.py --exclude trump          # remove events matching these terms
python analyze_kalshi_arb.py --clean                  # only non-flagged rows
python analyze_kalshi_arb.py --no-api                 # skip Kalshi resolution API calls
```

**`--N` filter**: Uses `GET /events/{ticker}?with_nested_markets=true` to get real `expected_expiration_time` per event. Results cached in `*_expiry_cache.json`. Falls back to ticker-regex date parsing.

**`--include` / `--exclude`**: Uses `GET /series` for proper category/tag matching. Falls back to EventId substring matching.

**Fraud flags:** `THIN_DEPTH` (MaxVolume < 2.0), `INSTANT_OPEN_CLOSE` (< 10ms), `REPEAT_SPAM` (> 100 windows/event).

**Production sim model:** Duration-tiered participation rates model competition (<0.5s=15%, 0.5–2s=30%, 2–60s=60%, >60s=85%).

## Polymarket APIs

- **Gamma API** (`gamma-api.polymarket.com`) — Market metadata, event listings (paginated via `/events`)
- **CLOB API** (`clob.polymarket.com`) — Order book, order placement, WebSocket feeds
- **Data API** (`data-api.polymarket.com`) — Historical trade data
- **WebSocket** (`wss://ws-subscriptions-clob.polymarket.com/ws/market`) — Real-time order book updates

Trade pagination uses timestamp-shifting to bypass the 3000-offset API limit.

## Kalshi APIs

- **REST** (`api.elections.kalshi.com/trade-api/v2`) — `/events`, `/markets`, `/portfolio/balance`, `/orders`, `/series`
- **WebSocket** (`api.elections.kalshi.com/trade-api/ws/v2`) — Real-time market deltas
- **Auth**: RSA-PSS signature. Message: `{timestampMs}{METHOD}{/trade-api/v2/path}`. Headers: `KALSHI-ACCESS-KEY`, `KALSHI-ACCESS-TIMESTAMP`, `KALSHI-ACCESS-SIGNATURE`.

## Database Schema (SQLite)

- **Markets**: MarketId (ConditionId), Title, EndDate, IsClosed
- **Outcomes**: OutcomeId (ClobTokenId), MarketId (FK), OutcomeName
- **Trades**: Id, OutcomeId (FK), Side, Price, Size, Timestamp, ProxyWallet, TransactionHash
- **Indexes**: (OutcomeId, Timestamp) on Trades; ProxyWallet on Trades

## Paper Trader Features (Polymarket)

- **Grid search**: Cartesian product over strategy parameters (thresholds, windows, timers)
- **Live controls**: P=Pause, R=Resume, M=Mute, Q=Quiet, V=Verbose, L=Latency toggle, D=Drop strategy, K=Cull worst performers
- **Settlement sweeper**: Every 15 min resolves closed markets, discovers new markets (>$50k volume), exports CSV
- **Market replay logger**: Non-blocking GZip recording of all WebSocket messages to `MarketData/` folder. Daily file rotation, session-unique filenames, batched flushes (every 5000 ticks).
- **Latency simulation**: Configurable delay on order submission to simulate network latency

## Python Scripts

| Script | Purpose |
|--------|---------|
| `analyze_kalshi_arb.py` | Kalshi ArbTelemetry CSV analysis (9 sections, fraud checks, resolution, production sim) |
| `ping_kalshi.py` | Latency benchmarking: DNS, TCP, TLS, REST, WebSocket, ICMP vs Kalshi |
| `fetch_token_id.py` | Search Polymarket events API for token IDs by name |
| `time_trades.py` | Measure real settlement delay (buy→sell round-trip) on Polymarket |
| `analyze_trades.py` | Post-trade analysis and visualization |
| `analyze_realistic_trades.py` | Realistic trade analysis with spread modeling |
| `analyze_arb.py` / `analyze_arb_execution.py` | Arb execution metrics |
| `analyze_replay.py` | Offline backtest analysis from replay logs |
| `heatmap.py` | Strategy parameter heatmap visualization |
| `check_proxy.py` | Proxy wallet validation |
| `approve_eoa.py` | EOA wallet approval for CLOB trading |
| `verify_sig.py` / `compare_sig.py` | EIP-712 signature verification |
| `test_order.py` | Order placement testing |
| `check_kalshi_books.py` | Inspect Kalshi order book snapshots |
| `test_kalshi_auth.py` | Verify Kalshi API authentication |

## External Dependencies

- **EF Core + SQLite** — Data persistence
- **Nethereum** (Signer.EIP712 + Web3) — Ethereum wallet signing for Polymarket live orders
- **RestSharp** — HTTP client for CLOB API
- **Serilog** — Structured logging (production project)
- **Microsoft.Extensions.Http** — HttpClientFactory for API clients

## Conventions

- Strategy parameters are passed via constructor; grid search generates all combinations
- `Func<IStrategy>` factory pattern used for portfolio backtests (fresh instance per market)
- `c.Factory()` creates new strategy instances per asset in live trading (no cross-asset state contamination)
- Console output uses `lock(ConsoleLock)` for thread safety in live traders
- ConcurrentDictionary for position tracking in multi-threaded contexts
- Environment variables for API credentials:
  - Polymarket: `POLY_PRIVATE_KEY`, `POLY_PROXY_ADDRESS`, `POLY_API_KEY`, `POLY_API_SECRET`, `POLY_API_PASSPHRASE`
  - Kalshi: `KALSHI_API_KEY_ID`, `KALSHI_PRIVATE_KEY_PATH`
- EIP-712 signing uses `signature_type=2` (POLY_GNOSIS_SAFE) for both C# and Python
- Kalshi auth uses RSA-PSS with per-request timestamp signature
