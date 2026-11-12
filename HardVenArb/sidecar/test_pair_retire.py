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


def kept_log(path):
    p = Path(path).with_name("pair_kept.jsonl")
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines()] if p.exists() else []


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

print("\n[1b] the next run, still not listed -> kept again, the SAME kept_since, nothing rewritten")
out, log, wrote, path = run(None, OTHER, path=path)
a = row(out, ABI["kalshi_ticker"])
check("kept", a.get("hardven_yes_token") == ABI["hardven_yes_token"])
check("kept_since unchanged", a.get("hardven_kept_since") == since, a.get("hardven_kept_since"))
check("file not rewritten", not wrote)
check("last line still reports it", "nothing changed, file not rewritten | kept 1 unlisted row(s)" in log, log.strip().splitlines()[-1])
check("second audit record", len(kept_log(path)) == 2)

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

print("\n[10] a kept row a later check of the same run clears -> logged as cleared, not as kept")
def blank_abi(pairs, sidecar, tol):
    for e in pairs:
        if e.get("kalshi_ticker") == ABI["kalshi_ticker"]:
            e["hardven_yes_token"] = e["hardven_no_token"] = ""
    return (0, 0, 1, 0)
out, log, wrote, path = run([copy.deepcopy(ABI)], OTHER, gate=blank_abi)
L = kept_log(path)
check("final says cleared later", L and L[0]["final"].startswith("cleared later"), L)
check("last line counts it out", "kept 0 unlisted row(s)" in log.strip().splitlines()[-1], log.strip().splitlines()[-1])

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

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
