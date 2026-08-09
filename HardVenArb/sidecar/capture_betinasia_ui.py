"""
capture_betinasia_ui.py -- record the BetInAsia bet-slip DOM so a UI placement path can be written
against REAL markup instead of guessed selectors.

WHY A SECOND CAPTURE TOOL. `betinasia_recon.py` records the NETWORK (WebSocket frames + JSON calls).
It never touches the DOM, so it cannot tell you what to click. `bet_capture.py` is the DOM recorder
that was built for Pinnacle -- interaction descriptors with ranked candidate selectors, gated
mutation snapshots of whatever the slip does in response, screenshots, and the network call the click
produced. It is site-agnostic (`BetSlipRecorder.start(page)` takes any Playwright page); it was only
ever wired to the Pinnacle sidecar's managed browser. This launches BetInAsia's own profile and hands
it that same recorder.

    python capture_betinasia_ui.py                     # then place ONE small bet by hand
    python capture_betinasia_ui.py --url https://black.betinasia.com/sportsbook
    BIA_WINDOW_POS=2000,60 python capture_betinasia_ui.py

WHAT TO DO ONCE THE WINDOW IS UP (the recorder only captures around real interactions):
  1. Navigate to a TENNIS match -- tennis first, per the build order.
  2. Click the moneyline odds. That mutation IS the bet slip; it is the money click.
  3. Type a stake. The stake field is the other thing the bot must drive.
  4. Submit. Keep it minimum size -- this is a real bet.
  5. Watch it appear in the bet bar / open orders.
  6. Ctrl+C here.

Then repeat later for SOCCER: a 1X2 slip may well have different markup from a 2-way, and guessing
which is why this tool exists.

OUTPUT: bet_capture_<ts>.jsonl + bet_capture_<ts>_shots/*.png -- BOTH GITIGNORED, both contain account
data (balance, bet ids, whatever is on screen). A redacted summary prints at exit.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

from playwright.async_api import async_playwright

from bet_capture import BetSlipRecorder, summarize

PROFILE = Path(__file__).parent / ".betinasia_profile"


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="https://black.betinasia.com")
    ap.add_argument("--no-summary", action="store_true")
    args = ap.parse_args()

    size = os.environ.get("BIA_WINDOW_SIZE", "1440,900")
    pos = os.environ.get("BIA_WINDOW_POS", "")
    chrome_args = [f"--window-size={size}"] + ([f"--window-position={pos}"] if pos else [])

    rec = BetSlipRecorder()
    path = None
    async with async_playwright() as pw:
        # Same persistent profile the network recon used, so the session carries over and this does not
        # look like a new device. Never the Pinnacle profile -- the two must not share a fingerprint.
        ctx = await pw.chromium.launch_persistent_context(
            str(PROFILE), headless=False, args=chrome_args, viewport=None,
        )
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        try:
            await page.goto(args.url, wait_until="domcontentloaded", timeout=60_000)
        except Exception as e:
            print(f"[UI CAPTURE] initial navigation: {type(e).__name__}: {e} (browse manually)")

        res = await rec.start(page)
        if not res.get("ok", True) and res.get("error"):
            print(f"[UI CAPTURE] could not arm recorder: {res['error']}")
            await ctx.close()
            return 2

        print("[UI CAPTURE] ARMED. Place ONE small bet by hand:")
        print("   click the moneyline odds -> type a stake -> submit -> see it in the bet bar")
        print("   (tennis first). Ctrl+C here when done.\n")

        try:
            while ctx.pages:
                await asyncio.sleep(5)
                st = rec.status()
                if st.get("events"):
                    print(f"\r[UI CAPTURE] events={st['events']} "
                          f"shots={st.get('screenshots', 0)}   ", end="", flush=True)
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
        finally:
            print()
            out = await rec.stop()
            path = out.get("file")
            print(f"[UI CAPTURE] stopped -> {path}")
            try:
                await ctx.close()
            except Exception:
                pass

    if path and not args.no_summary:
        print("\n" + "=" * 78)
        print(summarize(path, redact=True))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        sys.exit(0)
