"""Esports pairing (2026-11-19). Kalshi writes "Team Spirit" / "Xtreme Gaming" / "NIP" where Pinnacle writes "Spirit" /
"Xtreme" / "Ninjas In Pyjamas", and an org's academy is its name plus a word ("Spirit Academy" contains "spirit"), so
esports pairs EXACT name cores, never containment. Also: a best-of-two line that prices a draw is refused, the
orientation audit keeps "OG" / "G2"-style names (no 3-letter token), and the catalog leaves out the "(Kills)" twin.

Runs the REAL pair_pinnacle.main() on a temp pair file with /catalog stubbed, and the REAL PinnacleAdapter.catalog()
with the guest API stubbed (no sidecar, no network). Names and ids are the real 11-19 guest-board ones
(Dota 2 - BLAST Slam 238902, CS2 - STK Ranked 285202, Dota 2 - European Pro League 220253).

    python test_esports_pairing.py          # exit 0 = all pass
"""
from __future__ import annotations
import asyncio, contextlib, copy, datetime as dt, io, json, os, sys, tempfile, time, types
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.argv = sys.argv[:1]
sys.modules["slate_log"] = types.SimpleNamespace(record=lambda obs: {"ok": False}, LOG=Path("unused"))
import pair_pinnacle as P  # noqa: E402
import pair_auto as A  # noqa: E402
import sports  # noqa: E402

P._sidecar_evidence = lambda sidecar, sid, probe, timeout=3.0: {"stub": True, "redirect": sid}
PASS = FAIL = 0


def check(name, ok, got=""):
    global PASS, FAIL
    PASS, FAIL = PASS + bool(ok), FAIL + (not ok)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + ("" if ok or got == "" else f"  (got {got})"))


NOW = dt.datetime.now(dt.timezone.utc).replace(second=0, microsecond=0)
T1 = (NOW + dt.timedelta(hours=20)).replace(minute=0)      # BLAST Slam, first slot
T2 = T1 + dt.timedelta(hours=3)                             # second slot
T3 = (NOW + dt.timedelta(hours=5)).replace(minute=0)       # Stake Ranked
T4 = (NOW + dt.timedelta(hours=8)).replace(minute=0)       # an academy match
T5 = (NOW + dt.timedelta(hours=9)).replace(minute=0)       # a best-of-two
T6 = (NOW + dt.timedelta(hours=10)).replace(minute=0)      # a soccer namesake


def code(t):
    """Kalshi's ticker date+time (New York): 26NOV201100."""
    return t.astimezone(ZoneInfo("America/New_York")).strftime("%y%b%d%H%M").upper()


def iso(t):
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def game(lid, mid, home, away, start, league, sport="E Sports", draw=False):
    rows = [{"selection_id": f"{lid}:{mid}:{side}", "selection_name": name, "start_time": iso(start), "league": league,
             "sport": sport, "three_way": draw} for side, name in (("home", home), ("away", away))]
    if draw:
        rows.append({"selection_id": f"{lid}:{mid}:draw", "selection_name": "Draw", "start_time": iso(start),
                     "league": league, "sport": sport, "three_way": True})
    return rows


SLAM, STK, EPL = "Dota 2 - BLAST Slam", "CS2 - STK Ranked", "Dota 2 - European Pro League"
CATALOG = (game("238902", "1640567054", "Spirit", "Nemesis", T1, SLAM)
           + game("238902", "1640599999", "Spirit Academy", "Nemesis", T1, SLAM)       # decoy: the academy, same slot
           + game("238902", "1640570224", "Natus Vincere", "OG", T2, SLAM)
           + game("238902", "1640570253", "GamerLegion", "Xtreme", T2, SLAM)
           + game("285202", "1640693387", "BIG", "Ninjas In Pyjamas", T3, STK)
           + game("285202", "1640693388", "Sangal", "Luminosity", T3 + dt.timedelta(minutes=105), STK)  # starts late
           + game("220253", "1640620974", "Spirit Academy", "angry birds", T4, EPL)
           + game("285202", "1640700000", "Falcons", "MOUZ", T5, STK, draw=True)        # a best-of-two
           + game("300001", "1640800000", "Falcons", "Vitality", T6, "Saudi Arabia - Pro League", sport="Soccer"))


def krow(series, teams, side, yes_name, t, title):
    ev = f"{series}-{code(t)}{teams}"
    return {"kalshi_ticker": f"{ev}-{side}", "label": f"{yes_name} wins", "event_id": ev, "event_title": title,
            "kalshi_outcome": yes_name, "settlement_date": t.strftime("%Y-%m-%d"), "kalshi_yes_price": 0.5,
            "is_neg_risk": False, "hardven_min_size": 1.0, "hardven_yes_token": "", "hardven_no_token": ""}


ROWS = [
    krow("KXDOTA2GAME", "NEMTS", "TS", "Team Spirit", T1, "Team Nemesis vs. Team Spirit"),            # 0
    krow("KXDOTA2GAME", "OGNAVI", "OG", "OG", T2, "OG vs. Natus Vincere"),                             # 1
    krow("KXDOTA2GAME", "XTREMEGL", "XTREME", "Xtreme Gaming", T2, "Xtreme Gaming vs. GamerLegion"),   # 2
    krow("KXCS2GAME", "BIGNIP", "NIP", "NIP", T3, "BIG vs. NIP"),                                      # 3
    krow("KXCS2GAME", "LGSNG", "LG", "Luminosity", T3, "Luminosity vs. Sangal"),                       # 4
    krow("KXDOTA2GAME", "TSANG", "TS", "Team Spirit", T4, "Team Spirit vs. angry birds"),             # 5 academy only
    krow("KXCS2GAME", "FALMOUZ", "FAL", "Team Falcons", T5, "Team Falcons vs. MOUZ"),                  # 6 best-of-two
    krow("KXCS2GAME", "FALVIT", "VIT", "Team Vitality", T6, "Team Falcons vs. Team Vitality"),         # 7 soccer only
]


def run(rows, catalog):
    path = os.path.join(tempfile.mkdtemp(prefix="estest_"), "cross_pairs.json")
    Path(path).write_text(json.dumps(rows), encoding="utf-8")
    P.fetch_catalog = lambda sidecar, timeout=0: copy.deepcopy(catalog)
    sys.argv = ["pair_pinnacle.py", "--write", "--pairs", path, "--sidecar", "http://127.0.0.1:9", "--no-price-gate"]
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        P.main()
    return json.loads(Path(path).read_text(encoding="utf-8")), buf.getvalue()


print("[1] unit: name cores, the side test, the series lists, the league walk")
core = P._esports_core
check("'team spirit' -> 'spirit'", core("team spirit") == "spirit")
check("'xtreme gaming' -> 'xtreme'", core("xtreme gaming") == "xtreme")
check("'hermine esports club' -> 'hermine'", core("hermine esports club") == "hermine")
check("'drama e sports' (eSports spelled apart) -> 'drama'", core("drama e sports") == "drama")
check("'kuusamo gg' -> 'kuusamo', 'the chosen few' -> 'chosen few'",
      (core("kuusamo gg"), core("the chosen few")) == ("kuusamo", "chosen few"))
check("an academy keeps its word: 'spirit academy' stays", core("spirit academy") == "spirit academy")
check("never stripped to nothing: 'team' stays 'team', 'a team' -> 'a'", (core("team"), core("a team")) == ("team", "a"))
check("alias: 'nip' -> 'ninjas in pyjamas'", core("nip") == "ninjas in pyjamas")
check("_canon_team cuts esports names only", (P._canon_team("team spirit", "KXDOTA2GAME-X-TS"),
                                              P._canon_team("team spirit", "KXNBAGAME-X-TS")) == ("spirit", "team spirit"))
agree = P._names_agree
check("side test: 'OG' = 'OG' (no 3-letter token)", agree("KXDOTA2GAME-X-OG", "OG", "OG"))
check("side test: 'NIP' = 'Ninjas In Pyjamas'", agree("KXCS2GAME-X-NIP", "NIP", "Ninjas In Pyjamas"))
check("side test: 'Team Spirit' != 'Spirit Academy' (a shared token is not enough here)",
      not agree("KXDOTA2GAME-X-TS", "Team Spirit", "Spirit Academy"))
check("side test: 'G2' != 'G2 Ares'", not agree("KXCS2GAME-X-G2", "G2", "G2 Ares"))
check("side test for tennis unchanged (shared surname token)", agree("KXITFMATCH-X-ORT", "Jazmin Ortenzi", "Jasmin Ortenzi"))
es = sports.CATALOG["esports"]
check("sports.py: esports = Pinnacle sport 12", es.pinnacle_id == 12)
check("every catalog esports series is anchored to E SPORTS",
      all(A.SERIES_SPORT.get(s) == {"E SPORTS", "ESPORTS"} for s in es.moneyline),
      [s for s in es.moneyline if A.SERIES_SPORT.get(s) != {"E SPORTS", "ESPORTS"}])
check("esports is 2-way only (a line with a draw is refused)", "KXCS2GAME" in P._TWO_WAY_ONLY)
check("no simulated e-sports (ties / 3-way) in the moneyline",
      not ({"KXEBASKETBALLGAME", "KXESOCCERGAME", "KXEFOOTBALLGAME"} & set(es.moneyline)))
pl = sports.PINNACLE_LEAGUES
check("league walk: CS2 leagues for KXCS2GAME, Dota 2 leagues for KXDOTA2GAME, not each other's",
      (sports.league_wanted(STK, pl["KXCS2GAME"]), sports.league_wanted(SLAM, pl["KXDOTA2GAME"]),
       sports.league_wanted(STK, pl["KXDOTA2GAME"]), sports.league_wanted(SLAM, pl["KXCS2GAME"]))
      == (True, True, False, False))

print("\n[2] the real main(): the 11-19 board, names Kalshi writes differently, an academy, a best-of-two")
out, log = run(copy.deepcopy(ROWS), CATALOG)
by = {r["kalshi_ticker"]: r for r in out}
R = [by[r["kalshi_ticker"]] for r in ROWS]
check("'Team Spirit vs. Team Nemesis' -> Spirit v Nemesis (1640567054), NOT the academy's game in the same slot",
      (R[0].get("hardven_yes_token"), R[0].get("hardven_no_token")) == ("238902:1640567054:home",
                                                                      "238902:1640567054:away"),
      (R[0].get("hardven_yes_token"), R[0].get("hardven_no_token")))
check("YES 'OG' -> OG (away), and the orientation audit keeps it",
      R[1].get("hardven_yes_token") == "238902:1640570224:away" and "MIS-ORIENTED" not in log,
      (R[1].get("hardven_yes_token"), [l for l in log.splitlines() if "MIS-ORIENTED" in l][:2]))
check("YES 'Xtreme Gaming' -> Xtreme (away)", R[2].get("hardven_yes_token") == "238902:1640570253:away",
      R[2].get("hardven_yes_token"))
check("YES 'NIP' -> Ninjas In Pyjamas (away), named as Pinnacle names it",
      (R[3].get("hardven_yes_token"), R[3].get("hardven_yes_name"))
      == ("285202:1640693387:away", "ninjas in pyjamas"), (R[3].get("hardven_yes_token"), R[3].get("hardven_yes_name")))
check("a match listed 1h45 later on Pinnacle still pairs (within the 3h time rule)",
      R[4].get("hardven_yes_token") == "285202:1640693388:away", R[4].get("hardven_yes_token"))
check("Kalshi 'Team Spirit' vs a board with only 'Spirit Academy' stays EMPTY",
      not R[5].get("hardven_yes_token"), R[5].get("hardven_yes_token"))
check("...and the run says why", "esports pairs exact names only" in log,
      [l for l in log.splitlines() if "TSANG" in l][:2])
check("a best-of-two whose line prices a draw stays EMPTY",
      not R[6].get("hardven_yes_token"), R[6].get("hardven_yes_token"))
check("...refused as 3-way", any("FALMOUZ" in l and "3-way" in l for l in log.splitlines()),
      [l for l in log.splitlines() if "FALMOUZ" in l][:2])
check("a SOCCER game with the same two names is never taken", not R[7].get("hardven_yes_token"),
      R[7].get("hardven_yes_token"))

print("\n[3] with containment instead (the rule off), 'Team Spirit' WOULD take the academy's game")
real = P._is_esports
P._is_esports = lambda ticker: False
try:
    out2, _ = run(copy.deepcopy(ROWS), CATALOG)
finally:
    P._is_esports = real
r5 = next(r for r in out2 if r["kalshi_ticker"] == ROWS[5]["kalshi_ticker"])
check("rule off: 'Team Spirit' pairs 'Spirit Academy' v angry birds (the bug this prevents)",
      r5.get("hardven_yes_token") == "220253:1640620974:home", r5.get("hardven_yes_token"))

print("\n[4] the real PinnacleAdapter.catalog(): the league walk, the Kills twin, draws (guest API stubbed)")
from pinnacle_adapter import PinnacleAdapter  # noqa: E402

LEAGUES = [{"id": 220253, "name": EPL, "matchupCount": 2}, {"id": 285202, "name": STK, "matchupCount": 2},
           {"id": 990001, "name": "StarCraft 2 - Some Cup", "matchupCount": 4}]


def mu(mid, home, away, start, units="Regular", parent=None):
    m = {"id": mid, "type": "matchup", "startTime": iso(start), "units": units,
         "league": {"id": 0, "name": "", "sport": {"name": "E Sports"}},
         "participants": [{"alignment": "home", "name": home}, {"alignment": "away", "name": away}]}
    if parent:
        m["parentId"] = parent
    return m


def ml(mid, period, desigs):
    return {"type": "moneyline", "period": period, "matchupId": mid,
            "prices": [{"designation": d, "price": 100} for d in desigs]}


GUEST = {
    "/sports/12/leagues": LEAGUES,
    "/leagues/220253/matchups": [mu(1640620974, "4ikibamboni", "Zero Tenacity", T3),
                                 mu(1640706975, "4ikibamboni (Kills)", "Zero Tenacity (Kills)", T3, units="Kills",
                                    parent=1640620974)],
    "/leagues/220253/markets/straight": [ml(1640620974, 0, ("home", "away")), ml(1640706975, 3, ("home", "away")),
                                         ml(1640706975, 2, ("home", "away"))],
    "/leagues/285202/matchups": [mu(1640693388, "Sangal", "Luminosity", T3), mu(1640700000, "Falcons", "MOUZ", T5)],
    "/leagues/285202/markets/straight": [ml(1640693388, 0, ("home", "away")),
                                         ml(1640700000, 0, ("home", "draw", "away"))],
}
for lg in LEAGUES:
    for m in GUEST.get(f"/leagues/{lg['id']}/matchups", []):
        m["league"].update({"id": lg["id"], "name": lg["name"]})
asked: list[str] = []


async def fake_get(path):
    asked.append(path)
    return copy.deepcopy(GUEST.get(path.split("?")[0], []))

env_before = {k: os.environ.get(k) for k in ("HARDVEN_SPORTS", "HARDVEN_SERIES_ALLOW")}
os.environ["HARDVEN_SPORTS"], os.environ["HARDVEN_SERIES_ALLOW"] = "tennis,esports", "KXATPMATCH,KXCS2GAME,KXDOTA2GAME"
try:
    a = PinnacleAdapter()
    a._guest_get, a._catalog_sports, a._catalog_leagues, a._jitter_ms = fake_get, ["12"], [], 0
    now = time.time()
    a._live_meta = {
        "220253:1640706983": {"names": {"home": "4ikibamboni", "away": "Zero Tenacity"}, "league": EPL,
                              "sport": "E Sports", "start": iso(T3), "units": "Regular", "ts": now},
        "220253:1640706999": {"names": {"home": "4ikibamboni", "away": "Zero Tenacity"}, "league": EPL,
                              "sport": "E Sports", "start": iso(T3), "units": "Kills", "ts": now},
        "285202:1640799999": {"names": {"home": "Falcons", "away": "MOUZ"}, "league": STK, "sport": "E Sports",
                              "start": iso(T5), "units": "Regular", "ts": now}}
    a._cache["285202:1640799999:draw"] = object()          # the live best-of-two prices a draw
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        cat = asyncio.run(a.catalog())
finally:
    for k, v in env_before.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
sel = {c.selection_id: c for c in cat}
check("the walk skips a league no allowed series maps to (StarCraft 2 never fetched)",
      not any("990001" in p for p in asked), [p for p in asked if "990001" in p])
check("a pre-match best-of-three: 2-way", sel.get("285202:1640693388:home") is not None
      and not sel["285202:1640693388:home"].three_way)
check("a pre-match best-of-two whose moneyline prices a draw: 3-way, with its draw leg",
      sel.get("285202:1640700000:home") is not None and sel["285202:1640700000:home"].three_way
      and "285202:1640700000:draw" in sel)
check("the '(Kills)' twin (no full-match moneyline) is not cataloged",
      not any(k.startswith("220253:1640706975") for k in sel))
check("a LIVE child from the reader is cataloged, 2-way", sel.get("220253:1640706983:home") is not None
      and not sel["220253:1640706983:home"].three_way)
check("a LIVE Kills twin from the reader is not", not any(k.startswith("220253:1640706999") for k in sel))
check("a LIVE best-of-two whose draw is priced: 3-way", sel.get("285202:1640799999:home") is not None
      and sel["285202:1640799999:home"].three_way)
check("the walk asked for sport 12's league list and the two kept leagues, nothing else",
      all(p.startswith(("/sports/12", "/leagues/220253", "/leagues/285202")) for p in asked), asked)

print(f"\n{PASS}/{PASS + FAIL} passed")
sys.exit(0 if FAIL == 0 else 1)
