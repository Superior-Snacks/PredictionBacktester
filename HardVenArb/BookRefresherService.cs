using System.Collections.Concurrent;
using System.Globalization;
using System.Text.Json;
using PredictionBacktester.Engine;
using PredictionBacktester.Engine.LiveExecution;

namespace HardVenArb;

/// <summary>
/// Background service that prevents STALE_BOOK_OPEN flags on genuinely valid but
/// quiet markets by proactively refreshing books via REST before they cross the
/// 30-second staleness threshold.
///
/// HardVen: full REST snapshot (clears and rebuilds the book from /book endpoint).
/// Kalshi:     verifies top-of-book price via REST; if it matches the WS book within
///             tolerance, bumps the timestamp. If it diverges, logs a warning.
/// </summary>
public class BookRefresherService
{
    private readonly ConcurrentDictionary<string, LocalOrderBook> _books;
    private readonly KalshiOrderClient _kalshi;

    // Refresh any book whose last delta is older than this.
    // LocalOrderBook.IsStale() defaults to 120s — keep a 20s buffer so books
    // never reach the strategy's stale threshold before we've refreshed them.
    private const int RefreshAfterSeconds = 100;

    // Kalshi rate-limit: max requests per cycle and ms between each.
    // 75 × 150ms = ~11s of Kalshi work per 15s cycle — safe headroom.
    private const int KalshiMaxPerCycle   = 75;
    private const int KalshiIntervalMs    = 150; // ~6.7 req/s

    // Kalshi: flag a divergence if REST ask differs from WS ask by more than this
    private const decimal KalshiPriceTolerance = 0.05m;

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
            await Task.Delay(TimeSpan.FromSeconds(15), ct).ContinueWith(_ => { });
            if (ct.IsCancellationRequested) break;

            var now = DateTime.UtcNow;
            var kalshiStale  = new List<(LocalOrderBook book, string ticker, DateTime lastDelta)>();

            foreach (var (key, book) in _books)
            {
                if (!book.HasReceivedDelta) continue;
                if (book.IsDead) continue; // REST confirmed empty; market resolved/halted
                var age = now - book.LastDeltaAt;
                if (age.TotalSeconds < RefreshAfterSeconds) continue;

                // Only Kalshi (WS) books go quiet and benefit from a REST nudge. HardVen ("H:") books are
                // refreshed by HardVenWebsocketFeed's 9s sidecar poll; a quiet HardVen book means the sidecar
                // has no quote for it (resolved/off-board, or cleared by the staleness gate), which a REST
                // refresh can't fix — so skip it and let the strategy's stale guard close any open window.
                if (key.StartsWith("K:", StringComparison.Ordinal) && !key.EndsWith("_NO"))
                    kalshiStale.Add((book, key[2..], book.LastDeltaAt));
            }

            // Kalshi: serial with inter-request delay, capped per cycle to avoid 429s.
            // Sort oldest-first so the most overdue books get priority.
            kalshiStale.Sort((a, b) => a.lastDelta.CompareTo(b.lastDelta));
            int kalshiCount = 0;
            foreach (var (book, ticker, _) in kalshiStale.Take(KalshiMaxPerCycle))
            {
                if (ct.IsCancellationRequested) break;
                await RefreshKalshiBookAsync(book, ticker, ct);
                kalshiCount++;
                if (kalshiCount < kalshiStale.Count)
                    await Task.Delay(KalshiIntervalMs, ct).ContinueWith(_ => { });
            }

            int total = kalshiCount;
            int dead  = _books.Count(kv => kv.Value.IsDead);

            if (total > 0 || dead > 0)
            {
                string kalshiNote = kalshiStale.Count > KalshiMaxPerCycle
                    ? $" (Kalshi capped at {KalshiMaxPerCycle}/{kalshiStale.Count})"
                    : "";
                string deadNote = dead > 0
                    ? $" · {dead} REST-confirmed dead (resolved/halted)"
                    : "";
                Console.WriteLine($"[BOOK REFRESH] Refreshed {total} quiet book(s) via REST{kalshiNote}{deadNote}");
            }
        }
    }

    // ── Kalshi: verify top price; bump timestamp if valid ────────────────────
    // Kalshi's book state is split across LocalOrderBook + size maps in MarketStateTracker,
    // so we can't do a clean REST rebuild here. Instead, verify the implied ask price from
    // the REST book matches the WS book within tolerance, and if so mark it fresh.

    private async Task RefreshKalshiBookAsync(LocalOrderBook yesBook, string ticker, CancellationToken ct)
    {
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
                // REST confirmed empty book — mark both YES and NO dead so we stop polling.
                DebugLog.Books($"RefreshKalshiBookAsync {ticker}: REST returned no bid — marking dead");
                yesBook.MarkDead();
                if (_books.TryGetValue($"K:{ticker}_NO", out var noBookDead))
                    noBookDead.MarkDead();
                return;
            }

            DebugLog.Books($"RefreshKalshiBookAsync {ticker}: wsAsk={wsYesAsk:0.0000} restAsk={restYesAsk:0.0000} diff={Math.Abs(restYesAsk - wsYesAsk):0.0000}");
            if (Math.Abs(restYesAsk - wsYesAsk) <= KalshiPriceTolerance)
            {
                yesBook.MarkRestRefreshed();
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
            Console.WriteLine($"[BOOK REFRESH WARN] Kalshi {ticker}: {ApiErrorHelper.ClassifyKalshi(ex)}");
            DebugLog.Books($"RefreshKalshiBookAsync {ticker}: {ex.GetType().Name}: {ex}");
        }
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
