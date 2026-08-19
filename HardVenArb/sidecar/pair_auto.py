"""
pair_auto.py — auto-fill the bookmaker side of cross_pairs.json by matching to the Kalshi scaffolds.

Workflow:
  1. pairHard.py scaffolds cross_pairs.json: Kalshi side filled (incl. `event_title` = the "A vs B"
     matchup and `kalshi_outcome` = this market's YES side, e.g. "Jordan"/"Tie"/"Hijikata"), tokens BLANK.
  2. The sidecar runs the bookmaker adapter (HARDVEN_BOOK=bookmaker) and exposes /catalog — every game's
     "<idgm>:<idlg>:H/V" (and ":D" draw for 3-way) selections + team/player names + a three_way flag.
  3. This GETs /catalog and, per Kalshi entry, matches the bookmaker game by the SET of the two
     team/player names, maps the Kalshi YES outcome → the bookmaker selection that IS it
     (team → :H/:V; "Tie" → :D), writes the tokens, and stamps three_way for 3-way (soccer 1X2) pairs.

Matching is deterministic name-set matching (order/accent-insensitive) — best for sports' canonical
entities. Nothing is written without --write; unmatched entries are left blank (the bot skips blanks).

--fuzzy enables rapidfuzz name-variant matching for the systematic gaps (Kalshi city "boston" vs book
"boston red sox"; cricket "scotland" vs "scotland w"; soccer "congo dr" vs "dr congo"; boxing spelling).
Fuzzy-matched pairs are AUTO-FILLED but tagged "fuzzy": true so they can be re-verified before any
real-money M1 — the settlement back-test, not the matcher, is what authorizes betting. Fine for
observe-only M0 telemetry.

3-way (soccer): the three outcomes (TeamA / Tie / TeamB) are DISTINCT binaries, not mirrors — all three
are filled (each is its own NO-only pair). 2-way (tennis etc.): the two markets are mirrors → one filled.

  python pair_auto.py                       # dry-run preview (exact matches only)
  python pair_auto.py --write               # write cross_pairs.json (exact)
  python pair_auto.py --fuzzy --write       # also auto-fill team-name variants (flagged "fuzzy":true)
  python pair_auto.py --write --both        # also fill the 2-way mirror market (telemetry only; M1 = double bet)
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import unicodedata
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")  # Windows console: tolerate accented names

try:
    from rapidfuzz import fuzz  # optional; only used for the --fuzzy review suggestions
except Exception:
    fuzz = None


def _norm(s: str) -> str:
    """Lowercase, strip accents/punctuation, collapse spaces."""
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
    s = re.sub(r"[^a-z0-9 ]", " ", s.lower())
    return " ".join(s.split())


def _book_name(selection_name: str) -> str:
    """Bookmaker selection name → match key. 'Hijikata, Rinky' → 'hijikata' (surname before comma);
    'Jordan' / 'New York Yankees' → the whole normalized name (teams have no comma)."""
    return _norm(selection_name.split(",")[0])


def _teams_from_event_title(title: str):
    """'Jordan vs Argentina' / 'Hijikata vs Lehecka: Round Of 16' / 'Fight Night: Kape vs Horiguchi'
    → frozenset of the two competitor names. A colon can be a CARD-NAME PREFIX on the LEFT side
    (UFC 'Fight Night: …') OR a ROUND SUFFIX on the RIGHT ('…: Round Of 16'), so take the left
    competitor AFTER its last colon and the right competitor BEFORE its first colon."""
    parts = re.split(r"\bvs\.?\b", title or "", flags=re.IGNORECASE)
    if len(parts) != 2:
        return None
    a = _norm(parts[0].rsplit(":", 1)[-1])   # drop a leading "Fight Night:" / card-name prefix
    b = _norm(parts[1].split(":", 1)[0])      # drop a trailing ": Round Of 16" / ": Quarterfinal"
    return frozenset({a, b}) if a and b else None


def _kalshi_matchup_legacy(label: str):
    """Fallback for already-scaffolded entries with no event_title: parse the tennis-style label
    'Will X win the A vs B: Round...' → (yes_name, {A,B})."""
    mu = re.search(r"\bthe (.+?) vs (.+?)\s*[:?]", label or "")
    yes = re.search(r"\bWill (.+?) win\b", label or "")
    if not mu or not yes:
        return None
    a, b = _norm(mu.group(1)), _norm(mu.group(2))
    return (_norm(yes.group(1)), frozenset({a, b}))


def kalshi_key(entry: dict):
    """→ (yes_outcome, team_set, is_tie) or None. Prefer event_title + kalshi_outcome (works for ALL
    sports); fall back to the legacy tennis label for old entries."""
    et, ko = entry.get("event_title"), entry.get("kalshi_outcome")
    if et and ko:
        teams = _teams_from_event_title(et)
        if teams:
            y = _norm(ko)
            return (y, teams, y in ("tie", "draw"))
    legacy = _kalshi_matchup_legacy(entry.get("label", ""))
    if legacy:
        return (legacy[0], legacy[1], False)
    return None


def _score_book_team(outcome: str, k: str) -> int:
    """How strongly does book team `k` claim to BE the Kalshi outcome? 0-100, higher is stronger.

    Split out of _pick_book_team so both candidates can be scored on the same scale and COMPARED. The
    previous version returned the first key that satisfied a tier, which is only safe when at most one can
    - and in a two-player field that is not true. A shared token is the obvious case: 'Maria Fernanda
    Lopes' and 'Maria Sousa Salazar' share 'maria', so an overlap test hands back whichever happened to be
    first in the dict.

    Shared tokens are therefore weighted by how DISTINCTIVE they are. A surname carries the identity; a
    particle or initial ('de', 'del', 'van', 'm', 'jr') is shared by half a draw and decides nothing.
    """
    if k == outcome:
        return 100
    if outcome in k or k in outcome:
        return 95
    ot, kt = outcome.split(), k.split()
    shared = set(ot) & set(kt)
    if shared:
        # 4+ characters and not a known particle = a real name token, not a connector.
        strong = {t for t in shared if len(t) >= 4 and t not in _NAME_PARTICLES}
        if not strong:
            return 62
        # THE SURNAME CARRIES THE IDENTITY. Two players in one draw share a given name far more often
        # than a surname, so "same last token" is near-decisive while "same first name only" is weak —
        # which is exactly the pair that used to invert: 'Maria Fernanda Lopes' vs 'Maria Sousa Salazar'
        # share 'maria', and scoring that alongside a full containment made the field look like a tie.
        if ot and kt and ot[-1] == kt[-1]:
            return 88
        return 85 if len(strong) >= 2 else 70
    return int(fuzz.token_set_ratio(outcome, k)) if fuzz is not None else 0


# Tokens that recur across unrelated names and so cannot identify anyone on their own.
_NAME_PARTICLES = {"del", "dela", "della", "van", "von", "der", "den", "dos", "das", "santos",
                   "junior", "senior", "sousa", "silva"}


def _pick_book_team(outcome: str, team_keys, min_margin: int = 12):
    """Which book team key IS the Kalshi outcome — or None when the field cannot say.

    RETURNS None ON A TIE, and that is the point. This used to answer with the first key that matched any
    tier, so an ambiguous field silently produced a CONFIDENT wrong answer: the yes/no tokens came back
    inverted, both legs of the "arb" sat on the same outcome, and every number downstream — the arb, the
    LOCK, the P&L — was computed as though they were opposite. Two of those reached real money on
    2026-08-19 (KXITFMATCH-26AUG19IZQREY-REY and its siblings) and nothing in the pipeline could see it.

    A refused pair costs one market for one day. An inverted pair costs the stake, and looks like a win
    until it settles — so when the two candidates are within `min_margin` of each other, the honest answer
    is that this outcome is not resolvable and the caller should leave it unmatched.
    """
    keys = list(team_keys)
    if not keys:
        return None
    scored = sorted(((_score_book_team(outcome, k), k) for k in keys), reverse=True)
    best_score, best_key = scored[0]
    if best_score < 60:
        return None
    if len(scored) > 1 and best_score - scored[1][0] < min_margin:
        return None                      # too close to call — see the docstring
    return best_key


def _team_sim(k: str, b: str) -> int:
    """Per-team similarity 0-100. Prefers the LEGIT direction Kalshi ⊆ Book ('boston'⊆'boston red sox',
    'new york m'⊆'new york mets') = 100; PENALIZES Book ⊆ Kalshi (a barer book name, e.g. book 'new york'
    vs Kalshi 'new york m' — the hallmark of a wrong-league lookalike) = 60; else token_set_ratio
    (handles word-order 'congo dr'/'dr congo' and spelling 'lewicki'/'lewicky')."""
    if k == b or k in b:
        return 100
    if b in k:
        return 60
    return int(fuzz.token_set_ratio(k, b)) if fuzz is not None else 0


# LEAGUE ANCHOR: Kalshi series prefix → the bookmaker sport(s) it must match (the catalog's `sport` label,
# from DEFAULT_CATALOG_SPORTS). Stops a Kalshi market matching a similarly-named game in the WRONG sport.
# A series NOT listed here is simply not anchored (matches across all sports = old behaviour) — so if you
# add a series to pairHard.CLASSIC_SERIES, add it here too to get anchoring. Mirror of those sport groups.
SERIES_SPORT = {
    "KXMLBGAME": {"BASEBALL"}, "KXKBOGAME": {"BASEBALL"}, "KXNPBGAME": {"BASEBALL"},
    "KXNBAGAME": {"BASKETBALL"}, "KXWNBAGAME": {"BASKETBALL"}, "KXACBGAME": {"BASKETBALL"},
    "KXBBLGAME": {"BASKETBALL"}, "KXBBSERIEAGAME": {"BASKETBALL"}, "KXBBSERIEBGAME": {"BASKETBALL"},
    "KXBSLGAME": {"BASKETBALL"}, "KXLNBELITEGAME": {"BASKETBALL"}, "KXNZNBLGAME": {"BASKETBALL"},
    "KXPLKGAME": {"BASKETBALL"}, "KXSPBGAME": {"BASKETBALL"}, "KXVBAGAME": {"BASKETBALL"},
    "KXBSNGAME": {"BASKETBALL"}, "KXBIG3GAME": {"BASKETBALL"}, "KXISLGAME": {"BASKETBALL"},
    "KXNCAABBGAME": {"BASKETBALL"},
    "KXWCGAME": {"FIFA WORLD CUP", "SOCCER"}, "KXUSLGAME": {"SOCCER"}, "KXUSLCUPGAME": {"SOCCER"},
    "KXLALIGA2GAME": {"SOCCER"}, "KXCHLLDPGAME": {"SOCCER"}, "KXBOLPDIVGAME": {"SOCCER"},
    "KXNFLGAME": {"FOOTBALL"}, "KXNCAAFGAME": {"FOOTBALL"}, "KXCFLGAME": {"FOOTBALL"},
    "KXAFLGAME": {"AUSSIE RULES"},
    "KXATPMATCH": {"TENNIS"}, "KXWTAMATCH": {"TENNIS"}, "KXITFMATCH": {"TENNIS"}, "KXITFWMATCH": {"TENNIS"},
    "KXATPCHALLENGERMATCH": {"TENNIS"}, "KXWTACHALLENGERMATCH": {"TENNIS"},
    "KXT20MATCH": {"CRICKET"}, "KXWT20MATCH": {"CRICKET"}, "KXODIMATCH": {"CRICKET"},
    "KXTESTMATCH": {"CRICKET"}, "KXCOUNTYCHAMPMATCH": {"CRICKET"},
    "KXBOXING": {"BOXING"}, "KXUFCFIGHT": {"MARTIAL ARTS"},
}


def _expected_sports(ticker: str) -> set:
    """Bookmaker sport(s) a Kalshi ticker must match (by its series prefix); empty = not anchored."""
    return SERIES_SPORT.get((ticker or "").split("-")[0].upper(), set())


def _best_book_game(teams: frozenset, book: dict, threshold: int, expected_sports=None):
    """BIPARTITE per-team fuzzy match: each of the two Kalshi teams must match a DISTINCT book team
    (not an aggregate joined-string ratio, which let wrong-league lookalikes through — e.g. a Kalshi MLB
    'New York M vs Philadelphia' matching a different league's 'New York'/'Philadelphia'). LEAGUE-ANCHORED:
    when `expected_sports` is given, only book games in that sport are considered (a Kalshi MLB market can't
    fuzzy-grab a similarly-named cricket/KBO game). Returns (games_in_sport, worse_leg_score) of the best
    team-set whose WORSE leg clears `threshold`, else (None, best_worse)."""
    if fuzz is None:
        return None, 0
    kt = list(teams)
    if len(kt) != 2:
        return None, 0
    best_min, best_sum, best_games = 0, -1, None
    for bset, games in book.items():
        bt = list(bset)
        if len(bt) != 2:
            continue
        cand = [g for g in games if (not expected_sports or g.get("sport", "").upper() in expected_sports)]
        if not cand:
            continue   # no game in the expected sport for this team-set → not a candidate
        # two ways to assign the 2 Kalshi teams to the 2 book teams — take the better assignment,
        # scored by the WORSE of its two legs (so BOTH teams must match), tie-broken by the sum.
        s00, s11 = _team_sim(kt[0], bt[0]), _team_sim(kt[1], bt[1])
        s01, s10 = _team_sim(kt[0], bt[1]), _team_sim(kt[1], bt[0])
        m1, m2 = min(s00, s11), min(s01, s10)
        mn, sm = (m1, s00 + s11) if (m1, s00 + s11) >= (m2, s01 + s10) else (m2, s01 + s10)
        if (mn, sm) > (best_min, best_sum):
            best_min, best_sum, best_games = mn, sm, cand
    return (best_games, best_min) if best_games is not None and best_min >= threshold else (None, best_min)


def _date_close(kalshi_settlement: str, book_start: str, days: int = 1) -> bool:
    """Guard same-players-different-day mispairs. Kalshi '2026-06-18' vs bookmaker '2026061812:50:00';
    allow ±days for venue TZ slop; unparseable → don't block."""
    try:
        kd = datetime.strptime((kalshi_settlement or "")[:10], "%Y-%m-%d").date()
        bd = datetime.strptime((book_start or "")[:8], "%Y%m%d").date()
    except ValueError:
        return True
    return abs((kd - bd).days) <= days


# ── game-time matching (distinguishes a SERIES / doubleheader: same teams, different start) ──
_MON = {"JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
        "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12}


def _et_offset_hours(y: int, m: int, d: int) -> int:
    """US Eastern UTC offset: -4 (EDT) from 2nd Sun Mar to 1st Sun Nov, else -5 (EST). Kalshi ticker times
    are ET; the bookmaker start_time is UTC — so we convert ET→UTC to compare. Dependency-free (no tzdata)."""
    def nth_sun(year: int, month: int, n: int) -> int:
        first_wd = datetime(year, month, 1).weekday()      # Mon=0 … Sun=6
        return (1 + (6 - first_wd) % 7) + (n - 1) * 7
    try:
        day = datetime(y, m, d).date()
        return -4 if datetime(y, 3, nth_sun(y, 3, 2)).date() <= day < datetime(y, 11, nth_sun(y, 11, 1)).date() else -5
    except ValueError:
        return -5


def _book_dt(start: str):
    """Bookmaker start_time 'YYYYMMDDHH:MM:SS' (UTC) → naive UTC datetime; None if unparseable."""
    try:
        return datetime.strptime((start or "")[:8] + (start or "")[8:].replace(":", ""), "%Y%m%d%H%M%S")
    except (ValueError, TypeError):
        return None


def _kalshi_dt(entry: dict):
    """Kalshi game time from the ticker, e.g. KXMLBGAME-26JUN181840NYMPHI → 2026-06-18 18:40 ET → UTC.
    Returns a naive UTC datetime, or None when the ticker has no HHMM (combat/tennis list by date only)."""
    m = re.search(r"-(\d{2})([A-Z]{3})(\d{2})(\d{4})", entry.get("kalshi_ticker", ""))
    if not m or m.group(2) not in _MON:
        return None
    try:
        et = datetime(2000 + int(m.group(1)), _MON[m.group(2)], int(m.group(3)),
                      int(m.group(4)[:2]), int(m.group(4)[2:]))
    except ValueError:
        return None
    return et - timedelta(hours=_et_offset_hours(et.year, et.month, et.day))   # ET → UTC


def fetch_catalog(sidecar: str, timeout: float = 120.0) -> list[dict]:
    # /catalog is NOT a cheap read: it makes the sidecar run GetLeagues + a sequential chunked GetSchedule
    # over every match league through the browser. On a slower/cold server Chrome that easily exceeds 30s,
    # so the default is generous (override with --catalog-timeout / HARDVEN_CATALOG_TIMEOUT).
    with urllib.request.urlopen(f"{sidecar.rstrip('/')}/catalog", timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8")).get("selections", [])


def _league_id(sid: str) -> int:
    """Numeric league id (2nd segment of selection_id), for canonical-league ordering; non-numeric → huge."""
    parts = sid.split(":")
    return int(parts[1]) if len(parts) >= 2 and parts[1].isdigit() else 10 ** 9


def _better_listing(new: dict, prev: dict) -> bool:
    """On a same-teams collision across leagues, prefer the canonical listing: (1) a 3-way moneyline
    (has a draw) over a draw-less one, then (2) the lower (primary) league id. Deterministic — replaces
    the old last-write-wins, which let a duplicate high-id league (e.g. MLB 505) overwrite the real one (MLB 5)."""
    new_rank  = (1 if new["draw"]  else 0, -new["league"])    # higher tuple = more canonical
    prev_rank = (1 if prev["draw"] else 0, -prev["league"])
    return new_rank > prev_rank


def index_catalog(selections: list[dict]) -> dict:
    """{ frozenset({teamA_key, teamB_key}) : {date, three_way, teams:{key: sel_id}, draw: sel_id|None} }.

    Grouped by the bookmaker GAME id (idgm, the first segment of the selection_id) — NOT the event title.
    The same match can appear in several leagues (real matches vs prop/futures leagues), and merging by
    title would mix a pair's legs across different games. Grouping by idgm guarantees a pair's H/V/D all
    come from ONE game; on a team-set collision (same teams listed in two leagues — e.g. the same MLB
    game in leagues 5 and 505) we keep the most canonical listing via _better_listing (draw, then lowest
    league id), NOT whichever was iterated last."""
    games: dict[str, dict] = {}
    for s in selections:
        sid = s.get("selection_id", "")
        idgm = sid.split(":")[0]
        if not idgm:
            continue
        g = games.setdefault(idgm, {"date": (s.get("start_time") or "")[:8],
                                    "start": (s.get("start_time") or ""), "three_way": False,
                                    "teams": {}, "draw": None, "league": _league_id(sid),
                                    "sport": (s.get("sport") or "")})
        if s.get("three_way"):
            g["three_way"] = True
        name = s.get("selection_name", "")
        if sid.rsplit(":", 1)[-1] == "D" or _norm(name) == "draw":
            g["draw"] = sid
        else:
            g["teams"][_book_name(name)] = sid
    # team-set -> [game, ...]: a SERIES/doubleheader (same teams, different start) keeps EVERY game, so the
    # Kalshi market can match the one closest in time. Same-start duplicate LISTINGS (the MLB-5-vs-505 case)
    # still collapse to the canonical one via _better_listing.
    out: dict = {}
    for g in games.values():
        if len(g["teams"]) != 2:
            continue
        lst = out.setdefault(frozenset(g["teams"].keys()), [])
        twin = next((x for x in lst if x["start"] == g["start"]), None)   # same game in another league?
        if twin is None:
            lst.append(g)                            # different start = different game → keep both
        elif _better_listing(g, twin):
            lst[lst.index(twin)] = g                 # same game: keep the canonical (draw, lowest league id)
    return out


def _pick_game(games: list, entry: dict, time_tol_sec: float):
    """From the bookmaker games sharing a team-set, pick the one matching the Kalshi market's date/time.
    With a ticker TIME → the game whose start is closest, within time_tol. Without a time (combat/tennis,
    listed by date) → closest date within ±1 day. None if the nearest is still too far (the Kalshi game
    isn't on the board → avoids the wrong-day mispair)."""
    if not games:
        return None
    if len(games) == 1:
        g = games[0]
        return g if _date_close(entry.get("settlement_date", ""), g.get("start", ""), days=1) else None
    kdt = _kalshi_dt(entry)
    if kdt is not None:
        scored = [(abs((bd - kdt).total_seconds()), g) for g in games if (bd := _book_dt(g.get("start", "")))]
        if scored:
            diff, best = min(scored, key=lambda x: x[0])
            if diff <= time_tol_sec:
                return best
        # no time-close game → fall through to the date-only match below
    sd = entry.get("settlement_date", "")
    cands = [g for g in games if _date_close(sd, g.get("start", ""), days=1)]
    if not cands:
        return None

    def _ddiff(g: dict) -> int:
        try:
            kd = datetime.strptime(sd[:10], "%Y-%m-%d").date()
            return abs((kd - datetime.strptime((g.get("start", "") or "")[:8], "%Y%m%d").date()).days)
        except ValueError:
            return 99
    return min(cands, key=_ddiff)


def _fetch_implied(sidecar: str, tokens: set) -> dict:
    """{selection_id: implied_price} for OPEN book selections, via the sidecar /odds (one batched call)."""
    if not tokens:
        return {}
    try:
        q = urllib.request.quote(",".join(tokens))
        with urllib.request.urlopen(f"{sidecar.rstrip('/')}/odds?selections={q}", timeout=30) as r:
            data = json.loads(r.read().decode("utf-8"))
    except Exception as ex:
        print(f"[PAIR] price-gate: could not fetch /odds ({ex}) — skipping price validation.")
        return {}
    out = {}
    for sid, s in (data.get("selections") or {}).items():
        ip = s.get("implied_price")
        if s.get("status") == "open" and ip:
            out[sid] = float(ip)
    return out


def sibling_validate(pairs: list[dict]) -> tuple:
    """STRUCTURAL GATE — the two Kalshi markets of one event must MIRROR each other's book tokens.

    A 2-way event has two complementary Kalshi markets ("will A win" / "will B win"). Their book tokens are
    therefore necessarily crossed: A's yes-token IS B's no-token. If both rows carry the SAME pair of tokens,
    at least one of them maps Kalshi-YES onto the wrong side — and there is no price on earth that makes that
    consistent, so this needs no /odds call and works where the price gate cannot.

    WHY IT MATTERS (2026-08-12, real money): Dias vs Kawano Cho shipped with both siblings holding
    yes=away / no=home. The bot fired K_YES_P_NO on the Dias market, bought its `no` token — and that token
    WAS Dias. Two bets on the same player, i.e. a DOUBLED directional bet dressed as a hedge. It won, which
    is the only reason it did not cost $22. The price gate could not have saved it: at 0.465 vs 0.50 the
    agree- and invert-distances are equal to two decimal places, so "near a coin-flip the error is benign"
    is simply false — at a coin flip is exactly where an inverted pair is most likely AND most expensive.

    Blanks BOTH siblings rather than guessing which one is wrong: one of them is fine, but nothing here can
    tell which, and shipping a coin-flip guess is what caused this. Returns (checked_events, blanked_pairs).
    """
    by_event: dict[str, list[dict]] = {}
    for e in pairs:
        if e.get("hardven_yes_token") and e.get("hardven_no_token") and e.get("event_id"):
            by_event.setdefault(e["event_id"], []).append(e)

    checked = blanked = 0
    for ev, rows in by_event.items():
        if len(rows) != 2:
            continue                      # 1 side paired = nothing to cross-check; >2 = not a 2-way event
        checked += 1
        a, b = rows
        mirrored = (a["hardven_yes_token"] == b["hardven_no_token"]
                    and a["hardven_no_token"] == b["hardven_yes_token"])
        if mirrored:
            continue
        print(f"[PAIR] SIBLING-REJECT {ev}: {a.get('kalshi_ticker','?')} and {b.get('kalshi_ticker','?')} "
              f"do not mirror (yes={a['hardven_yes_token']} / yes={b['hardven_yes_token']}) — one of them "
              f"maps Kalshi-YES to the wrong side; blanking BOTH")
        for r in (a, b):
            r["hardven_yes_token"] = r["hardven_no_token"] = ""
            r.pop("fuzzy", None)
            r["sibling_rejected"] = True
            blanked += 1
    return checked, blanked


def duplicate_token_validate(pairs: list[dict]) -> tuple:
    """ONE BOOK FIXTURE CANNOT BE TWO KALSHI EVENTS. Blanks the entries that lost the tie.

    Teams play SERIES — the same two sides on consecutive days — and a matcher working from names alone
    happily pairs Friday's Kalshi game to Thursday's book fixture. Both entries then look perfect
    individually and even mirror correctly, because the collision is ACROSS events, not inside one; the
    sibling gate cannot see it. What it produces is a phantom arb priced off two different games, and
    those are not small: 2026-08-13 shipped Kiwoom/KT Wiz at net 0.83 and Hanshin/Hiroshima at 0.9296 —
    the second one clears MIN_PLAUSIBLE_NET and the dry run executed it.

    RESOLVED BY DATE, not blanked wholesale, because the book's own event key carries one
    (`sport:comp:YYYY-MM-DD~id~id:market:sel`). The entry whose Kalshi settlement_date is nearest that
    date keeps the token; the rest are cleared for re-match. A tie, or an unparseable date, blanks all
    claimants — guessing is what created the problem.

    Returns (tokens_examined, entries_blanked)."""
    from datetime import date as _date

    def book_date(tok: str):
        m = re.search(r"(\d{4})-(\d{2})-(\d{2})", tok or "")
        try:
            return _date(int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None
        except ValueError:
            return None

    def kalshi_date(e: dict):
        try:
            y, mth, d = (e.get("settlement_date") or "").split("-")
            return _date(int(y), int(mth), int(d))
        except (ValueError, AttributeError):
            return None

    by_token: dict[str, list[dict]] = {}
    for e in pairs:
        t = e.get("hardven_yes_token")
        if t and e.get("hardven_no_token"):
            by_token.setdefault(t, []).append(e)

    examined = blanked = 0
    for tok, rows in by_token.items():
        events = {r.get("event_id") for r in rows}
        if len(events) < 2:
            continue                       # the two SIBLINGS of one event share tokens by design
        examined += 1
        bd = book_date(tok)
        scored = []
        for r in rows:
            kd = kalshi_date(r)
            scored.append((abs((kd - bd).days) if (bd and kd) else None, r))
        gaps = [g for g, _ in scored if g is not None]
        winner = None
        if bd is not None and gaps and gaps.count(min(gaps)) == 1:
            winner = min(scored, key=lambda x: (x[0] is None, x[0]))[1]
        for gap, r in scored:
            if r is winner:
                continue
            print(f"[PAIR] DUPLICATE-TOKEN {r.get('kalshi_ticker','?')}: book fixture {tok[-34:]} is already "
                  f"claimed by {sorted(events - {r.get('event_id')})} "
                  f"({'date gap ' + str(gap) + 'd' if gap is not None else 'no usable date'}) — blanking")
            r["hardven_yes_token"] = r["hardven_no_token"] = ""
            r.pop("fuzzy", None)
            r["duplicate_token_rejected"] = True
            blanked += 1
    return examined, blanked


def price_validate(pairs: list[dict], sidecar: str, tol: float) -> tuple:
    """PRICE-CONSISTENCY GATE. For each filled pair, the Kalshi-YES probability (kalshi_yes_price, from
    pairHard) and the bookmaker yes-token's implied prob must AGREE on the favorite — both are P(the same
    team). If they're COMPLEMENTS (book ≈ 1 − kalshi) the two sides are inverted → swap the tokens. If
    NEITHER orientation fits within `tol`, the prices are unrelated → almost certainly the wrong game →
    blank the pair. This catches the mispairs team-matching can't: inverted sides + wrong-game. (Near a
    coin-flip the agree/invert distinction is ambiguous but the error is benign; the gate bites on lopsided
    games, which is where inversions create the fat phantom 'arbs'.) Returns (consistent, inverted, rejected,
    unvalidated).

    BLIND AT A COIN FLIP, and that is NOT harmless. When kalshi≈0.5 the agree- and invert-distances are
    equal, so this gate picks essentially at random — and picking wrong pairs Kalshi-YES with the book's
    SAME side, which the executor then buys twice instead of hedging. That is a doubled directional bet, not
    a wash: it loses the whole stake on both venues rather than netting out (real case 2026-08-12, Dias vs
    Kawano Cho at 0.465/0.50). `sibling_validate` above is the check that actually covers this, because it
    is structural rather than numeric. Run it FIRST."""
    tokens = {e[k] for e in pairs for k in ("hardven_yes_token", "hardven_no_token") if e.get(k)}
    implied = _fetch_implied(sidecar, tokens)
    consistent = inverted = rejected = unvalidated = 0
    for e in pairs:
        yt, nt = e.get("hardven_yes_token"), e.get("hardven_no_token")
        ky = e.get("kalshi_yes_price")
        if not (yt and nt):
            continue
        by = implied.get(yt)
        if ky is None or by is None:
            unvalidated += 1
            continue
        d_agree, d_invert = abs(ky - by), abs(ky - (1.0 - by))
        tk = e.get("kalshi_ticker", "?")
        if e.get("three_way"):
            # 3-way: only the agree orientation is valid (no complement). Reject a gross mismatch.
            if d_agree > tol:
                print(f"[PAIR] PRICE-REJECT (3way) {tk}  kalshi_yes={ky:.2f} book_yes={by:.2f}  Δ{d_agree:.2f}>{tol}")
                e["hardven_yes_token"] = e["hardven_no_token"] = ""
                e.pop("fuzzy", None)
                rejected += 1
            else:
                consistent += 1
            continue
        if min(d_agree, d_invert) > tol:
            print(f"[PAIR] PRICE-REJECT {tk}  kalshi_yes={ky:.2f} book_yes={by:.2f}  "
                  f"(agree Δ{d_agree:.2f} / invert Δ{d_invert:.2f} both >{tol}) — likely WRONG GAME")
            e["hardven_yes_token"] = e["hardven_no_token"] = ""
            e.pop("fuzzy", None)
            rejected += 1
        elif d_invert < d_agree:
            e["hardven_yes_token"], e["hardven_no_token"] = nt, yt   # sides were inverted → swap
            e["price_inverted_fixed"] = True
            print(f"[PAIR] INVERTED-FIXED {tk}  swapped sides (kalshi_yes={ky:.2f}, book_yes {by:.2f}→{1-by:.2f})")
            inverted += 1
        else:
            consistent += 1
    return consistent, inverted, rejected, unvalidated


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sidecar", default=os.environ.get("HARDVEN_SIDECAR_URL", "http://127.0.0.1:8787"))
    ap.add_argument("--pairs", default=str(Path(__file__).resolve().parent.parent / "cross_pairs.json"))
    ap.add_argument("--write", action="store_true", help="write the file (default = dry-run preview)")
    ap.add_argument("--both", action="store_true", help="also fill the 2-way mirror market (default: one)")
    ap.add_argument("--fuzzy", action="store_true",
                    help="enable fuzzy name-variant matching (auto-fills team-name variants; flags pairs 'fuzzy':true)")
    ap.add_argument("--fuzzy-threshold", type=int, default=85,
                    help="min per-team match score (0-100) — BOTH teams must clear it (default 85)")
    ap.add_argument("--no-price-gate", action="store_true",
                    help="skip the price-consistency gate (rejects wrong-game pairs + fixes inverted sides)")
    ap.add_argument("--price-tol", type=float, default=0.25,
                    help="price-gate tolerance 0-1: book vs Kalshi win-prob must agree within this (default 0.25)")
    ap.add_argument("--time-tol-hours", type=float, default=3.0,
                    help="for a series/doubleheader, the book game's start must be within this many hours of "
                         "the Kalshi ticker time (default 3)")
    ap.add_argument("--no-league-anchor", action="store_true",
                    help="skip the league/sport anchor (match across all sports — disable if a sport's label "
                         "is over-rejecting valid pairs)")
    ap.add_argument("--catalog-timeout", type=float,
                    default=float(os.environ.get("HARDVEN_CATALOG_TIMEOUT", "120")),
                    help="seconds to wait for /catalog — raise on a slow/cold server Chrome (default 120)")
    args = ap.parse_args()

    book = index_catalog(fetch_catalog(args.sidecar, args.catalog_timeout))
    print(f"[PAIR] {sum(len(v) for v in book.values())} bookmaker games "
          f"({len(book)} matchups) in /catalog")
    if args.fuzzy and fuzz is None:
        print("[PAIR] --fuzzy requested but rapidfuzz isn't installed:  pip install rapidfuzz")

    pairs = json.loads(Path(args.pairs).read_text(encoding="utf-8"))
    filled = already = skipped_dupe = fuzzy_n = anchor_rejected = 0
    unmatched: list[str] = []
    done_events: set[str] = set()   # for 2-way mirror dedupe (per Kalshi event_id)

    for e in pairs:
        tk = e.get("kalshi_ticker", "?")
        if e.get("hardven_yes_token") and e.get("hardven_no_token"):
            already += 1
            continue
        key = kalshi_key(e)
        if not key:
            unmatched.append(f"{tk} (couldn't parse Kalshi outcome)")
            continue
        yes_outcome, teams, is_tie = key
        expected = set() if args.no_league_anchor else _expected_sports(tk)   # league anchor by series
        raw = book.get(teams)                        # now a LIST (a series keeps every game)
        games = [g for g in raw if g.get("sport", "").upper() in expected] if (raw and expected) else raw
        is_fuzzy, fscore = False, 0
        if not games and args.fuzzy:
            games, fscore = _best_book_game(teams, book, args.fuzzy_threshold, expected)
            is_fuzzy = bool(games)
        if not games:
            if raw and expected:                     # teams matched but no game in the expected sport
                anchor_rejected += 1
                unmatched.append(f"{tk}  {sorted(teams)} (teams matched but no "
                                 f"{'/'.join(sorted(expected))} game — league-anchored out)")
            else:
                extra = f" (best fuzzy {fscore:.0f})" if (args.fuzzy and fuzz is not None) else ""
                unmatched.append(f"{tk}  {sorted(teams)}{extra}")
            continue
        # pick the game whose start matches the Kalshi ticker time (series/doubleheader) — date fallback inside
        entry = _pick_game(games, e, args.time_tol_hours * 3600)
        if entry is None:
            unmatched.append(f"{tk}  {sorted(teams)} (no book game near {e.get('settlement_date')}"
                             f"/ticker-time among {len(games)} candidate(s))")
            continue
        # 2-way markets are mirrors → fill one per event; 3-way outcomes are DISTINCT → fill all.
        if not entry["three_way"] and not args.both and e.get("event_id") in done_events:
            skipped_dupe += 1
            continue

        if is_tie:
            yes_tok = entry["draw"]
            no_tok = next(iter(entry["teams"].values()))   # any team (plumbing; YES-direction disabled)
            if not yes_tok:
                unmatched.append(f"{tk} (Tie, but no book draw selection)")
                continue
        else:
            yes_key = _pick_book_team(yes_outcome, entry["teams"].keys())
            if not yes_key:
                unmatched.append(f"{tk} (outcome '{yes_outcome}' not resolvable to a book team)")
                continue
            yes_tok = entry["teams"][yes_key]
            no_tok = entry["teams"][next(k for k in entry["teams"] if k != yes_key)]

        e["hardven_yes_token"], e["hardven_no_token"] = yes_tok, no_tok
        if entry["three_way"]:
            e["three_way"] = True   # NO-only hedge (Kalshi NO + book back-this-outcome)
        if is_fuzzy:
            e["fuzzy"] = True        # matched by fuzzy name variant — verify before M1 (back-test gates real money)
            fuzzy_n += 1
        done_events.add(e.get("event_id"))
        filled += 1
        tag = ("  [3-way NO-only]" if entry["three_way"] else "") + (f"  [FUZZY {fscore:.0f}]" if is_fuzzy else "")
        print(f"[PAIR] {tk:<34} YES={yes_outcome:<16} → {yes_tok} | NO → {no_tok}{tag}")

    print(f"\n[PAIR] filled={filled} (fuzzy={fuzzy_n})  already={already}  "
          f"skipped_mirror={skipped_dupe}  league-anchored-out={anchor_rejected}  unmatched={len(unmatched)}")
    for u in unmatched:
        print(f"   UNMATCHED: {u}")

    # ── price-consistency gate: reject wrong-game pairs + fix inverted sides by live prices ──
    gate = (0, 0, 0, 0)
    if not args.no_price_gate:
        # STRUCTURAL FIRST: mirroring needs no prices, so it still bites when the book is unreachable or
        # the market sits at a coin flip — precisely where price_validate is blind.
        sib_checked, sib_blanked = sibling_validate(pairs)
        if sib_blanked:
            print(f"[PAIR] sibling gate: {sib_blanked} pair(s) blanked across {sib_checked} 2-way event(s) "
                  f"— tokens did not mirror, so one side mapped Kalshi-YES backwards")
        elif sib_checked:
            print(f"[PAIR] sibling gate: {sib_checked} 2-way event(s) all mirror correctly")
        gate = price_validate(pairs, args.sidecar, args.price_tol)
        if any(gate):
            print(f"[PAIR] price-gate (tol={args.price_tol}): {gate[0]} consistent | {gate[1]} inverted-fixed | "
                  f"{gate[2]} wrong-game rejected | {gate[3]} unvalidated (no price)")
        # FINAL INTEGRITY CHECK — the price gate can UNDO the sibling gate. An INVERTED-FIXED swaps the
        # tokens on ONE entry, but a 2-way event's two entries are mirrors: invert one and not the other
        # and they end up IDENTICAL, which is the exact corruption the sibling gate exists to remove.
        # Observed 2026-08-13: 4 INVERTED-FIXED swaps left 3 broken sibling pairs in the written file.
        # Re-checking here is cheap and makes the written file's invariant unconditional.
        dup_c, dup_b = duplicate_token_validate(pairs)
        if dup_b:
            print(f'[PAIR] duplicate-token gate: {dup_b} entry(s) blanked across {dup_c} book fixture(s) claimed by more than one Kalshi event')
        post_c, post_b = sibling_validate(pairs)
        if post_b:
            print(f"[PAIR] post-price sibling re-check: {post_b} pair(s) blanked — an inverted-fixed swap "
                  f"had broken their mirror")

    valid = sum(1 for e in pairs if e.get("hardven_yes_token") and e.get("hardven_no_token"))
    if args.write and (filled or gate[1] or gate[2]):
        Path(args.pairs).write_text(json.dumps(pairs, indent=2), encoding="utf-8")
        print(f"\n[PAIR] wrote {valid} filled pair(s) → {args.pairs}")
    elif not args.write:
        print("\n[PAIR] dry-run (no file written). Re-run with --write to save.")


if __name__ == "__main__":
    main()
