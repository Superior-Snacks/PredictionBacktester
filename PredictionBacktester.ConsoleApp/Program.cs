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
var events = await apiClient.GetActiveEventsAsync();
foreach (var ev in events)
{
    Console.WriteLine($"\nEvent: {ev.Title}");

    foreach (var market in ev.Markets)
    {
        Console.WriteLine($"  -> Question: {market.Question}");

        if (!string.IsNullOrEmpty(market.ConditionId))
        {
            // 1. Try to save the market and capture the result
            bool wasNewMarket = await repository.SaveMarketAsync(market);

            if (wasNewMarket)
            {
                Console.WriteLine($"     [SAVED] New Market: {market.Question}");
                Console.WriteLine($"     Fetching ALL RAW TRADES (Paginated)...");

                // 1. Call the new Paginated Method
                var trades = await apiClient.GetAllTradesAsync(market.ConditionId);

                if (trades.Count > 0)
                {
                    Console.WriteLine($"     [SAVED] {trades.Count} total trades to database.");
                    await repository.SaveTradesAsync(trades);
                }
                else
                {
                    Console.WriteLine($"     [EMPTY] No trades found for this market.");
                }

                // 2. RATE LIMITING: Pause for 200 milliseconds before processing the next market
                await Task.Delay(200);
            }
            else
            {
                Console.WriteLine($"     [SKIPPED] Market already exists in database: {market.Question}");
            }
        }
    }
}

//spurja hversu marigir eru pulled og hernig ég reseta db