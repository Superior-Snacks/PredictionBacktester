"""Where is the balance on the page, and what selector reaches it?

The header read in `_balance_from_dom` guesses: money-shaped text (two decimals) in the top 160px,
rightmost wins. That was written without ever seeing the markup. This prints the actual candidates with
their position, classes and ancestry, so the selector can be chosen from evidence instead.

Runs against the SIDECAR's live browser — no second profile, no login, nothing clicked:

    python balance_recon.py                    # sidecar on 8788
    python balance_recon.py --port 8787

If the sidecar is not up, falls back to opening the profile itself (close the sidecar first, they
cannot share .betinasia_profile).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

PROFILE = Path(__file__).parent / ".betinasia_profile"

# Money-shaped: exactly two decimals. Board odds carry three (1.769) and scores are bare integers, so
# this alone excludes nearly everything on a sportsbook page.
MONEY = re.compile(r"^[$€£]?\s?\d[\d,]*\.\d{2}$")

DUMP_JS = r"""
() => {
  const money = /^[$€£]?\s?\d[\d,]*\.\d{2}$/;
  const out = [];
  const walk = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  let n;
  while ((n = walk.nextNode())) {
    const t = (n.textContent || "").trim();
    if (!money.test(t)) continue;
    const el = n.parentElement;
    if (!el) continue;
    const r = el.getBoundingClientRect();
    if (r.width === 0 && r.height === 0) continue;
    const chain = [];
    let p = el;
    for (let i = 0; i < 5 && p; i++) {
      chain.push(p.tagName.toLowerCase()
        + (p.id ? "#" + p.id : "")
        + (p.className && typeof p.className === "string"
            ? "." + p.className.trim().split(/\s+/).slice(0, 3).join(".")
            : ""));
      p = p.parentElement;
    }
    out.push({
      text: t,
      x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width),
      tag: el.tagName.toLowerCase(),
      cls: (typeof el.className === "string" ? el.className : "") || null,
      id: el.id || null,
      title: el.getAttribute("title") || null,
      testid: el.getAttribute("data-testid") || el.getAttribute("data-test") || null,
      chain: chain,
      near: (el.parentElement ? (el.parentElement.innerText || "") : "").replace(/\s+/g, " ").slice(0, 90),
    });
  }
  return {vw: window.innerWidth, vh: window.innerHeight, hits: out.slice(0, 40)};
}
"""


async def dump(page) -> None:
    try:
        res = await page.evaluate(DUMP_JS)
    except Exception as e:
        print(f"could not read the page: {type(e).__name__}: {e}")
        return
    print(f"viewport {res['vw']}x{res['vh']}   money-shaped text nodes: {len(res['hits'])}\n")
    if not res["hits"]:
        print("NONE. The balance may be behind an account menu — open it and re-run, or it is not\n"
              "rendered as `1,234.56`. Copy the element by hand in that case.")
        return
    # Header band first, rightmost first — the shape `_balance_from_dom` assumes.
    hits = sorted(res["hits"], key=lambda h: (h["y"] > 160, -h["x"]))
    for h in hits:
        band = "HEADER" if h["y"] <= 160 else "body  "
        print(f"  {band}  {h['text']:>12}   x={h['x']:>5} y={h['y']:>5} w={h['w']:>4}  <{h['tag']}>")
        for k in ("id", "testid", "title", "cls"):
            if h.get(k):
                print(f"                       {k:7} {str(h[k])[:80]}")
        print(f"                       chain   {' < '.join(h['chain'])[:110]}")
        if h["near"]:
            print(f"                       context {h['near']}")
        print()
    top = [h for h in hits if h["y"] <= 160]
    print("=" * 78)
    if top:
        print(f"CURRENT HEURISTIC would pick: {top[0]['text']!r} at x={top[0]['x']} y={top[0]['y']}")
        print("If that is not the balance, the fix is a stable attribute from the block above —")
        print("prefer data-testid or id; class names on this site are hashed and change on deploy.")
    else:
        print("NOTHING in the top 160px — the heuristic would find nothing and fall back to the fetch.")
        print("Either widen BIA_BALANCE_HEADER_PX, or anchor on an attribute instead of position.")


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8788)
    ap.add_argument("--url", default="https://black.betinasia.com/sportsbook/tennis")
    a = ap.parse_args()

    from playwright.async_api import async_playwright
    async with async_playwright() as pw:
        # Prefer the RUNNING sidecar's browser: no second profile, no login, and it is the exact page
        # the bot reads. Falls back to opening the profile only if nothing is listening.
        try:
            import urllib.request
            urllib.request.urlopen(f"http://127.0.0.1:{a.port}/health", timeout=2).read()
            print(f"[BAL] sidecar is up on {a.port}, but Playwright cannot attach to a browser it did not\n"
                  f"      launch. Stop the sidecar and re-run, or read the element by hand from its window.")
            return 2
        except Exception:
            pass
        print(f"[BAL] opening {a.url} on the profile (observe-only, nothing clicked)")
        ctx = await pw.chromium.launch_persistent_context(str(PROFILE), headless=False, viewport=None)
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await page.goto(a.url, wait_until="domcontentloaded")
        await asyncio.sleep(6)
        await dump(page)
        print("\n[BAL] leaving the window open for 60s — open the account menu now if the balance is not\n"
              "      in the header, and the dump will re-run.")
        await asyncio.sleep(60)
        await dump(page)
        await ctx.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
