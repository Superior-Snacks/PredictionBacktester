"""Is the running sidecar executing the code that is on disk right now?

Python does not hot-reload. An edited adapter is inert until the process restarts, and a test run against
a stale process produces results that look authoritative and are not. On 2026-08-15 that pattern burned
several hours: fixes were written, tested against a sidecar that had not been restarted, and the
unchanged failure was read as "the fix did not work" rather than "the fix is not running".

`/health` publishes the source hashes AS LOADED. This compares them against the files and says plainly
whether any conclusion drawn from this process is trustworthy.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import urllib.request

sys.stdout.reconfigure(encoding="utf-8")


def disk_hashes() -> dict:
    here = os.path.dirname(os.path.abspath(__file__))
    out = {}
    for f in ("betinasia_adapter.py", "pinnacle_adapter.py", "betinasia_observer.py", "app.py"):
        p = os.path.join(here, f)
        if os.path.exists(p):
            with open(p, "rb") as fh:
                out[f] = hashlib.sha256(fh.read()).hexdigest()[:10]
    return out


def check(port: int = 8788, quiet: bool = False) -> bool:
    """True if the sidecar is running current code. Prints a loud warning when it is not."""
    try:
        h = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=5))
    except Exception as e:
        print(f"[STALE?] sidecar unreachable on {port}: {type(e).__name__}")
        return False
    running = h.get("code")
    if not running:
        print("[STALE?] this sidecar predates the /health code fingerprint — it is running OLD code.\n"
              "         RESTART IT before trusting anything below.")
        return False
    disk = disk_hashes()
    drift = [f for f, v in disk.items() if f in running and running[f] != v]
    if drift:
        print("=" * 78)
        print("STALE SIDECAR — it is NOT running the code on disk. Edited since it started:")
        for f in drift:
            print(f"   {f}   running {running[f]}  vs  disk {disk[f]}")
        print(f"   (up {h.get('uptime_sec', '?')}s)")
        print("RESTART IT. Results from this process describe the OLD code and will mislead.")
        print("=" * 78)
        return False
    if not quiet:
        print(f"[code] sidecar matches disk (up {h.get('uptime_sec', '?')}s)")
    return True


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8788
    sys.exit(0 if check(port) else 1)
