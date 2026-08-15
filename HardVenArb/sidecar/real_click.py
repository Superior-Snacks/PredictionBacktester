"""Click with the REAL Windows mouse, at wherever the physical cursor already is.

THE QUESTION THIS SETTLES. A betslip the operator clicks survives indefinitely — verified with the mouse
physically upside down, so no input at all. A betslip the bot clicks via Playwright dies in ~1-3s. Focus
is ruled out (`click_compare.py --countdown`: window focused, cursor on the row, slip still died at
t+5.1s). What remains is the click's ORIGIN.

Playwright clicks through CDP `Input.dispatchMouseEvent`. Those events are `isTrusted`, but they are
synthesised inside the renderer and differ from hardware input in ways a page can read:
`pressure` 0 instead of ~0.5, `movementX/Y` 0, and `screenX/screenY` equal to `clientX/clientY` rather
than offset by the window's position on the desktop.

`SendInput` goes in at the OS layer instead. The browser receives a genuine WM_LBUTTONDOWN and
synthesises the DOM event itself, exactly as it does for a finger on a physical button — same pressure,
same movement deltas, same screen coordinates. (A low-level Windows hook could see LLMHF_INJECTED, but
JavaScript in a page cannot.) So if the slip survives a SendInput click and dies on a CDP click, the
click's origin is the whole answer, and the placement path has to drive the OS mouse rather than CDP.

    python real_click.py                 # hover a moneyline, it clicks after 8s, then times the slip
    python real_click.py --countdown 15
    python real_click.py --watch 60

Places nothing: it clicks a moneyline, which opens a betslip. Closes it on the way out.
"""
from __future__ import annotations

import argparse
import ctypes
import json
import sys
import time
import urllib.request

sys.stdout.reconfigure(encoding="utf-8")

if not sys.platform.startswith("win"):
    print("SendInput is Windows-only; this test needs the machine the browser is on.")
    sys.exit(2)

# ── SendInput plumbing ───────────────────────────────────────────────────────
ULONG_PTR = ctypes.POINTER(ctypes.c_ulong)
INPUT_MOUSE = 0
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", ctypes.c_long), ("dy", ctypes.c_long),
                ("mouseData", ctypes.c_ulong), ("dwFlags", ctypes.c_ulong),
                ("time", ctypes.c_ulong), ("dwExtraInfo", ULONG_PTR)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", ctypes.c_ulong), ("u", _INPUTUNION)]


class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


def cursor_pos() -> tuple[int, int]:
    p = POINT()
    ctypes.windll.user32.GetCursorPos(ctypes.byref(p))
    return p.x, p.y


def real_click(press_ms: int = 60) -> bool:
    """One genuine left click at the CURRENT cursor position. No move: the operator aimed it."""
    def send(flags: int) -> int:
        inp = INPUT(type=INPUT_MOUSE,
                    u=_INPUTUNION(mi=MOUSEINPUT(0, 0, 0, flags, 0, None)))
        return ctypes.windll.user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))
    if send(MOUSEEVENTF_LEFTDOWN) != 1:
        return False
    time.sleep(press_ms / 1000.0)
    return send(MOUSEEVENTF_LEFTUP) == 1


# ── sidecar reads ────────────────────────────────────────────────────────────
def _get(url, timeout=25):
    return json.load(urllib.request.urlopen(url, timeout=timeout))


def _post(url, timeout=30):
    return json.load(urllib.request.urlopen(
        urllib.request.Request(url, data=b"", method="POST"), timeout=timeout))


def slip_alive(base: str) -> bool:
    try:
        d = _get(f"{base}/debug/slip_dom")
    except Exception:
        return False
    return any("price-input" in (p.get("inputs") or "") for p in (d.get("pages") or []))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8788)
    ap.add_argument("--countdown", type=float, default=8.0)
    ap.add_argument("--watch", type=float, default=45.0)
    a = ap.parse_args()
    base = f"http://127.0.0.1:{a.port}"

    try:
        from staleness import check
        check(a.port)
    except ImportError:
        pass

    # Start clean, so "a slip exists" afterwards can only mean this click opened one.
    try:
        _post(f"{base}/slip_close")
    except Exception:
        pass
    if slip_alive(base):
        print("a betslip is ALREADY open — close it by hand first, or the timing measures the wrong one.")
        return 1

    print("=" * 74)
    print("HOVER the real mouse over a moneyline price in the sidecar's browser window.")
    print("Do NOT click. Just leave the pointer sitting on it. Keep still after that.")
    print(f"A real Windows click fires at the pointer in {a.countdown:.0f}s.")
    print("=" * 74)
    for r in range(int(a.countdown), 0, -1):
        x, y = cursor_pos()
        print(f"  {r}...  cursor at ({x},{y})      ", end="\r", flush=True)
        time.sleep(1)
    x, y = cursor_pos()
    print(f"\nclicking for real at ({x},{y}) via SendInput...")
    if not real_click():
        print("SendInput was rejected. If the browser runs elevated and this shell does not, Windows")
        print("blocks the injection (UIPI) -- run this terminal as administrator, or lower the browser.")
        return 1

    t0 = time.time()
    opened = False
    for _ in range(12):                      # the slip takes a moment to render
        if slip_alive(base):
            opened = True
            break
        time.sleep(0.5)
    if not opened:
        print("No betslip appeared. The pointer probably was not over a price cell -- the click landed")
        print("somewhere harmless. Re-run and aim at the odds number itself.")
        return 1
    print(f"betslip OPEN {time.time() - t0:.1f}s after the click. Watching (do not touch the mouse)...\n")

    died = None
    deadline = time.time() + a.watch
    while time.time() < deadline:
        if not slip_alive(base):
            died = time.time() - t0
            break
        print(".", end="", flush=True)
        time.sleep(1.0)

    print("\n")
    if died is None:
        print(f"SLIP SURVIVED {a.watch:.0f}s after a REAL OS CLICK, untouched.")
        print("=> The click's ORIGIN is the whole difference. CDP-dispatched clicks produce a slip the")
        print("   venue discards; hardware-level clicks do not. The placement path must drive the OS")
        print("   mouse (move + click via SendInput against the element's screen coordinates) rather")
        print("   than page.click(), OR the slip must be filled fast enough not to care.")
    else:
        print(f"SLIP DIED at t+{died:.1f}s even from a REAL mouse click.")
        print("=> Then the click is NOT the difference, and something about the operator's own sessions")
        print("   keeps slips alive that neither click reproduces. Next suspect: what else differs -- a")
        print("   slip opened from a DIFFERENT page/route, or a preceding interaction we are not making.")
    try:
        _post(f"{base}/slip_close")
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
