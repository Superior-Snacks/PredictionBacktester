"""resource_monitor.py - what the trading stack actually uses, measured from OUTSIDE it.

Sizing a dedicated machine needs the stack's own numbers, not the whole PC's. This samples every
RESOURCE_SAMPLE_SEC (5) seconds and attributes every process to one group by PROCESS TREE:

  bot      the C# EV bot: `dotnet run --project KalshiEvBot` / KalshiEvBot.exe and everything it starts
  sidecar  the Python sidecar (`uvicorn app:app`) and its subprocesses (pairing runs), minus its browser
  browser  the sidecar's Playwright Chrome: the node driver and every chrome.exe under it. Your own Chrome
           is a different tree and counts as other. 0 processes = a dark window.
  report   any report run - ev_report_discord.py, the bin/report build, a `--resolve` - and what it starts,
           wherever it was launched from (the block-end radar post comes from the sidecar, the daily report
           from the bot). Split out 2026-10-26: the first measured run took 3.8 GB for a minute, the stack's
           biggest peak, and it grows with the telemetry history.
  other    everything else on the machine = system busy minus the four groups above

Per group: CPU in CORES (one core flat out = 1.0), the busiest single thread (bot + sidecar; % of one core -
says whether a faster core or more cores helps), private memory, working set, I/O (on Windows this counts disk
AND network bytes), threads, handles. System: CPU %, busiest core, available RAM, swap, disk + network totals.

READ-ONLY. It never signals, suspends or re-prioritises another process, and runs itself at below-normal priority,
so it cannot slow the bot down; its own CPU is logged in every row (MonitorCpuPct) to prove it. It re-finds the
processes every sample, so it survives bot and sidecar restarts and dark windows without being restarted.

    python resource_monitor.py                  # record; leave it running in its own window
    python resource_monitor.py --report         # summarise what was recorded (+ sizing)
    python resource_monitor.py --report --days 3

THE BOT RUNS IT ITSELF (KalshiEvBot ResourceMonitor.cs, EV_RESOURCE_MONITOR=1): a windowless child started with
`--quiet --exit-with <bot pid>`, and the bot's `--resolve` report prints the summary as section 10b. ONE recorder
at a time: a second copy (the bot's after a restart, or one started by hand) waits on resource_logs/monitor.lock
until the first stops, so no sample is ever written twice.
"""
from __future__ import annotations

import argparse
import bisect
import csv
import glob
import math
import os
import re
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone

try:
    import psutil
except ImportError:
    sys.exit("resource_monitor needs psutil:  pip install psutil")

HERE = os.path.dirname(os.path.abspath(__file__))
WINDOWS = os.name == "nt"
GROUPS = ("bot", "sidecar", "browser", "report")
STACK = ("bot", "sidecar", "browser")                  # always running; a report comes and goes
REPORT_MARKERS = ("ev_report_discord", "bin/report", "--resolve")
GROUP_FIELDS = ("Procs", "CpuCores", "HotThreadPct", "PrivateMB", "WorkingSetMB", "IoKBs", "Threads", "Handles", "Pid")
BROWSER_NAMES = {"chrome", "chrome.exe", "node", "node.exe", "chromium", "chromium.exe",
                 "headless_shell", "headless_shell.exe"}
# Only these get their command line read: reading another process's command line is the expensive call, and the
# three roots are always one of these.
CMDLINE_NAMES = re.compile(r"^(dotnet|kalshievbot|python\d*(\.\d+)?|pythonw?)(\.exe)?$", re.I)
COLUMNS = (["Timestamp", "IntervalSec", "SysCpuPct", "SysMaxCorePct", "SysAvailMB", "SysUsedMB", "SwapUsedMB",
            "DiskReadKBs", "DiskWriteKBs", "NetRecvKBs", "NetSentKBs", "OtherCpuCores", "MonitorCpuPct"]
           + [f"{g}_{f}" for g in GROUPS for f in GROUP_FIELDS])
MB = 1024 * 1024


def _root_group(name: str, cmd: str) -> str | None:
    n, c = name.lower(), cmd.replace("\\", "/")
    if "resource_monitor" in c:
        return None                                    # this script: its cost is MonitorCpuPct
    if any(m in c for m in REPORT_MARKERS):
        return "report"
    if n.startswith("kalshievbot"):
        return "bot"
    if n.startswith("dotnet") and "KalshiEvBot" in c and (re.search(r"\brun\b", c) or "KalshiEvBot.dll" in c):
        return "bot"
    if n.startswith("python") and "uvicorn" in c and "app:app" in c:
        return "sidecar"
    return None


class Sampler:
    def __init__(self) -> None:
        self.ncpu = psutil.cpu_count() or 1
        self.procs: dict[int, psutil.Process] = {}     # pid -> cached Process (cpu_percent needs a baseline)
        self.ctime: dict[int, float] = {}
        self.cmd_cache: dict[tuple[int, float], str] = {}
        self.io_prev: dict[int, int] = {}
        self.thr_prev: dict[tuple[int, int], float] = {}
        self.t_prev = time.time()
        self.disk_prev = psutil.disk_io_counters()
        self.net_prev = psutil.net_io_counters()
        psutil.cpu_percent(None)
        psutil.cpu_percent(None, percpu=True)
        self.me = psutil.Process()
        self.me.cpu_percent(None)

    def _assign(self) -> dict[int, str]:
        """pid -> group, by walking down from the roots. A child created BEFORE its recorded parent is a reused
        pid, not a child - Windows keeps a dead parent's pid in the record - so it is not followed."""
        info, kids = {}, defaultdict(list)
        for p in psutil.process_iter(["pid", "ppid", "name", "create_time"], ad_value=None):
            i = p.info
            info[i["pid"]] = (i["name"] or "", i["create_time"] or 0.0)
            kids[i["ppid"]].append(i["pid"])
        roots = []
        for pid, (name, ct) in info.items():
            if not CMDLINE_NAMES.match(name):
                continue
            key = (pid, ct)
            if key not in self.cmd_cache:
                try:
                    self.cmd_cache[key] = " ".join(psutil.Process(pid).cmdline())
                except (psutil.Error, OSError):
                    self.cmd_cache[key] = ""
            g = _root_group(name, self.cmd_cache[key])
            if g:
                roots.append((pid, g))
        live = {(pid, ct) for pid, (_, ct) in info.items()}
        self.cmd_cache = {k: v for k, v in self.cmd_cache.items() if k in live}
        out: dict[int, str] = {}
        me = os.getpid()                               # started by the bot, this IS in the bot's tree: its own
        # Report roots FIRST: a run sits inside the sidecar's or the bot's tree, and whichever walk reaches a
        # pid first owns it.
        roots.sort(key=lambda t: t[1] != "report")
        for root, grp in roots:                        # cost is MonitorCpuPct, never part of "bot"
            stack = [(root, grp == "sidecar" and info[root][0].lower() in BROWSER_NAMES)]
            while stack:
                pid, in_browser = stack.pop()
                if pid in out or pid == me:
                    continue
                name, ct = info.get(pid, ("", 0.0))
                if grp == "sidecar" and (in_browser or name.lower() in BROWSER_NAMES):
                    out[pid], in_browser = "browser", True
                else:
                    out[pid] = grp
                for k in kids.get(pid, ()):
                    if k in info and info[k][1] >= ct:
                        stack.append((k, in_browser))
        return out

    def _proc(self, pid: int) -> psutil.Process | None:
        p = self.procs.get(pid)
        try:
            if p is None or p.create_time() != self.ctime.get(pid):
                p = psutil.Process(pid)
                self.procs[pid], self.ctime[pid] = p, p.create_time()
                p.cpu_percent(None)                    # baseline; the first interval of a new process reads 0
            return p
        except (psutil.Error, OSError):
            return None

    def sample(self) -> dict:
        now = time.time()
        dt = max(1e-3, now - self.t_prev)
        self.t_prev = now
        assign = self._assign()
        agg = {g: defaultdict(float) for g in GROUPS}
        seen = set()
        for pid, grp in assign.items():
            p = self._proc(pid)
            if p is None:
                continue
            seen.add(pid)
            a = agg[grp]
            try:
                with p.oneshot():
                    cpu = p.cpu_percent(None)
                    mi = p.memory_info()
                    priv = getattr(mi, "private", None)
                    if priv is None:
                        priv = mi.rss - getattr(mi, "shared", 0)
                    a["Procs"] += 1
                    a["CpuCores"] += cpu / 100.0
                    a["PrivateMB"] += priv / MB
                    a["WorkingSetMB"] += mi.rss / MB
                    a["Threads"] += p.num_threads()
                    if WINDOWS:
                        a["Handles"] += p.num_handles()
                    try:
                        io = p.io_counters()
                        tot = io.read_bytes + io.write_bytes
                        if pid in self.io_prev:
                            a["IoKBs"] += max(0, tot - self.io_prev[pid]) / 1024 / dt
                        self.io_prev[pid] = tot
                    except (psutil.Error, OSError, AttributeError):
                        pass
                    name = p.name().lower()
                    is_root_proc = (grp == "bot" and name.startswith("kalshievbot")) or \
                                   (grp == "sidecar" and name.startswith("python"))
                    if is_root_proc:
                        a["Pid"] = a["Pid"] or pid
                        for t in p.threads():
                            busy = t.user_time + t.system_time
                            k = (pid, t.id)
                            if k in self.thr_prev:
                                a["HotThreadPct"] = max(a["HotThreadPct"], 100 * (busy - self.thr_prev[k]) / dt)
                            self.thr_prev[k] = busy
            except (psutil.Error, OSError):
                continue
        for pid in [x for x in self.procs if x not in seen]:
            self.procs.pop(pid, None)
            self.ctime.pop(pid, None)
            self.io_prev.pop(pid, None)
        self.thr_prev = {k: v for k, v in self.thr_prev.items() if k[0] in seen}

        sys_cpu = psutil.cpu_percent(None)
        cores = psutil.cpu_percent(None, percpu=True)
        vm, sw = psutil.virtual_memory(), psutil.swap_memory()
        disk, net = psutil.disk_io_counters(), psutil.net_io_counters()

        def rate(cur, prev, attr):
            return max(0, getattr(cur, attr) - getattr(prev, attr)) / 1024 / dt if cur and prev else 0.0

        row = {
            "Timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "IntervalSec": round(dt, 2),
            "SysCpuPct": round(sys_cpu, 1),
            "SysMaxCorePct": round(max(cores) if cores else 0.0, 1),
            "SysAvailMB": round(vm.available / MB),
            "SysUsedMB": round((vm.total - vm.available) / MB),
            "SwapUsedMB": round(sw.used / MB),
            "DiskReadKBs": round(rate(disk, self.disk_prev, "read_bytes"), 1),
            "DiskWriteKBs": round(rate(disk, self.disk_prev, "write_bytes"), 1),
            "NetRecvKBs": round(rate(net, self.net_prev, "bytes_recv"), 1),
            "NetSentKBs": round(rate(net, self.net_prev, "bytes_sent"), 1),
            "MonitorCpuPct": round(self.me.cpu_percent(None), 2),
        }
        self.disk_prev, self.net_prev = disk, net
        ours = 0.0
        for g in GROUPS:
            a = agg[g]
            ours += a["CpuCores"]
            for f in GROUP_FIELDS:
                v = a[f]
                row[f"{g}_{f}"] = int(v) if f in ("Procs", "Threads", "Handles", "Pid") else round(v, 3 if f == "CpuCores" else 1)
        row["OtherCpuCores"] = round(max(0.0, sys_cpu / 100 * self.ncpu - ours), 3)
        return row


def _alive(pid: int) -> bool:
    try:
        return pid > 0 and psutil.pid_exists(pid) and psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except (psutil.Error, OSError):
        return False


def _lock_holder(lock: str) -> int:
    """The pid of another LIVE recorder holding the lock, else 0 (a stale lock from a killed run is ignored)."""
    try:
        pid = int(open(lock, encoding="utf-8").read().strip() or 0)
    except (OSError, ValueError):
        return 0
    if pid == os.getpid() or not _alive(pid):
        return 0
    try:
        return pid if "resource_monitor" in " ".join(psutil.Process(pid).cmdline()) else 0
    except (psutil.Error, OSError):
        return 0


def record(out_dir: str, interval: float, quiet: bool = False, exit_with: int = 0) -> None:
    os.makedirs(out_dir, exist_ok=True)
    try:
        me = psutil.Process()
        me.nice(psutil.BELOW_NORMAL_PRIORITY_CLASS if WINDOWS else 10)
    except (psutil.Error, OSError, AttributeError):
        pass
    # ONE RECORDER. A bot restart starts a new child while the old one is still finishing its last sample, and
    # a copy started by hand may already be running; two writers would put every sample in the file twice.
    lock = os.path.join(out_dir, "monitor.lock")
    noted = False
    while (holder := _lock_holder(lock)):
        if exit_with and not _alive(exit_with):
            return
        if not noted:
            noted = True
            print(f"[RESOURCE] another recorder (pid {holder}) is running - waiting for it to stop.", flush=True)
        time.sleep(interval)
    with open(lock, "w", encoding="utf-8") as f:
        f.write(str(os.getpid()))
    try:
        _record(out_dir, interval, quiet, exit_with)
    finally:
        try:
            if open(lock, encoding="utf-8").read().strip() == str(os.getpid()):
                os.remove(lock)
        except OSError:
            pass


def _day_path(out_dir: str, day: str) -> str:
    """The day's file for THIS column set. A file an older version wrote (other columns) is left as it is and the
    day continues in ResourceUsage_<day>_v2.csv (v3, ...): appending rows of different columns under its header
    would misalign every row after it. The report reads both, by column name."""
    want = ",".join(COLUMNS)
    p = ""
    for v in range(1, 20):
        p = os.path.join(out_dir, f"ResourceUsage_{day}{'' if v == 1 else f'_v{v}'}.csv")
        if not os.path.exists(p):
            return p
        try:
            with open(p, encoding="utf-8") as f:
                if f.readline().strip() == want:
                    return p
        except OSError:
            pass
    return p


def _record(out_dir: str, interval: float, quiet: bool, exit_with: int) -> None:
    s = Sampler()
    paths: dict[str, str] = {}
    time.sleep(min(interval, 2.0))
    first = s.sample()
    print(f"[RESOURCE] recording every {interval:g}s to {out_dir}  ({s.ncpu} logical cores, "
          f"{psutil.virtual_memory().total / 2 ** 30:.1f} GB RAM). Ctrl+C to stop.")
    print(f"[RESOURCE] found: bot {first['bot_Procs']} process(es) (KalshiEvBot pid {first['bot_Pid'] or '-'}), "
          f"sidecar {first['sidecar_Procs']} (pid {first['sidecar_Pid'] or '-'}), browser {first['browser_Procs']}")
    last_print = 0.0
    time.sleep(interval)                               # a full interval before the first logged row, or its rates
    while True:                                        # (and the monitor's own CPU) cover a few milliseconds
        if exit_with and not _alive(exit_with):
            print(f"[RESOURCE] the process that started this recorder (pid {exit_with}) is gone - stopping.", flush=True)
            return
        t0 = time.time()
        row = s.sample()
        day = row["Timestamp"][:10].replace("-", "")
        if day not in paths:
            paths[day] = _day_path(out_dir, day)
        path = paths[day]
        new = not os.path.exists(path)
        with open(path, "a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=COLUMNS)
            if new:
                w.writeheader()
            w.writerow(row)
        if not quiet and t0 - last_print >= 60:
            last_print = t0
            print(f"[{datetime.now().strftime('%H:%M:%S')}] "
                  + " | ".join(f"{g} {row[f'{g}_CpuCores']:.2f} cores {row[f'{g}_PrivateMB']:.0f} MB"
                               + (f" ({row[f'{g}_Procs']} procs)" if g == "browser" else "") for g in GROUPS)
                  + f" | system CPU {row['SysCpuPct']:.0f}%, {row['SysAvailMB'] / 1024:.1f} GB free"
                  + f" | monitor {row['MonitorCpuPct']:.1f}%", flush=True)
        time.sleep(max(0.5, interval - (time.time() - t0)))


# ── report ──────────────────────────────────────────────────────────────────────────────────────────
def _pct(v, q):
    v = sorted(x for x in v if x == x)
    if not v:
        return float("nan")
    k = (len(v) - 1) * q
    lo, hi = math.floor(k), math.ceil(k)
    return v[lo] + (v[hi] - v[lo]) * (k - lo)


def _num(r, k):
    try:
        return float(r.get(k) or "nan")
    except ValueError:
        return float("nan")


def _max(v):
    v = [x for x in v if x == x]                       # NaN-safe: a file from before a group existed reads NaN
    return max(v) if v else float("nan")


def _z(x):
    return x if x == x else 0.0


def report(out_dir: str, days: int) -> None:
    by_date = defaultdict(list)                        # a day whose columns changed mid-day has a _v2 file too
    for f in glob.glob(os.path.join(out_dir, "ResourceUsage_*.csv")):
        m = re.match(r"ResourceUsage_(\d{8})", os.path.basename(f))
        if m:
            by_date[m.group(1)].append(f)
    files = [f for d in sorted(by_date)[-days:] for f in sorted(by_date[d])]
    rows = [r for f in files for r in csv.DictReader(open(f, encoding="utf-8", newline=""))]
    rows.sort(key=lambda r: r.get("Timestamp", ""))
    if not rows:
        sys.exit(f"no samples in {out_dir} yet - run `python resource_monitor.py` first")
    N = lambda r, k: _num(r, k)
    up = [r for r in rows if N(r, "browser_Procs") > 0]
    running = [r for r in rows if N(r, "bot_Procs") > 0 or N(r, "sidecar_Procs") > 0]
    hours = sum(N(r, "IntervalSec") for r in rows) / 3600
    up_h = sum(N(r, "IntervalSec") for r in up) / 3600
    print(f"RESOURCE REPORT  {len(rows)} samples, {hours:.1f} h recorded ({up_h:.1f} h with the browser up = a work "
          f"window), {rows[0]['Timestamp']} -> {rows[-1]['Timestamp']}")
    print(f"   machine: {psutil.cpu_count(logical=False)} physical / {psutil.cpu_count()} logical cores, "
          f"{psutil.virtual_memory().total / 2 ** 30:.1f} GB RAM.  CPU is in CORES: 1.0 = one core flat out.\n")

    def block(title, sel):
        if not sel:
            print(f"   {title}: no samples")
            return
        print(f"   {title}  ({len(sel)} samples)")
        print("      group     CPU cores p50 / p95 / max    busiest thread p95 / max   private MB p50 / max   "
              "working set MB max   I/O KB/s p95   procs")
        for g in STACK:
            c = [N(r, f"{g}_CpuCores") for r in sel]
            hot = [N(r, f"{g}_HotThreadPct") for r in sel]
            pm = [N(r, f"{g}_PrivateMB") for r in sel]
            print(f"      {g:8} {_pct(c, .5):6.2f} {_pct(c, .95):6.2f} {_max(c):6.2f}       "
                  + (f"{_pct(hot, .95):5.0f}% {_max(hot):5.0f}%      " if g != "browser" else "      -             ")
                  + f"{_pct(pm, .5):7.0f} {_max(pm):7.0f}        {_max(N(r, f'{g}_WorkingSetMB') for r in sel):7.0f}"
                  + f"          {_pct([N(r, f'{g}_IoKBs') for r in sel], .95):7.1f}   "
                  + f"{_pct([N(r, f'{g}_Procs') for r in sel], .5):.0f}")
        stack_c = [sum(_z(N(r, f"{g}_CpuCores")) for g in STACK) for r in sel]
        stack_m = [sum(_z(N(r, f"{g}_PrivateMB")) for g in STACK) for r in sel]
        print(f"      STACK    {_pct(stack_c, .5):6.2f} {_pct(stack_c, .95):6.2f} {_max(stack_c):6.2f}"
              f"                                 {_pct(stack_m, .5):7.0f} {_max(stack_m):7.0f}")
        oc = [N(r, "OtherCpuCores") for r in sel]
        print(f"      other    {_pct(oc, .5):6.2f} {_pct(oc, .95):6.2f} {_max(oc):6.2f}   (everything else on this PC)")

    block("WORK WINDOWS (browser up)", up)
    block("DARK (bot + sidecar only)", [r for r in running if N(r, "browser_Procs") == 0])
    # report runs: consecutive samples with a report process alive
    runs, cur = [], None
    for r in rows:
        if N(r, "report_Procs") > 0:
            if cur is None:
                cur = []
                runs.append(cur)
            cur.append(r)
        else:
            cur = None
    if runs:
        peaks = [_max(N(r, "report_PrivateMB") for r in x) for x in runs]
        secs = [(datetime.strptime(x[-1]["Timestamp"], "%Y-%m-%dT%H:%M:%SZ")
                 - datetime.strptime(x[0]["Timestamp"], "%Y-%m-%dT%H:%M:%SZ")).total_seconds() + _z(N(x[0], "IntervalSec"))
                for x in runs]
        print(f"   REPORT RUNS  {len(runs)} run(s), ~{_pct(secs, .5):.0f} s each; peak RAM {_pct(peaks, .5):.0f} MB typical, "
              f"{_max(peaks):.0f} MB max; CPU up to {_max(N(r, 'report_CpuCores') for x in runs for r in x):.2f} cores")
    peak_all = _max(sum(_z(N(r, f"{g}_PrivateMB")) for g in GROUPS) for r in rows)
    print(f"   PEAK         stack + any report running at that moment: {peak_all / 1024:.1f} GB private at once\n")

    sc = [N(r, "SysCpuPct") for r in running]
    print(f"   SYSTEM while the stack ran: CPU {_pct(sc, .5):.0f}% / {_pct(sc, .95):.0f}% / {max(sc):.0f}% (p50/p95/max), "
          f"busiest core p95 {_pct([N(r, 'SysMaxCorePct') for r in running], .95):.0f}%, "
          f"free RAM min {min(N(r, 'SysAvailMB') for r in running) / 1024:.1f} GB, "
          f"swap max {max(N(r, 'SwapUsedMB') for r in running) / 1024:.1f} GB")
    net = [N(r, "NetRecvKBs") + N(r, "NetSentKBs") for r in up]
    disk = [N(r, "DiskWriteKBs") for r in running]
    if net:
        print(f"   NETWORK (whole PC, an upper bound for the stack) in windows: p95 {_pct(net, .95):.0f} KB/s, "
              f"max {max(net):.0f} KB/s;  disk writes p95 {_pct(disk, .95):.0f} KB/s")
    busy = sum(1 for r in running if N(r, "SysCpuPct") > 85) / max(1, len(running))
    core = sum(1 for r in running if N(r, "SysMaxCorePct") > 95) / max(1, len(running))
    lowm = sum(1 for r in running if N(r, "SysAvailMB") < 0.10 * psutil.virtual_memory().total / MB) / max(1, len(running))
    print(f"   CONTENTION: system CPU > 85% in {100 * busy:.1f}% of samples, a core > 95% in {100 * core:.1f}%, "
          f"free RAM < 10% in {100 * lowm:.1f}%")
    mon = [N(r, "MonitorCpuPct") for r in rows]
    print(f"   this monitor's own CPU: p50 {_pct(mon, .5):.1f}% / max {max(mon):.1f}% of one core\n")

    # memory trend per run (a run = one KalshiEvBot / sidecar pid), only runs long enough to mean something
    print("   MEMORY TREND per run (private MB/hour; a steady climb across a long run is a leak):")
    for g, pidcol in (("bot", "bot_Pid"), ("sidecar", "sidecar_Pid")):
        runs = defaultdict(list)
        for r in rows:
            if N(r, pidcol) > 0:
                t = datetime.strptime(r["Timestamp"], "%Y-%m-%dT%H:%M:%SZ").timestamp()
                runs[int(N(r, pidcol))].append((t, N(r, f"{g}_PrivateMB")))
        for pid, pts in runs.items():
            if len(pts) < 20 or pts[-1][0] - pts[0][0] < 2 * 3600:
                continue
            mt = sum(t for t, _ in pts) / len(pts)
            mm = sum(m for _, m in pts) / len(pts)
            slope = (sum((t - mt) * (m - mm) for t, m in pts) / max(1e-9, sum((t - mt) ** 2 for t, _ in pts))) * 3600
            print(f"      {g:8} pid {pid:6}: {pts[0][1]:6.0f} -> {pts[-1][1]:6.0f} MB over "
                  f"{(pts[-1][0] - pts[0][0]) / 3600:4.1f} h  ({slope:+.1f} MB/h)")
    print()

    # data the stack writes (telemetry CSVs + logs), per UTC day, from the files themselves
    per_day = defaultdict(int)
    for f in glob.glob(os.path.join(HERE, "*_2026*.csv")) + glob.glob(os.path.join(HERE, "*_2027*.csv")):
        m = re.search(r"_(\d{8})(?:_v\d+)?\.csv$", f)
        if m:
            per_day[m.group(1)] += os.path.getsize(f)
    recent = sorted(per_day.items())[-7:]
    if recent:
        avg = sum(v for _, v in recent) / len(recent) / MB
        print(f"   DATA WRITTEN (telemetry CSVs in the repo root, last {len(recent)} days): "
              + ", ".join(f"{d[4:6]}-{d[6:]} {v / MB:.0f} MB" for d, v in recent)
              + f"  -> ~{avg:.0f} MB/day, ~{avg * 365 / 1024:.0f} GB/year\n")

    # does load slow the bot? the bot->sidecar oracle hop by system CPU at that moment
    stamps = [datetime.strptime(r["Timestamp"], "%Y-%m-%dT%H:%M:%SZ").timestamp() for r in rows]
    buckets = defaultdict(list)
    for day in sorted({re.search(r"(\d{8})", os.path.basename(f)).group(1) for f in files}):
        tel = os.path.join(HERE, f"EvTelemetry_{day}.csv")
        if not os.path.exists(tel):
            continue
        csv.field_size_limit(10 ** 9)
        for t in csv.DictReader(open(tel, encoding="utf-8", newline="")):
            age = _num(t, "OracleAgeMs")
            if age != age:
                continue
            try:
                ts = datetime.fromisoformat(t["Timestamp"].replace("Z", "+00:00")[:26]).replace(tzinfo=timezone.utc).timestamp()
            except ValueError:
                continue
            i = bisect.bisect_left(stamps, ts)
            near = [j for j in (i - 1, i) if 0 <= j < len(stamps) and abs(stamps[j] - ts) <= 10]
            if not near:
                continue
            cpu = N(rows[min(near, key=lambda j: abs(stamps[j] - ts))], "SysCpuPct")
            buckets["< 50%" if cpu < 50 else "50-80%" if cpu < 80 else ">= 80%"].append(age)
    if buckets:
        print("   DOES LOAD SLOW THE BOT? tennis oracle read (OracleAgeMs) by system CPU at that moment:")
        for b in ("< 50%", "50-80%", ">= 80%"):
            v = buckets.get(b, [])
            if v:
                print(f"      system CPU {b:7} n={len(v):6}   p50 {_pct(v, .5):5.0f} ms   p90 {_pct(v, .9):5.0f} ms   "
                      f"p99 {_pct(v, .99):5.0f} ms")
        print("      flat across the rows = the machine has headroom; rising with CPU = the bot is waiting on it\n")

    # sizing: measured peaks with headroom, as a starting point rather than a verdict
    if up:
        sc95 = _pct([sum(_z(N(r, f"{g}_CpuCores")) for g in STACK) for r in up], .99)
        smax = peak_all / 1024                         # stack + a report, the moment RAM actually has to cover
        hot = max(_pct([N(r, f"{g}_HotThreadPct") for r in up], .95) for g in ("bot", "sidecar"))
        cores = max(4, math.ceil(2 * sc95))
        ram = 8 * math.ceil((smax * 1.5 + 4) / 8)
        print("   SIZING (this PC's measurements, with headroom - a starting point, not a verdict):")
        print(f"      CPU   the stack's p99 in a window is {sc95:.2f} cores -> {cores}+ physical cores leaves it 2x room")
        print(f"      CORE  busiest bot/sidecar thread p95 {hot:.0f}% of a core"
              + (" -> single-core speed matters: favour a high-clock CPU" if hot > 60 else
                 " -> no single thread is near a core's limit; core count matters more than clock"))
        print(f"      RAM   peak private memory incl. a report run {smax:.1f} GB -> {ram} GB total (1.5x + 4 GB for the OS)")
        if recent:
            print(f"      DISK  ~{avg * 365 / 1024:.0f} GB/year of telemetry -> any 256 GB+ SSD; an SSD matters more than size")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--report", action="store_true", help="summarise recorded samples instead of recording")
    ap.add_argument("--days", type=int, default=7, help="report: how many daily files to read (default 7)")
    ap.add_argument("--interval", type=float, default=float(os.environ.get("RESOURCE_SAMPLE_SEC", "5")))
    ap.add_argument("--out", default=os.path.join(HERE, "resource_logs"), help="where the daily CSVs go")
    ap.add_argument("--quiet", action="store_true", help="no per-minute console line (the bot's child uses this)")
    ap.add_argument("--exit-with", type=int, default=0, metavar="PID",
                    help="stop when this process exits (the bot passes its own pid, so a crash leaves no orphan)")
    a = ap.parse_args()
    if a.report:
        report(a.out, a.days)
    else:
        try:
            record(a.out, a.interval, a.quiet, a.exit_with)
        except KeyboardInterrupt:
            print("\n[RESOURCE] stopped.")


if __name__ == "__main__":
    main()
