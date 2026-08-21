using System.Globalization;
using System.Text;
using System.Text.Json;

namespace KalshiEvBot;

/// <summary>
/// One observation of how a market stood, captured at a moment. <c>Terminal</c> means it will never change
/// again — either it finalized, or Kalshi no longer serves it.
/// </summary>
public sealed record SettlementRecord(
    string Ticker, string Status, string Result, string Title, string EventTicker,
    string CloseTime, string ExpectedExpiration, DateTime AtUtc, string Note = "")
{
    public bool IsFinal => Status == "finalized" && (Result == "yes" || Result == "no");
    /// <summary>Kalshi no longer serves this market. The outcome is unrecoverable from the venue.</summary>
    public bool IsGone  => Status == "gone";
    public bool Terminal => IsFinal || IsGone;

    /// <summary>Did the side we would have bought win? Null while unresolved or unrecoverable.</summary>
    public bool? WonFor(string side)
        => !IsFinal ? null
         : string.Equals(side, "YES", StringComparison.OrdinalIgnoreCase) ? Result == "yes" : Result == "no";
}

/// <summary>
/// The permanent record of settlements — <b>append-only JSONL</b>, because it is the ONLY copy.
///
/// <para><b>Why this is not a cache.</b> Kalshi does not keep obscure markets around indefinitely; an ITF
/// or challenger match that settled last week may simply not answer today. So the outcome has to be
/// captured while it exists and then survive on our disk forever. A file we re-serialise on every write is
/// the wrong shape for that: one interrupted rewrite, one disk-full, one crash mid-flush, and the whole
/// history is gone with nothing to reconstruct it from. Appending a line per observation means a failure
/// costs at most the line being written, and a truncated tail still leaves every earlier record readable.</para>
///
/// <para><b>Nothing is ever deleted or overwritten.</b> Re-reading takes the LAST record per ticker, so a
/// market seen active and later finalized simply gains a second line. That also leaves an audit trail of
/// when we first saw each outcome, which is the only way to tell "we missed it" from "it never settled".</para>
/// </summary>
public sealed class SettlementStore
{
    private readonly string _path;
    private readonly object _lock = new();

    public string Path => _path;

    public SettlementStore(string? path = null)
    {
        _path = path ?? System.IO.Path.Combine(Directory.GetCurrentDirectory(), "ev_settlements.jsonl");
        MigrateLegacyJson();
    }

    /// <summary>Imports the older single-object <c>ev_settlements.json</c> exactly once, so an early run's
    /// results are not stranded in a format nothing reads any more.</summary>
    private void MigrateLegacyJson()
    {
        string legacy = System.IO.Path.Combine(
            System.IO.Path.GetDirectoryName(_path) ?? ".", "ev_settlements.json");
        if (!File.Exists(legacy) || File.Exists(_path)) return;
        try
        {
            using var doc = JsonDocument.Parse(File.ReadAllText(legacy));
            int n = 0;
            foreach (var p in doc.RootElement.EnumerateObject())
            {
                string status = p.Value.TryGetProperty("status", out var s) ? s.GetString() ?? "" : "";
                string result = p.Value.TryGetProperty("result", out var r) ? r.GetString() ?? "" : "";
                var at = p.Value.TryGetProperty("at", out var a) && a.TryGetDateTime(out var d) ? d : DateTime.UtcNow;
                Append(new SettlementRecord(p.Name, status, result, "", "", "", "", at, "migrated"));
                n++;
            }
            if (n > 0) Console.WriteLine($"[SETTLE] migrated {n} record(s) from ev_settlements.json → {_path}");
        }
        catch (Exception ex) { Console.WriteLine($"[SETTLE] legacy import skipped: {ex.Message}"); }
    }

    public void Append(SettlementRecord r)
    {
        var o = new
        {
            ticker = r.Ticker, status = r.Status, result = r.Result, title = r.Title,
            event_ticker = r.EventTicker, close_time = r.CloseTime,
            expected_expiration_time = r.ExpectedExpiration,
            at = r.AtUtc.ToString("o", CultureInfo.InvariantCulture), note = r.Note,
        };
        lock (_lock)
        {
            using var w = new StreamWriter(new FileStream(_path, FileMode.Append, FileAccess.Write, FileShare.Read),
                                           new UTF8Encoding(false));
            w.WriteLine(JsonSerializer.Serialize(o));
        }
    }

    /// <summary>Latest record per ticker. A malformed line is skipped rather than fatal — a truncated tail
    /// must never cost the whole history.</summary>
    public Dictionary<string, SettlementRecord> LoadLatest()
    {
        var outp = new Dictionary<string, SettlementRecord>(StringComparer.Ordinal);
        if (!File.Exists(_path)) return outp;

        foreach (var line in File.ReadLines(_path))
        {
            if (string.IsNullOrWhiteSpace(line)) continue;
            try
            {
                using var doc = JsonDocument.Parse(line);
                var e = doc.RootElement;
                string S(string k) => e.TryGetProperty(k, out var v) && v.ValueKind == JsonValueKind.String
                                    ? (v.GetString() ?? "") : "";
                string ticker = S("ticker");
                if (ticker.Length == 0) continue;
                DateTime.TryParse(S("at"), CultureInfo.InvariantCulture, DateTimeStyles.RoundtripKind, out var at);
                outp[ticker] = new SettlementRecord(ticker, S("status"), S("result"), S("title"),
                                                    S("event_ticker"), S("close_time"),
                                                    S("expected_expiration_time"), at, S("note"));
            }
            catch (JsonException) { /* skip the bad line, keep the history */ }
        }
        return outp;
    }
}
