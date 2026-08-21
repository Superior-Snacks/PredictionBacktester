using System.Text.Json;
using PredictionBacktester.Engine.LiveExecution;

namespace KalshiEvBot;

/// <summary>How a Kalshi market ended. <c>Result</c> is "yes" | "no" once <c>Status</c> is "finalized".</summary>
public readonly record struct Settlement(string Ticker, string Status, string Result, DateTime FetchedUtc)
{
    public bool IsFinal => Status == "finalized" && (Result == "yes" || Result == "no");

    /// <summary>Did the side we would have bought win? Null while the market is unresolved.</summary>
    public bool? WonFor(string side)
        => !IsFinal ? null
         : string.Equals(side, "YES", StringComparison.OrdinalIgnoreCase) ? Result == "yes" : Result == "no";
}

/// <summary>
/// Fetches Kalshi settlements for logged signals and caches them on disk.
///
/// <para><b>Only finalized results are cached.</b> An unresolved market is re-fetched every run — caching
/// "active" would freeze a market as never-settled and quietly shrink the sample forever, which is the one
/// failure mode a calibration report cannot detect from its own output.</para>
///
/// <para>Field shape verified against the live API 2026-08-21: <c>status</c> is <c>"active"</c> or
/// <c>"finalized"</c> (never "open" — see <c>reference_kalshi_api</c>), and <c>result</c> is an empty string
/// until finalized, then <c>"yes"</c>/<c>"no"</c>. Settlement is prompt when it happens — an ATP challenger
/// finalized within the hour — but matches that are postponed sit <c>active</c> for days against a fallback
/// <c>close_time</c> two weeks out, so "not settled yet" is normal and not an error.</para>
/// </summary>
public sealed class SettlementResolver
{
    private readonly KalshiOrderClient _kalshi;
    private readonly string _cachePath;
    private readonly Dictionary<string, Settlement> _cache = new(StringComparer.Ordinal);

    public int Fetched { get; private set; }
    public int FromCache { get; private set; }
    public int Failed { get; private set; }

    public SettlementResolver(KalshiOrderClient kalshi, string? cachePath = null)
    {
        _kalshi    = kalshi;
        _cachePath = cachePath ?? Path.Combine(Directory.GetCurrentDirectory(), "ev_settlements.json");
        Load();
    }

    private void Load()
    {
        try
        {
            if (!File.Exists(_cachePath)) return;
            using var doc = JsonDocument.Parse(File.ReadAllText(_cachePath));
            foreach (var p in doc.RootElement.EnumerateObject())
            {
                string status = p.Value.TryGetProperty("status", out var s) ? s.GetString() ?? "" : "";
                string result = p.Value.TryGetProperty("result", out var r) ? r.GetString() ?? "" : "";
                var at = p.Value.TryGetProperty("at", out var a) && a.TryGetDateTime(out var d) ? d : DateTime.UtcNow;
                var st = new Settlement(p.Name, status, result, at);
                if (st.IsFinal) _cache[p.Name] = st;      // only finals survive a restart
            }
        }
        catch (Exception ex)
        {
            Console.WriteLine($"[RESOLVE] cache unreadable ({ex.GetType().Name}) — starting fresh.");
        }
    }

    private void Save()
    {
        try
        {
            var obj = _cache.Where(kv => kv.Value.IsFinal).ToDictionary(
                kv => kv.Key,
                kv => new { status = kv.Value.Status, result = kv.Value.Result, at = kv.Value.FetchedUtc });
            File.WriteAllText(_cachePath,
                JsonSerializer.Serialize(obj, new JsonSerializerOptions { WriteIndented = true }));
        }
        catch (Exception ex) { Console.WriteLine($"[RESOLVE] could not write cache: {ex.Message}"); }
    }

    public async Task<Dictionary<string, Settlement>> ResolveAsync(IEnumerable<string> tickers,
                                                                   CancellationToken ct = default)
    {
        var want = tickers.Distinct(StringComparer.Ordinal).ToList();
        var outp = new Dictionary<string, Settlement>(StringComparer.Ordinal);
        int i = 0;

        foreach (var t in want)
        {
            ct.ThrowIfCancellationRequested();
            if (_cache.TryGetValue(t, out var hit) && hit.IsFinal)
            {
                outp[t] = hit; FromCache++; continue;
            }
            try
            {
                using var doc = await _kalshi.GetMarketAsync(t);
                var m = doc.RootElement.TryGetProperty("market", out var mk) ? mk : doc.RootElement;
                string status = m.TryGetProperty("status", out var s) ? s.GetString() ?? "" : "";
                string result = m.TryGetProperty("result", out var r) ? r.GetString() ?? "" : "";
                var st = new Settlement(t, status, result, DateTime.UtcNow);
                outp[t] = st;
                if (st.IsFinal) _cache[t] = st;
                Fetched++;
            }
            catch (Exception ex)
            {
                Failed++;
                Console.WriteLine($"[RESOLVE] {t}: {ex.GetType().Name}: {ex.Message}");
            }
            if (++i % 20 == 0) Console.Write($"\r[RESOLVE] {i}/{want.Count}…");
            await Task.Delay(120, ct);   // polite: this is a batch job, not a hot path
        }
        if (i >= 20) Console.WriteLine($"\r[RESOLVE] {i}/{want.Count} done.        ");
        Save();
        return outp;
    }
}
