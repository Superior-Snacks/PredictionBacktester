using Microsoft.Extensions.DependencyInjection;
using PredictionBacktester.Data.ApiClients;

// 1. Setup Dependency Injection
var services = new ServiceCollection();

// Configure the Gamma Client base URL
services.AddHttpClient("PolymarketGamma", client =>
{
    client.BaseAddress = new Uri("https://gamma-api.polymarket.com/");
});

// Configure the CLOB Client base URL
services.AddHttpClient("PolymarketClob", client =>
{
    client.BaseAddress = new Uri("https://clob.polymarket.com/");
});

// Register our custom client
services.AddTransient<PolymarketClient>();

var serviceProvider = services.BuildServiceProvider();

// 2. Resolve the client and test the API
var apiClient = serviceProvider.GetRequiredService<PolymarketClient>();

Console.WriteLine("Fetching Polymarket Events...");
var events = await apiClient.GetActiveEventsAsync(limit: 2); // Just pulling 2 for a quick test

foreach (var ev in events)
{
    Console.WriteLine($"\nEvent: {ev.Title}");

    foreach (var market in ev.Markets)
    {
        Console.WriteLine($"  -> Question: {market.Question}");

        // Let's grab the price history for the first outcome (usually "Yes")
        if (market.ClobTokenIds != null && market.ClobTokenIds.Length > 0)
        {
            string firstOutcomeId = market.ClobTokenIds[0];
            string firstOutcomeName = market.Outcomes[0];

            Console.WriteLine($"     Fetching history for outcome '{firstOutcomeName}'...");
            var history = await apiClient.GetPriceHistoryAsync(firstOutcomeId);

            Console.WriteLine($"     Got {history.Count} historical price ticks.");
            if (history.Count > 0)
            {
                Console.WriteLine($"     Latest Price: {history.Last().Price}");
            }
        }
    }
}