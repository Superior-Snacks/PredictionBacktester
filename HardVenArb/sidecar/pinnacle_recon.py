"""pinnacle_recon.py -- instrumented browser for Pinnacle recon, in-play edition.

Same playbook that produced the BetInAsia adapter, pointed at the questions the in-play CAMPER needs
answered before it is built on assumptions:

  1. IS THE RE-PRICE A WEBSOCKET FEED? The camper parks with a Quick Bet open for tens of seconds and
     relies on it re-pricing. If that is a socket push, an armed slip stays live for free. If it is
     polling, the interval IS the staleness, and the camper must re-read before every press.
  2. WHERE IS THE LIVE TAB? Every navigation is recorded, so browsing to in-play tennis by hand yields
     the exact URL the camper should park on — the same way the league-page URL was derived for BIA.
  3. WHAT IS PINNACLE WATCHING? BetInAsia's `/web/metrics/` turned out to carry `betslip.duration` and
     `betslip.source`, which changed how the bot behaves. Pinnacle's equivalent is unknown, and a bot
     that camps for minutes with a slip open is exactly the shape such telemetry would notice.

Usage — the sidecar must be STOPPED (they cannot share .pinnacle_profile):

    python pinnacle_recon.py                       # opens the live tennis page
    python pinnacle_recon.py --url https://www.pinnacle.com/en/tennis/matchups/live/
    python pinnacle_recon.py --seconds 600         # run longer
    PINNACLE_WINDOW_POS=2000,60 python pinnacle_recon.py

You browse; it records. Writes pinnacle_recon_YYYYmmdd_HHMMSS.jsonl (GITIGNORED — it WILL contain
session data once logged in) and prints a report on Ctrl+C:

  * every WS endpoint, frame counts, direction, and whether odds numbers appear in the frames
  * every JSON/XHR endpoint ranked by hits, with the POLLING INTERVAL where one is detectable
  * a TELEMETRY section: analytics/monitoring/error-reporting beacons, separated from product APIs
  * a FINGERPRINTING section: which detection-adjacent browser APIs the site actually touched

The fingerprint probe is an init script that wraps the APIs and counts reads. That is main-world and
therefore observable in principle — acceptable for a deliberate recon run, not something to leave on.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

from playwright.async_api import async_playwright

PROFILE = Path(os.environ.get("PINNACLE_USER_DATA_DIR")
               or (Path(__file__).resolve().parent / ".pinnacle_profile")).expanduser().resolve()

DEFAULT_URL = os.environ.get("PINNACLE_RECON_URL", "https://www.pinnacle.com/en/tennis/matchups/live/")

MAX_FRAME_CHARS = int(os.environ.get("PIN_RECON_MAX_FRAME_CHARS", "60000"))
MAX_FRAMES_PER_WS = int(os.environ.get("PIN_RECON_MAX_FRAMES", "4000"))

SKIP_URL = re.compile(r"\.(png|jpe?g|gif|svg|webp|woff2?|ttf|css|ico|mp4|m4s)(\?|$)", re.I)

# Anything matching these is MONITORING rather than product API. Kept and reported separately instead of
# skipped: for BetInAsia the telemetry endpoint was the single most behaviour-relevant thing found, and a
# recon that filters analytics out by default would have missed it entirely.
TELEMETRY = re.compile(
    r"googletagmanager|google-analytics|/gtag/|/collect|doubleclick|facebook|"
    r"sentry|bugsnag|rollbar|datadog|newrelic|nr-data|dynatrace|appdynamics|"
    r"hotjar|fullstory|logrocket|mouseflow|clarity\.ms|segment|amplitude|mixpanel|"
    r"perimeterx|px-cloud|datadome|akamai|imperva|distil|shieldsquare|castle|sift|"
    r"/metrics|/telemetry|/beacon|/track|/rum|/insight", re.I)

ODDS_HINT = re.compile(r'"(price|odds|handicap|moneyline|matchup|market|line|status)"', re.I)

# BET LIFECYCLE. Today a fill is detected by holding `page.expect_response` around the Place click and
# reading `betId` out of POST /bets/straight — a 15s timeout leaves the state UNKNOWN. That is workable
# for one bet at a time and poor for a CAMPER, which fires repeatedly and would serialise on it.
# BetInAsia turned out to PUSH its entire order lifecycle over the socket (order open -> bet placing ->
# bet done, with the routed price and stake), which needs no request and cannot be missed by a timeout.
# Whether Pinnacle does the same is unknown, and this run is the chance to find out.
BET_HINT = re.compile(r'"(betId|betid|wagerId|ticketId|wagerNumber|acceptedPrice|placedAt|'
                      r'betStatus|wagerStatus|stake|risk|win|toWin)"', re.I)
# Never truncate or drop these — the placement bodies are the artifact the whole question turns on.
BET_URL = re.compile(r"/bets(/|\?|$)|/bets/straight|/wagers|/tickets|/betslip|/wallet/balance", re.I)

# Wraps the APIs a bot-detector would read. Counts only — no values are altered, so the page behaves
# normally and only the ACCESS is recorded.
PROBE_JS = r"""
(() => {
  if (window.__pinprobe) return;
  const hits = {};
  window.__pinprobe = hits;
  const bump = (k) => { hits[k] = (hits[k] || 0) + 1; };
  const wrapGet = (obj, name, key) => {
    try {
      const d = Object.getOwnPropertyDescriptor(obj, name);
      if (!d || !d.get) return;
      Object.defineProperty(obj, name, Object.assign({}, d, {
        get: function () { bump(key); return d.get.call(this); }}));
    } catch (e) {}
  };
  const wrapFn = (obj, name, key) => {
    try {
      const f = obj[name];
      if (typeof f !== 'function') return;
      obj[name] = function () { bump(key); return f.apply(this, arguments); };
    } catch (e) {}
  };
  for (const p of ['webdriver','plugins','languages','platform','hardwareConcurrency','deviceMemory',
                   'userAgent','vendor','maxTouchPoints','connection','permissions'])
    wrapGet(Navigator.prototype, p, 'navigator.' + p);
  wrapFn(HTMLCanvasElement.prototype, 'toDataURL', 'canvas.toDataURL');
  wrapFn(HTMLCanvasElement.prototype, 'getContext', 'canvas.getContext');
  wrapFn(WebGLRenderingContext.prototype, 'getParameter', 'webgl.getParameter');
  try { wrapFn(WebGL2RenderingContext.prototype, 'getParameter', 'webgl2.getParameter'); } catch (e) {}
  wrapFn(window, 'requestIdleCallback', 'requestIdleCallback');
  wrapFn(Element.prototype, 'getBoundingClientRect', 'getBoundingClientRect');
  try { wrapFn(window.PerformanceObserver && window.PerformanceObserver.prototype, 'observe',
               'PerformanceObserver.observe'); } catch (e) {}
  try { wrapFn(window.Notification, 'requestPermission', 'Notification.requestPermission'); } catch (e) {}
  try { wrapFn(AudioContext.prototype, 'createOscillator', 'audio.createOscillator'); } catch (e) {}
  for (const t of ['mousemove','mousedown','keydown','touchstart','devicemotion','visibilitychange'])
    ((tt) => { const a = document.addEventListener;
      document.addEventListener = function (n) { if (n === tt) bump('listener:' + tt);
                                                 return a.apply(this, arguments); }; })(t);
  return 1;
})()
"""


def post_text(req) -> str:
    """Request body as text, or a marker — NEVER raising.

    `request.post_data` base64-decodes and then `.decode()`s as UTF-8, which throws on a binary body.
    Pinnacle posts gzipped payloads (0x1f 0x8b), so this raised on a large share of requests — and
    because it raised INSIDE the response handler, those responses were dropped from the capture
    entirely rather than merely logged noisily. A recon that silently discards the requests it cannot
    decode is worse than one that records them as opaque.
    """
    try:
        return req.post_data or ""
    except Exception:
        pass
    try:
        buf = req.post_data_buffer
        if buf:
            head = bytes(buf[:2])
            if head == b"\x1f\x8b":
                return f"[gzip {len(buf)}B]"
            return buf.decode("utf-8", errors="replace")
    except Exception:
        pass
    return "[unreadable body]"


class Recon:
    def __init__(self, path: Path):
        self.f = open(path, "a", encoding="utf-8")
        self.t0 = time.time()
        self.ws_frames = Counter()
        self.ws_odds = Counter()
        self.ws_binary = Counter()
        self.ws_pkt = Counter()          # (url, mqtt packet type) -> count
        self.ws_samples = defaultdict(list)
        self.http_hits = Counter()
        self.http_times = defaultdict(list)
        self.http_bodies = defaultdict(int)
        self.navs = []
        self.bet_http = []          # (t, method, url, status, post, body) for anything bet-shaped
        self.bet_ws = []            # (t, url, dir, payload) for WS frames naming a bet

    def w(self, kind: str, **kw) -> None:
        try:
            self.f.write(json.dumps({"t": round(time.time() - self.t0, 3), "kind": kind, **kw}) + "\n")
        except Exception:
            pass

    # ── websocket ────────────────────────────────────────────────────────────
    def on_ws(self, ws) -> None:
        url = ws.url
        self.w("ws_open", url=url)
        print(f"[WS OPEN] {url[:110]}")

        def frame(payload, direction):
            key = url.split("?")[0]
            self.ws_frames[(key, direction)] += 1
            n = self.ws_frames[(key, direction)]
            if n > MAX_FRAMES_PER_WS:
                return
            # BINARY FRAMES ARE THE WHOLE POINT HERE. Pinnacle's Arcadia socket is MQTT-over-WebSocket,
            # so every frame arrives as bytes and logging "[binary]" discards exactly the evidence the
            # question needs — whether the re-price is pushed, and what it contains. Keep the bytes:
            # hex for the header (MQTT's first byte encodes the packet type) and a lossy text rendering,
            # because MQTT PUBLISH payloads on this API are JSON often enough to be readable.
            if isinstance(payload, str):
                s = payload
            else:
                b = bytes(payload or b"")
                self.ws_binary[key] += 1
                if b:
                    self.ws_pkt[(key, b[0] >> 4)] += 1      # MQTT packet type = high nibble of byte 0
                s = (f"[binary {len(b)}B hex={b[:16].hex()} "
                     f"text={b.decode('utf-8', errors='replace')[:MAX_FRAME_CHARS]}]")
            if ODDS_HINT.search(s or ""):
                self.ws_odds[key] += 1
            # A bet named on the socket is the answer to "can a fill be listened for instead of awaited".
            if BET_HINT.search(s or ""):
                self.bet_ws.append((round(time.time() - self.t0, 1), key, direction, (s or "")[:900]))
            if len(self.ws_samples[key]) < 6 and direction == "in":
                self.ws_samples[key].append((s or "")[:400])
            self.w("ws_frame", url=key, dir=direction, body=(s or "")[:MAX_FRAME_CHARS])

        ws.on("framereceived", lambda p: frame(p, "in"))
        ws.on("framesent", lambda p: frame(p, "out"))
        ws.on("close", lambda _=None: self.w("ws_close", url=url))

    # ── http ─────────────────────────────────────────────────────────────────
    async def on_response(self, resp) -> None:
        """Never let a single bad response kill the handler. A raise here escapes into pyee's callback,
        prints a full traceback per request, and — the part that actually matters — abandons that
        response instead of recording it. Losing captures to a decode error is the opposite of recon."""
        try:
            await self._on_response(resp)
        except Exception as e:
            self.w("http_error", err=f"{type(e).__name__}: {e}")

    async def _on_response(self, resp) -> None:
        url = resp.url
        if SKIP_URL.search(url):
            return
        key = url.split("?")[0]
        self.http_hits[key] += 1
        self.http_times[key].append(time.time() - self.t0)
        req = resp.request
        body = None
        is_bet = bool(BET_URL.search(url))
        # Bodies are what identify a POLLING price endpoint from a static asset. Cheap cap per endpoint —
        # EXCEPT for bet endpoints, which are kept in full and forever. The BIA capture learned this the
        # hard way: a five-body cap threw away everything after the fifth response, and the order
        # lifecycle lives entirely in the ones after that.
        if is_bet or self.http_bodies[key] < 5:
            try:
                ct = (resp.headers or {}).get("content-type", "")
                if "json" in ct or "text" in ct:
                    body = (await resp.text())[:20000 if is_bet else 4000]
                    self.http_bodies[key] += 1
            except Exception:
                pass
        if is_bet:
            self.bet_http.append((round(time.time() - self.t0, 1), req.method, url, resp.status,
                                  post_text(req)[:1200], (body or "")[:1200]))
        self.w("http", url=url, method=req.method, status=resp.status,
               telemetry=bool(TELEMETRY.search(url)),
               post=post_text(req)[:2000] if req.method != "GET" else None,
               body=body)

    def on_nav(self, frame) -> None:
        try:
            if frame.parent_frame is not None:
                return
            self.navs.append((round(time.time() - self.t0, 1), frame.url))
            self.w("nav", url=frame.url)
            print(f"[NAV] {frame.url[:110]}")
        except Exception:
            pass

    # ── report ───────────────────────────────────────────────────────────────
    def report(self, probe: dict) -> None:
        print("\n" + "=" * 78)
        print("WEBSOCKETS  — is the re-price pushed?")
        print("=" * 78)
        if not self.ws_frames:
            print("  NO WebSocket frames at all. If prices still moved on screen, the re-price is POLLED")
            print("  and the HTTP section below will show the interval — the camper must then re-read")
            print("  the popover immediately before every press rather than trusting what it shows.")
        else:
            for (url, direction), n in self.ws_frames.most_common():
                mark = f"   <-- {self.ws_odds[url]} frames carry price/odds fields" if direction == "in" \
                       and self.ws_odds.get(url) else ""
                print(f"  {direction:3}  {n:6}  {url[:88]}{mark}")
            if self.ws_pkt:
                # MQTT control packet types, high nibble of the first byte. PUBLISH (3) inbound IS the
                # price push; if the socket is nothing but PINGREQ/PINGRESP (12/13) it is only a
                # keepalive and the prices really are coming from the ~6s HTTP poll.
                names = {1: "CONNECT", 2: "CONNACK", 3: "PUBLISH", 4: "PUBACK", 8: "SUBSCRIBE",
                         9: "SUBACK", 10: "UNSUBSCRIBE", 12: "PINGREQ", 13: "PINGRESP", 14: "DISCONNECT"}
                print("\n  MQTT packet types seen (high nibble of byte 0):")
                for (url, t), n in self.ws_pkt.most_common(12):
                    print(f"    {n:6}  type {t:2} {names.get(t, '?'):12} on {url[-40:]}")
                pub = sum(n for (_u, t), n in self.ws_pkt.items() if t == 3)
                if pub:
                    print(f"\n  {pub} PUBLISH frames — the socket carries DATA, not just keepalive.")
                else:
                    print("\n  NO PUBLISH frames — this socket may be keepalive only, in which case the")
                    print("  ~6s HTTP poll below is the real price source and IS the staleness bound.")
            for url, s in self.ws_samples.items():
                if s:
                    print(f"\n  sample inbound frames on {url[:70]}:")
                    for x in s[:3]:
                        print(f"    {x[:400]}")

        print("\n" + "=" * 78)
        print("HTTP  — product endpoints, with polling interval where detectable")
        print("=" * 78)
        for url, n in self.http_hits.most_common(24):
            if TELEMETRY.search(url):
                continue
            ts = sorted(self.http_times[url])
            gap = ""
            if len(ts) >= 3:
                gaps = [round(b - a, 1) for a, b in zip(ts, ts[1:])]
                gaps.sort()
                med = gaps[len(gaps) // 2]
                if med > 0:
                    gap = f"   every ~{med:.1f}s  <-- POLLED" if med < 120 else ""
            print(f"  {n:5}  {url[:92]}{gap}")

        print("\n" + "=" * 78)
        print("TELEMETRY / MONITORING  — what Pinnacle reports about this session")
        print("=" * 78)
        tel = [(u, n) for u, n in self.http_hits.most_common() if TELEMETRY.search(u)]
        if not tel:
            print("  none seen. (BetInAsia's /web/metrics/ carried betslip.duration and betslip.source,")
            print("  which changed how the bot behaves — absence here is a real and useful result.)")
        for u, n in tel:
            print(f"  {n:5}  {u[:92]}")

        print("\n" + "=" * 78)
        print("FINGERPRINT-ADJACENT APIs the page actually touched")
        print("=" * 78)
        if not probe:
            print("  probe recorded nothing (page may have reloaded after install)")
        for k, n in sorted(probe.items(), key=lambda kv: -kv[1])[:30]:
            print(f"  {n:7}x  {k}")

        print("\n" + "=" * 78)
        print("BET LIFECYCLE  — can a fill be LISTENED for, or only awaited?")
        print("=" * 78)
        if not self.bet_http and not self.bet_ws:
            print("  nothing bet-shaped seen. Place one small real bet during a recon run — this section")
            print("  is empty by construction until you do.")
        for t, url, direction, s in self.bet_ws[:14]:
            print(f"  t+{t:7.1f}s  WS {direction:3}  {url[:60]}")
            print(f"                {s[:200]}")
        if self.bet_ws:
            print("\n  ^^ A bet named on the SOCKET means the fill can be observed with no request and no")
            print("     15s expect_response window — which is what the camper needs to fire repeatedly.")
        for t, m, url, st, post, body in self.bet_http[:16]:
            print(f"  t+{t:7.1f}s  {m:5} {st}  {url[:78]}")
            if post:
                print(f"                POST {post[:170]}")
            if body:
                print(f"                <-   {body[:200]}")
        if self.bet_http and not self.bet_ws:
            print("\n  ^^ HTTP only, no socket frames. Then the current design (expect_response on")
            print("     POST /bets/straight) is already the best available, and the camper must keep")
            print("     firing serialised — worth knowing before building it the other way.")

        print("\n" + "=" * 78)
        print("NAVIGATION  — the URLs you visited (the camper needs the live-tennis one)")
        print("=" * 78)
        for t, u in self.navs[-25:]:
            print(f"  t+{t:7.1f}s  {u[:100]}")


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=DEFAULT_URL)
    ap.add_argument("--seconds", type=float, default=420.0)
    # OFF BY DEFAULT, and it must stay that way. The probe patches navigator getters, canvas, WebGL and
    # getBoundingClientRect — i.e. it makes the browser look exactly like the thing detectors hunt for,
    # on the LIVE-MONEY profile. Enabling it on 2026-08-16 produced the account's first-ever captcha.
    # Only use it on a THROWAWAY profile (PINNACLE_USER_DATA_DIR=/some/copy).
    ap.add_argument("--probe", action="store_true",
                    help="wrap fingerprint APIs to count reads. PATCHES NATIVES — never on the real "
                         "profile; point PINNACLE_USER_DATA_DIR at a copy first.")
    a = ap.parse_args()

    out = Path(__file__).parent / f"pinnacle_recon_{datetime.now():%Y%m%d_%H%M%S}.jsonl"
    rec = Recon(out)
    print(f"[REC] {out.name}  (gitignored — contains session data once logged in)")
    print(f"[REC] profile {PROFILE}")
    print("[REC] the SIDECAR MUST BE STOPPED — they cannot share this profile.\n")

    async with async_playwright() as pw:
        # ⚠ MIRROR pinnacle_session.py EXACTLY. This opens the LIVE-MONEY profile, so any difference in
        # launch configuration presents the account's own browser with a changed fingerprint. The first
        # version of this script omitted all of it — no `ignore_default_args`, so Chrome ran WITH
        # `--enable-automation` and `navigator.webdriver` read TRUE — and produced the account's first
        # captcha in its history. Keep these in step with pinnacle_session.py if that ever changes.
        win_args = []
        for env, flag in (("PINNACLE_WINDOW_POS", "--window-position"),
                          ("PINNACLE_WINDOW_SIZE", "--window-size")):
            v = (os.environ.get(env) or "").strip()
            if v:
                win_args.append(f"{flag}={v}")
        launch = dict(user_data_dir=str(PROFILE), headless=False,
                      viewport={"width": 1400, "height": 900},
                      args=[*win_args,
                            "--disable-blink-features=AutomationControlled",
                            "--disable-background-timer-throttling",
                            "--disable-backgrounding-occluded-windows",
                            "--disable-renderer-backgrounding"],
                      ignore_default_args=["--enable-automation"])
        try:
            ctx = await pw.chromium.launch_persistent_context(channel="chrome", **launch)
        except Exception:
            ctx = await pw.chromium.launch_persistent_context(**launch)
        await ctx.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});")
        if a.probe:
            print("[REC] ⚠ FINGERPRINT PROBE ON — it patches natives. Only acceptable on a COPY of the "
                  "profile; on the real one this is what triggered a captcha.")
            await ctx.add_init_script(PROBE_JS)

        def hook(pg):
            pg.on("websocket", rec.on_ws)
            pg.on("response", lambda r: asyncio.create_task(rec.on_response(r)))
            pg.on("framenavigated", rec.on_nav)

        ctx.on("page", hook)
        for pg in ctx.pages:
            hook(pg)
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await page.goto(a.url, wait_until="domcontentloaded")

        print("BROWSE NOW. Things worth doing while it records:")
        print("  1. find the LIVE tennis list (the URL is captured for the camper)")
        print("  2. open a Quick Bet on a live game, enter a stake, and LEAVE IT for a minute —")
        print("     watch whether the price moves, and whether the slip survives")
        print("  3. PLACE ONE SMALL REAL BET. Today a fill is detected by holding expect_response")
        print("     around the Place click; if the socket announces the bet instead, the camper can")
        print("     LISTEN rather than wait, and cannot miss a fill to a timeout. The BET LIFECYCLE")
        print("     section is empty unless a bet actually goes on.")
        print("  4. then open My Bets — that page fires the site's own listing call, worth capturing")
        print("  5. let it sit idle a while, so any heartbeat/telemetry cadence shows up")
        print(f"\nRecording for {a.seconds:.0f}s — Ctrl+C to stop early and print the report.\n")

        probe = {}
        try:
            end = time.time() + a.seconds
            while time.time() < end:
                await asyncio.sleep(15)
                try:
                    probe = await page.evaluate("() => window.__pinprobe || {}")
                except Exception:
                    pass
                ws_in = sum(n for (u, d), n in rec.ws_frames.items() if d == "in")
                print(f"  t+{time.time() - rec.t0:5.0f}s  ws_in={ws_in}  http={sum(rec.http_hits.values())}"
                      f"  endpoints={len(rec.http_hits)}")
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
        finally:
            try:
                probe = await page.evaluate("() => window.__pinprobe || {}") or probe
            except Exception:
                pass
            rec.report(probe)
            rec.f.close()
            print(f"\n[REC] written to {out.name}")
            try:
                await ctx.close()
            except Exception:
                pass
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        pass
