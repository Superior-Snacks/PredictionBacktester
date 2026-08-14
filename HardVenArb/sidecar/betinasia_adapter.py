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

import json
import os
import random
import re
import time
from typing import Optional

import sports as sports_cfg
from book_adapter import BetResult, BookAdapter, CatalogEntry, Selection
from betinasia_ws import BetInAsiaFeed
from human_mouse import CURSOR, VIEW_BOTTOM, VIEW_REST, VIEW_TOP

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
# TENNIS, not football. The landing page is the one tab we do NOT trade from — every sport we do trade
# gets its own parked board tab — so its only lasting effect is what it drags into the feed. Football is
# the venue's biggest book by an order of magnitude (3,495 catalog selections across fb/fb_ht/fb_htft/
# fb_corn/fb_corn_ht vs 150 tennis), none of which is paired while soccer is out of HARDVEN_SPORTS. That
# bulk is not free: it crowds every per-sport report (it is what pushed baseball and mma off the end of a
# top-12 coverage table and made them read as ABSENT for an afternoon) and it is subscription traffic for
# markets we cannot trade. Tennis is the smallest useful board and is paired, so the landing page earns
# its keep instead of just adding noise. BIA_START_URL still overrides.
START_URL = os.environ.get("BIA_START_URL", BASE_URL + "/sportsbook/tennis")

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
        # selection_id -> (stake_at_quoted_price, observed_at). Filled by slip_quote from the betslip
        # ladder; read by odds() in place of BIA_ASSUMED_MAX_STAKE. TTL'd because depth is a live number
        # and a remembered one silently becomes fiction — expiring back to the announced assumption is
        # honest, holding a ten-minute-old figure as current is not.
        # ── PLACEMENT GATES ───────────────────────────────────────────────────────────────────────────
        # Both were read by /health via getattr with a default, so this adapter reported bet_enabled=false
        # and max_stake=null however the environment was set — and place_bet would have raised
        # AttributeError on its first gate. Defined here so the two agree and the ceiling actually binds.
        # HARDVEN_MAX_STAKE is an INDEPENDENT ceiling from the bot's own ladder cap on purpose: two
        # numbers in two processes is what catches a units or FX error before it becomes a real bet.
        self._bet_enabled = os.environ.get("HARDVEN_BET_ENABLE") == "1"
        try:
            self._max_stake = float(os.environ.get("HARDVEN_MAX_STAKE", "0") or 0)
        except ValueError:
            self._max_stake = 0.0
        self._balance_currency = "USD"
        self._slip_depth: dict[str, tuple[float, float]] = {}
        self._slip_depth_ttl = float(os.environ.get("BIA_SLIP_DEPTH_TTL_SEC", "300"))
        self._depth_announced = False
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
            self._start_board_tabs()
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
        for attr in ("_sport_walk_task", "_board_tabs_task", "_pairing_task"):
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

    def _start_board_tabs(self) -> None:
        """Park one fully-expanded board tab per active sport, so a slip quote never has to navigate.

        Lazy creation (on the first quote for a sport) works, but it pays the load-and-expand cost on the
        execution path with the arb already ticking — which is the exact cost that lost the Vandecasteele
        arb on the Pinnacle side. Doing it at startup means the row is on screen before it is ever needed.

        BIA_BOARD_TABS=0 disables it and falls back to lazy creation. The delay lets the initial page and
        the sport walk settle first, so the tabs are opened into a quiet browser rather than competing with
        startup navigation."""
        if os.environ.get("BIA_BOARD_TABS") == "0":
            print("[BIA] board tabs OFF (BIA_BOARD_TABS=0) — slip quotes will open tabs on demand", flush=True)
            return
        import asyncio
        import sports as _sports

        targets = _sports.bia_paths(BASE_URL)
        if not targets:
            print("[BIA] board tabs: no sport has a verified BIA path (check HARDVEN_SPORTS)", flush=True)
            return

        # PERIODIC RESET. A parked tab's DOM is a snapshot of park time, so fixtures the venue lists later
        # are unclickable (the quote falls through to the rover) and unsubscribed (the page only subscribes
        # what it rendered). Reloading the whole sequence is the one action that refreshes both. 0 = never.
        reset_min = float(os.environ.get("BIA_BOARD_RESET_MIN", "60"))

        async def _park():
            await asyncio.sleep(float(os.environ.get("BIA_BOARD_TABS_DELAY_SEC", "20")))
            print(f"[BIA] parking {len(targets)} board tab(s): "
                  f"{', '.join(c for c, _ in targets)}", flush=True)
            try:
                await self.observer.open_sport_tabs(targets)
            except Exception as e:
                print(f"[BIA] board tabs failed: {type(e).__name__}: {e}", flush=True)
            if reset_min <= 0:
                return
            # JITTERED. A reload of every board at exactly 60.0-minute spacing is a machine signature
            # visible in request timestamps alone, and it costs nothing to scatter.
            import random as _rnd
            jit = float(os.environ.get("BIA_BOARD_RESET_JITTER_PCT", "25")) / 100.0
            print(f"[BIA] board reset every ~{reset_min:g} min (+/-{jit*100:.0f}% jitter)", flush=True)
            while True:
                await asyncio.sleep(reset_min * 60.0 * (1.0 + _rnd.uniform(-jit, jit)))
                try:
                    # HOLD THE SLIP LOCK for the whole reset: it is the same lock a quote takes, so a
                    # click can never find its tab closed underneath it, and a reset waits for a quote
                    # already in flight instead of racing it.
                    lock = getattr(self, "_slip_lock", None)
                    if lock is None:
                        lock = self._slip_lock = asyncio.Lock()
                    async with lock:
                        await self.observer.reset_sport_tabs(targets)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    # A failed reset must not kill the loop — the tabs that survive keep working and the
                    # next cycle tries again.
                    print(f"[BIA] board reset failed: {type(e).__name__}: {e}", flush=True)

        self._board_tabs_task = asyncio.create_task(_park())

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
            # REAL DEPTH WHERE WE HAVE IT. ASSUMED_MAX_STAKE is a placeholder that feeds
            # Selection.max_contracts -> StakeLadder.MaxDepthFraction -> the executor's depth gate, i.e.
            # sizing. The betslip's ladder is the venue's own answer, so prefer it while it is fresh and
            # fall back to the announced assumption once it ages out.
            stake = ASSUMED_MAX_STAKE
            hit = self._slip_depth.get(sid)
            if hit and (time.time() - hit[1]) <= self._slip_depth_ttl:
                stake = hit[0]
                if not self._depth_announced:
                    self._depth_announced = True
                    print(f"[BIA] using REAL betslip depth for sizing (first: {sid} -> "
                          f"{stake:,.2f}); assumed {ASSUMED_MAX_STAKE:.2f} still applies to "
                          f"selections never quoted, and after {self._slip_depth_ttl:.0f}s", flush=True)
            out[sid] = Selection(
                selection_id=sid,
                decimal_odds=float(price),
                max_stake=stake,
                status="open",
                ts=ts,
                live=_is_live(ev, start),
                cutoff=start,
            )
        return out

    def acca_ok_map(self, selection_ids: list[str]) -> dict[str, bool]:
        """Per selection: will the venue put this event on a BETSLIP at all?

        Published so the bot can stop spending slip-verify samples on events that can never be quoted.
        Measured 2026-08-14: 22 of 24 samples were refused in single-digit milliseconds because the
        event was not `available_for_accas` — the sampler was rationing a scarce, rate-limited resource
        and then handing almost all of it to events it had no way to read.

        This is a PROPERTY OF THE EVENT, not of the regime: across 3,809 captured events acca
        availability was 53.6% pre-match and 54.1% in-play. It tracks the sport and competition instead
        — cricket 6/49, esports 46/157, boxing 6/44, against tennis 155/171 and football 810/981. So a
        cricket-heavy session simply cannot be slip-verified, and that is worth knowing up front rather
        than discovering one refused sample at a time.

        Absent/unknown -> True, matching `ws_verified_map`: never let a missing flag silence a check.
        """
        out: dict[str, bool] = {}
        for sid in selection_ids:
            # NEVER let this raise: it runs inside /odds, which the bot polls every 3s for every book.
            # A throw here would take the whole price feed down to save a few sampling slots.
            try:
                parsed = parse_selection_id(sid)
                if not parsed:
                    continue
                sport, _comp, ekey, _mk, _sel = parsed
                ev = self.feed.get_event(sport, ekey) or {}
                if ev.get("available_for_accas") is False:
                    out[sid] = False
            except Exception:
                continue          # unknown => omitted => the bot's default (True) applies
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
    # The venue's own labels, from /accounting_info/. Anchoring the DOM read on THEIR wording rather than
    # on a CSS class: the classes are hashed CSS-module names (`_265f6344`) that change on every deploy,
    # while these strings are user-facing copy and are what the page actually renders.
    _BAL_LABELS = ("Current balance", "Available credit", "Balance")

    async def _balance_from_dom(self) -> Optional[float]:
        """Read the balance off the rendered page. No request at all, and invisible to page script.

        THE QUIETEST OPTION AVAILABLE. The fetch below is indistinguishable from the site's own request on
        the wire, but it is still a REQUEST — it appears in their access log at a cadence the UI would not
        produce. A locator read produces no traffic whatsoever, and Playwright runs it in an isolated
        world the page cannot instrument (measured: `test_dom_isolation.py`).

        Returns None when the balance is not on screen — it may sit behind an account menu, and opening
        one would be a UI action, which is the thing this exists to avoid. None is a real answer here.
        """
        obs = self.observer
        page = getattr(obs, "_page", None) if obs else None
        if page is None:
            return None
        try:
            # ── HEADER FIRST. The balance sits in the top-right corner of EVERY page (operator, confirmed
            # 2026-08-14), rendered as a bare amount with no label — so the label anchors below only work
            # on the account screen, which the bot never visits. Identify it by SHAPE and POSITION:
            # money is `1,234.56` (exactly two decimals), whereas board odds are `1.769` (three) and
            # scores are bare integers, so the format alone excludes almost everything on a board page.
            # Everything here is a locator operation, so none of it is visible to page script.
            money = re.compile(r"^[$€£]?\s?\d[\d,]*\.\d{2}$")
            cand = page.get_by_text(money)
            top_px = float(os.environ.get("BIA_BALANCE_HEADER_PX", "160"))
            best_x, best_val = -1.0, None
            for i in range(min(await cand.count(), 25)):
                try:
                    el = cand.nth(i)
                    box = await el.bounding_box()
                    if not box or box["y"] > top_px:
                        continue                       # below the header band — a board cell, not the balance
                    txt = (await el.inner_text()) or ""
                    val = float(re.sub(r"[^\d.]", "", txt) or "nan")
                except Exception:
                    continue
                # RIGHTMOST wins: the header carries other numbers (open stakes, a bet counter) and the
                # balance is the one in the corner.
                if val == val and 0 <= val < 10_000_000 and box["x"] > best_x:
                    best_x, best_val = box["x"], val
            if best_val is not None:
                return best_val

            for label in self._BAL_LABELS:
                loc = page.get_by_text(label, exact=False)
                if not await loc.count():
                    continue
                # Small ancestors only: the label and its value sit together, and a large container
                # would sweep in "Open stakes" and "Yesterday P/L" alongside.
                for depth in range(1, 5):
                    anc = loc.first.locator(f"xpath=ancestor::*[{depth}]")
                    if await anc.count() == 0:
                        continue
                    txt = " ".join(((await anc.first.inner_text()) or "").split())
                    # The number must follow the LABEL, not merely appear nearby.
                    m = re.search(rf"{re.escape(label)}\D{{0,14}}(-?[\d,]+(?:\.\d+)?)", txt, re.I)
                    if not m:
                        continue
                    try:
                        val = float(m.group(1).replace(",", ""))
                    except ValueError:
                        continue
                    if 0 <= val < 10_000_000:
                        return val
        except Exception as e:
            print(f"[BIA] balance DOM read failed: {type(e).__name__}: {e}", flush=True)
        return None

    async def balance(self) -> Optional[float]:
        """Account cash. None = unreadable.

        TRIES THE PAGE FIRST, then an authed fetch. `BIA_BALANCE_SOURCE` = `dom` (never issue a request),
        `fetch` (skip the DOM), or `auto` (default: DOM, falling back).

        WHY THE FETCH IS SECOND, NOT FIRST. `GET /v1/customers/{user}/accounting_info/` from page context
        is indistinguishable from the site's own call on the wire — same session, same TLS, same origin —
        so it was already the safe way to ASK. But it is still a request in their access log, on our
        schedule rather than the UI's, and a read off the rendered page costs nothing at all.

        NEEDS BIA_USERNAME for the fetch path only (the URL contains it); auth rides on the page's
        cookies, so no password is involved. The DOM path needs nothing.

        None IS A REAL ANSWER, not a failure: BalanceGuard treats it as UNKNOWN and does not halt, whereas
        returning 0.0 would halt a funded account -- the exact bug that bit Pinnacle twice. So every
        failure path here returns None rather than a number it is not sure of.
        """
        source = (os.environ.get("BIA_BALANCE_SOURCE") or "auto").lower()
        if source in ("auto", "dom"):
            v = await self._balance_from_dom()
            if v is not None:
                if not getattr(self, "_bal_src_logged", False):
                    self._bal_src_logged = True
                    print(f"[BIA] balance read FROM THE PAGE ({v:.2f}) — no request made", flush=True)
                return v
            if source == "dom":
                if not getattr(self, "_bal_warned", False):
                    self._bal_warned = True
                    print("[BIA] balance not on screen and BIA_BALANCE_SOURCE=dom — reporting UNKNOWN "
                          "(does not halt the guard). It may sit behind an account menu.", flush=True)
                return None

        user = (os.environ.get("BIA_USERNAME") or getattr(self.feed, "username", "") or "").strip()
        obs = self.observer
        page = getattr(obs, "_page", None) if obs else None
        if not user or page is None:
            if not getattr(self, "_bal_warned", False):
                self._bal_warned = True
                why = "BIA_USERNAME is unset" if not user else "no browser page"
                print(f"[BIA] balance unreadable ({why}) — reporting UNKNOWN, which does not halt the "
                      f"balance guard. Set BIA_USERNAME to enable it (path only; the page's own session "
                      f"provides auth).", flush=True)
            return None
        try:
            res = await page.evaluate(
                """async (u) => {
                    const r = await fetch(`/v1/customers/${u}/accounting_info/`,
                                          {credentials: 'include'});
                    if (!r.ok) return {err: 'HTTP ' + r.status};
                    return await r.json();
                }""", user)
        except Exception as e:
            print(f"[BIA] balance fetch failed: {type(e).__name__}: {e}", flush=True)
            return None
        if not isinstance(res, dict) or res.get("err"):
            print(f"[BIA] balance fetch: {res.get('err') if isinstance(res, dict) else res}", flush=True)
            return None
        # THE REAL SHAPE, read off a live response 2026-08-14 — `data` is a LIST OF RECORDS, not a dict:
        #   {"data":[{"key":"current_balance","label":"Current balance","unit":"USD","value":47.4675},
        #            {"key":"open_stakes",...},{"key":"available_credit",...}], "status":"ok"}
        # The previous code expected `data` to be a dict keyed by `current_balance`, so it fell through to
        # `res.get("current_balance")` -> None -> float(None) -> None. This path had therefore NEVER
        # returned a number, and the failure was invisible because None is also the legitimate "unknown".
        # Same lesson as balance()->0.0: a bug that returns the healthy idle value hides indefinitely.
        fields: dict[str, float] = {}
        data = res.get("data")
        if isinstance(data, list):
            for row in data:
                if isinstance(row, dict) and "key" in row:
                    fields[str(row["key"])] = row.get("value")
        elif isinstance(data, dict):
            fields = data
        else:
            fields = res
        raw = fields.get("current_balance")
        # Money is sometimes ["USD", 123.45] elsewhere in this API; accept a bare number too.
        if isinstance(raw, (list, tuple)) and len(raw) >= 2:
            raw = raw[1]
        try:
            bal = float(raw)
        except (TypeError, ValueError):
            print(f"[BIA] balance: no current_balance in {json.dumps(res)[:220]}", flush=True)
            return None
        if not getattr(self, "_bal_seen", False):
            self._bal_seen = True
            print(f"[BIA] balance read via an authed page fetch: {bal:.2f} "
                  f"(open_stakes={fields.get('open_stakes')}, "
                  f"commission={fields.get('commission_rate')}). Set BIA_BALANCE_SOURCE=dom to stop "
                  f"issuing this request once the on-page read is confirmed working.", flush=True)
        return bal

    # Hand-written class names on the slip — the only ones on this site that survive a deploy, unlike the
    # hashed CSS-module classes (`_265f6344`) everywhere else. Captured 2026-08-09.
    _PRICE_INPUT = ".price-input"
    _STAKE_INPUT = ".stake-input"

    async def place_bet(self, selection_id: str, stake: float, max_odds: float) -> BetResult:
        """Place through the UI, then report what ACTUALLY filled.

        DRIVEN BY CLICKS, not by calling /v1/orders/ directly. A UI click produces exactly the same two
        API calls, so clicking buys fingerprint and timing realism at no extra code path — and the venue
        sees the interaction sequence a person makes rather than a bare authed POST.

        NO RETRY, NO CANCEL (operator's call, 2026-08-14): few underlying books allow a cancel and the
        bookkeeping is not worth it, so this is one shot and irreversible in practice. That removes the
        idempotency question with it — `request_uuid` is generated by the page, and we never replay.

        THE RETURN CARRIES THE ROUTED NUMBERS, NOT THE REQUESTED ONES. The venue applies a haircut at
        routing (4.00 asked -> 3.9917) and can improve the price (1.88 asked -> 1.90 filled), so the
        Kalshi leg must be sized against `stake`/`actual_odds` here. Sizing off the request leaves the
        residual naked on every trade.
        """
        import asyncio as _aio

        if not self._bet_enabled:
            return BetResult(accepted=False, stake=stake,
                             reason="HARDVEN_BET_ENABLE is not set — the sidecar refuses to place")
        if self._max_stake and stake > self._max_stake:
            return BetResult(accepted=False, stake=stake,
                             reason=f"stake {stake:.2f} exceeds the sidecar ceiling "
                                    f"HARDVEN_MAX_STAKE={self._max_stake:.2f}")
        # MIN ORDER IS ODDS-DEPENDENT: observed "Less than min order of $3.80" at ~1.32, consistent with a
        # ~$5 minimum RETURN. Refuse here rather than let the UI reject after the slip is already open.
        min_return = float(os.environ.get("BIA_MIN_ORDER_RETURN", "5.0"))
        if max_odds > 1.0 and stake * max_odds < min_return - 1e-9:
            return BetResult(accepted=False, stake=stake,
                             reason=f"stake {stake:.2f} @ {max_odds} returns {stake * max_odds:.2f}, "
                                    f"below the venue's ~{min_return:.2f} minimum return")

        lock = getattr(self, "_slip_lock", None)
        if lock is None:
            lock = self._slip_lock = _aio.Lock()
        async with lock:
            try:
                self.observer.pause_organic()
            except Exception:
                pass
            try:
                return await self._place_via_ui(selection_id, stake, max_odds)
            except Exception as e:
                # An exception AFTER the Place click could still have placed. Say so rather than reporting
                # a clean rejection the caller would hedge against.
                print(f"[BIA BET] {type(e).__name__}: {e}", flush=True)
                return BetResult(accepted=False, stake=stake,
                                 reason=f"{type(e).__name__}: {e} — if this happened after the Place "
                                        f"click the order MAY be live; check /v1/orders/")
            finally:
                try:
                    self.observer.resume_organic()
                except Exception:
                    pass

    async def _place_via_ui(self, selection_id: str, stake: float, max_odds: float) -> BetResult:
        import asyncio as _aio

        # ── TOTAL TIME BUDGET, and why it is not a detail ────────────────────────────────────────────
        # The C# client posts /bet on a 90s HttpClient. If this call outruns that, the CLIENT aborts —
        # while the bet may already be placed. That is the exact shape of the 5s-timeout bug that killed
        # the best arb of 2026-08-12: an outer deadline silently overriding an inner one, with the loss
        # landing on the side that had already committed.
        # So the sidecar bounds ITSELF, well under the client, and spends what is left rather than
        # stacking independent per-step timeouts that can sum past it.
        t_budget = time.time() + float(os.environ.get("BIA_PLACE_TOTAL_BUDGET_SEC", "70"))

        # 1. Open the slip on this selection. Reuses the quote path wholesale: it finds the row by event
        #    key, cross-checks sport, clicks the cell whose PRICE matches, and refuses on ambiguity.
        q = await self._slip_quote_outer(selection_id)
        if not q.get("ok"):
            return BetResult(accepted=False, stake=stake,
                             reason=f"could not open the betslip: {q.get('error')}")
        # PRICE GATES FIRST — pure data, no page needed, and they give the most specific reason. Checking
        # the tab before them meant a slip priced out of the arb was refused as "the tab disappeared",
        # which is both wrong and the sort of message that sends a debugging session the wrong way.
        offered = float(q.get("decimal_odds") or 0.0)
        label = q.get("selection_label") or ""
        if offered <= 1.0:
            return BetResult(accepted=False, stake=stake, reason="the slip quoted no usable price")
        # max_odds is the caller's FLOOR: below it the arb is gone.
        if offered < max_odds - 1e-9:
            return BetResult(accepted=False, stake=stake, actual_odds=offered,
                             reason=f"slip offers {offered} but {max_odds} is required — not placing")

        page = getattr(self, "_slip_page", None)
        if page is None or page.is_closed():
            return BetResult(accepted=False, stake=stake, reason="the betslip's tab disappeared")

        # 2. Fill the form. Price first: changing it re-prices the slip, so a stake typed before would be
        #    re-validated against a different number.
        try:
            await CURSOR.click(page, page.locator(self._PRICE_INPUT).first, timeout=5_000)
            await page.locator(self._PRICE_INPUT).first.fill(str(max_odds))
            await _aio.sleep(random.uniform(0.15, 0.45))
            await CURSOR.click(page, page.locator(self._STAKE_INPUT).first, timeout=5_000)
            await page.locator(self._STAKE_INPUT).first.fill(f"{stake:.2f}")
            await _aio.sleep(random.uniform(0.25, 0.8))
        except Exception as e:
            return BetResult(accepted=False, stake=stake,
                             reason=f"could not fill the slip form ({type(e).__name__}: {e}) — "
                                    f"nothing was submitted")

        place = page.get_by_text("place", exact=False).last
        if not await place.count():
            return BetResult(accepted=False, stake=stake, reason="no Place control on the slip")

        # 3. Click Place and capture the order_id from the venue's own response to that click. Waiting on
        #    the response rather than scraping the DOM: the id is the join key for everything after this,
        #    and the page never renders it.
        order_id = None
        resp_budget = max(5.0, min(float(os.environ.get("BIA_PLACE_RESP_TIMEOUT", "20")),
                                   t_budget - time.time() - 10.0))   # leave 10s for the fill wait
        try:
            async with page.expect_response(
                    lambda r: "/v1/orders" in r.url and r.request.method == "POST",
                    timeout=resp_budget * 1000) as got:
                await CURSOR.click(page, place, timeout=8_000)
            resp = await got.value
            body = await resp.json()
            order_id = ((body or {}).get("data") or {}).get("order_id")
            print(f"[BIA BET] placed order {order_id}: asked {stake:.2f} @ {max_odds} "
                  f"on {label or selection_id}", flush=True)
        except Exception as e:
            # THE CLICK MAY HAVE LANDED. Never report this as a clean rejection.
            return BetResult(accepted=False, stake=stake,
                             reason=f"Place clicked but no order response seen ({type(e).__name__}) — "
                                    f"the order MAY BE LIVE. Do not hedge against this result.")
        if order_id is None:
            return BetResult(accepted=False, stake=stake,
                             reason="order response carried no order_id — the order MAY BE LIVE")

        # 4. Wait for the venue to say what actually filled. Pushed over the socket; no polling.
        #    Spends WHATEVER IS LEFT of the total budget rather than its own independent timeout — the
        #    order is already live at this point, so overrunning the client's 90s here is the worst
        #    possible moment to do it.
        fill_budget = max(3.0, min(float(os.environ.get("BIA_FILL_TIMEOUT", "45")),
                                   t_budget - time.time()))
        fill = await self.await_fill(int(order_id), timeout=fill_budget)
        filled = float(fill.get("filled_stake") or 0.0)
        price = fill.get("avg_price")
        if fill.get("timed_out") and filled <= 0:
            return BetResult(accepted=False, bet_id=str(order_id), stake=0.0,
                             reason=f"order {order_id} placed but unfilled after the wait — IT IS LIVE. "
                                    f"Treat as an open exposure, not a rejection.")
        if filled <= 0:
            return BetResult(accepted=False, bet_id=str(order_id), stake=0.0,
                             reason=f"order {order_id} closed with no fill "
                                    f"({fill.get('close_reason')})")
        print(f"[BIA BET] order {order_id} FILLED {filled:.4f} @ {price} "
              f"via {','.join(fill.get('bookies') or []) or '?'}"
              + (f"  (asked {stake:.2f} @ {max_odds})" if filled != stake or price != max_odds else ""),
              flush=True)
        return BetResult(accepted=True, bet_id=str(order_id),
                         actual_odds=price, stake=filled,
                         reason=None if fill.get("done") else "partial fill — order still open")

    async def _place_bet_unimplemented(self, selection_id: str, stake: float, max_odds: float) -> BetResult:
        """Superseded by place_bet above. Kept for the reasoning, which still holds for the parts NOT built:

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

    # WHAT A PRICE CELL LOOKS LIKE. Decimal odds always carry a decimal point on this venue ("1.062",
    # "3.79"); a BARE INTEGER in a board row is a score, a seed or a market count, never a price.
    #
    # This is a safety rule, not a tidiness one. `_find_price_cell` clicks whichever cell is closest to
    # the wanted price, and a tennis row in play renders game scores in the same spans -- so a score of
    # "4" sat 5.5% from a wanted 3.793 on 2026-08-13 and missed the 5% tolerance by a hair. At odds of
    # 3.90 the same cell would have PASSED, and the bot would have clicked a scoreboard, opened the slip
    # for whatever that cell belongs to, and reported the resulting price as verification. That is the
    # same class of error as the same-side fill: a plausible number from the wrong element.
    #
    # Fails closed in the harmless direction. If the venue ever does render 4.00 as "4" we decline a
    # quote we could have taken -- which costs nothing, and the refusal now prints the cell texts it saw,
    # so it shows up as data rather than as a silent miss.
    # Shape only — the value test (`> 1.0`) stays where it was, so "0.5" is rejected for being unpayable
    # rather than for being the wrong shape. Four decimals allowed: the feed quotes 3, but a rendered
    # price is the venue's business and refusing an extra digit would be an own goal.
    ODDS_TEXT = re.compile(r"^\d{1,3}\.\d{1,4}$")

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
        raw: dict[int, str] = {}               # every column's TEXT, parseable or not
        for col in (2, 3, 4):                  # 2-way uses 2..3, 3-way (wdw) adds 4
            try:
                loc = row.locator(f"div:nth-child({col}) > span").first
                if await loc.count() == 0:
                    continue
                txt = " ".join(((await loc.inner_text()) or "").split())
                raw[col] = txt
                seen[col] = float(txt.replace(",", ""))
            except (ValueError, TypeError):
                continue                       # present but not a number -- kept in `raw` for the error
            except Exception:
                continue
        if not seen and raw:
            return None, (f"columns are present but none parse as a price: {raw} — "
                          f"the row is probably rendering scores/status, not odds")
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
                return None, (f"captured column {captured} for {sport}/{sel} holds no PRICE. "
                              f"Column texts: {raw or '<none>'} — the row layout is not what was captured "
                              f"(in-play rows can carry an extra cell)")
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

    async def _find_price_cell(self, page, ekey: str, want: float):
        """Locator for the clickable cell showing `want` for this event, found BY PRICE across every link.

        WHY NOT A COLUMN MAP. The board's layout depends on MATCH STATE, not just sport. Captured pre-game,
        a baseball match is ONE link holding both prices ([label, home, away]). Once in-play the same match
        renders as TWO links, one per side, each `[team name, price]` — so "column 2" is a price before the
        game starts and a team name after it. A captured position cannot express that, and following it
        would click a team name or, worse, the wrong side.

        The price is the one thing that identifies a cell in every layout: we already hold the board odds
        for this exact selection, so whichever cell shows that number IS that selection. Self-verifying,
        layout-independent, and it fails closed — if no cell matches, or two match equally well, nothing is
        clicked.
        """
        links = page.locator(f'a[href*="{ekey}"]')
        try:
            n = await links.count()
        except Exception:
            return None, 0, "could not query the board"
        max_links = int(os.environ.get("BIA_SLIP_MAX_LINKS", "12"))
        max_spans = int(os.environ.get("BIA_SLIP_MAX_SPANS", "24"))
        cands = []                       # (abs error, locator, text, link index)
        seen: list[str] = []             # EVERY non-empty cell text, odds or not -- see below
        for i in range(min(n, max_links)):
            link = links.nth(i)
            try:
                spans = link.locator("div > span")
                m = await spans.count()
            except Exception:
                continue
            for j in range(min(m, max_spans)):
                cell = spans.nth(j)
                try:
                    txt = " ".join(((await cell.inner_text()) or "").split())
                except Exception:
                    continue
                if not txt:
                    continue
                if len(seen) < 24:
                    seen.append(txt)
                if not self.ODDS_TEXT.match(txt):
                    continue
                try:
                    val = float(txt.replace(",", ""))
                except (ValueError, TypeError):
                    continue
                if val <= 1.0:
                    continue             # decimal odds <= 1.0 pay nothing
                cands.append((abs(val - want), cell, txt, i))
        # Report WHAT THE ROW ACTUALLY SHOWS on every refusal. The old message named only the single
        # closest cell, which was not enough to tell "the venue suspended this price" from "we are
        # scanning the wrong elements" -- and those need opposite fixes. Measured 2026-08-13: 7 of 12
        # samples died here on tennis with closest cells reading 2/3/4/5, and the text alone could not
        # settle whether those were scores, seeds, or genuinely-integer odds.
        shown = ", ".join(repr(s) for s in seen[:16]) or "<no text in any cell>"
        if not cands:
            # WHERE ARE THE NUMBERS? `div > span` inside the <a> is where every working sport keeps its
            # price, but a row that renders team names and '-/-' score placeholders and nothing else
            # leaves two very different possibilities: the venue posts no odds for this fixture yet, or it
            # posts them somewhere this selector cannot see. Those need opposite fixes, and the cell texts
            # alone cannot separate them — so dump the row VERBATIM and let one probe decide.
            row_text = ""
            try:
                if n:
                    row_text = " ".join(((await links.first.inner_text()) or "").split())[:400]
            except Exception:
                pass
            return None, n, (f"no cell on any of {n} link(s) renders decimal odds "
                             f"(cells seen: {shown}) | whole row reads: {row_text!r}")
        cands.sort(key=lambda c: c[0])
        best = cands[0]
        if best[0] > want * self.COLUMN_MATCH_TOL:
            return None, n, (f"no cell matches the board price {want} "
                             f"(closest {best[2]} on link {best[3]}; cells seen: {shown}) — "
                             f"refusing rather than guessing")
        if len(cands) > 1:
            second = cands[1][0]
            if second <= max(best[0] * self.COLUMN_MATCH_MARGIN, best[0] + want * 0.005):
                return None, n, (f"two cells are equally close to {want} ({best[2]}, {cands[1][2]}) — "
                                 f"cannot tell the sides apart, refusing")
        return best[1], n, ""

    async def _find_board_row(self, page, ekey: str):
        """The board row for `ekey`, chosen by CONTENT rather than DOM order.

        Several links can carry the same event key -- the board row, a breadcrumb, an event-detail link,
        an in-play widget. `.first` takes whichever appears first in the document, which happened to be the
        board row on tennis and was NOT on baseball (the quote failed with "no price columns" before it
        ever clicked). So pick the link that actually HAS price cells: a breadcrumb cannot satisfy that,
        which makes the choice self-verifying rather than positional.
        """
        links = page.locator(f'a[href*="{ekey}"]')
        try:
            n = await links.count()
        except Exception:
            return None, 0
        for i in range(min(n, 12)):
            cand = links.nth(i)
            try:
                if await cand.locator("div:nth-child(2) > span").count() > 0:
                    return cand, n
            except Exception:
                continue
        return None, n

    def _league_url(self, sport: str, ekey: str, comp_id) -> str:
        """The league page holding this game: `{base}{sport_slug}/{country}/{comp_id}`, or "" if any part
        is unknown.

        Observed verbatim in the captures (`/sportsbook/football/AR/24805`), and every part is data we
        already hold: the WS `event` frame carries `country` and `competition_id` on 100% of 5,849 events
        captured, and `comp_id` rides inside the selection_id. So the rover addresses the league directly
        rather than loading the sport board and expanding it.

        The LEAGUE page, not the event page: it renders the same board rows `_find_price_cell` is already
        proven against, whereas the event page's layout is unverified — slip_quote currently treats
        landing on it as a failure."""
        country = (self.feed.get_event(sport, ekey) or {}).get("country") or ""
        path = sports_cfg.bia_path_by_code().get(sport, "")
        if not (country and path and comp_id):
            return ""
        return f"{BASE_URL.rstrip('/')}{path}/{country}/{comp_id}"

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

        # ONE ROVER, ONE QUOTE AT A TIME. Two callers now reach this: the executor verifying a trade it is
        # about to place, and the sampled verifier measuring board-vs-slip drift. They share a single tab,
        # so overlapping calls would have one navigating while the other is mid-click — and the price that
        # came back would belong to whichever game won the race. Serialise here rather than in each caller:
        # the constraint is the tab's, so the tab's owner should enforce it.
        lock = getattr(self, "_slip_lock", None)
        if lock is None:
            lock = self._slip_lock = _aio.Lock()
        async with lock:
            # FREEZE IDLE ACTIVITY FOR THE WHOLE QUOTE. A quote finds its row by price and then clicks it;
            # an organic scroll landing between those two steps moves the board underneath the click. Held
            # across the entire lock, not just the click, because the row is located first.
            try:
                self.observer.pause_organic()
            except Exception:
                pass
            try:
                return await self._slip_quote_outer(selection_id)
            finally:
                try:
                    self.observer.resume_organic()
                except Exception:
                    pass

    async def _slip_quote_outer(self, selection_id: str) -> dict:
        import asyncio as _aio
        if True:
            # DID THIS QUOTE COST THE VENUE ANYTHING? Most refusals are decided from data we already hold
            # (not available_for_accas, already subscribed, no odds on the row) and never touch the page,
            # so the caller can retry almost immediately instead of spending a whole sampling interval on
            # a "no". Only a real click earns the full cooldown. Safe to keep on the instance: this lock
            # serialises every caller, so there is only ever one quote in flight to attribute it to.
            self._slip_clicked = False
            self._slip_acca_flagged = False
            res = await self._slip_quote_locked(selection_id)
            if isinstance(res, dict):
                res.setdefault("clicked", bool(getattr(self, "_slip_clicked", False)))
                # False = the venue said this event cannot go on an accumulator. Reported on SUCCESS too:
                # a success here is the whole point — it proves the flag does not block a betslip read.
                res.setdefault("acca", not bool(getattr(self, "_slip_acca_flagged", False)))
            return res

    async def await_fill(self, order_id: int, timeout: float = 45.0) -> dict:
        """Block until the venue says this order is finished, then report what ACTUALLY filled.

        Rides the pushed `api` frames the page already receives — no polling, no request, and the whole
        lifecycle arrives here (order open -> bet placing -> bet done -> order done). Measured once at
        13.6s end to end, which is why the default timeout is generous: it is the BROKER routing to an
        underlying book and waiting for that book's acknowledgement, not a network round trip.

        Returns whatever is known when the timeout expires rather than raising. A timeout is NOT a
        failure to place — the order is live either way, and reporting it as unplaced would be the
        dangerous direction: the caller would size a hedge against nothing while real money is on.
        """
        import asyncio as _aio
        deadline = time.time() + timeout
        last = None
        while time.time() < deadline:
            f = self.feed.order_fill(order_id)
            if f.get("done"):
                f["timed_out"] = False
                return f
            last = f
            await _aio.sleep(0.15)
        (last or {}).update({"timed_out": True})
        print(f"[BIA] order {order_id} not finished after {timeout:.0f}s — reporting partial state. "
              f"The order is LIVE; do not treat this as unplaced.", flush=True)
        return last or {"known": False, "done": False, "timed_out": True,
                        "filled_stake": 0.0, "avg_price": None, "bookies": []}

    def order_state(self, order_id: int) -> dict:
        """Non-blocking read of the same thing — for status lines and diagnostics."""
        return self.feed.order_fill(order_id)

    async def slip_close(self) -> dict:
        """Close the betslip this bot opened. Escape, after a human pause.

        THREE THINGS COME OUT OF ONE KEYPRESS, and the third is a bug fix rather than cosmetics:

        1. `unwatch_acca_hcaps` is sent, which RELEASES the subscription. Until now a quoted event stayed
           subscribed for the life of the socket, so once its cached slip price aged out the event became
           permanently unquotable — "already subscribed but no price cached, re-clicking cannot help".
           Every event was effectively one-shot. Closing makes it quotable again.
        2. The venue's own `/web/metrics/` emits `betslip.close` with a duration. Measured 2026-08-14
           across five hand-driven closes (X button, Esc, re-clicking the odds, normal, held-then-closed):
           ALL FIVE reported one. A bot that opens slips and reports none is a distinguishing absence.
        3. It stops slips piling up open in a tab.

        ESCAPE rather than the X: it was the cheapest close tested (a keypress, no cursor travel, no
        element to locate) and produced a metric identical in shape to the button.

        Idempotent — with no slip open this is a no-op, so the caller can fire it unconditionally.
        """
        import asyncio as _aio
        import random as _rnd

        page = getattr(self, "_slip_page", None)
        if page is None:
            return {"ok": True, "closed": False, "reason": "no slip open"}
        lock = getattr(self, "_slip_lock", None)
        if lock is None:
            lock = self._slip_lock = _aio.Lock()
        async with lock:                      # never close underneath a quote that is mid-click
            page = getattr(self, "_slip_page", None)
            if page is None:
                return {"ok": True, "closed": False, "reason": "no slip open"}
            self._slip_page = None
            key = getattr(self, "_slip_open_key", None)
            self._slip_open_key = None
            try:
                if page.is_closed():
                    return {"ok": True, "closed": False, "reason": "the tab holding it is gone"}
                # A person does not hit Escape the instant they finish reading. Jittered, and generous:
                # the whole point is that nothing about this is metronomic.
                lo = float(os.environ.get("BIA_SLIP_CLOSE_DELAY_MIN_MS", "180"))
                hi = float(os.environ.get("BIA_SLIP_CLOSE_DELAY_MAX_MS", "1400"))
                await _aio.sleep(_rnd.uniform(min(lo, hi), max(lo, hi)) / 1000.0)
                # Focus first: Escape goes to the focused document, and the slip may be on a parked tab.
                if os.environ.get("BIA_SLIP_FOCUS_TAB", "1") == "1":
                    try:
                        await page.bring_to_front()
                    except Exception:
                        pass
                await page.keyboard.press("Escape")
                print(f"[BIA SLIP] closed {key} with Escape", flush=True)
                return {"ok": True, "closed": True, "event": list(key) if key else None}
            except Exception as e:
                return {"ok": False, "closed": False, "error": f"{type(e).__name__}: {e}"}

    async def _read_slip_panel(self, page) -> str:
        """The betslip panel's visible text, flattened. "" if it cannot be read.

        WHY THIS EXISTS. `selection_label` -- the venue's OWN name for what the betslip contains -- is the
        input to the executor's same-side guard (SideNamesOppose), the check added after two legs were
        bought on Sabrina Dias. Only the Pinnacle adapter ever returned it, so on BetInAsia that guard has
        been receiving "" and waving every trade through: side identification here rests entirely on
        matching the board PRICE, which is the very thing that cannot separate a near-even market.

        Reading the panel is the only independent answer. Deliberately raw text rather than a parsed name:
        the panel's structure has never been captured, and echoing back the catalog name we already asked
        for would be a cache agreeing with itself -- it would populate the field, satisfy the guard, and
        prove nothing. Look at the text first, parse it second.

        READ VIA LOCATORS, NOT evaluate(). Measured with `test_dom_isolation.py`: a page that patches the
        `innerText` getter sees **1** read from `locator.evaluate(el => el.innerText)` and **0** from
        `locator.inner_text()` — Playwright runs its own operations in an isolated world that page script
        cannot reach or patch, while our JS runs in the main world alongside theirs. The venue does not
        instrument DOM properties today, but our own canary shows how little that would cost them, so the
        ancestor walk is expressed as an XPath locator instead of a loop in their context.
        """
        try:
            loc = page.get_by_text("start acca", exact=False)
            if not await loc.count():
                return ""
            anchor = loc.first
            # `ancestor::*[n]` counts outward from the element (XPath reverse axis), so this is the same
            # walk the old loop did — deepest first, falling back when the tree is shallower.
            for depth in range(6, 0, -1):
                anc = anchor.locator(f"xpath=ancestor::*[{depth}]")
                if await anc.count() == 0:
                    continue
                txt = " ".join(((await anc.first.inner_text()) or "").split())
                if txt:
                    return txt[:4000]
            return " ".join(((await anchor.inner_text()) or "").split())[:4000]
        except Exception as e:
            return f"<unreadable: {type(e).__name__}>"

    # The betslip renders "{Sport} Start Acca {SELECTION} Stake $ Price Place ...". Observed verbatim
    # 2026-08-14 across three sports:
    #     "Baseball Start Acca Cleveland Guardians Moneyline (Inc. Overtime) Stake $ Price Place ..."
    #     "Tennis Start Acca Rei Sakamoto Stake $ Price Place ..."
    #     "MMA Start Acca Gillian Robertson, Moneyline Stake $ Price Place ..."
    _SLIP_LABEL_RE = re.compile(r"Start Acca\s+(.+?)\s+Stake\b")

    @staticmethod
    def _parse_slip_label(panel_text: str) -> str:
        """The competitor the betslip says it holds, or "" when it cannot be read.

        This is the venue's OWN answer to "what did I just click", and it is what feeds the executor's
        same-side guard. Returns "" rather than a guess on any doubt: an empty label leaves the guard in
        the state it has always been in on this venue, whereas a wrong one would actively approve a bet.
        """
        if not panel_text:
            return ""
        m = BetInAsiaAdapter._SLIP_LABEL_RE.search(panel_text)
        if not m:
            return ""
        label = m.group(1).strip()
        # Strip the market descriptor the venue appends, so what remains is the COMPETITOR. Left in, the
        # trailing words become the "last word" the surname test keys on -- "Cleveland Guardians Moneyline
        # (Inc. Overtime)" would be compared on "overtime".
        label = label.split(",")[0]
        label = re.split(r"\bMoneyline\b", label)[0]
        return " ".join(label.split()).strip()

    # The ladder under the slip: one row per book per price level. First row is "BEST PRICE", the rest
    # carry a running "TOTAL". Observed verbatim 2026-08-14:
    #   "4casters BEST PRICE 1.776 $9,852 bf TOTAL $9,881 1.769 $29 sxbet TOTAL $10,388 1.769 $507 ..."
    # (9,852 + 29 = 9,881 and + 507 = 10,388, so TOTAL is cumulative INCLUDING that row.)
    _LADDER_BEST = re.compile(r"([A-Za-z0-9_]+)\s+BEST PRICE\s+([\d.]+)\s+\$([\d,]+(?:\.\d+)?)")
    _LADDER_ROW = re.compile(
        r"([A-Za-z0-9_]+)\s+TOTAL\s+\$[\d,]+(?:\.\d+)?\s+([\d.]+)\s+\$([\d,]+(?:\.\d+)?)")

    @staticmethod
    def _parse_slip_ladder(panel_text: str) -> list[dict]:
        """[{book, odds, stake}] in the order the panel lists them (best price first). [] if unreadable."""
        if not panel_text:
            return []
        rows: list[tuple[int, str, float, float]] = []
        for rx in (BetInAsiaAdapter._LADDER_BEST, BetInAsiaAdapter._LADDER_ROW):
            for m in rx.finditer(panel_text):
                try:
                    rows.append((m.start(), m.group(1), float(m.group(2)),
                                 float(m.group(3).replace(",", ""))))
                except ValueError:
                    continue
        rows.sort(key=lambda r: r[0])          # document order = descending price
        return [{"book": b, "odds": o, "stake": s} for _pos, b, o, s in rows]

    async def _slip_price_from_dom(self, page) -> Optional[float]:
        """Decimal odds the OPEN betslip is currently showing, read off the page. None if not readable.

        AN INDEPENDENT SECOND OPINION ON THE SOCKET. `_slip_books` is a cache fed by `offers_acca_hcap`,
        and when the venue stops pushing there is no way to tell a QUIET market from a DEAD cache — which
        is the whole difficulty behind "already subscribed but no price is cached". The panel is rendered
        from whatever the venue last told the page, so it answers the question the cache cannot: is there
        actually a price on screen right now, and does it match what we remember?

        `.price-input` is the right field rather than the ladder's BEST PRICE row. The ladder is the raw
        pool including books this account cannot use — measured 2026-08-14, its best was 1.776 (4casters,
        an excluded crypto book) while the venue quoted 1.769. The input holds what would actually be
        placed. It is also one of the few HAND-WRITTEN class names on the site (`.price-input`,
        `.stake-input`), so unlike the hashed CSS-module classes it survives a deploy.

        Locator reads only — invisible to page script, and no request.
        """
        if page is None:
            return None
        try:
            inp = page.locator(".price-input")
            if await inp.count():
                raw = (await inp.first.input_value()) or ""
                txt = re.sub(r"[^\d.]", "", raw)
                if txt:
                    odds = float(txt)
                    if 1.0 < odds < 1000.0:
                        return odds
        except Exception:
            pass
        return None

    @staticmethod
    def _stake_at_price(ladder: list[dict], odds: float) -> Optional[float]:
        """Cash available AT the quoted price. None when the ladder cannot answer.

        DELIBERATELY NOT THE CUMULATIVE TOTAL, though that is what a taker would sweep. The rows above our
        price include books this account cannot use -- on 2026-08-14 the baseball BEST PRICE was 4casters
        at 1.776 for $9,852 while the venue actually quoted 1.769, and 4casters is one of the crypto books
        excluded for their artificial pre-live delay. Counting that $9,852 as reachable liquidity would
        inflate depth by 3x on that selection alone.
        Summing only the rows AT the quoted price is provably available at the price we are taking, and it
        errs small: over-stating depth costs real money, under-stating it costs a slightly smaller bet.
        """
        if not ladder or odds <= 0:
            return None
        total = sum(r["stake"] for r in ladder if abs(r["odds"] - odds) < 1e-9)
        return total if total > 0 else None

    async def _slip_quote_locked(self, selection_id: str) -> dict:
        import asyncio as _aio

        parsed = parse_selection_id(selection_id)
        if not parsed:
            return {"ok": False, "error": f"unparseable selection_id '{selection_id}'"}
        sport, comp_id, ekey, market_key, sel = parsed
        if market_key != MONEYLINE_BY_SPORT.get(sport):
            return {"ok": False, "error": f"slip quotes are moneyline-only; '{market_key}' is a derivative"}
        obs = self.observer
        if obs is None:
            return {"ok": False, "error": "no browser page (direct transport cannot open a betslip)"}

        t0 = time.time()
        key = (sport, ekey)

        # ── available_for_accas: A FLAG WE NEVER TESTED ──────────────────────────────────────────────
        # This used to REFUSE outright, on the reasoning that "refusing costs one dict lookup, clicking
        # costs a page load and a timeout". That reasoning was never checked against the venue: not once
        # has this code clicked a non-acca event to find out what actually happens. Meanwhile the flag is
        # a property of the sport/competition (cricket 6/49, esports 46/157, boxing 6/44 vs tennis 155/171),
        # so a cricket-heavy session was refusing nearly every sample on an untested assumption — 22 of 24
        # on 2026-08-14, and the roving tab never opened once all day because the gate fires before it.
        #
        # `available_for_accas` governs ACCUMULATORS. It is a real signal about the price CHANNEL we read
        # (the click sends watch_acca_hcaps), but a single bet is a different product, and the operator has
        # confirmed these markets are bettable by hand. So: try it, say so, and let the result decide.
        # BIA_SLIP_REFUSE_NON_ACCA=1 restores the refusal once there is evidence for it either way.
        ev_meta = self.feed.get_event(sport, ekey) or {}
        acca_flagged = ev_meta.get("available_for_accas") is False
        if acca_flagged and os.environ.get("BIA_SLIP_REFUSE_NON_ACCA") == "1":
            return {"ok": False, "acca": False,
                    "error": f"{ekey} is not available_for_accas and BIA_SLIP_REFUSE_NON_ACCA=1"}
        self._slip_acca_flagged = acca_flagged   # reported back so the outcome can be attributed
        if acca_flagged:
            print(f"[BIA SLIP] {ekey} is flagged NOT available_for_accas — quoting anyway to find out "
                  f"whether that actually blocks a betslip read", flush=True)

        # ── ALREADY SUBSCRIBED? Then do not click at all. ────────────────────────────────────────────
        # Proved 2026-08-11: the acca subscription behaves like the board one — once an event is
        # subscribed the venue KEEPS PUSHING updates (two further offers_acca_hcap arrived after a quote
        # returned), and a REPEAT watch_acca_hcaps is answered with `event_already_subscribed` INSTEAD of
        # a price. That is why every re-quote of the same event timed out while a fresh event worked
        # first time in 686ms. So a second click is not just wasteful, it is actively self-defeating.
        # Reading the live cache instead is faster AND removes a UI action, which is the direction the
        # anti-detection constraint pushes anyway.
        # ALREADY SUBSCRIBED means the price is LIVE, however old the last push is. Same reasoning the
        # board feed already uses: on a push-only venue the venue stamps a timestamp only when it actually
        # sends, so an old ts means A QUIET MARKET, not a stale price — the venue would have pushed if
        # anything changed. Requiring a recent push here was an outright BUG: a quiet subscribed market
        # aged past the limit, we clicked again, got `event_already_subscribed` (no price), and the quote
        # timed out on a price that was live the whole time. That is what produced the accumulating errors.
        subs = getattr(self.observer, "_acca_subs", None)
        subscribed = bool(subs is not None and key in subs)
        max_age = float(os.environ.get("BIA_SLIP_MAX_AGE_SEC", "10"))
        if subscribed:
            # Bounded only by the FEED being alive; a dead socket loses the subscription anyway.
            try:
                max_age = float((self.feed_health() or {}).get("stale_after_sec") or max_age)
            except Exception:
                pass
        cached = self.feed._slip_books.get(key)
        if cached and (time.time() - cached.get("ts", 0.0)) <= max_age:
            entry = (cached.get("markets") or {}).get(market_key)
            if entry:
                _line, sels = entry
                odds = sels.get(sel)
                if odds and odds > 1.0:
                    age = round(time.time() - cached.get("ts", 0.0), 2)
                    print(f"[BIA SLIP] {selection_id} -> {odds} from the live slip feed "
                          f"(age {age}s, {'subscribed' if subscribed else 'recent'}, no click)", flush=True)
                    return {"ok": True, "decimal_odds": odds,
                            "implied_price": round(1.0 / odds, 6),
                            "via": "cache",          # no click at all — see the subscribed fast path
                            "elapsed_ms": round((time.time() - t0) * 1000, 1),
                            "from_cache": True, "age_sec": age, "selection_id": selection_id}

        if subscribed:
            # ── SUBSCRIBED, NOTHING CACHED: ASK THE PAGE BEFORE GIVING UP ────────────────────────────
            # Clicking again genuinely cannot help — the venue answers `event_already_subscribed` and
            # pushes no price. But that only rules out the SOCKET. If a slip is open on screen it was
            # rendered from a price the venue did send, and the DOM still holds it. The cache being empty
            # and the venue having nothing to offer are different states, and this used to report both as
            # the same refusal ("the feed may be stale") without ever checking which.
            slip_pg = getattr(self, "_slip_page", None)
            if slip_pg is None or slip_pg.is_closed():
                slug = sports_cfg.bia_path_by_code().get(sport, "")
                slip_pg = await obs.sport_tab(sport, BASE_URL.rstrip("/") + slug if slug else "")
            dom_odds = await self._slip_price_from_dom(slip_pg)
            if dom_odds:
                panel_text = await self._read_slip_panel(slip_pg)
                ladder = self._parse_slip_ladder(panel_text)
                real_stake = self._stake_at_price(ladder, dom_odds)
                if real_stake is not None:
                    self._slip_depth[selection_id] = (real_stake, time.time())
                print(f"[BIA SLIP] {ekey}: socket had nothing cached, but the OPEN BETSLIP reads "
                      f"{dom_odds} — using the page", flush=True)
                return {"ok": True, "decimal_odds": dom_odds,
                        "implied_price": round(1.0 / dom_odds, 6),
                        "via": "dom", "from_dom": True,
                        "selection_label": self._parse_slip_label(panel_text),
                        "slip_panel_text": panel_text,
                        "max_stake": real_stake, "ladder": ladder[:24],
                        "elapsed_ms": round((time.time() - t0) * 1000, 1),
                        "selection_id": selection_id}
            # Nothing on screen either. If a slip IS open it is the thing holding the subscription, so
            # closing it sends `unwatch_acca_hcaps` and frees the event for a real re-click NEXT time —
            # turning a permanent dead end into a one-cycle delay. Costs a keypress.
            freed = False
            if slip_pg is not None and not slip_pg.is_closed():
                try:
                    if await slip_pg.get_by_text("start acca", exact=False).count():
                        await slip_pg.keyboard.press("Escape")
                        freed = True
                except Exception:
                    pass
            return {"ok": False,
                    "error": (f"{ekey} is already subscribed on the betslip channel, nothing is cached, "
                              f"and the page shows no price for {market_key}/{sel} either. "
                              + ("Closed the open slip to release the subscription — the next quote can "
                                 "re-click." if freed else
                                 "No slip is open to close, so the subscription cannot be released until "
                                 "this socket cycles."))}

        before_ts = (cached or {}).get("ts", 0.0)
        try:
            # BOARD TAB FIRST, ROVER AS FALLBACK.
            # The parked per-sport tab is already rendering this sport's whole slate, fully expanded, so
            # for the low-volume sports this bot covers the row is usually ALREADY ON SCREEN — making the
            # quote a pure find-and-click with no navigation at all. Only when the board does not carry the
            # game do we pay for the rover to go and find its league, which is the cost that lost the
            # Vandecasteele arb (>20s, cold league) on 2026-08-13.
            via = "sport-tab"
            slug = sports_cfg.bia_path_by_code().get(sport, "")
            board_url = BASE_URL.rstrip("/") + slug if slug else ""
            page = await obs.sport_tab(sport, board_url)
            row, n_links = (None, 0) if page is None else await self._find_board_row(page, ekey)
            if row is None:
                via = "rover"
                page = await obs.rover()
                if page is None:
                    return {"ok": False, "error": "could not open the roving tab"}
                row, n_links = await self._find_board_row(page, ekey)
            # NAVIGATE TO THE LEAGUE. The rover starts blank and generally is not showing this game, so
            # "row missing" is the normal first state, not an error. The event frames carry `country` and
            # `competition_id` on 100% of events, which is exactly the league page's address — so the
            # rover goes straight there instead of loading the whole sport board and expanding it.
            if row is None:
                url = self._league_url(sport, ekey, comp_id)
                if url and page.url.rstrip("/") != url.rstrip("/"):
                    print(f"[BIA SLIP] rover -> {url}", flush=True)
                    if await obs.rover(url) is None:
                        return {"ok": False, "error": f"rover could not reach {url}"}
                    await _aio.sleep(float(os.environ.get("BIA_ROVER_SETTLE_SEC", "1.0")))
                    row, n_links = await self._find_board_row(page, ekey)
            # The competition may still be collapsed -- its rows do not exist in the DOM until "Show more"
            # is expanded, so this is a PRECONDITION of finding the row, not a fallback.
            if row is None:
                for _ in range(int(os.environ.get("BIA_SHOW_MORE_CLICKS", "6"))):
                    more = page.get_by_text("Show more", exact=True)
                    if await more.count() == 0:
                        break
                    await CURSOR.click(page, more.first, timeout=5_000)
                    await _aio.sleep(0.4)
                    if await page.locator(f'a[href*="{ekey}"]').count():
                        break
                row, n_links = await self._find_board_row(page, ekey)
            if row is None:
                return {"ok": False,
                        "error": (f"no board ROW for {ekey} — {n_links} link(s) carry this event key but "
                                  f"none render price cells (rover is on {page.url!r}; the league page may "
                                  f"not list this game, or the row is not expanded)")}

            # CROSS-CHECK the href before clicking. The event key alone already identifies the match; sport
            # and comp_id are two more independent confirmations that cost nothing.
            if n_links > 1:
                print(f"[BIA SLIP] {ekey}: {n_links} links carry this key; using the one with price cells",
                      flush=True)
            href = await row.get_attribute("href") or ""
            if f"/{sport}/" not in href:
                return {"ok": False, "error": f"row href {href!r} is not sport {sport} -- refusing to click"}
            # COMP_ID IS A WARNING, NOT A VETO. The competition id is baked into the selection_id at pairing
            # time and the venue re-numbers competitions: measured 2026-08-14, a CPL fixture whose event key
            # matched EXACTLY was refused because the token said comp 60642 while the live href said 62718.
            # The event key is the identity here -- a date plus both team ids, and it is what the <a> was
            # selected by -- so a stale comp id cannot make this the wrong game, only the wrong league URL.
            # Refusing on it threw away correctly-identified cricket fixtures to protect against nothing.
            if f"/{comp_id}/" not in href:
                print(f"[BIA SLIP] {ekey}: token says comp {comp_id} but the row says {href!r} — "
                      f"stale competition id in the pair file; event key matches, so clicking anyway",
                      flush=True)

            # Locate the exact clickable cell BY PRICE — layout-independent (see _find_price_cell).
            book = self.feed._books.get((sport, ekey)) or {}
            mk_entry = (book.get("markets") or {}).get(market_key)
            if not mk_entry:
                return {"ok": False, "error": f"no board price cached for {market_key} — "
                                              f"cannot identify which cell is '{sel}'"}
            want = (mk_entry[1] or {}).get(sel)
            if not want or want <= 1.0:
                return {"ok": False, "error": f"no board price for selection '{sel}'"}
            cell, n_links, why = await self._find_price_cell(page, ekey, want)
            if cell is None:
                return {"ok": False, "error": why}
            if n_links > 1:
                print(f"[BIA SLIP] {ekey}: {n_links} links carry this key; matched the cell showing {want}",
                      flush=True)

            # ── ANTI-DETECTION: NEVER CLICK INTO A HIDDEN TAB ────────────────────────────────────────
            # Playwright dispatches through CDP, so the event carries isTrusted:true and looks real. What
            # does NOT look real is WHEN it arrives. A background tab reports
            # `document.visibilityState === "hidden"`, and a human cannot click a tab they are not looking
            # at — so a trusted click landing in a hidden document is a signature no genuine user can
            # produce, readable by ordinary page JS with no fingerprinting required. Parked per-sport tabs
            # made every quote today exactly that: five boards open, at most one visible.
            # bring_to_front() costs ~0.3s and makes the action physically possible. The sampler is
            # rate-limited to roughly one quote a minute, so the resulting tab switching is well inside
            # what a person watching several boards does anyway.
            # BIA_SLIP_FOCUS_TAB=0 restores the old behaviour; the visibility state is reported either way
            # so the choice is never silent.
            if os.environ.get("BIA_SLIP_FOCUS_TAB", "1") == "1":
                try:
                    await page.bring_to_front()
                    await _aio.sleep(float(os.environ.get("BIA_SLIP_FOCUS_SETTLE_SEC", "0.35")))
                except Exception as fe:
                    print(f"[BIA SLIP] could not focus the tab before clicking "
                          f"({type(fe).__name__}) — clicking into a possibly hidden tab", flush=True)
            # READING visibilityState IS ITSELF A MAIN-WORLD ACT, so it is DIAGNOSTIC-ONLY and off by
            # default. bring_to_front() is what makes the click legitimate; this only confirmed it — and
            # confirming it put a `document.visibilityState` read in the page's own context immediately
            # before every click, creating a read-then-click correlation that would not otherwise exist.
            # The venue polls that property 1.1x/s of its own accord, so one more read hides in the noise,
            # but the TIMING does not. Set BIA_SLIP_CHECK_VISIBILITY=1 when verifying by hand.
            vis = {}
            if os.environ.get("BIA_SLIP_CHECK_VISIBILITY") == "1":
                try:
                    vis = await page.evaluate(
                        "() => ({state: document.visibilityState, focused: document.hasFocus()})")
                except Exception:
                    vis = {}
                if vis.get("state") == "hidden":
                    print(f"[BIA SLIP] WARNING clicking while document.visibilityState=hidden — "
                          f"a human cannot do this; the venue can see it", flush=True)

            url_before = page.url
            slips_before = len(self.feed._slip_books)
            # HUMAN APPROACH, then a real click. `locator.click()` still does the clicking — it re-resolves
            # the element's live position, which matters on a board that reorders as odds tick — but the
            # cursor now travels there along a curved, decelerating path instead of appearing on the cell.
            # A page that records mousemove sees a reach, not a teleport. See human_mouse.py.
            if not await CURSOR.click(page, cell, timeout=5_000):
                return {"ok": False, "error": "the price cell could not be clicked"}
            self._slip_clicked = True      # past this point the venue has seen a UI action
            # Remember WHERE the slip is so it can be closed later (see slip_close). Without this the
            # bot opens betslips and never closes one — measured 2026-08-14 as the clearest difference
            # between a bot session and a hand-driven one.
            self._slip_page, self._slip_open_key = page, key
            await _aio.sleep(0.5)
            # BRING THE SLIP INTO VIEW. Reading it does not require this — inner_text and input_value work
            # on an off-screen node — but a person cannot open a betslip and then not look at it, and the
            # placement path will need the Place button genuinely clickable. Wheeled, not jumped.
            try:
                pnl = page.get_by_text("start acca", exact=False)
                if await pnl.count():
                    pbox = await pnl.first.bounding_box()
                    if pbox and not (VIEW_TOP <= pbox["y"] <= VIEW_BOTTOM):
                        await CURSOR.scroll(page, pbox["y"] - VIEW_REST)
            except Exception:
                pass
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
                            print(f"[BIA SLIP] {selection_id} -> {odds} via {via} in {ms:.0f}ms", flush=True)
                            panel_text = await self._read_slip_panel(page)
                            slip_label = self._parse_slip_label(panel_text)
                            ladder     = self._parse_slip_ladder(panel_text)
                            real_stake = self._stake_at_price(ladder, odds)
                            # CROSS-CHECK THE SOCKET AGAINST THE SCREEN. The slip is open right now, so
                            # both numbers exist at the same instant — the one moment they can be compared
                            # without the timestamp-alignment problem that made the 2026-08-11 board-vs-slip
                            # reading wrong. A disagreement means the cache is not what the venue is
                            # showing, which is the failure mode `_slip_books` cannot self-report.
                            dom_odds = await self._slip_price_from_dom(page)
                            if dom_odds and abs(dom_odds - odds) > 0.001:
                                print(f"[BIA SLIP] PRICE DISAGREEMENT {ekey}: socket says {odds}, the "
                                      f"open betslip shows {dom_odds} — trusting the socket, but the "
                                      f"cache is not what the venue is displaying", flush=True)
                            if real_stake is not None:
                                # REAL DEPTH, replacing BIA_ASSUMED_MAX_STAKE for this selection.
                                self._slip_depth[selection_id] = (real_stake, time.time())
                            if slip_label:
                                print(f"[BIA SLIP] betslip says: {slip_label!r}"
                                      + (f", ${real_stake:,.0f} at {odds}" if real_stake else ""),
                                      flush=True)
                            return {"ok": True, "decimal_odds": odds,
                                    "implied_price": round(1.0 / odds, 6),
                                    # Cash available AT the quoted price, read off the slip's own ladder.
                                    # None => nothing parsed => callers keep the assumed constant.
                                    "max_stake": real_stake,
                                    "ladder": ladder[:24],
                                    # The same price as the OPEN slip renders it. Equal to decimal_odds
                                    # means the socket cache and the screen agree; a difference is the
                                    # cache diverging from what the venue is actually showing.
                                    "dom_odds": dom_odds,
                                    # What the DOCUMENT reported at click time. "hidden" here means the
                                    # venue saw a click no human could have made.
                                    "visibility": vis,
                                    # The venue's own name for what it put on the slip -> feeds the
                                    # executor's same-side guard, which has been inert here until now.
                                    "selection_label": slip_label,
                                    # Raw text kept alongside it: the parse is a regex over a layout that
                                    # can change, and an operator needs to be able to see what it read.
                                    "slip_panel_text": panel_text,
                                    # WHICH TIER SERVED THIS. The caller paces itself by this: a
                                    # sport-tab quote is a find-and-click on a parked board (~0.5s) and
                                    # can be sampled often; a rover quote navigates to the league and is
                                    # both slow and a real UI action, so it earns a much longer cooldown.
                                    "via": via,
                                    "elapsed_ms": ms, "selection_id": selection_id}
                await _aio.sleep(0.1)
            # Timed out. Report WHAT WE OBSERVED so the next attempt does not need another guess:
            #   slip_books_grew   -> the venue IS pushing slip prices, just not for this event/market
            #   book_present      -> we have a book for the event but the moneyline key is absent from it
            #   slip_panel_seen   -> the panel really did open, so the click path is right
            bk = self.feed._slip_books.get(key) or {}
            panel = 0
            panel_text = ""
            try:
                panel = await page.get_by_text("start acca", exact=False).count()
                # WHAT IS ACTUALLY IN THE PANEL. Measured 2026-08-14 on a non-acca cricket match: the click
                # landed, `slip_panel_seen` was 1 — the panel really did open — and yet no offers_acca_hcap
                # ever arrived. So the acca CHANNEL is a dead end for these events, but the panel itself is
                # on screen, and if it renders a price then a DOM read gets what the socket will not.
                # Shares _read_slip_panel rather than repeating the walk: that one is locator-based and so
                # invisible to a page that instruments innerText, and a second copy would have kept the
                # main-world read alive on exactly the diagnostic path used when something is already wrong.
                if panel:
                    panel_text = await self._read_slip_panel(page)
            except Exception as pe:
                panel_text = f"<unreadable: {type(pe).__name__}>"
            return {"ok": False,
                    "error": f"no offers_acca_hcap for {ekey}/{market_key}/{sel} within the wait window",
                    "diag": {"slip_books_before": slips_before,
                             "slip_books_now": len(self.feed._slip_books),
                             "book_present": bool(bk),
                             "markets_in_book": sorted((bk.get("markets") or {}).keys())[:12],
                             "slip_panel_seen": panel,
                             "panel_text": panel_text,
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
        # socket COUNT is the reconnect signal: the OBSERVING tab holds one socket for hours, so a rising
        # count means it reloaded or dropped and came back (board subscriptions do NOT survive that).
        # Since the rover was added this is no longer a clean alarm — every rover navigation is a new
        # socket by design. Read it against `tabs.rover` below rather than on its own.
        out["sockets"] = getattr(obs, "_sockets", None)
        out["socket_urls"] = list(getattr(obs, "_socket_urls", []) or [])   # already token-redacted
        # THE TWO TABS, side by side. The observing tab should sit still (its accumulated subscriptions
        # are the feed); the rover is the only one that moves. If `observing` starts tracking `rover`,
        # something is navigating the wrong tab and the board will go with it.
        rv = getattr(obs, "_rover", None)
        out["tabs"] = {
            "observing": (getattr(obs, "_page", None).url
                          if getattr(obs, "_page", None) is not None else None),
            "rover": (rv.url if rv is not None and not rv.is_closed() else None),
            "rover_open": rv is not None and not rv.is_closed(),
        }
        # Betslip subscriptions, per socket. A quote REFUSES to click an already-subscribed event, so a
        # count that only ever grows (especially across reconnects) is the shape of the old global-set bug.
        out["acca_subs"] = {"total": len(getattr(obs, "_acca_subs", ()) or ()),
                            "by_socket": [len(v) for v in
                                          (getattr(obs, "_acca_by_ws", {}) or {}).values()]}
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
        # How many selections are sized on a MEASURED depth vs the announced guess. A guessed number that
        # looks like a measured one is the failure mode this whole field exists to prevent, so say which.
        now = time.time()
        live_depth = {k: v for k, v in self._slip_depth.items()
                      if (now - v[1]) <= self._slip_depth_ttl}
        # DETECTION CANARY. `tripped` non-empty means the venue read an API it read ZERO times in the
        # baseline — i.e. its behaviour changed and the current anti-detection posture is out of date.
        try:
            can = getattr(self.observer, "_canary", None)
            if can is not None:
                s["canary"] = can.report()
        except Exception:
            pass
        s.update({"book": self.name, "currency": self.feed.currency,
                  "assumed_max_stake": ASSUMED_MAX_STAKE,
                  "real_depth": {"selections": len(live_depth),
                                 "ttl_sec": self._slip_depth_ttl,
                                 "median": round(sorted(v[0] for v in live_depth.values())
                                                 [len(live_depth) // 2], 2) if live_depth else None,
                                 "note": "read off the betslip ladder at the quoted price; everything "
                                         "else falls back to assumed_max_stake"},
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
