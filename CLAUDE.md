# PredictionBacktester

Polymarket prediction market trading system: backtesting engine, paper trader, and live production executor.

## Architecture

Seven C# projects (.NET 10.0) in a layered architecture:

```
PredictionBacktester.Core          → Shared models, DTOs, interfaces
PredictionBacktester.Data          → SQLite DB (EF Core), Polymarket API clients, repositories
PredictionBacktester.Engine        → Backtesting runner, brokers (simulated + live), order books
PredictionBacktester.Strategies    → 14 trading strategy implementations
PredictionBacktester.ConsoleApp    → Interactive CLI (ingestion, backtesting, optimization)
PredictionLiveTrader               → Paper trading via WebSocket (live data, simulated orders)
PredictionLiveProduction           → Real money live trading (in development)
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
| `Engine/GlobalSimulatedBroker.cs` | Thread-safe multi-asset broker for live trading |
| `Engine/LiveExecution/PolymarketLiveBroker.cs` | Real order execution via CLOB API |
| `Engine/LiveExecution/PolymarketOrderClient.cs` | Polymarket CLOB API wrapper (auth + signing) |
| `Engine/IStrategy.cs` | Strategy interfaces: ITickStrategy, ICandleStrategy, ILiveStrategy |
| `Engine/LocalOrderBook.cs` | Real-time order book from WebSocket updates |
| `Data/ApiClients/PolymarketClient.cs` | REST client for Gamma/CLOB/Data APIs |
| `Data/Repositories/PolymarketRepository.cs` | Data access layer (queries, upserts, cleanup) |
| `Data/Database/PolymarketDbContext.cs` | EF Core DbContext (Markets, Outcomes, Trades) |
| `ConsoleApp/Program.cs` | CLI with 10 menu options (ingest, backtest, optimize, etc.) |
| `PredictionLiveTrader/Program.cs` | Paper trading with strategy grid search over WebSocket |
| `PredictionLiveProduction/Program.cs` | Production live trader (TODO) |

## Strategy System

Three interfaces, all extending `IStrategy`:

- **ITickStrategy** — `OnTick(Trade, SimulatedBroker)` — processes every raw trade
- **ICandleStrategy** — `OnCandle(Candle, SimulatedBroker)` + `Timeframe` — processes OHLCV candles (auto-aggregated from ticks)
- **ILiveStrategy** — `OnBookUpdate(LocalOrderBook, GlobalSimulatedBroker)` — processes real-time order book snapshots

Implemented strategies: RsiReversion, FlashCrashSniper, LiveFlashCrashSniper, LiveFlashCrashReverse, CandleSmaCrossover, BollingerBreakout, HybridConfluence, ThetaDecay, MeanReversionStatArb, OrderBookImbalance, VolumeAnomaly, PureBuyNo, DipBuying.

## Polymarket APIs

- **Gamma API** (`gamma-api.polymarket.com`) — Market metadata, event listings
- **CLOB API** (`clob.polymarket.com`) — Order book, order placement, WebSocket feeds
- **Data API** (`data-api.polymarket.com`) — Historical trade data

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
- Trade ledger with CSV export to Desktop

## External Dependencies

- **EF Core + SQLite** — Data persistence
- **Nethereum** (Signer.EIP712 + Web3) — Ethereum wallet signing for live orders
- **RestSharp** — HTTP client for CLOB API
- **Serilog** — Structured logging (production project)

## Conventions

- Strategy parameters are passed via constructor; grid search generates all combinations
- `Func<IStrategy>` factory pattern used for portfolio backtests (fresh instance per market)
- Console output uses `lock(ConsoleLock)` for thread safety in live traders
- ConcurrentDictionary for position tracking in multi-threaded contexts
- Python scripts (`analyze_trades.py`, `ping.py`) in root for post-analysis and latency monitoring
