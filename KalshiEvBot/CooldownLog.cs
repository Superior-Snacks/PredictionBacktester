using System.Globalization;

namespace KalshiEvBot;

/// <summary>
/// One row per candidate that cleared the EV threshold on the WS book but was NOT valued because its ticker
/// was inside the recheck cooldown. Written at most once per (ticker, side, cooldown window).
///
/// <para><b>Why this file exists.</b> The cooldown gate sits before the REST read and before the telemetry
/// write, so until 2026-09-15 a signal that appeared inside a 15-second window left no trace beyond a
/// lifetime counter that ticks on every 250ms poll. What the surviving rows showed: 73% of in-play signals
/// fired at the exact instant their cooldown lifted, having been blocked for up to 15s — and the ask moves
/// +1.73c in the first five seconds after a signal, so a signal seen 7.5s late has already given up about
/// half its edge. That was inferred. This makes it measured: how many candidates the cooldown hides, how
/// large they are, and — because each one is also scheduled into the follow-up tracker with
/// Decision=COOLDOWN_SKIP — what the ask did in the 5/10/20s after we <i>could</i> have fired.</para>
///
/// <para>Costs nothing at the venue: every field is already in memory when the gate fires. Bounded by the
/// dedupe to two rows per ticker per window.</para>
///
/// <para><b>Definition change 2026-09-17.</b> The first two days logged any candidate still clearing the
/// threshold on the WS book inside a window - 16,654 rows, all of them re-sightings of a price that had been
/// REST-valued 90ms earlier (join against the telemetry: 100% matched; six candidates genuinely appeared
/// mid-window). Rows now require NEW information since the check that armed the window - the side was not a
/// candidate then, or its WS EV improved by a full cent - and carry <c>EvAtArmCents</c> (empty when the side
/// was not a candidate at arming). Files without that column are from the old definition; the report says
/// so and ignores them rather than averaging the two.</para>
/// </summary>
public sealed class CooldownLog : IDisposable
{
    public static readonly string[] Columns =
    {
        "Timestamp", "Ticker", "Side", "InPlay", "WsAsk", "PTrueUsed", "EvWsCents", "WsDepthToLimit",
        "SecondsLeft", "MoveRegime", "EvAtArmCents",
    };

    private readonly RollingCsv _csv;

    public CooldownLog(string? directory = null, string prefix = "EvCooldownSkip")
        => _csv = new RollingCsv(directory ?? Directory.GetCurrentDirectory(), prefix, Columns);

    public string Path => _csv.Path;
    public long RowsWritten => _csv.RowsWritten;

    public void Write(DateTime utc, string ticker, string side, bool inPlay, decimal wsAsk, double pTrue,
                      double evWs, double depthToLimit, double secondsLeft, string regime, double evAtArm)
    {
        _csv.WriteRow(new[]
        {
            utc.ToString("o", CultureInfo.InvariantCulture), RollingCsv.Q(ticker), RollingCsv.Q(side),
            inPlay ? "1" : "0", RollingCsv.N(wsAsk, 4), RollingCsv.N(pTrue, 6), RollingCsv.N(evWs * 100, 2),
            RollingCsv.N(depthToLimit, 0), RollingCsv.N(secondsLeft, 1), RollingCsv.Q(regime),
            double.IsNaN(evAtArm) ? "" : RollingCsv.N(evAtArm * 100, 2),
        });
    }

    public void Dispose() => _csv.Dispose();
}
