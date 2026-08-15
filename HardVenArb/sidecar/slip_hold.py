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
    ap.add_argument("--watch-only", action="store_true",
                    help="THE MISSING CONTROL: the bot clicks nothing. YOU click a moneyline by hand and "
                         "then touch nothing; this times how long it lives. Never run — every 'a hand "
                         "click survives' data point so far involved either SendInput or you typing in "
                         "the slip afterwards. If an untouched hand-clicked slip also dies in ~1-3s, "
                         "there is no bot/human difference and there never was.")
    ap.add_argument("--nth", type=int, default=0,
                    help="pick the Nth candidate instead of the soonest. A slip that dies on its own "
                         "leaves its acca subscription held (slip_close only unwatches when a slip is "
                         "actually open), so the event it burned is unquotable until the socket cycles — "
                         "use this to step past it rather than restarting.")
    ap.add_argument("--seconds", type=float, default=120.0)
    ap.add_argument("--delay", type=float, default=3.0,
                    help="seconds to wait before touching anything, so you can click into the browser "
                         "window and take your hand off the mouse. Set 0 to start immediately.")
    ap.add_argument("--park-mouse", action="store_true",
                    help="after the CDP click, move the PHYSICAL cursor onto the slip (a move, not a "
                         "click). Tests whether CSS :hover on the real pointer is what keeps it alive.")
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
    # State the click mode up front. A cdp_raw A/B against a sidecar that never received the env var is
    # indistinguishable from cdp_raw failing, and that mistake has already cost one round trip.
    try:
        sw = (_get(f"{base}/health", timeout=5) or {}).get("switches") or {}
        print(f"[cfg] click_mode={sw.get('click_mode')}  organic={sw.get('organic')}  "
              f"sport_walk_delay={sw.get('sport_walk_delay')}")
    except Exception:
        pass

    if a.watch_only:
        # No catalog lookup, no click, no aim — the bot's only job is to hold a stopwatch.
        if slip_alive_now := slip_present(base)[0]:
            print("A betslip is already open — close it first so the timing starts from your click.")
            return 1
        print("=" * 74)
        print("CLICK A MONEYLINE BY HAND now, then TAKE YOUR HAND OFF THE MOUSE and touch nothing.")
        print("The bot will click nothing. It only watches.")
        print("=" * 74)
        t_wait = time.time()
        while time.time() - t_wait < 120:
            if slip_present(base)[0]:
                break
            time.sleep(0.4)
        else:
            print("no betslip appeared in 120s")
            return 1
        t_open = time.time()
        print(f"\nslip detected. watching, hands off...\n")
        died = None
        while time.time() - t_open < a.seconds:
            if not slip_present(base)[0]:
                died = time.time() - t_open
                break
            print(".", end="", flush=True)
            time.sleep(1.0)
        print("\n")
        if died is None:
            print(f"HAND-CLICKED SLIP SURVIVED {a.seconds:.0f}s UNTOUCHED.")
            print("=> There IS a real difference between a hand click and every bot click, and it")
            print("   survives all the eliminations. Worth chasing further.")
        else:
            print(f"HAND-CLICKED SLIP DIED at t+{died:.1f}s — untouched, clicked by you.")
            print("=> THERE IS NO BOT/HUMAN DIFFERENCE. The slip is simply short-lived for everyone,")
            print("   and both 'it survived' results (real_click.py, hand_click.py — each n=1) were")
            print("   noise. Nothing needs explaining; the placement path just has to interact with")
            print("   the slip immediately rather than admire it.")
        try:
            _post(f"{base}/slip_close", timeout=20)
        except Exception:
            pass
        return 0

    sid = a.selection_id
    if not sid:
        import datetime as dt
        cat = _get(f"{base}/catalog")["selections"]
        now = dt.datetime.now(dt.timezone.utc)
        cands = []
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
            if 40 < mins < 480:
                cands.append((mins, e["event"], e["selection_name"], e["selection_id"]))
        cands.sort(key=lambda r: (r[0], r[3]))
        if not cands:
            print(f"no pre-live {a.sport} candidate found")
            return 1
        if a.nth >= len(cands):
            print(f"--nth {a.nth} but only {len(cands)} candidates")
            return 1
        mins, ev, seln, sid = cands[a.nth]
        print(f"target[{a.nth}/{len(cands) - 1}]: {ev} -- {seln} (starts in {mins:.0f}m)")
    print(f"        {sid}\n")

    # HANDS OFF BEFORE THE BOT MOVES. With BIA_CLICK_MODE=os_hybrid the bot drives the PHYSICAL cursor,
    # so anything you do with the mouse in that window fights it — and the browser wants foreground focus
    # for the click to land on the element rather than merely activating the window.
    if a.delay > 0:
        print(f"\nClick into the BROWSER window now, then let go of the mouse. Starting in "
              f"{a.delay:.0f}s...")
        for r in range(int(a.delay), 0, -1):
            print(f"  {r}...   ", end="\r", flush=True)
            time.sleep(1)
        print("  go.      ")

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
          f"(clicked={q.get('clicked')})")

    if a.park_mouse:
        # Move the REAL cursor onto the panel. CSS :hover follows the physical pointer with no events
        # at all, which is the leading explanation left after the input probe cleared the venue of
        # inspecting clicks.
        try:
            r = _post(f"{base}/debug/park_mouse", timeout=30)
            print(f"parked physical cursor: {json.dumps(r)}")
        except Exception as e:
            print(f"park failed: {type(e).__name__}: {e}")
    print()

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
