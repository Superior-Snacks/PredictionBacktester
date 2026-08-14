"""The standing detection canary. Run: python test_canary.py

The canary's job is to be SILENT until the venue changes behaviour, then be impossible to miss. Both
halves are testable without a browser: the JS is checked for shape, and the Python collector is driven
with fake pages that return canned hit dictionaries.
"""
import asyncio
import os
import re
import sys

from canary import CANARY_JS, CANARY_KEY, Canary

bad = 0
lines: list[str] = []


def check(label, ok):
    global bad
    print(f"  {'PASS' if ok else 'FAIL'}  {label}")
    if not ok:
        bad += 1


class FakePage:
    def __init__(self, hits, closed=False, raises=False):
        self._hits, self._closed, self._raises = hits, closed, raises

    def is_closed(self):
        return self._closed

    async def evaluate(self, _js):
        if self._raises:
            raise RuntimeError("Execution context was destroyed")
        return self._hits


print("[1] the injected JS watches the right things, and only those")
for must in ["navigator.webdriver", "event.isTrusted", "navigator.plugins", "window.chrome",
             "canvas.toDataURL", "webgl.getParameter", "AudioContext", "RTCPeerConnection",
             "window.outerWidth"]:
    check(f"watches {must}", must in CANARY_JS)
# The noisy ones must NOT be watched, or the alert drowns in ordinary behaviour.
for must_not in ["visibilityState", "navigator.languages", "performance"]:
    check(f"does NOT watch {must_not} (measured as high-traffic)", must_not not in CANARY_JS)
check("storage is non-enumerable", "enumerable: false" in CANARY_JS)
check("preserves native toString", "nativeToString.call" in CANARY_JS)
check("braces balanced", CANARY_JS.count("{") == CANARY_JS.count("}"))
check("parens balanced", CANARY_JS.count("(") == CANARY_JS.count(")"))
check(f"reads back from window.{CANARY_KEY}", f'window.__hvq' in CANARY_JS)

print("\n[2] silent when nothing is read")
c = Canary(log=lines.append)
fresh = asyncio.run(c.poll([FakePage({}), FakePage({})]))
check("no hits -> no alert", fresh == [] and not lines)
check("report says untripped", c.report()["tripped"] == [])

print("\n[3] one read is enough, and it shouts")
lines.clear()
c = Canary(log=lines.append)
fresh = asyncio.run(c.poll([FakePage({"navigator.webdriver": {"n": 1, "stack": "at detect (evil.js:1)"}})]))
check("alerts on the first read", fresh == ["navigator.webdriver"])
out = "\n".join(lines)
check("alert is unmissable", "*** DETECTION CANARY ***" in out)
check("alert names the API", "navigator.webdriver" in out)
check("alert carries the calling stack", "evil.js" in out)
check("alert says what to do", "detect_recon.py" in out)
check("report lists it", c.report()["tripped"] == ["navigator.webdriver"])

print("\n[4] alerts ONCE, not every poll")
lines.clear()
for _ in range(5):
    fresh = asyncio.run(c.poll([FakePage({"navigator.webdriver": {"n": 99, "stack": "x"}})]))
check("no repeat alerts", fresh == [] and "*** DETECTION CANARY ***" not in "\n".join(lines))
check("but the count still climbs", c.report()["detail"]["navigator.webdriver"]["count"] == 99)

print("\n[5] a NEW api trips separately")
lines.clear()
fresh = asyncio.run(c.poll([FakePage({"navigator.webdriver": {"n": 99, "stack": "x"},
                                      "canvas.toDataURL": {"n": 2, "stack": "at fp (evil.js:9)"}})]))
check("second api alerts", fresh == ["canvas.toDataURL"])
check("both now listed", c.report()["tripped"] == ["canvas.toDataURL", "navigator.webdriver"])

print("\n[6] broken tabs cannot break the canary")
lines.clear()
c2 = Canary(log=lines.append)
fresh = asyncio.run(c2.poll([FakePage({}, closed=True),
                             FakePage({}, raises=True),
                             FakePage({"event.isTrusted": {"n": 1, "stack": "s"}})]))
check("a closed tab is skipped, a raising tab is swallowed, the live one still reports",
      fresh == ["event.isTrusted"])
check("polls counted", c2.report()["polls"] == 1)

print("\n[7] the off switch works")
os.environ["BIA_CANARY"] = "0"
check("BIA_CANARY=0 disables", Canary().enabled is False)
os.environ["BIA_CANARY"] = "1"
check("default is on", Canary().enabled is True)

print("\nALL PASS" if bad == 0 else f"\nFAILURES: {bad}")
sys.exit(1 if bad else 0)
