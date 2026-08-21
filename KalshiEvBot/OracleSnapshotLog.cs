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
        "KalshiWsAsk", "Source", "NumLegs", "PinOddsAll",
    };

    private readonly RollingCsv _csv;
    public string Path => _csv.Path;
    public long RowsWritten => _csv.RowsWritten;

    public OracleSnapshotLog(string? directory = null)
        => _csv = new RollingCsv(directory ?? Directory.GetCurrentDirectory(), "EvOracleSnap", Columns);

    private static string N(double v, int dp = 6) => RollingCsv.N(v, dp);
    private static string Q(string? s) => RollingCsv.Q(s);

    /// <summary>
    /// Records one pair's fair value. <paramref name="legQuotes"/> is EVERY outcome of the matchup in
    /// <c>pair.Legs</c> order — two for a tennis moneyline, three for a soccer 1X2. The probability written
    /// is always that of the Kalshi market's YES leg, so a NO row would be its complement and is not written.
    /// </summary>
    public void Write(EvPair pair, IReadOnlyList<OracleQuote> legQuotes, double ageMs,
                      decimal wsYesAsk, string deVigMethod)
    {
        int yi = pair.YesLegIndex;
        if (yi < 0 || legQuotes.Count != pair.Legs.Count) return;

        var odds = legQuotes.Select(q => q.DecimalOdds).ToArray();
        var prop = DeVig.ProportionalN(odds);
        var shin = DeVig.ShinN(odds);
        if (!prop.Ok || !shin.Ok) return;
        // S < 1 means the leg set is INCOMPLETE, not that the book is generous — see the guard in
        // EvEvaluator.Screen. Recording such a row would poison the calibration file with a P_true that
        // was never Pinnacle's opinion, and M1 cannot tell the difference months later.
        if (prop.Overround < -0.005) return;
        double pProp = prop.PTrue[yi], pShin = shin.PTrue[yi];
        if (pProp <= 0 || pProp >= 1) return;                // unquotable — nothing to grade later

        var mine = legQuotes[yi];
        string[] f =
        {
            DateTime.UtcNow.ToString("o", CultureInfo.InvariantCulture), Q(pair.KalshiTicker), Q(pair.EventId),
            "YES", Q(pair.KalshiOutcome), Q(pair.EventTitle), Q(pair.SettlementDate), mine.Live ? "1" : "0",
            N(mine.DecimalOdds, 4),
            N(odds.Length == 2 ? odds[1 - yi] : double.NaN, 4),   // meaningful only on a two-way
            N(prop.Overround + 1.0), N(prop.Overround), N(shin.ShinZ),
            N(pProp), N(pShin), N(deVigMethod == "shin" ? pShin : pProp),
            Q(deVigMethod), N(ageMs, 0), N(mine.MaxContracts, 2),
            N((double)wsYesAsk, 4), "snapshot",
            odds.Length.ToString(CultureInfo.InvariantCulture),
            Q(string.Join(";", odds.Select(o => o.ToString("0.####", CultureInfo.InvariantCulture)))),
        };

        // Same arity discipline as the signal telemetry, enforced inside WriteRow on every row.
        _csv.WriteRow(f);
    }

    public void Dispose() => _csv.Dispose();
}
