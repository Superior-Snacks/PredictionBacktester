using System.Diagnostics;
using System.Globalization;
using System.Text.RegularExpressions;

namespace KalshiEvBot;

/// <summary>
/// What the trading stack itself costs the machine, for sizing the dedicated PC it is going to move to
/// (2026-10-25). Two halves: <see cref="RunAsync"/> keeps the recorder running beside the bot, and
/// <see cref="Report"/> prints its summary as section 10b of <c>--resolve</c>.
///
/// <para><b>Why the recorder is a separate process and not a loop in here.</b> Measuring the bot from inside
/// the bot changes what is measured: enumerating every process on the machine every few seconds allocates, and
/// that garbage lands on the same heap and thread pool the screening loop runs on. resource_monitor.py runs as
/// a windowless child at below-normal priority, attributes every process on the PC to bot / sidecar / browser /
/// other by process tree, and writes resource_logs/ResourceUsage_*.csv. It is read-only, and its own cost is in
/// every row (MonitorCpuPct, ~2.5% of one core at the 5s default).</para>
///
/// <para><b>Lifetime.</b> Started with <c>--exit-with</c> this process's id, so a crash cannot leave it
/// orphaned; killed on a clean shutdown; restarted with a growing backoff if it dies on its own. A second
/// recorder - the next bot run's, or one started by hand - waits on resource_logs/monitor.lock, so no sample is
/// written twice. EV_RESOURCE_MONITOR=0 turns it off; EV_RESOURCE_SAMPLE_SEC sets the interval.</para>
/// </summary>
public static class ResourceMonitor
{
    public const string LogDir = "resource_logs";
    private static readonly string[] Groups = { "bot", "sidecar", "browser" };

    public static async Task RunAsync(CancellationToken ct)
    {
        if (EvConfig.Env("EV_RESOURCE_MONITOR", 1) == 0)
        {
            Console.WriteLine("[RESOURCE] monitor off (EV_RESOURCE_MONITOR=0).");
            return;
        }
        string root = Directory.GetCurrentDirectory();
        string script = Path.Combine(root, "resource_monitor.py");
        if (!File.Exists(script))
        {
            Console.WriteLine($"[RESOURCE] {script} not found - resource monitor off.");
            return;
        }
        double interval = EvConfig.Env("EV_RESOURCE_SAMPLE_SEC", 5);
        var backoff = TimeSpan.FromMinutes(1);
        while (!ct.IsCancellationRequested)
        {
            Process? p = null;
            var errTail = new Queue<string>();
            DateTime started = DateTime.UtcNow;
            try
            {
                var psi = new ProcessStartInfo("python",
                    $"\"{script}\" --quiet --exit-with {Environment.ProcessId} "
                    + $"--interval {interval.ToString(CultureInfo.InvariantCulture)}")
                {
                    WorkingDirectory = root, UseShellExecute = false, CreateNoWindow = true,
                    RedirectStandardOutput = true, RedirectStandardError = true,
                };
                p = Process.Start(psi);
                if (p is null) throw new InvalidOperationException("Process.Start returned null");
                // Drained, not ignored: an unread pipe fills and blocks the child. Only its own [RESOURCE] status
                // lines reach the console; the stderr tail is kept for the one line explaining a crash.
                p.OutputDataReceived += (_, e) =>
                {
                    if (e.Data is { } s && s.StartsWith("[RESOURCE]", StringComparison.Ordinal))
                        Con.Line(ConsoleColor.DarkGray, s);
                };
                p.ErrorDataReceived += (_, e) =>
                {
                    if (e.Data is not { } s) return;
                    lock (errTail) { errTail.Enqueue(s); while (errTail.Count > 4) errTail.Dequeue(); }
                };
                p.BeginOutputReadLine();
                p.BeginErrorReadLine();
                await p.WaitForExitAsync(ct);
                string why;
                lock (errTail) why = errTail.Count > 0 ? " - " + string.Join(" | ", errTail) : "";
                if (DateTime.UtcNow - started > TimeSpan.FromMinutes(10)) backoff = TimeSpan.FromMinutes(1);
                Con.Line(ConsoleColor.Yellow, $"[RESOURCE] recorder exited ({p.ExitCode}){why}; restarting in "
                                            + $"{backoff.TotalMinutes:0} min.");
            }
            catch (OperationCanceledException) { break; }
            catch (System.ComponentModel.Win32Exception ex)
            {
                // Nothing to retry against: no python on PATH will not fix itself while the bot runs.
                Con.Line(ConsoleColor.Yellow, $"[RESOURCE] cannot start python ({ex.Message}) - resource monitor off.");
                return;
            }
            catch (Exception ex)
            {
                Con.Line(ConsoleColor.Yellow, $"[RESOURCE] recorder failed ({ex.GetType().Name}: {ex.Message}); "
                                            + $"retrying in {backoff.TotalMinutes:0} min.");
            }
            finally
            {
                if (p is not null)
                {
                    try { if (!p.HasExited) p.Kill(entireProcessTree: true); } catch { }
                    p.Dispose();
                }
            }
            try { await Task.Delay(backoff, ct); } catch (OperationCanceledException) { break; }
            backoff = TimeSpan.FromMinutes(Math.Min(30, backoff.TotalMinutes * 2));
        }
    }

    // ── the report ─────────────────────────────────────────────────────────────────────────────────

    /// <summary>One sample row, only the columns the report reads. Per-group arrays are indexed like
    /// <see cref="Groups"/>.</summary>
    internal sealed record Sample(DateTime At, double Interval, double SysCpu, double MaxCore, double AvailMB,
                                  double UsedMB, double SwapMB, double NetKBs, double DiskWriteKBs,
                                  double OtherCores, double MonitorCpu, double[] Procs, double[] Cores,
                                  double[] Hot, double[] Priv, double[] Ws, double[] Io, double[] Pid);

    /// <summary>The last <paramref name="days"/> daily files. Streamed and split on commas: every column is a
    /// number or the timestamp, so there is nothing to quote, and a week at 5s is ~120k rows - far too many
    /// to hold as dictionaries the way <see cref="Csv.Read"/> does.</summary>
    internal static List<Sample> Load(string dir, int days)
    {
        var outp = new List<Sample>();
        string logs = Path.Combine(dir, LogDir);
        if (!Directory.Exists(logs)) return outp;
        foreach (string f in Directory.GetFiles(logs, "ResourceUsage_*.csv").OrderBy(x => x).TakeLast(days))
        {
            using var fs = new FileStream(f, FileMode.Open, FileAccess.Read, FileShare.ReadWrite | FileShare.Delete);
            using var r = new StreamReader(fs);
            string? head = r.ReadLine();
            if (head is null) continue;
            var ix = head.Split(',').Select((h, i) => (h, i)).ToDictionary(t => t.h, t => t.i, StringComparer.Ordinal);
            string? line;
            while ((line = r.ReadLine()) is not null)
            {
                var c = line.Split(',');
                if (c.Length < ix.Count) continue;              // a row cut short by a kill mid-write
                double N(string k) => ix.TryGetValue(k, out int i) && double.TryParse(c[i], NumberStyles.Any,
                                          CultureInfo.InvariantCulture, out double v) ? v : double.NaN;
                if (!DateTime.TryParse(c[0], CultureInfo.InvariantCulture,
                                       DateTimeStyles.AdjustToUniversal | DateTimeStyles.AssumeUniversal, out var at))
                    continue;
                double[] G(string f2) => Groups.Select(g => N($"{g}_{f2}")).ToArray();
                outp.Add(new Sample(at, N("IntervalSec"), N("SysCpuPct"), N("SysMaxCorePct"), N("SysAvailMB"),
                                    N("SysUsedMB"), N("SwapUsedMB"), N("NetRecvKBs") + N("NetSentKBs"),
                                    N("DiskWriteKBs"), N("OtherCpuCores"), N("MonitorCpuPct"),
                                    G("Procs"), G("CpuCores"), G("HotThreadPct"), G("PrivateMB"),
                                    G("WorkingSetMB"), G("IoKBs"), G("Pid")));
            }
        }
        return outp.OrderBy(s => s.At).ToList();
    }

    internal static double Pct(IEnumerable<double> v, double q)
    {
        var s = v.Where(double.IsFinite).OrderBy(x => x).ToList();
        if (s.Count == 0) return double.NaN;
        double k = (s.Count - 1) * q;
        int lo = (int)Math.Floor(k), hi = (int)Math.Ceiling(k);
        return s[lo] + (s[hi] - s[lo]) * (k - lo);
    }

    private static double Max(IEnumerable<double> v)
    {
        var s = v.Where(double.IsFinite).ToList();
        return s.Count == 0 ? double.NaN : s.Max();
    }

    /// <summary>Section 10b. Only the live (tennis) report prints it: the machine is shared by every pipeline,
    /// so it belongs in one report, not three. <paramref name="all"/> is the report's own telemetry, reused for
    /// the bot-vs-load line instead of re-reading the files.</summary>
    public static void Report(string dir, IReadOnlyList<Obs> all, int days = 7)
    {
        Console.WriteLine();
        var rows = Load(dir, days);
        if (rows.Count == 0)
        {
            Console.WriteLine("10b. RESOURCES  - no samples yet (the bot starts resource_monitor.py itself; "
                            + "EV_RESOURCE_MONITOR=0 turns it off)");
            return;
        }
        var up = rows.Where(r => r.Procs[2] > 0).ToList();                       // browser up = a work window
        var run = rows.Where(r => r.Procs[0] > 0 || r.Procs[1] > 0).ToList();
        var dark = run.Where(r => !(r.Procs[2] > 0)).ToList();
        double hours = rows.Sum(r => double.IsFinite(r.Interval) ? r.Interval : 0) / 3600;
        double upH = up.Sum(r => double.IsFinite(r.Interval) ? r.Interval : 0) / 3600;
        Console.WriteLine($"10b. RESOURCES  (what the stack itself uses - bot, sidecar and its browser, apart from "
                        + $"everything else on this PC; the machine-sizing check)");
        Console.WriteLine($"   {hours:0.0} h recorded over the last {Math.Min(days, rows.Select(r => r.At.Date).Distinct().Count())} "
                        + $"day(s), {upH:0.0} h with the browser up. CPU is in CORES: 1.0 = one core flat out "
                        + $"({Environment.ProcessorCount} logical cores here).");

        static double Stack(Sample r, double[] a) => a.Where(double.IsFinite).Sum();
        if (up.Count > 0)
        {
            Console.WriteLine("   WORK WINDOWS   CPU cores p50 / p95 /  max   busiest thread p95   private MB p50 /   max   "
                            + "I/O KB/s p95");
            for (int g = 0; g < Groups.Length; g++)
            {
                var c = up.Select(r => r.Cores[g]).ToList();
                string hot = g == 2 ? "        -" : $"{Pct(up.Select(r => r.Hot[g]), .95),8:0}%";
                Console.WriteLine($"     {Groups[g],-12} {Pct(c, .5),6:0.00} {Pct(c, .95),6:0.00} {Max(c),6:0.00}   {hot}          "
                                + $"{Pct(up.Select(r => r.Priv[g]), .5),6:0} {Max(up.Select(r => r.Priv[g])),6:0}   "
                                + $"{Pct(up.Select(r => r.Io[g]), .95),9:0.0}");
            }
            var sc = up.Select(r => Stack(r, r.Cores)).ToList();
            var sm = up.Select(r => Stack(r, r.Priv)).ToList();
            Console.WriteLine($"     {"STACK",-12} {Pct(sc, .5),6:0.00} {Pct(sc, .95),6:0.00} {Max(sc),6:0.00}                         "
                            + $"{Pct(sm, .5),6:0} {Max(sm),6:0}");
            var oc = up.Select(r => r.OtherCores).ToList();
            Console.WriteLine($"     {"other apps",-12} {Pct(oc, .5),6:0.00} {Pct(oc, .95),6:0.00} {Max(oc),6:0.00}   "
                            + "(everything else on this PC)");
        }
        if (dark.Count > 0)
            Console.WriteLine($"   DARK           stack CPU p95 {Pct(dark.Select(r => Stack(r, r.Cores)), .95):0.00} cores, "
                            + $"private max {Max(dark.Select(r => Stack(r, r.Priv))):0} MB (bot + sidecar, browser closed)");

        if (run.Count > 0)
        {
            double total = Pct(run.Select(r => r.AvailMB + r.UsedMB), .5);
            Console.WriteLine($"   MACHINE        CPU p50/p95/max {Pct(run.Select(r => r.SysCpu), .5):0}/"
                            + $"{Pct(run.Select(r => r.SysCpu), .95):0}/{Max(run.Select(r => r.SysCpu)):0}%, "
                            + $"busiest core p95 {Pct(run.Select(r => r.MaxCore), .95):0}%, free RAM min "
                            + $"{run.Min(r => r.AvailMB) / 1024:0.0} of {total / 1024:0.0} GB, swap max "
                            + $"{Max(run.Select(r => r.SwapMB)) / 1024:0.0} GB, network p95 {Pct(up.Select(r => r.NetKBs), .95):0} KB/s");
            double busy = 100.0 * run.Count(r => r.SysCpu > 85) / run.Count;
            double core = 100.0 * run.Count(r => r.MaxCore > 95) / run.Count;
            double lowm = 100.0 * run.Count(r => r.AvailMB < 0.10 * total) / run.Count;
            Console.WriteLine($"   CONTENTION     system CPU > 85% in {busy:0.0}% of samples, a core > 95% in {core:0.0}%, "
                            + $"free RAM < 10% in {lowm:0.0}%");
        }

        // DOES THE MACHINE SLOW THE BOT? The bot->sidecar oracle read, bucketed by what the PC was doing at that
        // second. Flat across the buckets = headroom; rising with load = the bot is waiting on the machine.
        var stamps = rows.Select(r => r.At.Ticks).ToArray();
        var buckets = new Dictionary<string, List<double>> { ["< 50%"] = new(), ["50-80%"] = new(), [">= 80%"] = new() };
        long tol = TimeSpan.FromSeconds(10).Ticks;
        foreach (var o in all)
        {
            if (!double.IsFinite(o.OracleAgeMs) || string.IsNullOrEmpty(o.Decision)) continue;
            long t = o.At.ToUniversalTime().Ticks;
            int i = Array.BinarySearch(stamps, t);
            if (i < 0) i = ~i;
            int best = -1;
            foreach (int j in new[] { i - 1, i })
                if (j >= 0 && j < stamps.Length && Math.Abs(stamps[j] - t) <= tol
                    && (best < 0 || Math.Abs(stamps[j] - t) < Math.Abs(stamps[best] - t)))
                    best = j;
            if (best < 0) continue;
            double cpu = rows[best].SysCpu;
            buckets[cpu < 50 ? "< 50%" : cpu < 80 ? "50-80%" : ">= 80%"].Add(o.OracleAgeMs);
        }
        if (buckets.Values.Any(b => b.Count > 0))
            Console.WriteLine("   BOT VS LOAD    oracle read p50/p90 by system CPU: "
                            + string.Join("  |  ", buckets.Where(b => b.Value.Count > 0).Select(b =>
                                  $"{b.Key} {Pct(b.Value, .5):0}/{Pct(b.Value, .9):0} ms (n={b.Value.Count})"))
                            + "   (flat = headroom)");

        // A steady climb across a long run is a leak; a run = one bot / sidecar pid.
        var trends = new List<string>();
        for (int g = 0; g < 2; g++)
            foreach (var run_ in rows.Where(r => r.Pid[g] > 0).GroupBy(r => r.Pid[g]))
            {
                var pts = run_.Where(r => double.IsFinite(r.Priv[g])).ToList();
                if (pts.Count < 20 || (pts[^1].At - pts[0].At).TotalHours < 2) continue;
                double mt = pts.Average(r => r.At.Ticks), mm = pts.Average(r => r.Priv[g]);
                double sxx = pts.Sum(r => (r.At.Ticks - mt) * (r.At.Ticks - mt));
                double slope = sxx > 0 ? pts.Sum(r => (r.At.Ticks - mt) * (r.Priv[g] - mm)) / sxx * TimeSpan.TicksPerHour : 0;
                trends.Add($"{Groups[g]} {pts[0].Priv[g]:0} -> {pts[^1].Priv[g]:0} MB over "
                         + $"{(pts[^1].At - pts[0].At).TotalHours:0.0} h ({slope:+0.0;-0.0} MB/h)");
            }
        if (trends.Count > 0)
            Console.WriteLine($"   MEMORY TREND   {string.Join(";  ", trends)}   (a steady climb is a leak)");

        // The telemetry the stack writes, per UTC day, from the files themselves.
        var perDay = new SortedDictionary<string, long>(StringComparer.Ordinal);
        foreach (string f in Directory.GetFiles(dir, "*.csv"))
        {
            var m = Regex.Match(Path.GetFileName(f), @"_(\d{8})(?:_v\d+)?\.csv$");
            if (m.Success) perDay[m.Groups[1].Value] = perDay.GetValueOrDefault(m.Groups[1].Value) + new FileInfo(f).Length;
        }
        var recent = perDay.TakeLast(7).ToList();
        double mbDay = recent.Count > 0 ? recent.Average(kv => kv.Value) / (1024.0 * 1024) : double.NaN;
        if (recent.Count > 0)
            Console.WriteLine($"   DATA           ~{mbDay:0} MB/day of telemetry over the last {recent.Count} day(s) "
                            + $"(~{mbDay * 365 / 1024:0} GB/year)");

        if (up.Count > 0)
        {
            double p99 = Pct(up.Select(r => Stack(r, r.Cores)), .99);
            double peakGb = Max(up.Select(r => Stack(r, r.Priv))) / 1024;
            double hot = Math.Max(Pct(up.Select(r => r.Hot[0]), .95), Pct(up.Select(r => r.Hot[1]), .95));
            int cores = Math.Max(4, (int)Math.Ceiling(2 * p99));
            int ram = 8 * (int)Math.Ceiling((peakGb * 1.5 + 4) / 8);
            Console.WriteLine($"   SIZING         CPU: stack p99 {p99:0.00} cores -> {cores}+ physical cores (2x room).  "
                            + $"RAM: stack peak {peakGb:0.0} GB -> {ram} GB (1.5x + 4 GB for the OS).");
            Console.WriteLine($"                  CORE: busiest bot/sidecar thread p95 {hot:0}% of a core -> "
                            + (hot > 60 ? "single-core speed matters, favour a high clock."
                                        : "no thread near a core's limit, core count matters more than clock.")
                            + (double.IsFinite(mbDay) ? $"  DISK: ~{mbDay * 365 / 1024:0} GB/year, any 256 GB+ SSD." : ""));
        }
        Console.WriteLine($"   (recorder's own cost: p50 {Pct(rows.Select(r => r.MonitorCpu), .5):0.0}% of one core, "
                        + $"below-normal priority. `python resource_monitor.py --report` prints the long form.)");
    }
}
