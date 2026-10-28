using System.Net;
using System.Text.Json;
using PredictionBacktester.Engine.LiveExecution;

namespace KalshiEvBot;

/// <summary>
/// Asks Kalshi how a market ended and writes the answer down permanently.
///
/// <para><b>The venue is not an archive.</b> Kalshi does not keep obscure markets available indefinitely —
/// an ITF or challenger match that settled last week may simply not answer today. The outcome therefore has
/// to be captured while it still exists, which makes this a WRITER first and a reader second: every answer
/// goes straight into an append-only <see cref="SettlementStore"/> the moment it arrives, and nothing is
/// ever re-derived from the venue afterwards.</para>
///
/// <para>That is also why <see cref="WatchAsync"/> exists. Resolving only when someone remembers to run
/// <c>--resolve</c> races the purge and loses; the live bot polls its own markets and banks each result
/// within minutes of settlement.</para>
///
/// <para>A 404 is recorded as the terminal status <c>"gone"</c>, distinct from <c>"active"</c>. That
/// distinction is the whole point: "we never got an answer" must be visible in the data, not hidden as a
/// market that is forever pending.</para>
///
/// <para>Field shape verified live 2026-08-21: <c>status</c> is <c>"active"</c>/<c>"finalized"</c> (never
/// "open"), <c>result</c> is <c>""</c> until final then <c>"yes"</c>/<c>"no"</c>. A played match finalizes
/// within the hour; a postponed one sits active for days against a fallback <c>close_time</c> two weeks out.</para>
/// </summary>
public sealed class SettlementResolver
{
    private readonly KalshiOrderClient _kalshi;
    private readonly SettlementStore _store;
    private Dictionary<string, SettlementRecord> _known;
    // Guards _known. The settle loop writes it while the status line counts it from another thread, and a plain
    // Dictionary that gains a key mid-count throws "Collection was modified" - on 2026-10-28 that silently ended
    // the 30-second status line for the rest of the run. Nothing outside this class sees the live dictionary.
    private readonly object _gate = new();

    public int Fetched { get; private set; }
    public int NewlyFinal { get; private set; }
    public int Gone { get; private set; }
    public int Failed { get; private set; }

    public SettlementStore Store => _store;

    public SettlementResolver(KalshiOrderClient kalshi, SettlementStore? store = null)
    {
        _kalshi = kalshi;
        _store  = store ?? new SettlementStore();
        _known  = _store.LoadLatest();
    }

    /// <summary>A copy, safe to enumerate on any thread while the settle loop keeps writing.</summary>
    public IReadOnlyDictionary<string, SettlementRecord> Known
    {
        get { lock (_gate) return new Dictionary<string, SettlementRecord>(_known, _known.Comparer); }
    }

    /// <summary>Final / gone / all records, counted under the lock: the status line's numbers, without a copy.</summary>
    public (int Final, int Gone, int Total) Counts()
    {
        lock (_gate)
        {
            int fin = 0, gone = 0;
            foreach (var r in _known.Values) { if (r.IsFinal) fin++; if (r.IsGone) gone++; }
            return (fin, gone, _known.Count);
        }
    }

    /// <summary>Markets with no terminal record yet — the only ones still worth a request.</summary>
    public List<string> Pending(IEnumerable<string> tickers)
    {
        var all = tickers.Distinct(StringComparer.Ordinal).ToList();
        lock (_gate) return all.Where(t => !(_known.TryGetValue(t, out var r) && r.Terminal)).ToList();
    }

    /// <summary>
    /// Fetches one market and banks the answer. Returns the record, or null if the request failed in a way
    /// that is not informative (network, 5xx) — those are retried later rather than recorded, because
    /// writing them as terminal would throw away a result we could still have got.
    /// </summary>
    public async Task<SettlementRecord?> FetchAndStoreAsync(string ticker)
    {
        try
        {
            using var doc = await _kalshi.GetMarketAsync(ticker);
            var m = doc.RootElement.TryGetProperty("market", out var mk) ? mk : doc.RootElement;
            string S(string k) => m.TryGetProperty(k, out var v) && v.ValueKind == JsonValueKind.String
                                ? (v.GetString() ?? "") : "";

            var rec = new SettlementRecord(ticker, S("status"), S("result"), S("title"), S("event_ticker"),
                                           S("close_time"), S("expected_expiration_time"), DateTime.UtcNow);
            Fetched++;

            // Only write when something CHANGED, or when it is terminal. An active market re-checked every
            // ten minutes for a fortnight would otherwise add two thousand identical lines per market.
            bool known;
            SettlementRecord? prev;
            lock (_gate) known = _known.TryGetValue(ticker, out prev);
            if (!known || prev!.Status != rec.Status || prev.Result != rec.Result)
            {
                _store.Append(rec);
                lock (_gate) _known[ticker] = rec;
                if (rec.IsFinal) NewlyFinal++;
            }
            return rec;
        }
        catch (HttpRequestException ex) when (ex.StatusCode == HttpStatusCode.NotFound)
        {
            // The venue has stopped serving it. Record that as terminal and unrecoverable — if we never
            // banked a result before this point, that observation is lost, and the data must say so rather
            // than leave the market looking eternally pending.
            var rec = new SettlementRecord(ticker, "gone", "", "", "", "", "", DateTime.UtcNow,
                                           "HTTP 404 — Kalshi no longer serves this market");
            bool terminal;
            lock (_gate) terminal = _known.TryGetValue(ticker, out var prev) && prev.Terminal;
            if (!terminal)
            {
                _store.Append(rec);
                lock (_gate) _known[ticker] = rec;
                Gone++;
                Console.WriteLine($"[SETTLE] {ticker}: GONE from Kalshi with no result banked — "
                                + "that observation is unrecoverable.");
            }
            return rec;
        }
        catch (Exception ex)
        {
            Failed++;
            Console.WriteLine($"[SETTLE] {ticker}: {ex.GetType().Name}: {ex.Message} (will retry)");
            return null;
        }
    }

    /// <summary>Batch resolve, for <c>--resolve</c>. Only touches markets without a terminal record.</summary>
    public async Task<Dictionary<string, SettlementRecord>> ResolveAsync(IEnumerable<string> tickers,
                                                                         CancellationToken ct = default)
    {
        var pending = Pending(tickers);
        int i = 0;
        foreach (var t in pending)
        {
            ct.ThrowIfCancellationRequested();
            await FetchAndStoreAsync(t);
            if (++i % 20 == 0) Console.Write($"\r[SETTLE] {i}/{pending.Count}…");
            await Task.Delay(120, ct);      // polite: a batch job, not a hot path
        }
        if (i >= 20) Console.WriteLine($"\r[SETTLE] {i}/{pending.Count} done.          ");
        var fresh = _store.LoadLatest();
        lock (_gate) _known = fresh;
        return new Dictionary<string, SettlementRecord>(fresh, fresh.Comparer);
    }

    /// <summary>
    /// Background loop for the live bot: bank every settlement while the venue still has it.
    ///
    /// <para>This is the half that actually protects the data. A market settles within the hour of the
    /// match, so polling its own watchlist every few minutes captures the result long before any purge —
    /// whereas a <c>--resolve</c> run days later is a race we can only lose, and lose silently.</para>
    /// </summary>
    public async Task WatchAsync(Func<IEnumerable<string>> tickers, CancellationToken ct)
    {
        var every = TimeSpan.FromMinutes(Math.Max(1, EvConfig.Env("EV_SETTLE_POLL_MIN", 10)));
        Console.WriteLine($"[SETTLE] watching for settlements every {every.TotalMinutes:0} min → {_store.Path}");

        while (!ct.IsCancellationRequested)
        {
            try { await Task.Delay(every, ct); } catch (OperationCanceledException) { break; }
            try
            {
                var pending = Pending(tickers());
                if (pending.Count == 0) continue;
                int before = NewlyFinal, gone = Gone;
                foreach (var t in pending)
                {
                    if (ct.IsCancellationRequested) break;
                    await FetchAndStoreAsync(t);
                    await Task.Delay(150, ct);
                }
                if (NewlyFinal > before || Gone > gone)
                    Console.WriteLine($"[SETTLE] banked {NewlyFinal - before} new result(s)"
                                    + (Gone > gone ? $", {Gone - gone} market(s) went missing" : "")
                                    + $" — {Counts().Final} final on record");
            }
            catch (OperationCanceledException) { break; }
            catch (Exception ex) { Console.WriteLine($"[SETTLE] watch error: {ex.GetType().Name}: {ex.Message}"); }
        }
    }
}
