"""The unpaired-board check (pair_pinnacle.board_unused + _unused_alert, 2026-11-27): every basketball / football /
baseball game on Pinnacle's board that no row of the pair file names is reported, with the nearest Kalshi event and
why; a game a row names - by matchup id, or by the two names it resolved to (an in-play listing has its own id) - is
not; tennis is not (board_misses covers it). The Discord line is sent once per game, never on a failed send.

    python test_pair_unused.py          # exit 0 = all pass
"""
from __future__ import annotations
import json
import os
import sys
import tempfile
import types
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.argv = sys.argv[:1]
import pair_pinnacle as P  # noqa: E402

PASS = FAIL = 0


def check(name, ok, got=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}" + (f"  (got {got})" if got != "" else ""))


def game(gid, a, b, start, sport="Basketball", league="NCAA", lid=493):
    return {"start": start, "three_way": False, "teams": {a: f"{lid}:{gid}:home", b: f"{lid}:{gid}:away"},
            "draw": None, "league": lid, "sport": sport, "league_name": league}


def book_of(*games):
    out: dict = {}
    for g in games:
        out.setdefault(frozenset(g["teams"]), []).append(g)
    return out


def rows(event, title, a_code, a, b_code, b, yes=None, no=None, yes_name=None, no_name=None):
    """The two Kalshi markets of a game; the first carries the tokens/names when given (one per event, as written)."""
    r1 = {"kalshi_ticker": f"{event}-{a_code}", "event_id": event, "event_title": title, "kalshi_outcome": a}
    r2 = {"kalshi_ticker": f"{event}-{b_code}", "event_id": event, "event_title": title, "kalshi_outcome": b}
    if yes:
        r1.update(hardven_yes_token=yes, hardven_no_token=no)
    if yes_name:
        r1.update(hardven_yes_name=yes_name, hardven_no_name=no_name)
    return [r1, r2]


print("[1] which board games are reported")
B = book_of(
    game("1", "nc wilmington", "princeton", "2026-11-27T20:00:00Z"),           # names differ -> REPORTED
    game("2", "memphis", "wake forest", "2026-11-27T18:30:00Z"),               # paired by id -> not
    game("3", "georgia", "central florida", "2026-11-28T00:00:00Z"),           # cleared later -> REPORTED
    game("4", "houston", "oakland", "2026-11-28T00:30:00Z"),                   # alias fault -> REPORTED
    game("5", "kansas", "villanova", "2026-11-27T22:00:00Z"),                  # in-play listing, own id -> not
    game("6", "sinner", "alcaraz", "2026-11-27T12:00:00Z", sport="Tennis", league="ATP", lid=1),   # tennis -> not
    game("7", "chicago state", "eastern michigan", "2026-11-27T19:00:00Z"),    # Kalshi has no such game -> REPORTED
    game("8", "kansas city chiefs", "denver broncos", "2026-11-28T01:15:00Z", sport="Football", league="NFL", lid=889),
    # the first live run (11-27 11:23): college FOOTBALL rides in with the NFL walk; no Kalshi series covers it here
    game("9", "georgia state", "ul lafayette", "2026-11-28T19:00:00Z", sport="Football", league="NCAA", lid=880),
    game("10", "ateneo", "la salle", "2026-11-27T08:00:00Z", league="Philippines - NCAA", lid=7777),   # not "^ncaa"
)
pairs = (rows("KXNCAAMBGAME-26NOV27PRINUNCW", "Princeton vs UNC Wilmington", "PRIN", "Princeton", "UNCW", "UNC Wilmington")
         + rows("KXNCAAMBGAME-26NOV27MEMWAKE", "Memphis vs Wake Forest", "MEM", "Memphis", "WAKE", "Wake Forest",
                yes="493:2:home", no="493:2:away", yes_name="memphis", no_name="wake forest")
         + rows("KXNCAAMBGAME-26NOV27UGAUCF", "Georgia vs UCF", "UGA", "Georgia", "UCF", "UCF")
         + rows("KXNCAAMBGAME-26NOV27OAKHOU", "Oakland vs Houston", "OAK", "Oakland", "HOU", "Houston")
         + rows("KXNCAAMBGAME-26NOV27VILLKU", "Villanova vs Kansas", "VILL", "Villanova", "KU", "Kansas",
                yes="493:99:home", no="493:99:away", yes_name="kansas", no_name="villanova")   # paired to the parent id
         + rows("KXNFLGAME-26NOV27KCDEN", "Kansas City vs Denver", "KC", "Kansas City", "DEN", "Denver",
                yes="889:8:home", no="889:8:away", yes_name="kansas city chiefs", no_name="denver broncos"))
unmatched = ["KXNCAAMBGAME-26NOV27PRINUNCW-PRIN  ['princeton', 'unc wilmington'] (best leg 60 — not on Pinnacle's board)",
             "KXNCAAMBGAME-26NOV27OAKHOU-HOU  ['athletics', 'houston'] (best leg 38 — not on Pinnacle's board)"]
filled_now = {"KXNCAAMBGAME-26NOV27UGAUCF-UCF"}
U = P.board_unused(pairs, B, filled_now, unmatched)
by = {u["game"]: u for u in U}
check("exactly the 4 unpaired games are reported", sorted(by) == ["1", "3", "4", "7"], sorted(by))
check("names differ: the matcher's own reason", "best leg 60" in by.get("1", {}).get("why", ""), by.get("1"))
check("names differ: the nearest Kalshi event", by.get("1", {}).get("kalshi") == "Princeton vs UNC Wilmington", by.get("1"))
check("cleared later: said so", "cleared later" in by.get("3", {}).get("why", ""), by.get("3"))
check("the alias fault still finds its Kalshi game (raw names scored too)",
      by.get("4", {}).get("kalshi") == "Oakland vs Houston" and "athletics" in by.get("4", {}).get("why", ""), by.get("4"))
check("no Kalshi game: said so", by.get("7", {}).get("kalshi") == "" and "no game" in by.get("7", {}).get("why", ""),
      by.get("7"))
check("a paired game is not reported (by id)", "2" not in by)
check("an in-play listing of a paired game is not reported (by names)", "5" not in by)
check("tennis is left to board_misses", "6" not in by)
check("a paired NFL game is not reported", "8" not in by)
check("college FOOTBALL (a league no row pairs, no allowed series) is not reported", "9" not in by)
check("a basketball league that only LOOKS like ours ('Philippines - NCAA') is not reported", "10" not in by)
# a league whose every game failed to pair is still watched, through its series' PINNACLE_LEAGUES pattern
unpaired_only = [r for r in pairs if not r.get("hardven_yes_token")]
UO = {u["game"] for u in P.board_unused(unpaired_only, B, set(), [])}
check("no row paired in NCAA at all: its games are still reported (by the '^ncaa' pattern)",
      {"1", "2", "3", "4", "5", "7"} <= UO and "9" not in UO and "10" not in UO, sorted(UO))
check("...and with no NFL row paired either, the NFL game is not (football has no league pattern)", "8" not in UO, UO)
check("the record carries board names, league and start",
      by.get("1", {}).get("board") == {"names": ["nc wilmington", "princeton"], "league": "NCAA",
                                       "start": "2026-11-27T20:00:00Z"}, by.get("1", {}).get("board"))
check("an empty board reports nothing", P.board_unused(pairs, {}, set(), []) == [])
NOW = P._pin_dt("2026-11-27T11:22:00Z")
HB = book_of(game("21", "chicago state", "eastern michigan", "2026-11-27T19:00:00Z"),          # in 8h -> reported
             game("22", "akron", "toledo", "2026-11-27T01:00:00Z"),                            # 10h ago (in play) -> reported
             game("23", "new york giants", "dallas cowboys", "2026-12-07T18:00:00Z", sport="Football", league="NFL", lid=889),
             game("24", "ohio", "kent state", "2026-11-26T20:00:00Z"))                         # 15h ago -> not
HU = {u["game"] for u in P.board_unused(pairs, HB, set(), [], now=NOW)}
check("with `now`: a game in the next 36h and one started up to 12h ago are reported", {"21", "22"} <= HU, HU)
check("with `now`: next week's NFL line (Kalshi may not list it yet) is not", "23" not in HU, HU)
check("with `now`: a game that started 15h ago is not", "24" not in HU, HU)

print("\n[2] the Discord line: once per game, never on a failed send, off by switch")
sent: list = []
ok_box = {"ok": True}


class FakeNotifier:
    def __init__(self, prefix=""):
        self.prefix = prefix

    async def send(self, message):
        sent.append(message)
        return ok_box["ok"]


sys.modules["notify"] = types.SimpleNamespace(Notifier=FakeNotifier)
os.environ["DISCORD_WEBHOOK_URL"] = "https://example.invalid/webhook"      # the fake above never posts it
os.environ.pop("HARDVEN_PAIR_UNUSED_ALERT", None)
state = Path(tempfile.mkdtemp(prefix="unused_")) / "pair_unused_alerted.json"
P._unused_alert(U, state, "2026-11-27T11:22:00Z")
check("first run: one message", len(sent) == 1, len(sent))
check("it lists every new game", all(s in sent[0] for s in ("NC Wilmington v Princeton", "Houston v Oakland",
                                                             "Central Florida v Georgia", "Chicago State v Eastern Michigan")),
      sent[0] if sent else "")
check("a name miss reads 'a name differs'", "a name differs (best leg 60)" in (sent[0] if sent else ""))
check("it says a MISSED pair, not a wrong one", "missed pair, not a wrong one" in (sent[0] if sent else ""))
P._unused_alert(U, state, "2026-11-27T12:52:00Z")
check("next run, same games: nothing", len(sent) == 1, len(sent))
U2 = U + P.board_unused(pairs, book_of(game("11", "drake", "boise state", "2026-11-27T22:30:00Z")), set(), [])
P._unused_alert(U2, state, "2026-11-27T14:22:00Z")
check("a new game: one message with only it", len(sent) == 2 and "Drake" in sent[1] and "Princeton" not in sent[1],
      sent[-1] if sent else "")
ok_box["ok"] = False
U3 = U2 + P.board_unused(pairs, book_of(game("12", "akron", "toledo", "2026-11-27T23:00:00Z")), set(), [])
P._unused_alert(U3, state, "2026-11-27T15:52:00Z")
ok_box["ok"] = True
P._unused_alert(U3, state, "2026-11-27T17:22:00Z")
check("a failed send is retried next run", len(sent) == 4 and "Akron" in sent[3], len(sent))
P._unused_alert(U3, state, "2026-12-02T17:22:00Z")
check("after 4 days a game may alert again (state pruned)", len(sent) == 5, len(sent))
os.environ["HARDVEN_PAIR_UNUSED_ALERT"] = "0"
P._unused_alert(U3 + P.board_unused(pairs, book_of(game("13", "ohio", "kent state", "2026-12-02T23:00:00Z")), set(), []),
                state, "2026-12-02T18:52:00Z")
check("HARDVEN_PAIR_UNUSED_ALERT=0: nothing", len(sent) == 5, len(sent))
os.environ.pop("HARDVEN_PAIR_UNUSED_ALERT", None)
os.environ.pop("DISCORD_WEBHOOK_URL", None)
P._unused_alert(U3 + P.board_unused(pairs, book_of(game("14", "ohio", "kent state", "2026-12-02T23:00:00Z")), set(), []),
                state, "2026-12-02T20:22:00Z")
check("no webhook: nothing", len(sent) == 5, len(sent))

print(f"\n{'=' * 58}\n  {PASS} passed, {FAIL} failed\n{'=' * 58}")
sys.exit(1 if FAIL else 0)
