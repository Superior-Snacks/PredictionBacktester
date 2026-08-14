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
from human_mouse import CURSOR

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
        # Events the PAGE has subscribed on the BETSLIP (acca) channel, KEYED BY SOCKET. Once subscribed
        # the venue keeps pushing that event's slip prices, and a REPEAT watch_acca_hcaps is answered
        # `event_already_subscribed` with NO price — so knowing this set is what stops us clicking a
        # second time and then waiting for a push that will never come.
        self._acca_by_ws: dict[int, set] = {}
        self._rover = None                             # fallback click tab (see rover())
        self._sport_tabs: dict = {}                    # sport code -> parked board tab (see sport_tab())

    @property
    def _acca_subs(self) -> set:
        """Every LIVE socket's betslip subscriptions, unioned.

        PER SOCKET, not global. A subscription belongs to the socket that sent it: close the tab — or
        navigate it, which drops the old socket — and the venue forgets it. A single global set would go
        on claiming the event was subscribed, and slip_quote REFUSES TO CLICK when it believes that, so
        every later quote for that event fails against a socket that never subscribed anything. That is a
        permanent self-inflicted blind spot, and a roving tab would mint one on every navigation."""
        out: set = set()
        for s in self._acca_by_ws.values():
            out |= s
        return out

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
        # BIA_RECON=1: record this browser's traffic in the SAME format betinasia_recon.py writes, so the
        # bot's session can be diffed against a hand-driven one (`human_envelope.py --compare`).
        # Needed because the two cannot be captured the same way: betinasia_recon.py owns its own browser
        # and profile, while a bot leg has to run inside the sidecar's — and both want .betinasia_profile,
        # so they can never be up at once. Arming the recorder HERE is the only way to record what the bot
        # actually sends. Off by default: the dump is large and contains session data.
        self._recon = None
        if os.environ.get("BIA_RECON") == "1":
            try:
                from datetime import datetime as _dt
                from betinasia_recon import Recon
                out = Path(__file__).parent / f"betinasia_recon_{_dt.now():%Y%m%d_%H%M%S}.jsonl"
                self._recon = Recon(out)
                self._log(f"RECON ON -> {out.name} (gitignored; contains session data)")
            except Exception as e:
                self._log(f"RECON requested but could not start: {type(e).__name__}: {e}")

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

    # ── parked per-sport board tabs ───────────────────────────────────────────
    async def sport_tab(self, sport: str, url: str = "", expand: bool = True):
        """The PARKED board tab for one sport. Returns its page, creating it on first use.

        WHY ONE PER SPORT, when a single tab already accumulates every sport's subscriptions. Prices are
        not the point here — the DOM is. A slip quote has to CLICK a row, and a row only exists on the page
        currently rendering that sport. With one tab the rover had to navigate to the league for every
        quote, and on a cold league that cost >20s and lost the arb (2026-08-13, Vandecasteele/Shelbayh).

        For the LOW-VOLUME sports this bot now covers, a sport's whole slate fits on its board page, so a
        parked, fully-expanded tab per sport means the row is already on screen when the arb fires and the
        quote is a pure find-and-click. The rover stays as the fallback for anything the board does not
        carry — which is what it was always for.

        Parked means PARKED: created once, expanded once, never navigated again. Its socket keeps the
        subscriptions it accumulated, and re-navigating would drop them."""
        if self._ctx is None:
            return None
        pg = self._sport_tabs.get(sport)
        if pg is not None and not pg.is_closed():
            # STILL ON ITS BOARD? A Page handle survives anything the OPERATOR does — focus, tab order,
            # scrolling — but not navigation: click into a match on this tab and the handle stays perfectly
            # valid while the page now shows something else entirely. The bot would then hunt for a tennis
            # row on a football page, find nothing, and silently fall through to the rover for every quote.
            # The URL is the cheap, honest check; if it has wandered, put it back.
            try:
                here = (pg.url or "").split("?")[0]
            except Exception:
                here = ""
            if url and here and not here.rstrip("/").endswith(url.rstrip("/").rsplit("/", 1)[-1]):
                self._log(f"sport tab {sport}: was navigated to {here[:70]} — restoring its board")
            else:
                return pg
            # fall through: re-navigate and re-expand this same tab
        if pg is None or pg.is_closed():
            pg = await self._ctx.new_page()      # _ctx.on("page") hooks its sockets for us
            self._sport_tabs[sport] = pg
        if url:
            try:
                await pg.goto(url, wait_until="domcontentloaded", timeout=60_000)
            except Exception as e:
                self._log(f"sport tab {sport}: navigation failed ({type(e).__name__}: {e})")
                return pg
            await asyncio.sleep(float(os.environ.get("BIA_SPORT_TAB_SETTLE_SEC", "3")))
            if expand:
                # Pay the expansion cost ONCE, here, instead of on every quote's critical path.
                try:
                    n = await self.expand_all(label=sport, page=pg)
                    self._log(f"sport tab {sport}: parked and expanded ({n} 'Show more' click(s))")
                except Exception as e:
                    self._log(f"sport tab {sport}: expand failed ({type(e).__name__}: {e})")
        return pg

    async def open_sport_tabs(self, targets: list, expand: bool = True) -> dict:
        """Park one board tab per (sport_code, url), expanded. Returns {sport: ok}."""
        out: dict[str, bool] = {}
        for code, url in targets:
            pg = await self.sport_tab(code, url, expand=expand)
            out[code] = pg is not None and not pg.is_closed()
            await asyncio.sleep(float(os.environ.get("BIA_SPORT_TAB_GAP_SEC", "2")))
        self._log(f"parked {sum(1 for v in out.values() if v)}/{len(targets)} sport board tab(s)")
        return out

    async def reset_sport_tabs(self, targets: list, expand: bool = True) -> dict:
        """Close every board tab (and the rover) and park the whole sequence again.

        WHY A PERIODIC RESET AT ALL. A parked tab can only be clicked for what its DOM holds, and that DOM
        is a snapshot of park time: fixtures the venue lists later never appear in it, so a slip quote for
        a game added an hour ago falls through to the rover and pays a navigation. Subscriptions have the
        same shape — the page subscribes what it RENDERED, so a board that has grown since is only
        partially covered. Reloading is the one action that refreshes both.

        WHAT IT COSTS. Closing a socket drops the subscriptions it was holding, so there is a real gap
        between close and re-park; that is the price of the refresh and the reason this belongs on a slow
        cadence rather than a fast one. Nothing is lost permanently — the re-park re-subscribes whatever
        the board now shows, which is a superset of what it showed before.

        The caller holds the slip lock across this, so a quote can never find its tab closed underneath
        it, and a reset never starts while a click is in flight."""
        old = list(self._sport_tabs.items())
        closed = 0
        for _sport, pg in old:
            try:
                if pg is not None and not pg.is_closed():
                    await pg.close()
                    closed += 1
            except Exception:
                pass
        self._sport_tabs.clear()
        rv, self._rover = self._rover, None
        try:
            if rv is not None and not rv.is_closed():
                await rv.close()
                closed += 1
        except Exception:
            pass
        self._log(f"board reset: closed {closed} tab(s), re-parking {len(targets)} sport(s)")
        return await self.open_sport_tabs(targets, expand=expand)

    # ── the roving tab ────────────────────────────────────────────────────────
    async def rover(self, url: str = ""):
        """The SECOND tab — the only one allowed to click. Returns the page, creating it on first use.

        WHY IT MUST NOT BE THE OBSERVING TAB. Board subscriptions accumulate on a socket and are never
        dropped (measured: tennis 6 -> 83, still 83 after navigating away to football), so the observing
        tab holds EVERY sport's book at once no matter what it is displaying — as long as it is left
        alone. Verifying a price is the opposite kind of act: navigate to that game's league page, click
        its moneyline, read the slip push. Doing that on the observing tab would drag the whole feed
        around for one quote. Splitting them lets the feed sit still while the rover moves, and two tabs
        is an unremarkable thing for a person to have open (nine parked ones are not).

        The rover is hooked by `_ctx.on("page")` like any other tab, so its own socket feeds the SAME
        caches — its slip pushes land in `_slip_books` exactly as the observer's board pushes do."""
        if self._ctx is None:
            return None
        pg = self._rover
        if pg is None or pg.is_closed():
            pg = await self._ctx.new_page()          # _ctx.on("page") hooks its sockets for us
            self._rover = pg
            self._log("opened the roving tab (slip verification only)")
        if url:
            try:
                await pg.goto(url, wait_until="domcontentloaded", timeout=60_000)
            except Exception as e:
                self._log(f"rover navigation to {url} failed ({type(e).__name__}: {e})")
                return None
        return pg

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
            await self.expand_all(code)
            cov = self.coverage(code)
            priced, cat = int(cov.get("priced") or 0), int(cov.get("catalog") or 0)
            seen[code] = priced
            pct = f"{100.0 * priced / cat:.0f}%" if cat else "n/a"
            self._log(f"visit {code}: {priced} priced / {cat} catalog matches ({pct})")
        self._log(f"sport walk done: {', '.join(f'{k}={v}' for k, v in seen.items())} "
                  f"| priced across all sports={self.coverage().get('priced_total')}")
        return seen

    async def expand_all(self, label: str = "", page=None) -> int:
        """Click every "Show more" on the current board until none remain. Returns clicks made.

        TWO REASONS, and the second is the bigger one:
        1. A collapsed competition's rows do not EXIST in the DOM, so `slip_quote` has to expand before it
           can find its row — paying that cost on the execution path, when the arb is already ticking.
           Doing it up front makes a quote a pure find-and-click.
        2. The page only subscribes what it RENDERED. Collapsed rows are never subscribed, which is why
           football sat at 148 priced against a 930-event catalog. Expanding is therefore the cheapest
           coverage fix available — it is the same action that makes quotes fast.

        Clicking "Show more" is ordinary browsing, not automation-only behaviour, and it is paced. Bounded
        by BIA_EXPAND_MAX so a board that regenerates the control cannot spin forever.
        """
        # WHICH PAGE. This used to hardcode self._page, so every per-sport call expanded the ORIGINAL tab
        # instead of the one asked about: parking 5 board tabs reported "tennis: 7 clicks" (that was
        # football, the main tab) and then 0 for the next four (football was already expanded). The sport
        # tabs themselves were never expanded at all. Take the page as an argument.
        page = page or self._page
        if page is None:
            return 0
        limit = int(os.environ.get("BIA_EXPAND_MAX", "40"))
        pace = float(os.environ.get("BIA_EXPAND_PACE_SEC", "0.35"))
        # SCROLL, DON'T JUST QUERY. The board renders lazily: sections below the fold are not in the DOM,
        # so get_by_text finds no "Show more" for them and — the bigger cost — THE PAGE NEVER SUBSCRIBES
        # THEM EITHER, because it only subscribes what it has rendered. Walking down the page is what turns
        # a partial board into a whole one, for prices as much as for clicking.
        scroll_rounds = int(os.environ.get("BIA_EXPAND_SCROLLS", "12"))
        clicks = 0
        stagnant = 0
        for _ in range(limit):
            try:
                more = page.get_by_text("Show more", exact=True)
                n = await more.count()
                if n == 0:
                    # Nothing visible — reveal more board and look again, rather than declaring it done.
                    if stagnant >= scroll_rounds:
                        break
                    stagnant += 1
                    await page.mouse.wheel(0, 1400)
                    await asyncio.sleep(pace)
                    continue
                stagnant = 0
                # Always take the FIRST: expanding removes that control, so the next iteration naturally
                # advances to the next competition. Indexing into a shifting list would skip entries.
                # Human approach on the way in — a board reset fires ~11 of these an hour, and a burst of
                # teleporting clicks is exactly the shape a click-stream check looks for.
                await CURSOR.click(page, more.first, timeout=5_000)
                clicks += 1
                await asyncio.sleep(pace)
            except Exception:
                break     # control vanished mid-click, or the board re-rendered — not worth retrying
        # Back to the top: a tab parked half-way down a board is both an odd thing to leave lying around
        # and a worse starting point for the next find-and-click.
        try:
            await page.keyboard.press("Home")
        except Exception:
            pass
        self._log(f"expanded {clicks} 'Show more' section(s){' on ' + label if label else ''} "
                  f"({stagnant} scroll(s) with nothing to expand)")
        return clicks

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
        # Every tab, including sport tabs and the rover — a slip quote happens on one of those, so
        # hooking only the first page would record everything EXCEPT the thing being measured.
        if getattr(self, "_recon", None) is not None:
            try:
                self._recon.hook_page(page)
            except Exception as e:
                self._log(f"recon could not hook a tab: {type(e).__name__}: {e}")

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
        # The socket is bound in so its betslip subscriptions can die WITH it (see _acca_subs).
        ws.on("framesent", lambda p, _w=ws: self._on_sent(p, _w))
        ws.on("close", lambda *_a, _w=ws: self._on_ws_close(_w))

    def _on_ws_close(self, ws) -> None:
        """Forget this socket's betslip subscriptions. The venue drops them when the socket goes, so
        keeping them would make slip_quote refuse to click an event nothing is subscribed to."""
        gone = self._acca_by_ws.pop(id(ws), None)
        if gone:
            self._log(f"socket closed - releasing {len(gone)} betslip subscription(s)")

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

    def _on_sent(self, payload, ws=None) -> None:
        """Record what the PAGE subscribed to, so coverage can be reported honestly. We never send."""
        if not isinstance(payload, str):
            return
        bucket = self._acca_by_ws.setdefault(id(ws), set())
        try:
            msg = json.loads(payload)
        except (json.JSONDecodeError, TypeError):
            return
        # SLIP SUBSCRIBE TRACE. `watch_acca_hcaps` is what makes the venue push betslip prices, and it is
        # sent by the PAGE when a slip opens. Without this line an outbound subscribe is invisible here,
        # so a failed slip quote cannot be told apart: "the click never opened a slip" (nothing sent) vs
        # "the venue ignored us" (sent, no reply). Log only -- we still never send.
        if isinstance(msg, list) and msg and msg[0] in ("watch_acca_hcaps", "unwatch_acca_hcaps"):
            ents = msg[1] if isinstance(msg[1], list) else []
            if ents and not isinstance(ents[0], list):
                ents = [ents]
            for e in ents:
                if isinstance(e, list) and len(e) >= 3:
                    k = (e[1], e[2])
                    if msg[0] == "watch_acca_hcaps":
                        bucket.add(k)
                    else:
                        bucket.discard(k)
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
                               # EVERY sport, not the top 12. BIA carries ~33 and football alone
                               # takes five slots (fb, fb_ht, fb_htft, fb_corn, fb_corn_ht) with 3.5k
                               # selections, so a low-volume sport fell off the end and READ AS ABSENT.
                               # That cost real debugging time: baseball and mma looked unsubscribed
                               # while /catalog had 46 and 44 selections for them all along.
                               for s, _ in cat.most_common()}
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
