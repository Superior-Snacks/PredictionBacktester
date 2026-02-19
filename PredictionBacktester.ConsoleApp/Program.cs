using Microsoft.EntityFrameworkCore;
using Microsoft.Extensions.DependencyInjection;
using PredictionBacktester.Core.Entities.Database;
using PredictionBacktester.Data.ApiClients;
using PredictionBacktester.Data.Database;     // <-- ADD THIS
using PredictionBacktester.Data.Repositories;
using PredictionBacktester.Engine;
using PredictionBacktester.Strategies;

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
    Console.WriteLine("4. Explore Market & Trade Data");
    Console.WriteLine("5. Explore Live API Data (Raw JSON)");
    Console.WriteLine("6. Run Portfolio Backtest (Dynamic Multi-Market)");
    Console.WriteLine("7. RunUniversalOptimizer");
    Console.WriteLine("8. exit");
    Console.Write("\nSelect an option (1-8): ");

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
            Console.Write("Enter Market ID to analyze: ");
            string targetMarketId = Console.ReadLine();

            // Your exact #1 RSI parameters from the Leaderboard!
            // Params: [ Period: 7 | Oversold: 40 | Overbought: 60 | TakeProfit: 0.85 | Risk: 0.05 ]
            IStrategy microscopeStrategy = new RsiReversionStrategy(TimeSpan.FromHours(1), 7, 40m, 60m, 0.05m, 24, 10000m, 0.85m);

            DateTime start = new DateTime(2024, 7, 1);
            DateTime end = new DateTime(2024, 11, 1);

            // Run it with a clean $1,000 wallet
            await engine.RunMarketSimulationAsync(targetMarketId, start, end, microscopeStrategy, 1000m);
            break;
        case "4":
            // Pass the database context directly to our new viewer
            await ExploreMarketData(dbContext);
            break;
        case "5":
            await ExploreLiveApiData(apiClient);
            break;
        case "6":
            await RunDynamicPortfolioBacktest(repository, engine);
            break;
        case "7":
            // 1. Define the levers for RSI
            decimal[] rsiPeriods = { 7, 10, 14 }; // How fast the rubber band reacts
            decimal[] oversoldLevels = { 20, 30, 40 }; // How deep the panic needs to be to buy YES
            decimal[] overboughtLevels = { 60, 70, 80 }; // How high the greed needs to be to buy NO
            decimal[] takeProfits = { 0.85m }; // Locking this in based on previous findings!
            decimal[] riskPcts = { 0.05m }; // Locking this in too!

            decimal[][] rsiGrid = { rsiPeriods, oversoldLevels, overboughtLevels, takeProfits, riskPcts };

            // 2. Map the array to the RSI Constructor!
            // combo[0] = Period
            // combo[1] = Oversold
            // combo[2] = Overbought
            // combo[3] = Take Profit
            // combo[4] = Risk
            Func<decimal[], IStrategy> rsiBuilder = (combo) =>
                new RsiReversionStrategy(TimeSpan.FromHours(1), (int)combo[0], combo[1], combo[2], combo[4], 24, 10000m, combo[3]);

            await RunUniversalOptimizer(repository, engine, rsiGrid, rsiBuilder, "RSI Mean Reversion (5D)");
            break;
        case "8":
            Console.WriteLine("Exiting...");
            return;
        default:
            Console.WriteLine("Invalid option.");
            break;
    }
}

// This math magic takes [[1, 2], [A, B], [X, Y]] and generates:
// [1, A, X], [1, A, Y], [1, B, X], [1, B, Y]...
List<decimal[]> GenerateCombinations(decimal[][] arrays)
{
    var result = new List<decimal[]> { Array.Empty<decimal>() };
    foreach (var array in arrays)
    {
        var temp = new List<decimal[]>();
        foreach (var existingCombo in result)
        {
            foreach (var item in array)
            {
                var newCombo = new decimal[existingCombo.Length + 1];
                existingCombo.CopyTo(newCombo, 0);
                newCombo[newCombo.Length - 1] = item;
                temp.Add(newCombo);
            }
        }
        result = temp;
    }
    return result;
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

async Task ExploreMarketData(PolymarketDbContext db)
{
    Console.WriteLine("\n--- DATA EXPLORER ---");
    Console.Write("Enter keyword to search (or press Enter for a random market): ");
    var search = Console.ReadLine()?.Trim();

    Market market = null;

    if (string.IsNullOrEmpty(search))
    {
        // Pick a random market directly inside the SQLite engine
        market = await db.Markets
            .Include(m => m.Outcomes)
            .OrderBy(m => EF.Functions.Random())
            .FirstOrDefaultAsync();
    }
    else
    {
        // Search for a market title containing the keyword (Case-Insensitive)
        market = await db.Markets
            .Include(m => m.Outcomes)
            .Where(m => EF.Functions.Like(m.Title, $"%{search}%"))
            .FirstOrDefaultAsync();
    }

    if (market == null)
    {
        Console.WriteLine("\n[ERROR] No market found matching that criteria.");
        return;
    }

    // --- DISPLAY MARKET INFO ---
    Console.WriteLine($"\n=========================================");
    Console.WriteLine($"MARKET: {market.Title}");
    Console.WriteLine($"MARKET ID: {market.MarketId}");
    Console.WriteLine($"OUTCOMES: {market.Outcomes.Count}");
    Console.WriteLine($"=========================================");

    foreach (var outcome in market.Outcomes)
    {
        Console.WriteLine($" - {outcome.OutcomeName ?? "Outcome"} (ID: {outcome.OutcomeId})");
    }

    // --- FIND TRADES ---
    var outcomeIds = market.Outcomes.Select(o => o.OutcomeId).ToList();
    var totalTrades = await db.Trades.CountAsync(t => outcomeIds.Contains(t.OutcomeId));

    Console.WriteLine($"\nTotal Trades in Database: {totalTrades}");

    if (totalTrades == 0)
    {
        Console.WriteLine("No trades saved for this market yet. Returning to menu...");
        return;
    }

    // --- FETCH SPECIFIC TRADE ---
    Console.Write($"\nEnter the trade number you want to view (1 to {totalTrades}): ");
    if (int.TryParse(Console.ReadLine(), out int tradeIndex) && tradeIndex > 0 && tradeIndex <= totalTrades)
    {
        var specificTrade = await db.Trades
            .Where(t => outcomeIds.Contains(t.OutcomeId))
            .OrderBy(t => t.Timestamp)
            .Skip(tradeIndex - 1) // If they want trade 10, skip the first 9
            .Take(1)              // And take exactly 1
            .FirstOrDefaultAsync();

        if (specificTrade != null)
        {
            var date = DateTimeOffset.FromUnixTimeSeconds(specificTrade.Timestamp).DateTime;
            Console.WriteLine($"\n--- TRADE #{tradeIndex} ---");
            Console.WriteLine($"Date:   {date} UTC");
            Console.WriteLine($"Price:  ${specificTrade.Price}");
            Console.WriteLine($"Shares: {specificTrade.Size}");
            Console.WriteLine($"Side:   {specificTrade.Side}"); // "BUY" or "SELL" if you track it
        }
    }
    else
    {
        Console.WriteLine("\n[ERROR] Invalid trade number. Returning to menu.");
    }
}

async Task ExploreLiveApiData(PolymarketClient api)
{
    Console.WriteLine("\n--- LIVE API EXPLORER ---");
    Console.WriteLine("Fetching a fresh batch of live events from Polymarket...");

    // 1. Fetch the first 50 active events directly from the API
    var events = await api.GetActiveEventsAsync(limit: 50, offset: 50000);
    if (events == null || events.Count == 0)
    {
        Console.WriteLine("\n[ERROR] Failed to fetch from API.");
        return;
    }

    // 2. Pick a random event, and grab its first valid market
    var random = new Random();
    var randomEvent = events[random.Next(events.Count)];
    var randomMarket = randomEvent.Markets?.FirstOrDefault(m => !string.IsNullOrEmpty(m.ConditionId));

    if (randomMarket == null)
    {
        Console.WriteLine("\n[SKIPPED] Random event had no valid markets. Try hitting 5 again!");
        return;
    }

    // 3. Setup JSON formatting options for pretty-printing
    var jsonOptions = new System.Text.Json.JsonSerializerOptions { WriteIndented = true };

    Console.WriteLine($"\n[1/2] RAW MARKET JSON (Direct from Gamma API)");
    Console.WriteLine("======================================================");
    string marketJson = System.Text.Json.JsonSerializer.Serialize(randomMarket, jsonOptions);
    Console.WriteLine(marketJson);

    Console.WriteLine("\nFetching recent trades for this market...");

    // 4. Fetch the trades from the API using the ConditionId
    var trades = await api.GetAllTradesAsync(randomMarket.ConditionId);

    if (trades != null && trades.Count > 0)
    {
        Console.WriteLine($"\n[2/2] RAW TRADE JSON (Trade 1 of {trades.Count} fetched)");
        Console.WriteLine("======================================================");
        string tradeJson = System.Text.Json.JsonSerializer.Serialize(trades.First(), jsonOptions);
        Console.WriteLine(tradeJson);
    }
    else
    {
        Console.WriteLine("\n[EMPTY] No recent trades found for this market.");
    }
}

async Task RunDynamicPortfolioBacktest(PolymarketRepository repo, BacktestRunner engine)
{
    Console.WriteLine("\n--- PORTFOLIO BACKTEST SETUP ---");

    // 1. Get Date Parameters from the User (with easy defaults)
    Console.Write("Enter Start Date (YYYY-MM-DD) [Default: 2024-07-01]: ");
    var startInput = Console.ReadLine();
    DateTime startDate = string.IsNullOrWhiteSpace(startInput) ? new DateTime(2024, 7, 1) : DateTime.Parse(startInput);

    Console.Write("Enter End Date (YYYY-MM-DD) [Default: 2024-11-01]: ");
    var endInput = Console.ReadLine();
    DateTime endDate = string.IsNullOrWhiteSpace(endInput) ? new DateTime(2024, 11, 1) : DateTime.Parse(endInput);

    Console.WriteLine($"\nScanning database for markets active between {startDate.ToShortDateString()} and {endDate.ToShortDateString()}...");

    // 2. Fetch the dynamic list from SQLite!
    var dynamicMarketIds = await repo.GetActiveMarketsInDateRangeAsync(startDate, endDate);

    if (dynamicMarketIds.Count == 0)
    {
        Console.WriteLine("\n[ERROR] No markets found with trades in that date range. Try expanding your dates!");
        return;
    }

    Console.WriteLine($"Found {dynamicMarketIds.Count} active markets! Initializing Time Machine...\n");

    // Timeframe: 1 Hour
    // Fast SMA: 5
    // Slow SMA: 25
    // Risk: 2%
    // Volume Window: 24 Hours
    // Min Volume: $10,000
    // Take Profit: $0.90
    IStrategy myStrategy = new CandleSmaCrossoverStrategy(TimeSpan.FromHours(1), 7, 15, 0.05m, 24, 100m, 0.85m);

    // 4. Run the Portfolio Engine
    await engine.RunPortfolioSimulationAsync(dynamicMarketIds, startDate, endDate, myStrategy);
}

async Task RunUniversalOptimizer(
    PolymarketRepository repo,
    BacktestRunner engine,
    decimal[][] parameterSpace,
    Func<decimal[], IStrategy> strategyFactory,
    string strategyName,
    decimal initialAllocationPerMarket = 1000m)
{
    Console.WriteLine($"\n--- N-DIMENSIONAL OPTIMIZER: {strategyName} ---");

    DateTime startDate = new DateTime(2024, 7, 1);
    DateTime endDate = new DateTime(2024, 11, 1);
    var outcomeIds = await repo.GetActiveOutcomesInDateRangeAsync(startDate, endDate);
    // 1. Generate every possible combination of your parameters
    var allCombinations = GenerateCombinations(parameterSpace);
    int totalTests = allCombinations.Count;

    Console.WriteLine($"Generated {totalTests} unique parameter combinations to test. Starting engine...\n");

    var results = new List<PortfolioResult>();
    int currentTest = 1;

    // 2. Loop through the generated combinations
    foreach (var combo in allCombinations)
    {
        // Print the combo to the screen so you know what's running
        string comboString = string.Join(", ", combo.Select(p => p.ToString("0.##")));
        Console.Write($"Testing [{currentTest}/{totalTests}] - Params: [{comboString}]... ");

        // 3. Ask the factory to build the strategy with this specific combo array!
        IStrategy strategy = strategyFactory(combo);

        var result = await engine.RunPortfolioSimulationAsync(outcomeIds, startDate, endDate, strategy, isSilent: true, initialAllocationPerMarket);

        result.Parameters = combo; // Save the array to the scorecard!
        results.Add(result);

        Console.WriteLine($"Return: {result.TotalReturn:F2}%, Win: {result.WinRate:F2}%");
        currentTest++;
    }

    // 4. PRINT THE LEADERBOARD
    Console.WriteLine("\n=========================================");
    Console.WriteLine($"          {strategyName.ToUpper()} LEADERBOARD       ");
    Console.WriteLine("=========================================");

    var topResults = results.OrderByDescending(r => r.TotalReturn).Take(5).ToList();

    for (int i = 0; i < topResults.Count; i++)
    {
        var r = topResults[i];
        string bestComboStr = string.Join(" | ", r.Parameters.Select(p => $"{p,5:0.##}"));
        Console.WriteLine($"#{i + 1} | Params: [{bestComboStr}] | Return: {r.TotalReturn:F2}% | Win: {r.WinRate:F2}% | Trades: {r.TotalTrades}");
    }
    Console.WriteLine("=========================================");
}

async Task RunActiveMarketSync(PolymarketClient api, PolymarketRepository repo)
{
    Console.WriteLine("\n--- SYNCING ACTIVE MARKETS ---");

    // 1. Ask the database who is still alive
    var openMarkets = await repo.GetOpenMarketIdsAsync();
    Console.WriteLine($"Found {openMarkets.Count} open markets in the database. Checking for new trades...");

    foreach (var conditionId in openMarkets)
    {
        Console.WriteLine($"\nSyncing Market: {conditionId}");

        // 2. Fetch missing trades since our last known timestamp
        long? newestKnown = await repo.GetNewestTradeTimestampAsync(conditionId);
        long searchTimestamp = newestKnown ?? 0; // If it's somehow 0, fetch from the beginning

        var missingTrades = await api.GetRecentTradesUntilAsync(conditionId, searchTimestamp);

        if (missingTrades.Count > 0)
        {
            await repo.SaveTradesAsync(missingTrades);
            Console.WriteLine($"   [UPDATED] Downloaded {missingTrades.Count} new trades.");
        }
        else
        {
            Console.WriteLine("   [UP TO DATE] No new volume.");
        }

        // 3. THE CLEANUP: Did it close while we were away?
        bool isNowClosed = await api.IsMarketClosedAsync(conditionId);
        if (isNowClosed)
        {
            await repo.MarkMarketClosedAsync(conditionId);
            Console.WriteLine("   [CLOSED] Market has officially resolved. State saved.");
        }

        await Task.Delay(100); // Respect the API rate limits!
    }

    Console.WriteLine("\n[COMPLETE] All active markets have been synced.");
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

//ok I need to be able to set paramiters for the stratergies, ammount of markets tested, time range tested, volume filtering and so on