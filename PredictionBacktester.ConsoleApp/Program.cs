using Microsoft.Extensions.DependencyInjection;
using PredictionBacktester.Data.ApiClients;
using System.Net.Http;

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

services.AddHttpClient("PolymarketData", client =>
{
    client.BaseAddress = new Uri("https://data-api.polymarket.com/");
});

// Register our custom client
services.AddTransient<PolymarketClient>();

var serviceProvider = services.BuildServiceProvider();

// 2. Resolve the client and test the API
var apiClient = serviceProvider.GetRequiredService<PolymarketClient>();

Console.WriteLine("Fetching Polymarket Events...");
var events = await apiClient.GetActiveEventsAsync(limit: 2); // Just pulling 2 for a quick test
Console.WriteLine(1);
Console.WriteLine(events);
foreach (var ev in events)
{
    Console.WriteLine(1.5);
    Console.WriteLine($"\nEvent: {ev.Title}");
    Console.WriteLine(2);

    foreach (var market in ev.Markets)
    {
        Console.WriteLine($"  -> Question: {market.Question}");
        Console.WriteLine(3);

        // Check if the market has a conditionId
        if (!string.IsNullOrEmpty(market.ConditionId))
        {
            Console.WriteLine(4);
            Console.WriteLine($"     Fetching RAW TRADES for market...");

            // Call the new Data API endpoint!
            var trades = await apiClient.GetTradesAsync(market.ConditionId);

            Console.WriteLine($"     Got {trades.Count} raw trades.");
            if (trades.Count > 0)
            {
                // Let's print out the exact wallet that made the first trade in the list
                var t = trades.First();
                Console.WriteLine($"     Example Trade: Wallet {t.ProxyWallet} {t.Side} {t.Size} shares at ${t.Price}");
            }
        }
    }
}