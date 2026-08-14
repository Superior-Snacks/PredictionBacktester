"""A standing canary: alert if the venue ever STARTS checking for automation.

`detect_recon.py` answers "are they checking?" for the 180 seconds it runs. This answers it continuously,
inside the bot's own browser, for the life of the session — because the useful version of that question is
not "were they checking on 2026-08-14" but "have they started".

WHAT IS WATCHED, AND WHY ONLY THESE. Every API here was measured at EXACTLY ZERO reads across a 180s
probe of the live site. That is what makes them usable as a canary: there is no baseline traffic to
tune out, so a single read is signal rather than noise. Deliberately excluded are the things the site
legitimately reads all the time — `visibilityState` (205x), `userAgent` (120x), `languages` (861x) —
which would drown the alert in ordinary behaviour.

    navigator.webdriver          the canonical automation check
    event.isTrusted              the canonical synthetic-event check
    navigator.plugins            classic headless tell
    window.chrome                classic headless tell
    window.outerWidth/Height     headless: outer == 0, or outer == inner
    canvas / WebGL / audio       device fingerprinting
    RTCPeerConnection            IP/network fingerprinting
    permissions.query            Notification-permission automation tell

⚠️ THE TRADE-OFF, STATED PLAINLY. Wrapping natives changes what `Function.prototype.toString` returns for
them, which is itself a tamper check a detector could run. That is mitigated here — the wrappers report
the ORIGINAL native source — but the mitigation is not perfect, and unlike the one-shot probe this runs
for the whole session. `BIA_CANARY=0` turns it off. The judgement encoded in the default (on) is that
knowing the day they start looking is worth more than the residual risk of looking slightly odd to a
check nobody currently runs.

Storage is a NON-ENUMERABLE window property, so `Object.keys(window)` does not show it. Read by polling
rather than by a Playwright binding: `expose_binding` installs a visibly non-native function on the page,
which is a louder artifact than a hidden property nobody enumerates.
"""
from __future__ import annotations

import os
import time

CANARY_KEY = "__hvq"

CANARY_JS = r"""
(() => {
  if (window.__hvq) return;
  const hits = {};
  // Non-enumerable: absent from Object.keys(window) and from a for..in sweep.
  Object.defineProperty(window, "__hvq", { value: hits, enumerable: false, configurable: false });

  const note = (name) => {
    const e = hits[name] || (hits[name] = { n: 0, at: Date.now(), stack: "" });
    e.n++;
    if (!e.stack) {
      e.stack = (new Error().stack || "").split("\n").slice(3, 5).map(s => s.trim()).join(" <- ");
    }
  };

  const nativeToString = Function.prototype.toString;
  const originals = new WeakMap();
  const patched = function () { return nativeToString.call(originals.get(this) || this); };
  originals.set(patched, nativeToString);
  Function.prototype.toString = patched;

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
  const wrapGet = (obj, prop, name) => {
    try {
      if (!obj) return;
      let d = Object.getOwnPropertyDescriptor(obj, prop), p = obj;
      while (!d && (p = Object.getPrototypeOf(p))) d = Object.getOwnPropertyDescriptor(p, prop);
      if (!d) return;
      if (d.get) {
        const orig = d.get;
        const get = function () { note(name); return orig.call(this); };
        originals.set(get, orig);
        Object.defineProperty(obj, prop, { get, configurable: true });
      } else if ("value" in d && d.configurable) {
        const v = d.value;
        Object.defineProperty(obj, prop, { get() { note(name); return v; }, configurable: true });
      }
    } catch (e) {}
  };

  wrapGet(Navigator.prototype, "webdriver", "navigator.webdriver");
  wrapGet(Navigator.prototype, "plugins",   "navigator.plugins");
  wrapGet(Event.prototype, "isTrusted",     "event.isTrusted");
  wrapGet(window, "chrome",                 "window.chrome");
  wrapGet(window, "outerWidth",             "window.outerWidth");
  wrapGet(window, "outerHeight",            "window.outerHeight");
  wrapFn(HTMLCanvasElement.prototype, "toDataURL", "canvas.toDataURL");
  wrapFn(CanvasRenderingContext2D.prototype, "getImageData", "canvas.getImageData");
  if (window.WebGLRenderingContext)  wrapFn(WebGLRenderingContext.prototype,  "getParameter", "webgl.getParameter");
  if (window.WebGL2RenderingContext) wrapFn(WebGL2RenderingContext.prototype, "getParameter", "webgl2.getParameter");
  wrapFn(window, "AudioContext", "AudioContext");
  wrapFn(window, "OfflineAudioContext", "OfflineAudioContext");
  wrapFn(window, "RTCPeerConnection", "RTCPeerConnection");
  try { wrapFn(navigator.permissions, "query", "permissions.query"); } catch (e) {}
  try { wrapGet(Notification, "permission", "Notification.permission"); } catch (e) {}
})();
"""


class Canary:
    """Collects hits across every tab and reports the first sighting of each API, loudly and once."""

    def __init__(self, log=print) -> None:
        self.enabled = os.environ.get("BIA_CANARY", "1") != "0"
        self.hits: dict[str, dict] = {}      # api -> {n, first_seen, stack, tabs}
        self.polls = 0
        self.started = time.time()
        self._log = log

    async def poll(self, pages) -> list[str]:
        """Read every tab's canary. Returns the APIs newly seen on THIS poll."""
        fresh: list[str] = []
        for page in pages:
            try:
                if page.is_closed():
                    continue
                got = await page.evaluate(f"() => window.{CANARY_KEY} || {{}}")
            except Exception:
                continue                      # navigating / closing — try again next poll
            for api, e in (got or {}).items():
                cur = self.hits.get(api)
                if cur is None:
                    self.hits[api] = {"n": e.get("n", 0), "first_seen": time.time(),
                                      "stack": e.get("stack", ""), "tabs": 1}
                    fresh.append(api)
                else:
                    cur["n"] = max(cur["n"], e.get("n", 0))
        self.polls += 1
        for api in fresh:
            h = self.hits[api]
            self._log("=" * 78)
            self._log(f"*** DETECTION CANARY *** the site just read {api} ({h['n']}x)")
            self._log(f"    {h['stack'][:200]}")
            self._log("    This API read ZERO times in the 2026-08-14 baseline. Something changed —")
            self._log("    re-run detect_recon.py before trusting the current anti-detection posture.")
            self._log("=" * 78)
        return fresh

    def report(self) -> dict:
        return {
            "enabled": self.enabled,
            "polls": self.polls,
            "watching_since_sec": round(time.time() - self.started, 1),
            "tripped": sorted(self.hits),
            "detail": {k: {"count": v["n"], "stack": v["stack"][:200]} for k, v in self.hits.items()},
            "note": ("every watched API measured ZERO reads on 2026-08-14, so any entry in `tripped` "
                     "means the site's behaviour changed"),
        }
