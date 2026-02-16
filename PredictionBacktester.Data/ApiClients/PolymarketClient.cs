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

    public PolymarketClient(IHttpClientFactory httpClientFactory)
    {
        // We inject a factory that gives us pre-configured clients for both APIs
        _gammaClient = httpClientFactory.CreateClient("PolymarketGamma");
        _clobClient = httpClientFactory.CreateClient("PolymarketClob");
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