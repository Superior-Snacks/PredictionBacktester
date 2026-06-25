"""
pair_pinnacle.py — auto-fill the Pinnacle side of cross_pairs.json against the Kalshi scaffolds.

Same job as pair_auto.py (the bookmaker matcher), but Pinnacle's /catalog differs in three ways the
matching has to handle — everything else is imported from pair_auto so the Kalshi-side parsing, the
league/sport anchor, the doubleheader time-match and the price gate stay byte-identical (a pairHard
scaffold change then can't drift between the two venues):

  1. selection_id = "{leagueId}:{matchupId}:{designation}" — the GAME id is the 2nd segment (bookmaker
     put it 1st), and the side is a semantic "home"/"away"/"draw" (bookmaker used "H"/"V"/"D").
  2. team names are FULL ("Kansas City Royals"); Kalshi uses the CITY ("Kansas City"). The match is
     therefore by CONTAINMENT (Kalshi ⊆ Pinnacle) — which _team_sim already scores 100, so it's a clean,
     deterministic match (NOT flagged "fuzzy"). Only sub-100 spelling/word-order variants need --fuzzy.
  3. start_time is ISO-8601 UTC ("2026-06-25T16:10:00Z"), not "YYYYMMDDHH:MM:SS".

  python pair_pinnacle.py                  # dry-run preview (clean containment matches)
  python pair_pinnacle.py --write          # write cross_pairs.json
  python pair_pinnacle.py --fuzzy --write  # also accept sub-100 name variants (tagged "fuzzy":true)

Needs rapidfuzz for the containment matcher (pip install rapidfuzz); without it only EXACT team-set
matches work, and a Kalshi city never exactly equals a Pinnacle full name → nothing fills.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")  # Windows console: tolerate accented names

# Venue-independent pieces — the Kalshi-side parsing, generic name matching, league anchor, price gate.
# Reused verbatim so the two matchers can't diverge on the Kalshi shapes pairHard produces.
from pair_auto import (
    _norm, _book_name, kalshi_key, _pick_book_team, _best_book_game, _better_listing,
    _expected_sports, _kalshi_dt, fetch_catalog, price_validate, fuzz,
)


# ── Pinnacle selection_id = "{leagueId}:{matchupId}:{designation}" ──────────────
def _game_id(sid: str) -> str:
    """matchupId — the GAME (2nd segment). Bookmaker put the game id first; Pinnacle puts league first."""
    p = sid.split(":")
    return p[1] if len(p) >= 3 else ""


def _designation(sid: str) -> str:
    """home / away / draw (3rd segment)."""
    p = sid.split(":")
    return p[2] if len(p) >= 3 else ""


def _league_id(sid: str) -> int:
    """Numeric league id (1st segment), for canonical-listing ordering; non-numeric → huge."""
    p = sid.split(":")
    return int(p[0]) if p and p[0].isdigit() else 10 ** 9


# Kalshi abbreviates a few MLB teams to forms that AREN'T a substring of the Pinnacle full name, so plain
# containment (Kalshi ⊆ Pinnacle) misses them. The Athletics are the notorious case — no city in the name,
# so Kalshi "A's" → norm "a s", which is not in "athletics"/"oakland athletics". Canonicalize the Kalshi
# side to a token Pinnacle's name still CONTAINS ("athletics"), so containment then scores 100. Add entries
# here as new abbreviation mismatches surface (the price gate / a manual check is the backstop).
_TEAM_ALIASES = {"a s": "athletics", "as": "athletics", "oakland": "athletics", "sacramento": "athletics"}


def _canon(key: str) -> str:
    return _TEAM_ALIASES.get(key, key)


# ── ISO-8601 UTC start_time (vs bookmaker's "YYYYMMDDHH:MM:SS") ─────────────────
def _pin_dt(start: str):
    """Pinnacle start_time '2026-06-25T16:10:00Z' → naive UTC datetime; None if unparseable."""
    if not start:
        return None
    try:
        dt = datetime.fromisoformat(start.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.astimezone(timezone.utc).replace(tzinfo=None) if dt.tzinfo else dt


def _date_close(kalshi_settlement: str, pin_start: str, days: int = 1) -> bool:
    """Guard same-teams-different-day mispairs (Kalshi '2026-06-25' vs Pinnacle ISO start); allow ±days for
    venue TZ slop; unparseable → don't block. ISO-aware mirror of pair_auto._date_close."""
    try:
        kd = datetime.strptime((kalshi_settlement or "")[:10], "%Y-%m-%d").date()
    except ValueError:
        return True
    pd = _pin_dt(pin_start)
    return True if pd is None else abs((kd - pd.date()).days) <= days


def index_catalog(selections: list[dict]) -> dict:
    """{ frozenset({teamA_key, teamB_key}) : [game, ...] } grouped by Pinnacle matchupId (2nd id segment).

    A team-set keeps EVERY game (an MLB doubleheader = same teams, two start times) so _pick_game can pick
    the one closest to the Kalshi ticker time. A same-start duplicate listing collapses to the canonical one
    via _better_listing. The soccer draw leg is detected by the 'draw' designation."""
    games: dict[str, dict] = {}
    for s in selections:
        sid = s.get("selection_id", "")
        gid = _game_id(sid)
        if not gid:
            continue
        g = games.setdefault(gid, {"start": (s.get("start_time") or ""), "three_way": False,
                                   "teams": {}, "draw": None, "league": _league_id(sid),
                                   "sport": (s.get("sport") or "")})
        if s.get("three_way"):
            g["three_way"] = True
        name = s.get("selection_name", "")
        if _designation(sid) == "draw" or _norm(name) == "draw":
            g["draw"] = sid
        else:
            g["teams"][_book_name(name)] = sid
    out: dict = {}
    for g in games.values():
        if len(g["teams"]) != 2:
            continue
        lst = out.setdefault(frozenset(g["teams"].keys()), [])
        twin = next((x for x in lst if x["start"] == g["start"]), None)   # same game listed twice?
        if twin is None:
            lst.append(g)                            # different start = different game → keep both
        elif _better_listing(g, twin):
            lst[lst.index(twin)] = g
    return out


def _pick_game(games: list, entry: dict, time_tol_sec: float):
    """From the Pinnacle games sharing a team-set, pick the one matching the Kalshi market's date/time.

    When the Kalshi ticker carries a game TIME (all baseball / most team sports), that time is AUTHORITATIVE:
    take the closest start, but ONLY if within time_tol — else None. This rejects the NEXT-DAY game in a daily
    series (same teams ~24h apart): Pinnacle lists only the near game, and the ±1-day date guard alone would
    wrongly let tomorrow's Kalshi ticker grab today's Pinnacle game (the KBO/NPB June-27→June-26 mispair).
    Doubleheaders still work — two same-day starts, the ticker time picks the right one. Tickers with NO time
    (combat/tennis, listed by date only) fall back to the closest date within ±1 day."""
    if not games:
        return None
    kdt = _kalshi_dt(entry)
    if kdt is not None:
        scored = [(abs((pd - kdt).total_seconds()), g) for g in games if (pd := _pin_dt(g.get("start", "")))]
        if not scored:
            return None
        diff, best = min(scored, key=lambda x: x[0])
        return best if diff <= time_tol_sec else None
    sd = entry.get("settlement_date", "")
    cands = [g for g in games if _date_close(sd, g.get("start", ""), days=1)]
    if not cands:
        return None

    def _ddiff(g: dict) -> int:
        pd = _pin_dt(g.get("start", ""))
        try:
            kd = datetime.strptime(sd[:10], "%Y-%m-%d").date()
        except ValueError:
            return 99
        return abs((kd - pd.date()).days) if pd else 99
    return min(cands, key=_ddiff)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sidecar", default=os.environ.get("HARDVEN_SIDECAR_URL", "http://127.0.0.1:8787"))
    ap.add_argument("--pairs", default=str(Path(__file__).resolve().parent.parent / "cross_pairs.json"))
    ap.add_argument("--write", action="store_true", help="write the file (default = dry-run preview)")
    ap.add_argument("--both", action="store_true", help="also fill the 2-way mirror market (default: one)")
    ap.add_argument("--fuzzy", action="store_true",
                    help="also accept sub-100 spelling/word-order variants (clean containment matches don't "
                         "need it); fuzzy-matched pairs are tagged 'fuzzy':true for re-verify before M1")
    ap.add_argument("--fuzzy-threshold", type=int, default=85,
                    help="min per-team score (0-100) for a --fuzzy match — BOTH teams must clear it (default 85)")
    ap.add_argument("--no-price-gate", action="store_true",
                    help="skip the price-consistency gate (rejects wrong-game pairs + fixes inverted sides)")
    ap.add_argument("--price-tol", type=float, default=0.25,
                    help="price-gate tolerance 0-1: Pinnacle vs Kalshi win-prob must agree within this (default 0.25)")
    ap.add_argument("--time-tol-hours", type=float, default=3.0,
                    help="doubleheader: the Pinnacle game's start must be within this many hours of the Kalshi "
                         "ticker time (default 3)")
    ap.add_argument("--no-league-anchor", action="store_true",
                    help="skip the league/sport anchor (match across all sports)")
    ap.add_argument("--catalog-timeout", type=float,
                    default=float(os.environ.get("HARDVEN_CATALOG_TIMEOUT", "60")),
                    help="seconds to wait for /catalog (default 60)")
    args = ap.parse_args()

    if fuzz is None:
        print("[PAIR] WARNING: rapidfuzz not installed — only EXACT team-set matches will work, and a Kalshi "
              "city ('Kansas City') never equals a Pinnacle full name ('Kansas City Royals'). "
              "Install it:  pip install rapidfuzz")

    book = index_catalog(fetch_catalog(args.sidecar, args.catalog_timeout))
    print(f"[PAIR] {sum(len(v) for v in book.values())} Pinnacle games ({len(book)} matchups) in /catalog")

    pairs = json.loads(Path(args.pairs).read_text(encoding="utf-8"))
    filled = already = skipped_dupe = fuzzy_n = anchor_rejected = 0
    unmatched: list[str] = []
    done_events: set[str] = set()   # 2-way mirror dedupe (per Kalshi event_id)

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
        teams = frozenset(_canon(t) for t in teams)   # fold Kalshi team abbreviations (A's → athletics)
        yes_outcome = _canon(yes_outcome)
        expected = set() if args.no_league_anchor else _expected_sports(tk)   # league anchor by series

        # MATCH: exact team-set first (rare — Kalshi city ≠ Pinnacle full name), then the bipartite matcher.
        # A clean containment (Kalshi ⊆ Pinnacle) scores 100 → treated as exact (no fuzzy tag); only sub-100
        # spelling/word-order variants need --fuzzy and get tagged 'fuzzy':true.
        raw = book.get(teams)
        games = [g for g in raw if g.get("sport", "").upper() in expected] if (raw and expected) else raw
        is_fuzzy, fscore = False, 100
        if not games:
            thr = args.fuzzy_threshold if args.fuzzy else 100   # without --fuzzy, accept only clean containment
            cand, fscore = _best_book_game(teams, book, thr, expected)
            if cand:
                games, is_fuzzy = cand, fscore < 100
        if not games:
            if raw and expected:                     # teams matched but no game in the expected sport
                anchor_rejected += 1
                unmatched.append(f"{tk}  {sorted(teams)} (teams matched but no "
                                 f"{'/'.join(sorted(expected))} game — league-anchored out)")
            elif fscore >= args.fuzzy_threshold:
                unmatched.append(f"{tk}  {sorted(teams)} (best leg {fscore:.0f}; pass --fuzzy to accept)")
            else:
                # below the fuzzy threshold = no real counterpart → almost always a game not on Pinnacle's
                # board yet (Pinnacle lists ~today's slate; future-dated Kalshi games fill on a later re-run).
                unmatched.append(f"{tk}  {sorted(teams)} (best leg {fscore:.0f} — not on Pinnacle's board)")
            continue
        # pick the game whose start matches the Kalshi ticker time (doubleheader) — date fallback inside
        entry = _pick_game(games, e, args.time_tol_hours * 3600)
        if entry is None:
            unmatched.append(f"{tk}  {sorted(teams)} (no Pinnacle game near {e.get('settlement_date')}"
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
                unmatched.append(f"{tk} (Tie, but no Pinnacle draw selection)")
                continue
        else:
            yes_key = _pick_book_team(yes_outcome, entry["teams"].keys())
            if not yes_key:
                unmatched.append(f"{tk} (outcome '{yes_outcome}' not resolvable to a Pinnacle team)")
                continue
            yes_tok = entry["teams"][yes_key]
            no_tok = entry["teams"][next(k for k in entry["teams"] if k != yes_key)]

        e["hardven_yes_token"], e["hardven_no_token"] = yes_tok, no_tok
        if entry["three_way"]:
            e["three_way"] = True   # NO-only hedge (Kalshi NO + Pinnacle back-this-outcome)
        if is_fuzzy:
            e["fuzzy"] = True        # sub-100 name variant — verify before M1 (the back-test gates real money)
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
        gate = price_validate(pairs, args.sidecar, args.price_tol)
        if any(gate):
            print(f"[PAIR] price-gate (tol={args.price_tol}): {gate[0]} consistent | {gate[1]} inverted-fixed | "
                  f"{gate[2]} wrong-game rejected | {gate[3]} unvalidated (no price)")

    valid = sum(1 for e in pairs if e.get("hardven_yes_token") and e.get("hardven_no_token"))
    if args.write and (filled or gate[1] or gate[2]):
        Path(args.pairs).write_text(json.dumps(pairs, indent=2), encoding="utf-8")
        print(f"\n[PAIR] wrote {valid} filled pair(s) → {args.pairs}")
    elif not args.write:
        print("\n[PAIR] dry-run (no file written). Re-run with --write to save.")


if __name__ == "__main__":
    main()
