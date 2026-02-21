using System;
using System.Collections.Generic;
using System.Linq;
using PredictionBacktester.Core.Entities;
using PredictionBacktester.Engine;

namespace PredictionBacktester.Strategies;

public class VolumeAnomalyStrategy : ICandleStrategy
{
    public TimeSpan Timeframe { get; }

    private readonly int _volumeSmaPeriod;
    private readonly decimal _volumeMultiplier;
    private readonly decimal _riskPercentage;
    private readonly decimal _takeProfitThreshold;
    private readonly decimal _stopLossPercentage;

    private readonly Queue<decimal> _volumeWindow;
    private decimal _previousClose;

    public VolumeAnomalyStrategy(
        TimeSpan timeframe,
        int volumeSmaPeriod = 48, // Compare to the last 48 hours of volume
        decimal volumeMultiplier = 5.0m, // The spike must be 5x the average volume
        decimal riskPercentage = 0.05m,
        decimal takeProfitThreshold = 0.85m,
        decimal stopLossPercentage = 0.20m)
    {
        Timeframe = timeframe;
        _volumeSmaPeriod = volumeSmaPeriod;
        _volumeMultiplier = volumeMultiplier;
        _riskPercentage = riskPercentage;
        _takeProfitThreshold = takeProfitThreshold;
        _stopLossPercentage = stopLossPercentage;

        _volumeWindow = new Queue<decimal>();
        _previousClose = 0m;
    }

    public void OnCandle(Candle candle, SimulatedBroker broker)
    {
        if (_previousClose == 0)
        {
            _previousClose = candle.Close;
            _volumeWindow.Enqueue(candle.Volume);
            return;
        }

        // Wait until we have enough history to know what "normal" volume is
        if (_volumeWindow.Count >= _volumeSmaPeriod)
        {
            ExecuteTradeLogic(candle, broker);
        }

        _volumeWindow.Enqueue(candle.Volume);
        if (_volumeWindow.Count > _volumeSmaPeriod) _volumeWindow.Dequeue();

        _previousClose = candle.Close;
    }

    private void ExecuteTradeLogic(Candle candle, SimulatedBroker broker)
    {
        decimal currentNoPrice = 1.00m - candle.Close;
        decimal currentEquity = broker.GetTotalPortfolioValue(candle.Close);
        decimal dollarsToInvest = Math.Min(currentEquity * _riskPercentage, broker.CashBalance);

        // Calculate normal volume
        decimal averageVolume = _volumeWindow.Count > 0 ? _volumeWindow.Average() : 0;

        // Is this a Smart Money anomaly?
        // We require the volume to be massive AND at least 1000 shares so we don't trigger on dead markets.
        bool isVolumeSpike = candle.Volume > (averageVolume * _volumeMultiplier) && candle.Volume > 1000m;

        // --- THE "YES" SIDE ---
        if (broker.PositionShares > 0)
        {
            bool isStopLoss = candle.Close <= (broker.AverageEntryPrice * (1m - _stopLossPercentage));

            if (candle.Close >= _takeProfitThreshold || isStopLoss)
                broker.SellAll(candle.Close, candle.Volume);
        }
        else if (isVolumeSpike && candle.Close > _previousClose && dollarsToInvest >= 1.00m)
        {
            // WHALE ALERT: Massive volume spike driving the price UP. Follow them!
            broker.Buy(candle.Close, dollarsToInvest, candle.Volume);
        }

        // --- THE "NO" SIDE ---
        if (broker.NoPositionShares > 0)
        {
            bool isNoStopLoss = currentNoPrice <= (broker.AverageNoEntryPrice * (1m - _stopLossPercentage));

            if (currentNoPrice >= _takeProfitThreshold || isNoStopLoss)
                broker.SellAllNo(candle.Close, candle.Volume);
        }
        else if (isVolumeSpike && candle.Close < _previousClose && dollarsToInvest >= 1.00m)
        {
            // WHALE ALERT: Massive volume spike driving the price DOWN. Follow them! (Buy NO)
            broker.BuyNo(candle.Close, dollarsToInvest, candle.Volume);
        }
    }
}