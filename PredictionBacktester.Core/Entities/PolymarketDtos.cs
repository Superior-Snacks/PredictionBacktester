using System.Text.Json.Serialization;
using System.Collections.Generic;

namespace PredictionBacktester.Core.Entities;

// --- GAMMA API MODELS (Market Metadata) ---

public class PolymarketEventResponse
{
    [JsonPropertyName("id")]
    public string Id { get; set; }

    [JsonPropertyName("title")]
    public string Title { get; set; }

    [JsonPropertyName("markets")]
    public List<PolymarketMarketResponse> Markets { get; set; }
}

public class PolymarketMarketResponse
{
    [JsonPropertyName("id")]
    public string Id { get; set; }

    [JsonPropertyName("question")]
    public string Question { get; set; }

    [JsonPropertyName("outcomes")]
    public string[] Outcomes { get; set; } // e.g., ["Yes", "No"]

    [JsonPropertyName("clobTokenIds")]
    public string[] ClobTokenIds { get; set; } // The ID needed to get the price history!
}

// --- CLOB API MODELS (Historical Prices) ---

public class PolymarketPriceHistoryResponse
{
    [JsonPropertyName("history")]
    public List<PolymarketTick> History { get; set; }
}

public class PolymarketTick
{
    [JsonPropertyName("t")]
    public long Timestamp { get; set; } // Unix timestamp

    [JsonPropertyName("p")]
    public decimal Price { get; set; }
}