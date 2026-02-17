using Microsoft.Extensions.DependencyInjection;
using PredictionBacktester.Data.ApiClients;
using PredictionBacktester.Data.Database;     // <-- ADD THIS
using PredictionBacktester.Data.Repositories;
using System.Net.Http;


// 1. Setup Dependency Injection
var services = new ServiceCollection();

// Add the DbContext to our Dependency Injection container
services.AddDbContext<PolymarketDbContext>();

// Register our new Repository
services.AddTransient<PolymarketRepository>();

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

var apiClient = serviceProvider.GetRequiredService<PolymarketClient>();
var repository = serviceProvider.GetRequiredService<PolymarketRepository>();

Console.WriteLine("Fetching Polymarket Events...");
var events = await apiClient.GetActiveEventsAsync(limit: 2); // Just pulling 2 for a quick test
foreach (var ev in events)
{
    Console.WriteLine($"\nEvent: {ev.Title}");

    foreach (var market in ev.Markets)
    {
        Console.WriteLine($"  -> Question: {market.Question}");

        if (!string.IsNullOrEmpty(market.ConditionId))
        {
            Console.WriteLine($"     Saving Market: {market.Question}");

            // 1. Save the Market and its Outcomes to SQLite
            await repository.SaveMarketAsync(market);

            Console.WriteLine($"     Fetching RAW TRADES...");
            var trades = await apiClient.GetTradesAsync(market.ConditionId);

            if (trades.Count > 0)
            {
                Console.WriteLine($"     Saving {trades.Count} trades to database...");

                // 2. Save the Trades to SQLite
                await repository.SaveTradesAsync(trades);
            }
        }
    }
}

//spurja hversu marigir eru pulled og hernig ég reseta db