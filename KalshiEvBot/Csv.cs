using System.Globalization;
using System.Text;

namespace KalshiEvBot;

/// <summary>
/// Minimal RFC4180 reader, matching what <see cref="EvTelemetry"/> writes. Deliberately hand-rolled and
/// tiny: the telemetry is the only product of M0, and a dependency that silently mis-parses one quoted
/// field would corrupt a settlement grade months from now with nothing to show for it.
/// </summary>
public static class Csv
{
    /// <summary>
    /// Reads a CSV into header-keyed rows. Returns an empty list for a missing or header-only file.
    ///
    /// <para><b>Opened with <c>FileShare.ReadWrite</c>, which is not optional here.</b> These files are read
    /// while the bot still has them open for appending — by <c>--resolve</c> from another process, and by
    /// <c>--verify</c> from inside the same one. A plain <c>StreamReader(path)</c> requests share-Read,
    /// which conflicts with the writer's existing Write handle and throws a sharing violation reported as
    /// "used by another process". Observed 2026-08-21: it crashed <c>--verify</c> outright and would have
    /// broken the "safe to run alongside" guarantee for <c>--resolve</c>.</para>
    /// </summary>
    public static List<Dictionary<string, string>> Read(string path)
    {
        var rows = new List<Dictionary<string, string>>();
        if (!File.Exists(path)) return rows;

        using var fs = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.ReadWrite);
        using var r = new StreamReader(fs);
        string? headerLine = r.ReadLine();
        if (headerLine is null) return rows;
        var header = SplitLine(headerLine);

        string? line;
        while ((line = ReadRecord(r)) is not null)
        {
            if (line.Length == 0) continue;
            var f = SplitLine(line);
            var d = new Dictionary<string, string>(header.Count, StringComparer.Ordinal);
            for (int i = 0; i < header.Count; i++)
                d[header[i]] = i < f.Count ? f[i] : "";
            rows.Add(d);
        }
        return rows;
    }

    /// <summary>One logical record, which may span physical lines if a quoted field contains a newline.</summary>
    private static string? ReadRecord(StreamReader r)
    {
        string? line = r.ReadLine();
        if (line is null) return null;
        while (CountUnescapedQuotes(line) % 2 == 1)
        {
            string? next = r.ReadLine();
            if (next is null) break;
            line += "\n" + next;
        }
        return line;
    }

    private static int CountUnescapedQuotes(string s)
    {
        int n = 0;
        foreach (char c in s) if (c == '"') n++;
        return n;
    }

    private static List<string> SplitLine(string line)
    {
        var outp = new List<string>();
        var sb = new StringBuilder();
        bool inQuotes = false;
        for (int i = 0; i < line.Length; i++)
        {
            char c = line[i];
            if (inQuotes)
            {
                if (c == '"')
                {
                    if (i + 1 < line.Length && line[i + 1] == '"') { sb.Append('"'); i++; }
                    else inQuotes = false;
                }
                else sb.Append(c);
            }
            else if (c == '"') inQuotes = true;
            else if (c == ',') { outp.Add(sb.ToString()); sb.Clear(); }
            else sb.Append(c);
        }
        outp.Add(sb.ToString());
        return outp;
    }

    public static double Num(Dictionary<string, string> row, string col)
        => row.TryGetValue(col, out var s) &&
           double.TryParse(s, NumberStyles.Any, CultureInfo.InvariantCulture, out var v) ? v : double.NaN;

    public static int Int(Dictionary<string, string> row, string col)
        => row.TryGetValue(col, out var s) &&
           int.TryParse(s, NumberStyles.Any, CultureInfo.InvariantCulture, out var v) ? v : 0;

    public static string Str(Dictionary<string, string> row, string col)
        => row.TryGetValue(col, out var s) ? s : "";
}
