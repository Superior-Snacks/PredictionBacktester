"""
MockBookAdapter — synthetic sportsbook so the whole pipeline (sidecar → C# feed → telemetry) is
testable WITHOUT a real book. Returns plausible, slightly-drifting odds for ANY requested selection id,
and simulates a fundable account + bet placement. Swap for a real adapter when you pick a book.

Force an arb while testing: set HARDVEN_MOCK_ODDS="<selection_id>=1.05,<id2>=10.0" to pin odds
(1.05 → implied price ~0.95; pair it against a cheap Kalshi leg to make kAsk + 1/O < 1).
"""
from __future__ import annotations

import hashlib
import os
import random
import time
import uuid

from book_adapter import BookAdapter, BetResult, CatalogEntry, Selection


def _seeded_base_odds(selection_id: str) -> float:
    """Deterministic 'fair' decimal odds per id (1.2–6.0) so each selection has a stable center."""
    h = int(hashlib.sha256(selection_id.encode()).hexdigest(), 16)
    return 1.2 + (h % 4800) / 1000.0  # 1.200 .. 6.000


class MockBookAdapter(BookAdapter):
    name = "mock"

    def __init__(self) -> None:
        self._balance = float(os.environ.get("HARDVEN_MOCK_BALANCE", "500"))
        self._bets: dict[str, dict] = {}
        self._pinned: dict[str, float] = {}
        for kv in os.environ.get("HARDVEN_MOCK_ODDS", "").split(","):
            if "=" in kv:
                k, v = kv.split("=", 1)
                try: self._pinned[k.strip()] = float(v)
                except ValueError: pass

    async def odds(self, selection_ids: list[str]) -> dict[str, Selection]:
        now = time.time()
        out: dict[str, Selection] = {}
        for sid in selection_ids:
            if sid in self._pinned:
                o = self._pinned[sid]
            else:
                base = _seeded_base_odds(sid)
                o = round(base * random.uniform(0.98, 1.02), 2)  # ±2% jitter each poll
            out[sid] = Selection(selection_id=sid, decimal_odds=o, max_stake=500.0, status="open", ts=now)
        return out

    async def catalog(self) -> list[CatalogEntry]:
        # A couple of fake entries so the pairing pipeline has something to enumerate.
        return [
            CatalogEntry(
                selection_id="MOCK_NBA_FINALS_SAS", sport="Basketball", league="NBA",
                event="2026 NBA Finals — Winner", market="Outright Winner",
                selection_name="San Antonio Spurs",
                start_time="2026-06-01T00:00:00Z",
                rules_text="Settled on the official NBA Finals champion.", rules_url=None,
            ),
            CatalogEntry(
                selection_id="MOCK_NBA_FINALS_NYK", sport="Basketball", league="NBA",
                event="2026 NBA Finals — Winner", market="Outright Winner",
                selection_name="New York Knicks",
                start_time="2026-06-01T00:00:00Z",
                rules_text="Settled on the official NBA Finals champion.", rules_url=None,
            ),
        ]

    async def balance(self) -> float:
        return round(self._balance, 2)

    async def place_bet(self, selection_id: str, stake: float, max_odds: float) -> BetResult:
        cur = (await self.odds([selection_id]))[selection_id]
        if cur.status != "open":
            return BetResult(accepted=False, reason="market suspended")
        if cur.decimal_odds > max_odds:
            return BetResult(accepted=False, actual_odds=cur.decimal_odds, reason="odds moved above max_odds")
        if stake > self._balance:
            return BetResult(accepted=False, reason="insufficient balance")
        bet_id = uuid.uuid4().hex[:12]
        self._balance -= stake
        self._bets[bet_id] = {
            "bet_id": bet_id, "selection_id": selection_id, "stake": stake,
            "odds": cur.decimal_odds, "status": "open",
            "potential_return": round(stake * cur.decimal_odds, 2), "placed_ts": time.time(),
        }
        return BetResult(accepted=True, bet_id=bet_id, actual_odds=cur.decimal_odds, stake=stake)

    async def open_bets(self) -> list[dict]:
        return [b for b in self._bets.values() if b["status"] == "open"]

    async def bet(self, bet_id: str):
        return self._bets.get(bet_id)
