"""
BookAdapter — the contract every sportsbook ("HardVen") integration implements.

This is THE map of what the sidecar must do. Everything site-specific (HTTP/Playwright, auth,
DOM/JSON parsing, bet-slip automation) lives inside an adapter; the FastAPI layer (app.py) and the
C# bot never change when you swap books. Add a book = add one BookAdapter subclass.

Milestones: only `odds()` (+ health) is needed for M0/telemetry. `catalog()` powers automated
pairing. `balance()`/`place_bet()`/`open_bets()`/`bet()` are M1 (live betting).
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, asdict
from typing import Optional


@dataclass
class Selection:
    """A single backable outcome's current price. price = 1/decimal_odds (the bot's per-contract cost)."""
    selection_id: str
    decimal_odds: float          # e.g. 2.50
    max_stake: float             # book's max accepted stake (cash) at these odds
    status: str = "open"         # "open" | "suspended" | "settled"
    ts: float = 0.0              # unix seconds when observed (staleness)
    live: bool = False           # IN-PLAY (live game) vs pre-match — drives the timing model (live=slow, pre=~instant)

    @property
    def implied_price(self) -> float:
        return 1.0 / self.decimal_odds if self.decimal_odds and self.decimal_odds > 0 else 1.0

    @property
    def max_contracts(self) -> float:
        # $1-payout contracts available = max_stake * decimal_odds
        return self.max_stake * self.decimal_odds if self.decimal_odds > 0 else 0.0

    def to_api(self) -> dict:
        d = asdict(self)
        d["implied_price"] = round(self.implied_price, 6)
        d["max_contracts"] = round(self.max_contracts, 4)
        return d


@dataclass
class BetResult:
    accepted: bool
    bet_id: Optional[str] = None
    actual_odds: Optional[float] = None   # odds actually accepted (may differ from requested → slippage)
    stake: Optional[float] = None
    reason: Optional[str] = None          # rejection reason when accepted is False

    def to_api(self) -> dict:
        return asdict(self)


@dataclass
class CatalogEntry:
    """One book selection, for the pairing pipeline to match against Kalshi markets."""
    selection_id: str
    sport: str
    league: str
    event: str
    market: str
    selection_name: str
    start_time: Optional[str] = None      # ISO 8601 UTC; drives the pre-live gate
    three_way: bool = False               # market has a draw/extra outcome → pair NO-only (soccer 1X2 etc.)
    rules_text: Optional[str] = None      # per-market rules, if scrapable (feeds the AI judge)
    rules_url: Optional[str] = None

    def to_api(self) -> dict:
        return asdict(self)


class BookAdapter(ABC):
    """Implement one per sportsbook. `name` identifies it (and selects it via HARDVEN_BOOK env)."""
    name: str = "abstract"

    async def startup(self) -> None:
        """Optional: open session / log in / launch browser. Called once on service start."""
        return None

    async def shutdown(self) -> None:
        """Optional: close session / browser. Called on service stop."""
        return None

    # ── M0: odds (the only thing telemetry needs) ──────────────────────────────
    @abstractmethod
    async def odds(self, selection_ids: list[str]) -> dict[str, Selection]:
        """Current odds for the requested selections. Missing/suspended ids may be omitted."""
        raise NotImplementedError

    # ── Pairing: catalog enumeration ───────────────────────────────────────────
    @abstractmethod
    async def catalog(self) -> list[CatalogEntry]:
        """Full enumerable catalog of selections (for automated pairing). Empty list if unsupported."""
        raise NotImplementedError

    # ── M1: betting + wallet confirmation ──────────────────────────────────────
    @abstractmethod
    async def balance(self) -> float:
        """Account cash balance."""
        raise NotImplementedError

    @abstractmethod
    async def place_bet(self, selection_id: str, stake: float, max_odds: float) -> BetResult:
        """Place a back bet. Accept only if odds <= max_odds. IRREVERSIBLE once accepted."""
        raise NotImplementedError

    @abstractmethod
    async def open_bets(self) -> list[dict]:
        """Currently open (unsettled) bets — the 'wallet' confirmation source."""
        raise NotImplementedError

    @abstractmethod
    async def bet(self, bet_id: str) -> Optional[dict]:
        """Status of a single bet by id (confirmation / settlement)."""
        raise NotImplementedError
