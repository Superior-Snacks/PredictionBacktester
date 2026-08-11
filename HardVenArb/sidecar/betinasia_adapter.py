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

# Sport page to deep-link on startup. NOT "/" -- that subscribes only the featured tournament.
# Default sport page. Football is the venue's biggest book by far (825 matches vs 75 tennis) and
# Kalshi carries ~342 soccer ties across 18 series, so this is where the pair count comes from.
# NOTE: one page load covers tennis completely (75/75) but only ~21% of football (161 of 754) --
# soccer needs a pass through the league pages, and subscriptions accumulate permanently once made.
BASE_URL = os.environ.get("BIA_BASE_URL", "https://black.betinasia.com")
START_URL = os.environ.get("BIA_START_URL", BASE_URL + "/sportsbook/football")

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


# BetInAsia's own keys are comma-separated ("2026-08-09,10047664,90384", "tennis_match,all") and the
# sidecar's /odds transport is `?selections=a,b,c` -- ALSO comma-separated. So a raw id is shredded
# into five useless fragments before the adapter ever sees it, and every lookup misses: the feed is
# healthy, the catalog is full, and odds() returns {} forever. Encode commas on the way out and decode
# on the way in so the id is a single opaque token to everything in between.
_COMMA_SUB = "~"          # not present in any sport, event key, market key or selection observed


def make_selection_id(sport: str, comp_id: int | str, event_key: str,
                      market_key: str, selection: str) -> str:
    """`{sport}:{comp_id}:{event_key}:{market_key}:{selection}`

    Colon-separated and safe: no sport, event key, market key or selection token observed in the
    recon contains a colon (event/market keys use commas). comp_id is carried INSIDE the id on
    purpose -- watch_hcaps needs it to subscribe, so odds() stays self-sufficient without a catalog
    round-trip, mirroring how the Pinnacle ids carry their league id for rove-nav.
    """
    return (f"{sport}:{comp_id}:{event_key.replace(',', _COMMA_SUB)}"
            f":{market_key.replace(',', _COMMA_SUB)}:{selection}")


def parse_selection_id(sid: str) -> Optional[tuple[str, str, str, str, str]]:
    """Inverse of make_selection_id: returns the REAL (comma-bearing) event and market keys."""
    parts = sid.split(":")
    if len(parts) != 5:
        return None
    return (parts[0], parts[1],
            parts[2].replace(_COMMA_SUB, ","),
            parts[3].replace(_COMMA_SUB, ","),
            parts[4])


class _IdentityFx:
    """FX provider for a USD account: the rate is 1.0 and never changes.

    NOT cosmetic. Without an `_fx` on the adapter `/fx` returns HTTP 400, the C# bot falls back to
    HARDVEN_FX_TO_USD, and that env var holds the EUR rate for the PINNACLE account -- 1.1540. Applied
    to a USD BetInAsia balance it inflates every book stake by 15.4%, so the two legs stop paying equal
    amounts and the "hedge" quietly becomes directional. That is the exact failure the FX layer was
    built for after the 6.9%-stale incident; here it would arrive through a 400 nobody reads.

    If the account is ever NOT USD this refuses to serve identity, because a wrong 1.0 is the same
    class of bug in the other direction.
    """

    def __init__(self, currency_fn):
        self._currency_fn = currency_fn

    def currency(self) -> str:
        return (self._currency_fn() or "USD").upper()

    @property
    def rate(self) -> float:
        return 1.0

    def status(self) -> dict:
        ccy = self.currency()
        ok = ccy == "USD"
        return {"currency": ccy, "rate": 1.0 if ok else 0.0, "source": "identity (USD account)",
                "age_sec": 0, "stale": not ok, "env_rate": 1.0, "env_drift_pct": 0.0,
                "last_error": None if ok else
                              f"account currency is {ccy}, not USD - identity FX is WRONG here; "
                              f"wire a real rate before sizing anything",
                "max_deviation": 0.0}

    async def refresh(self) -> dict:
        return self.status()

    def start(self) -> None:
        return None


class BetInAsiaAdapter(BookAdapter):
    name = "betinasia"

    def __init__(self) -> None:
        # BROWSER is the default and should stay that way: the bot opens the real page on the real
        # profile and only READS its socket. `direct` opens our own WS with the session token -- a
        # second client with a different TLS fingerprint and no surrounding page traffic. It exists
        # for offline/diagnostic use, not for anything the account does routinely.
        self.transport = os.environ.get("BIA_TRANSPORT", "browser").lower()
        self.observer = None
        self.feed = BetInAsiaFeed(passive=(self.transport == "browser"))
        # `/fx` reads adapter._fx. Absent => HTTP 400 => the bot silently uses the PINNACLE
        # EUR env rate on a USD account. See _IdentityFx.
        self._fx = _IdentityFx(currency_fn=lambda: self.feed.currency)
        self._started = False

    # ── lifecycle ─────────────────────────────────────────────────────────────
    async def startup(self) -> None:
        print(f"[BIA] assumed max_stake = {ASSUMED_MAX_STAKE:.2f} "
              f"(NO per-selection limit is published -- see MAX_STAKE_NOTE)", flush=True)
        if self.transport == "browser":
            from betinasia_observer import BetInAsiaObserver
            # Deep-link the sport page. Landing on "/" subscribes only the FEATURED tournament (6 of
            # 75 tennis matches); landing on /sportsbook/tennis subscribes all 75, with no clicking.
            # Subscriptions then persist for the life of the socket, so this is one page load and
            # then hours of pure observation.
            self.observer = BetInAsiaObserver(url=START_URL)
            await self.observer.start()
            self.feed = self.observer.feed
            print(f"[BIA] passive transport: watching the page at {START_URL}", flush=True)
            self._start_sport_walker()
            self._start_pairing_scheduler()
        else:
            print("[BIA] WARNING transport=direct - opening our OWN websocket. This is a second "
                  "client on the account; prefer BIA_TRANSPORT=browser.", flush=True)
            await self.feed.login()
            await self.feed.start()
        self._started = True
        if self.feed.currency != "USD":
            # Kalshi is USD. A USD BetInAsia account means the whole FX path collapses to identity;
            # anything else must go through the existing fx layer, so say so loudly.
            print(f"[BIA] WARNING account currency is {self.feed.currency}, not USD - "
                  f"HARDVEN_FX_TO_USD must be set", flush=True)

    async def shutdown(self) -> None:
        # Cancel our background loops BEFORE the browser goes away, so a walk in flight cannot raise
        # against a closed page and bury the real shutdown path in a traceback.
        import asyncio
        for attr in ("_sport_walk_task", "_pairing_task"):
            task = getattr(self, attr, None)
            if task is not None and not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
            setattr(self, attr, None)
        if self.observer is not None:
            await self.observer.stop()
        elif not self.feed.passive:
            await self.feed.stop()
        self._started = False

    def _start_sport_walker(self) -> None:
        """Visit every active sport's board once at startup, then re-visit on a slow cadence.

        WHY IT EXISTS. The page only subscribes what it RENDERED when you visited, and coverage decays
        silently as new fixtures are listed: a 90-min run held its 791 subscriptions perfectly while the
        catalog grew 789 -> 825, so coverage fell 99% -> 95% purely from the denominator. Nothing drops;
        the book simply grows past what we asked for. A periodic re-visit is therefore PURELY ADDITIVE --
        it picks up what is new and can never cost us a subscription we already hold.

        WHY ONE TAB. Subscriptions accumulate on the socket across navigation (tennis stayed at 83 after
        navigating to football), so one tab that walks the sports ends up holding all of them. See
        BetInAsiaObserver.visit_sports.

        Off by default (BIA_SPORT_WALK=1): the single-sport bot does not need it, and navigating a live
        bot's page is not something to start doing implicitly."""
        if os.environ.get("BIA_SPORT_WALK") != "1":
            return
        import asyncio
        import sports as _sports

        targets = _sports.bia_paths(BASE_URL)
        if not targets:
            print("[BIA] sport walk requested but no sport has a verified BIA path "
                  "(set HARDVEN_SPORTS, e.g. 'all')", flush=True)
            return
        dwell = float(os.environ.get("BIA_SPORT_DWELL_SEC", "25"))
        every_min = float(os.environ.get("BIA_SPORT_WALK_MIN", "180"))
        skipped = [s.key for s in _sports.bia_sports() if not s.bia_path]

        async def _walk() -> None:
            # Let the initial page settle first so the startup verdict is not measured mid-navigation.
            await asyncio.sleep(float(os.environ.get("BIA_SPORT_WALK_DELAY", "70")))
            while True:
                try:
                    if self.observer is not None:
                        await self.observer.visit_sports(targets, dwell=dwell)
                        print("[BIA] venue coverage by sport:\n"
                              + self.observer.coverage_table(), flush=True)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    print(f"[BIA] sport walk failed ({type(e).__name__}: {e}) - retrying next cycle",
                          flush=True)
                await asyncio.sleep(every_min * 60.0)

        self._sport_walk_task = asyncio.create_task(_walk())
        names = ", ".join(c for c, _ in targets)
        print(f"[BIA] sport walk ON: {names} (dwell {dwell:.0f}s, re-visit every {every_min:.0f} min)",
              flush=True)
        if skipped:
            print(f"[BIA] sport walk SKIPS {skipped} - no sportsbook slug observed in any capture, "
                  f"so the URL would be a guess", flush=True)

    def _start_pairing_scheduler(self) -> None:
        """Re-pair on a cadence, like Pinnacle does. Opt-in via BIA_AUTO_PAIR=1.

        Not optional in spirit: Kalshi posts tennis close to the event while BetInAsia lists a day
        ahead, so the pairable set is a MOVING INTERSECTION. Measured at 14:49 UTC, 22 of BIA's 23
        same-day games were paired but only 3 of its 68 next-day games -- Kalshi simply had not posted
        those markets yet. A one-shot pair therefore looks like poor coverage when it is really a
        snapshot of a narrow window; re-pairing is what converts it into the day's full overlap.

        We do NOT run pairHard.py here. Pinnacle's scheduler already keeps the Kalshi scaffold fresh in
        cross_pairs.json, so this only READS it (--sync-seeds) and writes its own file. Two schedulers
        scraping Kalshi in parallel would double the load for identical data, and only one of them can
        own cross_pairs.json.
        """
        if os.environ.get("BIA_AUTO_PAIR") != "1":
            return
        import asyncio
        from pathlib import Path
        from pairing_scheduler import PairingScheduler

        import sports as _sports

        here = Path(__file__).resolve().parent
        # DERIVE the pairing scope from the active sport set rather than defaulting to one sport.
        # A hardcoded "fb" default meant HARDVEN_SPORTS=all walked ten sport pages, subscribed all of
        # them, scaffolded 99 Kalshi series -- and then paired ONLY football, so nine sports' worth of
        # prices sat in the feed unpaired and the run would have read as "those sports have no arbs".
        _codes = _sports.bia_sport_codes()
        _default = "all" if len(_codes) > 1 else (_codes[0] if _codes else "fb")
        sport = os.environ.get("BIA_PAIR_SPORT", _default)
        pairs = os.environ.get("BIA_PAIRS_FILE", str(here.parent / "cross_pairs_bia.json"))
        interval = int(os.environ.get("BIA_PAIR_INTERVAL_MIN", "90"))
        seeds = os.environ.get("BIA_SEED_FILE", "")

        if seeds:
            # Legacy path: borrow another venue's already-scaffolded Kalshi side. Only correct while both
            # books cover the SAME sport.
            steps = [("betinasia fill", ["pair_betinasia.py", "--sport", sport, "--pairs", pairs,
                                         "--sync-seeds", seeds, "--write"], here)]
        else:
            # Scaffold our OWN Kalshi side. Pinnacle's cross_pairs.json is scoped by HARDVEN_SPORTS
            # (tennis), so once the two books run different sports there is no soccer in it to sync from
            # — this venue has to fetch its own. `--out` keeps it in a separate file: token formats are
            # venue-specific and a pairs file is read by exactly one book.
            steps = [("scaffold (Kalshi)", ["pairHard.py", "--out", pairs], here.parent),
                     ("betinasia fill", ["pair_betinasia.py", "--sport", sport, "--pairs", pairs,
                                         "--write"], here)]
        # ORDERING: pairing can only fill what the feed has PRICED, and prices only exist for sports the
        # walk has visited. The old 30s default fired while the walk was still on its first page, so the
        # first cycle of a ten-sport run would pair football and nothing else -- and the other nine would
        # read as "no games" for a full interval (90 min). Wait out the walk when one is scheduled.
        default_delay = 30.0
        if os.environ.get("BIA_SPORT_WALK") == "1":
            n = len(_sports.bia_paths(BASE_URL))
            walk_secs = (float(os.environ.get("BIA_SPORT_WALK_DELAY", "70"))
                         + n * (float(os.environ.get("BIA_SPORT_DWELL_SEC", "25")) + 5.0))
            default_delay = walk_secs + 30.0
        delay = float(os.environ.get("BIA_PAIR_STARTUP_DELAY", str(default_delay)))
        sched = PairingScheduler(initial_delay=delay, interval_min=interval, steps=steps)
        self._pairing_task = asyncio.create_task(sched.run())
        src = f"seeds <- {Path(seeds).name}" if seeds else "scaffolds its own Kalshi side"
        print(f"[BIA] auto-pair ON: {sport} every {interval} min ({src}, writes {Path(pairs).name}); "
              f"first run in {delay:.0f}s", flush=True)

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

    def feed_health(self) -> dict:
        """Is the SOCKET alive, independent of any one market's age?

        This is the signal a push-only venue needs. BetInAsia has no REST prices endpoint at all (the
        whole recon contains exactly one price-bearing HTTP response, and it is the betslip), so there
        is no Pinnacle-style 90s re-seed to keep per-quote timestamps moving. A pre-match market simply
        does not tick: measured over 120s, 2 of 20 pre-live selections got a fresher timestamp versus 8
        of 12 in-play, and the median pre-live quote was 834s old against a 30s freshness gate. Every
        pre-live price was therefore being discarded, which is why 221 of 221 windows were in-play.

        `stale` here means THE FEED stopped, not "this market is quiet" — frames arrive constantly
        across the whole book, so silence on the socket is unambiguous.
        """
        st = self.feed.stats()
        age = st.get("last_frame_age")
        limit = float(os.environ.get("BIA_FEED_STALE_SEC", "120"))
        alive = bool(st.get("connected") or (age is not None and age <= limit))
        return {"book": self.name, "alive": alive,
                "last_frame_age": age, "stale_after_sec": limit,
                "priced": st.get("priced", 0), "frames": st.get("frames", 0),
                # Tell the bot the per-quote age gate does not apply here, so it does not have to know
                # anything venue-specific itself.
                "quote_age_policy": "feed"}

    # Captured column order, kept ONLY as a cross-check on the price match below. A hardcoded map is the
    # weaker instrument: it has to be captured per sport, and it silently becomes wrong the day the site
    # reorders a layout. Where both are available they must AGREE.
    # Captured by clicking each column and reading the row's <a href> + the event's home/away:
    #   tennis   2026-08-11 (3 competitions)  col2 = p1 (first-named), col3 = p2
    #   baseball 2026-08-11,52826,10048573    col2 = 1.795 = Detroit  = HOME
    #                                         col3 = 2.210 = Cleveland = AWAY
    # The board lists HOME FIRST, so the layout is [label, home, away] in both sports. Note that is NOT
    # the MLB "away @ home" convention — which is precisely why this is captured rather than assumed.
    SLIP_COLUMN = {
        "tennis":   {"p1": 2, "p2": 3},
        "baseball": {"h": 2, "a": 3},
    }

    # Widest relative gap between the feed's board price and the price rendered in the row that we will
    # still call a match. They are the same number in principle, but the row can be a tick behind.
    COLUMN_MATCH_TOL = 0.05      # 5%
    COLUMN_MATCH_MARGIN = 3.0    # best candidate must be >=3x closer than the runner-up

    async def _identify_column(self, row, sport: str, ekey: str, market_key: str, sel: str):
        """Find which board column belongs to `sel` by MATCHING ITS PRICE, not by a captured position.

        The row renders both (or all three) prices as text, and the feed already knows the board price for
        each selection. So the column is identifiable from data we independently hold: whichever column
        shows this selection's price IS this selection. That beats a captured `nth-child` map on both
        counts -- it needs no per-sport capture, and it cannot silently point at the wrong side if the site
        reorders a layout, because the price would stop matching.

        Fails CLOSED. Returns (None, reason) when no column matches, when two columns are too close to
        separate (a market priced 1.925/1.925 genuinely cannot be told apart this way), or when the answer
        contradicts the captured map. Clicking the wrong column places a real bet on the wrong side.
        """
        book = self.feed._books.get((sport, ekey)) or {}
        entry = (book.get("markets") or {}).get(market_key)
        if not entry:
            return None, f"no board price cached for {market_key} — cannot identify the column"
        _line, sels = entry
        want = sels.get(sel)
        if not want or want <= 1.0:
            return None, f"no board price for selection '{sel}' — cannot identify the column"

        seen: dict[int, float] = {}
        for col in (2, 3, 4):                      # 2-way uses 2..3, 3-way (wdw) adds 4
            try:
                loc = row.locator(f"div:nth-child({col}) > span").first
                if await loc.count() == 0:
                    continue
                txt = ((await loc.inner_text()) or "").strip().replace(",", "")
                seen[col] = float(txt)
            except (ValueError, TypeError):
                continue
            except Exception:
                continue
        if not seen:
            # We matched an <a> and its href checked out, yet it renders no price cells. Almost certainly a
            # DIFFERENT link carrying the same event key (breadcrumb, event-detail link, in-play widget) --
            # `.first` takes DOM order, not "the board row". Report what we actually grabbed.
            try:
                txt = " ".join(((await row.inner_text()) or "").split())[:200]
            except Exception:
                txt = "<unreadable>"
            try:
                kids = await row.locator("> *").count()
            except Exception:
                kids = -1
            return None, (f"matched an <a> with {kids} child element(s) and no price columns -- "
                          f"probably not the board row. Its text: {txt!r}")

        # CAPTURED POSITION IS AUTHORITATIVE where we have one. Operator's call, and the right one: a
        # position observed by clicking is a fact, whereas the price match is an inference that refuses on
        # symmetric markets and on any stale-price disagreement. So the map decides, and the price becomes
        # a free CROSS-CHECK on it -- which is the stronger arrangement anyway, since the two instruments
        # fail in unrelated ways and both must agree before a real bet is placed.
        captured = self.SLIP_COLUMN.get(sport, {}).get(sel)
        if captured is not None:
            shown = seen.get(captured)
            if shown is None:
                return None, f"captured column {captured} for {sport}/{sel} is not present in the row"
            if abs(shown - want) > want * self.COLUMN_MATCH_TOL:
                return None, (f"captured column {captured} shows {shown} but the board says {want} for "
                              f"'{sel}' — layout may have changed; refusing until re-captured")
            return captured, ""

        ranked = sorted(seen.items(), key=lambda kv: abs(kv[1] - want))
        best_col, best_val = ranked[0]
        if abs(best_val - want) > want * self.COLUMN_MATCH_TOL:
            return None, (f"no column matches the board price {want} for '{sel}' (row shows {seen}) — "
                          f"refusing rather than guessing a side")
        if len(ranked) > 1:
            second = abs(ranked[1][1] - want)
            best = abs(best_val - want)
            # A pure RATIO test cannot separate a symmetric market: two columns both showing exactly the
            # wanted price give best == second == 0, and 0 < 0*margin is false, so it would wave through
            # a coin-flip on which side gets bet. Require an ABSOLUTE gap as well.
            if second <= max(best * self.COLUMN_MATCH_MARGIN, best + want * 0.005):
                return None, (f"columns {seen} are too close to tell apart for '{sel}' at {want} — "
                              f"refusing (a symmetric market cannot be identified by price)")
        return best_col, ""

    async def slip_quote(self, selection_id: str) -> dict:
        """Open the betslip for one selection and return the TRUE offered odds. Places nothing.

        WHY THIS IS NEEDED even though the board WS looks right: measured 2026-08-11 on a fast in-play
        match, board and slip agreed EXACTLY on 84% of selections sampled <=2s apart -- but the other 16%
        were worse at the slip (median 1.69%, max 6.52%) and NEVER better. One-directional, so a board
        price can only ever OVERSTATE an arb. On a 1-2c edge that is decisive.

        HOW THE ROW IS FOUND -- and why this is safer than the Pinnacle equivalent. The row is an <a> whose
        href is `/sportsbook/{sport}/{country}/{comp_id}/{event_key}`, and that event_key is byte-identical
        to the one inside our selection_id. So the market is addressable by EXACT ID, and sport + comp_id
        cross-check against the same href before anything is clicked. Pinnacle's odds button carries no
        matchup id at all, which is why its bet path has to probe positionally and read the popover back to
        discover what it actually selected; here a wrong row is detectable BEFORE the click.

        The price itself never arrives over HTTP -- all 15 captured /v1/betslips/ responses are price-free.
        Clicking makes the page send `watch_acca_hcaps`, and the venue answers `offers_acca_hcap`, which
        the feed parses into `_slip_books`. So this waits on the socket, not on a response body.
        """
        import asyncio as _aio

        parsed = parse_selection_id(selection_id)
        if not parsed:
            return {"ok": False, "error": f"unparseable selection_id '{selection_id}'"}
        sport, comp_id, ekey, market_key, sel = parsed
        if market_key != MONEYLINE_BY_SPORT.get(sport):
            return {"ok": False, "error": f"slip quotes are moneyline-only; '{market_key}' is a derivative"}
        obs = self.observer
        page = getattr(obs, "_page", None) if obs else None
        if page is None:
            return {"ok": False, "error": "no browser page (direct transport cannot open a betslip)"}

        t0 = time.time()
        key = (sport, ekey)

        # ── ALREADY SUBSCRIBED? Then do not click at all. ────────────────────────────────────────────
        # Proved 2026-08-11: the acca subscription behaves like the board one — once an event is
        # subscribed the venue KEEPS PUSHING updates (two further offers_acca_hcap arrived after a quote
        # returned), and a REPEAT watch_acca_hcaps is answered with `event_already_subscribed` INSTEAD of
        # a price. That is why every re-quote of the same event timed out while a fresh event worked
        # first time in 686ms. So a second click is not just wasteful, it is actively self-defeating.
        # Reading the live cache instead is faster AND removes a UI action, which is the direction the
        # anti-detection constraint pushes anyway.
        max_age = float(os.environ.get("BIA_SLIP_MAX_AGE_SEC", "10"))
        cached = self.feed._slip_books.get(key)
        if cached and (time.time() - cached.get("ts", 0.0)) <= max_age:
            entry = (cached.get("markets") or {}).get(market_key)
            if entry:
                _line, sels = entry
                odds = sels.get(sel)
                if odds and odds > 1.0:
                    age = round(time.time() - cached.get("ts", 0.0), 2)
                    print(f"[BIA SLIP] {selection_id} -> {odds} from the live slip feed "
                          f"(age {age}s, no click)", flush=True)
                    return {"ok": True, "decimal_odds": odds,
                            "implied_price": round(1.0 / odds, 6),
                            "elapsed_ms": round((time.time() - t0) * 1000, 1),
                            "from_cache": True, "age_sec": age, "selection_id": selection_id}

        before_ts = (cached or {}).get("ts", 0.0)
        try:
            row = page.locator(f'a[href*="{ekey}"]').first
            # The competition may still be collapsed -- its rows do not exist in the DOM until "Show more"
            # is expanded, so this is a PRECONDITION of finding the row, not a fallback.
            if await row.count() == 0:
                for _ in range(int(os.environ.get("BIA_SHOW_MORE_CLICKS", "6"))):
                    more = page.get_by_text("Show more", exact=True)
                    if await more.count() == 0:
                        break
                    await more.first.click(timeout=5_000)
                    await _aio.sleep(0.4)
                    if await page.locator(f'a[href*="{ekey}"]').count():
                        break
                row = page.locator(f'a[href*="{ekey}"]').first
            if await row.count() == 0:
                return {"ok": False, "error": f"event {ekey} is not on this board (wrong sport page?)"}

            # CROSS-CHECK the href before clicking. The event key alone already identifies the match; sport
            # and comp_id are two more independent confirmations that cost nothing.
            n_rows = await page.locator(f'a[href*="{ekey}"]').count()
            if n_rows > 1:
                print(f"[BIA SLIP] {ekey}: {n_rows} links match this event key -- using DOM-first", flush=True)
            href = await row.get_attribute("href") or ""
            if f"/{sport}/" not in href or f"/{comp_id}/" not in href:
                return {"ok": False, "error": f"row href {href!r} disagrees with token "
                                              f"(sport={sport} comp={comp_id}) -- refusing to click"}

            # Identify the column from the PRICE now that we have the row element.
            col, why = await self._identify_column(row, sport, ekey, market_key, sel)
            if col is None:
                return {"ok": False, "error": why}

            url_before = page.url
            slips_before = len(self.feed._slip_books)
            await row.locator(f"div:nth-child({col}) > span").first.click(timeout=5_000)
            await _aio.sleep(0.5)
            url_after = page.url
            # THE ROW IS AN <a href>. A real user's click is swallowed by the app (it opens the Quick Bet
            # panel); if the default link action fires instead, the SPA navigates to the event page, the
            # slip never opens and no watch_acca_hcaps is ever sent. That failure looks identical to "the
            # venue did not price it", so distinguish them explicitly rather than reporting the same
            # timeout for both.
            if url_after != url_before:
                print(f"[BIA SLIP] click NAVIGATED: {url_before} -> {url_after} (returning)", flush=True)
                try:
                    await page.go_back(wait_until="domcontentloaded", timeout=15_000)
                except Exception:
                    pass
                return {"ok": False, "navigated": True, "url_after": url_after,
                        "error": "the click followed the row link instead of opening the betslip -- "
                                 "the app's handler did not intercept it"}

            # Wait for the venue to push THIS event's slip prices. A changed ts (or a first appearance)
            # is the signal; a stale cached book must never be read as a fresh quote.
            deadline = time.time() + float(os.environ.get("BIA_SLIP_WAIT_SEC", "8"))
            while time.time() < deadline:
                bk = self.feed._slip_books.get(key)
                if bk and bk.get("ts", 0.0) > before_ts:
                    entry = (bk.get("markets") or {}).get(market_key)
                    if entry:
                        _line, sels = entry
                        odds = sels.get(sel)
                        if odds and odds > 1.0:
                            ms = round((time.time() - t0) * 1000, 1)
                            print(f"[BIA SLIP] {selection_id} -> {odds} in {ms:.0f}ms", flush=True)
                            return {"ok": True, "decimal_odds": odds,
                                    "implied_price": round(1.0 / odds, 6),
                                    "elapsed_ms": ms, "selection_id": selection_id}
                await _aio.sleep(0.1)
            # Timed out. Report WHAT WE OBSERVED so the next attempt does not need another guess:
            #   slip_books_grew   -> the venue IS pushing slip prices, just not for this event/market
            #   book_present      -> we have a book for the event but the moneyline key is absent from it
            #   slip_panel_seen   -> the panel really did open, so the click path is right
            bk = self.feed._slip_books.get(key) or {}
            panel = 0
            try:
                panel = await page.get_by_text("start acca", exact=False).count()
            except Exception:
                pass
            return {"ok": False,
                    "error": f"no offers_acca_hcap for {ekey}/{market_key}/{sel} within the wait window",
                    "diag": {"slip_books_before": slips_before,
                             "slip_books_now": len(self.feed._slip_books),
                             "book_present": bool(bk),
                             "markets_in_book": sorted((bk.get("markets") or {}).keys())[:12],
                             "slip_panel_seen": panel,
                             "url": page.url}}
        except Exception as e:
            return {"ok": False, "error": f"slip quote error: {type(e).__name__}: {e}"}
        finally:
            # ALWAYS close. An open slip is a detection tell, and it is what the next quote would collide
            # with. Escape first (no selector to rot); the close control is a build-hashed svg behind a
            # long nth-child path, so it is only a fallback.
            try:
                await page.keyboard.press("Escape")
            except Exception:
                pass

    def feed_diagnostics(self) -> dict:
        """Everything we know about the price socket and what it is covering, as JSON.

        WHY: the observer already tracks sockets, per-sport coverage and the eviction test, but all of it
        went to the CONSOLE only — so "is any sport's subscription dropped?" was unanswerable without
        reading scrollback. That is a fair question the moment one tab is holding ten sports at once.

        `resumed_after_quiet` is the number to read first. A dropped subscription cannot start updating
        again, so any nonzero value PROVES nothing was evicted. Read it before `alive`, which on a
        pre-live book mostly measures how chatty the markets are — it decays on healthy quiet markets and
        has caused a false "we're losing subscriptions" verdict before.

        Coverage decays SILENTLY even with zero drops: the page subscribes what it rendered when visited,
        so newly-listed fixtures are never auto-subscribed (measured: catalog grew 789 -> 825 in 90 min
        while subscriptions held at 791). `catalog_matches` vs `priced_total` is where that shows up, and
        the fix is another sport walk, not a reconnect.
        """
        st = self.feed.stats()
        out: dict = {
            "book": self.name,
            "transport": self.transport,
            "connected": bool(st.get("connected")),
            "frames": st.get("frames", 0),
            "last_frame_age": st.get("last_frame_age"),
            "subscribed": st.get("subs", 0),
            "events_known": st.get("events", 0),
            "priced": st.get("priced", 0),
        }
        # SLIP-CHANNEL AUDIT. `slip_quote` decides "already subscribed, read the cache" purely from a
        # timestamp, and there is no other way to see what the acca channel holds: `watch_acca_hcaps` is
        # logged but never recorded into `_subs`, so the subscribed SET is invisible. Without this the
        # cache decision is an inference; with it, it is inspectable.
        # NOTE subscriptions are PER-SOCKET — a page close/reconnect resets them, so this list is scoped
        # to the current socket, not the session.
        now = time.time()
        slip = []
        for (sp, ek), bk in (self.feed._slip_books or {}).items():
            slip.append({"sport": sp, "event": ek,
                         "age_sec": round(now - (bk.get("ts") or 0), 1),
                         "markets": len(bk.get("markets") or {})})
        slip.sort(key=lambda r: r["age_sec"])
        out["slip"] = {"events": len(slip),
                       "max_age_sec": float(os.environ.get("BIA_SLIP_MAX_AGE_SEC", "10")),
                       # fresh = a re-quote would be served from the feed with NO click
                       "fresh": sum(1 for r in slip
                                    if r["age_sec"] <= float(os.environ.get("BIA_SLIP_MAX_AGE_SEC", "10"))),
                       "already_subscribed_errors": getattr(self.feed, "_already_subscribed_seen", 0),
                       "recent": slip[:20]}

        obs = self.observer
        if obs is None:
            out["note"] = "no observer (direct transport) — per-sport coverage unavailable"
            return out
        # socket COUNT is the reconnect signal: this venue holds one socket for hours, so >1 means the
        # page reloaded or the connection dropped and came back (subscriptions do NOT survive that).
        out["sockets"] = getattr(obs, "_sockets", None)
        out["socket_urls"] = list(getattr(obs, "_socket_urls", []) or [])   # already token-redacted
        try:
            cov = obs.coverage()
            out["catalog_matches"] = cov.get("catalog_matches")
            out["priced_total"] = cov.get("priced_total")
            out["page_subscribed"] = cov.get("page_subscribed")
            out["by_sport"] = cov.get("by_sport")
        except Exception as ex:
            out["coverage_error"] = f"{type(ex).__name__}: {ex}"
        try:
            dr = obs.drop_report()
            out["drops"] = {k: dr.get(k) for k in
                            ("subscribed", "ever_priced", "alive",
                             "resumed_after_quiet", "events_that_resumed")}
        except Exception as ex:
            out["drop_error"] = f"{type(ex).__name__}: {ex}"
        return out

    # ── diagnostics ───────────────────────────────────────────────────────────
    def health(self) -> dict:
        s = self.feed.stats()
        # PRICED is the number that matters and the one the bot could not previously see: a catalog
        # with zero prices is the logged-out/never-subscribed state, and it is indistinguishable from
        # healthy unless this is published.
        if self.observer is not None:
            s["anonymous_socket"] = getattr(self.observer, "_anon_socket", False)
            s["sockets"] = getattr(self.observer, "_socket_urls", [])
            try:
                s["page_url"] = self.observer._page.url if self.observer._page else None
            except Exception:
                s["page_url"] = None
            # A SAMPLE of what is actually priced. `priced` alone cannot tell you that odds() is
            # looking up a market_key the feed never sends -- which reads as "no prices" while the
            # cache is full. Show sport/event/market so the two sides can be compared directly.
            try:
                sample = []
                for (sp, ek), b in list(self.feed._books.items()):
                    mk = list((b or {}).get("markets") or {})
                    if mk:
                        sample.append({"sport": sp, "event": ek, "markets": mk[:6]})
                    if len(sample) >= 5:
                        break
                s["priced_sample"] = sample
            except Exception:
                pass
            if s.get("events") and not s.get("priced"):
                s["WARNING"] = ("catalog present but ZERO prices - the page is not logged in or never "
                                "subscribed; odds() will return {} for every selection")
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
