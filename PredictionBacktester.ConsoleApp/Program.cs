using Microsoft.EntityFrameworkCore;
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

var dbContext = serviceProvider.GetRequiredService<PolymarketDbContext>();
await dbContext.Database.MigrateAsync();

var apiClient = serviceProvider.GetRequiredService<PolymarketClient>();
var repository = serviceProvider.GetRequiredService<PolymarketRepository>();

Console.WriteLine("Fetching Polymarket Events...");
int marketLimit = 100;
int marketOffset = 2900;
bool hasMoreMarkets = true;

Console.WriteLine("Starting full exchange sync...");
/*
// --- THE NEW OUTER LOOP ---
while (hasMoreMarkets)
{
    Console.WriteLine($"\n--- Fetching Events Page (Offset: {marketOffset}) ---");

    // Fetch a page of 100 events
    var events = await apiClient.GetActiveEventsAsync(limit: marketLimit, offset: marketOffset);

    // If the API gives us an empty list, we've downloaded the entire exchange!
    if (events == null || events.Count == 0)
    {
        Console.WriteLine("\n[FINISHED] No more markets to download. Sync complete!");
        hasMoreMarkets = false;
        break;
    }

    foreach (var ev in events)
    {
        if (ev.Markets == null)
        {
            continue;
        }
        foreach (var market in ev.Markets)
        {
            if (!string.IsNullOrEmpty(market.ConditionId))
            {
                bool wasNewMarket = await repository.SaveMarketAsync(market);

                if (wasNewMarket)
                {
                    Console.WriteLine($"     [SAVED] New Market: {market.Question}");

                    var trades = await apiClient.GetAllTradesAsync(market.ConditionId);

                    if (trades.Count > 0)
                    {
                        Console.WriteLine($"     [SAVED] {trades.Count} total trades.");
                        await repository.SaveTradesAsync(trades);
                    }
                    else
                    {
                        Console.WriteLine($"     [EMPTY] No trades found.");
                    }

                    // Protect our IP Address
                    await Task.Delay(200);
                }
                else
                {
                    Console.WriteLine($"     [SKIPPED] Market already exists: {market.Question}");
                }
            }
        }
    }

    // Move the cursor forward to get the next 100 events on the next loop
    marketOffset += marketLimit;
}
*/
