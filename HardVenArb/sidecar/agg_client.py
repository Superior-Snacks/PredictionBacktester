"""
AggregatorClient -- the contract a read-only ODDS AGGREGATOR implements.

This is the SECOND plug-point in the sidecar, and it is deliberately narrower than BookAdapter:

    BookAdapter      = a venue we can BET at        (odds + catalog + balance + place_bet + bets)
    AggregatorClient = a feed we can only READ      (quotes, and nothing else)

The split exists because the target architecture is "aggregator finds the arb, the book's UI places it".
AggregatorAdapter (aggregator_adapter.py) composes the two: quotes come from here, money moves through a
BookAdapter. Adding a vendor = one AggregatorClient subclass; nothing else in the sidecar or the C# bot moves.

TOKEN SPACE (the thing that actually costs work per vendor)
----------------------------------------------------------
The bot's canonical id is the BOOK's native token -- for Pinnacle, `leagueId:matchupId:home|away`. That is
what the UI clicks and what cross_pairs.json pairs against Kalshi, so it cannot be replaced by a vendor id.
A vendor therefore introduces a THIRD id space and a join:

    kalshi_ticker  <->  BOOK token (`221309:1633332341:home`)  <->  vendor key

`quotes()` is asked for BOOK tokens and must return quotes keyed by those same BOOK tokens. How a vendor
gets there is its own business: some expose book-native ids directly (cheap), most need an explicit map
(`agg_map.json`, see `load_token_map`). Resolve it inside the client so the adapter stays dumb.

FRESHNESS (read this before writing a vendor client)
---------------------------------------------------
`AggQuote.ts` MUST be the vendor's own last-update time for that line, NOT when we fetched it. The C# feed
gates on ts (HARDVEN_QUOTE_MAX_AGE_MS, default 30s) and clears the book when a quote ages out -- that gate is
the only thing standing between a stale line and a phantom arb. Stamping fetch time makes every quote look
permanently fresh and defeats it. If a vendor does not publish a per-line update time, say so by leaving ts
at 0.0 and let `stale_ts_policy` decide -- do not paper over it.
"""
from __future__ import annotations

import json
import os
import time
from abc import ABC, abstractmethod
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class AggQuote:
    """One selection's current price as the aggregator reports it.

    Mirrors book_adapter.Selection, minus the fields only a real account can know. `max_stake` and `cutoff`
    are Optional/0 on purpose: most read-only aggregators publish neither, and the adapter needs to be able
    to tell "the vendor says the limit is 500" from "the vendor said nothing and we guessed"."""
    token: str                          # BOOK-native token (what the bot and the UI use)
    decimal_odds: float
    book: str = ""                      # which sportsbook this line is FROM (vendor's book name)
    max_stake: Optional[float] = None   # None = vendor does not publish limits (very common)
    ts: float = 0.0                     # VENDOR's last-update time for this line; 0.0 = not published
    fetched_ts: float = 0.0             # when WE received it (diagnostics only -- never the freshness stamp)
    live: bool = False                  # in-play
    status: str = "open"                # "open" | "suspended"
    cutoff: float = 0.0                 # betting-close unix secs; 0 = unknown


class AggregatorClient(ABC):
    """Implement one per aggregator vendor. `name` identifies it (and selects it via HARDVEN_AGG_PROVIDER)."""
    name: str = "abstract"

    async def startup(self) -> None:
        """Optional: open session / authenticate / start a push subscription."""
        return None

    async def shutdown(self) -> None:
        return None

    @abstractmethod
    async def quotes(self, tokens: list[str]) -> dict[str, AggQuote]:
        """Current quotes for these BOOK-native tokens, keyed by the same tokens. Omit what you cannot
        resolve -- a missing token is read as "no aggregator opinion", which is safe; a WRONG token is not."""
        raise NotImplementedError

    def observe(self, truth: dict) -> None:
        """Optional hook: the composite adapter hands the INNER BOOK's own quotes here every poll
        ({token: book_adapter.Selection}). Real vendors ignore it -- it exists so the mock can mirror real
        prices with an injected lag, which is how the shadow analyzer gets validated against a known answer."""
        return None

    def health(self) -> dict:
        """Vendor-side diagnostics surfaced on /health (call counts, rate-limit headroom, last error)."""
        return {}

    # ── token mapping helper (shared by vendors that need an explicit join) ────
    @staticmethod
    def load_token_map(path: str = "") -> dict:
        """{book_token: vendor_key} from agg_map.json (sidecar dir by default). Missing file = {} so a vendor
        that resolves natively needs no map. Built by whatever pairing step the vendor allows."""
        p = Path(path) if path else Path(__file__).parent / "agg_map.json"
        try:
            m = json.loads(p.read_text(encoding="utf-8"))
            return m if isinstance(m, dict) else {}
        except FileNotFoundError:
            return {}
        except Exception as e:
            print(f"[AGG] token map {p.name} unreadable ({type(e).__name__}: {e}) - continuing with no map")
            return {}


class MockAggregatorClient(AggregatorClient):
    """Test instrument, not a simulation.

    It MIRRORS the inner book's real quotes (fed via `observe`) after a configurable delay, optionally with
    price noise and dropouts. That makes the answer KNOWN: inject 20s of lag, run shadow mode, and the
    analyzer must report ~20s. Until it does, a lag number measured against a real vendor means nothing.

        HARDVEN_AGG_MOCK_LAG_SEC    seconds to delay the mirror        (default 0)
        HARDVEN_AGG_MOCK_NOISE      +/- fraction of odds jitter        (default 0)
        HARDVEN_AGG_MOCK_DROP       fraction of tokens to omit         (default 0)
        HARDVEN_AGG_MOCK_NO_LIMITS  1 = report max_stake=None          (default 1, the common vendor case)
        HARDVEN_AGG_MOCK_NO_TS      1 = report ts=0 (no vendor clock)  (default 0)
    """
    name = "mock"

    def __init__(self) -> None:
        self._lag       = float(os.environ.get("HARDVEN_AGG_MOCK_LAG_SEC", "0") or 0)
        self._noise     = float(os.environ.get("HARDVEN_AGG_MOCK_NOISE", "0") or 0)
        self._drop      = float(os.environ.get("HARDVEN_AGG_MOCK_DROP", "0") or 0)
        self._no_limits = os.environ.get("HARDVEN_AGG_MOCK_NO_LIMITS", "1") == "1"
        self._no_ts     = os.environ.get("HARDVEN_AGG_MOCK_NO_TS", "0") == "1"
        # token -> deque[(observed_at, odds, live, status, cutoff)]; the lag is served out of this history
        self._hist: dict[str, deque] = {}
        self._calls = 0

    def observe(self, truth: dict) -> None:
        now = time.time()
        for tok, sel in (truth or {}).items():
            odds = getattr(sel, "decimal_odds", 0.0) or 0.0
            if odds <= 0:
                continue
            h = self._hist.get(tok)
            if h is None:
                h = self._hist[tok] = deque(maxlen=512)
            h.append((now, odds, bool(getattr(sel, "live", False)),
                      getattr(sel, "status", "open"), float(getattr(sel, "cutoff", 0.0) or 0.0)))
            # Trim anything older than the lag window plus slack -- unbounded history would grow per token.
            cut = now - (self._lag + 120.0)
            while h and h[0][0] < cut:
                h.popleft()

    async def quotes(self, tokens: list[str]) -> dict[str, AggQuote]:
        self._calls += 1
        now  = time.time()
        want = now - self._lag
        out: dict[str, AggQuote] = {}
        for i, tok in enumerate(tokens):
            h = self._hist.get(tok)
            if not h:
                continue                                  # never observed -> no vendor opinion (honest)
            if self._drop > 0 and ((i * 2654435761) % 1000) / 1000.0 < self._drop:
                continue                                  # deterministic per-token dropout
            # newest sample at or before `want`; if the history does not reach back that far, stay silent
            # rather than serving a too-fresh price -- that would understate the lag we are trying to measure.
            pick = None
            for rec in reversed(h):
                if rec[0] <= want:
                    pick = rec
                    break
            if pick is None:
                continue
            obs_ts, odds, live, status, cutoff = pick
            if self._noise > 0:
                odds = round(odds * (1.0 + ((((i * 40503) % 2001) - 1000) / 1000.0) * self._noise), 4)
            out[tok] = AggQuote(
                token=tok, decimal_odds=odds, book="mock",
                max_stake=None if self._no_limits else 500.0,
                ts=0.0 if self._no_ts else obs_ts,        # vendor clock = when the mock "saw" it
                fetched_ts=now, live=live, status=status, cutoff=cutoff,
            )
        return out

    def health(self) -> dict:
        return {"calls": self._calls, "tracked_tokens": len(self._hist),
                "lag_sec": self._lag, "noise": self._noise, "drop": self._drop}


def load_client() -> AggregatorClient:
    """Pick the aggregator vendor via HARDVEN_AGG_PROVIDER (default "mock"). Register new vendors here."""
    name = (os.environ.get("HARDVEN_AGG_PROVIDER") or "mock").lower()
    if name == "mock":
        return MockAggregatorClient()
    # Register vendors as they are built, e.g.:
    #   if name == "oddsjam":   from agg_oddsjam import OddsJamClient;     return OddsJamClient()
    #   if name == "opticodds": from agg_opticodds import OpticOddsClient; return OpticOddsClient()
    #   if name == "theodds":   from agg_theodds import TheOddsApiClient;  return TheOddsApiClient()
    raise ValueError(f"Unknown HARDVEN_AGG_PROVIDER={name!r} (no aggregator client registered)")
