"""What does BetInAsia actually READ about this browser?

Opens the real profile, instruments the browser APIs a bot-detector would use, sits there, and reports
every access with the stack that made it. OBSERVE-ONLY: it loads a page and watches. It never clicks,
never navigates beyond the start URL, never places anything.

    python detect_recon.py                       # 120s on the tennis board
    python detect_recon.py --secs 300 --url https://black.betinasia.com/sportsbook/baseball
    python detect_recon.py --profile-copy        # throwaway profile (see the warning below)

WHY BOTHER, given the capture scan found no DataDome/PerimeterX/Kasada/FingerprintJS? Because absence of a
VENDOR is not absence of CHECKING. The site's own bundle can read `document.visibilityState` in one line,
and the passive capture cannot see a property read -- only a network call. This closes that gap.

⚠️ THIS TEST IS ITSELF SLIGHTLY DETECTABLE. Wrapping a native function changes what
`Function.prototype.toString` returns for it, which is a classic tamper check. That is mitigated here (the
wrappers report the ORIGINAL native source), but the mitigation is not perfect. Use --profile-copy to run
against a throwaway copy of the profile if you would rather not spend the real session on it.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

PROFILE = Path(__file__).parent / ".betinasia_profile"
DEFAULT_URL = "https://black.betinasia.com/sportsbook/tennis"

# Injected BEFORE any page script runs. Records accesses into window.__hvDetect.
# Every wrapper delegates to the original, so nothing the page does changes behaviour.
PROBE_JS = r"""
(() => {
  if (window.__hvDetect) return;
  const log = {};
  window.__hvDetect = log;

  const note = (name) => {
    const e = log[name] || (log[name] = { count: 0, stacks: [] });
    e.count++;
    if (e.stacks.length < 3) {
      // Third and later frames: frame 0 is Error, frame 1 is this hook. What we want is the CALLER.
      const s = (new Error().stack || "").split("\n").slice(3, 6)
                  .map(x => x.trim()).filter(Boolean).join(" <- ");
      if (s && !e.stacks.includes(s)) e.stacks.push(s);
    }
  };

  // Keep wrapped natives looking native: a detector that calls toString() on them sees the real source.
  const nativeToString = Function.prototype.toString;
  const originals = new WeakMap();
  Function.prototype.toString = function () {
    const orig = originals.get(this);
    return nativeToString.call(orig || this);
  };
  originals.set(Function.prototype.toString, nativeToString);

  const wrapFn = (obj, prop, name) => {
    try {
      const orig = obj[prop];
      if (typeof orig !== "function") return;
      const fn = function (...args) { note(name); return orig.apply(this, args); };
      originals.set(fn, orig);
      obj[prop] = fn;
    } catch (e) {}
  };

  const wrapGetter = (obj, prop, name) => {
    try {
      let d = Object.getOwnPropertyDescriptor(obj, prop);
      let proto = obj;
      while (!d && (proto = Object.getPrototypeOf(proto))) {
        d = Object.getOwnPropertyDescriptor(proto, prop);
      }
      if (!d || !d.get) return;
      const orig = d.get;
      const get = function () { note(name); return orig.call(this); };
      originals.set(get, orig);
      Object.defineProperty(obj, prop, { get, configurable: true });
    } catch (e) {}
  };

  // ── the ones that matter for THIS bot ──────────────────────────────────────
  wrapGetter(Document.prototype, "visibilityState", "document.visibilityState");
  wrapGetter(Document.prototype, "hidden",          "document.hidden");
  wrapFn(Document.prototype, "hasFocus",            "document.hasFocus()");
  wrapGetter(Event.prototype, "isTrusted",          "event.isTrusted");

  // ── generic automation tells ───────────────────────────────────────────────
  for (const p of ["webdriver","plugins","languages","hardwareConcurrency","deviceMemory",
                   "userAgent","platform","vendor","maxTouchPoints"]) {
    wrapGetter(Navigator.prototype, p, "navigator." + p);
  }
  for (const p of ["width","height","availWidth","availHeight","colorDepth"]) {
    wrapGetter(Screen.prototype, p, "screen." + p);
  }

  // ── fingerprinting surfaces ────────────────────────────────────────────────
  wrapFn(HTMLCanvasElement.prototype, "toDataURL", "canvas.toDataURL()");
  wrapFn(CanvasRenderingContext2D.prototype, "getImageData", "canvas.getImageData()");
  if (window.WebGLRenderingContext) {
    wrapFn(WebGLRenderingContext.prototype, "getParameter", "webgl.getParameter()");
  }
  if (window.AudioContext) wrapFn(window, "AudioContext", "AudioContext()");
  wrapFn(window, "RTCPeerConnection", "RTCPeerConnection()");

  // ── which behavioural events does the page LISTEN for? ──────────────────────
  // A page that registers mousemove is watching how the pointer arrives. That is the listener our
  // curved-path work exists to satisfy, so knowing whether it is even registered is the point.
  const WATCH = new Set(["mousemove","mousedown","mouseup","click","pointermove","pointerdown",
                         "keydown","keyup","touchstart","wheel","scroll",
                         "visibilitychange","blur","focus","beforeunload"]);
  const addEL = EventTarget.prototype.addEventListener;
  const wrappedAdd = function (type, ...rest) {
    if (WATCH.has(type)) {
      const what = this === document ? "document" :
                   this === window ? "window" :
                   (this && this.tagName ? this.tagName.toLowerCase() : "node");
      note("listener:" + type + " on " + what);
    }
    return addEL.call(this, type, ...rest);
  };
  originals.set(wrappedAdd, addEL);
  EventTarget.prototype.addEventListener = wrappedAdd;
})();
"""

REPORT_ORDER = [
    ("THIS BOT'S EXPOSURE", ["document.visibilityState", "document.hidden", "document.hasFocus()",
                            "event.isTrusted", "listener:mousemove", "listener:pointermove",
                            "listener:mousedown", "listener:click", "listener:visibilitychange",
                            "listener:blur", "listener:focus"]),
    ("AUTOMATION TELLS", ["navigator.webdriver", "navigator.plugins", "navigator.languages",
                          "navigator.hardwareConcurrency", "navigator.deviceMemory",
                          "navigator.platform", "navigator.vendor", "navigator.maxTouchPoints"]),
    ("FINGERPRINTING", ["canvas.toDataURL()", "canvas.getImageData()", "webgl.getParameter()",
                        "AudioContext()", "RTCPeerConnection()", "screen.width", "screen.height",
                        "screen.availWidth", "screen.colorDepth"]),
]


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=DEFAULT_URL)
    ap.add_argument("--secs", type=float, default=120.0)
    ap.add_argument("--profile-copy", action="store_true",
                    help="run against a throwaway COPY of the profile")
    ap.add_argument("--json", default="", help="also write the raw report here")
    a = ap.parse_args()

    from playwright.async_api import async_playwright

    profile = PROFILE
    tmp = None
    if a.profile_copy:
        tmp = Path(tempfile.mkdtemp(prefix="bia_recon_"))
        profile = tmp / "profile"
        print(f"[RECON] copying profile -> {profile}")
        shutil.copytree(PROFILE, profile)

    print(f"[RECON] observe-only: loading {a.url} for {a.secs:.0f}s. Nothing will be clicked.")
    async with async_playwright() as pw:
        ctx = await pw.chromium.launch_persistent_context(str(profile), headless=False)
        await ctx.add_init_script(PROBE_JS)
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await page.goto(a.url, wait_until="domcontentloaded")
        # Deliberately no interaction: we are measuring what the page reads UNPROMPTED.
        await asyncio.sleep(a.secs)
        try:
            log = await page.evaluate("() => window.__hvDetect || {}")
        except Exception as e:
            print(f"[RECON] could not read the probe back: {e}")
            log = {}
        await ctx.close()
    if tmp:
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n" + "=" * 78)
    shown = set()
    for heading, names in REPORT_ORDER:
        print(f"\n### {heading}")
        any_hit = False
        for n in names:
            hits = {k: v for k, v in log.items() if k == n or k.startswith(n + " on ")}
            for k, v in sorted(hits.items()):
                shown.add(k)
                any_hit = True
                print(f"  {v['count']:6}x  {k}")
                for s in v.get("stacks", [])[:2]:
                    print(f"            {s[:150]}")
        if not any_hit:
            print("  (nothing — the page never touched any of these)")
    rest = {k: v for k, v in log.items() if k not in shown}
    if rest:
        print("\n### OTHER")
        for k, v in sorted(rest.items(), key=lambda kv: -kv[1]["count"]):
            print(f"  {v['count']:6}x  {k}")

    print("\n### HOW TO READ THIS")
    print("  document.visibilityState / hidden / hasFocus  -> can see a click in a background tab")
    print("  listener:mousemove                            -> is watching how the cursor arrives")
    print("  event.isTrusted                               -> is checking for synthetic events")
    print("  navigator.webdriver                           -> is checking for automation outright")
    print("  canvas/webgl/AudioContext                     -> is building a device fingerprint")
    print("  NOTHING in a section = not checked, at least on this page in this window.")

    if a.json:
        Path(a.json).write_text(json.dumps(log, indent=2), encoding="utf-8")
        print(f"\n[RECON] raw report -> {a.json}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
