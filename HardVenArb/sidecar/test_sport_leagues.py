"""The team-sport league filter on the pairing catalog walk (2026-10-16).

The walk costs two guest requests per Pinnacle league, every pairing run, from the operator's IP. Basketball is
102 Pinnacle leagues and hockey 33, against ~25 that any Kalshi series can use, so sports.PINNACLE_LEAGUES maps
each Kalshi series to its Pinnacle league NAME and the walk keeps only the leagues of the ALLOWED series. This
pins: the name rules, the exact leagues kept on the real 2026-10-16 board, that tennis/baseball/football walk
exactly as before, and that every basketball/hockey series is mapped, anchored and 2-way-guarded.

    python test_sport_leagues.py          # exit 0 = all pass
"""
from __future__ import annotations
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sports as S  # noqa: E402

PASS = FAIL = 0


def check(name, ok, got=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}" + (f"  (got {got})" if got != "" else ""))


# The guest /sports/{id}/leagues listing, 2026-10-16 (id, name, matchupCount).
BOARD_4 = [
    (487, 'NBA', 78), (578, 'WNBA', 42), (5270, 'NBA Preseason', 12), (206701, 'Japan - B.League One', 12),
    (208618, 'Lithuania - National Basketball League', 9), (210237, 'Poland - 1. Liga', 8),
    (474, 'Italy - Serie A2', 8), (268, 'ABA - Adriatic League', 8), (238263, 'Spain - Segunda FEB', 8),
    (437, 'Greek Basket League', 7), (527, 'Romania - Division A', 6), (216738, 'Switzerland - NLB', 6),
    (210247, 'Italy - Serie A2 Women', 6), (238265, 'Mexico - Liga ABE', 6), (210094, 'Spain - Liga Women', 6),
    (197721, 'Japan - B.League Premier', 6), (208750, 'Turkey - TBL First League', 6),
    (512, 'Poland - Basket Liga', 5), (210215, 'Czech Republic - ZBL Women', 5),
    (216565, 'Belgium - Top Division 1', 5), (210243, 'Poland - BLK Women', 5),
    (216294, 'Spain - LF Challenge Women', 5), (213142, 'Portugal - Liga Feminina de Basquetebol', 5),
    (313, 'Czech Republic - NBL', 5), (306, 'Croatia - A1 Liga', 5), (303, 'China - CBA', 5),
    (374, 'Europe - VTB United League', 5), (210301, 'Hungary - Nemzeti Bajnoksag I.A Women', 5),
    (216092, 'Kosovo - Superliga', 4), (216542, 'Sweden - Superettan', 4),
    (293, 'Brazil - Novo Basquete Brasil', 4), (485, 'Lithuania - Lietuvos Krepsinio Lyga', 4),
    (222835, 'British - SLB Women', 4), (414, 'France - Championnat Pro A', 4), (321, 'British - SLB', 4),
    (218837, 'Europe - BNXT League', 4), (203909, 'Hungary - Nemzeti Bajnoksag I.A', 4),
    (430, 'Germany - Pro A', 4), (412, 'France - Ligue Féminine de Basketball', 4),
    (210619, 'Finland - Korisliiga Women', 4), (216366, 'Czech Republic - Liga 1 Women', 4),
    (280547, 'British - Championship', 4), (567, 'Switzerland - SB League', 4),
    (213155, 'Austria - Zweite Liga', 4), (5251, 'Uruguay - Liga', 4), (544, 'Slovakia - Extraliga', 4),
    (210004, 'Portugal - Proliga', 4), (210291, 'Turkey - Super League Women', 4),
    (228035, 'Philippines - UAAP', 4), (202722, 'Estonian - Latvian Basketball League', 4),
    (216284, 'Puerto Rico - Superior Nacional Women', 4), (242092, 'Mexico - Liga ABE Women', 4),
    (274, 'Argentina - Liga Nacional', 3), (212148, 'Argentina - La Liga', 3), (482, 'Korean - KBL', 3),
    (537, 'Serbia - Nasa Liga', 3), (216605, 'Paraguay - LNCF Women', 3), (282, 'Australia - WNBL', 3),
    (216740, 'Switzerland - SB League Women', 3), (470, 'Italy - Lega A1 Women', 3),
    (197913, 'Turkey - Super League', 3), (409, 'Finland - Division 1', 3), (210482, 'Serbia - 1 ZLS Women', 3),
    (210233, 'Sweden - Basketligan Women', 3), (210239, 'Czech Republic - 1. Liga', 3),
    (462, 'Israel - Premier League', 3), (210293, 'Belgium - Division 1 Women', 3), (559, 'Spain - ACB', 3),
    (556, 'Spain - Liga FEB', 3), (5608, 'Philippines - PBA Governors Cup', 3),
    (207009, 'El Salvador - Liga Mayor de Baloncesto', 3), (408, 'Finland - Korisliiga', 3),
    (317, 'Denmark - Basketligaen', 3), (283241, 'Philippines - UAAP W', 2), (216824, 'Albania - Superleague', 2),
    (214842, 'New Zealand - NBL Women', 2), (319, 'Denmark - Kvindebasketligaen', 2), (280, 'Australia - NBL', 2),
    (203908, 'Chile - LNB', 2), (285, 'Austria - SuperLiga', 2), (216301, 'Chile - LNB Women', 2),
    (523, 'Romania - Division A Women', 2), (439, 'Greece - Elite League', 2),
    (517, 'Portugal - Liga Portuguesa de Basquetebol', 2), (212151, 'Argentina - LLF Women', 2),
    (280567, 'Algeria - Super Division', 2), (297, 'Bulgaria - NBL', 2), (565, 'Sweden - Basketligan', 1),
    (216339, 'El Salvador - Liga Mayor Women', 1), (216341, 'Turkey - TKBL Women', 1),
    (438, 'Greece - A1 Women', 1), (472, 'Italy - Lega A', 1), (210245, 'Slovakia - Extraliga Women', 1),
    (238434, 'Chinese Taipei - TPBL', 1), (550, 'FIBA - South American Club League', 1),
    (420, 'Germany - BBL Pokal', 1), (208325, 'Georgia - Super League', 1),
    (223634, 'Bosnia and Herzegovina - Prvenstvo BIH', 1), (211487, 'Qatar - Basketball League', 1),
    (493, 'NCAA', 1), (207008, 'Colombia - Baloncesto Profesional Colombiano', 1), (548, 'Slovenia - 1. SKL', 1),
]
BOARD_19 = [
    (1456, 'NHL', 18), (1264, 'USA - American Hockey League', 13), (206704, 'Canada - Western Hockey League', 10),
    (1293, 'Canada - Ontario Hockey League', 9), (1484, 'Russia - Kontinental Hockey League', 9),
    (5154, 'Russia - Vysshaya hokkeinaya Liga', 9), (217161, 'Sweden - HockeyEttan', 9),
    (149037, 'Russia - Junior Hockey League (MHL)', 8), (1282, 'Belarus - Extraleague', 7),
    (1297, 'Czech Republic - Extraliga', 7), (1379, 'Finland - Suomi Sarja U20', 7),
    (1401, 'Finland - SM Liiga', 7), (1419, 'Germany - DEL', 7), (1517, 'Sweden - SHL', 7),
    (1532, 'Switzerland - Nationalliga A', 7), (149036, 'Germany - DEL 2', 7),
    (206705, 'Czech Republic - 1. Liga', 7), (1275, 'Austria - EHL', 6),
    (1295, 'Canada - Quebec Major Junior Hockey League', 6), (1497, 'Slovakia - 1. Liga', 5),
    (206707, 'Switzerland - Swiss League', 5), (216131, 'Europe - Alps Hockey League', 5),
    (1498, 'Slovakia - Extraliga', 4), (222893, 'Norway - Eliteserien', 4), (1316, 'Denmark - Metal Ligaen', 3),
    (1597, 'World - Asian Hockey League', 3), (4529, 'Finland - Mestis', 3),
    (1477, 'Poland - Polska Hokey Liga', 2), (206706, 'British - Elite League', 2),
    (211710, 'Hungary - Erste Liga', 2), (217164, 'Norway - 1st Division', 2),
    (1446, 'Latvia - Latvian Hockey League', 1), (222925, 'Czech Republic - Extraliga U20', 1),
]


def pats_for(series):
    return tuple(dict.fromkeys(p for m in series for p in S.PINNACLE_LEAGUES.get(m, ())))


def kept_ids(board, pats):
    return {i for i, n, _c in board if S.league_wanted(n, pats)}


print("[1] the name rules")
check("substring, case-insensitive", S.league_wanted("Spain - ACB", ("spain - acb",)))
check("'$' anchors the end: the DEL, not DEL 2",
      S.league_wanted("Germany - DEL", ("germany - del$",)) and not S.league_wanted("Germany - DEL 2", ("germany - del$",)))
check("the BBL, not the BBL Pokal (cup)", not S.league_wanted("Germany - BBL Pokal", ("germany - bbl$",)))
check("a Women's league is NOT kept by a men's pattern",
      not S.league_wanted("Turkey - Super League Women", ("turkey - super league",)))
check("...but is by a pattern that says Women", S.league_wanted("NCAA Women", ("ncaa", "ncaa women")))
check("the 2nd tier is not the 1st", not S.league_wanted("Turkey - TBL First League", ("turkey - super league",)))
check("no pattern, no league", not S.league_wanted("NBA", ()))

print("\n[2] the real 2026-10-16 board, every series allowed")
bb = kept_ids(BOARD_4, pats_for(S.CATALOG["basketball"].moneyline))
want_bb = {487, 578, 5270, 206701, 197721, 437, 512, 313, 303, 374, 293, 414, 321, 482, 197913, 462, 559,
           5608, 408, 280, 285, 472, 493, 207008}
check("basketball keeps exactly the 24 leagues Kalshi lists games in", bb == want_bb,
      f"extra {sorted(bb - want_bb)} missing {sorted(want_bb - bb)}")
check("...of 102 (the walk Kalshi cannot use is not made)", len(BOARD_4) == 102 and len(bb) == 24)
ih = kept_ids(BOARD_19, pats_for(S.CATALOG["icehockey"].moneyline))
check("hockey keeps NHL, KHL, Liiga, DEL, SHL - and not DEL 2 / HockeyEttan / AHL",
      ih == {1456, 1484, 1401, 1419, 1517}, sorted(ih))

print("\n[3] who is filtered, from the env")
saved = {k: os.environ.get(k) for k in ("HARDVEN_SPORTS", "HARDVEN_SERIES_ALLOW")}
try:
    os.environ["HARDVEN_SPORTS"] = "tennis,baseball,amfootball"
    os.environ.pop("HARDVEN_SERIES_ALLOW", None)
    check("tennis / baseball / football walk every league, as before (no filter)",
          S.league_filter(33) is None and S.league_filter(3) is None and S.league_filter(15) is None)
    check("a sport that is not enabled is not filtered (it is not walked at all)", S.league_filter(4) is None)
    os.environ["HARDVEN_SPORTS"] = "tennis,basketball,icehockey"
    os.environ["HARDVEN_SERIES_ALLOW"] = "KXATPMATCH,KXACBGAME,KXKHLGAME"
    check("only the ALLOWED series' leagues: basketball", S.league_filter(4) == ("spain - acb",), S.league_filter(4))
    check("only the ALLOWED series' leagues: hockey", S.league_filter(19) == ("kontinental",), S.league_filter(19))
    check("tennis is still unfiltered alongside them", S.league_filter(33) is None)
    os.environ["HARDVEN_SERIES_ALLOW"] = "KXATPMATCH,KXKHLGAME"
    check("enabled but no series allowed = walk none of its leagues", S.league_filter(4) == (), S.league_filter(4))

    # THE WALK ITSELF, on a real adapter with the guest API stubbed: what it would request, and what it logs.
    from pinnacle_adapter import PinnacleAdapter  # noqa: E402
    os.environ["HARDVEN_SERIES_ALLOW"] = "KXATPMATCH,KXACBGAME,KXJBLEAGUEGAME,KXKHLGAME"
    a = PinnacleAdapter()
    a._catalog_leagues, a._catalog_sports = [], ["33", "4", "19"]
    tennis = [{"id": 9001, "name": "ATP Shanghai - R1", "matchupCount": 8},
              {"id": 9002, "name": "ATP Shanghai - Doubles", "matchupCount": 4},
              {"id": 9003, "name": "WTA Wuhan - QF", "matchupCount": 0}]
    boards = {"/sports/33/leagues": tennis,
              "/sports/4/leagues": [{"id": i, "name": n, "matchupCount": c} for i, n, c in BOARD_4],
              "/sports/19/leagues": [{"id": i, "name": n, "matchupCount": c} for i, n, c in BOARD_19]}
    calls = []

    async def fake_guest(path):
        calls.append(path)
        return boards.get(path)

    a._guest_get = fake_guest
    ids = asyncio.run(a._catalog_league_ids())
    check("the walk: every live tennis singles league + ACB + both B.Leagues + KHL, nothing else",
          sorted(ids) == sorted(["9001", "559", "206701", "197721", "1484"]) and len(ids) == len(set(ids)), ids)
    check("one listing request per sport - the 135 team-sport leagues cost 3 requests, not 270",
          calls == ["/sports/33/leagues", "/sports/4/leagues", "/sports/19/leagues"], calls)
    asyncio.run(a._catalog_league_ids())
    check("the skipped leagues are listed once per run, not every walk", a._league_skips_logged == {"4", "19"})
finally:
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v

print("\n[4] every basketball/hockey series is mapped, anchored and 2-way-guarded")
from pair_auto import SERIES_SPORT  # noqa: E402
from pair_pinnacle import _TWO_WAY_ONLY  # noqa: E402
team = [m for k in ("basketball", "icehockey") for m in S.CATALOG[k].moneyline]
check("each has a Pinnacle league map", all(m in S.PINNACLE_LEAGUES for m in team),
      [m for m in team if m not in S.PINNACLE_LEAGUES])
check("each is anchored to its sport (a 'Liverpool' is not the football club)",
      all(SERIES_SPORT.get(m) for m in team), [m for m in team if not SERIES_SPORT.get(m)])
check("basketball anchors to BASKETBALL, hockey to HOCKEY",
      all("BASKETBALL" in SERIES_SPORT[m] for m in S.CATALOG["basketball"].moneyline)
      and all("HOCKEY" in SERIES_SPORT[m] for m in S.CATALOG["icehockey"].moneyline))
check("each refuses a 3-way line", set(team) <= _TWO_WAY_ONLY, sorted(set(team) - _TWO_WAY_ONLY))
check("eBasketball (tie rule) is not in the catalog", "KXEBASKETBALLGAME" not in S.CATALOG["basketball"].moneyline)
check("NCAA hockey (50/50 ties) and the AHL are not in it either",
      not {"KXNCAAHGAME", "KXAHLGAME"} & set(S.CATALOG["icehockey"].moneyline))
check("both ship disabled - nothing changes until HARDVEN_SPORTS names them",
      not S.CATALOG["basketball"].enabled and not S.CATALOG["icehockey"].enabled)
check("both Pinnacle ids are the confirmed ones", S.CATALOG["basketball"].pinnacle_id == 4
      and S.CATALOG["icehockey"].pinnacle_id == 19)

print(f"\n{'=' * 58}\n  {PASS} passed, {FAIL} failed\n{'=' * 58}")
sys.exit(1 if FAIL else 0)
