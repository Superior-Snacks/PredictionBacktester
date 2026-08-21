using System.Globalization;
using System.Text;

namespace KalshiEvBot;

/// <summary>
/// Append-only CSV that rolls to a new file at UTC midnight and refuses to mix schemas.
///
/// <para><b>Why rolling matters for a run measured in weeks.</b> Stamping the filename once at startup
/// means a bot started on the 21st is still writing to <c>…_20260821.csv</c> on the 30th. Nothing is lost,
/// but every downstream date filter silently reads nine days of data as one, and the operator has no way to
/// tell a long run from a stuck one by looking at the directory.</para>
///
/// <para><b>Arity is checked on every row, not once.</b> A drifted column corrupts every row after it and
/// still parses cleanly, which is the one failure a settlement grade months later cannot detect from its own
/// output. Likewise a day-file whose header does not match ours is rolled to a <c>_v2</c> rather than
/// appended to, so two schemas can never interleave in one file.</para>
/// </summary>
public sealed class RollingCsv : IDisposable
{
    private readonly string _dir, _prefix, _header;
    private readonly int _arity;
    private readonly object _lock = new();
    private StreamWriter? _w;
    private string _stamp = "";

    public string Path { get; private set; } = "";
    public long RowsWritten { get; private set; }

    public RollingCsv(string directory, string filePrefix, string[] columns)
    {
        _dir    = directory;
        _prefix = filePrefix;
        _header = string.Join(",", columns);
        _arity  = columns.Length;
        Directory.CreateDirectory(_dir);
        Roll(DateTime.UtcNow);
    }

    private void Roll(DateTime utc)
    {
        string stamp = utc.ToString("yyyyMMdd", CultureInfo.InvariantCulture);
        if (stamp == _stamp && _w is not null) return;

        _w?.Flush();
        _w?.Dispose();
        _stamp = stamp;

        string stem = $"{_prefix}_{stamp}";
        string p = System.IO.Path.Combine(_dir, stem + ".csv");
        for (int v = 2; File.Exists(p) && ReadHeader(p) is string h && h != _header; v++)
        {
            Console.WriteLine($"[CSV] {System.IO.Path.GetFileName(p)} has a different column set — "
                            + "rolling to a new file rather than mixing schemas.");
            p = System.IO.Path.Combine(_dir, $"{stem}_v{v}.csv");
        }
        Path = p;

        bool fresh = !File.Exists(Path) || new FileInfo(Path).Length == 0;
        _w = new StreamWriter(new FileStream(Path, FileMode.Append, FileAccess.Write, FileShare.Read),
                              new UTF8Encoding(false)) { AutoFlush = true };
        if (fresh) _w.WriteLine(_header);
    }

    /// <summary>Share-ReadWrite for the same reason <see cref="Csv.Read"/> needs it: yesterday's file may
    /// still be held open elsewhere, and a sharing violation here would look like a header mismatch and
    /// silently roll to a spurious _v2.</summary>
    private static string? ReadHeader(string path)
    {
        try
        {
            using var fs = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.ReadWrite);
            using var r = new StreamReader(fs);
            return r.ReadLine();
        }
        catch { return null; }
    }

    public void WriteRow(string[] fields)
    {
        if (fields.Length != _arity)
            throw new InvalidOperationException(
                $"{_prefix} row/header arity mismatch: {fields.Length} values for {_arity} columns. "
              + "A column was added to one and not the other.");

        lock (_lock)
        {
            Roll(DateTime.UtcNow);           // cheap: a string compare on the common path
            _w!.WriteLine(string.Join(",", fields));
            RowsWritten++;
        }
    }

    // ── Field formatting, shared by every writer so two files never disagree on how a value looks ──────
    public static string N(double v, int dp = 6)
        => double.IsFinite(v) ? Math.Round(v, dp).ToString(CultureInfo.InvariantCulture) : "";

    public static string N(decimal v, int dp = 6)
        => Math.Round(v, dp).ToString(CultureInfo.InvariantCulture);

    public static string Q(string? s)
    {
        s ??= "";
        return s.IndexOfAny(new[] { ',', '"', '\n', '\r' }) < 0 ? s : "\"" + s.Replace("\"", "\"\"") + "\"";
    }

    public void Dispose() { lock (_lock) { try { _w?.Flush(); _w?.Dispose(); } catch { } _w = null; } }
}
