"""Why does a HUMAN-opened betslip survive when a BOT-opened one dies within seconds?

The operator established the fact that matters: holding the mouse still — physically upside down, no
input at all — a slip they clicked themselves stays open indefinitely, while a slip the bot clicks is
dismissed in ~1-3s. So it is not mouse motion, not our polling, not navigation, and not a timer. The
difference is in the click, or in the context the click happens in.

Two candidate explanations, and they need different fixes:

  A. THE CLICK IS MARKED. CDP-dispatched input is `isTrusted`, but it is not identical to a real mouse:
     `pressure` is 0 rather than ~0.5 on mousedown, `movementX/Y` are 0, and screenX/screenY are set
     equal to clientX/clientY instead of being offset by the window's position on screen.

  B. THE WINDOW IS NOT FOCUSED. When the bot clicks, the operator is in a terminal — so the browser has
     no OS focus and `document.hasFocus()` is false. `bring_to_front()` raises a TAB within the browser;
     it does not give the BROWSER window focus. The venue is already known to poll `visibilityState`
     ~1.1x/s, so it demonstrably watches this class of signal.

  --countdown lets the operator hover the real mouse over the moneyline (and focus the window) and have
  the bot click it N seconds later. That isolates the click from its context: same cursor position, same
  focused window, only the click's origin differs.

    python click_compare.py --countdown 8        # you hover + focus; bot clicks after 8s
    python click_compare.py                      # bot clicks cold, for the baseline
    python click_compare.py --watch 40           # how long the slip then survives

Reports document.hasFocus()/visibilityState BEFORE the click, AFTER it, and when the slip dies.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import time
import urllib.parse
import urllib.request

sys.stdout.reconfigure(encoding="utf-8")


def _get(url, timeout=30):
    return json.load(urllib.request.urlopen(url, timeout=timeout))


def _post(url, timeout=90):
    return json.load(urllib.request.urlopen(
        urllib.request.Request(url, data=b"", method="POST"), timeout=timeout))


def focus_state(base: str) -> str:
    """What the SITE sees: visibilityState + hasFocus per tab."""
    try:
        d = _get(f"{base}/debug/visibility", timeout=15)
    except Exception as e:
        return f"(visibility read failed: {type(e).__name__})"
    bits = []
    for t in d.get("tabs") or []:
        if t.get("error"):
            continue
        bits.append(f"{t.get('tab', '?')}: vis={t.get('vis')} focus={t.get('focus')}")
    return "  |  ".join(bits) or "(no tabs reported)"


def slip_alive(base: str) -> bool:
    try:
        d = _get(f"{base}/debug/slip_dom", timeout=20)
    except Exception:
        return False
    return any("price-input" in (p.get("inputs") or "") for p in (d.get("pages") or []))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8788)
    ap.add_argument("--sport", default="tennis")
    ap.add_argument("--selection-id", default=None)
    ap.add_argument("--countdown", type=float, default=0.0,
                    help="seconds to wait before clicking, so you can hover the real mouse over the "
                         "moneyline and click into the browser window to focus it")
    ap.add_argument("--watch", type=float, default=30.0)
    a = ap.parse_args()
    base = f"http://127.0.0.1:{a.port}"

    try:
        from staleness import check
        check(a.port)
    except ImportError:
        pass

    sid = a.selection_id
    if not sid:
        now = dt.datetime.now(dt.timezone.utc)
        best = None
        for e in _get(f"{base}/catalog")["selections"]:
            if e.get("three_way") or e.get("sport") != a.sport or not e.get("start_time"):
                continue
            try:
                t = dt.datetime.fromisoformat(e["start_time"].replace("Z", "+00:00"))
            except Exception:
                continue
            m = (t - now).total_seconds() / 60
            if 40 < m < 480 and (best is None or m < best[0]):
                best = (m, e)
        if not best:
            print("no pre-live candidate")
            return 1
        sid = best[1]["selection_id"]
        print(f"target: {best[1]['event']} -- {best[1]['selection_name']} (in {best[0]:.0f}m)")
    print(f"        {sid}\n")

    if a.countdown > 0:
        print("=" * 74)
        print("NOW: put the real mouse over that moneyline in the sidecar's browser window, and")
        print("     CLICK ONCE somewhere neutral in the window first so it has OS focus.")
        print(f"     The bot clicks in {a.countdown:.0f}s. Do not touch anything after that.")
        print("=" * 74)
        for r in range(int(a.countdown), 0, -1):
            print(f"  {r}...", end="\r", flush=True)
            time.sleep(1)
        print("  clicking now.        ")

    print(f"BEFORE click: {focus_state(base)}")
    t0 = time.time()
    try:
        q = _post(f"{base}/slip_quote?selection_id=" + urllib.parse.quote(sid, safe=""))
    except Exception as e:
        print(f"slip_quote failed: {type(e).__name__}: {e}")
        return 1
    if not q.get("ok"):
        print(f"slip_quote refused: {q.get('error')}")
        return 1
    print(f"quoted {q.get('decimal_odds')} in {time.time() - t0:.1f}s (clicked={q.get('clicked')})")
    print(f"AFTER  click: {focus_state(base)}\n")

    print(f"watching for {a.watch:.0f}s...")
    died = None
    deadline = time.time() + a.watch
    while time.time() < deadline:
        if not slip_alive(base):
            died = time.time() - t0
            break
        time.sleep(1.0)

    print()
    if died is None:
        print(f"SLIP SURVIVED {a.watch:.0f}s.")
        if a.countdown > 0:
            print("=> With the window focused and the cursor already on the row, the BOT's click produced")
            print("   a slip that lives. The click itself is fine; the missing ingredient is CONTEXT")
            print("   (focus / cursor position) -- which the bot can arrange without faking any input.")
    else:
        print(f"SLIP DIED at t+{died:.1f}s.   {focus_state(base)}")
        if a.countdown > 0:
            print("=> Even with the window focused and the real cursor parked on the row, the bot's click")
            print("   produces a short-lived slip while yours does not. That points at the CLICK itself")
            print("   being distinguishable (pressure/movement/screenXY on CDP-dispatched input),")
            print("   not at the context.")
    try:
        _post(f"{base}/slip_close", timeout=20)
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
