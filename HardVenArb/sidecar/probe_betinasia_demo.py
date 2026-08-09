"""
probe_betinasia_demo.py -- does the ANONYMOUS BetInAsia feed carry prices, or only the catalog?

The 2026-08-05 recon opened TWO sockets:

    wss://black.betinasia.com/folly/cpricefeed/?token=demo-ba66fe&lang=en   <- anonymous visitor
    wss://black.betinasia.com/cpricefeed/?token=<session_id>&lang=en        <- logged in

The demo socket pushed 45 `event` (catalog) frames unprompted, but the page never sent it a
`watch_hcaps`, so we do not know whether it would answer with `offers_hcap` prices. That single
question decides a lot: if the demo feed prices, the whole M0 telemetry path (odds + catalog +
pairing) can be built and validated with NO account and NO risk to the real one.

This connects exactly the way an anonymous browser visit does, listens for the catalog, subscribes to
the first few events it names, and reports what comes back. Read-only: it never logs in, never places
anything, and disconnects after --secs.

    python probe_betinasia_demo.py                 # 45s, default demo endpoint
    python probe_betinasia_demo.py --secs 90
    python probe_betinasia_demo.py --token demo-ba66fe    # reuse an observed token
"""
from __future__ import annotations

import argparse
import asyncio
import collections
import json
import secrets
import sys
import time

import websockets

sys.stdout.reconfigure(encoding="utf-8")

DEMO_URL = "wss://black.betinasia.com/folly/cpricefeed/"


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--secs", type=float, default=45.0)
    ap.add_argument("--url", default=DEMO_URL)
    ap.add_argument("--token", default="")
    ap.add_argument("--subscribe", type=int, default=25, help="events to watch_hcaps once seen")
    args = ap.parse_args()

    # The observed token was "demo-ba66fe": the "demo-" prefix plus 6 hex chars, with no HTTP call
    # anywhere in the capture that issued it -- i.e. the page mints it client-side. We do the same.
    token = args.token or f"demo-{secrets.token_hex(3)}"
    url = f"{args.url}?token={token}&lang=en"
    print(f"[PROBE] connecting anonymously (token={token})")

    types = collections.Counter()
    events: dict[tuple[str, str], int] = {}
    priced: dict[tuple[str, str], set] = collections.defaultdict(set)
    sample_price = []
    t0 = time.time()
    subscribed = False

    try:
        async with websockets.connect(url, max_size=None) as ws:
            print("[PROBE] connected")

            async def pinger():
                while True:
                    await asyncio.sleep(3)
                    try:
                        await ws.send(json.dumps(["ping", str(int(time.time() * 1000))]))
                    except Exception:
                        return

            ping = asyncio.create_task(pinger())
            try:
                while time.time() - t0 < args.secs:
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=5.0)
                    except asyncio.TimeoutError:
                        continue
                    try:
                        frame = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    msgs = [frame] if (isinstance(frame, list) and frame and isinstance(frame[0], str)) \
                        else [m for m in frame if isinstance(m, list) and m]
                    for m in msgs:
                        types[m[0]] += 1
                        if m[0] == "event" and len(m) >= 3 and isinstance(m[1], list) and len(m[1]) >= 2:
                            sport, ekey = m[1][0], m[1][1]
                            cid = (m[2] or {}).get("competition_id", 0)
                            events[(sport, ekey)] = cid
                        elif m[0] == "offers_hcap" and len(m) >= 3 and isinstance(m[1], list) and len(m[1]) >= 3:
                            key = (m[1][1], m[1][2])
                            for mk, val in (m[2] or {}).items():
                                if isinstance(val, list) and len(val) == 2 and val[1]:
                                    priced[key].add(mk)
                                    if len(sample_price) < 3:
                                        sample_price.append((key, mk, val))

                    # Once the catalog names some events, ask for their prices.
                    if not subscribed and len(events) >= 5:
                        batch = [[cid, sp, ek] for (sp, ek), cid in list(events.items())[:args.subscribe]]
                        await ws.send(json.dumps(["watch_hcaps", batch]))
                        subscribed = True
                        print(f"[PROBE] sent watch_hcaps for {len(batch)} event(s) -- waiting for prices")
            finally:
                ping.cancel()
    except Exception as e:
        print(f"[PROBE] connection failed: {type(e).__name__}: {e}")
        return 2

    print(f"\n----- {time.time()-t0:.0f}s -----")
    print(f"frame types      : {dict(types)}")
    print(f"catalog events   : {len(events)}")
    print(f"events PRICED    : {len(priced)}")
    for (key, mk, val) in sample_price:
        print(f"  sample  {key}  {mk} = {val}")

    if priced:
        print("\nVERDICT: the ANONYMOUS feed carries prices.")
        print("  => M0 (odds + catalog + pairing) can be built and validated with no account.")
    elif events:
        print("\nVERDICT: anonymous feed serves the CATALOG only (no offers_hcap after watch_hcaps).")
        print("  => catalog()/pairing testable now; odds() still needs BIA_USERNAME/BIA_PASSWORD.")
    else:
        print("\nVERDICT: nothing received -- the demo token may be rejected or the path has changed.")
        print("  => fall back to the logged-in recon (betinasia_recon.py).")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        sys.exit(0)
