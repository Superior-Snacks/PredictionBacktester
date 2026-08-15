"""Diff the DOM events of a BOT click against a HAND click, field by field.

WHY THIS EXISTS. Ten mechanisms were eliminated one by one on 2026-08-15 — our polling, navigation, our
own slip_close, the sport walk, organic activity, window focus, native-property reads, pointer position,
aim accuracy and dwell — and the surviving observations contradict every remaining theory:

  - a SendInput press worked once, and a hand press worked once, but both bot-driven together failed
  - a FAST hand click works, while a slow bot click with an 8s dwell does not

At the DOM level a SendInput click and a physical click are built from the same Windows message, so
nothing should differ. This stops assuming that and measures it.

    python event_diff.py            # guides both captures and prints the diff

If the two are identical, the click event is not the cause and the difference is somewhere else
entirely — which is itself the most useful result available, because it retires the whole line of
inquiry that has consumed the session.
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.request

sys.stdout.reconfigure(encoding="utf-8")

KEY = ("type", "isTrusted", "screenX", "screenY", "clientX", "clientY", "movementX", "movementY",
       "pressure", "pointerType", "pointerId", "isPrimary", "width", "height", "tiltX", "tiltY",
       "twist", "button", "buttons", "detail", "which", "coalesced", "predicted", "target")


def _get(url, timeout=30):
    return json.load(urllib.request.urlopen(url, timeout=timeout))


def capture(base: str, moves: bool = False) -> list:
    d = _get(f"{base}/debug/event_capture?moves={'true' if moves else 'false'}")
    if not d.get("ok"):
        print(f"capture failed: {d.get('error')}")
        return []
    return d.get("events") or []


def summarise(rows: list) -> dict:
    """One representative event per type — the press sequence is what matters, not the volume."""
    out = {}
    for r in rows:
        t = r.get("type")
        if t and t not in out:
            out[t] = r
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8788)
    ap.add_argument("--moves", action="store_true", help="include pointermove/mousemove")
    a = ap.parse_args()
    base = f"http://127.0.0.1:{a.port}"

    try:
        from staleness import check
        check(a.port)
    except ImportError:
        pass

    _get(f"{base}/debug/event_capture")          # install
    _get(f"{base}/debug/event_capture?reset=true")
    print("capture armed.\n")
    print("=" * 74)
    print("STEP 1 — click a moneyline BY HAND (the way that works). Then press Enter here.")
    print("=" * 74)
    input()
    hand = summarise(capture(base, a.moves))
    print(f"  captured {len(hand)} event type(s) from your click: {', '.join(hand) or '(none)'}")
    if not hand:
        print("  nothing captured — did the click land on the page? Re-run.")
        return 1

    try:
        urllib.request.urlopen(urllib.request.Request(f"{base}/slip_close", data=b"", method="POST"),
                               timeout=20).read()
    except Exception:
        pass
    _get(f"{base}/debug/event_capture?reset=true")

    print("\n" + "=" * 74)
    print("STEP 2 — now let the BOT click. In another terminal run:")
    print("             python slip_hold.py --quiet-secs 3 --nth 4")
    print("         Wait for it to finish, then press Enter here.")
    print("=" * 74)
    input()
    bot = summarise(capture(base, a.moves))
    print(f"  captured {len(bot)} event type(s) from the bot click: {', '.join(bot) or '(none)'}\n")
    if not bot:
        print("  nothing captured from the bot — did it click the same page?")
        return 1

    print("=" * 74)
    print("DIFF  (hand | bot)   — only fields that DISAGREE")
    print("=" * 74)
    any_diff = False
    for t in sorted(set(hand) | set(bot)):
        h, b = hand.get(t), bot.get(t)
        if h is None or b is None:
            print(f"\n{t}: present only in {'HAND' if b is None else 'BOT'} capture")
            any_diff = True
            continue
        lines = []
        for f in KEY:
            hv, bv = h.get(f), b.get(f)
            # clientX/Y and target legitimately differ (different rows) -- flag but do not alarm.
            if hv != bv:
                note = "  (expected: different element)" if f in ("clientX", "clientY", "pageX",
                                                                  "pageY", "screenX", "screenY",
                                                                  "target") else ""
                lines.append(f"    {f:20} {str(hv):>22} | {str(bv):<22}{note}")
        if lines:
            any_diff = True
            print(f"\n{t}:")
            print("\n".join(lines))
    if not any_diff:
        print("\nIDENTICAL on every captured field.")
        print("=> The click event is NOT the difference. Whatever dismisses a bot-opened betslip is not")
        print("   reading the event, and every theory about pressure/screen coords/trust is dead.")
        print("   Look instead at what happens AFTER the click: the sequence of app state, the betslip")
        print("   subscription, or something the bot session does that a hand session does not.")
    else:
        print("\n=> Fields above are where a bot click and a hand click actually differ. Ones marked")
        print("   'expected' are just different screen positions; anything else is the real signal.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
