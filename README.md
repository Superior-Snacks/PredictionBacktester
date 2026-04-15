# PredictionBacktester

A full-stack prediction market trading system for **Polymarket** and **Kalshi**: backtesting engine, paper traders, live production executor, and cross-platform arbitrage monitor.

## Overview

Covers the full lifecycle from strategy research to live execution:
- **Backtesting** — replay historical Polymarket trade data against any strategy
- **Paper trading** — run strategies against live WebSocket order book data with simulated fills
- **Production trading** — real order execution via Polymarket CLOB API
- **Kalshi arbitrage** — detect and paper-trade categorical market arbs on Kalshi (buy all YES legs for < $1.00)
- **Cross-platform arbitrage** — monitor matching Kalshi ↔ Polymarket binary markets for price divergence

## Architecture

```
PredictionBacktester.Core          → Shared models, DTOs, database entities
PredictionBacktester.Data          → SQLite persistence, Polymarket API clients
PredictionBacktester.Engine        → Backtesting engine, brokers, order books
PredictionBacktester.Strategies    → Trading strategy implementations
PredictionBacktester.ConsoleApp    → Interactive CLI for backtesting
PredictionLiveTrader               → Polymarket paper trading over live WebSocket data
PredictionLiveProduction           → Polymarket real money trading via CLOB API
KalshiPaperTrader                  → Kalshi categorical + binary arb paper trader
KalshiPolyCross                    → Cross-platform Kalshi ↔ Polymarket arb monitor
```

Built on .NET 10.0. EF Core + SQLite for data persistence.

## Quick Start

### Prerequisites
- [.NET 10.0 SDK](https://dotnet.microsoft.com/download)
- Python 3.10+ (for analysis scripts)

### Build & Run

```bash
dotnet build

# Backtesting CLI (historical data)
dotnet run --project PredictionBacktester.ConsoleApp

# Polymarket paper trader (live WebSocket, simulated orders)
dotnet run --project PredictionLiveTrader

# Polymarket production trader (real money — requires credentials)
dotnet run --project PredictionLiveProduction

# Kalshi arb paper trader
dotnet run --project KalshiPaperTrader

# Cross-platform Kalshi ↔ Polymarket arb monitor
dotnet run --project KalshiPolyCross
```

Database auto-migrates on first run (`polymarket_backtest.db`).

### Environment Variables

```bash
# Polymarket
export POLY_PRIVATE_KEY=<hex-private-key>
export POLY_PROXY_ADDRESS=<proxy-wallet-address>
export POLY_API_KEY=<clob-api-key>
export POLY_API_SECRET=<clob-api-secret>
export POLY_API_PASSPHRASE=<clob-api-passphrase>

# Kalshi
export KALSHI_API_KEY_ID=<uuid>
export KALSHI_PRIVATE_KEY_PATH=<path/to/rsa-key.pem>
```

---

## Trading Modes

### Backtesting (ConsoleApp)

Interactive CLI with 10 menu options: ingest historical trade data, run single-market or portfolio backtests, grid-search strategy parameters, export results to CSV.

### Polymarket Paper Trading (PredictionLiveTrader)

Connects to Polymarket's WebSocket feed. Runs multiple strategy configurations simultaneously with simulated fills.

**Features:**
- Cartesian grid search over strategy parameters
- GZip market data recording to `MarketData/` for offline replay
- Settlement sweeper auto-resolves closed markets every 15 minutes, discovers new markets (>$50k volume)
- CSV trade ledger export (auto-saved every 15 min + on shutdown)

**Live controls:**

| Key | Action |
|-----|--------|
| P | Pause (disconnect WebSocket) |
| R | Resume (reconnect) |
| M | Mute/unmute trade logs |
| Q | Quiet mode |
| V | Verbose book updates |
| L | Toggle latency simulation |
| D | Drop a strategy by name |
| K | Cull N worst performers |

### Polymarket Production Trading (PredictionLiveProduction)

Real order execution through Polymarket's CLOB API with EIP-712 wallet signing (POLY_GNOSIS_SAFE), risk controls, and Serilog structured logging.

### Kalshi Arb Paper Trader (KalshiPaperTrader)

Monitors all open Kalshi categorical markets for arbitrage opportunities: cases where buying all YES legs of a mutually-exclusive event costs less than $1.00 (the guaranteed payout).

**How it works:**
1. Scans all open Kalshi events via REST, keeps mutually-exclusive categorical events
2. Applies blocklist (`event_blocklist.json`) to exclude non-exhaustive series where all legs can resolve NO
3. Subscribes to real-time WebSocket order book updates for all legs
4. Continuously evaluates: `net cost = Σ(leg asks) + Σ(fees)` where fee = `0.07 × P × (1−P)` per contract
5. Arb fires when net cost < $1.00 with ≥ $0.02 profit after fees and sufficient depth
6. Logs every arb window (whether executed or not) to `ArbTelemetry_*.csv` for analysis

**Break-even gross cost thresholds (before fees):**

| Legs | Break-even gross |
|------|-----------------|
| 3    | ~$0.954         |
| 5    | ~$0.944         |
| 6    | ~$0.943         |

**Analyze results:**
```bash
python analyze_kalshi_arb.py                   # analyze latest CSV
python analyze_kalshi_arb.py --3               # filter events settling >3 months out
python analyze_kalshi_arb.py --include EPL,NBA # sports only
python analyze_kalshi_arb.py --exclude trump   # exclude by term
python analyze_kalshi_arb.py --clean           # non-flagged rows only
```

The analyzer fetches real resolution outcomes from the Kalshi API to validate actual win/loss performance.

### Cross-Platform Arb Monitor (KalshiPolyCross)

Monitors matching binary markets on both Kalshi and Polymarket simultaneously. When the same real-world event is priced differently across platforms, buying YES on one and NO on the other can cost less than $1.00.

- Sports-only filter on both sides
- Two-pass title matching to pair equivalent markets; auto-saves unambiguous pairs to `cross_pairs.json`
- Arb types: `K_YES_P_NO` or `K_NO_P_YES`
- Logs windows to `CrossArbTelemetry_*.csv`

---

## Strategies

| Strategy | Interface | Description |
|----------|-----------|-------------|
| **FlashCrashSniper** | ITickStrategy | Buys sharp price drops, exits on rebound |
| **LiveFlashCrashSniper** | ILiveStrategy | Real-time crash detection with anti-spoofing sustain timer and settlement lock |
| **LiveFlashCrashReverse** | ILiveStrategy | Short crashes (buy NO side) rather than catching dips |
| **PolymarketCategoricalArb** | ILiveStrategy | Multi-leg categorical arb: buy all YES legs when net cost < $1.00 |
| **FastMergeArbTelemetry** | ILiveStrategy | Telemetry-only variant — logs every arb window to CSV without executing |
| **RsiReversion** | ICandleStrategy | RSI-based mean reversion (oversold/overbought thresholds) |
| **CandleSmaCrossover** | ICandleStrategy | Golden/death cross entries on OHLCV candles |
| **BollingerBreakout** | ICandleStrategy | Buy below lower band, short above upper band |
| **HybridConfluence** | ICandleStrategy | RSI signal + SMA trend confirmation required |
| **ThetaDecay** | ICandleStrategy | Sells dead markets: buys NO when price flatlines ≤ $0.10 |
| **MeanReversionStatArb** | ILiveStrategy | Z-score reversion: buys 2 stddev below rolling mean |
| **OrderBookImbalance** | ILiveStrategy | Fires when bid volume > 5× ask volume (order book wall) |
| **VolumeAnomaly** | ICandleStrategy | Buys when volume spikes 5× rolling average |
| **PureBuyNo** | ICandleStrategy | Systematic NO-side buy-and-hold baseline |
| **DipBuying** | ITickStrategy | Dollar-cost averaging on dips |

### Strategy Interfaces

```csharp
interface ITickStrategy   { void OnTick(Trade trade, SimulatedBroker broker); }
interface ICandleStrategy { TimeSpan Timeframe { get; }
                            void OnCandle(Candle candle, SimulatedBroker broker); }
interface ILiveStrategy   { void OnBookUpdate(LocalOrderBook book, GlobalSimulatedBroker broker); }
```

---

## APIs

### Polymarket

| API | Base URL | Purpose |
|-----|----------|---------|
| Gamma | `gamma-api.polymarket.com` | Market metadata, event listings |
| CLOB | `clob.polymarket.com` | Order book snapshots, order placement |
| Data | `data-api.polymarket.com` | Historical trade data |
| WebSocket | `wss://ws-subscriptions-clob.polymarket.com/ws/market` | Real-time order book deltas |

Auth: EIP-712 signed orders with POLY_GNOSIS_SAFE (`signature_type=2`).

### Kalshi

| API | Base URL | Purpose |
|-----|----------|---------|
| REST | `api.elections.kalshi.com/trade-api/v2` | Events, markets, balance, orders, series |
| WebSocket | `api.elections.kalshi.com/trade-api/ws/v2` | Real-time market deltas |

Auth: RSA-PSS signature. Message: `{timestampMs}{METHOD}{path}`. Headers: `KALSHI-ACCESS-KEY`, `KALSHI-ACCESS-TIMESTAMP`, `KALSHI-ACCESS-SIGNATURE`.

---

## Python Analysis Scripts

| Script | Purpose |
|--------|---------|
| `analyze_kalshi_arb.py` | Full Kalshi arb session analysis: fraud checks, resolution lookup, production sim |
| `ping_kalshi.py` | Latency benchmark vs Kalshi (DNS, TCP, TLS, REST, WS, ICMP) |
| `fetch_token_id.py` | Look up Polymarket token IDs by market name |
| `time_trades.py` | Measure real settlement latency on Polymarket |
| `analyze_trades.py` | Post-trade analysis and visualization |
| `analyze_realistic_trades.py` | Spread modeling and realistic fill simulation |
| `heatmap.py` | Strategy parameter optimization heatmap |
| `check_proxy.py` | Validate Polymarket proxy wallet |
| `approve_eoa.py` | Approve EOA wallet for CLOB trading |
| `verify_sig.py` | EIP-712 signature verification |
| `test_order.py` | Test order placement |
| `check_kalshi_books.py` | Inspect Kalshi order book snapshots |
| `test_kalshi_auth.py` | Verify Kalshi API authentication |

---

## Project Structure

```
├── PredictionBacktester.Core/
│   └── Entities/               # DTOs, DB models (Market, Outcome, Trade), Candle
├── PredictionBacktester.Data/
│   ├── ApiClients/             # Polymarket REST client
│   ├── Database/               # EF Core DbContext
│   └── Repositories/           # Data access layer
├── PredictionBacktester.Engine/
│   ├── LiveExecution/          # PolymarketLiveBroker, OrderClient (EIP-712)
│   ├── GlobalSimulatedBroker.cs
│   ├── SimulatedBroker.cs
│   ├── LocalOrderBook.cs       # WS delta accumulator
│   └── BacktestRunner.cs
├── PredictionBacktester.Strategies/
│   ├── FastMergeArbTelemetryStrategy.cs
│   ├── PolymarketCategoricalArbStrategy.cs
│   └── (13 other strategy files)
├── PredictionBacktester.ConsoleApp/
│   └── Program.cs              # Interactive CLI
├── PredictionLiveTrader/
│   ├── Program.cs
│   ├── PaperBroker.cs
│   └── MarketReplayLogger.cs
├── PredictionLiveProduction/
│   ├── Program.cs
│   └── ProductionBroker.cs
├── KalshiPaperTrader/
│   ├── Program.cs              # Main arb loop
│   └── KalshiPaperBroker.cs
├── KalshiPolyCross/
│   ├── Program.cs              # Market matcher + dual WS loop
│   └── CrossPlatformArbTelemetryStrategy.cs
├── event_blocklist.json        # Auto-updated non-exhaustive series blocklist
├── cross_pairs.json            # Verified Kalshi ↔ Polymarket market pairs
├── MarketData/                 # Recorded WebSocket data (.gz)
├── analyze_kalshi_arb.py       # Main Kalshi arb analyzer
└── *.py                        # Other Python utility scripts
```

## Tech Stack

- **.NET 10.0** — Core framework
- **EF Core + SQLite** — Data persistence with auto-migrations
- **Nethereum** — Ethereum EIP-712 signing for Polymarket CLOB orders
- **RestSharp** — HTTP client for Polymarket CLOB API
- **Serilog** — Structured logging (production)
- **Python** — Post-session analysis, API utilities, latency testing
