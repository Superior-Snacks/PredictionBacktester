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
    bool InPriceWindow, string Decision, int NumLegs, string PinOddsAll, bool OracleWsVerified,
    double WsDepthToLimit, double CapacityUsd, string MoveRegime, string VenueVerify);

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
        "OrderFeeUsd", "StakeUsd", "InPriceWindow", "Decision", "NumLegs", "PinOddsAll", "OracleWsVerified", "WsDepthToLimit", "CapacityUsd", "MoveRegime", "VenueVerify",
    };

    private readonly RollingCsv _csv;
    public string Path => _csv.Path;
    public long RowsWritten => _csv.RowsWritten;

    public EvTelemetry(string? directory = null)
        => _csv = new RollingCsv(directory ?? Directory.GetCurrentDirectory(), "EvTelemetry", Columns);

    private static string N(double v, int dp = 6) => RollingCsv.N(v, dp);
    private static string N(decimal v, int dp = 6) => RollingCsv.N(v, dp);
    private static string Q(string? s) => RollingCsv.Q(s);

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
            s.NumLegs.ToString(CultureInfo.InvariantCulture), Q(s.PinOddsAll), s.OracleWsVerified ? "1" : "0",
            N(s.WsDepthToLimit, 2), N(s.CapacityUsd, 2), Q(s.MoveRegime), Q(s.VenueVerify),
        };

        // Arity is checked inside WriteRow, on EVERY row. A one-column drift corrupts everything after it
        // and still reads as plausible data forever — it nearly happened on the arb telemetry.
        _csv.WriteRow(f);
    }

    public void Dispose() => _csv.Dispose();
}
