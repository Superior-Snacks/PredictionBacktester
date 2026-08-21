using System.Globalization;
using System.Text;

namespace KalshiEvBot;

/// <summary>
/// One fully-evaluated candidate: everything that went into the decision, and the decision.
///
/// <para>Carries BOTH de-vig outputs and BOTH Kalshi prices rather than only the ones used. M1 grades
/// these rows against settlement months from now, and the questions it will want to ask — was the
/// proportional bias the problem, was the WS phantom the problem — cannot be reconstructed from a row that
/// only recorded the winner.</para>
/// </summary>
public sealed record EvSignal(
    DateTime AtUtc, string Ticker, string EventId, string Side, string Outcome, string EventTitle,
    string SettlementDate, bool InPlay,
    double PinOddsMine, double PinOddsOther, double PinSum, double Vig, double ShinZ,
    double PTrueProp, double PTrueShin, double PTrueUsed, string DeVigMethod,
    double OracleAgeMs, double OracleDepth,
    decimal WsAsk, decimal RestAsk, double WsBookAgeMs, decimal WsAskDepth,
    double Fee, double Cost, double EvProp, double EvShin, double Ev, double EvWs, double LimitPrice,
    SizeResult Size, double BankrollUsd, double OrderFeeUsd, double StakeUsd,
    bool InPriceWindow, string Decision);

/// <summary>
/// Append-only CSV of every REST-valued candidate. This file IS milestone M1 — the bot places no orders,
/// so the log is the entire product of M0, and a column that silently shifts invalidates every conclusion
/// drawn from it later. Hence the arity check on every write and the header check on every open.
/// </summary>
public sealed class EvTelemetry : IDisposable
{
    /// <summary>Column order is the contract. Add at the END only, never insert — an old file and a new
    /// one must stay readable by the same parser, and the header check below will rotate to a new file if
    /// they ever disagree anyway.</summary>
    public static readonly string[] Columns =
    {
        "Timestamp", "Ticker", "EventId", "Side", "Outcome", "EventTitle", "SettlementDate", "InPlay",
        "PinOddsMine", "PinOddsOther", "PinSumS", "Vig", "ShinZ",
        "PTrueProp", "PTrueShin", "PTrueUsed", "DeVigMethod", "OracleAgeMs", "OracleDepth",
        "KalshiWsAsk", "KalshiRestAsk", "WsRestGapCents", "WsBookAgeMs", "WsAskDepth",
        "FeePerContract", "CostPerContract", "EvProp", "EvShin", "Ev", "EvWs", "LimitPrice",
        "KellyF", "Alpha", "Beta", "Fraction", "BankrollUsd", "TargetUsd", "Contracts", "FlooredToZero",
        "OrderFeeUsd", "StakeUsd", "InPriceWindow", "Decision",
    };

    private readonly StreamWriter _w;
    private readonly object _lock = new();
    public string Path { get; }
    public long RowsWritten { get; private set; }

    public EvTelemetry(string? directory = null)
    {
        string dir = directory ?? Directory.GetCurrentDirectory();
        Directory.CreateDirectory(dir);
        string header = string.Join(",", Columns);
        string stem = $"EvTelemetry_{DateTime.UtcNow:yyyyMMdd}";

        // Daily append, but NEVER into a file whose header differs from ours. A schema change mid-day
        // would otherwise interleave rows of two different shapes in one file, and the corruption is
        // invisible until an analysis silently reads the wrong column as EV.
        string p = System.IO.Path.Combine(dir, stem + ".csv");
        for (int v = 2; File.Exists(p) && ReadHeader(p) is string h && h != header; v++)
        {
            Console.WriteLine($"[TELEMETRY] {System.IO.Path.GetFileName(p)} has a different column set — "
                            + $"rotating rather than mixing schemas in one file.");
            p = System.IO.Path.Combine(dir, $"{stem}_v{v}.csv");
        }
        Path = p;

        bool fresh = !File.Exists(Path) || new FileInfo(Path).Length == 0;
        _w = new StreamWriter(new FileStream(Path, FileMode.Append, FileAccess.Write, FileShare.Read),
                              new UTF8Encoding(false)) { AutoFlush = true };
        if (fresh) _w.WriteLine(header);
    }

    private static string? ReadHeader(string path)
    {
        try { using var r = new StreamReader(path); return r.ReadLine(); } catch { return null; }
    }

    private static string N(double v, int dp = 6)
        => double.IsFinite(v) ? Math.Round(v, dp).ToString(CultureInfo.InvariantCulture) : "";
    private static string N(decimal v, int dp = 6)
        => Math.Round(v, dp).ToString(CultureInfo.InvariantCulture);
    private static string Q(string? s)
    {
        s ??= "";
        return s.IndexOfAny(new[] { ',', '"', '\n', '\r' }) < 0 ? s : "\"" + s.Replace("\"", "\"\"") + "\"";
    }

    public void Write(EvSignal s)
    {
        string[] f =
        {
            s.AtUtc.ToString("o", CultureInfo.InvariantCulture), Q(s.Ticker), Q(s.EventId), Q(s.Side),
            Q(s.Outcome), Q(s.EventTitle), Q(s.SettlementDate), s.InPlay ? "1" : "0",
            N(s.PinOddsMine, 4), N(s.PinOddsOther, 4), N(s.PinSum, 6), N(s.Vig, 6), N(s.ShinZ, 6),
            N(s.PTrueProp), N(s.PTrueShin), N(s.PTrueUsed), Q(s.DeVigMethod), N(s.OracleAgeMs, 0), N(s.OracleDepth, 2),
            N(s.WsAsk, 4), N(s.RestAsk, 4), N((double)(s.RestAsk - s.WsAsk) * 100.0, 2),
            N(s.WsBookAgeMs, 0), N(s.WsAskDepth, 2),
            N(s.Fee), N(s.Cost), N(s.EvProp), N(s.EvShin), N(s.Ev), N(s.EvWs), N(s.LimitPrice, 4),
            N(s.Size.KellyF), N(s.Size.Alpha, 4), N(s.Size.Beta, 4), N(s.Size.Fraction),
            N(s.BankrollUsd, 2), N(s.Size.TargetUsd, 2),
            s.Size.Contracts.ToString(CultureInfo.InvariantCulture), s.Size.FlooredToZero ? "1" : "0",
            N(s.OrderFeeUsd, 2), N(s.StakeUsd, 2), s.InPriceWindow ? "1" : "0", Q(s.Decision),
        };

        // THE ARITY CHECK. A one-column drift corrupts every row from that point on and reads as plausible
        // data forever — it nearly happened on the arb telemetry. Fail loudly at the write, not quietly in
        // an analysis six weeks later.
        if (f.Length != Columns.Length)
            throw new InvalidOperationException(
                $"EvTelemetry row/header arity mismatch: {f.Length} values for {Columns.Length} columns. "
              + "A column was added to one and not the other.");

        lock (_lock) { _w.WriteLine(string.Join(",", f)); RowsWritten++; }
    }

    public void Dispose() { try { _w.Flush(); _w.Dispose(); } catch { } }
}
