using System.Globalization;
using System.Text.Json;

namespace KalshiEvBot;

/// <summary>
/// THE BALANCE SANITY CHECK (2026-11-23). Live Kelly sizes every bet as a share of ONE balance read a day - the
/// trading shard's cash, read by <c>RefreshBankrollAsync</c> - and nothing checked that number. While fixed dollar
/// caps sat on top ($21 a bet, 35 contracts, $400 a day) a wrong read could cost at most those. They came off on
/// 2026-11-23 so the bot scales with the account by itself, and every bound left - EV_MAX_TRADE_FRACTION per bet,
/// EV_LIVE_DAILY_FRAC per day, the derived per-game cap - is a share of that same number. A read that came back
/// ten times too big would make every one of them ten times too big.
///
/// <para><b>The rule.</b> The SIZING equity may rise at most <c>EV_EQUITY_MAX_DAILY_RISE</c> (default 0.5 = +50%)
/// per local day over the figure accepted on an EARLIER day. A monthly $500 deposit (+28% at $1,790, less every
/// month after) passes the same day; a bigger one ramps in over a few days; a wrong read is held at the ceiling
/// and said out loud. A fall is taken at once - shrinking with the account is Kelly's protection, never something
/// to slow down.</para>
///
/// <para><b>Why a persisted base.</b> The base is the last figure accepted on a PREVIOUS day, kept in
/// <c>ev_equity_guard.json</c>, so restarts cannot compound the allowance: three restarts in one day still allow
/// +50%, not 1.5 cubed. With no file (first run, or deleted) the read is accepted and becomes the base - which is
/// also the override: to take a held read at once (a big deposit you trust), delete the file and restart.</para>
/// </summary>
public static class EquityGuard
{
    /// <summary>What is kept between reads: the local day it was written, that day's base, and the figure accepted.</summary>
    public sealed record State(string Day, double BaseUsd, double AcceptedUsd);

    /// <summary>One read's outcome. <c>Held</c> = the read was above the ceiling and sizing uses the ceiling.
    /// <c>BaseUsd</c> 0 = nothing to compare against (first run, or the check off): the read stands as it is.</summary>
    public readonly record struct Decision(double UseUsd, double ReadUsd, double BaseUsd, double CeilingUsd, bool Held);

    public static string DayKey(DateTime localDate) => localDate.ToString("yyyy-MM-dd", CultureInfo.InvariantCulture);

    public static string DefaultPath => Path.Combine(Directory.GetCurrentDirectory(), "ev_equity_guard.json");

    /// <summary>The rule, pure: no clock, no file. A same-day re-read (a restart) compares against that day's
    /// stored base; a new day compares against the last figure accepted.</summary>
    public static Decision Decide(double readUsd, DateTime localDate, double maxRise, State? prev)
    {
        double baseUsd = prev is null ? 0 : prev.Day == DayKey(localDate) ? prev.BaseUsd : prev.AcceptedUsd;
        if (maxRise <= 0 || !(baseUsd > 0) || !double.IsFinite(baseUsd))
            return new Decision(readUsd, readUsd, 0, double.PositiveInfinity, false);
        double ceiling = baseUsd * (1.0 + maxRise);
        return readUsd > ceiling ? new Decision(ceiling, readUsd, baseUsd, ceiling, true)
                                 : new Decision(readUsd, readUsd, baseUsd, ceiling, false);
    }

    /// <summary>What to keep after <see cref="Decide"/>: today's base (unchanged by a same-day re-read) and today's
    /// accepted figure, which becomes tomorrow's base.</summary>
    public static State Next(Decision d, DateTime localDate)
        => new(DayKey(localDate), d.BaseUsd > 0 ? d.BaseUsd : d.UseUsd, d.UseUsd);

    /// <summary>Null when there is nothing usable - missing, unreadable or nonsensical - so the next read is
    /// accepted as on a first run. Never throws: a damaged file must not stop the bot sizing.</summary>
    public static State? Load(string path)
    {
        try
        {
            if (!File.Exists(path)) return null;
            var s = JsonSerializer.Deserialize<State>(File.ReadAllText(path));
            return s is not null && !string.IsNullOrEmpty(s.Day) && s.AcceptedUsd > 0 && double.IsFinite(s.AcceptedUsd)
                 ? s : null;
        }
        catch { return null; }
    }

    /// <summary>Write-then-rename, so a crash mid-write leaves the previous state rather than half a file.</summary>
    public static void Save(string path, State s)
    {
        string tmp = path + ".tmp";
        File.WriteAllText(tmp, JsonSerializer.Serialize(s, new JsonSerializerOptions { WriteIndented = true }));
        File.Move(tmp, path, overwrite: true);
    }
}
