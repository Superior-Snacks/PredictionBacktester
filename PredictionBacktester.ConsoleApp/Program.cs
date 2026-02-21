using Microsoft.EntityFrameworkCore;
using Microsoft.Extensions.DependencyInjection;
using PredictionBacktester.Core.Entities.Database;
using PredictionBacktester.Data.ApiClients;
using PredictionBacktester.Data.Database;
using PredictionBacktester.Data.Repositories;
using PredictionBacktester.Engine;
using PredictionBacktester.Strategies;
using System.Net.Http;

// ==========================================
// 1. SETUP DEPENDENCY INJECTION
// ==========================================
var services = new ServiceCollection();

services.AddDbContext<PolymarketDbContext>();
services.AddTransient<PolymarketRepository>();
services.AddTransient<BacktestRunner>();
services.AddTransient<PolymarketClient>();

services.AddHttpClient("PolymarketGamma", client => { client.BaseAddress = new Uri("https://gamma-api.polymarket.com/"); });
services.AddHttpClient("PolymarketClob", client => { client.BaseAddress = new Uri("https://clob.polymarket.com/"); });
services.AddHttpClient("PolymarketData", client => { client.BaseAddress = new Uri("https://data-api.polymarket.com/"); });

var serviceProvider = services.BuildServiceProvider();

var dbContext = serviceProvider.GetRequiredService<PolymarketDbContext>();
await dbContext.Database.MigrateAsync();

var apiClient = serviceProvider.GetRequiredService<PolymarketClient>();
var repository = serviceProvider.GetRequiredService<PolymarketRepository>();
var engine = serviceProvider.GetRequiredService<BacktestRunner>();

// ==========================================
// 2. MAIN CLI LOOP
// ==========================================
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
    Console.WriteLine("7. Run Universal Optimizer");
    Console.WriteLine("8. Exit");
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
            // --- LEVERS FOR CASE 3 ---
            DateTime case3Start = new DateTime(2024, 7, 1);
            DateTime case3End = new DateTime(2024, 11, 1);
            decimal startingCapital = 1000m;
            IStrategy microscopeStrategy = new RsiReversionStrategy(TimeSpan.FromHours(1), 7, 40m, 60m, 0.05m, 24, 10000m, 0.85m);

            Console.Write("Enter OutcomeID to analyze: ");
            string targetMarketId = Console.ReadLine()?.Trim();

            if (!string.IsNullOrEmpty(targetMarketId))
            {
                await engine.RunMarketSimulationAsync(targetMarketId, case3Start, case3End, microscopeStrategy, startingCapital);
            }
            break;

        case "4":
            Console.WriteLine("\n--- REVERSE LOOKUP ---");
            Console.Write("Enter an Outcome ID to identify it (or press Enter to search by name): ");
            var outcomeSearch = Console.ReadLine()?.Trim();

            if (!string.IsNullOrEmpty(outcomeSearch) && outcomeSearch.Length > 20)
            {
                var foundOutcome = await dbContext.Outcomes
                    .Include(o => o.Market)
                    .FirstOrDefaultAsync(o => o.OutcomeId == outcomeSearch);

                if (foundOutcome != null)
                {
                    Console.WriteLine($"\n[TARGET IDENTIFIED]");
                    Console.WriteLine($"Market: {foundOutcome.Market?.Title}");
                    Console.WriteLine($"Outcome: {foundOutcome.OutcomeName}");
                    break;
                }
            }
            await ExploreMarketData(dbContext);
            break;

        case "5":
            await ExploreLiveApiData(apiClient);
            break;

        case "6":
            // --- LEVERS FOR CASE 6 ---
            DateTime case6Start = new DateTime(2024, 7, 1);
            DateTime case6End = new DateTime(2024, 11, 1);
            string case6Keyword = "Bitcoin"; // e.g., "Bitcoin", "Trump", "NFL"
            Func<IStrategy> case6StrategyFactory = () => new HybridConfluenceStrategy(TimeSpan.FromHours(1), 10, 50, 40m, 80m, 0.05m, 24, 10000m, 0.85m);

            await RunDynamicPortfolioBacktest(repository, engine, case6Start, case6End, case6Keyword, case6StrategyFactory);
            break;

        case "7":
            // --- LEVERS FOR CASE 7 ---
            DateTime case7Start = new DateTime(2024, 7, 1);
            DateTime case7End = new DateTime(2024, 11, 1);
            string case7Keyword = "";
            // 1. Define the Levers for Smart Money Volume Tracker
            decimal[] volumePeriods = { 24, 48, 72 }; // How far back defines "normal" volume?
            decimal[] volumeMultipliers = { 3.0m, 5.0m, 10.0m }; // How massive must the spike be? (3x, 5x, 10x)
            decimal[] takeProfits = { 0.85m };
            decimal[] riskPcts = { 0.05m };
            decimal[] stopLosses = { 0.15m, 0.25m }; // Keep a tight leash in case it's a fakeout

            decimal[][] volumeGrid = { volumePeriods, volumeMultipliers, takeProfits, riskPcts, stopLosses };

            Func<decimal[], IStrategy> volumeBuilder = (combo) =>
                new VolumeAnomalyStrategy(TimeSpan.FromHours(1), (int)combo[0], combo[1], combo[3], combo[2], combo[4]);

            // Feel free to test this on "Bitcoin", "Election", or just the whole exchange!
            // var outcomeIds = await repo.GetActiveOutcomesInDateRangeAsync(startDate, endDate);
            await RunUniversalOptimizer(repository, engine, volumeGrid, volumeBuilder, case7Start, case7End, case7Keyword, "Hybrid Confluence (SMA + RSI)");
            break;

        case "8":
            Console.WriteLine("Exiting...");
            return;

        default:
            Console.WriteLine("Invalid option.");
            break;
    }
}

// ==========================================
// UTILITY METHODS
// ==========================================

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
    int marketOffset = 30000;
    bool hasMoreMarkets = true;

    Console.WriteLine("Starting full exchange sync...");

    while (hasMoreMarkets)
    {
        Console.WriteLine($"\n--- Fetching Events Page (Offset: {marketOffset}) ---");
        var events = await apiClient.GetActiveEventsAsync(limit: marketLimit, offset: marketOffset);

        if (events == null || events.Count == 0)
        {
            Console.WriteLine("\n[FINISHED] No more markets to download. Sync complete!");
            hasMoreMarkets = false;
            break;
        }

        foreach (var ev in events)
        {
            if (ev.Markets == null) continue;

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
                        await Task.Delay(200);
                    }
                    else
                    {
                        Console.WriteLine($"     [SKIPPED] Market already exists: {market.Question}");
                    }
                }
            }
        }
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
        await Task.Delay(200);
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
        market = await db.Markets
            .Include(m => m.Outcomes)
            .OrderBy(m => EF.Functions.Random())
            .FirstOrDefaultAsync();
    }
    else
    {
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

    Console.WriteLine($"\n=========================================");
    Console.WriteLine($"MARKET: {market.Title}");
    Console.WriteLine($"MARKET ID: {market.MarketId}");
    Console.WriteLine($"OUTCOMES: {market.Outcomes.Count}");
    Console.WriteLine($"=========================================");

    foreach (var outcome in market.Outcomes)
    {
        Console.WriteLine($" - {outcome.OutcomeName ?? "Outcome"} (ID: {outcome.OutcomeId})");
    }

    var outcomeIds = market.Outcomes.Select(o => o.OutcomeId).ToList();
    var totalTrades = await db.Trades.CountAsync(t => outcomeIds.Contains(t.OutcomeId));

    Console.WriteLine($"\nTotal Trades in Database: {totalTrades}");

    if (totalTrades == 0) return;

    Console.Write($"\nEnter the trade number you want to view (1 to {totalTrades}): ");
    if (int.TryParse(Console.ReadLine(), out int tradeIndex) && tradeIndex > 0 && tradeIndex <= totalTrades)
    {
        var specificTrade = await db.Trades
            .Where(t => outcomeIds.Contains(t.OutcomeId))
            .OrderBy(t => t.Timestamp)
            .Skip(tradeIndex - 1)
            .Take(1)
            .FirstOrDefaultAsync();

        if (specificTrade != null)
        {
            var date = DateTimeOffset.FromUnixTimeSeconds(specificTrade.Timestamp).DateTime;
            Console.WriteLine($"\n--- TRADE #{tradeIndex} ---");
            Console.WriteLine($"Date:   {date} UTC");
            Console.WriteLine($"Price:  ${specificTrade.Price}");
            Console.WriteLine($"Shares: {specificTrade.Size}");
            Console.WriteLine($"Side:   {specificTrade.Side}");
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
    var events = await api.GetActiveEventsAsync(limit: 50, offset: 50000);
    if (events == null || events.Count == 0)
    {
        Console.WriteLine("\n[ERROR] Failed to fetch from API.");
        return;
    }

    var random = new Random();
    var randomEvent = events[random.Next(events.Count)];
    var randomMarket = randomEvent.Markets?.FirstOrDefault(m => !string.IsNullOrEmpty(m.ConditionId));

    if (randomMarket == null) return;

    var jsonOptions = new System.Text.Json.JsonSerializerOptions { WriteIndented = true };

    Console.WriteLine($"\n[1/2] RAW MARKET JSON");
    Console.WriteLine(System.Text.Json.JsonSerializer.Serialize(randomMarket, jsonOptions));

    var trades = await api.GetAllTradesAsync(randomMarket.ConditionId);

    if (trades != null && trades.Count > 0)
    {
        Console.WriteLine($"\n[2/2] RAW TRADE JSON (Trade 1 of {trades.Count} fetched)");
        Console.WriteLine(System.Text.Json.JsonSerializer.Serialize(trades.First(), jsonOptions));
    }
}

async Task RunDynamicPortfolioBacktest(PolymarketRepository repo, BacktestRunner engine, DateTime startDate, DateTime endDate, string domainKeyword, Func<IStrategy> strategyFactory)
{
    Console.WriteLine("\n--- PORTFOLIO BACKTEST SETUP ---");
    Console.WriteLine($"Scanning database for '{domainKeyword}' outcomes active between {startDate.ToShortDateString()} and {endDate.ToShortDateString()}...");

    var outcomeIds = await repo.GetActiveOutcomesInDateRangeAsync(startDate, endDate, domainKeyword);

    if (outcomeIds.Count == 0)
    {
        Console.WriteLine("\n[ERROR] No outcomes found with trades in that date range. Try expanding your dates!");
        return;
    }

    Console.WriteLine($"Found {outcomeIds.Count} active outcomes! Initializing Time Machine...\n");
    await engine.RunPortfolioSimulationAsync(outcomeIds, startDate, endDate, strategyFactory);
}

async Task RunUniversalOptimizer(
    PolymarketRepository repo,
    BacktestRunner engine,
    decimal[][] parameterSpace,
    Func<decimal[], IStrategy> strategyFactory,
    DateTime startDate,
    DateTime endDate,
    string domainKeyword,
    string strategyName,
    decimal initialAllocationPerMarket = 1000m)
{
    Console.WriteLine($"\n--- N-DIMENSIONAL OPTIMIZER: {strategyName} ---");

    var outcomeIds = await repo.GetActiveOutcomesInDateRangeAsync(startDate, endDate, domainKeyword);

    var allCombinations = GenerateCombinations(parameterSpace);
    int totalTests = allCombinations.Count;

    Console.WriteLine($"Generated {totalTests} unique parameter combinations to test. Starting engine...\n");

    var results = new List<PortfolioResult>();
    int currentTest = 1;

    foreach (var combo in allCombinations)
    {
        string comboString = string.Join(", ", combo.Select(p => p.ToString("0.##")));
        Console.Write($"Testing [{currentTest}/{totalTests}] - Params: [{comboString}]... ");

        Func<IStrategy> portfolioFactory = () => strategyFactory(combo);

        var result = await engine.RunPortfolioSimulationAsync(outcomeIds, startDate, endDate, portfolioFactory, isSilent: true, initialAllocationPerMarket);
        result.Parameters = combo;
        results.Add(result);

        Console.WriteLine($"Return: {result.TotalReturn:F2}%, Win: {result.WinRate:F2}%");
        currentTest++;
    }

    Console.WriteLine("\n=========================================");
    Console.WriteLine($"          {strategyName.ToUpper()} LEADERBOARD        ");
    Console.WriteLine("=========================================");

    var topResults = results.OrderByDescending(r => r.TotalReturn).Take(5).ToList();

    for (int i = 0; i < topResults.Count; i++)
    {
        var r = topResults[i];
        string bestComboStr = string.Join(" | ", r.Parameters.Select(p => $"{p,5:0.##}"));
        Console.WriteLine($"#{i + 1} | Params: [{bestComboStr}] | PnL: ${r.NetProfit,7:F2} | Win: {r.WinRate:F2}% | Trades: {r.TotalTrades}");
    }
    Console.WriteLine("=========================================");
}