using PredictionBacktester.Core.Entities;
using System.Net.Http;
using System.Net.Http.Json;
using System.Text.Json;
using System.Text.Json.Serialization;

namespace PredictionBacktester.Data.ApiClients;

public class PolymarketClient
{
    private readonly HttpClient _gammaClient;
    private readonly HttpClient _clobClient;
    private readonly HttpClient _dataClient;

    public PolymarketClient(IHttpClientFactory httpClientFactory)
    {
        _gammaClient = httpClientFactory.CreateClient("PolymarketGamma");
        _clobClient = httpClientFactory.CreateClient("PolymarketClob");
        _dataClient = httpClientFactory.CreateClient("PolymarketData");
    }

    /// <summary>
    /// Fetches a list of active events and their nested markets.
    /// </summary>
    public async Task<List<PolymarketEventResponse>> GetActiveEventsAsync(int limit = 100, int offset = 0)
    {
        // 1. Let's remove the 'active' and 'closed' filters temporarily to force it to give us ANYTHING
        var url = $"events?limit={limit}&offset={offset}";

        try
        {
            // 2. Instead of direct JSON conversion, let's download the raw string first
            var rawJson = await _gammaClient.GetStringAsync(url);

            // 3. Print the first 500 characters to the console so we can see what they sent us
            Console.WriteLine("\n--- RAW API RESPONSE ---");
            Console.WriteLine(rawJson.Substring(0, Math.Min(rawJson.Length, 500)) + "...\n");

            // 4. Now deserialize it manually
            var events = JsonSerializer.Deserialize<List<PolymarketEventResponse>>(rawJson);
            return events ?? new List<PolymarketEventResponse>();
        }
        catch (Exception ex)
        {
            // If Cloudflare blocks us or there is an HTTP error, this will catch it!
            Console.WriteLine($"\nAPI ERROR: {ex.Message}");
            return new List<PolymarketEventResponse>();
        }
    }

    public async Task<List<PolymarketTradeResponse>> GetTradesAsync(string conditionId)
    {
        // The Data API uses the conditionId to find all trades for a specific market
        var url = $"trades?market={conditionId}";

        try
        {
            var trades = await _dataClient.GetFromJsonAsync<List<PolymarketTradeResponse>>(url);
            return trades ?? new List<PolymarketTradeResponse>();
        }
        catch (Exception ex)
        {
            Console.WriteLine($"     API ERROR fetching trades: {ex.Message}");
            return new List<PolymarketTradeResponse>();
        }
    }

    /// <summary>
    /// Fetches the historical price data for a specific outcome token (e.g., the "Yes" share).
    /// </summary>
    public async Task<List<PolymarketTick>> GetPriceHistoryAsync(string clobTokenId)
    {
        // interval=max gets all history. fidelity=60 gets 1-hour intervals (candles).
        var url = $"prices-history?market={clobTokenId}&interval=max&fidelity=60";

        var response = await _clobClient.GetFromJsonAsync<PolymarketPriceHistoryResponse>(url);

        return response?.History ?? new List<PolymarketTick>();
    }
}