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
        self._last_update: dict[tuple, float] = {}     # (sport, ekey) -> last offers_hcap time
        self._sub_order: dict[tuple, int] = {}         # (sport, ekey) -> order the PAGE subscribed it
        self._sub_time: dict[tuple, float] = {}
        self._server_says: list[tuple] = []            # verbatim error/api frames (limit announcements)
        self._anon_socket = False                      # saw the logged-OUT demo feed
        self._socket_urls: list[str] = []               # token-redacted, for diagnosis
        self._quiet_ref = float(os.environ.get("BIA_QUIET_SEC", "600"))
        self._resumed = 0                              # updates that arrived after > _quiet_ref silence
        self._resumed_keys: set = set()

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
        asyncio.create_task(self._first_look())

    # ── multi-sport coverage ──────────────────────────────────────────────────
    async def visit_sports(self, targets: list, dwell: float = 25.0) -> dict:
        """Walk a list of (sport_code, url) sportsbook pages ONCE, letting each one subscribe its board.

        WHY ONE TAB AND NOT ONE TAB PER SPORT. Measured 2026-08-09/10: subscriptions ACCUMULATE on the
        socket and are never dropped -- tennis went 6 -> 83 on opening the tennis page and was STILL 83
        after navigating away to football, and a 90-min run showed 426 distinct events go quiet >10 min
        then resume (a dropped subscription cannot resume). So a single tab that visits each sport in turn
        ends up holding every sport's book simultaneously, and N pinned tabs would buy nothing.
        It is also the safer shape: a real user browses sports one after another in one tab. Nine tabs
        parked on nine sportsbook sections is not a thing a person does, and anti-detection is a hard
        constraint here.

        Navigation only -- no clicking, no scrolling, no `watch_hcaps` from our side. The page decides
        what to subscribe; we just give it the chance to."""
        seen: dict[str, int] = {}
        for code, url in targets:
            if self._page is None:
                break
            try:
                await self._page.goto(url, wait_until="domcontentloaded", timeout=60_000)
            except Exception as e:
                self._log(f"visit {code}: navigation failed ({type(e).__name__}: {e}) - skipping")
                seen[code] = -1
                continue
            # Dwell so the board renders and its subscribe batches go out. The transport paces them.
            await asyncio.sleep(max(dwell, 1.0))
            cov = self.coverage(code)
            priced, cat = int(cov.get("priced") or 0), int(cov.get("catalog") or 0)
            seen[code] = priced
            pct = f"{100.0 * priced / cat:.0f}%" if cat else "n/a"
            self._log(f"visit {code}: {priced} priced / {cat} catalog matches ({pct})")
        self._log(f"sport walk done: {', '.join(f'{k}={v}' for k, v in seen.items())} "
                  f"| priced across all sports={self.coverage().get('priced_total')}")
        return seen

    async def _first_look(self, after: float = 60.0) -> None:
        """One loud verdict once the page has had time to settle.

        A logged-in tennis page reaches ~100% priced within a minute; a logged-out or
        never-subscribed one sits at a full catalog and zero prices FOREVER while every other signal
        (socket connected, frames arriving, catalog populated, /health ok) reads healthy. This is the
        line that tells those two apart without anyone having to go looking."""
        await asyncio.sleep(after)
        st = self.feed.stats()
        urls = ", ".join(self._socket_urls) or "(none)"
        page_url = ""
        try:
            page_url = self._page.url if self._page else ""
        except Exception:
            pass
        self._log(f"after {after:.0f}s: sockets={self._sockets} [{urls}] frames={self._frames} "
                  f"catalog={st['events']} priced={st['priced']} page={page_url[:70]}")
        if st["events"] and not st["priced"]:
            self._log("*** NO PRICES. The catalog arrives on any connection, so this is NOT a "
                      "connectivity problem. Either the page is not logged in, or it is not showing "
                      "a sport list (prices only exist for what the page subscribes). odds() will "
                      "return {} for every selection until this is fixed. ***")

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
        url = ws.url or ""
        if PRICE_FEED_HINT not in url:
            return                                   # analytics/chat sockets are not our protocol
        self._sockets += 1
        # WHICH socket matters. The site opens TWO that both match "cpricefeed":
        #   /cpricefeed/?token=<session_id>    logged in  -> catalog AND prices
        #   /folly/cpricefeed/?token=demo-...  anonymous  -> catalog ONLY
        # On the demo socket everything looks healthy -- a full catalog arrives, `catalog()` returns
        # hundreds of selections, /health says ok -- and `odds()` silently returns {} forever because
        # nothing was ever subscribed. Name the socket so that state is visible at a glance.
        demo = "/folly/" in url or "token=demo" in url
        self._anon_socket = self._anon_socket or demo
        kind = "ANONYMOUS/demo (catalog only, NO PRICES)" if demo else "authed"
        # Keep the URL with the token REDACTED: the token is a bearer credential for the whole
        # account, but the path and token PREFIX are what identify which feed this is.
        import re as _re
        self._socket_urls.append(_re.sub(r"token=([^&]{0,6})[^&]*", r"token=\1...", url))
        self._log(f"price feed socket #{self._sockets}: {kind}")
        if demo:
            self._log("WARNING the page is NOT logged in. It will serve a full catalog and zero "
                      "prices, which looks identical to a healthy idle bot. Log in in the window.")
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
        # Per-event last-update stamps + verbatim capture of anything the server says about limits.
        # Needed to tell a DROPPED subscription from a merely QUIET one (see drop_report).
        now = time.time()
        for m in (frame if isinstance(frame, list) and frame
                  and not isinstance(frame[0], str) else [frame]):
            if not (isinstance(m, list) and m):
                continue
            if m[0] == "offers_hcap" and len(m) >= 3 and isinstance(m[1], list) and len(m[1]) >= 3:
                k = (m[1][1], m[1][2])
                prev = self._last_update.get(k)
                # RESUMED-AFTER-QUIET is the real eviction test. A dropped subscription cannot start
                # updating again without a resubscribe, so a single resume disproves eviction for
                # that event. "alive" (updated recently) does NOT do this job: a pre-live soccer book
                # barely ticks, so silence is its normal state and the alive count decays on quiet
                # markets that were never dropped at all.
                if prev is not None and (now - prev) > self._quiet_ref:
                    self._resumed += 1
                    self._resumed_keys.add(k)
                self._last_update[k] = now
            # SLIP CHANNEL TRACE. The betslip price only exists because the PAGE subscribes it, so when a
            # quote times out we must be able to tell "we never asked" from "we asked and got nothing".
            # Logged in both directions rather than counted, because this fires only while a slip is open.
            elif m[0] == "offers_acca_hcap" and len(m) >= 2:
                print(f"[BIA-OBS] <- offers_acca_hcap {m[1]}", flush=True)
            elif m[0] in ("error", "api"):
                self._server_says.append((round(now - self._started, 1), json.dumps(m)[:400]))

    def _on_sent(self, payload) -> None:
        """Record what the PAGE subscribed to, so coverage can be reported honestly. We never send."""
        if not isinstance(payload, str):
            return
        try:
            msg = json.loads(payload)
        except (json.JSONDecodeError, TypeError):
            return
        # SLIP SUBSCRIBE TRACE. `watch_acca_hcaps` is what makes the venue push betslip prices, and it is
        # sent by the PAGE when a slip opens. Without this line an outbound subscribe is invisible here,
        # so a failed slip quote cannot be told apart: "the click never opened a slip" (nothing sent) vs
        # "the venue ignored us" (sent, no reply). Log only -- we still never send.
        if isinstance(msg, list) and msg and msg[0] in ("watch_acca_hcaps", "unwatch_acca_hcaps"):
            print(f"[BIA-OBS] -> {msg[0]} {json.dumps(msg[1])[:160]}", flush=True)
            return
        if not (isinstance(msg, list) and msg and msg[0] in ("watch_hcaps", "watch_event")):
            return
        ents = msg[1] if isinstance(msg[1], list) else []
        if ents and not isinstance(ents[0], list):
            ents = [ents]
        for e in ents:
            if isinstance(e, list) and len(e) >= 3:
                k = (e[1], e[2])
                self.feed._subs[k] = e[0]
                if k not in self._sub_order:
                    # ORDER matters: a server-side cap that evicts the oldest subscription shows up as
                    # silence correlated with subscription order, which is the only way to tell a cap
                    # from a market that simply is not ticking.
                    self._sub_order[k] = len(self._sub_order)
                    self._sub_time[k] = time.time()

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


    def coverage_table(self) -> str:
        """Per-sport venue coverage, ranked — the BOOK half of "which sport is most prone to arbs".

        The Kalshi half (how many contests it lists, and how much they trade) is measured separately; an
        arb needs BOTH sides, so a sport is only a candidate where these two overlap. Reported as a table
        rather than a dict because its whole job is to be read by a human deciding where to point the bot.

        `priced` is the number that matters: catalog is pushed for free on any connection, so a sport can
        show hundreds of catalog events and still be worth nothing until the page has subscribed them."""
        cov = self.coverage()
        rows = []
        for sport, d in (cov.get("by_sport") or {}).items():
            m, p = d["matches"], d["priced"]
            rows.append((p, sport, m, d["outrights"], (100.0 * p / m) if m else 0.0))
        rows.sort(reverse=True)
        out = [f"{'SPORT':<12}{'priced':>8}{'catalog':>9}{'cover':>8}{'outrights':>11}",
               "-" * 48]
        for p, sport, m, o, pct in rows:
            out.append(f"{sport:<12}{p:>8}{m:>9}{pct:>7.0f}%{o:>11}")
        out.append("-" * 48)
        out.append(f"{'TOTAL':<12}{cov.get('priced_total', 0):>8}{cov.get('catalog_matches', 0):>9}")
        return "\n".join(out)

    def drop_report(self, sport: Optional[str] = None, quiet_sec: float = 600.0) -> dict:
        """Which subscriptions are still ALIVE, grouped by league — the league-drop test.

        THE CONFOUND, stated up front: pre-live soccer barely ticks (4 of 90 events moved in a 7.4-min
        capture), so "no recent update" does NOT mean "dropped". Silence is the normal state of a
        pre-match book. Two signals separate a real eviction from ordinary quiet:

          * ORDER CORRELATION. A server-side cap evicts the OLDEST subscriptions, so the dead ones
            cluster at low `sub_order` while recently-added ones keep ticking. `alive_by_quartile`
            below is that test: a clean gradient across quartiles is a cap, a flat profile is not.
          * NEVER-vs-STOPPED. An event that priced once and went quiet is alive-but-still. One that
            was subscribed and NEVER priced was probably never really registered.

        And the third possibility the idle hour is for: if drops are TIME based, the dead set grows
        with wall-clock while `sub_order` stays uncorrelated.
        """
        now = time.time()
        rows = []
        for k, order in self._sub_order.items():
            sp, ekey = k
            if sport and sp != sport:
                continue
            if "multirunner" in ekey:
                continue
            ev = self.feed.all_events().get(k) or {}
            last = self._last_update.get(k)
            rows.append({
                "league": ev.get("competition_name") or "?",
                "order": order,
                "sub_age": round(now - self._sub_time.get(k, now), 1),
                "ever_priced": last is not None,
                "quiet_sec": round(now - last, 1) if last else None,
                "alive": bool(last and (now - last) <= quiet_sec),
            })
        rows.sort(key=lambda r: r["order"])
        by_league: dict[str, dict] = {}
        for r in rows:
            d = by_league.setdefault(r["league"], {"subscribed": 0, "ever_priced": 0, "alive": 0})
            d["subscribed"] += 1
            d["ever_priced"] += int(r["ever_priced"])
            d["alive"] += int(r["alive"])
        quart = []
        if rows:
            n = max(1, len(rows) // 4)
            for i in range(0, len(rows), n):
                chunk = rows[i:i + n]
                quart.append({"orders": f"{chunk[0]['order']}-{chunk[-1]['order']}",
                              "n": len(chunk),
                              "alive": sum(r["alive"] for r in chunk),
                              "ever_priced": sum(r["ever_priced"] for r in chunk)})
        return {"t": round(now - self._started, 1), "sport": sport,
                "subscribed": len(rows),
                "ever_priced": sum(r["ever_priced"] for r in rows),
                "alive": sum(r["alive"] for r in rows),
                # THE EVICTION TEST. Any nonzero value proves subscriptions are NOT being dropped:
                # a quiet event that starts updating again was never evicted. Read this before
                # `alive`, which on a pre-live book mostly measures how chatty the market is.
                "resumed_after_quiet": self._resumed,
                "events_that_resumed": len(self._resumed_keys),
                "quiet_threshold_sec": quiet_sec,
                "alive_by_quartile": quart,
                "by_league": dict(sorted(by_league.items(),
                                         key=lambda kv: -kv[1]["subscribed"])),
                "server_says": self._server_says[-8:]}

    def horizon_report(self, sport: str) -> str:
        """Split priced-vs-not by how soon the match starts, and by league.

        Diagnoses an incomplete sport page. Two very different causes look identical in a bare
        percentage:
          * DATE WINDOW  -- everything starting today is priced, later dates are not. The page shows
                            today's card; the rest will price when their day comes round.
          * COUNT CEILING -- today's games are themselves only partly priced. The page is
                            virtualising a long list and subscribing what it renders, so coverage
                            depends on scroll position rather than on the calendar.
        """
        import datetime as _dt
        now = _dt.datetime.now(_dt.timezone.utc)
        buckets = {"<24h": [0, 0], "24-72h": [0, 0], ">72h": [0, 0], "no start": [0, 0]}
        leagues_missing: collections.Counter = collections.Counter()
        for (s, k), ev in self.feed.all_events().items():
            if s != sport or "multirunner" in k:
                continue
            priced = bool((self.feed._books.get((s, k)) or {}).get("markets"))
            st = (ev or {}).get("start_ts")
            key = "no start"
            if st:
                try:
                    d = _dt.datetime.fromisoformat(str(st).replace("Z", "+00:00"))
                    hrs = (d - now).total_seconds() / 3600.0
                    key = "<24h" if hrs < 24 else ("24-72h" if hrs < 72 else ">72h")
                except Exception:
                    pass
            buckets[key][0 if priced else 1] += 1
            if not priced:
                leagues_missing[(ev or {}).get("competition_name") or "?"] += 1

        lines = ["  horizon      priced  unpriced"]
        for k, (p, u) in buckets.items():
            if p or u:
                lines.append(f"  {k:<11}{p:>7}{u:>10}")
        near = buckets["<24h"]
        near_total = near[0] + near[1]
        # A handful of unpriced near games is noise, not a ceiling: a real virtualised-list ceiling
        # leaves a LARGE fraction of today's card unsubscribed. The first cut fired on any nonzero and
        # so called a 785/789 run a "COUNT CEILING" off 2 stragglers.
        if near_total and near[1] / near_total <= 0.2:
            lines.append(f"  => NO CEILING: {near[0]}/{near_total} of the next 24h is priced"
                         + (f" ({near[1]} stragglers)." if near[1] else "."))
            if buckets[">72h"][1]:
                lines.append("     Unpriced far-future games are normal - they price when their day "
                             "comes round.")
        elif near[1]:
            lines.append("  => COUNT CEILING: even matches starting inside 24h are unpriced, so the "
                         "page is subscribing what it RENDERS. Coverage depends on scroll position.")
            lines.append("     Worst-affected leagues: "
                         + ", ".join(f"{n}x {nm[:26]}" for nm, n in leagues_missing.most_common(4)))
        return "\n".join(lines)


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="https://black.betinasia.com")
    ap.add_argument("--secs", type=float, default=120.0)
    ap.add_argument("--sport", default="tennis")
    ap.add_argument("--log", default="",
                    help="append periodic JSONL snapshots here. USE THIS for long runs -- otherwise "
                         "an hour of measurement exists only in the terminal and dies with it.")
    ap.add_argument("--snap-every", type=float, default=60.0, help="seconds between --log snapshots")
    ap.add_argument("--quiet-sec", type=float, default=600.0,
                    help="an event silent longer than this counts as not-alive in the drop report")
    args = ap.parse_args()

    obs = BetInAsiaObserver(url=args.url)
    await obs.start()
    print("[BIA-OBS] Leave it alone to measure PURE passive coverage, or browse normally to see what "
          "ordinary navigation adds. Ctrl+C to stop early.\n")
    logfp = open(args.log, "a", encoding="utf-8") if args.log else None
    if logfp:
        print(f"[BIA-OBS] logging snapshots every {args.snap_every:.0f}s -> {args.log}")
    try:
        deadline = time.time() + args.secs
        next_snap = time.time() + args.snap_every
        while time.time() < deadline:
            await asyncio.sleep(5)
            c = obs.coverage(args.sport)
            d = obs.drop_report(args.sport, args.quiet_sec)
            print(f"\r[BIA-OBS] frames={c['frames']:6d}  subscribed={d['subscribed']:4d}  "
                  f"ever-priced={d['ever_priced']:4d}  alive={d['alive']:4d}  "
                  f"{args.sport}: {c['priced']}/{c['catalog']} priced   ", end="", flush=True)
            if logfp and time.time() >= next_snap:
                logfp.write(json.dumps(d) + "\n")
                logfp.flush()
                next_snap = time.time() + args.snap_every
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        if logfp:
            logfp.write(json.dumps(obs.drop_report(args.sport, args.quiet_sec)) + "\n")
            logfp.close()
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
            # WHY the gap matters more than its size. A page showing 775 fixtures may subscribe only
            # what it renders (a COUNT ceiling -> scrolling would fix it) or only today's card (a DATE
            # window -> a later visit picks the rest up). Those need opposite responses, and the
            # horizon split is what tells them apart.
            print(obs.horizon_report(args.sport))
    await obs.stop()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        sys.exit(0)
