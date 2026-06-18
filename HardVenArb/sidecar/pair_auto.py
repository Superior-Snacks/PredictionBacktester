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
from datetime import datetime
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
    """'Jordan vs Argentina' / 'Hijikata vs Lehecka: Round Of 16' → frozenset({'jordan','argentina'})."""
    parts = re.split(r"\bvs\.?\b", title or "", flags=re.IGNORECASE)
    if len(parts) != 2:
        return None
    a = _norm(parts[0].split(":")[0])
    b = _norm(parts[1].split(":")[0])   # strip a trailing ": Round Of 16" / ": Quarterfinal"
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


def _pick_book_team(outcome: str, team_keys):
    """Which book team key IS the Kalshi outcome — exact/substring → token-overlap → fuzzy. Safe even on
    fuzzy (it's a choice between the 2 teams of an already-matched game): 'rinky hijikata'→'hijikata',
    'boston'→'boston red sox', 'congo dr'→'dr congo', 'ryszard lewicki'→'ryszard lewicky'."""
    keys = list(team_keys)
    for k in keys:
        if k == outcome or k in outcome or outcome in k:
            return k
    ot = set(outcome.split())
    for k in keys:
        if ot & set(k.split()):
            return k
    if fuzz is not None and keys:
        score, best = max((fuzz.token_set_ratio(outcome, k), k) for k in keys)
        if score >= 60:
            return best
    return None


def _best_book_game(teams: frozenset, book: dict, threshold: int):
    """Fuzzy-match a Kalshi team-set to a bookmaker game (token_set_ratio of the joined names).
    Returns (book_entry, score), or (None, best_score) if nothing clears `threshold`."""
    if fuzz is None:
        return None, 0
    kjoin = " ".join(sorted(teams))
    best_score, best_key = 0, None
    for bk in book:
        s = fuzz.token_set_ratio(kjoin, " ".join(sorted(bk)))
        if s > best_score:
            best_score, best_key = s, bk
    return (book[best_key], best_score) if best_key is not None and best_score >= threshold else (None, best_score)


def _date_close(kalshi_settlement: str, book_start: str, days: int = 1) -> bool:
    """Guard same-players-different-day mispairs. Kalshi '2026-06-18' vs bookmaker '2026061812:50:00';
    allow ±days for venue TZ slop; unparseable → don't block."""
    try:
        kd = datetime.strptime((kalshi_settlement or "")[:10], "%Y-%m-%d").date()
        bd = datetime.strptime((book_start or "")[:8], "%Y%m%d").date()
    except ValueError:
        return True
    return abs((kd - bd).days) <= days


def fetch_catalog(sidecar: str) -> list[dict]:
    with urllib.request.urlopen(f"{sidecar.rstrip('/')}/catalog", timeout=30) as r:
        return json.loads(r.read().decode("utf-8")).get("selections", [])


def index_catalog(selections: list[dict]) -> dict:
    """{ frozenset({teamA_key, teamB_key}) : {date, three_way, teams:{key: sel_id}, draw: sel_id|None} }.

    Grouped by the bookmaker GAME id (idgm, the first segment of the selection_id) — NOT the event title.
    The same match can appear in several leagues (real matches vs prop/futures leagues), and merging by
    title would mix a pair's legs across different games. Grouping by idgm guarantees a pair's H/V/D all
    come from ONE game; on a team-set collision (same teams in two leagues) we keep the game that has a
    draw (the real moneyline) over one that doesn't (a prop)."""
    games: dict[str, dict] = {}
    for s in selections:
        sid = s.get("selection_id", "")
        idgm = sid.split(":")[0]
        if not idgm:
            continue
        g = games.setdefault(idgm, {"date": (s.get("start_time") or "")[:8], "three_way": False,
                                    "teams": {}, "draw": None})
        if s.get("three_way"):
            g["three_way"] = True
        name = s.get("selection_name", "")
        if sid.rsplit(":", 1)[-1] == "D" or _norm(name) == "draw":
            g["draw"] = sid
        else:
            g["teams"][_book_name(name)] = sid
    out: dict = {}
    for g in games.values():
        if len(g["teams"]) != 2:
            continue
        key = frozenset(g["teams"].keys())
        if key in out and out[key]["draw"] and not g["draw"]:
            continue  # keep the existing match (has draw) over this draw-less prop game
        out[key] = g
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sidecar", default=os.environ.get("HARDVEN_SIDECAR_URL", "http://127.0.0.1:8787"))
    ap.add_argument("--pairs", default=str(Path(__file__).resolve().parent.parent / "cross_pairs.json"))
    ap.add_argument("--write", action="store_true", help="write the file (default = dry-run preview)")
    ap.add_argument("--both", action="store_true", help="also fill the 2-way mirror market (default: one)")
    ap.add_argument("--fuzzy", action="store_true",
                    help="enable fuzzy name-variant matching (auto-fills team-name variants; flags pairs 'fuzzy':true)")
    ap.add_argument("--fuzzy-threshold", type=int, default=85,
                    help="min token_set_ratio (0-100) for a fuzzy GAME match (default 85)")
    args = ap.parse_args()

    book = index_catalog(fetch_catalog(args.sidecar))
    print(f"[PAIR] {len(book)} bookmaker games in /catalog")
    if args.fuzzy and fuzz is None:
        print("[PAIR] --fuzzy requested but rapidfuzz isn't installed:  pip install rapidfuzz")

    pairs = json.loads(Path(args.pairs).read_text(encoding="utf-8"))
    filled = already = skipped_dupe = fuzzy_n = 0
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
        entry = book.get(teams)
        is_fuzzy, fscore = False, 0
        if entry is None and args.fuzzy:
            entry, fscore = _best_book_game(teams, book, args.fuzzy_threshold)
            is_fuzzy = entry is not None
        if entry is None:
            extra = f" (best fuzzy {fscore:.0f})" if (args.fuzzy and fuzz is not None) else ""
            unmatched.append(f"{tk}  {sorted(teams)}{extra}")
            continue
        if not _date_close(e.get("settlement_date", ""), entry["date"]):
            unmatched.append(f"{tk}  {sorted(teams)} (date mismatch {e.get('settlement_date')} vs {entry['date']})")
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
          f"skipped_mirror={skipped_dupe}  unmatched={len(unmatched)}")
    for u in unmatched:
        print(f"   UNMATCHED: {u}")

    if args.write and filled:
        Path(args.pairs).write_text(json.dumps(pairs, indent=2), encoding="utf-8")
        print(f"\n[PAIR] wrote {filled} filled pair(s) → {args.pairs}")
    elif not args.write:
        print("\n[PAIR] dry-run (no file written). Re-run with --write to save.")


if __name__ == "__main__":
    main()
