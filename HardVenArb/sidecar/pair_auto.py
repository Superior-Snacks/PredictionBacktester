"""
pair_auto.py — auto-fill the bookmaker side of cross_pairs.json by matching players to the Kalshi scaffolds.

Workflow:
  1. pairHard.py scaffolds cross_pairs.json with the Kalshi side filled + hardven_*_token BLANK.
  2. The sidecar runs with the bookmaker adapter (HARDVEN_BOOK=bookmaker) and exposes /catalog
     (every tennis game's "<idgm>:<idlg>:H/V" selections + player names, via GetSchedule).
  3. This script GETs /catalog, matches each Kalshi entry to a bookmaker game by PLAYER SURNAME SET,
     maps the Kalshi YES player → the bookmaker :H/:V that IS that player, and writes the tokens.

Matching is by the set of two surnames (order-independent, accent/case-insensitive), which is robust for
tennis (unique players per day). A wrong pairing is real money later, so review the printed report;
nothing is written without --write, and unmatched entries are left blank (the bot skips blanks).

  python pair_auto.py                 # dry-run: print what it WOULD fill
  python pair_auto.py --write         # actually write cross_pairs.json (one pair per match)
  python pair_auto.py --write --both  # fill BOTH mirror markets per match (telemetry only; M1 = double bet)
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

sys.stdout.reconfigure(encoding="utf-8")  # Windows console: tolerate accented player names


def _norm(s: str) -> str:
    """Lowercase, strip accents/punctuation, collapse spaces — for surname comparison."""
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
    s = re.sub(r"[^a-z0-9 ]", " ", s.lower())
    return " ".join(s.split())


def _book_surname(selection_name: str) -> str:
    """Bookmaker selection 'Hijikata, Rinky' → 'hijikata' (the part before the comma)."""
    return _norm(selection_name.split(",")[0])


def _date_close(kalshi_settlement: str, book_start: str, days: int = 1) -> bool:
    """Guard against same-players-different-day mispairs. Kalshi settlement '2026-06-18' vs bookmaker
    start '2026061812:50:00'. Allow ±`days` for venue timezone slop; if either is unparseable, don't block."""
    try:
        kd = datetime.strptime((kalshi_settlement or "")[:10], "%Y-%m-%d").date()
        bd = datetime.strptime((book_start or "")[:8], "%Y%m%d").date()
    except ValueError:
        return True
    return abs((kd - bd).days) <= days


def _kalshi_matchup(label: str):
    """Kalshi label 'Will X win the A vs B: Round...' → (yes_surname, {A_surname, B_surname}) or None."""
    mu = re.search(r"\bthe (.+?) vs (.+?)\s*[:?]", label)
    yes = re.search(r"\bWill (.+?) win\b", label)
    if not mu or not yes:
        return None
    a, b = _norm(mu.group(1)), _norm(mu.group(2))            # matchup surnames (Kalshi uses surnames)
    yes_full = _norm(yes.group(1))                            # YES player full name, e.g. 'rinky hijikata'
    # which matchup surname is the YES player? (surname is a token-subset of the full name)
    yes_sn = a if a and a in yes_full else (b if b and b in yes_full else None)
    if yes_sn is None:
        return None
    return yes_sn, frozenset({a, b})


def fetch_catalog(sidecar: str) -> list[dict]:
    with urllib.request.urlopen(f"{sidecar.rstrip('/')}/catalog", timeout=30) as r:
        return json.loads(r.read().decode("utf-8")).get("selections", [])


def index_catalog(selections: list[dict]) -> dict:
    """{ frozenset({surnameA, surnameB}) : {"date": "YYYYMMDD", "sels": {surname: selection_id}} }."""
    by_event: dict[str, dict] = {}
    for s in selections:
        ev = by_event.setdefault(s.get("event", ""), {"date": (s.get("start_time") or "")[:8], "sels": {}})
        ev["sels"][_book_surname(s.get("selection_name", ""))] = s.get("selection_id")
    return {frozenset(ev["sels"].keys()): ev for ev in by_event.values() if len(ev["sels"]) == 2}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sidecar", default=os.environ.get("HARDVEN_SIDECAR_URL", "http://127.0.0.1:8787"))
    ap.add_argument("--pairs", default=str(Path(__file__).resolve().parent.parent / "cross_pairs.json"))
    ap.add_argument("--write", action="store_true", help="write the file (default = dry-run preview)")
    ap.add_argument("--both", action="store_true", help="fill both mirror markets per match (default: one)")
    args = ap.parse_args()

    book = index_catalog(fetch_catalog(args.sidecar))
    print(f"[PAIR] {len(book)} bookmaker matches in /catalog")

    pairs = json.loads(Path(args.pairs).read_text(encoding="utf-8"))
    filled, skipped_dupe, unmatched, already = 0, 0, [], 0
    done_events: set[str] = set()

    for e in pairs:
        if e.get("hardven_yes_token") and e.get("hardven_no_token"):
            already += 1
            continue
        parsed = _kalshi_matchup(e.get("label", ""))
        if not parsed:
            unmatched.append(e.get("kalshi_ticker", "?") + " (couldn't parse label)")
            continue
        yes_sn, surnames = parsed
        entry = book.get(surnames)
        if not entry:
            unmatched.append(f"{e.get('kalshi_ticker','?')}  {sorted(surnames)}")
            continue
        if not _date_close(e.get("settlement_date", ""), entry["date"]):
            unmatched.append(f"{e.get('kalshi_ticker','?')}  {sorted(surnames)} (date mismatch "
                             f"{e.get('settlement_date')} vs {entry['date']} — same players, different day?)")
            continue
        if not args.both and e.get("event_id") in done_events:
            skipped_dupe += 1
            continue
        sns = entry["sels"]
        no_sn = next(s for s in surnames if s != yes_sn)
        yes_tok, no_tok = sns.get(yes_sn), sns.get(no_sn)
        if not yes_tok or not no_tok:
            unmatched.append(f"{e.get('kalshi_ticker','?')} (surname not in book selections)")
            continue
        e["hardven_yes_token"], e["hardven_no_token"] = yes_tok, no_tok
        done_events.add(e.get("event_id"))
        filled += 1
        print(f"[PAIR] {e.get('kalshi_ticker'):<32} YES={yes_sn:<14} → {yes_tok} | NO → {no_tok}")

    print(f"\n[PAIR] filled={filled}  already={already}  skipped_mirror={skipped_dupe}  unmatched={len(unmatched)}")
    for u in unmatched:
        print(f"   UNMATCHED: {u}")

    if args.write and filled:
        Path(args.pairs).write_text(json.dumps(pairs, indent=2), encoding="utf-8")
        print(f"\n[PAIR] wrote {filled} filled pair(s) → {args.pairs}")
    elif not args.write:
        print("\n[PAIR] dry-run (no file written). Re-run with --write to save.")


if __name__ == "__main__":
    main()
