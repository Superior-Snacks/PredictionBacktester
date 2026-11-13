"""pair_pinnacle's retire step (2026-11-12): a row whose matchup has left the board KEEPS its pairing unless the
run finds the replacement - and every kept row is re-justified each run (age cap, sidecar evidence, audit log).
Runs the REAL main() end to end on a temp pair file, with /catalog, the price gate, the slate log and the sidecar
evidence call stubbed (no sidecar, no network, nothing written outside the temp dir).

Built on the real rows of KXITFMATCH-26NOV12ABIGAV (Abibsi v Gavrielides, ITF Monastir): the pre-match matchup
P 209991:1640349718 and its live replacement C 209991:1640367833. Times are relative to now.

    python test_pair_retire.py          # exit 0 = all pass
"""
from __future__ import annotations
import contextlib, copy, datetime as dt, io, json, os, sys, tempfile, time, types
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.argv = sys.argv[:1]
sys.modules["slate_log"] = types.SimpleNamespace(record=lambda obs: {"ok": False}, LOG=Path("unused"))
import pair_pinnacle as P  # noqa: E402

REAL_EVIDENCE = P._sidecar_evidence
P._sidecar_evidence = lambda sidecar, sid, probe, timeout=3.0: {"stub": True, "redirect": sid}
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
iso = lambda t: t.strftime("%Y-%m-%dT%H:%M:%SZ")
START, OLD, TODAY = iso(NOW - dt.timedelta(hours=2)), iso(NOW - dt.timedelta(hours=13)), NOW.strftime("%Y-%m-%d")

ABI = {"kalshi_ticker": "KXITFMATCH-26NOV12ABIGAV-ABI", "label": "Mohamed Ali Abibsi wins",
       "event_title": "Abibsi vs Gavrielides", "kalshi_outcome": "Mohamed Ali Abibsi",
       "event_id": "KXITFMATCH-26NOV12ABIGAV", "settlement_date": TODAY, "kalshi_yes_price": 0.065,
       "is_neg_risk": False, "hardven_min_size": 1.0,
       "hardven_league_url": "https://www.pinnacle.bet/en/tennis/itf-men-monastir-r16/matchups/",
       "hardven_start_time": START,
       "hardven_yes_token": "209991:1640349718:home", "hardven_no_token": "209991:1640349718:away",
       "hardven_yes_name": "mohamed ali abibsi", "hardven_no_name": "liam gavrielides"}
GAV = {"kalshi_ticker": "KXITFMATCH-26NOV12ABIGAV-GAV", "label": "Liam Gavrielides wins",
       "event_title": "Abibsi vs Gavrielides", "kalshi_outcome": "Liam Gavrielides",
       "event_id": "KXITFMATCH-26NOV12ABIGAV", "settlement_date": TODAY, "kalshi_yes_price": 0.935,
       "is_neg_risk": False, "hardven_min_size": 1.0, "hardven_yes_token": "", "hardven_no_token": ""}


def game(lid, mid, home, away, start=START, league="ITF Men Monastir - R16"):
    return [{"selection_id": f"{lid}:{mid}:home", "selection_name": home, "start_time": start, "league": league,
             "sport": "Tennis"},
            {"selection_id": f"{lid}:{mid}:away", "selection_name": away, "start_time": start, "league": league,
             "sport": "Tennis"}]


OTHER = game("209991", "1640399999", "Jan Novak", "Petr Svoboda")            # keeps the catalog non-empty
PRE = game("209991", "1640349718", "Mohamed Ali Abibsi", "Liam Gavrielides")   # P, still listed
LIVE = game("209991", "1640367833", "Mohamed Ali Abibsi", "Liam Gavrielides")  # C, the live replacement


def run(rows, catalog, path=None, gate=None):
    """main() on a temp pair file -> (rows after, output, file rewritten?, the pair file's path)."""
    if path is None:
        path = os.path.join(tempfile.mkdtemp(prefix="pairtest_"), "cross_pairs.json")
        Path(path).write_text(json.dumps(rows), encoding="utf-8")
    before = Path(path).read_text(encoding="utf-8")
    P.fetch_catalog = lambda sidecar, timeout=0: copy.deepcopy(catalog)
    argv = ["pair_pinnacle.py", "--write", "--pairs", path, "--sidecar", "http://127.0.0.1:9"]
    if gate is None:
        argv.append("--no-price-gate")
    else:
        P.price_validate = gate
    sys.argv = argv
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        P.main()
    after = Path(path).read_text(encoding="utf-8")
    return json.loads(after), buf.getvalue(), after != before, path


def row(rows, ticker):
    return next(r for r in rows if r["kalshi_ticker"] == ticker)


def _log_lines(path):
    p = Path(path).with_name("pair_kept.jsonl")
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines()] if p.exists() else []


def kept_log(path):
    """The row records (kept / released) - the per-run records are run_log's, the endings ended_log's."""
    return [j for j in _log_lines(path) if "run" not in j and j.get("decision") != "ended"]


def ended_log(path):
    """How rows kept until a run ended at it (re-filled / other market / conflict)."""
    return [j for j in _log_lines(path) if j.get("decision") == "ended"]


def run_log(path):
    return [j["run"] for j in _log_lines(path) if "run" in j]


print("[1] the match is past its listed start: P off the board, no replacement yet -> the row KEEPS P")
out, log, wrote, path = run([copy.deepcopy(ABI), copy.deepcopy(GAV)], OTHER)
a = row(out, ABI["kalshi_ticker"])
check("yes token kept", a.get("hardven_yes_token") == ABI["hardven_yes_token"], a.get("hardven_yes_token"))
check("no token kept", a.get("hardven_no_token") == ABI["hardven_no_token"], a.get("hardven_no_token"))
check("names kept", (a.get("hardven_yes_name"), a.get("hardven_no_name")) == ("mohamed ali abibsi", "liam gavrielides"))
check("the mirror market stays unpaired", not row(out, GAV["kalshi_ticker"]).get("hardven_yes_token"))
since = a.get("hardven_kept_since")
check("kept_since stamped (now)", bool(since) and abs((P._pin_dt(since) - NOW).total_seconds()) < 120, since)
check("file rewritten (to carry the stamp)", wrote)
check("summary", "0 re-filled from it, 1 not listed right now and KEPT" in log and "0 released by the 12h age cap" in log, log[-500:])
check("last line says what was kept", log.strip().splitlines()[-1].endswith("kept 1 unlisted row(s), released 0 (pair_kept.jsonl)"),
      log.strip().splitlines()[-1])
L = kept_log(path)
check("one audit record", len(L) == 1, L)
check("record: kept, final kept, the token, the evidence",
      L and L[0]["decision"] == "kept" and L[0]["final"] == "kept" and L[0]["token"] == ABI["hardven_yes_token"]
      and L[0]["evidence"] == {"stub": True, "redirect": ABI["hardven_yes_token"]}, L[:1])
check("record: hours past the listed start", L and 1.9 < L[0]["hours_past_start"] < 2.1, L[:1])
RL = run_log(path)
check("one run record: held 1, kept 1", RL == [{"held": 1, "refilled": 0, "kept": 1, "cleared_later": 0,
                                               "released": 0, "named": 0, "ended": 0, "names_kept": 0}], RL)
check("the run record shares the rows' timestamp, and comes first",
      _log_lines(path)[0].get("run") is not None and _log_lines(path)[0]["at"] == L[0]["at"], _log_lines(path)[:2])

print("\n[1b] the next run, still not listed -> kept again, the SAME kept_since, nothing rewritten")
out, log, wrote, path = run(None, OTHER, path=path)
a = row(out, ABI["kalshi_ticker"])
check("kept", a.get("hardven_yes_token") == ABI["hardven_yes_token"])
check("kept_since unchanged", a.get("hardven_kept_since") == since, a.get("hardven_kept_since"))
check("file not rewritten", not wrote)
check("last line still reports it", "nothing changed, file not rewritten | kept 1 unlisted row(s)" in log, log.strip().splitlines()[-1])
check("second audit record", len(kept_log(path)) == 2)
check("second run record", len(run_log(path)) == 2 and run_log(path)[1]["kept"] == 1, run_log(path))

print("\n[2] the live replacement is on the board -> RE-POINTED at it, and the kept_since goes")
prev = dict(copy.deepcopy(ABI), hardven_kept_since=since)
out, log, wrote, path = run([prev, copy.deepcopy(GAV)], OTHER + LIVE)
a = row(out, ABI["kalshi_ticker"])
check("yes -> live matchup, same player (home)", a.get("hardven_yes_token") == "209991:1640367833:home", a.get("hardven_yes_token"))
check("no -> live matchup, the opponent", a.get("hardven_no_token") == "209991:1640367833:away", a.get("hardven_no_token"))
check("kept_since cleared", "hardven_kept_since" not in a, a.get("hardven_kept_since"))
check("the mirror market stays unpaired", not row(out, GAV["kalshi_ticker"]).get("hardven_yes_token"))
check("summary", "1 re-filled from it, 0 not listed" in log, log[-500:])
check("no audit record (nothing kept)", not kept_log(path))
EL = ended_log(path)
check("the ending is logged: re-pointed at the live matchup, paired at the end of the run",
      len(EL) == 1 and EL[0]["how"] == "re-pointed" and EL[0]["token"] == ABI["hardven_yes_token"]
      and EL[0]["new_token"] == "209991:1640367833:home" and EL[0]["kept_since"] == since
      and EL[0]["final"] == "paired", EL)
check("but a run record: the held row was re-filled", run_log(path) == [{"held": 1, "refilled": 1, "kept": 0,
                                                                         "cleared_later": 0, "released": 0,
                                                                         "named": 0, "ended": 1, "names_kept": 0}], run_log(path))

print("\n[3] the event re-pairs through its OTHER market -> the old row stays clear (no two matchups per event)")
out, log, wrote, path = run([copy.deepcopy(GAV), copy.deepcopy(ABI)], OTHER + LIVE)
g, a = row(out, GAV["kalshi_ticker"]), row(out, ABI["kalshi_ticker"])
check("sibling paired to the live matchup, its own player (away)", g.get("hardven_yes_token") == "209991:1640367833:away", g.get("hardven_yes_token"))
check("old row NOT restored to P", not a.get("hardven_yes_token"), a.get("hardven_yes_token"))

print("\n[4] P still listed -> re-filled to the same matchup, ONE market per event (the behaviour since August)")
out, log, wrote, path = run([copy.deepcopy(ABI), copy.deepcopy(GAV)], OTHER + PRE)
a = row(out, ABI["kalshi_ticker"])
check("still on P", a.get("hardven_yes_token") == ABI["hardven_yes_token"], a.get("hardven_yes_token"))
check("the mirror market is NOT paired too", not row(out, GAV["kalshi_ticker"]).get("hardven_yes_token"))
check("not marked as kept", "hardven_kept_since" not in a)

print("\n[5] a whole league missing from one catalog walk -> the row is kept")
out, log, wrote, path = run([copy.deepcopy(ABI)], game("6870", "1640300001", "Ann Lee", "Bea Kim", league="WTA X"))
check("kept", row(out, ABI["kalshi_ticker"]).get("hardven_yes_token") == ABI["hardven_yes_token"])

print("\n[6] one run, two events: the listed one re-fills, the unlisted one is kept, nothing else moves")
FOO = dict(copy.deepcopy(ABI), kalshi_ticker="KXITFMATCH-26NOV12FOOBAR-FOO", label="Ann Foo wins",
           event_title="Foo vs Bar", kalshi_outcome="Ann Foo", event_id="KXITFMATCH-26NOV12FOOBAR",
           hardven_yes_token="209991:1640355555:home", hardven_no_token="209991:1640355555:away",
           hardven_yes_name="ann foo", hardven_no_name="bea bar")
out, log, wrote, path = run([copy.deepcopy(ABI), copy.deepcopy(GAV), copy.deepcopy(FOO)], OTHER + LIVE)
check("listed event re-pointed", row(out, ABI["kalshi_ticker"]).get("hardven_yes_token") == "209991:1640367833:home")
f = row(out, FOO["kalshi_ticker"])
check("unlisted event kept, tokens and names", (f.get("hardven_yes_token"), f.get("hardven_no_name")) == ("209991:1640355555:home", "bea bar"))
check("counts", "re-checked 2 paired row(s) against the board: 1 re-filled from it, 1 not listed" in log, log[-500:])
check("other fields untouched", {k: v for k, v in f.items() if not k.startswith("hardven_")} ==
      {k: v for k, v in FOO.items() if not k.startswith("hardven_")})

print("\n[7] AGE CAP: 13h past its listed start and still not listed -> RELEASED (cleared), logged")
out, log, wrote, path = run([dict(copy.deepcopy(ABI), hardven_start_time=OLD)], OTHER)
a = row(out, ABI["kalshi_ticker"])
check("cleared", not a.get("hardven_yes_token") and "hardven_yes_name" not in a and "hardven_kept_since" not in a, a)
check("summary", "1 released by the 12h age cap" in log, log[-500:])
L = kept_log(path)
check("audit record: released with the reason", L and L[0]["decision"] == "released" and "past its listed start" in L[0]["reason"], L)
check("run record counts the release", run_log(path) == [{"held": 1, "refilled": 0, "kept": 0, "cleared_later": 0,
                                                           "released": 1, "named": 0, "ended": 0, "names_kept": 0}], run_log(path))

print("\n[8] the cap is HARDVEN_PAIR_KEEP_MAX_H")
os.environ["HARDVEN_PAIR_KEEP_MAX_H"] = "20"
try:
    out, log, wrote, path = run([dict(copy.deepcopy(ABI), hardven_start_time=OLD)], OTHER)
finally:
    del os.environ["HARDVEN_PAIR_KEEP_MAX_H"]
check("13h kept under a 20h cap", row(out, ABI["kalshi_ticker"]).get("hardven_yes_token") == ABI["hardven_yes_token"])

print("\n[9] no listed start: the cap counts from when the keeping began")
nostart = {k: v for k, v in ABI.items() if k != "hardven_start_time"}
out, log, wrote, path = run([dict(copy.deepcopy(nostart), hardven_kept_since=OLD)], OTHER)
check("kept 13h ago -> released", not row(out, ABI["kalshi_ticker"]).get("hardven_yes_token"))
out, log, wrote, path = run([dict(copy.deepcopy(nostart), hardven_kept_since=START)], OTHER)
check("kept 2h ago -> kept", row(out, ABI["kalshi_ticker"]).get("hardven_yes_token") == ABI["hardven_yes_token"])

print("\n[10] a kept row a later check of the same run clears (the name audit: its matchup is listed under OTHER\n"
      "     names, so the matcher could not re-fill it) -> logged as cleared, not as kept, and the clear is written")
RENAMED = game("209991", "1640349718", "Xavier Abcde", "Yannick Defgh")
prev = dict(copy.deepcopy(ABI), hardven_kept_since=START)        # kept since an earlier run: nothing to stamp
out, log, wrote, path = run([prev], OTHER + RENAMED)
L = kept_log(path)
a = row(out, ABI["kalshi_ticker"])
check("final says cleared later", L and L[0]["final"].startswith("cleared later"), L)
check("the clear reached the file (tokens, names and the kept stamp gone)",
      wrote and not a.get("hardven_yes_token") and not a.get("hardven_yes_name") and "hardven_kept_since" not in a, a)
check("last line counts it out", "kept 0 unlisted row(s)" in log.strip().splitlines()[-1], log.strip().splitlines()[-1])
check("run record: kept 0, cleared_later 1", run_log(path) and run_log(path)[0]["kept"] == 0
      and run_log(path)[0]["cleared_later"] == 1, run_log(path))

print("\n[11] the evidence call against a sidecar that is not there")
probe = {"ok": True}
t0 = time.time()
ev = REAL_EVIDENCE("http://127.0.0.1:9", "209991:1640349718:home", probe, timeout=2.0)
check("returns the error", "error" in ev, ev)
check("stops asking after one failure", probe["ok"] is False and REAL_EVIDENCE("http://127.0.0.1:9", "x:y:home", probe) == {})
check("fails fast", time.time() - t0 < 5, round(time.time() - t0, 1))

print("\n[12] a kept row that LOST its names is named from the sidecar's record of its matchup (never on a guess)")
NAMELESS = {k: v for k, v in ABI.items() if k not in ("hardven_yes_name", "hardven_no_name")}
STUB = P._sidecar_evidence
try:
    P._sidecar_evidence = lambda s, sid, probe, timeout=3.0: {
        "parent_names": {"home": "Mohamed Ali Abibsi", "away": "Liam Gavrielides"}}
    out, log, wrote, path = run([copy.deepcopy(NAMELESS)], OTHER)
    a = row(out, ABI["kalshi_ticker"])
    check("names filled (YES = the Kalshi outcome's player)",
          (P._norm(a.get("hardven_yes_name") or ""), P._norm(a.get("hardven_no_name") or "")) ==
          (P._norm("Mohamed Ali Abibsi"), P._norm("Liam Gavrielides")), (a.get("hardven_yes_name"), a.get("hardven_no_name")))
    check("logged names_from=sidecar", kept_log(path)[0].get("names_from") == "sidecar", kept_log(path))
    check("run record: named 1", run_log(path) and run_log(path)[0]["named"] == 1, run_log(path))
    check("file rewritten", wrote)

    P._sidecar_evidence = lambda s, sid, probe, timeout=3.0: {
        "parent_names": {"home": "Liam Gavrielides", "away": "Mohamed Ali Abibsi"}}   # YES side = the OTHER player
    out, log, wrote, path = run([copy.deepcopy(NAMELESS)], OTHER)
    a = row(out, ABI["kalshi_ticker"])
    check("a YES name that does not name the Kalshi outcome is NOT used", not a.get("hardven_yes_name"), a.get("hardven_yes_name"))
    check("still kept, logged names_from=none", a.get("hardven_yes_token") == ABI["hardven_yes_token"]
          and kept_log(path)[0].get("names_from") == "none", kept_log(path))

    P._sidecar_evidence = lambda s, sid, probe, timeout=3.0: {"error": "URLError"}
    out, log, wrote, path = run([copy.deepcopy(NAMELESS)], OTHER)
    check("sidecar unreachable: kept, names_from=none", kept_log(path)[0].get("names_from") == "none")
finally:
    P._sidecar_evidence = STUB
out, log, wrote, path = run([copy.deepcopy(ABI)], OTHER)
check("a row that has its names: names_from=file, untouched", kept_log(path)[0].get("names_from") == "file"
      and row(out, ABI["kalshi_ticker"]).get("hardven_yes_name") == "mohamed ali abibsi")

print("\n[13] the PRICE GATE never sees a kept row (between session windows its price is frozen) - but still\n"
      "     checks every row the board re-filled")
FOO_LISTED = game("209991", "1640355555", "Ann Foo", "Bea Bar")
seen = []
def spy_blank_all(pairs, sidecar, tol, **kw):
    """A gate that would clear EVERY row it is given - the in-play-vs-frozen-price misfire of 11-12/13."""
    for e in pairs:
        if e.get("hardven_yes_token"):
            seen.append(e["kalshi_ticker"])
            e["hardven_yes_token"] = e["hardven_no_token"] = ""
    return (0, 0, len(seen), 0)
out, log, wrote, path = run([copy.deepcopy(ABI), copy.deepcopy(FOO)], OTHER + FOO_LISTED, gate=spy_blank_all)
check("the gate was given the re-filled row only", seen == [FOO["kalshi_ticker"]], seen)
check("the kept row survives it, final kept", row(out, ABI["kalshi_ticker"]).get("hardven_yes_token") == ABI["hardven_yes_token"]
      and kept_log(path)[0]["final"] == "kept", kept_log(path))
check("the re-filled row is still gated (cleared by it)", not row(out, FOO["kalshi_ticker"]).get("hardven_yes_token"))

print("\n[14] how a row kept until now ENDS is logged, every way it can")
k = dict(copy.deepcopy(ABI), hardven_kept_since=START)
out, log, wrote, path = run([k, copy.deepcopy(GAV)], OTHER + PRE)
EL = ended_log(path)
check("P back on the board -> 'listed again', same token, paired", len(EL) == 1 and EL[0]["how"] == "listed again"
      and EL[0]["new_token"] == ABI["hardven_yes_token"] and EL[0]["final"] == "paired", EL)
check("  and the run record counts it", run_log(path)[0]["ended"] == 1 and run_log(path)[0]["kept"] == 0, run_log(path))
check("  and the row is no longer marked kept", "hardven_kept_since" not in row(out, ABI["kalshi_ticker"]))
out, log, wrote, path = run([copy.deepcopy(GAV), dict(copy.deepcopy(ABI), hardven_kept_since=START)], OTHER + LIVE)
EL = ended_log(path)
check("its event re-paired through the sibling -> 'other market'", len(EL) == 1 and EL[0]["how"] == "other market"
      and EL[0]["new_token"] is None and "final" not in EL[0], EL)
out, log, wrote, path = run([copy.deepcopy(ABI), copy.deepcopy(GAV)], OTHER + PRE)
check("a row that was NOT kept before logs no ending", not ended_log(path) and run_log(path)[0]["ended"] == 0,
      _log_lines(path))
def swap_all(pairs, sidecar, tol, **kw):
    for e in pairs:
        if e.get("hardven_yes_token"):
            e["hardven_yes_token"], e["hardven_no_token"] = e["hardven_no_token"], e["hardven_yes_token"]
    return (0, 1, 0, 0)
out, log, wrote, path = run([dict(copy.deepcopy(ABI), hardven_kept_since=START), copy.deepcopy(GAV)], OTHER + PRE,
                            gate=swap_all)
EL = ended_log(path)
check("re-filled, then the gate swaps its sides -> final says so (and the name audit then clears it)",
      len(EL) == 1 and EL[0]["final"].startswith(("swapped", "cleared")), EL)

print("\n[15] COIN FLIPS (the REAL price gate, prices stubbed): Pinnacle's margin puts a near-even match above 50c on\n"
      "     BOTH sides, so Kalshi 0.49 vs Pinnacle 0.52 reads as 'inverted'. The board's names settle the side.")
import pair_auto as A  # noqa: E402
REAL_GATE, REAL_FETCH = A.price_validate, A._fetch_implied
try:
    def prices(home, away):
        A._fetch_implied = lambda sidecar, tokens: {t: (home if t.endswith(":home") else away) for t in tokens}

    prices(0.52, 0.52)
    out, log, wrote, path = run([dict(copy.deepcopy(ABI), kalshi_yes_price=0.49), copy.deepcopy(GAV)], OTHER + PRE,
                                gate=REAL_GATE)
    a = row(out, ABI["kalshi_ticker"])
    check("stays paired on the named side (Abibsi = home), not swapped and blanked",
          (a.get("hardven_yes_token"), a.get("hardven_yes_name")) == (ABI["hardven_yes_token"], "mohamed ali abibsi"),
          (a.get("hardven_yes_token"), a.get("hardven_yes_name")))
    check("logged NAMES-KEEP, no MIS-ORIENTED", "NAMES-KEEP KXITFMATCH-26NOV12ABIGAV-ABI" in log and "MIS-ORIENTED" not in log,
          log[-600:])
    check("not flagged inverted", not a.get("price_inverted_fixed"))
    check("run record: names_kept 1", run_log(path)[0]["names_kept"] == 1, run_log(path))

    prices(0.85, 0.17)                                          # the named side at 0.85 vs Kalshi 0.10: no coin flip
    out, log, wrote, path = run([dict(copy.deepcopy(ABI), kalshi_yes_price=0.10), copy.deepcopy(GAV)], OTHER + PRE,
                                gate=REAL_GATE)
    check("names never rescue a real conflict: 0.75 off on its named side -> rejected as a wrong game",
          not row(out, ABI["kalshi_ticker"]).get("hardven_yes_token") and "PRICE-REJECT" in log
          and run_log(path)[0]["names_kept"] == 0, log[-600:])

    prices(0.08, 0.95)                                          # the usual case: both venues agree
    out, log, wrote, path = run([copy.deepcopy(ABI), copy.deepcopy(GAV)], OTHER + PRE, gate=REAL_GATE)
    check("agreeing prices: paired, nothing logged", row(out, ABI["kalshi_ticker"]).get("hardven_yes_token") ==
          ABI["hardven_yes_token"] and "NAMES-KEEP" not in log and "PRICE-REJECT" not in log, log[-400:])

    TWIN = dict(copy.deepcopy(ABI), kalshi_outcome="Mohamed Abibsi", kalshi_yes_price=0.49)
    prices(0.52, 0.52)
    out, log, wrote, path = run([TWIN, copy.deepcopy(GAV)], OTHER + game("209991", "1640349718", "Mohamed Ali Abibsi",
                                                                       "Karim Abibsi"), gate=REAL_GATE)
    check("names that cannot settle it (both players share 'abibsi'): left to the gate as before",
          "NAMES-KEEP" not in log and run_log(path)[0]["names_kept"] == 0, log[-600:])
finally:
    A.price_validate, A._fetch_implied = REAL_GATE, REAL_FETCH

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
