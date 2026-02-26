# PredictionBacktester

A Polymarket prediction market backtesting and live paper trading platform built in C# / .NET 10.0.

Ingest real trade data from Polymarket's APIs, replay it through configurable trading strategies, then deploy the best performers as live paper traders against real-time WebSocket order book feeds.

## Features

- **Historical backtesting** — Replay tick-level and candle-aggregated trade data through 13 strategy implementations
- **Portfolio backtesting** — Test strategies across multiple markets simultaneously with a shared broker
- **Parameter optimization** — Grid search / Cartesian product sweeps across strategy parameter spaces
- **Live paper trading** — Real-time WebSocket order book feed with simulated execution, settlement sweeping, and automatic new market discovery
- **Data pipeline** — Ingest, sync, and manage Polymarket market/trade data in a local SQLite database
- **CSV export** — Full trade ledger export for external analysis (pairs with included Python analysis script)

## Project Structure

```
PredictionBacktester.Core/          Domain entities, DTOs, interfaces (no dependencies)
PredictionBacktester.Data/          EF Core DbContext, API clients, repository (SQLite)
PredictionBacktester.Engine/        Trading engine: brokers, strategy interfaces, backtest runner
PredictionBacktester.Strategies/    13 trading strategy implementations
PredictionBacktester.ConsoleApp/    CLI entry point for backtesting and data management
PredictionLiveTrader/               Live paper trading engine with WebSocket order book
```

**Dependency flow:** `Core <- Data <- Engine <- Strategies <- ConsoleApp / LiveTrader`

## Prerequisites

- [.NET 10.0 SDK](https://dotnet.microsoft.com/download)
- Internet connection (Polymarket API access)
- Python 3 with `pandas` (optional, for `analyze_trades.py`)

## Build & Run

```bash
# Build all projects
dotnet build PredictionBacktester.slnx

# Run the backtester CLI
dotnet run --project PredictionBacktester.ConsoleApp

# Run the live paper trader
dotnet run --project PredictionLiveTrader
```

## Backtester CLI

The console app provides a menu-driven interface for data management and backtesting:

| Option | Description |
|--------|-------------|
| 1 | **Standard Ingestion** — Full exchange sync, fetches all active markets and trades |
| 2 | **Smart Daily Sync** — Updates open markets for new trades, then discovers new markets |
| 3 | **Strategy Backtest** — Run a single strategy on a specific outcome over a time range |
| 4 | **Explore Market & Trade Data** — Search markets by keyword, view outcomes and trades |
| 5 | **Explore Live API Data** — Fetch raw JSON from the Polymarket API |
| 6 | **Portfolio Backtest** — Test a strategy across multiple markets matching a keyword |
| 7 | **Universal Optimizer** — Grid search across strategy parameter combinations |
| 8 | **Data Cleanup** — Remove closed markets with zero trades |
| 9 | **Exit** |
| 10 | **Reset Database** — Wipe and rebuild the schema (requires confirmation) |

## Live Paper Trader

The live trader connects to Polymarket's WebSocket order book feed and runs multiple strategy instances in parallel against real-time data.

### Controls

| Key | Action |
|-----|--------|
| `P` | Pause — disconnects WebSocket, wipes stale order book state |
| `R` | Resume — reconnects and resubscribes to all markets |
| `M` | Mute/unmute trade execution logs |
| `Ctrl+C` | Graceful shutdown — exports CSV ledger, then exits |

### Strategy Grid Search

Strategies are configured as a declarative grid in `PredictionLiveTrader/Program.cs`. Each parameter set is defined as an array, and LINQ Cartesian products generate all combinations automatically:

```csharp
decimal[] sniperThresholds = { 0.02m, 0.05m, 0.15m, 0.30m };
long[] sniperWindows = { 20, 30, 60 };

var sniperGrid = from threshold in sniperThresholds
                 from window in sniperWindows
                 select new { threshold, window };
```

Adding a new variation = adding one value to an array.

### Background Tasks

- **Performance dashboard** — Prints equity, PnL, and trade stats for every strategy every 15 minutes
- **Settlement sweeper** — Resolves closed markets against final payout prices every 15 minutes
- **Market discovery** — Discovers new markets crossing the $50k volume threshold and subscribes to their order books mid-session
- **Auto-export** — Saves the trade ledger to CSV on every sweep cycle and on shutdown

## Strategies

### Live (Order Book)

| Strategy | Description |
|----------|-------------|
| **LiveFlashCrashSniper** | Detects rapid price drops within a time window and buys the dip, selling on rebound |
| **LiveFlashCrashReverse** | Trend follower that shorts crashes via NO tokens, riding the momentum down |
| **OrderBookImbalance** | Scalps bid/ask depth imbalances (e.g., 5x more bids than asks) |
| **MeanReversionStatArb** | Z-score based mean reversion using a rolling price window |

### Backtest — Candle-Based

| Strategy | Description |
|----------|-------------|
| **RsiReversion** | RSI mean reversion, buys oversold and sells overbought |
| **SmaCrossover** | Fast/slow SMA crossover with volume filtering |
| **BollingerBreakout** | Trades breakouts from Bollinger Bands |
| **VolumeAnomaly** | Detects and trades volume spikes (5x+ average) |
| **ThetaDecay** | Targets sub-$0.10 "lottery ticket" outcomes with flatline detection |
| **HybridConfluence** | Combines RSI + SMA signals for confluence-based entries |
| **PureBuyNo** | Directional bias — buys the NO side once |

### Backtest — Tick-Based

| Strategy | Description |
|----------|-------------|
| **FlashCrashSniper** | Historical tick-level version of the live flash crash sniper |
| **DipBuying** | Simple dip buyer: buys below $0.40, sells above $0.60 |

## Architecture

### Data Flow

```
Polymarket API --> PolymarketClient --> Repository --> SQLite DB
                                                        |
                                              BacktestRunner / LiveTrader
                                                        |
                                                    Strategy
                                                        |
                                              SimulatedBroker / PaperBroker
                                                        |
                                                ExecutedTrade Ledger --> CSV
```

### Key Design Decisions

- **Omnidirectional positions** — YES and NO sides tracked independently per asset, allowing strategies to hold both simultaneously
- **Spread penalty** — A 1.5-cent spread penalty is applied inside the broker to simulate real market conditions
- **Thread-safe broker** — `GlobalSimulatedBroker` uses `ConcurrentDictionary` for positions and `lock` for cash/ledger mutations
- **Per-asset strategy instances** — Each asset gets its own strategy instance via factory functions, ensuring isolated internal state
- **Timestamp pagination** — The API client bypasses Polymarket's 3000-offset limit by shifting the `end_ts` parameter backward through time

### External APIs

| API | Base URL | Purpose |
|-----|----------|---------|
| Gamma | `https://gamma-api.polymarket.com/` | Market metadata and event discovery |
| CLOB | `https://clob.polymarket.com/` | Historical price ticks |
| Data | `https://data-api.polymarket.com/` | Raw trade data |
| WebSocket | `wss://ws-subscriptions-clob.polymarket.com/ws/market` | Real-time order book |

### Database

SQLite at `polymarket_backtest.db` with three tables: **Markets**, **Outcomes**, **Trades**. Indexed on `(OutcomeId, Timestamp)` for fast backtesting queries.

## Trade Analysis

After a live trading session, analyze the exported CSV with the included Python script:

```bash
python analyze_trades.py
```

This produces five dashboards:
1. **Strategy Leaderboard** — All strategies ranked by total PnL
2. **Parameter Impact** — Which threshold/window/take-profit values perform best on average
3. **Strategy Type Comparison** — Aggregate performance by strategy family
4. **Execution Balance** — Buy/sell/resolve breakdown per strategy (spot bag-holders)
5. **Top Traded Markets** — Most active markets by execution count

## Dependencies

| Package | Version | Used In |
|---------|---------|---------|
| Microsoft.EntityFrameworkCore.Sqlite | 10.0.3 | Data |
| Microsoft.Extensions.Http | 10.0.3 | Data, ConsoleApp, LiveTrader |

No third-party charting, ML, or analysis libraries — intentionally minimal.

## Domain Concepts

- **Market** — A prediction market (e.g., "Will X happen by Y date?")
- **Outcome** — Binary YES/NO side, identified by a `ClobTokenId`
- **ClobTokenIds[0]** is always the YES token, **ClobTokenIds[1]** is always the NO token
- **YES + NO prices sum to ~$1.00** — buying NO at $0.30 is equivalent to shorting YES at $0.70
- **Trade** — Tick-level data: price, size, timestamp, wallet address, side
- **Candle** — OHLCV aggregation computed from raw trades
- All financial values use `decimal` for precision
