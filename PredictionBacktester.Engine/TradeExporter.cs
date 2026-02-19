using System.Collections.Generic;
using System.IO;
using System.Text;

namespace PredictionBacktester.Engine;

public static class TradeExporter
{
    public static void ExportToCsv(List<ExecutedTrade> trades, string filePath)
    {
        var csv = new StringBuilder();

        // Write the Header Row
        csv.AppendLine("Date,MarketId,Side,Price,Shares,DollarValue");

        // Write every single trade
        foreach (var t in trades)
        {
            // Formatting dates and decimals so Excel reads them perfectly
            csv.AppendLine($"{t.Date:yyyy-MM-dd HH:mm:ss},{t.MarketId},{t.Side},{t.Price:F4},{t.Shares:F4},{t.DollarValue:F2}");
        }

        File.WriteAllText(filePath, csv.ToString());
    }
}