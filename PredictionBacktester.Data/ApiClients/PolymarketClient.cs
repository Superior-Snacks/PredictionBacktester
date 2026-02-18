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

    /// <summary>
    /// Fetches older trades using dynamic Timestamp Pagination to infinitely bypass the offset limit.
    /// </summary>
    public async Task<List<PolymarketTradeResponse>> GetTradesBeforeTimestampAsync(string conditionId, long beforeTimestamp)
    {
        var allTrades = new List<PolymarketTradeResponse>();
        int limit = 500;
        int offset = 0;

        long currentTimestampLimit = beforeTimestamp;

        while (true)
        {
            // 1. Changed '&before=' to '&end_ts=' (Polymarket's actual undocumented parameter)
            var url = $"trades?market={conditionId}&limit={limit}&offset={offset}&end_ts={currentTimestampLimit}";

            try
            {
                var batch = await _dataClient.GetFromJsonAsync<List<PolymarketTradeResponse>>(url);
                if (batch == null || batch.Count == 0) break;

                allTrades.AddRange(batch);
                offset += limit;

                if (offset >= 3000)
                {
                    offset = 0;
                    long oldestInBatch = batch.Min(t => t.Timestamp);

                    // 2. THE SAFETY BREAK: If time didn't move backward, the API rejected our filter!
                    if (oldestInBatch >= currentTimestampLimit)
                    {
                        Console.WriteLine("     [API LIMIT] Server ignored timestamp. Hard cap reached for this market.");
                        break; // Kill the loop instantly
                    }

                    // 3. Subtract 1 second so we don't fetch the exact same trade again
                    currentTimestampLimit = oldestInBatch - 1;

                    Console.WriteLine($"     [SHIFT] Reset offset. Shifting time window to end_ts={currentTimestampLimit}...");
                }

                if (batch.Count < limit) break;

                await Task.Delay(100);
            }
            catch (Exception ex)
            {
                Console.WriteLine($"     API ERROR at offset {offset}: {ex.Message}");
                break;
            }
        }

        return allTrades;
    }
}