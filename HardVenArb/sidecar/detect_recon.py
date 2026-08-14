"""What does BetInAsia actually READ about this browser, and what leaves the machine?

Opens the real profile, instruments the browser APIs a bot-detector would use, records every access WITH
THE CODE THAT MADE IT, watches the network alongside, and turns the result into bot-design implications.

OBSERVE-ONLY: it loads a page and watches. It never clicks, never navigates beyond the start URL, never
places anything.

    python detect_recon.py                          # 120s on the tennis board
    python detect_recon.py --secs 300 --json d.json
    python detect_recon.py --profile-copy           # throwaway profile (see the warning below)

WHY A BROWSER PROBE AND NOT JUST THE NETWORK CAPTURE. A property READ makes no request, so a passive HTTP
capture cannot see `document.visibilityState` being polled. Conversely a probe cannot see TLS/JA3
fingerprinting, HTTP/2 frame ordering, or anything computed server-side from data already sent. The two
are complements; `--net` reports the second half so the gap is at least visible.

⚠️ ATTRIBUTION IS BY TOP STACK FRAME — the code that made the call, not whoever called it. An earlier
version matched patterns against the whole joined stack, so every app-level handler registered inside a
React effect was filed as "React" and BIA's own reads disappeared into library noise. If you change this,
keep the top-frame rule.

⚠️ THIS TEST IS ITSELF SLIGHTLY DETECTABLE. Wrapping a native changes what `Function.prototype.toString`
returns for it, a classic tamper check. Mitigated (wrappers report the ORIGINAL native source) but not
perfectly. `--profile-copy` runs against a throwaway copy if you would rather not spend the real session.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import shutil
import sys
import tempfile
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

PROFILE = Path(__file__).parent / ".betinasia_profile"
DEFAULT_URL = "https://black.betinasia.com/sportsbook/tennis"

PROBE_JS = r"""
(() => {
  if (window.__hvDetect) return;
  const t0 = Date.now();
  const log = {};
  window.__hvDetect = log;

  const note = (name) => {
    const e = log[name] || (log[name] = { count: 0, first: null, last: null, stacks: {} });
    e.count++;
    const at = (Date.now() - t0) / 1000;
    if (e.first === null) e.first = at;
    e.last = at;
    // Frame 0 is Error, frame 1 is this hook, frame 2 is the CALLER. Keep the caller and its caller.
    const raw = (new Error().stack || "").split("\n").slice(3, 5).map(x => x.trim()).filter(Boolean);
    if (raw.length) {
      const key = raw.join(" <- ");
      if (Object.keys(e.stacks).length < 6) e.stacks[key] = (e.stacks[key] || 0) + 1;
      else if (e.stacks[key] !== undefined) e.stacks[key]++;
    }
  };

  // Keep wrapped natives looking native to a toString() tamper check.
  const nativeToString = Function.prototype.toString;
  const originals = new WeakMap();
  const patchedToString = function () {
    const orig = originals.get(this);
    return nativeToString.call(orig || this);
  };
  originals.set(patchedToString, nativeToString);
  Function.prototype.toString = patchedToString;

  const wrapFn = (obj, prop, name) => {
    try {
      if (!obj) return;
      const orig = obj[prop];
      if (typeof orig !== "function") return;
      const fn = function (...a) { note(name); return orig.apply(this, a); };
      originals.set(fn, orig);
      obj[prop] = fn;
    } catch (e) {}
  };

  // Walks the prototype chain, so `navigator.webdriver` (defined on Navigator.prototype) is caught.
  const wrapGetter = (obj, prop, name) => {
    try {
      if (!obj) return;
      let d = Object.getOwnPropertyDescriptor(obj, prop), proto = obj;
      while (!d && (proto = Object.getPrototypeOf(proto))) d = Object.getOwnPropertyDescriptor(proto, prop);
      if (!d) return;
      if (d.get) {
        const orig = d.get;
        const get = function () { note(name); return orig.call(this); };
        originals.set(get, orig);
        Object.defineProperty(obj, prop, { get, configurable: true });
      } else if ("value" in d && d.configurable) {
        // Plain data properties (window.outerWidth, window.chrome) need a value trap instead.
        const v = d.value;
        Object.defineProperty(obj, prop, { get() { note(name); return v; }, configurable: true });
      }
    } catch (e) {}
  };

  // ── visibility / focus: can they see a click in a background tab? ───────────
  wrapGetter(Document.prototype, "visibilityState", "document.visibilityState");
  wrapGetter(Document.prototype, "hidden",          "document.hidden");
  wrapFn(Document.prototype, "hasFocus",            "document.hasFocus()");
  wrapGetter(Event.prototype, "isTrusted",          "event.isTrusted");

  // ── outright automation checks ─────────────────────────────────────────────
  for (const p of ["webdriver","plugins","languages","hardwareConcurrency","deviceMemory",
                   "userAgent","userAgentData","platform","vendor","maxTouchPoints","connection",
                   "permissions","mediaDevices","doNotTrack","pdfViewerEnabled"]) {
    wrapGetter(Navigator.prototype, p, "navigator." + p);
  }
  wrapFn(Navigator.prototype, "getBattery", "navigator.getBattery()");
  wrapGetter(window, "chrome", "window.chrome");
  try { wrapGetter(Notification, "permission", "Notification.permission"); } catch (e) {}
  try { wrapFn(navigator.permissions, "query", "navigator.permissions.query()"); } catch (e) {}
  try { wrapFn(navigator.mediaDevices, "enumerateDevices", "mediaDevices.enumerateDevices()"); } catch (e) {}

  // ── HEADLESS / WINDOW-GEOMETRY tells ───────────────────────────────────────
  // outerWidth === 0, or outerWidth === innerWidth, is the classic headless signature.
  for (const p of ["outerWidth","outerHeight","innerWidth","innerHeight","devicePixelRatio"]) {
    wrapGetter(window, p, "window." + p);
  }
  for (const p of ["width","height","availWidth","availHeight","colorDepth","pixelDepth"]) {
    wrapGetter(Screen.prototype, p, "screen." + p);
  }

  // ── device fingerprinting ──────────────────────────────────────────────────
  wrapFn(HTMLCanvasElement.prototype, "toDataURL", "canvas.toDataURL()");
  wrapFn(HTMLCanvasElement.prototype, "toBlob",    "canvas.toBlob()");
  wrapFn(CanvasRenderingContext2D.prototype, "getImageData", "canvas.getImageData()");
  if (window.WebGLRenderingContext)  wrapFn(WebGLRenderingContext.prototype,  "getParameter", "webgl.getParameter()");
  if (window.WebGL2RenderingContext) wrapFn(WebGL2RenderingContext.prototype, "getParameter", "webgl2.getParameter()");
  wrapFn(window, "AudioContext", "AudioContext()");
  wrapFn(window, "OfflineAudioContext", "OfflineAudioContext()");
  wrapFn(window, "RTCPeerConnection", "RTCPeerConnection()");
  wrapFn(window, "OffscreenCanvas", "OffscreenCanvas()");
  try { wrapFn(speechSynthesis, "getVoices", "speechSynthesis.getVoices()"); } catch (e) {}
  try { wrapFn(Intl.DateTimeFormat.prototype, "resolvedOptions", "Intl.resolvedOptions()"); } catch (e) {}

  // ── timing: a tight loop here can measure how slow our hooks are ───────────
  wrapFn(performance, "now", "performance.now()");

  // ── which behavioural events are LISTENED for, and by whom ─────────────────
  const WATCH = new Set(["mousemove","mousedown","mouseup","click","dblclick","contextmenu",
                         "pointermove","pointerdown","pointerup","keydown","keyup","keypress",
                         "touchstart","touchmove","wheel","scroll","copy","paste",
                         "visibilitychange","blur","focus","beforeunload","pagehide","devicemotion"]);
  const addEL = EventTarget.prototype.addEventListener;
  const wrappedAdd = function (type, ...rest) {
    if (WATCH.has(type)) {
      const what = this === document ? "document" : this === window ? "window"
                 : (this && this.tagName ? this.tagName.toLowerCase() : "node");
      note("listener:" + type + " on " + what);
    }
    return addEL.call(this, type, ...rest);
  };
  originals.set(wrappedAdd, addEL);
  EventTarget.prototype.addEventListener = wrappedAdd;
})();
"""

SECTIONS = [
    ("VISIBILITY / FOCUS — can they see a click in a hidden tab?",
     ["document.visibilityState", "document.hidden", "document.hasFocus()",
      "listener:visibilitychange", "listener:blur", "listener:focus", "listener:pagehide"]),
    ("AUTOMATION CHECKS — are they looking for a bot at all?",
     ["navigator.webdriver", "event.isTrusted", "window.chrome", "navigator.plugins",
      "navigator.permissions", "navigator.permissions.query()", "Notification.permission",
      "navigator.pdfViewerEnabled", "navigator.maxTouchPoints", "navigator.hardwareConcurrency",
      "navigator.deviceMemory", "window.outerWidth", "window.outerHeight", "window.innerWidth",
      "window.innerHeight"]),
    ("BEHAVIOURAL — are they watching how input arrives?",
     ["listener:mousemove", "listener:pointermove", "listener:mousedown", "listener:mouseup",
      "listener:click", "listener:keydown", "listener:keyup", "listener:keypress",
      "listener:wheel", "listener:scroll", "listener:touchstart", "listener:devicemotion"]),
    ("DEVICE FINGERPRINTING",
     ["canvas.toDataURL()", "canvas.toBlob()", "canvas.getImageData()", "webgl.getParameter()",
      "webgl2.getParameter()", "AudioContext()", "OfflineAudioContext()", "RTCPeerConnection()",
      "OffscreenCanvas()", "speechSynthesis.getVoices()", "mediaDevices.enumerateDevices()",
      "navigator.getBattery()", "Intl.resolvedOptions()", "screen.", "window.devicePixelRatio"]),
]

ASSET = re.compile(r"\.(js|css|png|jpg|jpeg|svg|woff2?|ico|gif|webp|map)(\?|$)", re.I)


def owner_of(frame: str) -> str:
    """Who made this call? TOP FRAME ONLY — see the module docstring."""
    if "googletagmanager" in frame or "google-analytics" in frame: return "Google Analytics/GTM"
    if "zdassets" in frame or "zendesk" in frame:                  return "Zendesk"
    if "framework-" in frame:                                      return "React internals"
    if "webpack-" in frame:                                        return "BIA webpack runtime"
    if "_app-" in frame:                                           return "*** BIA APP CODE ***"
    m = re.search(r"chunks/([\w.-]+)\.js", frame)
    if m:                                                          return f"BIA chunk {m.group(1)}"
    if "betinasia" in frame:                                       return "BIA (other)"
    return "unknown"


def owners_for(entry: dict) -> list[str]:
    return sorted({owner_of(k.split("<-")[0].strip()) for k in (entry.get("stacks") or {})}) or ["<no stack>"]


def print_entry(name: str, e: dict, secs: float) -> None:
    rate = f"{e['count']/secs:.2f}/s" if secs > 0 else "?"
    span = ""
    if e.get("first") is not None:
        span = f"  first {e['first']:.1f}s last {e['last']:.1f}s"
        if e["count"] > 5 and (e["last"] - e["first"]) > secs * 0.5:
            span += "  [POLLED THROUGHOUT]"
        elif e["count"] > 2 and e["last"] < 10:
            span += "  [load-time only]"
    print(f"  {e['count']:7}x ({rate:>8})  {name}{span}")
    for owner in owners_for(e):
        print(f"              by {owner}")
    for frame, n in sorted((e.get("stacks") or {}).items(), key=lambda kv: -kv[1])[:2]:
        print(f"                 {frame.split('<-')[0].strip()[:120]}")


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=DEFAULT_URL)
    ap.add_argument("--secs", type=float, default=120.0)
    ap.add_argument("--profile-copy", action="store_true", help="run against a throwaway COPY of the profile")
    ap.add_argument("--json", default="", help="also write the raw report here")
    ap.add_argument("--net", action="store_true", default=True, help="report non-asset traffic too")
    a = ap.parse_args()

    from playwright.async_api import async_playwright

    profile, tmp = PROFILE, None
    if a.profile_copy:
        tmp = Path(tempfile.mkdtemp(prefix="bia_recon_"))
        profile = tmp / "profile"
        print(f"[RECON] copying profile -> {profile}")
        shutil.copytree(PROFILE, profile)

    posts: list[tuple[str, str, str]] = []
    hosts: dict[str, int] = {}

    print(f"[RECON] observe-only: {a.url} for {a.secs:.0f}s. THE BOT will not click anything.")
    print("[RECON] DRIVE IT YOURSELF while this runs. The betting flow cannot be reached by URL — it is a")
    print("        state you click into — so open a betslip, set a stake, and go as far as you are willing")
    print("        (stopping short of Place is fine; the checks that matter load with the panel). Every")
    print("        tab is instrumented, so whatever the flow touches is recorded.")
    async with async_playwright() as pw:
        ctx = await pw.chromium.launch_persistent_context(str(profile), headless=False)
        await ctx.add_init_script(PROBE_JS)

        def on_request(req):
            try:
                from urllib.parse import urlparse
                h = urlparse(req.url).netloc
                hosts[h] = hosts.get(h, 0) + 1
                if req.method == "POST" and not ASSET.search(req.url):
                    posts.append((h, urlparse(req.url).path, (req.post_data or "")[:300]))
            except Exception:
                pass

        ctx.on("request", on_request)
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await page.goto(a.url, wait_until="domcontentloaded")
        await asyncio.sleep(a.secs)
        # READ BACK FROM EVERY TAB, merged. Driving the betting flow by hand can open a new tab, and
        # reading only the first page would report "they check nothing" about a tab nobody looked at.
        log = await read_probe(ctx)
        await ctx.close()
    if tmp:
        shutil.rmtree(tmp, ignore_errors=True)

    report(log, a.secs, posts if a.net else None)

    if a.json:
        Path(a.json).write_text(json.dumps(
            {"log": log, "posts": posts, "hosts": hosts, "secs": a.secs, "url": a.url},
            indent=2), encoding="utf-8")
        print(f"\n[RECON] raw report -> {a.json}")
    return 0


def report(log: dict, secs: float, posts: "list | None" = None) -> None:
    """Print the whole read-out. Shared so betinasia_recon.py can arm the same probe during a
    network capture and produce one combined session instead of two that cannot run at once —
    both own a browser on .betinasia_profile, so they were mutually exclusive."""
    if not log:
        print("[RECON] EMPTY REPORT — the probe never armed, or the page replaced the document after "
              "init. Nothing below can be trusted as 'they do not check this'.")

    shown: set[str] = set()
    for heading, prefixes in SECTIONS:
        print(f"\n{'=' * 78}\n### {heading}")
        hits = {k: v for k, v in log.items() if any(k == p or k.startswith(p) for p in prefixes)}
        if not hits:
            print("  NOT CHECKED — nothing in this group was touched.")
            continue
        for k, v in sorted(hits.items(), key=lambda kv: -kv[1]["count"]):
            shown.add(k)
            print_entry(k, v, secs)

    rest = {k: v for k, v in log.items() if k not in shown}
    if rest:
        print(f"\n{'=' * 78}\n### EVERYTHING ELSE (stacks included — do not eyeball counts)")
        for k, v in sorted(rest.items(), key=lambda kv: -kv[1]["count"]):
            print_entry(k, v, secs)

    # ── who is doing the looking ──────────────────────────────────────────────
    agg: dict[str, float] = {}
    for k, v in log.items():
        os_ = owners_for(v)
        for o in os_:
            agg[o] = agg.get(o, 0) + v["count"] / len(os_)
    print(f"\n{'=' * 78}\n### ACCESSES BY SCRIPT (top frame)")
    for o, c in sorted(agg.items(), key=lambda kv: -kv[1]):
        print(f"  {c:9.0f}  {o}")

    if posts is not None:
        print(f"\n{'=' * 78}\n### WHAT LEFT THE BROWSER (non-asset POSTs)")
        seen = set()
        for h, p, body in posts:
            if (h, p) in seen:
                continue
            seen.add((h, p))
            print(f"  {h}{p}\n      {body[:220]}")
        if not posts:
            print("  (none)")

    # ── design implications, derived from what was actually observed ──────────
    def touched(*prefixes) -> int:
        return sum(v["count"] for k, v in log.items()
                   if any(k == p or k.startswith(p) for p in prefixes))

    print(f"\n{'=' * 78}\n### WHAT THIS MEANS FOR THE BOT")
    vis = touched("document.visibilityState", "document.hidden", "document.hasFocus()")
    if vis:
        print(f"  [MUST] visibility is read {vis}x -> NEVER act in a hidden tab. Keep "
              f"BIA_SLIP_FOCUS_TAB=1 and bring_to_front() before every click.")
    else:
        print("  [ok]   visibility never read on this page -> background clicking is not seen HERE.")
    mm = touched("listener:mousemove", "listener:pointermove")
    app_mm = any("*** BIA APP CODE ***" in owners_for(v)
                 for k, v in log.items() if k.startswith(("listener:mousemove", "listener:pointermove")))
    if mm and app_mm:
        print("  [MUST] the SITE'S OWN code listens for mousemove -> human cursor paths are load-bearing.")
    elif mm:
        print("  [nice] mousemove listeners are library-only (React/Zendesk event delegation) -> human "
              "cursor paths are cheap insurance, not a requirement.")
    else:
        print("  [ok]   nobody listens for mousemove.")
    wd = touched("navigator.webdriver", "event.isTrusted", "window.chrome")
    print(f"  [{'MUST' if wd else 'ok'}]   automation checks (webdriver/isTrusted/chrome): {wd} read(s)"
          + ("  -> stealth patching needed." if wd else "  -> no stealth patching needed."))
    fp = touched("canvas.", "webgl", "AudioContext", "OfflineAudioContext", "RTCPeerConnection",
                 "OffscreenCanvas", "speechSynthesis", "mediaDevices", "navigator.getBattery")
    print(f"  [{'MUST' if fp else 'ok'}]   device fingerprinting: {fp} call(s)"
          + ("  -> the profile must stay on ONE machine." if fp else "  -> no device fingerprint taken."))
    print("  [ALWAYS] none of the above can see SERVER-SIDE analysis. /web/metrics/ already reports "
          "betslip.duration, betslip.source and context.tabId, so slip-hold time and tab count are "
          "recorded regardless of what this run found.")


async def read_probe(ctx) -> dict:
    """Merge `window.__hvDetect` from every tab in a context. Shared with betinasia_recon.py."""
    log: dict = {}
    for pg in list(ctx.pages):
        try:
            if pg.is_closed():
                continue
            got = await pg.evaluate("() => window.__hvDetect || {}")
        except Exception:
            continue
        for k, v in (got or {}).items():
            cur = log.get(k)
            if cur is None:
                log[k] = v
                continue
            cur["count"] = cur.get("count", 0) + v.get("count", 0)
            for f, pick in (("first", min), ("last", max)):
                vals = [x for x in (cur.get(f), v.get(f)) if x is not None]
                if vals:
                    cur[f] = pick(vals)
            cur.setdefault("stacks", {}).update(v.get("stacks") or {})
    return log


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
