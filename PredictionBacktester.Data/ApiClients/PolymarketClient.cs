using System.Net.Http.Json;
using PredictionBacktester.Core.Entities;
using System.Net.Http;

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
    public async Task<List<PolymarketEventResponse>> GetActiveEventsAsync(int limit = 100, int offset = 10000)
    {
        var url = $"events?closed=false&active=true&limit={limit}&offset={offset}";

        // GetFromJsonAsync handles the HTTP GET request and JSON deserialization in one line
        var events = await _gammaClient.GetFromJsonAsync<List<PolymarketEventResponse>>(url);

        return events ?? new List<PolymarketEventResponse>();
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