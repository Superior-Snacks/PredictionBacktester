using System.Collections.Generic;
using System.Text.Json;
using System.Text.Json.Serialization;

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
    [JsonConverter(typeof(PolymarketStringArrayConverter))] // <-- ADD THIS
    public string[] Outcomes { get; set; }

    [JsonPropertyName("clobTokenIds")]
    [JsonConverter(typeof(PolymarketStringArrayConverter))] // <-- ADD THIS
    public string[] ClobTokenIds { get; set; }
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

public class PolymarketStringArrayConverter : JsonConverter<string[]>
{
    public override string[] Read(ref Utf8JsonReader reader, Type typeToConvert, JsonSerializerOptions options)
    {
        // 1. Check if Polymarket sent us a stringified array like "[\"Yes\", \"No\"]"
        if (reader.TokenType == JsonTokenType.String)
        {
            string jsonString = reader.GetString();
            if (string.IsNullOrWhiteSpace(jsonString)) return Array.Empty<string>();

            // Unpack the string back into a real JSON array
            return JsonSerializer.Deserialize<string[]>(jsonString) ?? Array.Empty<string>();
        }

        // 2. Fallback: If Polymarket ever fixes their API and sends a real array
        if (reader.TokenType == JsonTokenType.StartArray)
        {
            var list = new List<string>();
            while (reader.Read() && reader.TokenType != JsonTokenType.EndArray)
            {
                list.Add(reader.GetString());
            }
            return list.ToArray();
        }

        return Array.Empty<string>();
    }

    public override void Write(Utf8JsonWriter writer, string[] value, JsonSerializerOptions options)
    {
        JsonSerializer.Serialize(writer, value, options);
    }
}