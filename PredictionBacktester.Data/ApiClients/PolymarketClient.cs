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

    /// <summary>
    /// Fetches ALL raw, tick-level trades for a specific market using pagination.
    /// </summary>
    public async Task<List<PolymarketTradeResponse>> GetAllTradesAsync(string conditionId)
    {
        var allTrades = new List<PolymarketTradeResponse>();
        int limit = 500; // Pull 500 trades per request
        int offset = 0;

        while (true)
        {
            // We append both limit and offset to the URL
            var url = $"trades?market={conditionId}&limit={limit}&offset={offset}";

            try
            {
                var batch = await _dataClient.GetFromJsonAsync<List<PolymarketTradeResponse>>(url);

                // If the API returns null or an empty list, we've reached the end!
                if (batch == null || batch.Count == 0)
                {
                    break;
                }

                allTrades.AddRange(batch);
                offset += limit; // Move the cursor forward by 500 for the next loop

                // If the API gave us less than 500 trades, we know it was the final page
                if (batch.Count < limit)
                {
                    break;
                }

                // RATE LIMITING: Pause for 100 milliseconds before asking for the next page.
                // This ensures we only make 10 requests per second, keeping us safely under the limit.
                await Task.Delay(100);
            }
            catch (Exception ex)
            {
                Console.WriteLine($"     API ERROR fetching trades at offset {offset}: {ex.Message}");
                break; // Stop looping if the API throws an error
            }
        }

        return allTrades;
    }
}