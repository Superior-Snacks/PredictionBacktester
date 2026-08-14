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
    if name == "betinasia":
        from betinasia_adapter import BetInAsiaAdapter    # httpx login + WS price feed; no browser at all
        return BetInAsiaAdapter()
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
    # The venue-side BETTING CONTRACT, published so the bot can verify it agrees with its own sizing BEFORE it
    # fires anything. These are the sidecar's own numbers, deliberately independent of the C# ladder: a hard cap
    # in a separate process is what catches a units/FX/depth bug in the bot before it becomes a real bet. But a
    # mismatch is only safe if it is LOUD — a sidecar cap below the ladder's rung rejects the book leg AFTER the
    # Kalshi leg has filled, i.e. a naked leg on every single arb. See the C# preflight in Program.cs.
    h["betting"] = {
        "max_stake": getattr(adapter, "_max_stake", None),      # HARDVEN_MAX_STAKE (account currency)
        "bet_enabled": bool(getattr(adapter, "_bet_enabled", False)),   # HARDVEN_BET_ENABLE
        "currency": getattr(adapter, "_balance_currency", "") or None,
    }
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
async def odds(selections: str = Query(..., description="comma-separated selection ids"),
               fresh: int = Query(0, description="1 = re-read these selections FROM THE VENUE first "
                                                 "(execution-path verify only, never the poll loop)")):
    ids = [s for s in (x.strip() for x in selections.split(",")) if s]
    if not ids:
        raise HTTPException(400, "no selections")
    # INDEPENDENT VERIFY. Without this, /odds answers from the same cache the caller screened on, so a
    # "verification" is a cache agreeing with itself -- it agreed 110/110 times while the independently
    # checked Kalshi leg disagreed 76% of the time. `fresh=1` forces a live venue read first, and the
    # `venue_fresh` flag below tells the caller whether that actually happened, so a failed refetch can
    # never masquerade as a confirmed price.
    # TRI-STATE, not a boolean. "cannot" and "tried and failed" must not collapse into one value:
    #   ok          - the venue was re-read; the price below is confirmed
    #   failed      - we asked the venue and it did not answer  -> the caller should REFUSE
    #   unsupported - this book has no independent price read AT ALL
    # BetInAsia is push-only with no REST price endpoint (the entire recon contains exactly one
    # price-bearing HTTP response, and it is /v1/betslips/), so "unsupported" is permanent and structural
    # there, not a failure. Reporting it as `failed` would have made the C# side refuse every BIA arb the
    # moment a quiet pre-live quote aged past the stale gate -- i.e. exactly the windows that venue exists
    # to trade, killed by a gate meant for a different book's failure mode.
    venue_refetch = None
    if fresh:
        rf = getattr(adapter, "refetch_from_venue", None)
        if not callable(rf):
            venue_refetch = "unsupported"
        else:
            try:
                venue_refetch = "ok" if (await rf(ids)).get("ok") else "failed"
            except Exception:
                venue_refetch = "failed"
    result = await adapter.odds(ids)
    # wv = per-selection "WS-verified" (live WS coverage) vs screening-only (httpx re-seed of an untabbed tail
    # league). The C# bot fires /verify on an arb whose leg is wv=false, then trusts it only once WS-confirmed.
    wv_fn = getattr(adapter, "ws_verified_map", None)
    wv = wv_fn(list(result.keys())) if wv_fn else {}
    # acca = can this event go on a BETSLIP at all? Only published by books that know (BetInAsia reads it
    # off the event frame). The bot uses it to skip slip-verify samples that would be refused instantly —
    # see BetInAsiaAdapter.acca_ok_map. Absent => the bot assumes True and behaves exactly as before.
    acca_fn = getattr(adapter, "acca_ok_map", None)
    acca = acca_fn(list(result.keys())) if acca_fn else {}
    sels = {}
    for sid, sel in result.items():
        d = sel.to_api()
        if sid in wv:
            d["wv"] = bool(wv[sid])
        if sid in acca:
            d["acca"] = bool(acca[sid])
        sels[sid] = d
    resp = {"selections": sels, "ts": time.time()}
    if venue_refetch is not None:
        resp["venue_refetch"] = venue_refetch
        resp["venue_fresh"] = (venue_refetch == "ok")   # kept for readability in logs/diagnostics
    # FEED HEALTH rides along with every poll. The C# freshness gate was per-QUOTE age, which is right
    # for a book whose sidecar serves a frozen last-known price when its fetch fails (a stale ts really
    # can mean "our session died"). On a push-only venue the same signal means the opposite: the ts is
    # stamped only when the venue actually sends that event, so an old ts is a QUIET MARKET, not a dead
    # one — and discarding it threw away every pre-live price. Adapters that can tell the difference
    # publish it here; the bot then trusts a quiet quote while the FEED is alive, and clears everything
    # when it is not.
    fh = getattr(adapter, "feed_health", None)
    if callable(fh):
        try:
            resp["feed"] = fh()
        except Exception:
            pass
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


@app.post("/slip_quote")
async def slip_quote(selection_id: str):
    """Open the betslip popover for one selection and return the TRUE offered odds. Places nothing.

    The executor calls this after detecting an arb and before firing: it is the only price Pinnacle will
    actually honour, and it is where the screened price and the obtainable price diverge. Costs a few
    seconds, so it belongs on the execution path only — never on the poll loop."""
    fn = getattr(adapter, "slip_quote", None)
    if not callable(fn):
        raise HTTPException(404, f"book '{adapter.name}' cannot quote from a betslip")
    return await fn(selection_id)


@app.post("/slip_close")
async def slip_close():
    """Close the betslip this bot opened, if any. Idempotent — safe to call unconditionally.

    Not merely tidiness: closing sends `unwatch_acca_hcaps`, which frees the subscription. Without it a
    quoted event stays subscribed for the life of the socket and becomes permanently unquotable once its
    cached price ages out, so every event is one-shot. It also makes the venue's own betslip.close metric
    fire, which a hand-driven session emits on every close and the bot emitted on none."""
    fn = getattr(adapter, "slip_close", None)
    if not callable(fn):
        raise HTTPException(404, f"book '{adapter.name}' has no betslip to close")
    return await fn()


@app.get("/debug/feed")
async def debug_feed():
    """Price-socket + coverage diagnostics. Answers "is the WS up, and is anything dropped?".

    Read `drops.resumed_after_quiet` first: a dropped subscription cannot resume, so any nonzero value
    proves nothing was evicted. `sockets` > 1 means the connection came back at some point — and
    subscriptions do NOT survive a reconnect, so that is when a re-walk is needed. A falling
    priced/catalog ratio with zero drops is the silent-decay case: new fixtures were listed and never
    subscribed, which another sport walk fixes."""
    fn = getattr(adapter, "feed_diagnostics", None)
    if not callable(fn):
        raise HTTPException(404, f"book '{adapter.name}' publishes no feed diagnostics")
    return fn()


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


# ── operator control plane (driven remotely from Discord via the C# listener) ─
def _lifecycle():
    lc = getattr(adapter, "_lifecycle", None)
    if lc is None:
        raise HTTPException(400, "lifecycle not running (PINNACLE_LIFECYCLE=1 required)")
    return lc


class ControlRequest(BaseModel):
    """One shape for every control verb — the C# listener forwards Discord args verbatim."""
    reason: str = "discord"
    minutes: float = 60.0            # force_open duration
    pins: str = ""                   # "09:00-12:00,20:00-23:00" (replaces the set; "" clears)
    key: str = ""                    # toggle name
    value: str = ""                  # toggle value
    # schedule knobs — None = leave unchanged
    lead_min: float | None = None
    trail_min: float | None = None
    min_gap_min: float | None = None
    min_games: int | None = None
    max_blocks: int | None = None
    session_hours: float | None = None
    jitter_min: float | None = None
    horizon_hours: int | None = None
    paired_only: str | None = None
    today_only: str | None = None


@app.get("/control/state")
async def control_state():
    """Everything an operator can change, plus what the bot is doing about it."""
    lc = getattr(adapter, "_lifecycle", None)
    ctl = getattr(adapter, "_control", None)
    guard = getattr(adapter, "_balance_guard", None)
    return {
        "lifecycle": lc.status() if lc is not None else {"state": "not-running"},
        "control": ctl.view() if ctl is not None else None,
        "toggles": ctl.toggle_view() if ctl is not None else None,
        "balance": guard.status() if guard is not None else None,
        "book": adapter.name,
    }


@app.post("/control/pause")
async def control_pause(req: ControlRequest):
    return await _lifecycle().pause(req.reason)


@app.post("/control/resume")
async def control_resume(req: ControlRequest):
    return await _lifecycle().resume()


@app.post("/control/force_open")
async def control_force_open(req: ControlRequest):
    return await _lifecycle().force_open(req.minutes, req.reason)


@app.post("/control/banking")
async def control_banking(req: ControlRequest):
    """Hands-off banking window: open the site in the bot's own Chrome profile and freeze all automation so the
    operator can deposit/withdraw undisturbed. Deliberately opens THROUGH a balance halt — the halt closes the
    browser, and a closed browser is exactly what you need open to refill the account. Auto-reverts to the halt."""
    return await _lifecycle().banking(req.minutes or 30.0)


@app.post("/control/pins")
async def control_pins(req: ControlRequest):
    """Replace the pinned-hours set (empty string clears all pins) and replan immediately."""
    return await _lifecycle().set_pins(req.pins)


@app.post("/control/schedule")
async def control_schedule(req: ControlRequest):
    return await _lifecycle().apply_config(
        lead_min=req.lead_min, trail_min=req.trail_min, min_gap_min=req.min_gap_min,
        min_games=req.min_games, max_blocks=req.max_blocks, session_hours=req.session_hours,
        jitter_min=req.jitter_min, horizon_hours=req.horizon_hours,
        paired_only=req.paired_only, today_only=req.today_only)


@app.post("/control/toggle")
async def control_toggle(req: ControlRequest):
    """Flip a runtime flag. Only flags the SIDECAR re-reads live can take effect immediately; flags the C#
    bot reads once at startup are saved but reported as needing a restart (never silently ignored)."""
    ctl = getattr(adapter, "_control", None)
    if ctl is None:
        raise HTTPException(400, "control state unavailable (PINNACLE_LIFECYCLE=1 required)")
    if not req.key:
        return ctl.toggle_view()
    return ctl.set_toggle(req.key, req.value)


@app.post("/control/balance")
async def control_balance(kalshi_usd: float):
    """The C# bot pushes its Kalshi cash here (the sidecar holds no Kalshi credentials). The guard judges
    BOTH legs in one place and halts the schedule if either floor is breached."""
    guard = getattr(adapter, "_balance_guard", None)
    if guard is None:
        raise HTTPException(400, "balance guard not running (PINNACLE_LIFECYCLE=1 required)")
    guard.push_kalshi(kalshi_usd)
    return await guard.check_now()


@app.get("/debug/schedule")
async def debug_schedule():
    """The lifecycle's work-window plan: every planned session (local + UTC, games, duration, past/NOW/
    upcoming), the current window with minutes-to-close, and the plan's provenance (mode, how many games were
    fetched / kept for today / paired, jitter). The schedule half of "what is the bot doing and why"."""
    lc = getattr(adapter, "_lifecycle", None)
    if lc is None:
        raise HTTPException(400, "lifecycle not running (PINNACLE_LIFECYCLE=1 required)")
    return lc.status()


@app.get("/debug/board_scan")
async def debug_board_scan():
    """SCROLL the board's virtualised list end-to-end and report EVERY league on it, which of ours matched,
    and which paired leagues are absent (those are the ones that legitimately need a dedicated tab). This is
    the check-by-eye answer to 'is the board really covering that league?' — an observation, not an
    inference. Runs on demand; the tab manager does the same scan every PINNACLE_BOARD_SCAN_MIN minutes."""
    fn = getattr(adapter, "board_full_scan", None)
    if not callable(fn):
        raise HTTPException(400, "adapter has no board_full_scan (Pinnacle adapter only)")
    return await fn()


@app.get("/debug/board_dom")
async def debug_board_dom():
    """What the MAIN BOARD is currently showing, and which paired leagues that matched (and how). Answers
    'why does league X still get a dedicated tab when it's right there on the board' with evidence: the raw
    hrefs/texts scanned, the paired league slugs, and the per-league match reason."""
    fn = getattr(adapter, "board_dom_scan", None)
    if not callable(fn):
        raise HTTPException(400, "adapter has no board_dom_scan (Pinnacle adapter only)")
    return await fn()


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
    # None = the wallet could not be READ (pre-login / auth failure), which is NOT the same as 0.00. Serialised
    # as null + readable:false so a caller can tell them apart; the C# client already maps a missing/non-numeric
    # balance to 0 and low-cash-skips, so this stays backward-compatible.
    resp = {"balance": amt, "readable": amt is not None}
    s = _session_state()
    if s is not None and s.get("currency"):
        resp["currency"] = s.get("currency")   # account currency (e.g. EUR) — Kalshi is USD; FX-convert at M1
    fx = getattr(adapter, "_fx", None)
    if fx is not None:
        resp["fx_to_usd"] = fx.rate            # rides along so a caller gets cash + rate in one read
        if amt is not None:
            resp["balance_usd"] = round(amt * fx.rate, 2)
    return resp


@app.get("/fx")
async def fx_rate(refresh: bool = False):
    """LIVE account-currency→USD rate — the number that sizes the book leg. The C# bot polls this instead of
    trusting a hand-set env var (which was found 6.9% stale on 2026-08-06, silently turning hedged arbs into
    directional positions). Falls back to HARDVEN_FX_TO_USD; `stale` says whether the last fetch succeeded."""
    fx = getattr(adapter, "_fx", None)
    if fx is None:
        raise HTTPException(400, "no FX provider on this adapter")
    return await fx.refresh() if refresh else fx.status()


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
