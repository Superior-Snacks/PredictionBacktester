using System;

namespace PredictionBacktester.Engine;

public class ExecutedTrade
{
    public string MarketId { get; set; }
    public DateTime Date { get; set; }
    public string Side { get; set; }
    public decimal Price { get; set; }
    public decimal Shares { get; set; }
    public decimal DollarValue { get; set; }
}