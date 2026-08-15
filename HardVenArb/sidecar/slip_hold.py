"""Open a betslip, then do NOTHING — and watch whether it survives.

The simplest possible isolation. `_place_via_ui` does a dozen things after the slip opens, so a slip that
vanishes mid-drive could be caused by any of them. This does exactly one thing (click the moneyline via
/slip_quote) and then only LOOKS, once a second, via /debug/slip_dom — which is read-only and runs in
Playwright's isolated world.

If the slip dies here, nothing in the placement path is responsible: something else in the sidecar is
navigating or closing the page. Prime suspects, both on timers that ignore the slip:
  - the SPORT WALK (`BIA_SPORT_WALK_DELAY` 70s after startup, then `BIA_SPORT_DWELL_SEC` 25s per sport)
    which NAVIGATES the sport tab, the same tab the slip opens on
  - organic activity (keyboard scroll, sport-nav clicks, Escape to dismiss popups)

    python slip_hold.py --sport tennis            # pick a target automatically
    python slip_hold.py --selection-id "tennis:339:..."
    python slip_hold.py --seconds 180             # watch longer

Always closes the slip on the way out: a slip left open holds its acca subscription, and the event
becomes unquotable until the socket cycles.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.parse
import urllib.request

sys.stdout.reconfigure(encoding="utf-8")


def _get(url: str, timeout: int = 30):
    return json.load(urllib.request.urlopen(url, timeout=timeout))


def _post(url: str, timeout: int = 60):
    return json.load(urllib.request.urlopen(
        urllib.request.Request(url, data=b"", method="POST"), timeout=timeout))


def slip_present(base: str) -> tuple[bool, str, str]:
    """Is a betslip on screen, and what is the page? Returns (present, detail, fingerprint).

    The FINGERPRINT (tab count + urls) is the discriminator that matters. A slip can vanish two very
    different ways and they need completely different fixes:
      - the URL changed  => something NAVIGATED the tab out from under it (sport walk, board reset)
      - the URL is identical => the SLIP ITSELF was closed in place (Escape, a re-click, or the venue)
    Without this the two are indistinguishable, which is how the sport walk got blamed for something
    that happened before the sport walk had even started.
    """
    try:
        d = _get(f"{base}/debug/slip_dom", timeout=20)
    except Exception as e:
        return False, f"slip_dom failed: {type(e).__name__}", "?"
    pages = d.get("pages") or []
    fp = f"{len(pages)} tab(s): " + " | ".join((p.get("url") or "?")[:70] for p in pages)
    for pg in pages:
        inputs = pg.get("inputs") or ""
        if "price-input" in inputs:
            val = ""
            for line in inputs.splitlines():
                if "price-input" in line:
                    val = line.strip()[:96]
            return True, val, fp
    return False, "no price-input on any tab", fp


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8788)
    ap.add_argument("--sport", default="tennis")
    ap.add_argument("--selection-id", default=None)
    ap.add_argument("--seconds", type=float, default=120.0)
    ap.add_argument("--quiet-secs", type=float, default=0.0,
                    help="LOOK AT NOTHING for this long after the quote, then check once. Distinguishes "
                         "'the venue closed it' from 'our own polling closed it' — /debug/slip_dom does "
                         "hundreds of locator reads per call, and the slip died after 1-3 polls both "
                         "times, which is exactly what an observer effect looks like.")
    a = ap.parse_args()
    base = f"http://127.0.0.1:{a.port}"

    try:
        from staleness import check
        check(a.port)
    except ImportError:
        pass

    sid = a.selection_id
    if not sid:
        import datetime as dt
        cat = _get(f"{base}/catalog")["selections"]
        now = dt.datetime.now(dt.timezone.utc)
        best = None
        for e in cat:
            if e.get("three_way") or (a.sport and e.get("sport") != a.sport):
                continue
            s = e.get("start_time")
            if not s:
                continue
            try:
                t = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
            except Exception:
                continue
            mins = (t - now).total_seconds() / 60
            if 40 < mins < 480 and (best is None or mins < best[0]):
                best = (mins, e)
        if not best:
            print(f"no pre-live {a.sport} candidate found")
            return 1
        sid = best[1]["selection_id"]
        print(f"target: {best[1]['event']} -- {best[1]['selection_name']} (starts in {best[0]:.0f}m)")
    print(f"        {sid}\n")

    print("opening the betslip (one click, then nothing)...")
    t0 = time.time()
    try:
        q = _post(f"{base}/slip_quote?selection_id=" + urllib.parse.quote(sid, safe=""), timeout=60)
    except Exception as e:
        print(f"slip_quote failed: {type(e).__name__}: {e}")
        return 1
    if not q.get("ok"):
        print(f"slip_quote refused: {q.get('error')}")
        return 1
    print(f"quoted {q.get('decimal_odds')} in {time.time() - t0:.1f}s "
          f"(clicked={q.get('clicked')})\n")

    if a.quiet_secs > 0:
        print(f"NOT LOOKING for {a.quiet_secs:.0f}s (no slip_dom calls at all)...")
        time.sleep(a.quiet_secs)
        here, detail, fp = slip_present(base)
        el = time.time() - t0
        print(f"  first look at t+{el:.1f}s: {'SLIP ALIVE' if here else 'SLIP GONE'}  {detail}")
        print(f"  page: {fp}\n")
        if here:
            print("=> THE SLIP SURVIVED WHILE UNOBSERVED. Our own /debug/slip_dom polling was closing it")
            print("   -- hundreds of locator reads per call against an open slip. The venue is innocent")
            print("   and so is the placement path; the MEASUREMENT was the bug.")
        else:
            print("=> gone even with nobody looking. Not an observer effect: the venue (or something on")
            print("   a timer) dismisses an idle slip. Next question is how long a HUMAN-opened slip")
            print("   survives untouched -- if it also dies, the slip is simply short-lived and the")
            print("   placement path must fill it fast rather than narrate through it.")
        try:
            _post(f"{base}/slip_close", timeout=20)
        except Exception:
            pass
        return 0

    print("watching. '.' = slip still there\n")
    died_at = None
    fp_at_death = fp_before = None
    deadline = time.time() + a.seconds
    last = True
    _, _, fp_before = slip_present(base)
    print(f"  page now: {fp_before}\n")
    try:
        while time.time() < deadline:
            here, detail, fp = slip_present(base)
            el = time.time() - t0
            if here != last or (died_at is None and not here):
                print(f"\n  t+{el:5.1f}s  {'SLIP BACK' if here else 'SLIP GONE'}  {detail}")
                print(f"           page: {fp}")
                if not here and died_at is None:
                    died_at, fp_at_death = el, fp
            else:
                print(".", end="", flush=True)
            last = here
            time.sleep(1.0)
    except KeyboardInterrupt:
        print("\n(interrupted)")

    print()
    if died_at is None:
        print(f"SLIP SURVIVED the full {a.seconds:.0f}s untouched.")
        print("=> nothing in the sidecar closes an idle slip, so the placement path itself is where it")
        print("   dies -- look at what _place_via_ui does between the quote and the first fill.")
    else:
        print(f"SLIP DIED at t+{died_at:.1f}s with NOBODY TOUCHING IT.")
        if fp_at_death and fp_before and fp_at_death != fp_before:
            print("=> THE PAGE CHANGED. Something NAVIGATED the tab; the slip was collateral.")
            print(f"   before: {fp_before}")
            print(f"   after : {fp_at_death}")
        else:
            print("=> THE PAGE IS UNCHANGED — same tab, same URL. Nothing navigated: the SLIP ITSELF")
            print("   was closed. That is either an Escape/click from our side, or the venue dismissing")
            print("   it. Check the sidecar console at that timestamp for a `closed ... with Escape`")
            print("   line: if it is absent, the venue closed it and no amount of our own restraint")
            print("   will keep it open.")

    try:
        _post(f"{base}/slip_close", timeout=20)
        print("\nslip closed (releases the acca subscription so the event stays quotable)")
    except Exception as e:
        print(f"\ncould not close the slip: {type(e).__name__} -- it may stay unquotable until restart")
    return 0


if __name__ == "__main__":
    sys.exit(main())
