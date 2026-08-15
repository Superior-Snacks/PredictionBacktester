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


async def calibrate(cdp, page) -> Optional[tuple[float, float, float, float]]:
    """Solve `screen = origin + client * scale` per axis, by MEASURING two points.

    Returns (origin_x, origin_y, scale_x, scale_y) in physical pixels per CSS pixel.

    WHY TWO. The first version measured ONE point and folded the residual into the origin, which corrects
    an offset and cannot correct a SCALE error — and a wrong scale produces an error that GROWS with
    distance from the probe. Observed 2026-08-15: the cursor stopped "way before the moneylines", i.e.
    short, and increasingly so further down the page. That is the signature of a scale error, and it came
    from trusting `devicePixelRatio` and the outerHeight/innerHeight chrome arithmetic to be exact.

    Two probes make the mapping empirical: command two screen positions, read back where the page says
    the pointer actually landed, and solve. No DPI assumption survives, no chrome-geometry assumption
    survives, and a maximised-vs-restored window makes no difference. The geometric estimate is still
    computed, but only to aim the probes somewhere inside the viewport.
    """
    base = await viewport_origin(cdp, page)
    if base is None:
        return None
    ox, oy, dpr = base
    sig = (round(ox), round(oy), round(dpr, 3))
    cached = _CALIB.get(id(page))
    if cached and cached[-1] == sig:
        return cached[:4]

    # Listen from the isolated world: the page's own scripts cannot enumerate our listener.
    await _isolated_eval(cdp, page,
                         "(() => { window.__hvp = null; document.addEventListener('mousemove',"
                         " e => { window.__hvp = [e.clientX, e.clientY]; }, true); return 1; })()")

    async def probe(cx: float, cy: float):
        """Aim at a client point using the ESTIMATE, then report (commanded_screen, observed_client)."""
        sx, sy = ox + cx * dpr, oy + cy * dpr
        await _isolated_eval(cdp, page, "window.__hvp = null")
        if not await human_move_to(sx, sy, steps=8):
            return None
        await asyncio.sleep(0.12)
        got = await _isolated_eval(cdp, page, "window.__hvp")
        if not (isinstance(got, list) and len(got) == 2):
            return None
        return sx, sy, float(got[0]), float(got[1])

    # Far apart, so the solved scale is not dominated by rounding, but both well inside any viewport.
    p1 = await probe(140.0, 200.0)
    p2 = await probe(620.0, 560.0)
    if not p1 or not p2:
        print("[os_mouse] calibration FAILED — no mousemove observed. The pointer may be landing outside "
              "the window entirely; check the browser is on screen and not minimised.", flush=True)
        return None
    d_client_x, d_client_y = p2[2] - p1[2], p2[3] - p1[3]
    if abs(d_client_x) < 5 or abs(d_client_y) < 5:
        print(f"[os_mouse] calibration FAILED — the two probes landed on the same spot "
              f"(dx={d_client_x:.0f} dy={d_client_y:.0f}). Cannot solve a scale.", flush=True)
        return None
    kx = (p2[0] - p1[0]) / d_client_x
    ky = (p2[1] - p1[1]) / d_client_y
    ox2 = p1[0] - p1[2] * kx
    oy2 = p1[1] - p1[3] * ky
    if abs(kx - dpr) > 0.02 or abs(ky - dpr) > 0.02:
        print(f"[os_mouse] calibrated scale {kx:.3f}x{ky:.3f} differs from devicePixelRatio {dpr:.3f} — "
              f"using the MEASURED value (this is why one-point calibration was aiming short).",
              flush=True)
    _CALIB[id(page)] = (ox2, oy2, kx, ky, sig)
    return ox2, oy2, kx, ky


async def click_element(cdp, page, loc, timeout: int = 5000) -> bool:
    """Scroll the element into view with CDP, then click it with the REAL mouse.

    ⚠ COORDINATE CLICKING GIVES UP THE ONE GUARANTEE `locator.click()` PROVIDES: it re-resolves the
    element at click time, so it cannot land on whatever slid into a remembered position. This board
    reorders as odds tick, and on 2026-08-15 the first version of this function clicked a DIFFERENT MATCH
    ENTIRELY — asked for 2026-08-16,101774,99176 and navigated to 2026-08-15,58962,80512 — because
    `calibrate()` ran BETWEEN reading the box and moving to it, spending ~200ms during which the row moved.
    On a live account that is a bet on the wrong market.

    So the order is now: calibrate FIRST (it moves the cursor and takes time), then read the box, then
    move, then RE-READ the box and confirm the aim point is still inside the element before pressing. The
    re-read is what restores the guarantee — if the row shifted under us, nothing is clicked.
    """
    if not _WIN:
        return False
    # FIRST, because it moves the physical cursor and costs time. Cached per window position, so this is
    # free on every click after the first.
    cal = await calibrate(cdp, page)
    if cal is None:
        return False
    ox, oy, kx, ky = cal
    for attempt in (1, 2):
        try:
            await loc.scroll_into_view_if_needed(timeout=timeout)
            box = await loc.bounding_box()
        except Exception:
            return False
        if not box:
            return False
        cx = box["x"] + box["width"] * random.uniform(0.35, 0.65)
        cy = box["y"] + box["height"] * random.uniform(0.35, 0.65)
        if not await human_move_to(ox + cx * kx, oy + cy * ky):
            return False
        await asyncio.sleep(random.uniform(0.05, 0.14))  # people do not click the instant they arrive
        try:
            now = await loc.bounding_box()
        except Exception:
            now = None
        if now and (now["x"] <= cx <= now["x"] + now["width"]
                    and now["y"] <= cy <= now["y"] + now["height"]):
            return click_here()
        # The element moved while the cursor travelled. Re-aim once; if it moves again, refuse — a board
        # reordering that fast is not one to fire a coordinate click into.
        print(f"[os_mouse] target moved under the cursor (attempt {attempt}) — re-aiming rather than "
              f"clicking whatever is there now", flush=True)
    return False
