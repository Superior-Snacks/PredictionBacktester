"""
HardVen sidecar — the local "private API" the C# bot calls. Book-agnostic FastAPI layer over a
pluggable BookAdapter. The bot is identical across venues; only the adapter changes per sportsbook.

Run:   HARDVEN_BOOK=mock uvicorn app:app --port 8787      (run from this sidecar/ directory)
Test:  curl "http://127.0.0.1:8787/odds?selections=MOCK_NBA_FINALS_SAS,MOCK_NBA_FINALS_NYK"

Select the book with the HARDVEN_BOOK env var (default "mock"). Add a real book by writing a new
BookAdapter subclass and registering it in load_adapter() below.
"""
from __future__ import annotations

import os
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel

from book_adapter import BookAdapter
from mock_adapter import MockBookAdapter


def load_adapter() -> BookAdapter:
    name = os.environ.get("HARDVEN_BOOK", "mock").lower()
    if name == "mock":
        return MockBookAdapter()
    if name == "bookmaker":
        from bookmaker_adapter import BookmakerAdapter   # lazy: only needs Playwright when selected
        return BookmakerAdapter()
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


@app.get("/health")
async def health():
    return {"ok": True, "book": adapter.name, "ts": time.time()}


# ── M0: odds (the only endpoint telemetry needs) ──────────────────────────────
@app.get("/odds")
async def odds(selections: str = Query(..., description="comma-separated selection ids")):
    ids = [s for s in (x.strip() for x in selections.split(",")) if s]
    if not ids:
        raise HTTPException(400, "no selections")
    result = await adapter.odds(ids)
    return {"selections": {sid: sel.to_api() for sid, sel in result.items()}, "ts": time.time()}


# ── Pairing: catalog ──────────────────────────────────────────────────────────
@app.get("/catalog")
async def catalog():
    return {"selections": [c.to_api() for c in await adapter.catalog()]}


# ── M1: betting + wallet confirmation ─────────────────────────────────────────
@app.get("/balance")
async def balance():
    return {"balance": await adapter.balance()}


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
