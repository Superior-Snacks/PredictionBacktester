"""
betinasia_observer.py -- PASSIVE transport: open the page, watch its network, emit nothing.

THE RULE THIS IMPLEMENTS (operator, 2026-08-09): "the bot should not manually do anything, just open
the page and watch what happens in the network and on screen."

So this owns a real browser on the real profile and reads the frames THE PAGE receives. It never
opens its own WebSocket, never logs in over httpx, never sends a `watch_hcaps`, never clicks. The
account emits exactly what a person sitting in front of the site emits, because that is literally
what is happening -- we are only reading over its shoulder.

WHY NOT THE DIRECT CLIENT. `BetInAsiaFeed` can open its own socket with the session token, and it is
read-only, but it is still a second client: different TLS fingerprint, no browser origin, no matching
page traffic around it. Cheap to build, and exactly the sort of thing a broker watching for
arbitrage bots would notice. The parser it contains is transport-independent, so we keep 100% of it
(protocol decode, market taxonomy, catalog builder, selection ids, all of the tests) and swap only
how frames arrive.

WHAT PASSIVE COSTS YOU -- measured, not guessed:
    catalog                     ALL of it (88 tennis), pushed unprompted on connect   -> free
    prices, page load only      ~12 of 88 tennis events                               -> 14%
    prices, after the sport tab is opened   100 -> 206 tennis over a session
The catalog is free because the server volunteers it. Prices are not: the page only subscribes to
what it is showing. A bot that never navigates therefore sees the full fixture list and prices for a
fraction of it. Navigating IS ordinary user behaviour -- it is the single most common action on the
site -- but it is a decision for the operator, not something this module does on its own.

    python betinasia_observer.py --secs 120        # watch, then report coverage
    python betinasia_observer.py --url https://black.betinasia.com/sportsbook
"""
from __future__ import annotations

import argparse
import asyncio
import collections
import json
import os
import sys
import time
from pathlib import Path
from typing import Callable, Optional

sys.stdout.reconfigure(encoding="utf-8")

from betinasia_ws import BetInAsiaFeed

PROFILE = Path(__file__).parent / ".betinasia_profile"
PRICE_FEED_HINT = "cpricefeed"          # both /cpricefeed/ and /folly/cpricefeed/ carry the protocol


class BetInAsiaObserver:
    """Owns a browser page and pumps its WS frames into a passive BetInAsiaFeed (the parser)."""

    def __init__(self, url: str = "https://black.betinasia.com",
                 on_log: Optional[Callable[[str], None]] = None) -> None:
        self.url = url
        self._log = on_log or (lambda m: print(f"[BIA-OBS] {m}", flush=True))
        # passive=True makes the feed refuse to open a socket or send a frame even if something
        # downstream asks it to -- the guard is in the object, not in this file's good intentions.
        self.feed = BetInAsiaFeed(on_log=self._log, passive=True)
        self._pw = None
        self._ctx = None
        self._page = None
        self._sockets = 0
        self._frames = 0
        self._started = 0.0

    # ── lifecycle ─────────────────────────────────────────────────────────────
    async def start(self) -> None:
        from playwright.async_api import async_playwright

        size = os.environ.get("BIA_WINDOW_SIZE", "1440,900")
        pos = os.environ.get("BIA_WINDOW_POS", "")
        args = [f"--window-size={size}"] + ([f"--window-position={pos}"] if pos else [])
        headless = os.environ.get("BIA_HEADLESS", "0") == "1"

        self._pw = await async_playwright().start()
        self._ctx = await self._pw.chromium.launch_persistent_context(
            str(PROFILE), headless=headless, args=args, viewport=None)
        self._ctx.on("page", self._hook_page)
        for pg in self._ctx.pages:
            self._hook_page(pg)
        self._page = self._ctx.pages[0] if self._ctx.pages else await self._ctx.new_page()
        self._started = time.time()
        try:
            await self._page.goto(self.url, wait_until="domcontentloaded", timeout=60_000)
        except Exception as e:
            self._log(f"initial navigation: {type(e).__name__}: {e}")
        self._log("observing - the page drives, we only read")

    async def stop(self) -> None:
        for closer in (getattr(self._ctx, "close", None), getattr(self._pw, "stop", None)):
            if closer:
                try:
                    await closer()
                except Exception:
                    pass
        self._ctx = self._pw = self._page = None

    # ── frame plumbing ────────────────────────────────────────────────────────
    def _hook_page(self, page) -> None:
        page.on("websocket", self._hook_ws)

    def _hook_ws(self, ws) -> None:
        if PRICE_FEED_HINT not in (ws.url or ""):
            return                                   # analytics/chat sockets are not our protocol
        self._sockets += 1
        self._log(f"price feed socket seen ({self._sockets})")
        ws.on("framereceived", self._on_frame)
        # framesent is deliberately NOT hooked for parsing -- we never act on what the page asks for,
        # we only parse what the server answers. It is watched purely to report coverage below.
        ws.on("framesent", self._on_sent)

    def _on_frame(self, payload) -> None:
        if not isinstance(payload, str):
            return
        try:
            frame = json.loads(payload)
        except (json.JSONDecodeError, TypeError):
            return
        self._frames += 1
        self.feed.handle_frame(frame)

    def _on_sent(self, payload) -> None:
        """Record what the PAGE subscribed to, so coverage can be reported honestly. We never send."""
        if not isinstance(payload, str):
            return
        try:
            msg = json.loads(payload)
        except (json.JSONDecodeError, TypeError):
            return
        if not (isinstance(msg, list) and msg and msg[0] in ("watch_hcaps", "watch_event")):
            return
        ents = msg[1] if isinstance(msg[1], list) else []
        if ents and not isinstance(ents[0], list):
            ents = [ents]
        for e in ents:
            if isinstance(e, list) and len(e) >= 3:
                self.feed._subs[(e[1], e[2])] = e[0]

    # ── reporting ─────────────────────────────────────────────────────────────
    def coverage(self, sport: Optional[str] = None) -> dict:
        """What the page has given us: catalog (free) vs priced (only what it subscribed to).

        MATCHES ONLY. Outrights (`...,multirunner,...`) are excluded from both sides of the ratio:
        they price through `watch_event`/`offers_event`, the sport page never subscribes them unless
        you open the outrights tab, and `catalog()` skips them anyway. Counting them made a run that
        had subscribed literally every match report 94% and look like it had a gap.
        """
        def is_match(k: str) -> bool:
            return "multirunner" not in k

        events = self.feed.all_events()
        cat = collections.Counter(s for (s, k) in events if is_match(k))
        outr = collections.Counter(s for (s, k) in events if not is_match(k))
        priced = collections.Counter(
            s for (s, k), b in self.feed._books.items() if is_match(k) and (b or {}).get("markets"))
        out = {"sockets": self._sockets, "frames": self._frames,
               "catalog_matches": sum(cat.values()), "catalog_outrights": sum(outr.values()),
               "priced_total": sum(priced.values()), "page_subscribed": len(self.feed._subs)}
        if sport:
            out["sport"] = sport
            out["catalog"] = cat.get(sport, 0)
            out["outrights"] = outr.get(sport, 0)
            out["priced"] = priced.get(sport, 0)
        else:
            out["by_sport"] = {s: {"matches": cat[s], "outrights": outr.get(s, 0),
                                   "priced": priced.get(s, 0)}
                               for s, _ in cat.most_common(12)}
        return out


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="https://black.betinasia.com")
    ap.add_argument("--secs", type=float, default=120.0)
    ap.add_argument("--sport", default="tennis")
    args = ap.parse_args()

    obs = BetInAsiaObserver(url=args.url)
    await obs.start()
    print("[BIA-OBS] Leave it alone to measure PURE passive coverage, or browse normally to see what "
          "ordinary navigation adds. Ctrl+C to stop early.\n")
    try:
        deadline = time.time() + args.secs
        while time.time() < deadline:
            await asyncio.sleep(5)
            c = obs.coverage(args.sport)
            print(f"\r[BIA-OBS] frames={c['frames']:6d}  catalog={c['catalog_matches']:5d} matches  "
                  f"page-subscribed={c['page_subscribed']:4d}  "
                  f"{args.sport}: {c['priced']}/{c['catalog']} priced   ", end="", flush=True)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    print("\n")
    full = obs.coverage()
    print(json.dumps(full, indent=2))
    c = obs.coverage(args.sport)
    if c["catalog"]:
        pct = 100 * c["priced"] // c["catalog"]
        print(f"\n{args.sport}: {c['priced']}/{c['catalog']} MATCHES priced ({pct}%)"
              f"  [+{c['outrights']} outrights, not subscribed by the sport page and not paired]")
        if pct >= 100:
            print("  => full coverage from one page load. Nothing was clicked; the page did this itself.")
        else:
            print("  => the gap is matches the page never subscribed to. Give it longer (the app "
                  "subscribes over several seconds) before treating it as a real ceiling.")
    await obs.stop()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        sys.exit(0)
