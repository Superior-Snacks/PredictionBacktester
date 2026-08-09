"""
BetInAsiaAdapter — second venue behind the BookAdapter seam.

STATUS: M0 (odds + catalog). Betting is Phase 3 and every M1 method refuses loudly rather than
guessing; the placement contract has never been captured (no bet was placed during the 2026-08-05
recon), so there is nothing to implement yet and a plausible-looking guess is worse than a refusal.

Nothing outside this file changes to add this book: the C# bot, executor, lifecycle, scheduler,
balance guard, telemetry and analyzer are all book-agnostic by construction.

MARKET TAXONOMY (derived from the recon, 1440 offers_hcap market entries across 16 sports)
------------------------------------------------------------------------------------------
BetInAsia uses three different moneyline spellings depending on sport family:

    tennis_match,all      tennis           selections p1 / p2
    ml                    mma, boxing, basket                a / h
    time_win,tp,all,ml    baseball, af, ih, cricket, esports, rl, darts, snooker, volley, arf   a / h

and marks 3-way (draw) markets with `wdw`:

    wdw                   fb (soccer)
    time_win,tp,reg,wdw   ih, cricket, ru

Everything else is a DERIVATIVE (ah, ahou, tahou, cs, clean, gr, dc, game_win, tennis_match,1|2 …)
and stays telemetry-only while HARDVEN_MONEYLINE_ONLY=1.

DELIBERATE EXCLUSION — `time_win,tp,reg,ml`: `reg` means REGULATION ONLY, whereas `all` includes
overtime. Kalshi settles on the final result, so the two are NOT the same market and pairing them
would be a silent mis-hedge. It is classified as a derivative, not a moneyline.
"""
from __future__ import annotations

import os
import time
from typing import Optional

from book_adapter import BetResult, BookAdapter, CatalogEntry, Selection
from betinasia_ws import BetInAsiaFeed

# ── The max_stake problem ─────────────────────────────────────────────────────
MAX_STAKE_NOTE = """
BetInAsia publishes NO per-selection stake limit. The price frames carry only [line, [[sel, odds]]],
and the account fields that look like limits are not usable as one:

    credit_limit          ["USD", 0.0]              (no credit line)
    max_stake_per_event   null
    settings.max_order    999999999999999999        (a sentinel, not a limit)
    max_betslips          8                         (slip COUNT, not stake)

Selection.max_stake feeds max_contracts, which feeds StakeLadder.MaxDepthFraction (never bet more
than 1/3 of book max) and the executor's depth gate. Letting the 1e18 sentinel through would not
"disable a limit" -- it would silently DELETE a sizing safety gate while everything still looked
healthy, which is the same failure shape as the balance()-returns-0.0 bug and the phantom-fee bug.

So we substitute an explicit, conservative assumption: BIA_ASSUMED_MAX_STAKE (default 100.0, account
currency). It is announced at startup and reported through /health so it can never be a silent
default. Replace it the moment Phase 0 captures a real limit from a bet-slip/quote call.
"""

ASSUMED_MAX_STAKE = float(os.environ.get("BIA_ASSUMED_MAX_STAKE", "100.0"))

MONEYLINE_KEYS = {"tennis_match,all", "ml", "time_win,tp,all,ml"}
THREE_WAY_KEYS = {"wdw", "time_win,tp,reg,wdw", "time_win,tp,all,wdw"}

# ── Feed market_key -> order bet_type ─────────────────────────────────────────
# The PRICE feed and the ORDER api speak different vocabularies, and the translation is a lookup
# table, not a mechanical transform -- two of the four observed pairs would break any rule you could
# write (`ml` and `time_win,tp,all,ml` pass their tail through, `tennis_match,all` and `wdw` do not):
#
#   sport     feed market_key        POST /v1/betslips/ bet_type       (observed 2026-08-09)
#   tennis    tennis_match,all       for,tset,all,vwhatever,p2
#   basket    ml                     for,ml,h
#   baseball  time_win,tp,all,ml     for,tp,all,ml,a
#   fb        wdw                    for,h                             (middle segment is EMPTY)
#
# Shape is  "for," + <infix> + "," + <selection>, with the selection token passed through unchanged
# (p1/p2 for tennis, h/a elsewhere, presumably d for a soccer draw -- not yet observed).
# `for` = back. The feed also supports `against` (lay) -- `against,tset,all,vset1,p1` was observed --
# i.e. the same economic position from the other side. We only ever back, so `for` is hard-coded.
#
# CONFIRMATION OF AN EARLIER CALL: baseball's slip came back described as "Cleveland Guardians
# Moneyline (Inc. Overtime)", so `tp,all,ml` really does include overtime. That is why
# `time_win,tp,reg,ml` (regulation only) stays classified as a derivative -- Kalshi settles on the
# final result, so pairing the `reg` variant would be a silent mis-hedge.
BET_TYPE_INFIX = {
    "tennis_match,all":   "tset,all,vwhatever",
    "ml":                 "ml",
    "time_win,tp,all,ml": "tp,all,ml",
    "wdw":                "",              # soccer 1X2: no infix at all
}


def make_bet_type(market_key: str, selection: str) -> Optional[str]:
    """Feed market_key + selection -> the `bet_type` string POST /v1/betslips/ expects.

    None for any market we have not observed a slip for: sending a guessed bet_type would either be
    rejected or -- worse -- accepted as a DIFFERENT market than the one we priced.
    """
    if market_key not in BET_TYPE_INFIX:
        return None
    infix = BET_TYPE_INFIX[market_key]
    return f"for,{infix},{selection}" if infix else f"for,{selection}"

# Selection tokens that mean "the home/first side" and "the away/second side" respectively.
HOME_SELECTIONS = {"h", "p1"}
AWAY_SELECTIONS = {"a", "p2"}

# Which moneyline spelling each sport uses. Inverted from MONEYLINE_KEYS + the observed per-sport
# census, so catalog() can name a game's moneyline BEFORE any price has been seen. A sport missing
# from this map is skipped rather than guessed -- an invented market key pairs a leg we cannot price.
MONEYLINE_BY_SPORT = {
    "tennis":   "tennis_match,all",
    "basket":   "ml",
    "mma":      "ml",
    "boxing":   "ml",
    "fb":       "wdw",                  # soccer is 3-way
    "baseball": "time_win,tp,all,ml",
    "af":       "time_win,tp,all,ml",
    "ih":       "time_win,tp,all,ml",
    "cricket":  "time_win,tp,all,ml",
    "esports":  "time_win,tp,all,ml",
    "rl":       "time_win,tp,all,ml",
    "darts":    "time_win,tp,all,ml",
    "snooker":  "time_win,tp,all,ml",
    "volley":   "time_win,tp,all,ml",
    "arf":      "time_win,tp,all,ml",
}

# Tennis names its sides p1/p2; every other sport observed uses h/a.
TWO_WAY_SELECTIONS = {"tennis": ("p1", "p2")}
THREE_WAY_SELECTIONS = ("h", "d", "a")   # soccer 1X2; `d` inferred, not yet observed on a slip


def is_moneyline(market_key: str) -> bool:
    return market_key in MONEYLINE_KEYS


def is_three_way(market_key: str) -> bool:
    return market_key in THREE_WAY_KEYS


def make_selection_id(sport: str, comp_id: int | str, event_key: str,
                      market_key: str, selection: str) -> str:
    """`{sport}:{comp_id}:{event_key}:{market_key}:{selection}`

    Colon-separated and safe: no sport, event key, market key or selection token observed in the
    recon contains a colon (event/market keys use commas). comp_id is carried INSIDE the id on
    purpose -- watch_hcaps needs it to subscribe, so odds() stays self-sufficient without a catalog
    round-trip, mirroring how the Pinnacle ids carry their league id for rove-nav.
    """
    return f"{sport}:{comp_id}:{event_key}:{market_key}:{selection}"


def parse_selection_id(sid: str) -> Optional[tuple[str, str, str, str, str]]:
    parts = sid.split(":")
    if len(parts) != 5:
        return None
    return parts[0], parts[1], parts[2], parts[3], parts[4]


class BetInAsiaAdapter(BookAdapter):
    name = "betinasia"

    def __init__(self) -> None:
        self.feed = BetInAsiaFeed()
        self._started = False

    # ── lifecycle ─────────────────────────────────────────────────────────────
    async def startup(self) -> None:
        print(f"[BIA] assumed max_stake = {ASSUMED_MAX_STAKE:.2f} "
              f"(NO per-selection limit is published -- see MAX_STAKE_NOTE)", flush=True)
        await self.feed.login()
        await self.feed.start()
        self._started = True
        if self.feed.currency != "USD":
            # Kalshi is USD. A USD BetInAsia account means the whole FX path collapses to identity;
            # anything else must go through the existing fx layer, so say so loudly.
            print(f"[BIA] WARNING account currency is {self.feed.currency}, not USD - "
                  f"HARDVEN_FX_TO_USD must be set", flush=True)

    async def shutdown(self) -> None:
        await self.feed.stop()
        self._started = False

    # ── M0: odds ──────────────────────────────────────────────────────────────
    async def odds(self, selection_ids: list[str]) -> dict[str, Selection]:
        # Subscribe anything we are being asked about but do not yet watch. The first call for a new
        # selection therefore returns nothing and the next poll (~2s later) has prices.
        want: list[tuple[int, str, str]] = []
        for sid in selection_ids:
            p = parse_selection_id(sid)
            if not p:
                continue
            sport, comp_id, ekey, _mk, _sel = p
            try:
                want.append((int(comp_id), sport, ekey))
            except ValueError:
                continue
        if want:
            await self.feed.watch(want)

        out: dict[str, Selection] = {}
        for sid in selection_ids:
            p = parse_selection_id(sid)
            if not p:
                continue
            sport, _comp, ekey, market_key, sel = p
            got = self.feed.get_market(sport, ekey, market_key)
            if not got:
                continue
            _line, sels, ts = got
            price = sels.get(sel)
            if not price or price <= 1.0:
                # decimal odds <= 1.0 pay nothing; treat as unpriced rather than emit a bogus price
                continue
            ev = self.feed.get_event(sport, ekey) or {}
            start = _start_ts_epoch(ev.get("start_ts"))
            out[sid] = Selection(
                selection_id=sid,
                decimal_odds=float(price),
                max_stake=ASSUMED_MAX_STAKE,
                status="open",
                ts=ts,
                live=_is_live(ev, start),
                cutoff=start,
            )
        return out

    # ── Pairing: catalog ──────────────────────────────────────────────────────
    async def catalog(self) -> list[CatalogEntry]:
        """Built from the WS `event` frames, NOT from observed prices.

        The feed pushes its whole catalog unprompted on connect -- 87 tennis events with team names,
        competition and start time, before a single `watch_hcaps` -- whereas prices only exist for
        what we have already subscribed. Requiring a price here would be circular: the pairer needs
        the game list in order to decide what is worth subscribing to.

        The moneyline market key is therefore SYNTHESISED from the sport (see MONEYLINE_BY_SPORT)
        rather than read off an observed book. That is safe because the key is a property of the
        sport, not of the event -- all three spellings are pinned by tests.

        `/v1/events/{user}/suggested/` is the REST equivalent, but it returns ids only with no team
        names, so it is used to DRIVE subscriptions (`feed.watch_sport`) rather than to build this.
        """
        entries: list[CatalogEntry] = []
        for (sport, ekey), ev in self.feed.all_events().items():
            market_key = MONEYLINE_BY_SPORT.get(sport)
            if not market_key:
                continue          # sport whose moneyline spelling we have not pinned -- skip, never guess
            if ev.get("event_type") == "multirunner":
                continue          # outrights: not a 2-way market, priced via offers_event
            home, away = _sides(ev)
            if not home or not away:
                continue          # no teams -> nothing a pairer could match against Kalshi
            comp_id = ev.get("competition_id")
            if comp_id is None:
                book = self.feed._books.get((sport, ekey)) or {}
                comp_id = book.get("comp_id", 0)
            three = is_three_way(market_key)
            for sel in (THREE_WAY_SELECTIONS if three else TWO_WAY_SELECTIONS.get(sport, ("h", "a"))):
                entries.append(CatalogEntry(
                    selection_id=make_selection_id(sport, comp_id, ekey, market_key, sel),
                    sport=sport,
                    league=ev.get("competition_name") or "",
                    event=ev.get("event_name") or f"{home} vs. {away}",
                    market=market_key,
                    selection_name=_selection_name(sel, home, away),
                    start_time=ev.get("start_ts"),
                    three_way=three,
                ))
        return entries

    async def watch_sport(self, sport: str, limit: int = 500) -> int:
        """Subscribe a whole sport's upcoming events via the REST event list. Telemetry entry point."""
        return await self.feed.watch_sport(sport, limit=limit)

    # ── M1: not built (Phase 3) ───────────────────────────────────────────────
    async def balance(self) -> Optional[float]:
        """None = UNREADABLE, which is the correct answer today: the balance endpoint has not been
        captured yet (the recon never logged a funded session's wallet call). Returning 0.0 here
        would trip BalanceGuard into halting a funded account -- the exact bug that bit Pinnacle
        twice. BalanceGuard keeps None as UNKNOWN and does not halt."""
        return None

    async def place_bet(self, selection_id: str, stake: float, max_odds: float) -> BetResult:
        """NOT IMPLEMENTED, and deliberately not forced into this signature yet.

        The BookAdapter contract says "IRREVERSIBLE once accepted", which is true of Pinnacle: you
        click, and you are on. BetInAsia does not work that way -- the 2026-08-09 capture shows an
        order RESTING:

            POST /v1/orders/  {..., "duration": 259200}   -> status "open",
                                                             bet_bar_values.unplaced ["USD", 5.0]
            DELETE /v1/betslips/{id}/                     -> cancel while open

        so a book leg may fill later, partially, or never, and can be pulled. That is closer to the
        analyzer's `--hardven-first` model (an unfilled leg cancelled = a free miss) than to the
        Kalshi-first hedge race, and it is strictly SAFER -- but it is an executor-level change, not
        an adapter detail. Partial fills are real, not theoretical: the captured order asked for
        USD 5.0 and got USD 4.994, a residual the integer Kalshi leg cannot match.

        Implementing this signature as if it were immediate-and-irreversible would report a resting
        order as a completed hedge. Refusing is the honest answer until the execution model is
        settled.
        """
        return BetResult(accepted=False,
                         reason="betinasia placement not implemented (Phase 3) - orders REST and "
                                "partially fill, which the immediate/irreversible place_bet "
                                "contract cannot express")

    async def open_bets(self) -> list[dict]:
        return []

    async def bet(self, bet_id: str) -> Optional[dict]:
        return None

    # ── diagnostics ───────────────────────────────────────────────────────────
    def health(self) -> dict:
        s = self.feed.stats()
        s.update({"book": self.name, "currency": self.feed.currency,
                  "assumed_max_stake": ASSUMED_MAX_STAKE,
                  "can_place_bets": self.feed.can_place_bets,
                  "betting_implemented": False})
        return s


# ── helpers ───────────────────────────────────────────────────────────────────
def _sides(ev: dict) -> tuple[str, str]:
    """Team names. Two observed event shapes: `home`/`away` strings for normal matches, and a
    `teams: [{team_id, name}]` array for multirunner/outright events."""
    home = ev.get("home") or ""
    away = ev.get("away") or ""
    if home or away:
        return home, away
    teams = ev.get("teams") or []
    names = [t.get("name", "") for t in teams if isinstance(t, dict)]
    return (names[0] if len(names) > 0 else "", names[1] if len(names) > 1 else "")


def _selection_name(sel: str, home: str, away: str) -> str:
    if sel in HOME_SELECTIONS and home:
        return home
    if sel in AWAY_SELECTIONS and away:
        return away
    return sel


def _is_live(ev: dict, start_epoch: float) -> bool:
    """IN-PLAY if the feed says so OR the scheduled start has passed. Either signal is enough.

    Do NOT trust `ir_status` alone. Across a 7.4-minute capture the feed produced 13 in-play ->
    pre-live transitions (matches ending) and ZERO pre-live -> in-play ones: we have no evidence it
    announces a kickoff at all. Believing the flag on its own is the same failure that produced the
    Pinnacle in-play tag bug -- a game goes live, nothing updates, and a PRELIVE_ONLY bot fires into a
    live book with live latency and a moving price.

    `start_ts` makes this deterministic instead of hopeful: the schedule is known in advance, so
    liveness never depends on the venue volunteering anything. Wrong in the safe direction too --
    tennis routinely starts late, so `now >= start_ts` can call a still-pre-match game live, and a
    pre-live-only bot then declines an arb it could have taken. A skipped edge costs nothing; a naked
    leg into a live book costs money.
    """
    if ev.get("ir_status"):
        return True
    return bool(start_epoch) and time.time() >= start_epoch


def _start_ts_epoch(start_ts: Optional[str]) -> float:
    """ISO-8601 `2026-08-05T18:30:00Z` -> unix seconds. 0 when absent/unparseable (= unknown)."""
    if not start_ts:
        return 0.0
    try:
        from datetime import datetime, timezone
        return datetime.fromisoformat(str(start_ts).replace("Z", "+00:00")) \
                       .replace(tzinfo=timezone.utc).timestamp()
    except Exception:
        return 0.0
