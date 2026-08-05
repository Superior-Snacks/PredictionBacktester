"""
AggregatorAdapter -- composite BookAdapter: prices from a read-only ODDS AGGREGATOR, money through a real
book's adapter (Pinnacle's UI path today).

    odds()                     <- AggregatorClient  (or the inner book, in shadow mode)
    catalog()                  <- inner book        (pairing still needs BOOK-native tokens for the UI to click)
    balance/place_bet/bets     <- inner book        (an aggregator has no account)
    everything else            <- inner book        (via __getattr__: verify_now, session_status, _bet_lock, ...)

The C# bot sees no difference: it still polls GET /odds and gets `price = 1/decimal_odds, size = max_contracts`.
No executor, telemetry, pairing or gate code changes to run this.

TWO MODES (HARDVEN_AGG_MODE)
---------------------------
  shadow (DEFAULT)  Serve the INNER book's own quotes -- byte-for-byte today's behaviour, zero risk -- while
                    fetching the aggregator in parallel and logging both to AggShadow_*.csv. This is how you
                    find out whether a vendor is worth trusting: on the real bot, on the real watched tokens,
                    at the real poll cadence, against a feed you already trust. Run it for a slate, then run
                    analyze_agg_shadow.py.
  live              Serve the AGGREGATOR's quotes. Only after shadow says the agreement and lag are good.

Both modes ALWAYS poll the inner book. That is deliberate and load-bearing: PinnacleAdapter.odds() is what
registers active leagues, triggers the REST seed and starts the WS -- so skipping it would silently kill
ws_verified_map()/verify_now(), i.e. the gate that stops a naked Kalshi leg. Aggregator finds, Pinnacle WS
and the bet-slip popover still confirm.

    HARDVEN_BOOK=aggregator             turn this adapter on
    HARDVEN_AGG_PROVIDER=mock           which vendor client (agg_client.load_client)
    HARDVEN_AGG_PLACEMENT_BOOK=pinnacle which book actually takes the bet
    HARDVEN_AGG_MODE=shadow|live
    HARDVEN_AGG_LIMITS=inner|vendor|assumed   where max_stake comes from in live mode (default inner)
    HARDVEN_AGG_ASSUMED_MAX_STAKE=100.0       used only by HARDVEN_AGG_LIMITS=assumed
    HARDVEN_AGG_TS_POLICY=strict|fetch        what to do when a vendor publishes no per-line update time
    HARDVEN_AGG_SHADOW=1                      write the shadow CSV (default 1)
    HARDVEN_AGG_SHADOW_HEARTBEAT_SEC=300      per-token heartbeat row even when nothing moves
"""
from __future__ import annotations

import asyncio
import csv
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from agg_client import AggQuote, AggregatorClient, load_client
from book_adapter import BetResult, BookAdapter, CatalogEntry, Selection


def _build_inner() -> BookAdapter:
    """The book that actually takes the bet. Same registry shape as app.load_adapter, kept separate so the
    two plug-points stay independent (you can point the aggregator at any book we can place on)."""
    name = (os.environ.get("HARDVEN_AGG_PLACEMENT_BOOK") or "pinnacle").lower()
    if name == "pinnacle":
        from pinnacle_adapter import PinnacleAdapter
        return PinnacleAdapter()
    if name == "bookmaker":
        from bookmaker_adapter import BookmakerAdapter
        return BookmakerAdapter()
    if name == "mock":
        from mock_adapter import MockBookAdapter
        return MockBookAdapter()
    raise ValueError(f"Unknown HARDVEN_AGG_PLACEMENT_BOOK={name!r}")


class AggregatorAdapter(BookAdapter):
    name = "aggregator"

    def __init__(self) -> None:
        self._inner: BookAdapter = _build_inner()
        self._client: AggregatorClient = load_client()
        self._mode = (os.environ.get("HARDVEN_AGG_MODE") or "shadow").lower()
        if self._mode not in ("shadow", "live"):
            raise ValueError(f"HARDVEN_AGG_MODE must be 'shadow' or 'live', got {self._mode!r}")
        self._limits    = (os.environ.get("HARDVEN_AGG_LIMITS") or "inner").lower()
        self._assumed   = float(os.environ.get("HARDVEN_AGG_ASSUMED_MAX_STAKE", "100") or 100)
        self._ts_policy = (os.environ.get("HARDVEN_AGG_TS_POLICY") or "strict").lower()
        self._shadow_on = os.environ.get("HARDVEN_AGG_SHADOW", "1") == "1"
        self._hb_sec    = float(os.environ.get("HARDVEN_AGG_SHADOW_HEARTBEAT_SEC", "300") or 300)

        self._csv_path: Optional[Path] = None
        self._csv_day  = ""
        self._last_row: dict[str, tuple] = {}     # token -> (book_odds, agg_odds, logged_at) for change-only logging
        self._warned_ts = False
        self._warned_limits = False
        self._agg_errors = 0
        self._agg_calls  = 0
        self._last_agg_error = ""

    # ── lifecycle ─────────────────────────────────────────────────────────────
    async def startup(self) -> None:
        await self._inner.startup()
        await self._client.startup()
        print(f"[AGG] mode={self._mode} provider={self._client.name} placement={self._inner.name} "
              f"limits={self._limits} ts_policy={self._ts_policy} shadow_csv={'on' if self._shadow_on else 'off'}")
        if self._mode == "shadow":
            print("[AGG] SHADOW: serving the inner book's quotes (unchanged behaviour); "
                  "aggregator is logged for comparison only.")
        else:
            print("[AGG] LIVE: serving AGGREGATOR quotes to the bot. Inner book still polled "
                  "(keeps the WS warm for verify_now).")

    async def shutdown(self) -> None:
        try:
            await self._client.shutdown()
        finally:
            await self._inner.shutdown()

    # ── odds: the whole point of this class ───────────────────────────────────
    async def odds(self, selection_ids: list[str]) -> dict[str, Selection]:
        # ALWAYS poll the inner book (league registration + WS seed + verify path), and the aggregator in
        # parallel so the shadow comparison is same-instant rather than staggered by a round trip.
        inner_task = asyncio.create_task(self._inner.odds(selection_ids))
        agg_task   = asyncio.create_task(self._safe_quotes(selection_ids))
        inner, agg = await asyncio.gather(inner_task, agg_task)

        try:
            self._client.observe(inner)      # test-instrument hook; real vendors ignore it
        except Exception:
            pass

        if self._shadow_on:
            try:
                self._log_shadow(selection_ids, inner, agg)
            except Exception as e:
                print(f"[AGG] shadow log failed ({type(e).__name__}: {e})")

        if self._mode == "shadow":
            return inner
        return self._to_selections(selection_ids, inner, agg)

    async def _safe_quotes(self, tokens: list[str]) -> dict[str, AggQuote]:
        """A vendor outage must never take the bot down: on error return {} -- which in shadow mode logs a gap
        and in live mode ages every quote out of the C# freshness gate, i.e. the books clear and no arb is
        computed. Failing to 'no opinion' is the only safe direction."""
        self._agg_calls += 1
        try:
            return await self._client.quotes(tokens) or {}
        except Exception as e:
            self._agg_errors += 1
            self._last_agg_error = f"{type(e).__name__}: {e}"
            if self._agg_errors in (1, 5, 25) or self._agg_errors % 100 == 0:
                print(f"[AGG] quotes() failed x{self._agg_errors} ({self._last_agg_error})")
            return {}

    def _to_selections(self, tokens: list[str], inner: dict, agg: dict[str, AggQuote]) -> dict[str, Selection]:
        """Aggregator quote -> the Selection shape the C# feed consumes (LIVE mode only)."""
        out: dict[str, Selection] = {}
        for tok in tokens:
            q = agg.get(tok)
            if q is None or q.decimal_odds <= 0:
                continue                                   # no vendor opinion -> no book -> no arb
            ib = inner.get(tok)

            # max_stake drives max_contracts, which is the DEPTH the stake ladder sizes against. A vendor that
            # publishes no limit cannot be allowed to invent one.
            if self._limits == "vendor":
                max_stake = q.max_stake or 0.0
            elif self._limits == "assumed":
                max_stake = q.max_stake if q.max_stake is not None else self._assumed
            else:                                          # "inner": vendor price, BOOK's real published limit
                max_stake = q.max_stake if q.max_stake is not None else (
                    getattr(ib, "max_stake", 0.0) if ib is not None else 0.0)
            if max_stake <= 0 and not self._warned_limits:
                self._warned_limits = True
                print(f"[AGG] WARNING: no max_stake for {tok} (limits={self._limits}) - depth will read 0 and "
                      "the ladder will not size a bet. Set HARDVEN_AGG_LIMITS=assumed to override.")

            # Freshness: the vendor's own line-update time, never our fetch time. See agg_client docstring.
            ts = q.ts
            if ts <= 0:
                if self._ts_policy == "fetch":
                    ts = q.fetched_ts or time.time()
                    if not self._warned_ts:
                        self._warned_ts = True
                        print("[AGG] WARNING: HARDVEN_AGG_TS_POLICY=fetch - vendor publishes no per-line update "
                              "time, so quotes are stamped at FETCH time. The C# staleness gate can no longer "
                              "detect a frozen vendor line. This is the phantom-arb failure mode.")
                else:
                    if not self._warned_ts:
                        self._warned_ts = True
                        print("[AGG] vendor publishes no per-line update time (ts=0) and TS_POLICY=strict - "
                              "quotes will age out immediately and no arb will be computed. That is the safe "
                              "failure. Set HARDVEN_AGG_TS_POLICY=fetch only if you accept the risk.")

            # cutoff/live: prefer the vendor's, fall back to the book's (which the WS does publish).
            cutoff = q.cutoff or (getattr(ib, "cutoff", 0.0) if ib is not None else 0.0)
            live   = q.live if q.live else bool(getattr(ib, "live", False)) if ib is not None else q.live
            out[tok] = Selection(selection_id=tok, decimal_odds=q.decimal_odds, max_stake=max_stake,
                                 status=q.status, ts=ts, live=live, cutoff=cutoff)
        return out

    # ── shadow tape ───────────────────────────────────────────────────────────
    def _csv(self) -> Optional[Path]:
        day = datetime.now(timezone.utc).strftime("%Y%m%d")
        if self._csv_path is not None and day == self._csv_day:
            return self._csv_path
        path = Path(__file__).parent.parent / f"AggShadow_{day}.csv"
        if not path.exists():
            with path.open("w", newline="", encoding="utf-8") as f:
                csv.writer(f).writerow([
                    "Time", "Token", "Mode", "Provider",
                    "BookOdds", "AggOdds", "DiffPct",
                    "BookTs", "AggTs", "AggAgeSec", "AggChangedAgeSec",
                    "BookLive", "AggLive", "BookStatus", "AggStatus",
                    "BookMaxStake", "AggMaxStake", "Present",
                ])
        self._csv_path, self._csv_day = path, day
        return path

    def _log_shadow(self, tokens: list[str], inner: dict, agg: dict[str, AggQuote]) -> None:
        """Event tape, not a full sample dump: a row per token only when either side MOVED since its last row
        (or on a slow heartbeat). 644 tokens at a 9s poll would be ~6M rows/day logged naively; the analyzer
        only needs the transitions to measure agreement and follow-lag."""
        now  = time.time()
        path = self._csv()
        if path is None:
            return
        rows = []
        for tok in tokens:
            ib, q = inner.get(tok), agg.get(tok)
            if ib is None and q is None:
                continue
            b_odds = round(float(getattr(ib, "decimal_odds", 0.0) or 0.0), 4)
            a_odds = round(float(q.decimal_odds), 4) if q is not None else 0.0
            prev   = self._last_row.get(tok)
            if prev is not None:
                p_b, p_a, p_t = prev
                if b_odds == p_b and a_odds == p_a and (now - p_t) < self._hb_sec:
                    continue
            self._last_row[tok] = (b_odds, a_odds, now)

            diff_pct = round((a_odds / b_odds - 1.0) * 100.0, 4) if (b_odds > 0 and a_odds > 0) else ""
            a_ts     = float(q.ts) if (q is not None and q.ts) else 0.0
            a_chg    = float(getattr(q, "changed_ts", 0.0) or 0.0) if q is not None else 0.0
            present  = ("both" if (ib is not None and q is not None)
                        else "book_only" if ib is not None else "agg_only")
            rows.append([
                datetime.now(timezone.utc).isoformat(timespec="milliseconds"), tok, self._mode,
                self._client.name,
                b_odds or "", a_odds or "", diff_pct,
                round(float(getattr(ib, "ts", 0.0) or 0.0), 3) or "", round(a_ts, 3) or "",
                round(now - a_ts, 3) if a_ts else "",
                round(now - a_chg, 3) if a_chg else "",
                int(bool(getattr(ib, "live", False))) if ib is not None else "",
                int(bool(q.live)) if q is not None else "",
                getattr(ib, "status", "") if ib is not None else "",
                q.status if q is not None else "",
                round(float(getattr(ib, "max_stake", 0.0) or 0.0), 2) if ib is not None else "",
                ("" if q is None or q.max_stake is None else round(float(q.max_stake), 2)),
                present,
            ])
        if rows:
            with path.open("a", newline="", encoding="utf-8") as f:
                csv.writer(f).writerows(rows)

    # ── everything that needs an ACCOUNT goes to the inner book ───────────────
    async def catalog(self) -> list[CatalogEntry]:
        return await self._inner.catalog()

    async def balance(self) -> float:
        return await self._inner.balance()

    async def place_bet(self, selection_id: str, stake: float, max_odds: float) -> BetResult:
        return await self._inner.place_bet(selection_id, stake, max_odds)

    async def open_bets(self) -> list[dict]:
        return await self._inner.open_bets()

    async def bet(self, bet_id: str) -> Optional[dict]:
        return await self._inner.bet(bet_id)

    def session_status(self) -> dict:
        """Explicit (not via __getattr__) so the aggregator's own health rides along on /health and /odds --
        the bot's session-ready gate keeps working off the INNER book's login, which is what still matters."""
        fn = getattr(self._inner, "session_status", None)
        s = dict(fn()) if callable(fn) else {"ready": True}
        s["aggregator"] = {"provider": self._client.name, "mode": self._mode,
                           "calls": self._agg_calls, "errors": self._agg_errors,
                           "last_error": self._last_agg_error, **(self._client.health() or {})}
        return s

    def __getattr__(self, item):
        """Delegate every adapter extra app.py duck-types (verify_now, ws_verified_map, find_bet, _bet_lock,
        _browser, _tab_manager, probe_bet_endpoints, verify_bet_ui, straight_snapshot, ...) to the inner book.
        Only reached for attributes this class does not define, so the overrides above always win."""
        if item.startswith("__") or item == "_inner":
            raise AttributeError(item)
        inner = self.__dict__.get("_inner")
        if inner is None:
            raise AttributeError(item)
        return getattr(inner, item)
