"""
betinasia_recon.py -- instrumented browser for BetInAsia recon. The account-free first step of the same
playbook that built the Pinnacle adapter: open a managed window, YOU browse (and log in, when you have the
account), and every WebSocket + JSON API call the site makes is recorded for the adapter design.

    python betinasia_recon.py                          # opens https://betinasia.com
    python betinasia_recon.py --url https://black.betinasia.com
    BIA_WINDOW_POS=2000,60 python betinasia_recon.py   # park it on the second display

Writes betinasia_recon_YYYYmmdd_HHMMSS.jsonl (gitignored -- it WILL contain session/account data once you
log in) and prints a live summary every 30s. Ctrl+C (or closing the window) prints the final report:
every WS endpoint with frame counts + samples, and the JSON endpoints ranked by hits.

What the dump must answer before a betinasia_adapter.py is worth writing:
  1. WS: exposed? JSON or binary? subscribe protocol? pre-match or in-play only?
  2. Prices: PER-BOOK (a 'pinnacle' column -- the roundabout-WS idea needs this) or blended best-price only?
  3. Selection ids: does the feed expose the underlying book's event/market ids (mechanical token mapping)
     or only BetInAsia's own ids (another name-matching problem)?
  4. The odds/catalog REST endpoints and their shapes (the odds()/catalog() implementation).
  5. Once an account exists: the bet POST (bet_capture.py can record the slip flow in detail later).

SEPARATE PROFILE (.betinasia_profile): never share the Pinnacle profile -- the two sessions must not
cross-contaminate fingerprints or cookies.
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

PROFILE = Path(__file__).parent / ".betinasia_profile"
# 4000 was "enough to see the schema" but it truncated 46% of the 2026-08-05 capture into invalid
# JSON, and the casualties were exactly the long ones: the `event` catalog frames (teams arrays) came
# through almost entirely unusable, so the replay corpus in test_betinasia.py can barely exercise
# catalog(). 60k keeps whole frames while still bounding a runaway socket. Override if a capture gets
# unwieldy -- but never below ~20k, or the catalog path goes untested again.
MAX_FRAME_CHARS = int(os.environ.get("BIA_RECON_MAX_FRAME_CHARS", "60000"))
MAX_FRAMES_PER_WS = 120     # cap for the BULK 'event' catalog frames (they repeat); non-event frames below
MAX_INTERESTING_PER_WS = 400  # price/odds/other frames are the prize -- keep far more of them
SKIP_URL = re.compile(r"\.(png|jpe?g|gif|svg|webp|woff2?|ttf|css|ico|mp4)(\?|$)|googletagmanager|"
                      r"google-analytics|hotjar|sentry|intercom|facebook|doubleclick", re.I)
ODDSY = re.compile(r"odd|price|market|event|match|sport|line|bet|fixture|selection|tennis", re.I)


_MSG_TYPE = re.compile(r'^\[?\["(\w+)"')      # Molly frames look like [["event",...],["price",...]] etc.
_BORING = {"event", "ping", "pong"}           # catalog sync + keepalives: store a few, then just count


class Recon:
    def __init__(self, out_path: Path):
        self.out = out_path.open("a", encoding="utf-8")
        self.ws_frames: dict[str, int] = Counter()          # ALL frames seen, per socket
        self.ws_stored: dict[str, int] = Counter()          # bulk 'event'/keepalive frames stored
        self.ws_interesting: dict[str, int] = Counter()     # non-boring (price/odds/...) frames stored
        self.ws_types: dict[str, Counter] = defaultdict(Counter)   # per-socket message-type census
        self.endpoints: Counter = Counter()
        self.t0 = time.time()
        self.n_records = 0

    def rec(self, kind: str, **kw) -> None:
        row = {"t": round(time.time() - self.t0, 3), "kind": kind, **kw}
        self.out.write(json.dumps(row, ensure_ascii=False) + "\n")
        self.n_records += 1
        if self.n_records % 200 == 0:
            self.out.flush()

    # ── WebSockets: the main prize ─────────────────────────────────────────────
    def hook_ws(self, ws) -> None:
        url = ws.url
        self.rec("ws_open", url=url)
        print(f"[WS OPEN] {url}")

        def frame(direction):
            def h(payload):
                self.ws_frames[url] += 1
                body = payload if isinstance(payload, str) else f"<binary {len(payload)}B>"
                m = _MSG_TYPE.match(body) if isinstance(payload, str) else None
                mtype = m.group(1) if m else ("<binary>" if not isinstance(payload, str) else "<other>")
                self.ws_types[url][mtype] += 1
                # PRICE frames are the prize and arrive AFTER the event-catalog flood. Store non-boring frames
                # (anything but event/ping/pong) far more generously so a catalog burst can't bury the schema.
                if mtype not in _BORING:
                    if self.ws_interesting[url] >= MAX_INTERESTING_PER_WS:
                        return
                    self.ws_interesting[url] += 1
                elif self.ws_stored[url] >= MAX_FRAMES_PER_WS:
                    return
                else:
                    self.ws_stored[url] += 1
                self.rec("ws_frame", url=url, dir=direction, n=self.ws_frames[url], mtype=mtype,
                         body=str(body)[:MAX_FRAME_CHARS])
            return h

        ws.on("framesent", frame("out"))
        ws.on("framereceived", frame("in"))
        ws.on("close", lambda: self.rec("ws_close", url=url, total_frames=self.ws_frames[url]))

    # ── XHR/fetch JSON traffic: the catalog/odds REST shapes ───────────────────
    async def hook_response(self, resp) -> None:
        url = resp.url
        if SKIP_URL.search(url):
            return
        ctype = (resp.headers or {}).get("content-type", "")
        if "json" not in ctype and "text/plain" not in ctype:
            return
        key = url.split("?")[0]
        self.endpoints[key] += 1
        body = ""
        if self.endpoints[key] <= 5:                     # store the first few bodies per endpoint, then count
            try:
                body = (await resp.text())[:MAX_FRAME_CHARS]
            except Exception:
                body = "<unreadable>"
        req = resp.request
        post = ""
        if req.method == "POST" and self.endpoints[key] <= 5:
            try:
                post = (req.post_data or "")[:800]
            except Exception:
                post = ""
        self.rec("http", method=req.method, status=resp.status, url=url[:400], post=post, body=body)

    def hook_page(self, page) -> None:
        page.on("websocket", self.hook_ws)
        page.on("response", lambda r: asyncio.create_task(self.hook_response(r)))
        page.on("worker", lambda w: self.rec("worker", url=w.url))   # Pinnacle ran its WS in a worker; check here too
        self.rec("page_open", url=page.url)

    # ── summaries ──────────────────────────────────────────────────────────────
    def summary(self, final: bool = False) -> None:
        mins = (time.time() - self.t0) / 60.0
        print(f"\n----- recon {'FINAL' if final else 'status'} @ {mins:.1f} min -----")
        if self.ws_frames:
            print("WebSockets:")
            for url, n in self.ws_frames.most_common():
                types = ", ".join(f"{t}:{c}" for t, c in self.ws_types[url].most_common(6))
                print(f"  {n:>6} frames  {url[:100]}")
                print(f"           types: {types}")
                nonboring = self.ws_types[url].keys() - _BORING
                if nonboring:
                    print(f"           >>> NON-CATALOG frame types seen (the prize): {sorted(nonboring)}")
        else:
            print("WebSockets: none seen yet")
        oddsy = [(u, n) for u, n in self.endpoints.most_common(30) if ODDSY.search(u)]
        if oddsy:
            print("Likely odds/catalog endpoints (hits):")
            for u, n in oddsy[:15]:
                print(f"  {n:>5}  {u[:110]}")
        print(f"records written: {self.n_records}")


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="https://www.betinasia.com")
    args = ap.parse_args()

    out = Path(__file__).parent / f"betinasia_recon_{datetime.now():%Y%m%d_%H%M%S}.jsonl"
    rec = Recon(out)
    print(f"[RECON] dump -> {out.name}  (gitignored; contains session data once logged in)")
    print("[RECON] browse the site: open the tennis page, watch a match's odds, log in when you have the")
    print("        account. Every WS + JSON call is recorded. Ctrl+C here (or close the window) to finish.")

    pos = os.environ.get("BIA_WINDOW_POS", "")
    size = os.environ.get("BIA_WINDOW_SIZE", "1440,900")
    args_list = [f"--window-size={size}"]
    if pos:
        args_list.append(f"--window-position={pos}")

    async with async_playwright() as pw:
        ctx = await pw.chromium.launch_persistent_context(
            str(PROFILE), headless=False, args=args_list,
            viewport=None,                              # let the window size rule (no fixed viewport tell)
        )
        ctx.on("page", rec.hook_page)
        for pg in ctx.pages:
            rec.hook_page(pg)
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        try:
            await page.goto(args.url, wait_until="domcontentloaded", timeout=60_000)
        except Exception as e:
            print(f"[RECON] initial navigation: {type(e).__name__}: {e} (browse manually)")

        try:
            while ctx.pages:                            # closing the last window ends the run
                await asyncio.sleep(30)
                rec.summary()
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
        finally:
            rec.summary(final=True)
            rec.out.flush()
            rec.out.close()
            try:
                await ctx.close()
            except Exception:
                pass
    print(f"[RECON] done -> {out}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        sys.exit(0)
