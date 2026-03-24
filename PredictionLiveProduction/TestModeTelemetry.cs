using System;
using System.Diagnostics;
using Serilog;

namespace PredictionLiveProduction;

/// <summary>
/// Records precise timestamps at every stage of the trading pipeline during --test mode.
/// Prints a full timing breakdown when the test trade completes.
/// </summary>
public static class TestModeTelemetry
{
    public static bool IsActive { get; set; }

    // Tracks whether the test cycle is complete (buy filled + sell filled)
    public static bool TestComplete { get; private set; }
    public static string? TestAssetId { get; private set; }

    private static readonly Stopwatch _sw = new();

    // Timestamps (ms from start of stopwatch)
    private static long _crashSpottedMs;
    private static long _sustainConfirmedMs;
    private static long _buySubmittedMs;
    private static long _buyFillDetectedMs;
    private static string? _buyFillSource;
    private static decimal _buyPrice;
    private static decimal _buyShares;
    private static long _settlementUnlockedMs;
    private static long _exitTriggeredMs;
    private static string? _exitReason;
    private static decimal _exitBidPrice;
    private static long _sellSubmittedMs;
    private static long _sellFillDetectedMs;
    private static string? _sellFillSource;
    private static decimal _sellPrice;
    private static decimal _sellShares;
    private static decimal _pnl;

    public static void CrashSpotted(string assetId, decimal bestAsk, decimal gap)
    {
        if (!IsActive || _crashSpottedMs > 0) return; // Only record first crash
        _sw.Start();
        _crashSpottedMs = _sw.ElapsedMilliseconds;
        TestAssetId = assetId;
        Log.Warning("[TEST] ====== CRASH SPOTTED ======");
        Log.Warning("[TEST] T+{Ms}ms | Asset: {Asset} | Ask: {Ask:0.000} | Gap: {Gap:0.000}",
            _crashSpottedMs, assetId[..Math.Min(12, assetId.Length)] + "...", bestAsk, gap);
    }

    public static void SustainConfirmed(long sustainedMs)
    {
        if (!IsActive) return;
        _sustainConfirmedMs = _sw.ElapsedMilliseconds;
        Log.Warning("[TEST] T+{Ms}ms | Crash sustained for {Sustained}ms — anti-spoof passed",
            _sustainConfirmedMs, sustainedMs);
    }

    public static void BuySubmitted(decimal price, decimal dollars)
    {
        if (!IsActive) return;
        _buySubmittedMs = _sw.ElapsedMilliseconds;
        Log.Warning("[TEST] T+{Ms}ms | BUY ORDER SUBMITTED | Price: {Price:0.000} | Dollars: {Dollars:0.00}",
            _buySubmittedMs, price, dollars);
    }

    public static void BuyFilled(decimal shares, decimal price, string fillSource)
    {
        if (!IsActive) return;
        _buyFillDetectedMs = _sw.ElapsedMilliseconds;
        _buyFillSource = fillSource;
        _buyPrice = price;
        _buyShares = shares;
        Log.Warning("[TEST] T+{Ms}ms | BUY FILLED | {Shares:0.00} shares @ {Price:0.000} | via {Source} | Latency: {Latency}ms",
            _buyFillDetectedMs, shares, price, fillSource, _buyFillDetectedMs - _buySubmittedMs);
    }

    public static void SettlementUnlocked()
    {
        if (!IsActive) return;
        _settlementUnlockedMs = _sw.ElapsedMilliseconds;
        Log.Warning("[TEST] T+{Ms}ms | Settlement lock released — exits now active",
            _settlementUnlockedMs);
    }

    public static void ExitTriggered(string reason, decimal bidPrice, decimal entryPrice)
    {
        if (!IsActive) return;
        _exitTriggeredMs = _sw.ElapsedMilliseconds;
        _exitReason = reason;
        _exitBidPrice = bidPrice;
        Log.Warning("[TEST] T+{Ms}ms | EXIT TRIGGERED: {Reason} | Bid: {Bid:0.000} | Entry: {Entry:0.000} | Hold time: {Hold}ms",
            _exitTriggeredMs, reason, bidPrice, entryPrice, _exitTriggeredMs - _buyFillDetectedMs);
    }

    public static void SellSubmitted()
    {
        if (!IsActive) return;
        _sellSubmittedMs = _sw.ElapsedMilliseconds;
        Log.Warning("[TEST] T+{Ms}ms | SELL ORDER SUBMITTED",
            _sellSubmittedMs);
    }

    public static void SellFilled(decimal shares, decimal price, string fillSource, decimal pnl)
    {
        if (!IsActive) return;
        _sellFillDetectedMs = _sw.ElapsedMilliseconds;
        _sellFillSource = fillSource;
        _sellPrice = price;
        _sellShares = shares;
        _pnl = pnl;
        Log.Warning("[TEST] T+{Ms}ms | SELL FILLED | {Shares:0.00} shares @ {Price:0.000} | via {Source} | Latency: {Latency}ms",
            _sellFillDetectedMs, shares, price, fillSource, _sellFillDetectedMs - _sellSubmittedMs);

        PrintSummary();
        TestComplete = true;
    }

    private static void PrintSummary()
    {
        long total = _sellFillDetectedMs - _crashSpottedMs;
        Log.Warning("[TEST] ====================================");
        Log.Warning("[TEST]        TEST TRADE COMPLETE");
        Log.Warning("[TEST] ====================================");
        Log.Warning("[TEST] Crash spotted        T+{Ms}ms", _crashSpottedMs);
        Log.Warning("[TEST] Sustain confirmed    T+{Ms}ms  (+{Delta}ms)", _sustainConfirmedMs, _sustainConfirmedMs - _crashSpottedMs);
        Log.Warning("[TEST] Buy submitted        T+{Ms}ms  (+{Delta}ms)", _buySubmittedMs, _buySubmittedMs - _sustainConfirmedMs);
        Log.Warning("[TEST] Buy filled           T+{Ms}ms  (+{Delta}ms)  via {Source}", _buyFillDetectedMs, _buyFillDetectedMs - _buySubmittedMs, _buyFillSource);
        Log.Warning("[TEST] Settlement unlocked  T+{Ms}ms  (+{Delta}ms)", _settlementUnlockedMs, _settlementUnlockedMs - _buyFillDetectedMs);
        Log.Warning("[TEST] Exit triggered       T+{Ms}ms  (+{Delta}ms)  reason: {Reason}", _exitTriggeredMs, _exitTriggeredMs - _settlementUnlockedMs, _exitReason);
        Log.Warning("[TEST] Sell submitted       T+{Ms}ms  (+{Delta}ms)", _sellSubmittedMs, _sellSubmittedMs - _exitTriggeredMs);
        Log.Warning("[TEST] Sell filled          T+{Ms}ms  (+{Delta}ms)  via {Source}", _sellFillDetectedMs, _sellFillDetectedMs - _sellSubmittedMs, _sellFillSource);
        Log.Warning("[TEST] ------------------------------------");
        Log.Warning("[TEST] Total pipeline:    {Total}ms", total);
        Log.Warning("[TEST] Buy:  {Shares:0.00} shares @ ${Price:0.000}", _buyShares, _buyPrice);
        Log.Warning("[TEST] Sell: {Shares:0.00} shares @ ${Price:0.000}", _sellShares, _sellPrice);
        Log.Warning("[TEST] PnL:  ${Pnl:0.0000}", _pnl);
        Log.Warning("[TEST] ====================================");
    }

    /// <summary>Reset all state for a fresh test run.</summary>
    public static void Reset()
    {
        _sw.Reset();
        _crashSpottedMs = 0;
        _sustainConfirmedMs = 0;
        _buySubmittedMs = 0;
        _buyFillDetectedMs = 0;
        _buyFillSource = null;
        _buyPrice = 0;
        _buyShares = 0;
        _settlementUnlockedMs = 0;
        _exitTriggeredMs = 0;
        _exitReason = null;
        _exitBidPrice = 0;
        _sellSubmittedMs = 0;
        _sellFillDetectedMs = 0;
        _sellFillSource = null;
        _sellPrice = 0;
        _sellShares = 0;
        _pnl = 0;
        TestComplete = false;
        TestAssetId = null;
    }
}
