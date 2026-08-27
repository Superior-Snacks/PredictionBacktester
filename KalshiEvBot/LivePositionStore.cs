using System.Globalization;
using System.Text.Json;

namespace KalshiEvBot;

/// <summary>
/// Survives a restart so the bot cannot buy the same side twice.
///
/// <para><b>The bug this closes.</b> <see cref="LiveExecutor"/> enforces "one filled entry per side" and
/// "$N per game" with two in-memory dictionaries. Held only in RAM, a restart — a crash, a supervisor, an
/// operator stopping and starting the bot — silently forgets every position already taken, and the caps
/// become "per process" rather than "per market". Over an unattended weekend that is the difference between
/// $10 a game and $10 a game <i>per restart</i>, with nobody watching to notice.</para>
///
/// <para><b>Written atomically.</b> Serialise to a sibling <c>.tmp</c> and rename over the target, because
/// the alternative failure — a half-written file — loses the whole record rather than one entry, and it
/// would do so at exactly the moment a crash proved we needed it. A rename is atomic on NTFS.</para>
///
/// <para><b>Entries expire.</b> A market settles within days, after which its record can never block
/// anything real; keeping it forever just grows a file that is read on every start. Anything older than
/// <c>EV_LIVE_STATE_TTL_DAYS</c> (default 7) is dropped on load. The TTL is deliberately far longer than a
/// tennis match so it can never expire a position that is still open.</para>
///
/// <para><b>A missing or corrupt file is not fatal.</b> It loads as empty and says so. Refusing to start
/// would turn a cosmetic problem into an outage; the honest cost of a lost file is the same double-entry
/// risk we had before this class existed, and it is reported rather than hidden.</para>
/// </summary>
public sealed class LivePositionStore
{
    private readonly string _path;
    private readonly object _lock = new();
    private readonly double _ttlDays;

    public string Path => _path;
    public int LoadedFilled { get; private set; }
    public int LoadedEvents { get; private set; }
    public string LoadNote { get; private set; } = "";

    public LivePositionStore(string? path = null)
    {
        _path = path ?? System.IO.Path.Combine(Directory.GetCurrentDirectory(), "ev_live_positions.json");
        _ttlDays = EvConfig.Env("EV_LIVE_STATE_TTL_DAYS", 7);
    }

    private sealed record Spend(double Usd, string At);
    private sealed record State(int Version, Dictionary<string, string> Filled, Dictionary<string, Spend> Spent);

    /// <summary>Reads the record, dropping anything past its TTL. Never throws.</summary>
    public (Dictionary<string, string> Filled, Dictionary<string, decimal> Spent) Load()
    {
        var filled = new Dictionary<string, string>(StringComparer.Ordinal);
        var spent  = new Dictionary<string, decimal>(StringComparer.Ordinal);
        try
        {
            if (!File.Exists(_path)) { LoadNote = "no prior state (first run)"; return (filled, spent); }
            var st = JsonSerializer.Deserialize<State>(File.ReadAllText(_path));
            if (st is null) { LoadNote = "state file unreadable — starting empty"; return (filled, spent); }
            var cutoff = DateTime.UtcNow.AddDays(-_ttlDays);
            int dropped = 0;
            foreach (var (k, at) in st.Filled ?? new())
            {
                if (Fresh(at, cutoff)) filled[k] = at; else dropped++;
            }
            foreach (var (k, v) in st.Spent ?? new())
            {
                if (Fresh(v.At, cutoff)) spent[k] = (decimal)v.Usd; else dropped++;
            }
            LoadedFilled = filled.Count; LoadedEvents = spent.Count;
            LoadNote = $"{filled.Count} filled side(s), {spent.Count} event(s) with spend"
                     + (dropped > 0 ? $"; {dropped} expired entr(y/ies) dropped" : "");
        }
        catch (Exception ex)
        {
            // Corrupt or partially-written: say so loudly and continue. The cost is the double-entry risk
            // that existed before this class, which is bad but not worse than refusing to trade at all.
            LoadNote = $"state file CORRUPT ({ex.GetType().Name}) — starting empty, "
                     + "so a side bought before the restart could be bought again";
        }
        return (filled, spent);
    }

    /// <summary>Parses a round-trip ("o") timestamp back to UTC.
    ///
    /// <para><c>RoundtripKind</c> ALONE. Combining it with <c>AdjustToUniversal</c> is an invalid style
    /// combination and <see cref="DateTime.TryParse(string, IFormatProvider, DateTimeStyles, out DateTime)"/>
    /// THROWS on it rather than returning false — which the caller's catch then reported as a corrupt file.
    /// The effect was that every load looked corrupt and silently started empty, i.e. the persistence did
    /// nothing at all while appearing to work. The self-test above exists because of exactly this.</para></summary>
    private static bool Fresh(string iso, DateTime cutoff)
        => DateTime.TryParse(iso, CultureInfo.InvariantCulture, DateTimeStyles.RoundtripKind, out var t)
           && t.ToUniversalTime() >= cutoff;

    /// <summary>Persists the current caps. Never throws — a failed write must not take a trading bot down,
    /// and the in-memory state is still correct for this process.</summary>
    public void Save(IReadOnlyDictionary<string, string> filled, IReadOnlyDictionary<string, decimal> spent)
    {
        try
        {
            var st = new State(1,
                new Dictionary<string, string>(filled, StringComparer.Ordinal),
                spent.ToDictionary(kv => kv.Key,
                                   kv => new Spend((double)kv.Value,
                                                   DateTime.UtcNow.ToString("o", CultureInfo.InvariantCulture)),
                                   StringComparer.Ordinal));
            lock (_lock)
            {
                string tmp = _path + ".tmp";
                File.WriteAllText(tmp, JsonSerializer.Serialize(st, new JsonSerializerOptions { WriteIndented = true }));
                File.Move(tmp, _path, overwrite: true);      // atomic on NTFS
            }
        }
        catch (Exception ex)
        {
            Console.WriteLine($"[LIVE STATE] could not persist positions: {ex.GetType().Name}: {ex.Message} "
                            + "— caps still hold for THIS process, but a restart would forget them.");
        }
    }
}
