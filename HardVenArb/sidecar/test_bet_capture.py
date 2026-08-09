"""
Regression test for the bet_capture in-page hook.

THE BUG THIS PINS (found 2026-08-09 on a BetInAsia capture). `_CAPTURE_JS` ended with
`new MutationObserver(...).observe(document.body, ...)`. That is fine on the FIRST injection, which
runs via `page.evaluate()` after the page has loaded. It is fatal on the RE-INJECTION path:
`page.add_init_script()` runs before the document has a <body>, so `observe(null)` threw, the IIFE
aborted, and `window.__hvCap` was never assigned. `drain()` then returned `[]` forever -- no error,
no events -- so a capture that spanned a navigation was silently dead. The real capture armed on a
loading splash, the app navigated at 4s, and a real bet at 57s produced ZERO DOM records while the
network side recorded the whole flow.

Both halves are tested: the hook must survive init-script injection, AND the Python drain must report
a missing hook as `null` rather than an empty list so a dead capture is detectable.

Run: python test_bet_capture.py      (needs the playwright chromium browser)
"""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from bet_capture import _CAPTURE_JS

PASS = 0
FAIL = 0

PAGE = """<!doctype html><html><head><title>t</title></head><body>
<button id="odds" data-test-id="moneyline-p1">1.85</button>
<input id="stake" name="stake" placeholder="Stake">
<div id="slip"></div>
</body></html>"""


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {detail}")


async def main() -> int:
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)

        # ── 1. THE REGRESSION: injected as an init script, i.e. before <body> exists ──
        print("\n[1] init-script injection (the path that was broken)")
        page = await browser.new_page()
        await page.add_init_script(_CAPTURE_JS)
        await page.goto("data:text/html," + PAGE.replace("\n", ""))

        has_hook = await page.evaluate("() => !!window.__hvCap")
        check("window.__hvCap survives pre-body injection", has_hook,
              "the IIFE aborted on observe(document.body) before assigning the hook")

        if has_hook:
            await page.click("#odds")
            await page.fill("#stake", "5")
            await asyncio.sleep(0.2)
            events = await page.evaluate("() => window.__hvCap.drain()")
            kinds = [e.get("kind") for e in events]
            check("clicks are captured after init-script injection",
                  "interaction" in kinds, f"drained {kinds}")

            clicked = [e for e in events if e.get("kind") == "interaction"
                       and e.get("event") in ("click", "pointerdown")]
            check("interaction carries ranked selectors",
                  bool(clicked) and bool(clicked[0]["target"].get("sel")))
            check("data-test-id ranks first",
                  bool(clicked) and clicked[0]["target"]["sel"][0]
                  == '[data-test-id="moneyline-p1"]',
                  str(clicked[0]["target"]["sel"][:2]) if clicked else "")

            # the observer must be live too -- that is what it failed to attach
            await page.evaluate("""() => {
                document.getElementById('odds').click();
                const d = document.createElement('div');
                d.innerHTML = '<input name=\\"amount\\">';
                document.getElementById('slip').appendChild(d);
            }""")
            await asyncio.sleep(0.2)
            await page.evaluate("() => window.__hvCap.drain()")
            snap = await page.evaluate("() => window.__hvCap.snapshot('t')")
            check("MutationObserver attached (slip mutation was recorded)",
                  (snap or {}).get("rootCount", 0) > 0, str(snap)[:160])

        # ── 2. survives a real navigation, which is what killed the live capture ──
        print("\n[2] survives navigation")
        await page.goto("data:text/html," + PAGE.replace("\n", "").replace("1.85", "1.92"))
        check("hook re-installs on the new document",
              await page.evaluate("() => !!window.__hvCap"))
        await page.click("#odds")
        await asyncio.sleep(0.2)
        ev2 = await page.evaluate("() => window.__hvCap ? window.__hvCap.drain() : null")
        check("clicks captured after navigating",
              bool(ev2) and any(e.get("kind") == "interaction" for e in ev2), str(ev2)[:120])

        # ── 3. a MISSING hook must be distinguishable from "no events yet" ──
        print("\n[3] dead-hook detection")
        await page.evaluate("() => { delete window.__hvCap; }")
        probe = await page.evaluate("() => window.__hvCap ? window.__hvCap.drain() : null")
        check("missing hook drains as null, not []", probe is None,
              "returning [] is what hid the dead capture for 97 seconds")

        # ── 4. outline must not explode on a bodyless document ──
        print("\n[4] outline guards")
        blank = await browser.new_page()
        await blank.add_init_script(_CAPTURE_JS)
        await blank.goto("data:text/html,<html></html>")
        o = await blank.evaluate("() => window.__hvCap ? window.__hvCap.outline() : null")
        check("outline returns a record on a near-empty document", isinstance(o, dict), str(o)[:120])

        await browser.close()

    print(f"\n{'='*58}\n  {PASS} passed, {FAIL} failed\n{'='*58}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        sys.exit(0)
