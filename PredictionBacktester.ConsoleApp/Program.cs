using Microsoft.EntityFrameworkCore;
using Microsoft.Extensions.DependencyInjection;
using PredictionBacktester.Data.ApiClients;
using PredictionBacktester.Data.Database;     // <-- ADD THIS
using PredictionBacktester.Data.Repositories;
using PredictionBacktester.Engine;
using System.Net.Http;


// 1. Setup Dependency Injection
var services = new ServiceCollection();

// Add the DbContext to our Dependency Injection container
services.AddDbContext<PolymarketDbContext>();

// Register our new Repository
services.AddTransient<PolymarketRepository>();

services.AddTransient<BacktestRunner>();

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

services.AddTransient<PolymarketClient>();

var serviceProvider = services.BuildServiceProvider();

var dbContext = serviceProvider.GetRequiredService<PolymarketDbContext>();
await dbContext.Database.MigrateAsync();

var apiClient = serviceProvider.GetRequiredService<PolymarketClient>();
var repository = serviceProvider.GetRequiredService<PolymarketRepository>();
var engine = serviceProvider.GetRequiredService<BacktestRunner>();

while (true)
{
    Console.WriteLine("\n=====================================");
    Console.WriteLine("    POLYMARKET BACKTESTER ENGINE     ");
    Console.WriteLine("=====================================");
    Console.WriteLine("1. Run Standard Ingestion (Sync New Markets)");
    Console.WriteLine("2. Run Deep Sync (Fix 3000+ Trade Markets)");
    Console.WriteLine("3. Run Strategy Backtest (Time Machine)");
    Console.WriteLine("4. Exit");
    Console.Write("\nSelect an option (1-4): ");

    var choice = Console.ReadLine();

    switch (choice)
    {
        case "1":
            await RunStandardIngestion(apiClient, repository);
            break;
        case "2":
            await RunDeepSync(apiClient, repository);
            break;
        case "3":
            Console.WriteLine("\n[Starting Simulation...]");
            await engine.RunMarketSimulationAsync("0xYOUR_REAL_MARKET_ID_HERE");
            break;
        case "4":
            Console.WriteLine("Exiting...");
            return;
        default:
            Console.WriteLine("Invalid option.");
            break;
    }
}

// ==========================================
// CLI COMMAND METHODS
// ==========================================

async Task RunStandardIngestion(PolymarketClient api, PolymarketRepository repo)
{
    Console.WriteLine("Fetching Polymarket Events...");
    int marketLimit = 100;
    int marketOffset = 2900;
    bool hasMoreMarkets = true;

    Console.WriteLine("Starting full exchange sync...");

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
}

async Task RunDeepSync(PolymarketClient api, PolymarketRepository repo)
{
    Console.WriteLine("\nScanning database for incomplete markets...");

    var incompleteMarkets = await repo.GetIncompleteMarketsAsync();
    Console.WriteLine($"Found {incompleteMarkets.Count} markets that need Deep Sync.");

    foreach (var kvp in incompleteMarkets)
    {
        string conditionId = kvp.Key;
        long oldestTimestamp = kvp.Value;

        Console.WriteLine($"\nDeep Syncing Market: {conditionId}");
        Console.WriteLine($"Fetching trades older than timestamp: {oldestTimestamp}...");

        // Call the new method!
        var olderTrades = await api.GetTradesBeforeTimestampAsync(conditionId, oldestTimestamp);

        if (olderTrades.Count > 0)
        {
            Console.WriteLine($"[SAVED] Recovered {olderTrades.Count} historical trades!");
            await repo.SaveTradesAsync(olderTrades);
        }
        else
        {
            Console.WriteLine($"[COMPLETE] No older trades found.");
        }

        await Task.Delay(200); // Rate Limit
    }

    Console.WriteLine("\nDeep Sync Finished.");
}
/*
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

// Register the engine
services.AddTransient<BacktestRunner>();

var serviceProvider = services.BuildServiceProvider();

// Resolve the engine
var engine = serviceProvider.GetRequiredService<BacktestRunner>();

// Pick a ConditionId that you saw successfully save in your PowerShell logs!
// (Replace this hash with the real ConditionId from your logs or database)
string testMarketId = "0xe099310e095cef92526d3410317f1254e1123584c54b5833fa6bbd2a903e2249";// mjög nýlegt kanski virkar ekki ef powershell running

await engine.RunMarketSimulationAsync(testMarketId);*/