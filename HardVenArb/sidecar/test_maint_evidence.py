"""The maintenance latch keeps evidence (2026-11-01).

A 503 at ~09:01 and another at the 09:15 re-check held the morning block until 12:00, and the log could not
say what Pinnacle had actually answered. This pins: a 5xx's response is written to the evidence folder with
its gist (HTML <title> / JSON message) in the log line; a re-check that still fails is kept too; a caller
without a response still latches; the per-episode budget holds; a 200 still releases; and when the latch
closes an OPEN site, every tab is screenshotted BEFORE the browser stops, and a hung tab cannot hold the
close. Offline: no venue, no Discord, no browser - all fakes.

    python test_maint_evidence.py          # exit 0 = all pass
"""
from __future__ import annotations
import asyncio
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path

os.environ.pop("DISCORD_WEBHOOK_URL", None)           # nothing here may post
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import maintenance  # noqa: E402
from lifecycle import PinnacleLifecycle  # noqa: E402

PASS = FAIL = 0


def check(name, ok, got=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}" + (f"  (got {got})" if got != "" else ""))


class Resp:
    def __init__(self, code, text="", headers=None, url="https://guest.api.arcadia.pinnacle.com/0.1/sports/33/leagues"):
        self.status_code, self.text, self.headers, self.url = code, text, headers or {}, url

    def json(self):
        import json
        return json.loads(self.text)


def reset(folder):
    maintenance._since = None
    maintenance._on_enter.clear()
    maintenance._on_exit.clear()
    maintenance._notify = None
    maintenance._ev_count = 0
    maintenance.SHOTS = Path(folder)


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="maint_ev_")
    reset(tmp)

    html = ("<html><head><title>Pinnacle - Scheduled Maintenance</title></head>"
            "<body><h1>We'll be back soon</h1></body></html>")
    maintenance.note_status(503, "slate /sports/33/leagues", Resp(503, html, {"server": "cloudflare", "retry-after": "600"}))
    files = sorted(Path(tmp).glob("*_http503.txt"))
    check("a 503 latches", maintenance.active())
    check("...and its response is kept", len(files) == 1, [f.name for f in files])
    body = files[0].read_text(encoding="utf-8") if files else ""
    check("...with the page title as its gist", "gist: Pinnacle - Scheduled Maintenance" in body, body[:200])
    check("...headers and body included", "retry-after: 600" in body and "We'll be back soon" in body)
    check("...and /health can point at it", maintenance.status()["last_evidence"] == str(files[0]) if files else False)

    js = '{"title": "Service Unavailable", "status": 503}'
    maintenance.note_status(503, "re-check window open", Resp(503, js))
    files = sorted(Path(tmp).glob("*.txt"))
    check("a re-check that still fails is kept too, in its own file",
          len(files) == 2 and sum("Service Unavailable" in f.read_text(encoding="utf-8") for f in files) == 1
          and sum("Scheduled Maintenance" in f.read_text(encoding="utf-8") for f in files) == 1,
          [f.name for f in files])
    check("JSON body -> its title as the gist", maintenance._gist(Resp(503, js)) == "Service Unavailable",
          maintenance._gist(Resp(503, js)))
    check("plain text body -> its first words", maintenance._gist(Resp(502, "  upstream \n connect error ")) == "upstream connect error")

    maintenance.note_status(200, "slate /sports/33/leagues", Resp(200, "[]"))
    check("a 200 still releases", not maintenance.active())

    reset(tmp)
    maintenance.note_status(504, "GUEST GET /x")              # an old caller: no response object
    check("no response -> still latches, nothing written", maintenance.active() and maintenance._ev_count == 0)
    maintenance._since = None

    tmp2 = tempfile.mkdtemp(prefix="maint_ev_")
    reset(tmp2)
    for _ in range(maintenance.EVIDENCE_MAX + 5):
        maintenance.note_status(503, "re-check", Resp(503, "x"))
    check("the per-episode budget holds", maintenance._ev_count == maintenance.EVIDENCE_MAX
          and len(list(Path(tmp2).glob("*.txt"))) == maintenance.EVIDENCE_MAX, len(list(Path(tmp2).glob("*.txt"))))
    maintenance._since = None

    # ── the screenshot comes BEFORE the browser stops ─────────────────────────────────────────
    order = []

    class FakePage:
        url = "https://www.pinnacle.bet/en/tennis/matchups/"
        def is_closed(self): return False
        async def screenshot(self, path, full_page=False):
            order.append("screenshot"); Path(path).write_bytes(b"\x89PNG fake")
        async def title(self): return "Pinnacle - Scheduled Maintenance"
        async def evaluate(self, js): return "We'll be back soon"

    class FakeBrowser:
        async def start(self): order.append("start")
        async def stop(self): order.append("stop")
        def open_pages(self): return [FakePage()]

    tmp3 = tempfile.mkdtemp(prefix="maint_ev_")
    reset(tmp3)

    async def scenario():
        lc = PinnacleLifecycle(FakeBrowser(), [33])
        lc._windows = [(datetime(2026, 11, 1, 9, 0), datetime(2026, 11, 1, 14, 0), 3)]
        await lc.tick(now=datetime(2026, 11, 1, 9, 30))       # inside -> open
        maintenance.on_enter(lambda: lc.maintenance_close())
        maintenance.note_status(503, "GUEST GET /sports/33/leagues", Resp(503, html))
        await lc.tick(now=datetime(2026, 11, 1, 9, 31))       # the maintenance close
        return lc
    lc = asyncio.run(scenario())
    check("latch while OPEN closes the site", not lc._open and order[-1] == "stop", order)
    check("...screenshot taken BEFORE the browser stops", order == ["start", "screenshot", "stop"], order)
    pngs, txts = list(Path(tmp3).glob("*_tab0.png")), list(Path(tmp3).glob("*_tab0.txt"))
    check("...the tab's screenshot and text are kept", len(pngs) == 1 and len(txts) == 1, [p.name for p in Path(tmp3).iterdir()])
    check("...text file has url, title, visible text",
          bool(txts) and all(s in txts[0].read_text(encoding="utf-8")
                             for s in ("url: https://www.pinnacle.bet", "Scheduled Maintenance", "We'll be back soon")))
    maintenance._since = None

    # ── a 200 mid-window REOPENS the current window (2026-11-01: back at 12:00, dark until 14:13) ──────
    starts = []

    class ReBrowser(FakeBrowser):
        async def start(self): starts.append("start")

    tmp4 = tempfile.mkdtemp(prefix="maint_ev_")
    reset(tmp4)

    async def reopen(from_thread: bool):
        lc = PinnacleLifecycle(ReBrowser(), [33])
        lc._loop = asyncio.get_running_loop()                  # run() captures this in production
        lc._windows = [(datetime(2026, 11, 1, 9, 0), datetime(2026, 11, 1, 14, 0), 3)]
        maintenance.on_enter(lambda: lc.maintenance_close())
        maintenance.on_exit(lambda: lc.maintenance_release())
        await lc.tick(now=datetime(2026, 11, 1, 9, 30))       # open
        maintenance.note_status(503, "slate /sports/33/leagues", Resp(503, html))
        await lc.tick(now=datetime(2026, 11, 1, 9, 31))       # maintenance close, held until 14:00
        held = (lc._open, lc._override)
        if from_thread:                                        # the slate fetch runs in a worker thread
            await asyncio.to_thread(maintenance.note_status, 200, "slate /sports/33/leagues", Resp(200, "[]"))
            await asyncio.sleep(0.05)                          # let the hop to the loop run
        else:
            maintenance.note_status(200, "GUEST GET /x", Resp(200, "[]"))
        await lc.tick(now=datetime(2026, 11, 1, 12, 0))       # the woken re-tick
        return held, lc._open, lc._override

    for from_thread in (False, True):
        starts.clear()
        maintenance._on_enter.clear(); maintenance._on_exit.clear(); maintenance._since = None
        held, is_open, ov = asyncio.run(reopen(from_thread))
        how = "from the slate worker thread" if from_thread else "on the loop"
        check(f"a 200 {how}: the maintenance close was holding first", held == (False, "maintenance"), held)
        check(f"...lifts it and the current window opens again", is_open and ov is None and len(starts) == 2,
              (is_open, ov, starts))

    src = Path(__file__).with_name("pinnacle_adapter.py").read_text(encoding="utf-8")
    check("the adapter wires the release on latch exit",
          "maintenance.on_exit(lambda: self._lifecycle and self._lifecycle.maintenance_release())" in src)

    class HungPage(FakePage):
        async def screenshot(self, path, full_page=False):
            await asyncio.sleep(30)

    got = asyncio.run(maintenance.capture_pages([HungPage()], timeout=0.2))
    check("a hung tab cannot hold the close (gives up, no raise)", got == [], got)
    check("no tabs (site dark) -> nothing, no raise", asyncio.run(maintenance.capture_pages([])) == [])

    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
