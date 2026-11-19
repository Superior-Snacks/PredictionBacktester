"""Cricket pairing (2026-11-19): Pinnacle names a women's side exactly like the men's and says "Women" only in the
LEAGUE, while Kalshi writes "Adelaide Strikers Women" and files women's matches under their own series. Runs the
REAL pair_pinnacle.main() on a temp pair file with /catalog stubbed (no sidecar, no network), like
test_pair_retire.py. Names, leagues and ids are the real 11-19 guest-board ones (WBBL 216411, women's T20I 212283).

    python test_cricket_pairing.py          # exit 0 = all pass
"""
from __future__ import annotations
import contextlib, copy, datetime as dt, io, json, os, sys, tempfile, types
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.argv = sys.argv[:1]
sys.modules["slate_log"] = types.SimpleNamespace(record=lambda obs: {"ok": False}, LOG=Path("unused"))
import pair_pinnacle as P  # noqa: E402
import sports  # noqa: E402

P._sidecar_evidence = lambda sidecar, sid, probe, timeout=3.0: {"stub": True, "redirect": sid}
PASS = FAIL = 0


def check(name, ok, got=""):
    global PASS, FAIL
    PASS, FAIL = PASS + bool(ok), FAIL + (not ok)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + ("" if ok or got == "" else f"  (got {got})"))


NOW = dt.datetime.now(dt.timezone.utc).replace(second=0, microsecond=0)
T_W = (NOW + dt.timedelta(hours=20)).replace(minute=10)            # the women's game of a double-header
T_M = T_W + dt.timedelta(hours=4)                                   # the men's game, same clubs, same day
T_I = (NOW + dt.timedelta(hours=26)).replace(minute=15)             # a women's T20I


def code(t):
    """Kalshi's ticker date+time (New York): 26NOV200310."""
    return t.astimezone(ZoneInfo("America/New_York")).strftime("%y%b%d%H%M").upper()


def game(lid, mid, home, away, start, league):
    s = start.strftime("%Y-%m-%dT%H:%M:%SZ")
    return [{"selection_id": f"{lid}:{mid}:{side}", "selection_name": name, "start_time": s, "league": league,
             "sport": "Cricket"} for side, name in (("home", home), ("away", away))]


WBBL = game("216411", "1640689108", "Adelaide Strikers", "Melbourne Renegades", T_W,
            "Australia - Twenty20 Big Bash Women")
BBL = game("216400", "1640700001", "Adelaide Strikers", "Melbourne Renegades", T_M, "Australia - Big Bash League")
T20IW = game("212283", "1640622349", "Pakistan", "Ireland", T_I, "Twenty20 International - Women")
CATALOG = WBBL + BBL + T20IW


def krow(series, teams, side, yes_name, t, title=None):
    ev = f"{series}-{code(t)}{teams}"
    return {"kalshi_ticker": f"{ev}-{side}", "label": f"{yes_name} wins", "event_id": ev,
            "event_title": title, "kalshi_outcome": yes_name, "settlement_date": t.strftime("%Y-%m-%d"),
            "kalshi_yes_price": 0.5, "is_neg_risk": False, "hardven_min_size": 1.0,
            "hardven_yes_token": "", "hardven_no_token": ""}


ROWS = [
    # women's WBBL, Kalshi names carry "Women"
    krow("KXWT20MATCH", "ADSWMRW", "ADSW", "Adelaide Strikers Women", T_W,
         "Adelaide Strikers Women vs Melbourne Renegades Women"),
    # the men's game of the double-header (a men's series, plain names)
    krow("KXT20MATCH", "ADSMR", "ADS", "Adelaide Strikers", T_M, "Adelaide Strikers vs Melbourne Renegades"),
    # a women's T20I where Kalshi's names carry NO "Women" (seen 11-15: KXWT20MATCH-26NOV150000NEDPAK)
    krow("KXWT20MATCH", "PAKIRE", "PAK", "Pakistan", T_I, "Pakistan vs Ireland"),
    # a MEN's market for the same two nations at the same time: Pinnacle lists only the women's game
    krow("KXT20MATCH", "PAKIRE", "PAK", "Pakistan", T_I, "Pakistan vs Ireland"),
]


def run(rows, catalog):
    path = os.path.join(tempfile.mkdtemp(prefix="crktest_"), "cross_pairs.json")
    Path(path).write_text(json.dumps(rows), encoding="utf-8")
    P.fetch_catalog = lambda sidecar, timeout=0: copy.deepcopy(catalog)
    sys.argv = ["pair_pinnacle.py", "--write", "--pairs", path, "--sidecar", "http://127.0.0.1:9", "--no-price-gate"]
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        P.main()
    return json.loads(Path(path).read_text(encoding="utf-8")), buf.getvalue()


print("[1] unit: names, gender, the series lists")
check("'adelaide strikers women' -> 'adelaide strikers'",
      P._canon_team("adelaide strikers women", "KXWT20MATCH-X-ADSW") == "adelaide strikers")
check("'northern districts men' -> 'northern districts'",
      P._canon_team("northern districts men", "KXTESTMATCH-X-ND") == "northern districts")
check("a name that IS a gender word is never emptied", P._cricket_core("w") == "w")
check("tennis names are untouched (a player called '... Men' would be)",
      P._canon_team("abibsi", "KXITFMATCH-X-ABI") == P._canon("abibsi"))
check("women's series = women", P._cricket_women("KXWT20MATCH-X-PAK", "Pakistan vs Ireland"))
check("men's series + 'Women' in the title = women", P._cricket_women("KXT20MATCH-X-A", "A Women vs B Women"))
check("men's series, plain title = men", not P._cricket_women("KXT20MATCH-X-PAK", "Pakistan vs Ireland"))
g_w, g_m = {"league_name": "Australia - Twenty20 Big Bash Women"}, {"league_name": "Australia - Big Bash League"}
check("gender filter keeps the women's league for a women's market",
      P._cricket_gender_ok([g_w, g_m], "KXWT20MATCH-X-A", "") == [g_w])
check("...and the men's league for a men's market", P._cricket_gender_ok([g_w, g_m], "KXT20MATCH-X-A", "") == [g_m])
check("non-cricket games pass untouched", P._cricket_gender_ok([g_w, g_m], "KXNBAGAME-X-A", "") == [g_w, g_m])
check("cricket is a 2-way-only sport (a 3-way line is refused)", "KXT20MATCH" in P._TWO_WAY_ONLY)
cr = sports.CATALOG["cricket"]
check("sports.py: cricket = Pinnacle sport 8", cr.pinnacle_id == 8)
check("sports.py: no Test / first-class / thin series in the moneyline",
      not ({"KXTESTMATCH", "KXWTESTMATCH", "KXCOUNTYCHAMPMATCH", "KXSSHIELDMATCH", "KXT20CANADAMATCH"}
           & set(cr.moneyline)))
import pair_auto as A  # noqa: E402
check("every catalog cricket series is anchored to CRICKET",
      all(A.SERIES_SPORT.get(s) == {"CRICKET"} for s in cr.moneyline), [s for s in cr.moneyline
                                                                        if A.SERIES_SPORT.get(s) != {"CRICKET"}])

print("\n[2] the real main(): a WBBL/BBL double-header and a women's T20I")
out, log = run(copy.deepcopy(ROWS), CATALOG)
by = {r["kalshi_ticker"]: r for r in out}
w, m, iw, im = (by[r["kalshi_ticker"]] for r in ROWS)
check("women's WBBL market -> the WOMEN's game (216411), YES = Adelaide = home",
      w.get("hardven_yes_token") == "216411:1640689108:home" and w.get("hardven_no_token") == "216411:1640689108:away",
      (w.get("hardven_yes_token"), w.get("hardven_no_token")))
check("men's market of the double-header -> the MEN's game (216400)",
      m.get("hardven_yes_token") == "216400:1640700001:home", m.get("hardven_yes_token"))
check("women's T20I with plain names -> the women's game (212283)",
      iw.get("hardven_yes_token") == "212283:1640622349:home", iw.get("hardven_yes_token"))
check("a MEN's market whose teams Pinnacle lists only in a women's league stays EMPTY",
      not im.get("hardven_yes_token") and not im.get("hardven_no_token"), im.get("hardven_yes_token"))
check("...and the run says why", "only in a women's league - refused" in log,
      [l for l in log.splitlines() if "PAKIRE" in l][:2])

print("\n[3] without the gender rule the men's market WOULD take the women's game (the bug this prevents)")
real = P._cricket_gender_ok
P._cricket_gender_ok = lambda games, ticker, title: games
try:
    out2, _ = run(copy.deepcopy(ROWS), CATALOG)
finally:
    P._cricket_gender_ok = real
im2 = next(r for r in out2 if r["kalshi_ticker"] == ROWS[3]["kalshi_ticker"])
check("rule off: the men's Pakistan market pairs the women's game", im2.get("hardven_yes_token") == "212283:1640622349:home",
      im2.get("hardven_yes_token"))

print(f"\n{PASS}/{PASS + FAIL} passed")
sys.exit(0 if FAIL == 0 else 1)
