# PredictionBacktester TODOs

## Critical Bugs
- [ ] **Concurrency Crash Risk**: In `FastMergeArbTelemetryStrategy.cs`, `_tokenToEventMap` is a standard `Dictionary` but is read without a lock in `OnBookUpdate` (the hot path). Change to `ConcurrentDictionary<string, string>`.
- [ ] **State Corruption Risk**: In `ProductionBroker.SaveState`, writing directly to `bot_state.json` can corrupt the file if the bot crashes mid-write. Implement atomic file writes (write to `bot_state.json.tmp` first, then use `File.Move(..., overwrite: true)`).

## Performance & Latency (High Priority)
- [ ] **GC Pressure in Hot Path**: In `FastMergeArbTelemetryStrategy.EvaluateArbitrageTelemetry`, string concatenation (`currentLegTickers += ...`) allocates thousands of short-lived string objects per second. Switch to `List<string>` and `string.Join("|", ...)` to reduce Gen 0 garbage collections and latency spikes.

## Architecture & Maintenance
- [ ] **Refactor `Program.cs` God Object**: Separate the Polymarket and Kalshi WebSocket client loops into dedicated handler classes (e.g., `KalshiWebsocketFeed`, `PolymarketWebsocketFeed`).
- [ ] **Improve WebSocket Buffer**: Replace the static 64KB buffer (`new byte[65536]`) in `Program.cs` with `System.Buffers.ArrayPool<byte>.Shared.Rent()` to reduce memory allocation overhead on large orderbook snapshots.
- [ ] **State Management**: Encapsulate static state dictionaries (`books`, `yesSizes`, `noSizes`) from `Program.cs` into a dedicated `MarketStateTracker` class.

## Domain Logic
- [ ] **Fee Model Mismatch**: `FastMergeArbTelemetryStrategy` uses the Polymarket dynamic fee formula (`fee = p * _feeRate * Math.Pow(...)`). Kalshi uses a different fee structure. Update the fee calculation to accurately reflect Kalshi's quadratic or flat fee schedules.
- [ ] **Race Condition in Arb Updates**: `FastMergeArbTelemetryStrategy` updates properties directly on instances inside `_activeArbs` (e.g., `currentArb.BestGrossCost = ...`). If two WebSocket threads hit this simultaneously, telemetry can be corrupted. Apply appropriate locking or atomic replacements for these updates.

## Market Matching (LLM Integration)
- [ ] **Coarse Filtering (C#)**: Replace the current `Pass 1/Pass 2` regex logic in `Program.cs` with a fast keyword-overlap filter to narrow down the 4,000+ markets per platform to a short list of high-probability candidates.
- [ ] **Semantic Matching (Gemini API)**: Iterate through the coarse-filtered lists and call the Gemini API via Google AI Studio to pick the exact semantic match.
- [ ] **Rate Limiting & Persistence**: Implement a `Task.Delay(4000)` between API calls to stay within the free tier (15 RPM) and auto-save the high-confidence matches directly to `cross_pairs.json`.