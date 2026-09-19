using System.Globalization;

namespace KalshiEvBot;

/// <summary>One follow-up row, reduced to the ten columns the report reads. <c>EntryUtc</c> is kept as the
/// exact string the tracker wrote (section 10 joins it against the cooldown log by string) and also parsed
/// once into <c>EntryAt</c> (UTC; <c>DateTime.MinValue</c> when unparseable).</summary>
public sealed record FollowRow(string Ticker, string Side, string Decision, string EntryUtc, DateTime EntryAt,
                               double AgeSec, double EntryAsk, double EntryPTrue, double NowAsk, double NowBid,
                               double EntryDepth);

/// <summary>
/// Per-file typed cache for the report's inputs.
///
/// <para><b>Why.</b> <c>--resolve</c> re-parsed every day's CSV on every run — 1.1 GB of telemetry into
/// 2.8M <c>Dictionary&lt;string,string&gt;</c> rows of 55 strings each, then the 305 MB of follow-ups
/// three separate times (once per section that reads them). Each day added a day's parsing to every
/// subsequent run, which is exactly the "slower every day" the operator noticed on 2026-09-19. Yet a
/// past day's file never changes: <see cref="RollingCsv"/> appends to today's file only. So the parsed,
/// typed rows of any file that is not still being written can be stored once and read back as a compact
/// binary — tens of times faster than the CSV, and without the dictionary garbage.</para>
///
/// <para><b>What is cached, and what deliberately is not.</b> The cache holds the row-derived fields only.
/// Everything that can change between runs stays at load time: the settlement outcome (<c>Won</c>), the
/// mis-oriented ticker list (<c>ev_misoriented.json</c>), and the source-gap / in-play-age exclusions
/// (constants that may be retuned). A cache is keyed on the source file's length and last-write time and
/// on <see cref="Version"/>; a mismatch on any of them re-parses and rewrites. Today's file misses every
/// run, which is one day's parsing instead of a month's. A corrupt or unreadable cache is treated as a
/// miss, never as an error — the CSV is always the source of truth.</para>
///
/// <para><b>Files.</b> <c>&lt;dir&gt;/.evcache/&lt;csv-basename&gt;.&lt;kind&gt;.bin</c>. Safe to delete at
/// any time; the next run rebuilds them.</para>
/// </summary>
public static class ReportCache
{
    /// <summary>Bump when the set of fields extracted per row changes: every cache is then rebuilt.</summary>
    public const int Version = 1;
    private const string Magic = "EVCACHE";

    public static int Hits, Misses;
    public static double ParseSeconds, CacheSeconds;

    private static string CachePath(string src, string kind)
    {
        string dir = Path.Combine(Path.GetDirectoryName(src) ?? ".", ".evcache");
        Directory.CreateDirectory(dir);
        return Path.Combine(dir, Path.GetFileNameWithoutExtension(src) + "." + kind + ".bin");
    }

    // ── telemetry / oracle snapshots → Obs (Won unset) ───────────────────────────────────────────────
    public readonly record struct TelemetryFile(int RawRows, List<Obs> Rows, bool FromCache);

    public static TelemetryFile LoadTelemetry(string src)
    {
        var fi = new FileInfo(src);
        string cp = CachePath(src, "obs");
        var sw = System.Diagnostics.Stopwatch.StartNew();
        if (TryReadObs(cp, fi, out int raw, out var rows))
        {
            Hits++; CacheSeconds += sw.Elapsed.TotalSeconds;
            return new TelemetryFile(raw, rows, true);
        }
        Misses++;
        var csv = Csv.Read(src);
        rows = new List<Obs>(csv.Count);
        foreach (var r in csv)
        {
            var o = Calibration.ObsFromRow(r);
            if (o is not null) rows.Add(o);
        }
        ParseSeconds += sw.Elapsed.TotalSeconds;
        TryWriteObs(cp, fi, csv.Count, rows);
        return new TelemetryFile(csv.Count, rows, false);
    }

    private static bool TryReadObs(string cp, FileInfo src, out int raw, out List<Obs> rows)
    {
        raw = 0; rows = new List<Obs>();
        try
        {
            if (!File.Exists(cp)) return false;
            using var fs = new FileStream(cp, FileMode.Open, FileAccess.Read, FileShare.Read, 1 << 16);
            using var r = new BinaryReader(fs);
            if (!HeaderOk(r, src, "obs", out raw, out int n)) return false;
            var tbl = new List<string>();
            rows = new List<Obs>(n);
            for (int i = 0; i < n; i++)
            {
                string ticker = ReadStr(r, tbl), side = ReadStr(r, tbl);
                var at = DateTime.FromBinary(r.ReadInt64());
                double pProp = r.ReadDouble(), pShin = r.ReadDouble(), pUsed = r.ReadDouble(),
                       restAsk = r.ReadDouble(), cost = r.ReadDouble(), ev = r.ReadDouble();
                int contracts = r.ReadInt32();
                byte flags = r.ReadByte();
                double age = r.ReadDouble();
                string regime = ReadStr(r, tbl), decision = ReadStr(r, tbl), mtype = ReadStr(r, tbl);
                double depth = r.ReadDouble(), gap = r.ReadDouble();
                rows.Add(new Obs(ticker, side, at, pProp, pShin, pUsed, restAsk, cost, ev, contracts,
                                 (flags & 1) != 0, age, (flags & 2) != 0, null,
                                 (flags & 4) != 0 ? 1 : (flags & 8) != 0 ? 0 : -1,
                                 regime, decision, mtype, depth, gap));
            }
            return true;
        }
        catch { rows = new List<Obs>(); return false; }
    }

    private static void TryWriteObs(string cp, FileInfo src, int raw, List<Obs> rows)
    {
        string tmp = cp + ".tmp";
        try
        {
            using (var fs = new FileStream(tmp, FileMode.Create, FileAccess.Write, FileShare.None, 1 << 16))
            using (var w = new BinaryWriter(fs))
            {
                WriteHeader(w, src, "obs", raw, rows.Count);
                var tbl = new Dictionary<string, int>(StringComparer.Ordinal);
                foreach (var o in rows)
                {
                    WriteStr(w, o.Ticker, tbl); WriteStr(w, o.Side, tbl);
                    w.Write(o.At.ToBinary());
                    w.Write(o.PProp); w.Write(o.PShin); w.Write(o.PUsed);
                    w.Write(o.RestAsk); w.Write(o.Cost); w.Write(o.Ev);
                    w.Write(o.Contracts);
                    w.Write((byte)((o.InPlay ? 1 : 0) | (o.IsSignal ? 2 : 0)
                                   | (o.WsVerified == 1 ? 4 : 0) | (o.WsVerified == 0 ? 8 : 0)));
                    w.Write(o.OracleAgeMs);
                    WriteStr(w, o.Regime, tbl); WriteStr(w, o.Decision, tbl); WriteStr(w, o.MarketType, tbl);
                    w.Write(o.WsDepth); w.Write(o.SrcGapCents);
                }
            }
            File.Move(tmp, cp, overwrite: true);
        }
        catch (Exception ex)
        {
            Console.WriteLine($"[CACHE] could not write {Path.GetFileName(cp)} ({ex.GetType().Name}) - the report is unaffected.");
            try { File.Delete(tmp); } catch { }
        }
    }

    // ── follow-ups → FollowRow, all files of a prefix, memoised per process ──────────────────────────
    private static readonly Dictionary<string, List<FollowRow>> _followMemo = new(StringComparer.OrdinalIgnoreCase);

    /// <summary>Every follow-up row under <c>dir/&lt;prefix&gt;_*.csv</c> in file order. Loaded once per
    /// process however many sections ask - the three that read it used to parse all of it each.</summary>
    public static List<FollowRow> LoadFollowUps(string dir, string prefix)
    {
        string key = dir + "|" + prefix;
        if (_followMemo.TryGetValue(key, out var memo)) return memo;
        var all = new List<FollowRow>();
        foreach (string f in Directory.GetFiles(dir, prefix + "_*.csv").OrderBy(f => f))
            all.AddRange(LoadFollowFile(f));
        _followMemo[key] = all;
        return all;
    }

    private static List<FollowRow> LoadFollowFile(string src)
    {
        var fi = new FileInfo(src);
        string cp = CachePath(src, "follow");
        var sw = System.Diagnostics.Stopwatch.StartNew();
        if (TryReadFollow(cp, fi, out var rows))
        {
            Hits++; CacheSeconds += sw.Elapsed.TotalSeconds;
            return rows;
        }
        Misses++;
        rows = new List<FollowRow>();
        foreach (var r in Csv.Read(src))
        {
            string eu = Csv.Str(r, "EntryUtc");
            DateTime.TryParse(eu, CultureInfo.InvariantCulture,
                              DateTimeStyles.AdjustToUniversal | DateTimeStyles.AssumeUniversal, out var at);
            rows.Add(new FollowRow(Csv.Str(r, "Ticker"), Csv.Str(r, "Side"), Csv.Str(r, "Decision"), eu, at,
                                   Csv.Num(r, "AgeSec"), Csv.Num(r, "EntryAsk"), Csv.Num(r, "EntryPTrue"),
                                   Csv.Num(r, "NowAsk"), Csv.Num(r, "NowBid"), Csv.Num(r, "EntryDepth")));
        }
        ParseSeconds += sw.Elapsed.TotalSeconds;
        TryWriteFollow(cp, fi, rows);
        return rows;
    }

    private static bool TryReadFollow(string cp, FileInfo src, out List<FollowRow> rows)
    {
        rows = new List<FollowRow>();
        try
        {
            if (!File.Exists(cp)) return false;
            using var fs = new FileStream(cp, FileMode.Open, FileAccess.Read, FileShare.Read, 1 << 16);
            using var r = new BinaryReader(fs);
            if (!HeaderOk(r, src, "follow", out _, out int n)) return false;
            var tbl = new List<string>();
            rows = new List<FollowRow>(n);
            for (int i = 0; i < n; i++)
            {
                string ticker = ReadStr(r, tbl), side = ReadStr(r, tbl), dec = ReadStr(r, tbl);
                string eu = r.ReadString();
                var at = DateTime.FromBinary(r.ReadInt64());
                rows.Add(new FollowRow(ticker, side, dec, eu, at, r.ReadDouble(), r.ReadDouble(), r.ReadDouble(),
                                       r.ReadDouble(), r.ReadDouble(), r.ReadDouble()));
            }
            return true;
        }
        catch { rows = new List<FollowRow>(); return false; }
    }

    private static void TryWriteFollow(string cp, FileInfo src, List<FollowRow> rows)
    {
        string tmp = cp + ".tmp";
        try
        {
            using (var fs = new FileStream(tmp, FileMode.Create, FileAccess.Write, FileShare.None, 1 << 16))
            using (var w = new BinaryWriter(fs))
            {
                WriteHeader(w, src, "follow", rows.Count, rows.Count);
                var tbl = new Dictionary<string, int>(StringComparer.Ordinal);
                foreach (var o in rows)
                {
                    WriteStr(w, o.Ticker, tbl); WriteStr(w, o.Side, tbl); WriteStr(w, o.Decision, tbl);
                    w.Write(o.EntryUtc);
                    w.Write(o.EntryAt.ToBinary());
                    w.Write(o.AgeSec); w.Write(o.EntryAsk); w.Write(o.EntryPTrue);
                    w.Write(o.NowAsk); w.Write(o.NowBid); w.Write(o.EntryDepth);
                }
            }
            File.Move(tmp, cp, overwrite: true);
        }
        catch (Exception ex)
        {
            Console.WriteLine($"[CACHE] could not write {Path.GetFileName(cp)} ({ex.GetType().Name}) - the report is unaffected.");
            try { File.Delete(tmp); } catch { }
        }
    }

    // ── format helpers ───────────────────────────────────────────────────────────────────────────────
    private static void WriteHeader(BinaryWriter w, FileInfo src, string kind, int raw, int n)
    {
        w.Write(Magic); w.Write(Version); w.Write(kind);
        w.Write(src.Length); w.Write(src.LastWriteTimeUtc.Ticks);
        w.Write(raw); w.Write(n);
    }

    private static bool HeaderOk(BinaryReader r, FileInfo src, string kind, out int raw, out int n)
    {
        raw = 0; n = 0;
        if (r.ReadString() != Magic || r.ReadInt32() != Version || r.ReadString() != kind) return false;
        if (r.ReadInt64() != src.Length || r.ReadInt64() != src.LastWriteTimeUtc.Ticks) return false;
        raw = r.ReadInt32(); n = r.ReadInt32();
        return n >= 0;
    }

    // Strings are interned in order of first appearance: a 0 marker introduces a new string, any other
    // value is 1 + its index. Tickers and decisions repeat thousands of times, so this is most of the
    // size saving and all of the string allocation saving on read.
    private static void WriteStr(BinaryWriter w, string s, Dictionary<string, int> tbl)
    {
        if (tbl.TryGetValue(s, out int idx)) { w.Write7BitEncodedInt(idx + 1); return; }
        tbl[s] = tbl.Count;
        w.Write7BitEncodedInt(0);
        w.Write(s);
    }

    private static string ReadStr(BinaryReader r, List<string> tbl)
    {
        int v = r.Read7BitEncodedInt();
        if (v == 0) { string s = r.ReadString(); tbl.Add(s); return s; }
        return tbl[v - 1];
    }

    public static string Summary()
        => $"{Hits} file(s) from cache in {CacheSeconds:0.0}s, {Misses} parsed in {ParseSeconds:0.0}s "
         + "(a past day's file is parsed once and cached under .evcache/; today's is parsed every run)";
}
