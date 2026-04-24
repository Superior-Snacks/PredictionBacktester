using System.Collections.Concurrent;
using System.Globalization;
using System.Text.Json;
using PredictionBacktester.Engine;
using PredictionBacktester.Engine.LiveExecution;

namespace KalshiPolyCross;

/// <summary>
/// Background service that prevents STALE_BOOK_OPEN flags on genuinely valid but
/// quiet markets by proactively refreshing books via REST before they cross the
/// 30-second staleness threshold.
///
/// Polymarket: full REST snapshot (clears and rebuilds the book from /book endpoint).
/// Kalshi:     verifies top-of-book price via REST; if it matches the WS book within
///             tolerance, bumps the timestamp. If it diverges, logs a warning.
/// </summary>
public class BookRefresherService
{
    private readonly ConcurrentDictionary<string, LocalOrderBook> _books;
    private readonly KalshiOrderClient _kalshi;
    private readonly HttpClient _http = new() { Timeout = TimeSpan.FromSeconds(5) };
    private readonly SemaphoreSlim _kalshiSem = new(2, 2); // Kalshi REST rate-limit guard
    private readonly SemaphoreSlim _polySem   = new(4, 4); // Poly is less strict

    // Refresh any book whose last delta is older than this
    private const int RefreshAfterSeconds = 20;

    // Kalshi: flag a divergence if REST ask differs from WS ask by more than this
    private const decimal KalshiPriceTolerance = 0.05m;

    private const string PolyBookUrl = "https://clob.polymarket.com/book?token_id=";

    public BookRefresherService(
        ConcurrentDictionary<string, LocalOrderBook> books,
        KalshiOrderClient kalshi)
    {
        _books  = books;
        _kalshi = kalshi;
    }

    public async Task RunAsync(CancellationToken ct)
    {
        while (!ct.IsCancellationRequested)
        {
            // Run slightly under the stale threshold so refreshes land before 30s
            await Task.Delay(TimeSpan.FromSeconds(15), ct).ContinueWith(_ => { });
            if (ct.IsCancellationRequested) break;

            var now = DateTime.UtcNow;
            var tasks = new List<Task>();

            foreach (var (key, book) in _books)
            {
                if (!book.HasReceivedDelta) continue;
                if ((now - book.LastDeltaAt).TotalSeconds < RefreshAfterSeconds) continue;

                if (key.StartsWith("P:", StringComparison.Ordinal))
                {
                    string tokenId = key[2..];
                    tasks.Add(RefreshPolyBookAsync(book, tokenId, ct));
                }
                else if (key.StartsWith("K:", StringComparison.Ordinal) && !key.EndsWith("_NO"))
                {
                    string ticker = key[2..];
                    tasks.Add(RefreshKalshiBookAsync(book, ticker, ct));
                }
            }

            if (tasks.Count > 0)
            {
                await Task.WhenAll(tasks);
                Console.WriteLine($"[BOOK REFRESH] Refreshed {tasks.Count} quiet book(s) via REST");
            }
        }
    }

    // ── Polymarket: full snapshot clear+rebuild ───────────────────────────────

    private async Task RefreshPolyBookAsync(LocalOrderBook book, string tokenId, CancellationToken ct)
    {
        await _polySem.WaitAsync(ct);
        try
        {
            string json = await _http.GetStringAsync(PolyBookUrl + tokenId, ct);
            using var doc = JsonDocument.Parse(json);
            var root = doc.RootElement;

            if (root.TryGetProperty("bids", out var bids) && root.TryGetProperty("asks", out var asks))
            {
                book.ProcessBookUpdate(bids, asks);
                book.MarkDeltaReceived();
            }
        }
        catch (OperationCanceledException) { }
        catch (Exception ex)
        {
            Console.WriteLine($"[BOOK REFRESH WARN] Poly {tokenId[..Math.Min(8, tokenId.Length)]}: {ex.Message}");
        }
        finally { _polySem.Release(); }
    }

    // ── Kalshi: verify top price; bump timestamp if valid ────────────────────
    // Kalshi's book state is split across LocalOrderBook + size maps in MarketStateTracker,
    // so we can't do a clean REST rebuild here. Instead, verify the implied ask price from
    // the REST book matches the WS book within tolerance, and if so mark it fresh.

    private async Task RefreshKalshiBookAsync(LocalOrderBook yesBook, string ticker, CancellationToken ct)
    {
        await _kalshiSem.WaitAsync(ct);
        try
        {
            using var doc = await _kalshi.GetMarketOrderBookAsync(ticker);
            var root  = doc.RootElement;
            var obEl  = root.TryGetProperty("orderbook", out var ob) ? ob : root;

            // YES ask (implied) = 1 - best NO bid
            decimal bestNoBid  = GetBestBidFromKalshiSide(obEl, "no");
            decimal restYesAsk = bestNoBid > 0m ? Math.Round(1m - bestNoBid, 4) : -1m;

            decimal wsYesAsk = yesBook.GetBestAskPrice();

            if (restYesAsk < 0m)
            {
                // REST returned empty book — market may have resolved or halted; don't refresh
                return;
            }

            if (Math.Abs(restYesAsk - wsYesAsk) <= KalshiPriceTolerance)
            {
                yesBook.MarkRestRefreshed();

                // Also refresh the implied NO book if it's tracked
                // (NO book key = "K:{ticker}_NO" — look it up from the same _books dict)
                if (_books.TryGetValue($"K:{ticker}_NO", out var noBook))
                    noBook.MarkRestRefreshed();
            }
            else
            {
                Console.WriteLine($"[BOOK REFRESH WARN] Kalshi {ticker}: " +
                                  $"WS ask={wsYesAsk:0.0000} REST ask={restYesAsk:0.0000} — diverged, book may be stale");
            }
        }
        catch (OperationCanceledException) { }
        catch (Exception ex)
        {
            Console.WriteLine($"[BOOK REFRESH WARN] Kalshi {ticker}: {ex.Message}");
        }
        finally { _kalshiSem.Release(); }
    }

    private static decimal GetBestBidFromKalshiSide(JsonElement book, string side)
    {
        if (!book.TryGetProperty(side, out var arr)) return -1m;
        decimal best = 0m;
        foreach (var lvl in arr.EnumerateArray())
        {
            var items = lvl.EnumerateArray().ToArray();
            if (items.Length < 2) continue;
            decimal priceCents = items[0].ValueKind == JsonValueKind.Number
                ? items[0].GetDecimal()
                : decimal.TryParse(items[0].GetString(), NumberStyles.Any,
                      CultureInfo.InvariantCulture, out decimal p) ? p : 0m;
            best = Math.Max(best, priceCents / 100m);
        }
        return best;
    }
}
