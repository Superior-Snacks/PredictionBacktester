"""Single-keypress control for the sidecar window.

WHY A KEYPRESS AND NOT JUST THE HTTP ENDPOINT. The endpoint is the real interface — scriptable, remote,
reachable from Discord. But the moment this exists for is not scriptable: the operator is sitting in front of
the browser, has just noticed the bot about to do something, and wants it to STOP before it does. Alt-tabbing
to another terminal to compose a curl is several seconds of exactly the interference being prevented. One key
in the window that is already printing the log is the whole point.

RUNS ON A THREAD, NOT THE EVENT LOOP. `asyncio.add_reader` does not support stdin on Windows (Proactor), and
this sidecar's primary home IS Windows — headed Chrome, a DPAPI-bound profile. So a daemon thread does the
blocking read and hands work back with `run_coroutine_threadsafe`. Daemon so it can never hold up shutdown.

DEGRADES SILENTLY AND ON PURPOSE. Under `uvicorn` in a normal terminal stdin is a console and this works.
Under a service manager, a pipe, or nohup, there is no console: the listener notices, says so once, and stops.
A sidecar must never fail to start because nobody was there to press a key.
"""
from __future__ import annotations

import asyncio
import os
import sys
import threading
from typing import Awaitable, Callable, Optional


def _has_console() -> bool:
    try:
        return bool(sys.stdin) and sys.stdin.isatty()
    except Exception:
        return False


class HotkeyListener:
    """Maps single characters to coroutines. `handlers` is {key: (description, coro_factory)}."""

    def __init__(self, loop: asyncio.AbstractEventLoop,
                 handlers: dict[str, tuple[str, Callable[[], Awaitable]]]):
        self._loop = loop
        self._handlers = {k.lower(): v for k, v in handlers.items()}
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

    # ── platform reads ────────────────────────────────────────────────────────
    def _read_windows(self) -> Optional[str]:
        import msvcrt
        if not msvcrt.kbhit():
            return None
        ch = msvcrt.getwch()
        # Arrow/function keys arrive as a two-character sequence; swallow the second half so it is not
        # mistaken for a command (an Up-arrow would otherwise read as 'H').
        if ch in ("\x00", "\xe0"):
            try:
                msvcrt.getwch()
            except Exception:
                pass
            return None
        return ch

    def _read_posix(self) -> Optional[str]:
        import select
        import termios
        import tty
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        try:
            tty.setcbreak(fd)                       # cbreak, not raw: Ctrl-C still reaches the process
            if select.select([sys.stdin], [], [], 0.2)[0]:
                return sys.stdin.read(1)
            return None
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)

    # ── loop ──────────────────────────────────────────────────────────────────
    def _run(self) -> None:
        windows = os.name == "nt"
        while not self._stop.is_set():
            try:
                ch = self._read_windows() if windows else self._read_posix()
            except Exception as e:
                print(f"[SIDECAR KEYS] input unavailable ({type(e).__name__}: {e}) - hotkeys off. "
                      f"Use the HTTP control endpoints instead.", flush=True)
                return
            if not ch:
                if windows:
                    self._stop.wait(0.12)           # kbhit is a poll; posix select already waited
                continue
            entry = self._handlers.get(ch.lower())
            if entry is None:
                if ch.strip():
                    keys = " ".join(sorted(self._handlers))
                    print(f"[SIDECAR KEYS] '{ch}' is not a command. Keys: {keys}", flush=True)
                continue
            _desc, make = entry
            try:
                asyncio.run_coroutine_threadsafe(make(), self._loop)
            except Exception as e:
                print(f"[SIDECAR KEYS] '{ch}' failed: {type(e).__name__}: {e}", flush=True)

    def start(self) -> bool:
        if not _has_console():
            print("[SIDECAR KEYS] no console attached (piped or service-managed) - hotkeys disabled. "
                  "The HTTP control endpoints still work.", flush=True)
            return False
        self._thread = threading.Thread(target=self._run, name="sidecar-hotkeys", daemon=True)
        self._thread.start()
        lines = ", ".join(f"[{k}] {d}" for k, (d, _) in sorted(self._handlers.items()))
        print(f"[SIDECAR KEYS] hotkeys ON - {lines}", flush=True)
        return True

    def stop(self) -> None:
        self._stop.set()
