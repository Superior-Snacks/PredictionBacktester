# PredictionBacktester

Polymarket prediction market backtesting and live paper trading platform built in .NET 10.0 / C#.

## Project Structure

```
PredictionBacktester.Core/          # Domain entities, DTOs, interfaces (no dependencies)
PredictionBacktester.Data/          # EF Core DbContext, API clients, repository (SQLite)
PredictionBacktester.Engine/        # Trading engine: brokers, strategy interfaces, backtest runner
PredictionBacktester.Strategies/    # 13 trading strategy implementations
PredictionBacktester.ConsoleApp/    # CLI entry point for backtesting and data management
PredictionLiveTrader/               # Live paper trading engine with WebSocket order book
```

Solution file: `PredictionBacktester.slnx`

## Architecture

**Dependency flow:** Core ← Data ← Engine ← Strategies ← ConsoleApp / LiveTrader

**Key patterns:**
- Repository pattern (`PolymarketRepository`)
- Strategy pattern (`IStrategy` / `ITickStrategy` / `ICandleStrategy` / `ILiveStrategy`)
- DI via `Microsoft.Extensions.DependencyInjection` in both apps
- Thread-safe broker (`GlobalSimulatedBroker` uses `ConcurrentDictionary` + locks)

**Data flow:**
Polymarket API → PolymarketClient → Repository → SQLite DB → BacktestRunner → Strategy → SimulatedBroker → ExecutedTrade ledger → CSV export

## Key Files

| File | Purpose |
|------|---------|
| `Core/DbModels.cs` | Market, Outcome, Trade EF entities |
| `Core/Candle.cs` | OHLCV candle model |
| `Core/PolymarketDtos.cs` | API response DTOs (Gamma, CLOB, Data APIs) |
| `Data/PolymarketDbContext.cs` | EF Core context, indexes, schema |
| `Data/PolymarketClient.cs` | HTTP clients for 3 Polymarket APIs |
| `Data/PolymarketRepository.cs` | Data access queries |
| `Engine/IStrategy.cs` | Strategy interfaces (tick, candle, live) |
| `Engine/SimulatedBroker.cs` | Single-market broker simulation |
| `Engine/GlobalSimulatedBroker.cs` | Multi-market thread-safe broker |
| `Engine/BacktestRunner.cs` | Backtest orchestration |
| `Engine/LocalOrderBook.cs` | Order book model (SortedDictionary) |
| `Engine/TradeExporter.cs` | CSV export |
| `ConsoleApp/Program.cs` | CLI menu (9 options: ingest, backtest, optimize, etc.) |
| `PredictionLiveTrader/Program.cs` | WebSocket live paper trading with 8+ strategies |

## Domain Concepts

- **Market** - A prediction market (e.g., "Will X happen?")
- **Outcome** - Binary YES/NO side, identified by `ClobTokenId`
- **Trade** - Tick-level data: price, size, timestamp, wallet, side
- **Candle** - OHLCV aggregation from trades
- **Spread penalty** - 0.015 (1.5 cents) simulates market realism
- Positions tracked separately for YES and NO sides (omnidirectional)
- `decimal` type used for all financial calculations

## External APIs

| Name | Base URL | Purpose |
|------|----------|---------|
| Gamma | `https://gamma-api.polymarket.com/` | Market metadata |
| CLOB | `https://clob.polymarket.com/` | Historical price ticks |
| Data | `https://data-api.polymarket.com/` | Raw trade data |

HTTP clients registered via named `IHttpClientFactory`.

## Database

- **SQLite** at `polymarket_backtest.db`
- **Tables:** Markets, Outcomes, Trades
- **Key index:** `(OutcomeId, Timestamp)` for fast backtesting
- **Migrations** in `Data/Migrations/`

## Strategy Types

**Tick-based:** FlashCrashSniper, LiveFlashCrashSniper, LiveFlashCrashReverse
**Candle-based:** RsiReversion, SmaCrossover, BollingerBreakout, VolumeAnomaly, ThetaDecay
**Order book:** OrderBookImbalance, MeanReversionStatArb
**Hybrid/Utility:** HybridConfluence, DipBuying, PureBuyNo

All strategies implement one of: `ITickStrategy`, `ICandleStrategy`, or `ILiveStrategy`.

## Build & Run

```bash
dotnet build PredictionBacktester.slnx          # Build all projects
dotnet run --project PredictionBacktester.ConsoleApp   # Run backtester CLI
dotnet run --project PredictionLiveTrader               # Run live paper trader
```

**Framework:** .NET 10.0
**Key packages:** EF Core 10.0.3 (SQLite), Microsoft.Extensions.Http 10.0.3

## Coding Conventions

- PascalCase for public members, camelCase for private fields/params
- File-scoped namespaces
- One class per file
- Implicit usings and nullable reference types enabled
- Extensive `Console.WriteLine` for logging (color-coded in live trader)
- LINQ for data queries
- No unit tests — validation done through backtests and paper trading
