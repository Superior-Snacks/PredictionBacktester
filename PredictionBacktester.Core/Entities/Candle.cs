namespace PredictionBacktester.Core.Entities;

public class Candle
{
    public long OpenTimestamp { get; set; }
    public decimal Open { get; set; }
    public decimal High { get; set; }
    public decimal Low { get; set; }
    public decimal Close { get; set; }
    public decimal Volume { get; set; }
}