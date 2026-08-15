"""The bot aims; YOU press. Isolates the button press from everything else.

By 2026-08-15 these had each been eliminated as the reason a bot-opened betslip dies in ~1-3s while a
hand-opened one lives:

  - our own polling            slip_hold.py --quiet-secs 20 (no reads at all) still died
  - navigation                 same tab, same URL across the death
  - our own slip_close         no `closed ... with Escape` in the console during the window
  - the sport walk / organic   died before either could run
  - window focus               click_compare.py: vis=visible focus=True throughout, died anyway
  - input forensics            input_probe.py: every property read was React's SyntheticEvent
                               constructor plus two by Google Analytics. The venue reads nothing.
  - pointer position           --park-mouse with a MEASURED calibration; still died
  - aim accuracy               two-point calibration proved correct (scale 1.25 on a 125% display)

What is left is the press. This run has the bot find the row, scroll it into view and travel the physical
cursor onto the price cell — then stop. You press the button with the mouse held in the air, so no motion
is added. Everything is identical to a bot click except who closed the switch.

    python hand_click.py --nth 2

  slip survives -> the PRESS is the difference. SendInput is not equivalent to a hardware button for
                   this site, and no amount of aiming fixes it.
  slip dies     -> the press is NOT the difference either, and the original real_click.py result (n=1)
                   did not reproduce. There was never a distinction to explain.
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
    ap.add_argument("--nth", type=int, default=0)
    ap.add_argument("--watch", type=float, default=45.0)
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
        cands = []
        for e in _get(f"{base}/catalog")["selections"]:
            if e.get("three_way") or e.get("sport") != a.sport or not e.get("start_time"):
                continue
            try:
                t = dt.datetime.fromisoformat(e["start_time"].replace("Z", "+00:00"))
            except Exception:
                continue
            m = (t - now).total_seconds() / 60
            if 40 < m < 480:
                cands.append((m, e["event"], e["selection_name"], e["selection_id"]))
        cands.sort(key=lambda r: (r[0], r[3]))
        if not cands or a.nth >= len(cands):
            print(f"no candidate at --nth {a.nth} ({len(cands)} available)")
            return 1
        m, ev, seln, sid = cands[a.nth]
        print(f"target[{a.nth}/{len(cands) - 1}]: {ev} -- {seln} (starts in {m:.0f}m)")
    print(f"        {sid}\n")

    if slip_alive(base):
        try:
            _post(f"{base}/slip_close", timeout=20)
        except Exception:
            pass

    print("The bot will now move YOUR cursor onto that price cell and stop. Do not touch the mouse.")
    print("Take your hand off it now.\n")
    time.sleep(3)
    try:
        r = _post(f"{base}/debug/aim?selection_id=" + urllib.parse.quote(sid, safe=""))
    except Exception as e:
        print(f"aim failed: {type(e).__name__}: {e}")
        return 1
    if not r.get("aimed"):
        print(f"aim did not complete: {r.get('error')}")
        return 1
    print(f"AIMED.  client={r.get('client')}  screen={r.get('screen')}  "
          f"cursor_now={r.get('cursor_now')}  scale={r.get('scale')}")
    if r.get("screen") and r.get("cursor_now") and \
            max(abs(r["screen"][0] - r["cursor_now"][0]), abs(r["screen"][1] - r["cursor_now"][1])) > 3:
        print("  ⚠ the cursor is not where it was aimed — the click would land elsewhere.")

    print("\n" + "=" * 74)
    print("NOW: LIFT THE MOUSE OFF THE DESK and press the left button once.")
    print("     Lifting it means the press adds no movement — the only new input is the button.")
    print("=" * 74)
    print("\nwaiting for the betslip to appear...")

    t0 = time.time()
    opened = False
    while time.time() - t0 < 60:
        if slip_alive(base):
            opened = True
            break
        time.sleep(0.5)
    if not opened:
        print("no betslip appeared in 60s — was the press registered, and was the cursor on the price?")
        return 1
    print(f"betslip OPEN {time.time() - t0:.1f}s after aiming. Watching, hands off...\n")

    t1 = time.time()
    died = None
    while time.time() - t1 < a.watch:
        if not slip_alive(base):
            died = time.time() - t1
            break
        print(".", end="", flush=True)
        time.sleep(1.0)

    print("\n")
    if died is None:
        print(f"SLIP SURVIVED {a.watch:.0f}s — bot aimed, YOU pressed.")
        print("=> THE PRESS IS THE DIFFERENCE. Bot-controlled travel is fine; SendInput is not")
        print("   equivalent to a hardware button here. That is a hard blocker for unattended UI")
        print("   execution and worth knowing before building anything else on it.")
    else:
        print(f"SLIP DIED at t+{died:.1f}s — even though YOU pressed the button.")
        print("=> The press is NOT the difference either. Every hypothesis is now eliminated, which")
        print("   means real_click.py's original result (n=1) simply did not reproduce -- there was")
        print("   no distinction to explain. The slip is short-lived for everyone, and the placement")
        print("   path must fill it in ~1-2s rather than be made 'more human'.")
    try:
        _post(f"{base}/slip_close", timeout=20)
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
