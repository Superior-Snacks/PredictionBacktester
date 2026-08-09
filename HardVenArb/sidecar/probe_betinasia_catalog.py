"""
probe_betinasia_catalog.py -- how complete is the WS catalog, and how many events can ONE socket price?

Two questions this answers, neither of which the other tools can:

  1. COMPLETENESS. The feed pushes its whole catalog unprompted on connect. Tennis was verified that
     way (87 events, all before the first subscribe, 0 missing vs the REST list, 74 of 88 matches in
     Challenger/ITF tiers that no featured board would carry). Soccer is ~10x bigger and reaches
     much further into the future, so it deserves its own measurement rather than an assumption.

  2. SUBSCRIPTION CEILING. Catalog arrives free; PRICES do not -- each event needs a `watch_hcaps`.
     That is a WS message, not a tab, so one connection is still enough in principle. Whether one
     connection will actually price N events is unproven: the `api/info` frame reports
     `registered_events` (which stayed 0 through 34 real browser subscribes, so it is NOT counting
     this) and `max_queue_size: 50000` (a queue depth, not a subscription cap).

DEFAULT IS OBSERVE-ONLY, and that is not a formality -- it is the whole point.

Connecting and listening is EXACTLY what a browser does: the catalog push is unprompted, so phases 1
and 2 add no behaviour to the account at all. Subscribing is different. Measured off three real
sessions, the browser's envelope is: batch sizes min 1 / MEDIAN 3 / max 32, a ~77-event page-load
burst, then ~2-3 events/sec. A real session does subscribe hundreds of events, so volume alone is not
the tell -- SHAPE is. `--subscribe` therefore paces through BetInAsiaFeed._send_watch (which enforces
the envelope at the transport, so nothing here can bypass it) and is capped at --max-subscribe.

    python probe_betinasia_catalog.py --sport tennis              # observe only
    python probe_betinasia_catalog.py --sport fb                  # observe only, soccer
    python probe_betinasia_catalog.py --sport fb --subscribe 60   # opt in, browser-paced

Needs BIA_USERNAME / BIA_PASSWORD in the environment (the WS token IS the login session_id).
Never opens a betslip and never places anything, in any mode.
"""
from __future__ import annotations

import argparse
import asyncio
import collections
import sys
import time

sys.stdout.reconfigure(encoding="utf-8")

from betinasia_ws import BetInAsiaFeed
from betinasia_adapter import MONEYLINE_BY_SPORT, is_moneyline


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sport", default="tennis", help="tennis | fb (soccer) | baseball | basket ...")
    ap.add_argument("--subscribe", type=int, default=0,
                    help="OPT-IN: subscribe this many events to measure pricing. 0 = observe only "
                         "(the default). Paced to the browser envelope by the feed itself.")
    ap.add_argument("--max-subscribe", type=int, default=120,
                    help="hard ceiling on --subscribe; a browsing session reaches a few hundred over "
                         "minutes, so keep single probes well under that")
    ap.add_argument("--settle", type=float, default=12.0,
                    help="seconds of catalog quiet before declaring the push complete")
    ap.add_argument("--price-wait", type=float, default=45.0,
                    help="seconds to wait for prices after subscribing")
    args = ap.parse_args()

    feed = BetInAsiaFeed()
    try:
        await feed.login()
    except Exception as e:
        print(f"[PROBE] login failed: {type(e).__name__}: {e}")
        return 2
    await feed.start()

    # ── phase 1: let the unprompted catalog push finish ───────────────────────
    print(f"[PROBE] waiting for the catalog push to go quiet ({args.settle:.0f}s of no new events)...")
    last_count, quiet_since, t0 = -1, time.time(), time.time()
    while True:
        await asyncio.sleep(1.0)
        n = len(feed.all_events())
        if n != last_count:
            last_count, quiet_since = n, time.time()
            print(f"\r[PROBE] catalog: {n} events...", end="", flush=True)
        if time.time() - quiet_since >= args.settle:
            break
        if time.time() - t0 > 180:
            print("\n[PROBE] catalog never went quiet after 180s -- reporting what arrived")
            break
    print()

    events = feed.all_events()
    by_sport = collections.Counter(sport for (sport, _k) in events)
    print(f"\n[PROBE] catalog settled: {len(events)} events across {len(by_sport)} sports "
          f"in {time.time()-t0:.0f}s")
    for sp, n in by_sport.most_common(14):
        mark = "  <-- target" if sp == args.sport else ""
        print(f"    {n:5d}  {sp}{mark}")

    mine = {(s, k): v for (s, k), v in events.items() if s == args.sport}
    print(f"\n[PROBE] {args.sport}: {len(mine)} events in the WS catalog")
    if not mine:
        print("[PROBE] nothing for that sport -- check the sport code (soccer is 'fb')")
        await feed.stop()
        return 1

    # ── phase 2: is the WS catalog a superset of what REST lists? ─────────────
    # limit=25 is what the site's own page requests; a bigger number is a shape the real client never
    # produces, and the diff works fine on a sample.
    rest = await feed.list_events(args.sport, limit=25)
    rest_ids = {e.get("id") for e in rest if e.get("id")}
    ws_ids = {k for (_s, k) in mine}
    missing = rest_ids - ws_ids
    print(f"[PROBE] REST lists {len(rest_ids)}; missing from the WS catalog: {len(missing)}")
    if missing:
        print(f"        e.g. {list(missing)[:5]}")
        print("        => the WS push is NOT complete for this sport; drive subscriptions off REST")
    elif rest_ids:
        print("        => WS catalog is a SUPERSET of the REST list (one connection sees everything)")

    # league + horizon spread: a featured board would be a handful of leagues, all imminent
    leagues = collections.Counter(v.get("competition_name") or "?" for v in mine.values())
    starts = sorted(str(v.get("start_ts") or "") for v in mine.values() if v.get("start_ts"))
    print(f"[PROBE] {len(leagues)} distinct leagues; top: "
          f"{[f'{n}x {nm[:28]}' for nm, n in leagues.most_common(4)]}")
    if starts:
        print(f"[PROBE] start times span {starts[0][:16]} -> {starts[-1][:16]}")

    # ── phase 3: OPT-IN only -- subscribe a browser-sized sample and count pricing ──
    if args.subscribe <= 0:
        print("\n[PROBE] observe-only (default). Everything above came from listening to a push the "
              "site makes to any logged-in client -- no behaviour was added to the account.")
        print("        To measure pricing, re-run with e.g. --subscribe 60 (browser-paced).")
        await feed.stop()
        return 0

    n_sub = min(args.subscribe, args.max_subscribe)
    if n_sub < args.subscribe:
        print(f"\n[PROBE] capping --subscribe {args.subscribe} -> {n_sub} (--max-subscribe)")

    # Prefer events that start SOON: a far-future game having no book is not a subscription ceiling,
    # and mixing the two makes the result unreadable.
    soon = sorted(((v.get("start_ts") or "9999", k, v) for (s, k), v in mine.items()
                   if v.get("competition_id") is not None))
    targets = [(v.get("competition_id"), args.sport, k) for _st, k, v in soon][:n_sub]
    ml_key = MONEYLINE_BY_SPORT.get(args.sport)
    print(f"\n[PROBE] subscribing {len(targets)} soonest {args.sport} event(s) on ONE socket "
          f"(moneyline key: {ml_key})")
    print(f"        paced by the feed: batches of {__import__('betinasia_ws').SUB_BATCH}, "
          f"{__import__('betinasia_ws').SUB_PACE_SEC}s apart -- inside the measured browser envelope")
    t1 = time.time()
    await feed.watch(targets)

    deadline = time.time() + args.price_wait
    while time.time() < deadline:
        await asyncio.sleep(1.0)
        priced = sum(1 for (s, k) in [(t[1], t[2]) for t in targets]
                     if (feed._books.get((s, k)) or {}).get("markets"))
        print(f"\r[PROBE] priced {priced}/{len(targets)} after {time.time()-t1:4.0f}s   ",
              end="", flush=True)
        if priced >= len(targets):
            break
    print()

    priced_books = {(t[1], t[2]): (feed._books.get((t[1], t[2])) or {}) for t in targets}
    priced = [k for k, b in priced_books.items() if b.get("markets")]
    with_ml = [k for k in priced if ml_key and ml_key in priced_books[k].get("markets", {})]
    print(f"\n[PROBE] RESULT after {time.time()-t1:.0f}s")
    print(f"    subscribed        : {len(targets)}")
    print(f"    priced (any mkt)  : {len(priced)}  ({100*len(priced)//max(len(targets),1)}%)")
    print(f"    with a MONEYLINE  : {len(with_ml)}  ({100*len(with_ml)//max(len(targets),1)}%)")
    print(f"    feed stats        : {feed.stats()}")
    if len(priced) < len(targets):
        print("    NOTE: unpriced events are normal for far-future games -- check the start-time "
              "spread above before reading this as a subscription ceiling.")

    await feed.stop()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        sys.exit(0)
