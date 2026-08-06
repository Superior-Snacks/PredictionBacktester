"""
HardVen sidecar — the local "private API" the C# bot calls. Book-agnostic FastAPI layer over a
pluggable BookAdapter. The bot is identical across venues; only the adapter changes per sportsbook.

Run:   HARDVEN_BOOK=mock uvicorn app:app --port 8787      (run from this sidecar/ directory)
Test:  curl "http://127.0.0.1:8787/odds?selections=MOCK_NBA_FINALS_SAS,MOCK_NBA_FINALS_NYK"

Select the book with the HARDVEN_BOOK env var (default "mock"). Add a real book by writing a new
BookAdapter subclass and registering it in load_adapter() below.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel

from bet_capture import BetSlipRecorder
from book_adapter import BetResult, BookAdapter
from env_util import load_dotenv_upwards
from mock_adapter import MockBookAdapter

load_dotenv_upwards()


# Quiet uvicorn's access log for the /odds poll: the bot hits it every ~2s, so the line fires constantly
# and buries the [BOOKMAKER] diagnostics. We drop ONLY /odds; other endpoints (/catalog, /health, /balance)
# still log. The access record's args are (client, method, full_path, http_version, status). Set
# HARDVEN_LOG_ODDS=1 to keep them.
if os.environ.get("HARDVEN_LOG_ODDS") != "1":
    class _SuppressOddsAccessLog(logging.Filter):
        def filter(self, record: logging.LogRecord) -> bool:
            a = record.args
            return not (isinstance(a, tuple) and len(a) >= 3 and isinstance(a[2], str)
                        and a[2].startswith("/odds"))
    logging.getLogger("uvicorn.access").addFilter(_SuppressOddsAccessLog())


def load_adapter() -> BookAdapter:
    name = os.environ.get("HARDVEN_BOOK", "mock").lower()
    if name == "mock":
        return MockBookAdapter()
    if name == "bookmaker":
        from bookmaker_adapter import BookmakerAdapter   # lazy: only needs Playwright when selected
        return BookmakerAdapter()
    if name == "pinnacle":
        from pinnacle_adapter import PinnacleAdapter      # clean httpx (no browser); replays x-session headers
        return PinnacleAdapter()
    if name == "aggregator":
        # COMPOSITE: prices from a read-only odds aggregator, bets/balance through a real book's adapter
        # (HARDVEN_AGG_PLACEMENT_BOOK, default pinnacle). Defaults to HARDVEN_AGG_MODE=shadow, which serves the
        # inner book's own quotes unchanged and only LOGS the aggregator for comparison.
        from aggregator_adapter import AggregatorAdapter
        return AggregatorAdapter()
    # Register more books here as you build them, e.g.:
    #   if name == "mybook": from mybook_adapter import MyBookPlaywrightAdapter; return MyBookPlaywrightAdapter()
    raise ValueError(f"Unknown HARDVEN_BOOK={name!r} (no adapter registered)")


adapter: BookAdapter = load_adapter()


@asynccontextmanager
async def lifespan(app: FastAPI):
    await adapter.startup()
    print(f"[SIDECAR] HardVen book adapter '{adapter.name}' ready.")
    yield
    await adapter.shutdown()


app = FastAPI(title="HardVen Sidecar", lifespan=lifespan)


class BetRequest(BaseModel):
    selection_id: str
    stake: float
    max_odds: float
    # PREVIEW LOCK: the CALLER declares this is a rehearsal (the bot sets it for every --dry-run). When true the
    # sidecar refuses to place REGARDLESS of HARDVEN_BET_ENABLE — so a dry run can never place a real bet even
    # when the env is armed for live trading. Without this, `--dry-run` + HARDVEN_LIVE_BET_PATH=1 + an armed
    # HARDVEN_BET_ENABLE=1 reaches _place_via_ui for real (observed 2026-08-04; only saved by an odds move).
    preview: bool = False


def _session_state() -> dict | None:
    """Adapter session readiness (Pinnacle browser-source exposes session_status(); others have no session
    gate → treated as always ready). Surfaced on /health + /odds so the C# bot knows when login is captured."""
    fn = getattr(adapter, "session_status", None)
    if not callable(fn):
        return None
    try:
        return fn()
    except Exception:
        return None


@app.get("/health")
async def health():
    h = {"ok": True, "book": adapter.name, "ts": time.time()}
    s = _session_state()
    if s is not None:
        h["session_ready"] = bool(s.get("ready", True))
        h["session"] = s
    return h


@app.post("/shutdown")
async def shutdown_sidecar():
    """Graceful stop of the whole sidecar (closes the managed browser + Pinnacle session). Used by the bot's
    `--stop-sidecar` so an UNATTENDED run tears everything down when it finishes (`--try N` reached or
    `--stop-after` elapsed) instead of leaving a logged-in browser open for hours.

    REFUSES while a bet is in flight — killing the browser mid-placement could leave a bet in an unknown state.
    The sidecar binds to 127.0.0.1, so only local processes can call this."""
    lock = getattr(adapter, "_bet_lock", None)
    if lock is not None and lock.locked():
        raise HTTPException(409, "a bet is in flight — refusing to shut down")

    async def _stop():
        await asyncio.sleep(0.25)          # let the HTTP response flush first
        try:
            # Bounded: a wedged browser must not leave the sidecar (and its Chrome windows) alive forever.
            await asyncio.wait_for(adapter.shutdown(), timeout=20)
            print("[SIDECAR] shutdown complete — browser + session closed. Exiting.")
        except asyncio.TimeoutError:
            print("[SIDECAR] shutdown timed out after 20s — forcing exit (a Chrome window may survive; close it).")
        except Exception as e:
            print(f"[SIDECAR] shutdown error ({type(e).__name__}: {e}) — forcing exit.")
        os._exit(0)

    asyncio.create_task(_stop())
    print("[SIDECAR] /shutdown received — closing browser + session, then exiting.")
    return {"stopping": True}


# ── M0: odds (the only endpoint telemetry needs) ──────────────────────────────
@app.get("/odds")
async def odds(selections: str = Query(..., description="comma-separated selection ids")):
    ids = [s for s in (x.strip() for x in selections.split(",")) if s]
    if not ids:
        raise HTTPException(400, "no selections")
    result = await adapter.odds(ids)
    # wv = per-selection "WS-verified" (live WS coverage) vs screening-only (httpx re-seed of an untabbed tail
    # league). The C# bot fires /verify on an arb whose leg is wv=false, then trusts it only once WS-confirmed.
    wv_fn = getattr(adapter, "ws_verified_map", None)
    wv = wv_fn(list(result.keys())) if wv_fn else {}
    sels = {}
    for sid, sel in result.items():
        d = sel.to_api()
        if sid in wv:
            d["wv"] = bool(wv[sid])
        sels[sid] = d
    resp = {"selections": sels, "ts": time.time()}
    s = _session_state()
    if s is not None:
        resp["session_ready"] = bool(s.get("ready", True))   # rides along so the C# /odds poll sees readiness
        if "scheduled_dark" in s:
            resp["scheduled_dark"] = bool(s.get("scheduled_dark"))   # planned close (no alert) vs unexpected logout
    return resp


# ── Verify-on-detection: promote a league to a live WS tab on demand ──────────
@app.post("/verify")
async def verify_league(lid: str):
    """The C# bot calls this when it spots an arb on a screening-only (wv=false) leg — the sidecar opens a live
    WS tab for that league so the arb can be confirmed on real-time prices before it's trusted/executed."""
    fn = getattr(adapter, "request_league_verify", None)
    if not fn:
        raise HTTPException(400, "adapter has no request_league_verify")
    return await fn(lid)


# ── Pairing: catalog ──────────────────────────────────────────────────────────
@app.post("/verify_now")
async def verify_now(selection_id: str, timeout: float = 10.0):
    """SYNCHRONOUS verify: point the roving tab at this selection's league and wait for live WS coverage, so the
    bot can re-check and fire on the SAME arb window instead of skipping it. Unlike `/verify` (dedicated-tab
    pool, can answer `at-cap`), the rove tab is uncapped."""
    fn = getattr(adapter, "verify_now", None)
    if not callable(fn):
        raise HTTPException(400, "adapter has no verify_now (Pinnacle adapter only)")
    return await fn(selection_id, timeout)


@app.get("/catalog")
async def catalog():
    return {"selections": [c.to_api() for c in await adapter.catalog()]}


@app.get("/debug/probe_bets")
async def debug_probe_bets():
    """Read-only discovery: which authed endpoint LISTS bets? Feeds the open_bets()/bet() build (crash recovery
    + settlement). Places nothing; cannot trip the session-death give-up (uses the raw client)."""
    fn = getattr(adapter, "probe_bet_endpoints", None)
    if not callable(fn):
        raise HTTPException(400, "adapter has no probe_bet_endpoints (Pinnacle adapter only)")
    return await fn()


@app.get("/debug/visibility")
async def debug_visibility():
    """What the SITE sees about each managed tab: document.visibilityState / hidden / hasFocus.

    Why it matters: a tab parked on another Windows VIRTUAL DESKTOP (or minimised / fully occluded) reports
    `hidden`. A hidden tab is unremarkable on its own — but the organic layer SCROLLING and CLICKING a hidden
    page is not something a real user does, and `bring_to_front` cannot fix it when Windows' foreground-lock
    refuses the raise (you see a taskbar flash instead). Poll this from your OWN desktop — reading it does not
    disturb the bot window, whereas opening DevTools on that window would change the very thing you're measuring.

    visible => the placement/organic gestures are coherent with what the page reports (a virtual DISPLAY gives
    this; a virtual DESKTOP usually does not)."""
    out = []
    js = ("() => ({vis: document.visibilityState, hidden: document.hidden, "
          "focus: document.hasFocus(), w: window.innerWidth, h: window.innerHeight})")
    br = getattr(adapter, "_browser", None)
    pages = []
    primary = getattr(br, "_page", None) if br else None
    if primary is not None:
        pages.append(("primary", primary))
    tm = getattr(adapter, "_tab_manager", None)
    if tm is not None:
        try:
            for pg, lid in (tm.reader_tabs() or []):
                pages.append((f"reader:{lid}", pg))
        except Exception:
            pass
    for name, pg in pages:
        try:
            if pg.is_closed():
                out.append({"tab": name, "error": "closed"})
                continue
            out.append({"tab": name, **(await pg.evaluate(js))})
        except Exception as e:
            out.append({"tab": name, "error": f"{type(e).__name__}: {e}"})
    n_vis = sum(1 for t in out if t.get("vis") == "visible")
    return {"tabs": out, "visible_count": n_vis, "total": len(out)}


@app.get("/debug/schedule")
async def debug_schedule():
    """The lifecycle's work-window plan: every planned session (local + UTC, games, duration, past/NOW/
    upcoming), the current window with minutes-to-close, and the plan's provenance (mode, how many games were
    fetched / kept for today / paired, jitter). The schedule half of "what is the bot doing and why"."""
    lc = getattr(adapter, "_lifecycle", None)
    if lc is None:
        raise HTTPException(400, "lifecycle not running (PINNACLE_LIFECYCLE=1 required)")
    return lc.status()


@app.get("/debug/tabs")
async def debug_tabs():
    """The tab manager's own account of every tab: what it's SHOWING vs what it SHOULD show (on_station),
    why it holds a slot (hot_games inside HARDVEN_TAB_HOT_HOURS), which paired leagues the featured board
    covers, and the current hot ranking with each league's coverage source. The direct answer to "what is
    happening on the bot's tabs and what is supposed to be on them"."""
    tm = getattr(adapter, "_tab_manager", None)
    if tm is None:
        raise HTTPException(400, "no tab manager (HARDVEN_TAB_MANAGER=1 + browser session required)")
    return tm.status()


@app.get("/debug/reader")
async def debug_reader(ttl: float = 30.0):
    """Coverage diagnostic: the matchups ('lid:mid') the browser-WS reader has actually pushed odds for within
    `ttl`s. Used by coverage_check.py to compare the reader's live slate against the guest board (ground truth)."""
    fn = getattr(adapter, "reader_live_mids", None)
    mids = fn(ttl) if fn else []
    bfn = getattr(adapter, "board_lids", None)
    board = sorted(bfn()) if bfn else []   # leagues the FEATURED BOARD streams (sport-level topics)
    return {"live_mids": mids, "count": len(mids), "board_lids": board, "board_count": len(board)}


@app.get("/debug/straight")
async def debug_straight(lid: str, source: str = "authed"):
    """{token: decimal} for a league's straight markets from `source` (authed|guest). probe_reseed_delay.py polls
    BOTH over time to measure how far the public guest feed lags the logged-in authed feed."""
    fn = getattr(adapter, "straight_snapshot", None)
    if not fn:
        raise HTTPException(400, "adapter has no straight_snapshot")
    return await fn(lid, source)


@app.get("/debug/browser_fetch")
async def debug_browser_fetch(lid: str):
    """Feasibility probe: fetch the AUTHED /markets/straight from INSIDE the logged-in browser page (genuine
    Chrome TLS) to test moving the re-seed off httpx. GREEN (ok=true, n_markets>0) ⇒ browser-fetch re-seed is
    viable for zero non-Chrome footprint; an error (esp. CORS) ⇒ stick with the authed httpx re-seed."""
    fn = getattr(adapter, "browser_fetch_straight_probe", None)
    if not fn:
        raise HTTPException(400, "adapter has no browser_fetch_straight_probe")
    return await fn(lid)


# ── M1: bet-slip flow capture (arms the browser to record ONE manual bet) ─────
_recorder = BetSlipRecorder()


def _managed_page():
    """The sidecar's logged-in Playwright page. NOTE: `adapter._session` is the x-session STRING; the browser
    session object is `adapter._browser` (a PinnacleBrowserSession, present only when
    PINNACLE_SESSION_SOURCE=browser)."""
    br = getattr(adapter, "_browser", None)
    return getattr(br, "_page", None) if br else None


@app.post("/capture/start")
async def capture_start():
    """Arm the bet-slip recorder, then place ONE small bet BY HAND in the managed browser. Records the
    interaction sequence, the DOM regions that change at each stage, screenshots, and the bet POST -- the raw
    material for writing `_place_via_ui()` against real markup. Places nothing itself."""
    page = _managed_page()
    if page is None:
        raise HTTPException(400, "no managed browser page (PINNACLE_SESSION_SOURCE=browser and logged in?)")
    res = await _recorder.start(page)
    if not res.get("ok"):
        raise HTTPException(400, res.get("error", "capture failed to start"))
    return res


@app.post("/capture/stop")
async def capture_stop():
    res = await _recorder.stop()
    if not res.get("ok"):
        raise HTTPException(400, res.get("error", "capture not running"))
    return res


@app.get("/capture/status")
async def capture_status():
    return _recorder.status()


class BetTestRequest(BaseModel):
    selection_id: str
    stake: float = 2.0
    max_odds: float = 1.01     # floor on acceptable decimal odds; 1.01 = accept whatever is offered
    submit: bool = False       # False = full dress rehearsal, stops before clicking Place Bet
    record: bool = True        # capture the attempt (builds the library for spotting flow variations)


@app.post("/bet/test")
async def bet_test(req: BetTestRequest):
    """Manual single-bet harness for testing UI placement. Runs the REAL path, so what you exercise is what
    runs live. `submit=false` (DEFAULT) stops just before clicking Place Bet -- it navigates, finds the row,
    verifies the popover is the intended market, and enters the stake, placing nothing. `submit=true`
    additionally requires HARDVEN_BET_ENABLE=1.

    With `record=true` each attempt is captured to its own bet_capture_*.jsonl + screenshots, so repeated runs
    accumulate the evidence needed to spot flow variations (accept-odds prompts, suspended markets, live vs
    pre-match layouts) BEFORE they cause a wrong bet."""
    fn = getattr(adapter, "verify_bet_ui", None)
    if not callable(fn):
        raise HTTPException(400, "adapter has no verify_bet_ui (Pinnacle adapter only)")
    page = _managed_page()
    recording = False
    if req.record and page is not None and not _recorder.status()["active"]:
        recording = (await _recorder.start(page)).get("ok", False)
    try:
        res = await fn(req.selection_id, req.stake, req.max_odds, submit=req.submit)
    finally:
        if recording:
            await _recorder.stop()
    out = res.to_api()
    out["submitted"] = req.submit
    if recording:
        out["capture"] = _recorder.status()["file"]
    return out


# ── M1: betting + wallet confirmation ─────────────────────────────────────────
@app.get("/balance")
async def balance():
    amt = await adapter.balance()
    resp = {"balance": amt}
    s = _session_state()
    if s is not None and s.get("currency"):
        resp["currency"] = s.get("currency")   # account currency (e.g. EUR) — Kalshi is USD; FX-convert at M1
    return resp


@app.post("/bet")
async def place_bet(req: BetRequest):
    # PREVIEW LOCK (caller-declared rehearsal) — checked BEFORE the adapter, so it holds for every book and
    # cannot be defeated by an armed HARDVEN_BET_ENABLE. The bot sets preview=true on every --dry-run.
    if req.preview:
        print(f"[PINNACLE BET] PREVIEW-LOCKED (caller sent preview=true) - WOULD place {req.stake:.2f} on "
              f"{req.selection_id} @ max_odds>={req.max_odds:.4f}. No bet placed.")
        return BetResult(accepted=False, stake=req.stake,
                         reason="preview-locked: caller declared a rehearsal (--dry-run) — no bet placed").to_api()
    return (await adapter.place_bet(req.selection_id, req.stake, req.max_odds)).to_api()


@app.get("/bets/find")
async def find_bet(selection_id: str, since: str = ""):
    """How did the Pinnacle leg on `selection_id` actually finish — win / loss / **void**? Called when a position
    settles, so the bot books the TRUE outcome instead of assuming the arb resolved symmetrically. `since` is the
    position's entry time (ISO); a 5-minute grace absorbs clock skew. 404 when no matching bet is found."""
    fn = getattr(adapter, "find_bet", None)
    if not callable(fn):
        raise HTTPException(400, "adapter has no find_bet (Pinnacle adapter only)")
    b = await fn(selection_id, since)
    if b is None:
        raise HTTPException(404, f"no bet found for {selection_id}")
    return b


@app.get("/bets/open")
async def open_bets():
    return {"bets": await adapter.open_bets()}


@app.get("/bets/{bet_id}")
async def get_bet(bet_id: str):
    b = await adapter.bet(bet_id)
    if b is None:
        raise HTTPException(404, "bet not found")
    return b
