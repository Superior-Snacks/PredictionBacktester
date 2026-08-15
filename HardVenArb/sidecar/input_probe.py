"""Does BetInAsia read the MouseEvent properties that give a CDP click away?

Installs getters on MouseEvent/PointerEvent prototypes, waits while a click happens, then reports which
properties the site's own code read and from where. That decides the execution architecture:

  reads screenX / screenY  -> CDP can NEVER set those. The OS mouse (BIA_CLICK_MODE=os_hybrid) is the
                              only route, and the dismissal is a deliberate check.
  reads pressure only      -> a raw CDP dispatch can set `force`. Cheap fix. (Already tested and it did
                              NOT save the slip, so this outcome would be surprising and worth a re-test.)
  reads nothing            -> the venue is not inspecting clicks at all. The slip dying has another
                              cause and os_hybrid would be treating a symptom. The venue's own
                              /web/metrics/ telemetry carries no input data either, which points the
                              same way -- see metrics_audit.py.

Prints the nested `reads` properly, which PowerShell's default table view collapses to nothing.

    python input_probe.py                 # install, prompt for a click, report
    python input_probe.py --read-only     # just report what has accumulated
    python input_probe.py --reset         # clear the counters and keep watching
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.request

sys.stdout.reconfigure(encoding="utf-8")


def _get(url, timeout=25):
    return json.load(urllib.request.urlopen(url, timeout=timeout))


def report(d: dict) -> None:
    reads = d.get("reads") or {}
    if not reads:
        print("\nNOTHING READ.")
        print("The site did not touch pressure / screenX / screenY / movementX / pointerType on any")
        print("mouse event. So it is NOT inspecting the click, and the betslip dying has a different")
        print("cause -- os_hybrid would be treating a symptom rather than the disease.")
        print("(Caveat: the probe only sees reads that happen AFTER it was installed, and only on the")
        print(" page it was installed on. Re-install after any navigation.)")
        return
    print(f"\n{len(reads)} distinct read site(s):\n")
    interesting = []
    for k, n in sorted(reads.items(), key=lambda kv: -kv[1]):
        prop = k.split(" @ ")[0]
        print(f"  {n:5}x  {k}")
        if prop in ("screenX", "screenY", "pressure", "movementX", "movementY", "pointerType"):
            interesting.append(prop)
    if interesting:
        uniq = sorted(set(interesting))
        print(f"\nTELLTALE PROPERTIES READ: {', '.join(uniq)}")
        if "screenX" in uniq or "screenY" in uniq:
            print("=> screen coordinates. CDP sets these equal to clientX/clientY and cannot be made to")
            print("   do otherwise, so BIA_CLICK_MODE=os_hybrid is REQUIRED, not optional.")
        elif "pressure" in uniq:
            print("=> pressure only. A raw CDP dispatch can set force=0.5 (BIA_CLICK_MODE=cdp_raw).")
            print("   That was already tried and the slip still died -- so re-test carefully, or the")
            print("   read is incidental and something else closes the slip.")
    else:
        print("\nReads happened, but none of the telltale properties. Likely ordinary UI code.")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8788)
    ap.add_argument("--read-only", action="store_true")
    ap.add_argument("--reset", action="store_true")
    a = ap.parse_args()
    base = f"http://127.0.0.1:{a.port}/debug/input_probe"

    try:
        from staleness import check
        check(a.port)
    except ImportError:
        pass

    if a.reset:
        print(json.dumps(_get(f"{base}?reset=true")))
        print("counters cleared; click something, then run with --read-only")
        return 0

    d = _get(base)
    if not d.get("ok"):
        print(f"probe failed: {d.get('error')}")
        return 1
    print(f"probe on {d.get('url')}  ({'already installed' if d.get('already') else 'installed now'})")

    if a.read_only:
        report(d)
        return 0

    print("\nNOW: click a moneyline in the sidecar's browser window (any way -- by hand, or run")
    print("     slip_hold.py from another terminal). Then press Enter here.")
    try:
        input()
    except (EOFError, KeyboardInterrupt):
        print("(no input; reading anyway)")
    report(_get(base))
    print("\nThis probe replaces the page's own getters, so restart the sidecar when you are done")
    print("rather than leaving it installed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
