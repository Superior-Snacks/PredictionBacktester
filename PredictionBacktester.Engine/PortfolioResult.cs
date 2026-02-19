namespace PredictionBacktester.Engine;

public class PortfolioResult
{
    // Now it can hold 2, 5, or 100 parameters!
    public decimal[] Parameters { get; set; }

    public decimal TotalReturn { get; set; }
    public decimal WinRate { get; set; }
    public int TotalTrades { get; set; }
}