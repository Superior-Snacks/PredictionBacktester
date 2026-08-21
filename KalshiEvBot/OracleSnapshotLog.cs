using System.Globalization;
using System.Text;

namespace KalshiEvBot;

/// <summary>
/// Periodic record of the ORACLE alone: what Pinnacle, de-vigged, believed about every paired match.
///
/// <para><b>Why this exists separately from the signal telemetry.</b> The question that decides the whole
/// strategy — is Pinnacle's de-vigged line predictive on ITF/challenger tennis? — needs
/// <c>(P_true, outcome)</c> pairs. It does not need a +EV window, a Kalshi price, or a REST call. Waiting
/// for signals to accumulate answers it at the rate signals happen, which is a fortnight; logging every
/// pair on a timer answers it in days, and answers it even if no window ever opens.</para>
///
/// <para><b>Costs nothing.</b> Pure sidecar data the bot is already polling — no Kalshi REST, no extra
/// request to Pinnacle. The Kalshi WS ask rides along as context only and never prices anything.</para>
///
/// <para>One row per PAIR, not per side: de-vig forces <c>P_true(NO) = 1 − P_true(YES)</c>, so a NO row
/// would be the same observation written twice and would halve every confidence interval for free.</para>
///
/// <para>Column names deliberately match <see cref="EvTelemetry"/> where they overlap, so
/// <see cref="Calibration.FromTelemetry"/> grades both files through one code path.</para>
/// </summary>
public sealed class OracleSnapshotLog : IDisposable
{
    public static readonly string[] Columns =
    {
        "Timestamp", "Ticker", "EventId", "Side", "Outcome", "EventTitle", "SettlementDate", "InPlay",
        "PinOddsMine", "PinOddsOther", "PinSumS", "Vig", "ShinZ",
        "PTrueProp", "PTrueShin", "PTrueUsed", "DeVigMethod", "OracleAgeMs", "OracleDepth",
        "KalshiWsAsk", "Source",
    };

    private readonly StreamWriter _w;
    private readonly object _lock = new();
    public string Path { get; }
    public long RowsWritten { get; private set; }

    public OracleSnapshotLog(string? directory = null)
    {
        string dir = directory ?? Directory.GetCurrentDirectory();
        Directory.CreateDirectory(dir);
        string header = string.Join(",", Columns);
        string stem   = $"EvOracleSnap_{DateTime.UtcNow:yyyyMMdd}";

        string p = System.IO.Path.Combine(dir, stem + ".csv");
        for (int v = 2; File.Exists(p) && Header(p) is string h && h != header; v++)
            p = System.IO.Path.Combine(dir, $"{stem}_v{v}.csv");
        Path = p;

        bool fresh = !File.Exists(Path) || new FileInfo(Path).Length == 0;
        _w = new StreamWriter(new FileStream(Path, FileMode.Append, FileAccess.Write, FileShare.Read),
                              new UTF8Encoding(false)) { AutoFlush = true };
        if (fresh) _w.WriteLine(header);
    }

    private static string? Header(string path)
    {
        try { using var r = new StreamReader(path); return r.ReadLine(); } catch { return null; }
    }

    private static string N(double v, int dp = 6)
        => double.IsFinite(v) ? Math.Round(v, dp).ToString(CultureInfo.InvariantCulture) : "";
    private static string Q(string? s)
    {
        s ??= "";
        return s.IndexOfAny(new[] { ',', '"', '\n', '\r' }) < 0 ? s : "\"" + s.Replace("\"", "\"\"") + "\"";
    }

    public void Write(EvPair pair, OracleQuote yes, OracleQuote no, double ageMs,
                      decimal wsYesAsk, string deVigMethod)
    {
        var prop = DeVig.Proportional(yes.DecimalOdds, no.DecimalOdds);
        var shin = DeVig.Shin(yes.DecimalOdds, no.DecimalOdds);
        if (prop.PTrue <= 0 || prop.PTrue >= 1) return;      // unquotable — nothing to grade later

        string[] f =
        {
            DateTime.UtcNow.ToString("o", CultureInfo.InvariantCulture), Q(pair.KalshiTicker), Q(pair.EventId),
            "YES", Q(pair.KalshiOutcome), Q(pair.EventTitle), Q(pair.SettlementDate), yes.Live ? "1" : "0",
            N(yes.DecimalOdds, 4), N(no.DecimalOdds, 4), N(prop.Overround + 1.0), N(prop.Overround), N(shin.ShinZ),
            N(prop.PTrue), N(shin.PTrue), N(deVigMethod == "shin" ? shin.PTrue : prop.PTrue),
            Q(deVigMethod), N(ageMs, 0), N(yes.MaxContracts, 2),
            N((double)wsYesAsk, 4), "snapshot",
        };

        // Same arity discipline as the signal telemetry: a drifted column corrupts every row after it and
        // still parses, which is the failure mode a settlement grade months later cannot detect.
        if (f.Length != Columns.Length)
            throw new InvalidOperationException(
                $"OracleSnapshotLog row/header arity mismatch: {f.Length} values for {Columns.Length} columns.");

        lock (_lock) { _w.WriteLine(string.Join(",", f)); RowsWritten++; }
    }

    public void Dispose() { try { _w.Flush(); _w.Dispose(); } catch { } }
}
