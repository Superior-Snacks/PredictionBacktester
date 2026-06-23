#!/usr/bin/env python3
"""server_health.py — is the e2-medium throttling the HardVenArb bot / Chrome?

Read-only snapshot of the throttle-relevant pressure on a Linux server, so you can tell whether the VM
is starving the bot (the silent failure mode: Chrome gets throttled -> odds fetches slow -> HardVen
quotes go stale -> telemetry quietly dries up):

  * CPU steal %       - the hypervisor de-scheduling this VM. On a shared-core e2, burst-credit
                        exhaustion shows up here: sustained steal > ~10% == you ARE being throttled.
  * load per core     - CPU saturation (load1 / nproc).
  * RAM + swap         - memory pressure; heavy swap use makes Chrome crawl (the reason we added swap).
  * bot/Chrome procs   - summed %CPU + RSS for chrome / dotnet / python.
  * sidecar round-trip - times GET /health so a sluggish sidecar/Chrome is visible directly.

Prints OK or WARN + the specific reasons. Complements the bot's own logs: when it warns about STALE
quotes or a poll timeout, run this to see WHY -- throttled vs swapping vs just a network blip.

  python3 server_health.py                      # one-shot report
  python3 server_health.py --watch 10           # refresh every 10s (Ctrl-C to stop)
  SIDECAR=http://127.0.0.1:8787 python3 server_health.py
  python3 server_health.py --sidecar http://127.0.0.1:8787

Linux only (reads /proc). No third-party deps.
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import time
import urllib.request

# Thresholds -> a WARN reason. Tuned for an e2-medium (2 vCPU shared-core, 4 GB) running ONE Chrome.
STEAL_PCT_WARN   = 10.0   # CPU stolen by the hypervisor -> being throttled
LOAD_PER_CORE_WARN = 1.5  # run-queue longer than the cores can serve
MEM_AVAIL_MIB_WARN = 300  # less headroom than this and a Chrome spike risks the OOM killer
SWAP_USED_MIB_WARN = 512  # actively swapping -> Chrome latency
SIDECAR_SEC_WARN   = 2.0  # GET /health slower than this -> sidecar/Chrome sluggish


def _cpu_counters() -> tuple[int, int, int]:
    """(total, idle, steal) jiffies from the aggregate /proc/stat cpu line."""
    with open("/proc/stat") as f:
        v = [int(x) for x in f.readline().split()[1:]]   # user nice system idle iowait irq softirq steal ...
    return sum(v[:8]), v[3] + v[4], v[7]                 # total(user..steal), idle+iowait, steal


def cpu_sample(interval: float = 1.0) -> tuple[float, float]:
    """Busy% and steal% over `interval` (two /proc/stat reads)."""
    t1, i1, s1 = _cpu_counters()
    time.sleep(interval)
    t2, i2, s2 = _cpu_counters()
    dt = max(t2 - t1, 1)
    return 100.0 * (dt - (i2 - i1)) / dt, 100.0 * (s2 - s1) / dt


def meminfo() -> tuple[int, int, int, int]:
    """(total, available, swap_total, swap_used) in MiB."""
    d = {}
    with open("/proc/meminfo") as f:
        for line in f:
            k, _, val = line.partition(":")
            d[k.strip()] = int(val.split()[0])   # kB
    avail = d.get("MemAvailable", d.get("MemFree", 0))
    swap_used = d.get("SwapTotal", 0) - d.get("SwapFree", 0)
    return d["MemTotal"] // 1024, avail // 1024, d.get("SwapTotal", 0) // 1024, swap_used // 1024


def proc_sum(pattern: str) -> str:
    """Summed %CPU + RSS (MiB) for processes whose comm matches `pattern`."""
    try:
        out = subprocess.run(["ps", "-eo", "comm,%cpu,rss", "--no-headers"],
                             capture_output=True, text=True, timeout=5).stdout
    except Exception:
        return "(ps unavailable)"
    rx, cpu, rss = re.compile(pattern), 0.0, 0
    for line in out.splitlines():
        f = line.split()
        if len(f) < 3 or not rx.search(f[0]):
            continue
        try:
            cpu += float(f[1]); rss += int(f[2])
        except ValueError:
            pass
    return f"{cpu:4.0f}% cpu, {rss // 1024:5d} MiB"


def sidecar_time(base: str) -> tuple[float | None, str | None]:
    """Seconds to GET /health, or (None, error)."""
    url = base.rstrip("/") + "/health"
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            r.read()
        return time.perf_counter() - t0, None
    except Exception as e:
        return None, str(e)


def report(sidecar: str) -> None:
    busy, steal = cpu_sample()
    cores = os.cpu_count() or 1
    with open("/proc/loadavg") as f:
        load1 = float(f.readline().split()[0])
    lpc = load1 / cores
    memtot, memavail, swaptot, swapused = meminfo()
    sc_sec, sc_err = sidecar_time(sidecar)

    warn: list[str] = []
    if steal > STEAL_PCT_WARN:
        warn.append(f"CPU steal {steal:.1f}% — the hypervisor is throttling this VM (burst exhausted)")
    if lpc > LOAD_PER_CORE_WARN:
        warn.append(f"load {load1:.2f} on {cores} cores ({lpc:.2f}/core) — CPU saturated")
    if memavail < MEM_AVAIL_MIB_WARN:
        warn.append(f"only {memavail} MiB RAM available — Chrome spike risks the OOM killer")
    if swapused > SWAP_USED_MIB_WARN:
        warn.append(f"{swapused} MiB swap in use — active swapping makes Chrome crawl")
    if sc_sec is None:
        warn.append(f"sidecar {sidecar} not responding ({sc_err})")
    elif sc_sec > SIDECAR_SEC_WARN:
        warn.append(f"sidecar /health took {sc_sec:.2f}s — sidecar/Chrome sluggish")

    sc_txt = f"{sc_sec:.3f}s" if sc_sec is not None else f"UNREACHABLE ({sc_err})"
    print(f"── HardVenArb server health  {time.strftime('%H:%M:%S')} ──")
    print(f"CPU    busy {busy:5.1f}%   steal {steal:4.1f}%    load {load1:.2f} / {cores} cores ({lpc:.2f}/core)")
    print(f"MEM    {memavail} / {memtot} MiB avail     swap {swapused} / {swaptot} MiB used")
    print(f"PROC   chrome : {proc_sum('chrome')}")
    print(f"       dotnet : {proc_sum('dotnet|HardVen')}")
    print(f"       python : {proc_sum('python')}")
    print(f"SIDE   {sidecar.rstrip('/')}/health  →  {sc_txt}")
    if warn:
        print("VERDICT  ⚠️  WARN:")
        for w in warn:
            print(f"         • {w}")
    else:
        print("VERDICT  ✅ OK — no throttling or memory pressure detected")


def main() -> None:
    ap = argparse.ArgumentParser(description="HardVenArb server resource/throttle health check.")
    ap.add_argument("--sidecar", default=os.environ.get("SIDECAR", "http://127.0.0.1:8787"),
                    help="sidecar base URL (default $SIDECAR or http://127.0.0.1:8787)")
    ap.add_argument("--watch", type=float, metavar="SEC",
                    help="refresh every SEC seconds instead of a one-shot report")
    args = ap.parse_args()

    if not os.path.exists("/proc/stat"):
        sys.exit("server_health.py is Linux-only (needs /proc) — run it ON the server, not Windows.")

    if not args.watch:
        report(args.sidecar)
        return
    try:
        while True:
            print("\033[2J\033[H", end="")   # clear screen + home cursor
            report(args.sidecar)
            time.sleep(max(args.watch, 1.0))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
