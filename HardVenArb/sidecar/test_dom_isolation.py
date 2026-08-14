"""Which DOM reads can a page SEE us make?

The venue does not currently instrument DOM properties — but our own canary proves how easy it is, so
"they could start" is a real scenario rather than paranoia. This decides what to do about it, on evidence:

    locator.inner_text()          Playwright's own operation. Documented to run in an isolated utility
                                  world, which page script cannot reach or patch.
    locator.evaluate(el => ...)   OUR javascript. Runs in the page's MAIN world, alongside theirs.

If the first is invisible and the second is not, then every read we can express as a locator operation
should be, and the ones that genuinely need main-world JS are the residual surface.

Runs against a data: URL — no network, no profile, no venue. Chromium only.

    python test_dom_isolation.py
"""
import asyncio
import sys

PAGE = """<!doctype html><html><body>
<div id="wrap"><div><span id="target">1.769</span></div></div>
</body></html>"""

# Patch the innerText getter in the MAIN world and count reads, exactly as a detector would.
SPY = r"""
(() => {
  window.__seen = 0;
  const d = Object.getOwnPropertyDescriptor(HTMLElement.prototype, "innerText");
  if (!d || !d.get) { window.__spyFailed = true; return; }
  const orig = d.get;
  Object.defineProperty(HTMLElement.prototype, "innerText", {
    get() { window.__seen++; return orig.call(this); },
    configurable: true,
  });
})();
"""

bad = 0


def check(label, ok):
    global bad
    print(f"  {'PASS' if ok else 'FAIL'}  {label}")
    if not ok:
        bad += 1


async def main() -> int:
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        ctx = await browser.new_context()
        await ctx.add_init_script(SPY)
        page = await ctx.new_page()
        await page.goto("data:text/html," + PAGE)

        if await page.evaluate("() => window.__spyFailed === true"):
            print("  SKIP  this browser does not expose an innerText getter to patch")
            await browser.close()
            return 0

        loc = page.locator("#target")

        # 1. Playwright's own read
        await page.evaluate("() => window.__seen = 0")
        txt = await loc.inner_text()
        seen_locator = await page.evaluate("() => window.__seen")
        print(f"\n[1] locator.inner_text() -> {txt!r}")
        check(f"the page observed {seen_locator} read(s)", True)

        # 2. our own main-world javascript
        await page.evaluate("() => window.__seen = 0")
        txt2 = await loc.evaluate("el => el.innerText")
        seen_eval = await page.evaluate("() => window.__seen")
        print(f"\n[2] locator.evaluate(el => el.innerText) -> {txt2!r}")
        check(f"the page observed {seen_eval} read(s)", True)

        # 3. the ancestor walk we actually use for the betslip panel
        await page.evaluate("() => window.__seen = 0")
        walked = await loc.evaluate(
            "el => { let n = el; for (let i = 0; i < 2 && n.parentElement; i++) n = n.parentElement;"
            "        return (n.innerText || ''); }")
        seen_walk = await page.evaluate("() => window.__seen")
        print(f"\n[3] the ancestor-walk evaluate -> {walked!r}")
        check(f"the page observed {seen_walk} read(s)", True)

        # 4. the same thing expressed as a LOCATOR (xpath ancestor + inner_text)
        await page.evaluate("() => window.__seen = 0")
        anc = loc.locator("xpath=ancestor::*[2]")
        n_anc = await anc.count()
        anc_txt = await anc.first.inner_text() if n_anc else ""
        seen_anc = await page.evaluate("() => window.__seen")
        print(f"\n[4] xpath ancestor locator + inner_text() -> {anc_txt!r} ({n_anc} match)")
        check(f"the page observed {seen_anc} read(s)", True)

        print("\n=== VERDICT ===")
        if seen_eval > 0 and seen_locator == 0:
            print("  Playwright's locator reads are INVISIBLE; our evaluate() is VISIBLE.")
            print("  -> express every read as a locator operation where possible.")
            check("locator reads are invisible to the page", True)
            check("evaluate reads are visible to the page", True)
            check("the xpath-ancestor form reproduces the walk invisibly",
                  seen_anc == 0 and anc_txt.strip() != "")
        elif seen_eval > 0 and seen_locator > 0:
            print("  BOTH are visible — Playwright's read goes through the same patched getter.")
            print("  -> swapping evaluate for locator APIs buys NOTHING. Do not bother.")
            check("finding recorded (no fix available by this route)", True)
        else:
            print(f"  Inconclusive: locator={seen_locator} evaluate={seen_eval}")
            check("conclusive result", False)

        await browser.close()
    return 1 if bad else 0


if __name__ == "__main__":
    code = asyncio.run(main())
    print("\nALL PASS" if code == 0 else "\nFAILURES")
    sys.exit(code)
