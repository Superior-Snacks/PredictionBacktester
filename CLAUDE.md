# PredictionBacktester

Polymarket prediction market trading system: backtesting engine, paper trader, and live production executor.

## Architecture

Seven C# projects (.NET 10.0) in a layered architecture:

```
PredictionBacktester.Core          → Shared models, DTOs, interfaces
PredictionBacktester.Data          → SQLite DB (EF Core), Polymarket API clients, repositories
PredictionBacktester.Engine        → Backtesting runner, brokers (simulated + live), order books
PredictionBacktester.Strategies    → 15 trading strategy implementations
PredictionBacktester.ConsoleApp    → Interactive CLI (ingestion, backtesting, optimization)
PredictionLiveTrader               → Paper trading via WebSocket (live data, simulated orders)
PredictionLiveProduction           → Real money live trading via CLOB API
```

## Build & Run

```bash
dotnet build                                          # Build entire solution
dotnet run --project PredictionBacktester.ConsoleApp   # Run backtester CLI
dotnet run --project PredictionLiveTrader               # Run paper trader
dotnet run --project PredictionLiveProduction           # Run production trader
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
| `Engine/LocalOrderBook.cs` | Real-time order book from WebSocket updates |
| `Data/ApiClients/PolymarketClient.cs` | REST client for Gamma/CLOB/Data APIs |
| `Data/Repositories/PolymarketRepository.cs` | Data access layer (queries, upserts, cleanup) |
| `Data/Database/PolymarketDbContext.cs` | EF Core DbContext (Markets, Outcomes, Trades) |
| `ConsoleApp/Program.cs` | CLI with 10 menu options (ingest, backtest, optimize, etc.) |
| `PredictionLiveTrader/Program.cs` | Paper trading with strategy grid search over WebSocket |
| `PredictionLiveTrader/PaperBroker.cs` | Simulated broker with production constraints (min size, max bet, dust detection) |
| `PredictionLiveTrader/MarketReplayLogger.cs` | GZip-compressed WebSocket data recorder for replay |
| `PredictionLiveProduction/Program.cs` | Production live trader with Serilog logging |
| `PredictionLiveProduction/ProductionBroker.cs` | Real CLOB execution with risk controls |

## Strategy System

Three interfaces, all extending `IStrategy`:

- **ITickStrategy** — `OnTick(Trade, SimulatedBroker)` — processes every raw trade
- **ICandleStrategy** — `OnCandle(Candle, SimulatedBroker)` + `Timeframe` — processes OHLCV candles (auto-aggregated from ticks)
- **ILiveStrategy** — `OnBookUpdate(LocalOrderBook, GlobalSimulatedBroker)` — processes real-time order book snapshots

Implemented strategies: RsiReversion, FlashCrashSniper, LiveFlashCrashSniper, LiveFlashCrashReverse, CandleSmaCrossover, BollingerBreakout, HybridConfluence, ThetaDecay, MeanReversionStatArb, OrderBookImbalance, VolumeAnomaly, PureBuyNo, DipBuying, PolymarketCategoricalArb.

### Key Strategy Features

- **LiveFlashCrashSniper**: Anti-spoofing stopwatch (`requiredSustainMs`) delays entry until crash sustains for N ms. Settlement lock (`settlementLockMs`) blocks sells after buy to simulate blockchain settlement.
- **PolymarketCategoricalArb**: Multi-leg arbitrage across categorical market outcomes. Buys all outcomes when total cost < threshold, profits at settlement. Cash balance check prevents partial leg execution.

## Broker Architecture

- **GlobalSimulatedBroker** (base): Thread-safe multi-asset broker with per-broker liquidity tracking (`GetAvailableAskSize`/`GetAvailableBidSize`), latency simulation, and `virtual GetMinSize()` for market-specific minimum trade sizes.
- **PaperBroker** (paper trader): Overrides `SubmitBuyOrder`/`SubmitSellAllOrder` to enforce production constraints: max bet cap, price boundary checks, share rounding, min size rejection, dust detection with one-time logging.
- **ProductionBroker** (live): Routes orders through `PolymarketLiveBroker` → `PolymarketOrderClient` for real CLOB execution with EIP-712 signing (POLY_GNOSIS_SAFE, signature_type=2).

## Paper Trader Features

- **Grid search**: Cartesian product over strategy parameters (thresholds, windows, timers)
- **Live controls**: P=Pause, R=Resume, M=Mute, Q=Quiet, V=Verbose, L=Latency toggle, D=Drop strategy, K=Cull worst performers
- **Settlement sweeper**: Every 15 min resolves closed markets, discovers new markets (>$50k volume), exports CSV
- **Market replay logger**: Non-blocking GZip recording of all WebSocket messages to `MarketData/` folder. Daily file rotation, session-unique filenames, batched flushes (every 5000 ticks).
- **Latency simulation**: Configurable delay on order submission to simulate network latency

## Polymarket APIs

- **Gamma API** (`gamma-api.polymarket.com`) — Market metadata, event listings (paginated via `/events` endpoint)
- **CLOB API** (`clob.polymarket.com`) — Order book, order placement, WebSocket feeds
- **Data API** (`data-api.polymarket.com`) — Historical trade data
- **WebSocket** (`wss://ws-subscriptions-clob.polymarket.com/ws/market`) — Real-time order book updates

Trade pagination uses timestamp-shifting to bypass the 3000-offset API limit.

## Database Schema (SQLite)

- **Markets**: MarketId (ConditionId), Title, EndDate, IsClosed
- **Outcomes**: OutcomeId (ClobTokenId), MarketId (FK), OutcomeName
- **Trades**: Id, OutcomeId (FK), Side, Price, Size, Timestamp, ProxyWallet, TransactionHash
- **Indexes**: (OutcomeId, Timestamp) on Trades; ProxyWallet on Trades

## Trading Simulation Details

- Spread penalty: 1.5 cents per trade (configurable)
- Dual-sided positions: YES and NO tracked independently
- Forced liquidation at market end to realize all P&L
- Equity curve tracking with peak/drawdown metrics
- Trade ledger with CSV export (auto-saved every 15 min + on shutdown)
- Per-broker liquidity consumption prevents strategies from double-counting book depth

## Python Scripts

| Script | Purpose |
|--------|---------|
| `fetch_token_id.py` | Search Polymarket events API for token IDs by market name |
| `time_trades.py` | Measure real settlement delay (buy→sell round-trip) on Polymarket |
| `ping.py` | Latency monitoring to Polymarket CLOB API |
| `analyze_trades.py` | Post-trade analysis and visualization |
| `analyze_realistic_trades.py` | Realistic trade analysis with spread modeling |
| `heatmap.py` | Strategy parameter heatmap visualization |
| `check_proxy.py` | Proxy wallet validation |
| `approve_eoa.py` | EOA wallet approval for CLOB trading |
| `verify_sig.py` | EIP-712 signature verification |
| `test_order.py` | Order placement testing |

## External Dependencies

- **EF Core + SQLite** — Data persistence
- **Nethereum** (Signer.EIP712 + Web3) — Ethereum wallet signing for live orders
- **RestSharp** — HTTP client for CLOB API
- **Serilog** — Structured logging (production project)
- **Microsoft.Extensions.Http** — HttpClientFactory for API clients

## Conventions

- Strategy parameters are passed via constructor; grid search generates all combinations
- `Func<IStrategy>` factory pattern used for portfolio backtests (fresh instance per market)
- `c.Factory()` creates new strategy instances per asset in live trading (no cross-asset state contamination)
- Console output uses `lock(ConsoleLock)` for thread safety in live traders
- ConcurrentDictionary for position tracking in multi-threaded contexts
- Environment variables for API credentials: `POLY_PRIVATE_KEY`, `POLY_PROXY_ADDRESS`, `POLY_API_KEY`, `POLY_API_SECRET`, `POLY_API_PASSPHRASE`
- EIP-712 signing uses `signature_type=2` (POLY_GNOSIS_SAFE) for both C# and Python
