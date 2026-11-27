"""The tennis player checks (pair_pinnacle TENNIS, 2026-11-27): a tennis market sees only the games its players can be
(gender, Kalshi's surnames as WHOLE words, Kalshi's full names), and a row whose player shares only a surname with
Kalshi's is judged by the price gate on tight terms (pair_auto.price_validate `tight`) and cleared when it cannot be.
Built on the 7 wrong pairs the 11-27 history audit found (LIULIX, SHIHOX, WANSUN, HANCHE, SATCHO, CHEHAN, MARMAX) and
the same-player renderings beside them (Yara / Yaroslava Bartashevich, ...). Runs the REAL main() end to end on a temp
pair file, /catalog and the sidecar's /odds stubbed (no network, nothing written outside the temp dir).

    python test_pair_tennis_names.py          # exit 0 = all pass
"""
from __future__ import annotations
import contextlib, copy, datetime as dt, io, json, os, sys, tempfile, types
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.argv = sys.argv[:1]
sys.modules["slate_log"] = types.SimpleNamespace(record=lambda obs: {"ok": False}, LOG=Path("unused"))
import pair_pinnacle as P  # noqa: E402
import pair_auto as A  # noqa: E402

P._sidecar_evidence = lambda sidecar, sid, probe, timeout=3.0: {"stub": True}
os.environ["HARDVEN_PRICE_GATE_RETRIES"] = "0"           # no feed-waking waits inside a test
os.environ.pop("HARDVEN_PAIR_CLASH_TOL", None)
REAL_GATE, REAL_FETCH = A.price_validate, A._fetch_implied
PASS = FAIL = 0


def check(name, ok, got=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}" + (f"  (got {got})" if got != "" else ""))


NOW = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
TODAY = NOW.strftime("%Y-%m-%d")
TDAY = NOW.strftime("%y") + NOW.strftime("%b").upper() + NOW.strftime("%d")    # the ticker's date, e.g. 26NOV27
START = (NOW + dt.timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ")


def game(lid, mid, home, away, league, start=START):
    return [{"selection_id": f"{lid}:{mid}:home", "selection_name": home, "start_time": start, "league": league,
             "sport": "Tennis"},
            {"selection_id": f"{lid}:{mid}:away", "selection_name": away, "start_time": start, "league": league,
             "sport": "Tennis"}]


def event(series, code, title, a_code, a, b_code, b, a_price=0.60):
    """The two Kalshi markets of a match, unpaired (as the scaffold writes them)."""
    ev = f"{series}-{TDAY}{code}"
    mk = lambda c, o, p: {"kalshi_ticker": f"{ev}-{c}", "event_id": ev, "event_title": title, "kalshi_outcome": o,
                          "label": f"{o} wins", "settlement_date": TODAY, "kalshi_yes_price": p, "is_neg_risk": False,
                          "hardven_min_size": 1.0, "hardven_yes_token": "", "hardven_no_token": ""}
    return [mk(a_code, a, a_price), mk(b_code, b, round(1 - a_price, 2))]


FILLER = game("1", "9000001", "Jan Novak", "Petr Svoboda", "ITF Men Monastir - R16")   # keeps the catalog non-empty


def run(rows, catalog, gate=False, prices=None):
    """main() on a temp pair file -> (rows after, output, the temp dir). `prices` = {token: implied} for the REAL gate."""
    d = tempfile.mkdtemp(prefix="tennisnames_")
    path = os.path.join(d, "cross_pairs.json")
    Path(path).write_text(json.dumps(rows), encoding="utf-8")
    P.fetch_catalog = lambda sidecar, timeout=0: copy.deepcopy(catalog + FILLER)
    argv = ["pair_pinnacle.py", "--write", "--pairs", path, "--sidecar", "http://127.0.0.1:9", "--price-tol", "0.12"]
    if gate:
        A._fetch_implied = lambda sidecar, tokens, **kw: {t: prices[t] for t in tokens if t in (prices or {})}
        P.price_validate = REAL_GATE
    else:
        argv.append("--no-price-gate")
    sys.argv = argv
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            P.main()
    finally:
        A._fetch_implied = REAL_FETCH
    return json.loads(Path(path).read_text(encoding="utf-8")), buf.getvalue(), d


def paired(rows):
    """{ticker: (yes token, yes name)} of the rows that carry a pairing."""
    return {r["kalshi_ticker"]: (r.get("hardven_yes_token"), r.get("hardven_yes_name")) for r in rows
            if r.get("hardven_yes_token")}


def name_log(d):
    p = Path(d, "pair_name_checks.jsonl")
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines()] if p.exists() else []


def misses(d):
    p = Path(d, "pair_misses.jsonl")
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if "run" not in json.loads(l)] \
        if p.exists() else []


print("[1] the player verdict, on the history's real names")
for k, p, want in (("Jiumeng Liu", "claire liu", "clash"), ("Meng Yi Chen", "xing dao chen", "clash"),
                   ("Sohee Choi", "onyu choi", "clash"), ("Xing Dao Chen", "yan cheng chen", "clash"),
                   ("Tatjana Maria", "isabella maria serban", "clash"), ("Jiho Shin", "woobin shin", "clash"),
                   ("Junjie Sun", "xinran sun", "clash"), ("Janie Ho", "thomas fancutt", "none"),
                   ("Yara Bartashevich", "yaroslava bartashevich", "same"), ("Alexander Rozin", "sasha rozin", "same"),
                   ("Mariia Lazarenko", "masha lazarenko", "same"), ("Pyotr Nesterov", "petr nesterov", "same"),
                   ("Aleksandr Shevchenko", "alexander shevchenko", "same"),
                   ("Menelaos Efstathiou", "melios efstathiou", "same"), ("Qian Sun", "yingqun sun", "same"),
                   ("Jazmin Ortenzi", "jasmin ortenzi", "same"), ("Yi Li", "yi li", "same"),
                   ("Meng Yi Chen", "mengyi chen", "same"), ("Yan Cheng CHEN", "yan cheng chen", "same"),
                   ("Felipe Meligeni Alves", "felipe meligeni rodrigues alves", "same"),
                   ("Maria Camila Osorio", "maria osorio serrano", "same"), ("A. Zverev", "alexander zverev", "same"),
                   ("Lucas Andrade Da Silva", "joao da silva", "clash"), ("Yi Li", "", "")):
    check(f"{k!r} / {p!r} -> {want or 'unknown'}", P._player_verdict(k, p) == want, P._player_verdict(k, p))

print("\n[2] league gender")
for lg, want in (("ITF Men Maanshan - R1", "M"), ("ITF Women Tianjin - R1", "W"), ("ATP Challenger Seoul - Qualifiers", "M"),
                 ("WTA Osaka - Qualifiers", "W"), ("WTA 125K Angers", "W"), ("Billie Jean King Cup", "W"),
                 ("Davis Cup", "M"), ("United Cup", ""), ("Exhibition", "")):
    check(f"{lg!r} -> {want or 'neither'}", P._tennis_gender(lg) == want, P._tennis_gender(lg))

print("\n[3] the side checks (orientation audit / names-settle) read tennis through the same verdict")
check("a surname-only match names the same SIDE", P._names_agree("KXITFWMATCH-X-LIU", "Jiumeng Liu", "Claire Liu"))
check("the opponent does not", not P._names_agree("KXITFWMATCH-X-LIU", "Jiumeng Liu", "Maddison Inglis"))
check("a two-letter name agrees with itself (the token test read 'Yi Li' as nobody)",
      P._names_agree("KXITFWMATCH-X-LI", "Yi Li", "Yi Li"))
check("team sports unchanged: Ole Miss / Mississippi agree through the alias",
      P._names_agree("KXNCAAMBGAME-X-MISS", "Ole Miss", "Mississippi"))

print("\n[4] LIULIX: 'li' was inside 'Maddison Inglis' - the only 'Liu v Li' on the board was another match")
LIU = event("KXITFWMATCH", "LIULIX", "Liu vs Li", "LIU", "Jiumeng Liu", "LI", "Yi Li")
WRONG = game("227589", "1638621924", "Claire Liu", "Maddison Inglis", "WTA Osaka - Qualifiers")
out, log, d = run(copy.deepcopy(LIU), WRONG)
check("not paired", not paired(out), paired(out))
check("the UNMATCHED line says what was refused and why",
      "refused Pinnacle's 'claire liu v maddison inglis'" in log and "'li' is only inside 'maddison inglis'" in log,
      [l for l in log.splitlines() if "LIULIX" in l][:3])
NL = name_log(d)
check("pair_name_checks.jsonl: a run record, then the refusal",
      NL and NL[0].get("run") == {"refused": 1, "clash_ok": 0, "clash_cleared": 0}
      and any(r.get("kind") == "refused" and r["board"]["names"] == ["claire liu", "maddison inglis"] for r in NL), NL)
check("the run's last line counts it", "tennis names: 1 game(s) refused" in log.strip().splitlines()[-1],
      log.strip().splitlines()[-1])
RIGHT = game("230001", "1638700001", "Jiumeng Liu", "Yi Li", "ITF Women Qian Daohu - R1")
out, log, d = run(copy.deepcopy(LIU), WRONG + RIGHT)
check("with the right match listed too: paired to it", paired(out).get(LIU[0]["kalshi_ticker"]) ==
      ("230001:1638700001:home", "jiumeng liu"), paired(out))
check("...and not flagged a name clash (Yi Li / Yi Li, two-letter names)", "NAME-CLASH" not in log, log[-800:])

print("\n[5] SHIHOX: a women's market on a MEN's game ('ho' inside 'Thomas Fancutt'); WANSUN: 'wang' in 'Sawangkaew'")
SHI = event("KXITFWMATCH", "SHIHOX", "Shin vs Ho", "SHI", "Jiho Shin", "HOX", "Janie Ho")
SEOUL = game("198848", "1639229281", "Woobin Shin", "Thomas Fancutt", "ATP Challenger Seoul - Qualifiers")
out, log, d = run(copy.deepcopy(SHI), SEOUL)
check("not paired", not paired(out), paired(out))
check("refused: 'ho' is only inside 'thomas fancutt'", "'ho' is only inside 'thomas fancutt'" in log,
      [l for l in log.splitlines() if "SHIHOX" in l][:3])
WAN = event("KXITFWMATCH", "WANSUN", "Wang vs Sun", "WAN", "Xinyu Wang", "SUN", "Junjie Sun")
GZ = game("3145", "1639157541", "Xinran Sun", "Mananchaya Sawangkaew", "WTA Guangzhou - R1")
out, log, d = run(copy.deepcopy(WAN), GZ)
check("WANSUN not paired, refused for 'wang' inside 'mananchaya sawangkaew'", not paired(out)
      and "'wang' is only inside 'mananchaya sawangkaew'" in log, [l for l in log.splitlines() if "WANSUN" in l][:3])

print("\n[6] HANCHE: whole-word surnames, but a MEN's league for a women's market")
HAN = event("KXITFWMATCH", "HANCHE", "HAN vs Chen", "HAN", "Jiaqi Han", "CHE", "Meng Yi Chen")
MAANSHAN = game("242565", "1634697909", "Zhi Han", "Xing Dao Chen", "ITF Men Maanshan - R1")
out, log, d = run(copy.deepcopy(HAN), MAANSHAN)
check("not paired", not paired(out), paired(out))
check("refused: a men's league for a women's market",
      "a men's league (ITF Men Maanshan - R1) for a women's market" in log, [l for l in log.splitlines() if "HANCHE" in l][:3])
TIANJIN = game("8612", "1634753783", "Jiaqi Han", "Meng Yi Chen", "ITF Women Tianjin - R1")
out, log, d = run(copy.deepcopy(HAN), MAANSHAN + TIANJIN)
check("with the women's match listed: paired to it", paired(out).get(HAN[0]["kalshi_ticker"], ("",))[0].startswith("8612:")
      or paired(out).get(HAN[1]["kalshi_ticker"], ("",))[0].startswith("8612:"), paired(out))
MIXED = game("5555", "1634799999", "Jiaqi Han", "Meng Yi Chen", "United Cup")
out, log, d = run(copy.deepcopy(HAN), MIXED)
check("a league that names no gender passes", bool(paired(out)), paired(out))

print("\n[7] a game that names Kalshi's players beats one whose players only share the surnames")
SAT = event("KXITFWMATCH", "SATCHO", "Sato vs Choi", "SAT", "Rina Sato", "CHO", "Sohee Choi", a_price=0.30)
OTHER_CHOI = game("221782", "1635255649", "Mei Sato", "Onyu Choi", "ITF Women Yeongwol - R1")
OUR_CHOI = game("221790", "1635255777", "Rina Sato", "Sohee Choi", "ITF Women Yeongwol - R1")
out, log, d = run(copy.deepcopy(SAT), OTHER_CHOI + OUR_CHOI)
check("paired to Rina Sato v Sohee Choi", any(t.startswith("221790:") for t, _ in paired(out).values()), paired(out))
check("no clash to price-check", "NAME-CLASH" not in log)

print("\n[8] SATCHO as it was: only the other Sato v Choi listed - paired by surname, then the price decides (tol 0.05)")
Y_SAT, N_SAT = "221782:1635255649:home", "221782:1635255649:away"         # Mei Sato / Onyu Choi
# Kalshi Rina Sato at 0.30 against Pinnacle's Mei Sato: the gate judges the de-vigged price
out, log, d = run(copy.deepcopy(SAT), OTHER_CHOI, gate=True, prices={Y_SAT: 0.42, N_SAT: 0.62})
r = paired(out)
check("prices 0.30 vs 0.40 (de-vigged): CLEARED, the prices disagree", not r and
      "CLEARED: the prices disagree" in log, [l for l in log.splitlines() if "NAME-CLASH" in l])
NL = name_log(d)
check("logged as a clash with its verdicts and result",
      any(x.get("kind") == "clash" and x.get("result") == "apart" and x.get("verdicts") == ["clash", "clash"] for x in NL), NL)
check("the in-play watch does not report it (another match, by price)",
      not any(m["event_id"].endswith("SATCHO") for m in misses(d)), misses(d))
out, log, d = run(copy.deepcopy(SAT), OTHER_CHOI, gate=True, prices={Y_SAT: 0.33, N_SAT: 0.71})
check("prices agree (0.30 vs 0.317) away from a coin flip: STANDS",
      paired(out).get(SAT[0]["kalshi_ticker"], ("",))[0] == Y_SAT and "STANDS, the price confirms it" in log,
      (paired(out), [l for l in log.splitlines() if "NAME-CLASH" in l]))
check("...and the run's last line says 1 confirmed",
      "1 surname-only match(es) confirmed by price, 0 cleared" in log.strip().splitlines()[-1], log.strip().splitlines()[-1])
SAT_EVEN = event("KXITFWMATCH", "SATCHO", "Sato vs Choi", "SAT", "Rina Sato", "CHO", "Sohee Choi", a_price=0.50)
out, log, d = run(copy.deepcopy(SAT_EVEN), OTHER_CHOI, gate=True, prices={Y_SAT: 0.52, N_SAT: 0.52})
check("a coin flip (0.50 vs 0.50): CLEARED - any two matches agree there", not paired(out)
      and "CLEARED: a coin flip" in log, [l for l in log.splitlines() if "NAME-CLASH" in l])
check("...and the in-play watch may still report it (it could be the same player)",
      any(m["event_id"].endswith("SATCHO") and "NAME CLASH" in m["why"] for m in misses(d)), misses(d))
out, log, d = run(copy.deepcopy(SAT), OTHER_CHOI, gate=True, prices={})
check("no price: CLEARED (the gate alone would have let it stand, unvalidated)", not paired(out)
      and "CLEARED: no price to confirm it by" in log, [l for l in log.splitlines() if "NAME-CLASH" in l])
out, log, d = run(copy.deepcopy(SAT), OTHER_CHOI)
check("--no-price-gate: CLEARED too", not paired(out) and "CLEARED: no price" in log,
      [l for l in log.splitlines() if "NAME-CLASH" in l])
LOOSE = {Y_SAT: 0.395, N_SAT: 0.645}                                      # de-vigged 0.38: 0.08 from Kalshi's 0.30
out, log, d = run(copy.deepcopy(SAT), OTHER_CHOI, gate=True, prices=LOOSE)
check("0.30 vs 0.38: cleared at the default 0.05", not paired(out), paired(out))
os.environ["HARDVEN_PAIR_CLASH_TOL"] = "0.12"
out, log, d = run(copy.deepcopy(SAT), OTHER_CHOI, gate=True, prices=LOOSE)
check("HARDVEN_PAIR_CLASH_TOL=0.12 loosens it: 0.30 vs 0.38 stands", bool(paired(out)), paired(out))
os.environ.pop("HARDVEN_PAIR_CLASH_TOL", None)

print("\n[9] the OPPONENT's name counts too: Kalshi 'Jiumeng Liu v Yi Li', Pinnacle 'Jiumeng Liu v Xinyu Li'")
OPP = game("230002", "1638700002", "Jiumeng Liu", "Xinyu Li", "ITF Women Qian Daohu - R1")
out, log, d = run(copy.deepcopy(LIU), OPP, gate=True, prices={"230002:1638700002:home": 0.80,
                                                              "230002:1638700002:away": 0.25})
check("flagged (opponent shares only 'li'), prices disagree (0.60 vs 0.76): cleared",
      not paired(out) and "opponent 'Yi Li' / Pinnacle 'xinyu li'" in log,
      [l for l in log.splitlines() if "NAME-CLASH" in l])

print("\n[10] renderings of one player pair as before, with no price check")
BAR = event("KXITFWMATCH", "BAROSU", "Bartashevich vs Osuigwe", "BAR", "Yara Bartashevich", "OSU", "Whitney Osuigwe")
OSU = game("210500", "1640000001", "Yaroslava Bartashevich", "Whitney Osuigwe", "ITF Women Ortisei - SF")
out, log, d = run(copy.deepcopy(BAR), OSU)
check("Yara / Yaroslava Bartashevich: paired, no clash", bool(paired(out)) and "NAME-CLASH" not in log,
      (paired(out), log[-500:]))
ROZ = event("KXITFMATCH", "SHIROZ", "Shimizu vs Rozin", "SHI", "Yuta Shimizu", "ROZ", "Alexander Rozin")
SAS = game("230274", "1639375316", "Yuta Shimizu", "Sasha Rozin", "ITF Men Monastir - R16")
out, log, d = run(copy.deepcopy(ROZ), SAS)
check("Alexander / Sasha Rozin: paired, no clash", bool(paired(out)) and "NAME-CLASH" not in log,
      (paired(out), log[-500:]))
REI = event("KXATPCHALLENGERMATCH", "SEYREI", "Thiago Seyboth Wild vs Joao Lucas Reis Da Silva", "SEY",
            "Thiago Seyboth Wild", "REI", "Joao Lucas Reis Da Silva")
REIG = game("280700", "1640000777", "Thiago Seyboth Wild", "Joao Reis da Silva", "ATP Challenger Montevideo - R16")
out, log, d = run(copy.deepcopy(REI), REIG)
check("a title with FULL names, Pinnacle dropping the middle one (Reis Da Silva, 11-06): paired",
      bool(paired(out)) and "REFUSED" not in log, (paired(out), [l for l in log.splitlines() if "SEYREI" in l][:3]))
ABI = event("KXITFMATCH", "ABIGAV", "Abibsi vs Gavrielides", "ABI", "Mohamed Ali Abibsi", "GAV", "Liam Gavrielides",
            a_price=0.065)
MON = game("209991", "1640349718", "Mohamed Ali Abibsi", "Liam Gavrielides", "ITF Men Monastir - R16")
out, log, d = run(copy.deepcopy(ABI), MON)
check("an ordinary match: paired, nothing refused, no clash", bool(paired(out)) and "REFUSED" not in log
      and "NAME-CLASH" not in log and not [x for x in name_log(d) if "run" not in x], (paired(out), name_log(d)))

print("\n[11] price_validate's tight terms on their own")
rows_ = [{"kalshi_ticker": "T-1", "kalshi_yes_price": 0.70, "hardven_yes_token": "y1", "hardven_no_token": "n1"},
         {"kalshi_ticker": "T-2", "kalshi_yes_price": 0.70, "hardven_yes_token": "y2", "hardven_no_token": "n2"},
         {"kalshi_ticker": "T-3", "kalshi_yes_price": 0.53, "hardven_yes_token": "y3", "hardven_no_token": "n3"},
         {"kalshi_ticker": "T-4", "kalshi_yes_price": 0.70, "hardven_yes_token": "y4", "hardven_no_token": "n4"},
         {"kalshi_ticker": "T-5", "kalshi_yes_price": 0.70, "hardven_yes_token": "y5", "hardven_no_token": "n5"}]
A._fetch_implied = lambda sidecar, tokens, **kw: {k: v for k, v in {
    "y1": 0.72, "n1": 0.31,          # de-vigged 0.699: ok
    "y2": 0.81, "n2": 0.22,          # 0.786: apart (0.086)
    "y3": 0.55, "n3": 0.48,          # 0.534: even
    "y5": 0.74}.items() if k in tokens}               # y4 unpriced; n5 missing -> the yes token's own 0.74: ok
try:
    out_: list = []
    g = A.price_validate(rows_, "http://127.0.0.1:9", 0.12, tight={id(r): 0.05 for r in rows_}, tight_out=out_)
finally:
    A._fetch_implied = REAL_FETCH
v = {t: x for t, x, _, _ in out_}
check("verdicts ok / apart / even / unpriced / ok (no opponent price: the yes token's own)",
      v == {"T-1": "ok", "T-2": "apart", "T-3": "even", "T-4": "unpriced", "T-5": "ok"}, v)
check("only the ok rows keep their tokens", [bool(r["hardven_yes_token"]) for r in rows_] == [True, False, False, False, True],
      [r["hardven_yes_token"] for r in rows_])
check("counted: 2 consistent, 3 rejected", g == (2, 0, 3, 0), g)
check("never swapped", not any(r.get("price_inverted_fixed") for r in rows_))

print(f"\n{'=' * 58}\n  {PASS} passed, {FAIL} failed\n{'=' * 58}")
sys.exit(1 if FAIL else 0)
