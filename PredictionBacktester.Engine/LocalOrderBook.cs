using System;
using System.Collections.Generic;
using System.Linq;
using System.Text.Json;

namespace PredictionBacktester.Engine;

public class LocalOrderBook
{
    public string AssetId { get; private set; }

    // Dictionaries to hold Price -> Volume
    // SortedDictionary automatically keeps the prices ordered!
    public SortedDictionary<decimal, decimal> Bids { get; private set; }
    public SortedDictionary<decimal, decimal> Asks { get; private set; }

    public LocalOrderBook(string assetId)
    {
        AssetId = assetId;
        Bids = new SortedDictionary<decimal, decimal>();
        Asks = new SortedDictionary<decimal, decimal>();
    }

    // This method will process the live JSON arrays from the WebSocket
    public void ProcessBookUpdate(JsonElement bidsEl, JsonElement asksEl)
    {
        // --- PROCESS BIDS (Buyers) ---
        if (bidsEl.ValueKind == JsonValueKind.Array)
        {
            foreach (var bid in bidsEl.EnumerateArray())
            {
                decimal price = decimal.Parse(bid.GetProperty("price").GetString() ?? "0");
                decimal size = decimal.Parse(bid.GetProperty("size").GetString() ?? "0");

                if (size == 0)
                {
                    Bids.Remove(price); // Order was canceled or filled!
                }
                else
                {
                    Bids[price] = size; // Order was added or updated
                }
            }
        }

        // --- PROCESS ASKS (Sellers) ---
        if (asksEl.ValueKind == JsonValueKind.Array)
        {
            foreach (var ask in asksEl.EnumerateArray())
            {
                decimal price = decimal.Parse(ask.GetProperty("price").GetString() ?? "0");
                decimal size = decimal.Parse(ask.GetProperty("size").GetString() ?? "0");

                if (size == 0)
                {
                    Asks.Remove(price);
                }
                else
                {
                    Asks[price] = size;
                }
            }
        }
    }

    // Helper to instantly get the highest willing buyer
    public decimal GetBestBidPrice()
    {
        if (Bids.Count == 0) return 0.00m;
        return Bids.Keys.Max();
    }

    // Helper to instantly get the lowest willing seller
    public decimal GetBestAskPrice()
    {
        if (Asks.Count == 0) return 1.00m;
        return Asks.Keys.Min();
    }

    // Helper to check how many shares buyers actually want
    public decimal GetBestBidSize()
    {
        if (Bids.Count == 0) return 0.00m;
        return Bids[GetBestBidPrice()];
    }

    // Helper to check how many shares are actually available at the best price
    public decimal GetBestAskSize()
    {
        if (Asks.Count == 0) return 0.00m;
        return Asks[GetBestAskPrice()];
    }
}