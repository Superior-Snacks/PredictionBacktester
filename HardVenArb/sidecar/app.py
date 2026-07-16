"""
HardVen sidecar — the local "private API" the C# bot calls. Book-agnostic FastAPI layer over a
pluggable BookAdapter. The bot is identical across venues; only the adapter changes per sportsbook.

Run:   HARDVEN_BOOK=mock uvicorn app:app --port 8787      (run from this sidecar/ directory)
Test:  curl "http://127.0.0.1:8787/odds?selections=MOCK_NBA_FINALS_SAS,MOCK_NBA_FINALS_NYK"

Select the book with the HARDVEN_BOOK env var (default "mock"). Add a real book by writing a new
BookAdapter subclass and registering it in load_adapter() below.
"""
from __future__ import annotations

import logging
import os
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel

from book_adapter import BookAdapter
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


# ── M0: odds (the only endpoint telemetry needs) ──────────────────────────────
@app.get("/odds")
async def odds(selections: str = Query(..., description="comma-separated selection ids")):
    ids = [s for s in (x.strip() for x in selections.split(",")) if s]
    if not ids:
        raise HTTPException(400, "no selections")
    result = await adapter.odds(ids)
    resp = {"selections": {sid: sel.to_api() for sid, sel in result.items()}, "ts": time.time()}
    s = _session_state()
    if s is not None:
        resp["session_ready"] = bool(s.get("ready", True))   # rides along so the C# /odds poll sees readiness
        if "scheduled_dark" in s:
            resp["scheduled_dark"] = bool(s.get("scheduled_dark"))   # planned close (no alert) vs unexpected logout
    return resp


# ── Pairing: catalog ──────────────────────────────────────────────────────────
@app.get("/catalog")
async def catalog():
    return {"selections": [c.to_api() for c in await adapter.catalog()]}


@app.get("/debug/reader")
async def debug_reader(ttl: float = 30.0):
    """Coverage diagnostic: the matchups ('lid:mid') the browser-WS reader has actually pushed odds for within
    `ttl`s. Used by coverage_check.py to compare the reader's live slate against the guest board (ground truth)."""
    fn = getattr(adapter, "reader_live_mids", None)
    mids = fn(ttl) if fn else []
    return {"live_mids": mids, "count": len(mids)}


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
    return (await adapter.place_bet(req.selection_id, req.stake, req.max_odds)).to_api()


@app.get("/bets/open")
async def open_bets():
    return {"bets": await adapter.open_bets()}


@app.get("/bets/{bet_id}")
async def get_bet(bet_id: str):
    b = await adapter.bet(bet_id)
    if b is None:
        raise HTTPException(404, "bet not found")
    return b
