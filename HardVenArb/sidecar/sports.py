"""
sports.py — the SINGLE source of truth for which sports the bot runs and how each maps across the two venues.

"The sports we run may change each time" used to mean editing 4 places (schedule --sports, PINNACLE_LIFECYCLE_
SPORTS, pairHard's CLASSIC_SERIES, pair_derivatives' KALSHI_SERIES). Now every consumer reads from here:
  - schedule.py / lifecycle / adapter  → Pinnacle sport ids, display names, per-sport game DURATION
  - pair_derivatives.py                → Kalshi spread/total series -> ("spread"|"total", Pinnacle sport id)
  - pairHard.py                        → Kalshi moneyline series allowlist (the default scaffold scope)

ADD a sport = add one entry to CATALOG. CHANGE the active set = set env HARDVEN_SPORTS (comma keys, e.g.
"baseball,tennis" or just "tennis"), or flip `enabled=` in the catalog. Unset HARDVEN_SPORTS = every enabled
entry below. Everything downstream follows from that one choice.
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass


@dataclass(frozen=True)
class Sport:
    key: str                      # our name, e.g. "baseball"
    pinnacle_id: int              # Pinnacle sport id (3 = baseball, 33 = tennis); 0 = NOT paired on Pinnacle
    duration_min: int             # typical match length (minutes) — the window's post-game tail
    moneyline: tuple[str, ...]    # Kalshi per-game WINNER series (pairHard allowlist)
    spread: tuple[str, ...] = ()  # Kalshi SPREAD series (pair_derivatives)
    total: tuple[str, ...] = ()   # Kalshi TOTAL series (pair_derivatives)
    # ── BetInAsia side ───────────────────────────────────────────────────────────────────────────────────
    bia_sport: str = ""           # BIA sport code = the MONEYLINE_BY_SPORT key ("" = not carried on BIA)
    bia_path: str = ""            # BIA sportsbook slug. ONLY set from a slug OBSERVED in a real capture —
                                  # a guessed URL is a 404 the observer would report as "no coverage".
    enabled: bool = True          # default-ON set when HARDVEN_SPORTS is unset. New sports ship OFF so the
                                  # LIVE Pinnacle bot's behaviour cannot change under it; select explicitly.


# ── the catalog (add a sport = add an entry) ──────────────────────────────────────────────────────────────
CATALOG: dict[str, Sport] = {
    "baseball": Sport(
        key="baseball", pinnacle_id=3, duration_min=210,
        # KXLMBGAME (Mexican league) added 2026-08-10 off the live enumeration — 4 series / 98 open games.
        moneyline=("KXMLBGAME", "KXKBOGAME", "KXNPBGAME", "KXLMBGAME"),
        spread=("KXMLBSPREAD", "KXKBOSPREAD"),               # NPB has no Kalshi spread/total
        total=("KXMLBTOTAL", "KXKBOTOTAL"),
        bia_sport="baseball", bia_path="/sportsbook/baseball",
    ),
    "tennis": Sport(
        key="tennis", pinnacle_id=33, duration_min=180,
        # NOTE: KXATPEXACTMATCH is deliberately ABSENT — it is "ATP Exact Match Score" (a set-score market),
        # NOT a moneyline. Pairing it would hedge a winner against a scoreline.
        moneyline=("KXATPMATCH", "KXWTAMATCH", "KXITFMATCH", "KXITFWMATCH",
                   "KXATPCHALLENGERMATCH", "KXWTACHALLENGERMATCH"),
        spread=("KXATPGSPREAD",),   # games handicap — ATP only (WTA/ITF have no Kalshi games markets)
        total=("KXATPGTOTAL",),
        bia_sport="tennis", bia_path="/sportsbook/tennis",
    ),
    "soccer": Sport(
        key="soccer", pinnacle_id=29, duration_min=150,   # 90' + half + stoppage + settle tail
        # 3-way (home/draw/away) — the catalog + pair_pinnacle tag soccer three_way and pair NO-only
        # (Kalshi NO + Pinnacle back-this-outcome). All confirmed "Team/Team/Tie" per-game winner series.
        # In-season drivers: MLS, Liga MX, UCL qualifiers (July), World Cup (live now, "Regulation Time
        # Moneyline"). Plus USL/USL Cup + off-season club leagues (La Liga 2, Chile Primera, Bolivia Primera)
        # that fill when they run.
        # Enumerated live off Kalshi 2026-08-10: 18 series carrying 684 open per-match markets (~342 ties),
        # 5.6x the whole tennis book. The previous list held only the Americas + UCL and missed EVERY major
        # European league — EPL, La Liga, Serie A, Ligue 1, Eredivisie, Bundesliga 2, EFL Championship,
        # Brasileiro A/B/C, Turkish Super Lig, Danish Superliga, UAE — i.e. most of the market.
        # Out-of-season entries are harmless (they simply return no games), so seasonal ones are kept.
        # ENUMERATED LIVE 2026-08-10: all 59 series Kalshi actually had open (651 games). The previous
        # hand-written list of 23 was itself an expansion of an earlier 9 — twice it was short, so this is
        # now taken wholesale from the API rather than curated. Out-of-season entries return no games.
        moneyline=("KXMLSGAME", "KXCLUBFGAME", "KXDFBPOKALGAME", "KXUECLGAME", "KXARGNACBGAME",
                   "KXBRASILEIROGAME", "KXLIGAMXGAME", "KXLEAGUESCUPGAME", "KXLALIGAGAME",
                   "KXARGPREMDIVGAME", "KXCOPPAITALIAGAME", "KXUSLGAME", "KXUELGAME", "KXDIMAYORGAME",
                   "KXBRASILEIROCGAME", "KXEFLCHAMPIONSHIPGAME", "KXBRASILEIROBGAME", "KXEFLL1GAME",
                   "KXLALIGA2GAME", "KXVENFUTVEGAME", "KXEPLGAME", "KXSERIEAGAME", "KXURYPDGAME",
                   "KXLIGAPORTUGALGAME", "KXECULPGAME", "KXJLEAGUEGAME", "KXUCLGAME", "KXLIGUE1GAME",
                   "KXSUPERLIGGAME", "KXPERLIGA1GAME", "KXEKSTRAKLASAGAME", "KXBELGIANPLGAME",
                   "KXEREDIVISIEGAME", "KXBUNDESLIGA2GAME", "KXSAUDIPLGAME", "KXALLSVENSKANGAME",
                   "KXNWSLGAME", "KXLIGAEXPGAME", "KXCZEFLGAME", "KXSCOCUPGAME", "KXCHNSLGAME",
                   "KXCONMEBOLSUDGAME", "KXCONMEBOLLIBGAME", "KXCHLLDPGAME", "KXAPFDDHGAME",
                   "KXELITESERIENGAME", "KXUAEPLGAME", "KXDENSUPERLIGAGAME", "KXKLEAGUEGAME",
                   "KXHNLGAME", "KXASEANGAME", "KXCANPLGAME", "KXUSLCUPGAME", "KXAFCCLGAME",
                   "KXFRASUPERCUPGAME", "KXENGCSGAME", "KXBOLPDIVGAME", "KXUEFASCGAME", "KXEFLCUPGAME",
                   "KXWCGAME"),   # WC kept from the old list: seasonal, absent from the Aug enumeration
        spread=(),   # soccer Asian-handicap / goal-totals derivatives = future work (3-way base needs its own pairing)
        total=(),
        bia_sport="fb", bia_path="/sportsbook/football",
    ),

    # ── WIDE-TELEMETRY SPORTS (added 2026-08-10) ─────────────────────────────────────────────────────────
    # All ship enabled=False: they are for BIA breadth measurement and must not silently change what the
    # live Pinnacle bot runs. Select them explicitly (HARDVEN_SPORTS=... or "all").
    # pinnacle_id=0 == "we have not established this sport's Pinnacle id" — the Pinnacle helpers skip those,
    # so an unset id can never be mistaken for sport 0.
    # Kalshi series enumerated live 2026-08-10; durations are ESTIMATES (they only size the window tail).
    "basketball": Sport(
        key="basketball", pinnacle_id=0, duration_min=150,
        # NBA/NCAAB/Euroleague are OFF-SEASON now (verified to exist via /series) — they ramp in October and
        # are what makes this sport worth carrying; the six live ones are minor leagues.
        moneyline=("KXNBAGAME", "KXNCAABGAME", "KXEUROLEAGUEGAME", "KXWNBAGAME",
                   "KXLNBPGAME", "KXPBAGAME", "KXVBAGAME", "KXBSNGAME", "KXCEBLGAME"),
        bia_sport="basket", bia_path="/sportsbook/basketball", enabled=False,
    ),
    "amfootball": Sport(
        key="amfootball", pinnacle_id=0, duration_min=240,
        moneyline=("KXNFLGAME", "KXNCAAFGAME", "KXCFLGAME"),
        bia_sport="af", bia_path="/sportsbook/american-football", enabled=False,
    ),
    "cricket": Sport(
        key="cricket", pinnacle_id=0, duration_min=480,   # T20 ~4h but ODI runs ~8h — sized for the long form
        moneyline=("KXT20MATCH", "KXWT20MATCH", "KXODIMATCH", "KXCPLMATCH",
                   "KXHUNDREDMATCH", "KXWHUNDREDMATCH"),
        bia_sport="cricket", bia_path="/sportsbook/cricket", enabled=False,
    ),
    "esports": Sport(
        key="esports", pinnacle_id=0, duration_min=180,
        moneyline=("KXLOLGAME", "KXCS2GAME", "KXVALORANTGAME", "KXDOTA2GAME", "KXR6GAME", "KXXAIGAME"),
        bia_sport="esports", bia_path="/sportsbook/esports", enabled=False,
    ),
    "mma": Sport(
        key="mma", pinnacle_id=0, duration_min=300,   # event-driven: the whole card, not one fight
        moneyline=("KXUFCFIGHT",),
        bia_sport="mma", bia_path="/sportsbook/mma", enabled=False,
    ),
    "boxing": Sport(
        key="boxing", pinnacle_id=0, duration_min=300,
        moneyline=("KXBOXING", "KXFLOYDTYSONFIGHT"),
        bia_sport="boxing", bia_path="/sportsbook/boxing", enabled=False,
    ),
    "icehockey": Sport(
        key="icehockey", pinnacle_id=0, duration_min=180,
        moneyline=("KXNHLGAME",),   # off-season now; ramps October
        bia_sport="ih", bia_path="/sportsbook/ice-hockey", enabled=False,
    ),
    "darts": Sport(
        key="darts", pinnacle_id=0, duration_min=180,
        moneyline=("KXDARTSMATCH",),
        # bia_path deliberately EMPTY: no /sportsbook/darts slug appears in any capture, so it is unverified.
        # The observer skips pathless sports rather than navigating to a guessed 404.
        bia_sport="darts", bia_path="", enabled=False,
    ),
}


# ── the active set ────────────────────────────────────────────────────────────────────────────────────────
def enabled_sports() -> list[Sport]:
    """The sports the bot runs THIS session: env HARDVEN_SPORTS (comma keys) if set to any known key, else
    every entry marked enabled in the catalog. Unknown keys are warned + skipped (never crash)."""
    sel = (os.environ.get("HARDVEN_SPORTS") or "").strip()
    if sel:
        keys = [k.strip().lower() for k in sel.split(",") if k.strip()]
        # "all" = the entire catalog, including the enabled=False wide-telemetry sports. This is the BIA
        # breadth mode; explicit selection has always overridden `enabled`, so this just names that.
        if "all" in keys:
            return list(CATALOG.values())
        unknown = [k for k in keys if k not in CATALOG]
        if unknown:
            print(f"[SPORTS] HARDVEN_SPORTS: unknown key(s) {unknown}; known: {sorted(CATALOG)}", file=sys.stderr)
        chosen = [CATALOG[k] for k in keys if k in CATALOG]
        if chosen:
            return chosen
    return [s for s in CATALOG.values() if s.enabled]


def pinnacle_ids() -> list[int]:
    """Pinnacle sport ids for the active sports (schedule / lifecycle / adapter).

    Skips `pinnacle_id == 0`, which means "no Pinnacle id established for this sport" — the BIA-only
    wide-telemetry entries. Without the filter, selecting them would hand Pinnacle a literal sport 0."""
    return [s.pinnacle_id for s in enabled_sports() if s.pinnacle_id]


def name_by_id() -> dict[int, str]:
    """Pinnacle id -> our name, for the WHOLE catalog (display + duration lookup never miss on an id).
    Entries with no Pinnacle id are excluded — they would otherwise all collide on key 0."""
    return {s.pinnacle_id: s.key for s in CATALOG.values() if s.pinnacle_id}


def duration_by_name() -> dict[str, int]:
    """Our name -> game-length minutes, WHOLE catalog (the window tail)."""
    return {s.key: s.duration_min for s in CATALOG.values()}


def moneyline_series() -> set[str]:
    """Kalshi per-game winner series for the active sports — pairHard's default scaffold allowlist."""
    return {m for s in enabled_sports() for m in s.moneyline}


def derivative_series() -> dict[str, tuple[str, int]]:
    """Kalshi spread/total series -> ('spread'|'total', pinnacle_id) for the active sports — pair_derivatives."""
    out: dict[str, tuple[str, int]] = {}
    for s in enabled_sports():
        for ser in s.spread:
            out[ser] = ("spread", s.pinnacle_id)
        for ser in s.total:
            out[ser] = ("total", s.pinnacle_id)
    return out


def bia_sports() -> list[Sport]:
    """Active sports BetInAsia actually carries (has a `bia_sport` code). Everything else is Pinnacle-only."""
    return [s for s in enabled_sports() if s.bia_sport]


def bia_sport_codes() -> list[str]:
    """BIA sport codes for the active sports — what `pair_betinasia.py --sport` consumes."""
    return [s.bia_sport for s in bia_sports()]


def bia_path_by_code() -> dict[str, str]:
    """{bia_sport_code: sportsbook slug} for the active sports carrying a VERIFIED slug.

    The rover builds league URLs (`{slug}/{country}/{comp_id}`) from this. Sports with an empty
    `bia_path` are omitted rather than guessed: an unverified slug is a 404, and navigating the rover
    into a 404 looks exactly like "this game has no betslip"."""
    return {s.bia_sport: s.bia_path for s in bia_sports() if s.bia_path}


def bia_paths(base: str = "https://black.betinasia.com") -> list[tuple[str, str]]:
    """[(bia_sport_code, absolute sportsbook URL)] for the active sports — the observer's visit list.

    Sports with an EMPTY `bia_path` are skipped: an unverified slug is a 404, and a 404 renders no board,
    which the coverage report would read as "this sport has no games" rather than "we never got there"."""
    return [(s.bia_sport, base.rstrip("/") + s.bia_path) for s in bia_sports() if s.bia_path]


if __name__ == "__main__":
    act = enabled_sports()
    print(f"[SPORTS] active: {[s.key for s in act]}  (HARDVEN_SPORTS={os.environ.get('HARDVEN_SPORTS') or '<all enabled>'})")
    print(f"  pinnacle ids   : {pinnacle_ids()}")
    print(f"  moneyline      : {len(moneyline_series())} series")
    print(f"  derivatives    : {derivative_series()}")
    print(f"  BIA codes      : {bia_sport_codes()}")
    for code, url in bia_paths():
        print(f"    {code:9s} {url}")
    missing = [s.key for s in enabled_sports() if s.bia_sport and not s.bia_path]
    if missing:
        print(f"  BIA path UNVERIFIED (skipped by the observer): {missing}")
