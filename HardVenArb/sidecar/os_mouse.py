"""Hybrid pointer: CDP finds the target, the REAL Windows mouse clicks it.

WHY BOTH. Proven on BetInAsia 2026-08-15: a betslip opened by a CDP click is dismissed within ~1-3s
while one opened by a `SendInput` click survives. But CDP is not the enemy — 'Show more' expansions are
CDP clicks and they stick, and CDP is the only thing that can resolve an element's LIVE position on a
board that reorders as odds tick. Coordinate-clicking from stale positions means clicking whatever slid
into them, which is how you bet the wrong side.

So each layer does what only it can:
  - CDP/Playwright: locate the element, scroll it into view, read its live bounding box
  - Windows SendInput: move the physical cursor there and press the physical button

THE HARD PART IS THE TRANSLATION, not the click. An element's box is in CSS pixels relative to the
viewport; SendInput wants physical pixels on the virtual desktop. Between them sit the window's position,
the browser chrome above the viewport, and the display's DPI scale. All three are read from the page's own
`window` — via an ISOLATED WORLD, so the page's own scripts cannot see the read (main-world `evaluate` is
observable, and this module exists to avoid being observed).

CALIBRATION CHECKS THE ARITHMETIC rather than trusting it: after computing where a client point should be
on screen, it moves there, reads back where the page says the pointer landed, and applies the residual.
That absorbs DPI rounding and any chrome geometry the formula gets wrong, and it re-runs whenever the
window moves.

Windows-only by nature. On anything else `available()` is False and callers fall back to CDP.
"""
from __future__ import annotations

import asyncio
import ctypes
import random
import sys
from typing import Optional

_WIN = sys.platform.startswith("win")

if _WIN:
    # PER-MONITOR DPI AWARE V2. Without this, GetSystemMetrics reports CSS-ish scaled pixels on a
    # scaled display and every absolute move lands short — silently, and proportionally to the scale.
    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except Exception:
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            pass

    ULONG_PTR = ctypes.POINTER(ctypes.c_ulong)
    INPUT_MOUSE = 0
    MOUSEEVENTF_MOVE = 0x0001
    MOUSEEVENTF_ABSOLUTE = 0x8000
    MOUSEEVENTF_VIRTUALDESK = 0x4000
    MOUSEEVENTF_LEFTDOWN = 0x0002
    MOUSEEVENTF_LEFTUP = 0x0004
    SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN = 76, 77
    SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN = 78, 79

    class MOUSEINPUT(ctypes.Structure):
        _fields_ = [("dx", ctypes.c_long), ("dy", ctypes.c_long),
                    ("mouseData", ctypes.c_ulong), ("dwFlags", ctypes.c_ulong),
                    ("time", ctypes.c_ulong), ("dwExtraInfo", ULONG_PTR)]

    class _U(ctypes.Union):
        _fields_ = [("mi", MOUSEINPUT)]

    class INPUT(ctypes.Structure):
        _anonymous_ = ("u",)
        _fields_ = [("type", ctypes.c_ulong), ("u", _U)]

    class POINT(ctypes.Structure):
        _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


def available() -> bool:
    return _WIN


def cursor_pos() -> tuple[int, int]:
    p = POINT()
    ctypes.windll.user32.GetCursorPos(ctypes.byref(p))
    return p.x, p.y


def _virtual_desktop() -> tuple[int, int, int, int]:
    g = ctypes.windll.user32.GetSystemMetrics
    return (g(SM_XVIRTUALSCREEN), g(SM_YVIRTUALSCREEN),
            g(SM_CXVIRTUALSCREEN), g(SM_CYVIRTUALSCREEN))


def _send(flags: int, dx: int = 0, dy: int = 0) -> bool:
    inp = INPUT(type=INPUT_MOUSE, u=_U(mi=MOUSEINPUT(dx, dy, 0, flags, 0, None)))
    return ctypes.windll.user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT)) == 1


def move_to(sx: float, sy: float) -> bool:
    """Absolute move in PHYSICAL screen pixels, normalised across the whole virtual desktop."""
    vx, vy, vw, vh = _virtual_desktop()
    if vw <= 1 or vh <= 1:
        return False
    nx = int(round((sx - vx) * 65535.0 / (vw - 1)))
    ny = int(round((sy - vy) * 65535.0 / (vh - 1)))
    nx = max(0, min(65535, nx))
    ny = max(0, min(65535, ny))
    return _send(MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK, nx, ny)


async def human_move_to(sx: float, sy: float, steps: int = 0) -> bool:
    """Travel there in steps rather than teleporting. The OS reports every intermediate position, so a
    one-jump move is a straight line at infinite speed in the event stream — the very thing the Bezier
    path in human_mouse.py exists to avoid, and it would be a shame to reintroduce it at this layer."""
    x0, y0 = cursor_pos()
    dist = max(abs(sx - x0), abs(sy - y0))
    n = steps or max(6, min(28, int(dist / 22)))
    for i in range(1, n + 1):
        t = i / n
        ease = t * t * (3 - 2 * t)                      # smoothstep: accelerate out, decelerate in
        jx = random.uniform(-1.2, 1.2) if i < n else 0.0
        jy = random.uniform(-1.2, 1.2) if i < n else 0.0
        if not move_to(x0 + (sx - x0) * ease + jx, y0 + (sy - y0) * ease + jy):
            return False
        await asyncio.sleep(random.uniform(0.006, 0.018))
    return True


def click_here(press_ms: Optional[int] = None) -> bool:
    """Press and release at the current position."""
    if not _send(MOUSEEVENTF_LEFTDOWN):
        return False
    import time as _t
    _t.sleep((press_ms if press_ms is not None else random.randint(45, 95)) / 1000.0)
    return _send(MOUSEEVENTF_LEFTUP)


# ── client -> screen ─────────────────────────────────────────────────────────
_ISOLATED = {}          # page -> (executionContextId, world name)
_CALIB = {}             # page -> (origin_x, origin_y, scale, window_signature)

_METRICS_JS = """
({sx: window.screenX, sy: window.screenY,
  ow: window.outerWidth, oh: window.outerHeight,
  iw: window.innerWidth, ih: window.innerHeight,
  dpr: window.devicePixelRatio || 1})
"""


async def _isolated_eval(cdp, page, expr: str):
    """Evaluate in an ISOLATED world: same DOM and same `window`, invisible to the page's own scripts."""
    key = id(page)
    ctx = _ISOLATED.get(key)
    if ctx is None:
        frame_tree = await cdp.send("Page.getFrameTree")
        fid = frame_tree["frameTree"]["frame"]["id"]
        w = await cdp.send("Page.createIsolatedWorld",
                           {"frameId": fid, "worldName": "hv", "grantUniveralAccess": False})
        ctx = w["executionContextId"]
        _ISOLATED[key] = ctx
    r = await cdp.send("Runtime.evaluate",
                       {"expression": expr, "contextId": ctx, "returnByValue": True})
    return (r.get("result") or {}).get("value")


async def viewport_origin(cdp, page) -> Optional[tuple[float, float, float]]:
    """(screen_x, screen_y, scale) of the viewport's top-left, in PHYSICAL pixels."""
    m = await _isolated_eval(cdp, page, _METRICS_JS)
    if not m:
        return None
    dpr = float(m.get("dpr") or 1.0)
    border = max(0.0, (m["ow"] - m["iw"]) / 2.0)        # side borders; 0 on modern Chrome
    chrome_h = max(0.0, m["oh"] - m["ih"] - border)     # tab strip + omnibox
    # window.screenX/Y are CSS pixels of the desktop; multiply through by dpr for physical.
    return ((m["sx"] + border) * dpr, (m["sy"] + chrome_h) * dpr, dpr)


async def calibrate(cdp, page) -> Optional[tuple[float, float, float]]:
    """Verify the arithmetic by moving there and reading back where the page says the pointer landed.

    The formula above is right in principle and wrong in practice often enough to matter — fractional DPI
    scales round, and some window states change the chrome height. One probe move costs nothing and turns
    a guess into a measurement; the residual is folded into the origin.
    """
    base = await viewport_origin(cdp, page)
    if base is None:
        return None
    ox, oy, dpr = base
    sig = (round(ox), round(oy), round(dpr, 3))
    cached = _CALIB.get(id(page))
    if cached and cached[3] == sig:
        return cached[:3]

    # Listen from the isolated world: the page's own scripts cannot enumerate our listener.
    await _isolated_eval(cdp, page,
                         "(() => { window.__hvp = null; document.addEventListener('mousemove',"
                         " e => { window.__hvp = [e.clientX, e.clientY]; }, true); return 1; })()")
    target_client = (120.0, 220.0)                      # safely inside any viewport
    await human_move_to(ox + target_client[0] * dpr, oy + target_client[1] * dpr, steps=8)
    await asyncio.sleep(0.12)
    got = await _isolated_eval(cdp, page, "window.__hvp")
    if isinstance(got, list) and len(got) == 2:
        # Residual in CLIENT px -> correct the origin in PHYSICAL px.
        ox += (target_client[0] - float(got[0])) * dpr
        oy += (target_client[1] - float(got[1])) * dpr
    _CALIB[id(page)] = (ox, oy, dpr, sig)
    return ox, oy, dpr


async def click_element(cdp, page, loc, timeout: int = 5000) -> bool:
    """Scroll the element into view with CDP, then click it with the REAL mouse.

    The box is read AFTER the scroll and immediately before the move, so the coordinates are as live as
    a CDP click's would be.
    """
    if not _WIN:
        return False
    try:
        await loc.scroll_into_view_if_needed(timeout=timeout)
        box = await loc.bounding_box()
    except Exception:
        return False
    if not box:
        return False
    cal = await calibrate(cdp, page)
    if cal is None:
        return False
    ox, oy, dpr = cal
    cx = box["x"] + box["width"] * random.uniform(0.35, 0.65)
    cy = box["y"] + box["height"] * random.uniform(0.35, 0.65)
    if not await human_move_to(ox + cx * dpr, oy + cy * dpr):
        return False
    await asyncio.sleep(random.uniform(0.05, 0.14))     # people do not click the instant they arrive
    return click_here()
