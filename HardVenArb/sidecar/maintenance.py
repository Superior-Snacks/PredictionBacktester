"""Pinnacle maintenance latch: notice a venue outage from the first 5xx, hold every non-essential loop, and
probe on a slow cadence until the venue answers again.

WHY THIS EXISTS
---------------
2026-09-14: Pinnacle went into maintenance some time after 11:11. Nothing in the sidecar knew. The
12:01 pairing run got HTTP 503 from the guest API and wrote the pair files empty (fixed separately - the
pairers now refuse). The 12:15 window opened the browser onto a maintenance page, and for the next hour:
the login watcher flapped `in -> unknown -> in` nine times (the page has a nav bar, no account button),
the session-refresh loop reloaded the maintenance page every 15 minutes to "re-mint" a session that could
not be minted, the organic layer nav-clicked around it and the drift watchdog kept steering it "back to
the trading sport", and the lifecycle re-planned the day off an empty slate every ten minutes. None of it
was harmful - guest-level page loads against a 503 - and none of it was smart. An operator reading the
log could not tell "maintenance" from "everything is subtly broken".

WHAT COUNTS AS MAINTENANCE
--------------------------
A 502/503/504 from the guest API, from any caller. That is the one signal the log actually contained, it
is unambiguous, and it needs no assumption about what the maintenance page looks like. Network errors do
NOT count: a local blip is not the venue being down, and latching on one would hold the bot for nothing.
Exit is symmetric: any guest call answering 200 clears it, including the probe below.

WHAT HOLDS WHILE LATCHED
------------------------
  session refresh   no reload; there is no session to re-mint
  organic activity  paused via the session's gate; nothing to browse
  board-drift fix   no navigation; the "drift" IS the maintenance page
  lifecycle plan    keeps the last windows; does not re-author off an empty slate
  pairing run       skipped outright; the pairers would refuse anyway, this saves the attempt
  odds WS           not touched - it is already down (no session, no connect), and reconnect is its own
                    state machine with its own give-up guard

THE RE-CHECK
------------
No probe loop. On entry the lifecycle CLOSES the site - the browser comes down exactly as it does for a
scheduled dark window - and stays dark until the next block boundary: the next window's open, or a
scheduled replan hour. At that boundary the lifecycle makes ONE guest GET of /sports/33/leagues (the exact
call that failed at 12:01, and the cheapest thing the venue serves). 200 clears the latch and the window
opens normally; 5xx keeps it, and the site stays closed until the boundary after that. Between boundaries
there is no contact with the venue at all. Discord gets one line on the way in (with the time of the next
re-check) and one on the way out (with the duration), so an outage is a fact in the channel rather than
something inferred from silence.

Module-level state on purpose: the adapter, the session, the lifecycle, the pairing scheduler and
schedule.py all see the same latch with no plumbing through five constructors.
"""
from __future__ import annotations
import asyncio, os, time
import httpx

try:
    from notify import Notifier
except Exception:                                   # pragma: no cover - notify is optional
    Notifier = None                                 # type: ignore

GUEST_BASE = os.environ.get("PINNACLE_GUEST_BASE", "https://guest.api.arcadia.pinnacle.com/0.1")
GUEST_KEY  = os.environ.get("PINNACLE_API_KEY", "CmX2KcMrXuFmNg6YFbmTxE0y9CIrOi0R")
PROBE_PATH = "/sports/33/leagues"
MAINT_CODES = {502, 503, 504}

_since: float | None = None
_entered_by = ""
_last_code: int | None = None
_probes = 0
_episodes = 0
_on_enter: list = []
_on_exit: list = []
_notify = Notifier() if Notifier else None
_loop: asyncio.AbstractEventLoop | None = None      # the main loop, captured by probe_loop()


def _post(message: str) -> None:
    """Discord, from any thread. note_status() is called from the lifecycle's slate fetch, which runs in a
    worker thread via to_thread - there is no running loop there, and Notifier.send_bg silently drops the
    message in that case. Hop to the main loop when we are not on it."""
    if not (_notify and _notify.enabled):
        return
    try:
        asyncio.get_running_loop()
        _notify.send_bg(message)                      # already on a loop thread
    except RuntimeError:
        if _loop is not None:
            _loop.call_soon_threadsafe(_notify.send_bg, message)


def active() -> bool:
    return _since is not None


def since() -> float | None:
    return _since


def minutes() -> float:
    return (time.time() - _since) / 60 if _since else 0.0


def status() -> dict:
    """For /health. Says whether the venue is down, since when, and how many probes have failed."""
    return {"active": active(), "since": _since, "minutes": round(minutes(), 1),
            "entered_by": _entered_by, "last_code": _last_code, "rechecks": _probes, "episodes": _episodes}


def on_enter(cb) -> None:
    """Register a zero-arg callable to run when the latch sets (e.g. pause organic activity)."""
    _on_enter.append(cb)


def on_exit(cb) -> None:
    _on_exit.append(cb)


def note_status(code: int | None, source: str) -> None:
    """Every guest GET reports here. 5xx enters maintenance; 200 exits it. Anything else is ignored -
    a 404 on one path is not the venue being down, and a network error is not either."""
    global _last_code
    if code is None:
        return
    _last_code = code
    if code in MAINT_CODES:
        _enter(code, source)
    elif code == 200 and active():
        _exit(source)


def _enter(code: int, source: str) -> None:
    global _since, _entered_by, _probes, _episodes
    if _since is not None:
        return
    _since = time.time()
    _entered_by = source
    _probes = 0
    _episodes += 1
    print(f"[PINNACLE MAINT] *** venue answered HTTP {code} on {source} - treating as MAINTENANCE. Closing the "
          f"site; re-check at the next block boundary. ***", flush=True)
    for cb in _on_enter:
        try:
            cb()
        except Exception as ex:
            print(f"[PINNACLE MAINT] on_enter hook failed: {type(ex).__name__}: {ex}")
    _post(f"🛠️ **Pinnacle MAINTENANCE** detected (HTTP {code} on `{source}`). Closing the site; "
          f"re-check at the next block.")


def _exit(source: str) -> None:
    global _since
    if _since is None:
        return
    mins = minutes()
    _since = None
    print(f"[PINNACLE MAINT] venue answered 200 on {source} after {mins:.0f} min ({_probes} re-check(s)) - "
          f"maintenance OVER. Releasing holds.", flush=True)
    for cb in _on_exit:
        try:
            cb()
        except Exception as ex:
            print(f"[PINNACLE MAINT] on_exit hook failed: {type(ex).__name__}: {ex}")
    _post(f"✅ **Pinnacle back** after {mins:.0f} min of maintenance ({_probes} re-check(s)).")


def bind_loop() -> None:
    """Capture the main loop so _post can hop to it from a worker thread. Call once from the adapter."""
    global _loop
    _loop = asyncio.get_running_loop()


_client: httpx.AsyncClient | None = None


async def recheck(source: str) -> bool:
    """ONE guest GET at a block boundary. Reports through note_status, so 200 releases the latch and 5xx
    keeps it. Returns True when the venue answered 200. Never raises."""
    global _client, _probes
    if _client is None:
        _client = httpx.AsyncClient(
            headers={"accept": "application/json", "x-api-key": GUEST_KEY,
                     "origin": "https://www.pinnacle.bet", "referer": "https://www.pinnacle.bet/",
                     "user-agent": "Mozilla/5.0"},
            timeout=20.0, follow_redirects=True)
    _probes += 1
    try:
        r = await _client.get(GUEST_BASE + PROBE_PATH)
        code = r.status_code
    except Exception as ex:
        print(f"[PINNACLE MAINT] re-check ({source}): {type(ex).__name__}: {ex} - treating as still down.")
        return False
    if code != 200:
        print(f"[PINNACLE MAINT] re-check ({source}): HTTP {code} after {minutes():.0f} min - still down.")
    note_status(code, f"re-check {source}")
    return code == 200
