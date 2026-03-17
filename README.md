# PredictionBacktester

A full-stack Polymarket prediction market trading system with backtesting, paper trading, and live production execution.

## Overview

This system lets you develop, backtest, paper-trade, and deploy trading strategies on [Polymarket](https://polymarket.com) — a decentralized prediction market on Polygon. It connects to Polymarket's CLOB (Central Limit Order Book) via REST APIs and WebSocket for real-time order book data.

## Architecture

```
PredictionBacktester.Core          → Shared models, DTOs, database entities
PredictionBacktester.Data          → SQLite persistence, Polymarket API clients
PredictionBacktester.Engine        → Backtesting engine, brokers, order books
PredictionBacktester.Strategies    → 15 trading strategies
PredictionBacktester.ConsoleApp    → Interactive CLI for backtesting
PredictionLiveTrader               → Paper trading over live WebSocket data
PredictionLiveProduction           → Real money trading via CLOB API
```

Built on .NET 10.0 with EF Core + SQLite for data persistence.

## Quick Start

### Prerequisites
- [.NET 10.0 SDK](https://dotnet.microsoft.com/download)
- Python 3.10+ (for utility scripts)

### Build & Run

```bash
# Build everything
dotnet build

# Run the backtesting CLI
dotnet run --project PredictionBacktester.ConsoleApp

# Run the paper trader (live WebSocket data, simulated orders)
dotnet run --project PredictionLiveTrader

# Run the production trader (real money — requires API credentials)
dotnet run --project PredictionLiveProduction
```

The database auto-migrates on first run.

## Trading Modes

### 1. Backtesting (ConsoleApp)

Interactive CLI with 10 menu options:
- Ingest historical trade data from Polymarket APIs
- Run single-market or portfolio backtests
- Grid-search strategy parameters
- Export results to CSV

### 2. Paper Trading (PredictionLiveTrader)

Connects to Polymarket's WebSocket feed for real-time order book data. Runs multiple strategy configurations simultaneously with simulated execution.

**Features:**
- Cartesian grid search over strategy parameters
- Live keyboard controls (Pause/Resume/Mute/Drop/Cull)
- Settlement sweeper auto-resolves closed markets every 15 minutes
- Automatic new market discovery (>$50k volume threshold)
- Market data recording to compressed `.gz` files for replay
- Latency simulation for realistic fill modeling
- CSV trade ledger export (auto-saved + on shutdown)

**Controls:**
| Key | Action |
|-----|--------|
| P | Pause (disconnects WebSocket) |
| R | Resume (reconnects) |
| M | Mute/unmute trade logs |
| Q | Quiet mode (silence all output) |
| V | Verbose book updates |
| L | Toggle latency simulation |
| D | Drop a strategy by name |
| K | Cull N worst performers |

### 3. Production Trading (PredictionLiveProduction)

Real order execution through Polymarket's CLOB API with:
- EIP-712 wallet signing (POLY_GNOSIS_SAFE)
- Risk controls and position limits
- Serilog structured logging
- Daily loss tracking with auto-pause

## Strategies

| Strategy | Type | Description |
|----------|------|-------------|
| FlashCrashSniper | Tick | Buys sharp price drops, sells on rebound |
| LiveFlashCrashSniper | Live | Real-time crash detection with anti-spoofing timer and settlement lock |
| LiveFlashCrashReverse | Live | Reverse flash crash (sells into spikes) |
| PolymarketCategoricalArb | Live | Multi-leg arbitrage across categorical market outcomes |
| RsiReversion | Tick | RSI-based mean reversion |
| CandleSmaCrossover | Candle | SMA crossover on OHLCV candles |
| BollingerBreakout | Candle | Bollinger Band breakout entries |
| HybridConfluence | Candle | Multi-indicator confluence |
| ThetaDecay | Live | Time-decay exploitation near market expiry |
| MeanReversionStatArb | Tick | Statistical arbitrage mean reversion |
| OrderBookImbalance | Live | Order book imbalance detection |
| VolumeAnomaly | Tick | Volume spike detection |
| PureBuyNo | Live | Systematic NO-side buying |
| DipBuying | Tick | Dollar-cost averaging on dips |

### Strategy Interfaces

```csharp
// Process every raw trade
interface ITickStrategy : IStrategy {
    void OnTick(Trade trade, SimulatedBroker broker);
}

// Process OHLCV candles (auto-aggregated)
interface ICandleStrategy : IStrategy {
    int Timeframe { get; }
    void OnCandle(Candle candle, SimulatedBroker broker);
}

// Process real-time order book snapshots
interface ILiveStrategy : IStrategy {
    void OnBookUpdate(LocalOrderBook book, GlobalSimulatedBroker broker);
}
```

## Python Utilities

```bash
# Find token IDs for any Polymarket market
python fetch_token_id.py "Bitcoin"

# Measure real settlement delay on Polymarket
python time_trades.py

# Monitor API latency
python ping.py
```

## Configuration

### Environment Variables (Production)

```
POLY_PRIVATE_KEY=<your-private-key>
POLY_PROXY_ADDRESS=<your-proxy-wallet>
POLY_API_KEY=<clob-api-key>
POLY_API_SECRET=<clob-api-secret>
POLY_API_PASSPHRASE=<clob-api-passphrase>
```

### Polymarket APIs Used

| API | Base URL | Purpose |
|-----|----------|---------|
| Gamma | `gamma-api.polymarket.com` | Market metadata, event listings |
| CLOB | `clob.polymarket.com` | Order book, order placement |
| Data | `data-api.polymarket.com` | Historical trades |
| WebSocket | `wss://ws-subscriptions-clob.polymarket.com/ws/market` | Real-time book updates |

## Project Structure

```
├── PredictionBacktester.Core/
│   └── Entities/            # DTOs, database models, candle types
├── PredictionBacktester.Data/
│   ├── ApiClients/          # Polymarket REST client
│   ├── Database/            # EF Core DbContext
│   └── Repositories/       # Data access layer
├── PredictionBacktester.Engine/
│   ├── LiveExecution/       # PolymarketLiveBroker, OrderClient
│   ├── GlobalSimulatedBroker.cs
│   ├── SimulatedBroker.cs
│   ├── LocalOrderBook.cs
│   └── BacktestRunner.cs
├── PredictionBacktester.Strategies/
│   └── (15 strategy files)
├── PredictionBacktester.ConsoleApp/
│   └── Program.cs           # Interactive CLI
├── PredictionLiveTrader/
│   ├── Program.cs           # Paper trading engine
│   ├── PaperBroker.cs       # Simulated broker with production constraints
│   └── MarketReplayLogger.cs # WebSocket data recorder
├── PredictionLiveProduction/
│   ├── Program.cs           # Production trading engine
│   └── ProductionBroker.cs  # Real CLOB execution
├── MarketData/              # Recorded WebSocket data (.gz)
├── *.py                     # Python utility scripts
└── publish/                 # Release binaries
```

## Tech Stack

- **.NET 10.0** — Core framework
- **EF Core + SQLite** — Data persistence with auto-migrations
- **Nethereum** — Ethereum EIP-712 signing for CLOB orders
- **RestSharp** — HTTP client for CLOB API
- **Serilog** — Structured logging (production)
- **Python** — Utility scripts for analysis and API testing
