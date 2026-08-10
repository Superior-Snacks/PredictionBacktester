"""
pair_betinasia.py -- fill Kalshi-seeded cross_pairs entries with BetInAsia selection ids.

Same job as pair_pinnacle.py, different venue, and simpler in one important way: BetInAsia's event key
is `YYYY-MM-DD,<p1_id>,<p2_id>`, so once a match is named ONCE the pairing can be cached by PLAYER ID
rather than re-matched by string every run. Names drift ("Thiago Agustin Tirante" vs "T. Tirante");
integer ids do not. The cache lives in `bia_player_ids.json` next to the pairs file.

NO GUEST API. Every BetInAsia read is authed and the transport is the passive browser observer, so the
sidecar must already be up on the sport page before this runs:

    # terminal 1 -- sidecar with the BIA adapter (deep-links /sportsbook/tennis, subscribes all 75)
    HARDVEN_BOOK=betinasia python -m uvicorn app:app --port 8787
    # wait for the catalog push + subscriptions to settle (~20-30s), then:
    python pair_betinasia.py --sport tennis            # dry-run preview
    python pair_betinasia.py --sport tennis --write

WHY A SEPARATE PAIRS FILE. Token formats differ per venue (`221310:1633549397:home` for Pinnacle vs
`tennis:338:2026-08-09,...:tennis_match,all:p1` here) and a pairs file is read by exactly one book at a
time, so writing BIA tokens into cross_pairs.json would silently destroy the Pinnacle pairing. Default
target is cross_pairs_bia.json; point --pairs at whatever you want filled.

THE SEED. Like pair_pinnacle, this FILLS entries that already carry a kalshi_ticker; it does not invent
them. Seed the file from the Kalshi side first (pair_auto / the existing tennis seeder), or copy
cross_pairs.json and blank the hardven_* fields with --reseed-from.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

from pair_auto import fetch_catalog, price_validate, fuzz
from env_util import atomic_write_json
from betinasia_adapter import MONEYLINE_BY_SPORT, is_three_way

CACHE_NAME = "bia_player_ids.json"


# ── name handling ─────────────────────────────────────────────────────────────
def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
    s = re.sub(r"[^a-z0-9 ]", " ", s.lower())
    return " ".join(s.split())


def _surname(name: str) -> str:
    """Last token of a normalised name. Kalshi writes 'Tallon Griekspoor', tournament feeds sometimes
    write 'T. Griekspoor' or reorder to 'Griekspoor Tallon'; the surname survives all three."""
    parts = _norm(name).split()
    return parts[-1] if parts else ""


SAME_SURNAME_DIFFERENT_PERSON = 50.0   # deliberately below any sane --threshold


def _given(name: str) -> list[str]:
    """Given names = everything before the surname. 'Thiago Agustin Tirante' -> ['thiago','agustin']."""
    return _norm(name).split()[:-1]


def _tok_compatible(x: str, y: str) -> bool:
    """Same name token, allowing an INITIAL to stand for the name it abbreviates."""
    return x == y or (len(x) == 1 and y.startswith(x)) or (len(y) == 1 and x.startswith(y))


def _given_compatible(ga: list[str], gb: list[str]) -> bool:
    """Do two given-name lists describe the same person?

    ORDER-INDEPENDENT on purpose. Comparing positionally looks right and is wrong, because Spanish and
    Italian COMPOUND SURNAMES break the alignment: `_surname()` keeps only the final token, so Kalshi's
    "Jorda Sanchis" becomes given=['jorda'] while the venue's "David Jorda Sanchis" becomes
    given=['david','jorda']. Left-aligned that compares 'jorda' to 'david' and rejects a real player;
    right-aligned it would instead break 'T. Tirante' vs 'Thiago Agustin Tirante'. Requiring every
    token of the SHORTER list to find a partner anywhere in the longer one handles both, and still
    rejects siblings -- 'alexander' simply has no partner in ['mischa'].

    Found by the data: this rule change recovered Jorda Sanchis, Alcala Gurri and D'Agostino, which a
    first cut at the sibling fix had wrongly thrown away.
    """
    if not ga or not gb:
        return True                       # one side is surname-only: nothing to contradict
    short, long = (ga, gb) if len(ga) <= len(gb) else (gb, ga)
    return all(any(_tok_compatible(x, y) for y in long) for x in short)


def _name_score(a: str, b: str) -> float:
    """0-100. Exact normalised equality, then surname equality GATED ON THE GIVEN NAME, then fuzzy.

    The gate is the whole point. Surname equality alone scored 95 and cleared any sane threshold, so
    'Alexander Zverev' matched 'Mischa Zverev' and 'Andy Murray' matched 'Jamie Murray' -- different
    people, and tennis is full of them (siblings, and common surnames all over the Challenger tour).
    A pair built that way is not a near miss: the two legs back opposite players, so the "hedge" is a
    doubled directional bet that loses on both branches. It has to fail closed.

    Initials still work, because that is the real-world variation we must survive: 'T. Tirante' vs
    'Thiago Agustin Tirante' is the SAME player and still scores 95.
    """
    na, nb = _norm(a), _norm(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 100.0
    sa, sb = _surname(a), _surname(b)
    if sa and sa == sb:
        ga, gb = _given(a), _given(b)
        if not _given_compatible(ga, gb):
            return SAME_SURNAME_DIFFERENT_PERSON
        return 95.0 if (ga and gb) else 90.0     # surname-only side is plausible but weaker
    if fuzz is None:
        return 0.0
    # Different surnames: never let fuzzy alone carry a pair to a passing score. Two unrelated players
    # can share a first name and token_sort_ratio rewards that far too generously.
    return min(float(fuzz.token_sort_ratio(na, nb)), 85.0)


# ── selection ids ─────────────────────────────────────────────────────────────
def _parse_sid(sid: str):
    p = sid.split(":")
    return tuple(p) if len(p) == 5 else None


def _event_players(event_key: str) -> tuple[str, str]:
    """`2026-08-09,10047664,90384` -> ('10047664', '90384'). ('','') for outrights/odd shapes."""
    parts = (event_key or "").split(",")
    if len(parts) == 3 and parts[1] != "multirunner":
        return parts[1], parts[2]
    return "", ""


def _event_date(event_key: str) -> str:
    return (event_key or "").split(",")[0]


def _date_close(kalshi_settlement: str, bia_start: str, days: int = 1) -> bool:
    """Guard same-players-different-day mispairs. Unparseable -> don't block."""
    try:
        kd = datetime.strptime((kalshi_settlement or "")[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return True
    try:
        bd = datetime.fromisoformat(str(bia_start).replace("Z", "+00:00")).date()
    except (ValueError, TypeError):
        try:
            bd = datetime.strptime(str(bia_start)[:10], "%Y-%m-%d").date()
        except (ValueError, TypeError):
            return True
    return abs((kd - bd).days) <= days


# ── catalog indexing ──────────────────────────────────────────────────────────
def index_catalog(selections: list[dict], sport: str) -> dict:
    """event_key -> {'sport','league','start','players':{sel_token: (name, selection_id)}}"""
    games: dict[str, dict] = {}
    for s in selections:
        if s.get("sport") != sport:
            continue
        p = _parse_sid(s.get("selection_id", ""))
        if not p:
            continue
        _sp, _comp, ekey, market_key, sel = p
        if market_key != MONEYLINE_BY_SPORT.get(sport):
            continue                       # moneyline only; derivatives are telemetry-only
        g = games.setdefault(ekey, {"sport": sport, "league": s.get("league") or "",
                                    "start": s.get("start_time") or "",
                                    "three_way": bool(s.get("three_way")), "players": {}})
        g["players"][sel] = (s.get("selection_name") or "", s.get("selection_id"))
    return games


def _match_game(entry: dict, games: dict, cache: dict, threshold: float):
    """Find the BIA game for a Kalshi entry. Returns (event_key, game, score, how) or None.

    Player-ID cache first (durable), then names. The cache is only trusted when BOTH sides of the tie
    resolve to the same event -- a half-hit means a player moved on to a different match, which is
    exactly the same-players-different-day error the date guard also covers.
    """
    title = entry.get("event_title") or entry.get("label") or ""
    yes_name = entry.get("kalshi_outcome") or ""
    # Kalshi tennis titles read "Griekspoor vs Merida"; take both sides.
    sides = [s.strip() for s in re.split(r"\bvs\.?\b", title, flags=re.I) if s.strip()]
    if len(sides) != 2:
        sides = [yes_name, ""]

    # 1) cached player ids
    ids = {cache.get(_norm(s)) for s in sides if _norm(s) in cache}
    ids.discard(None)
    if len(ids) == 2:
        for ekey, g in games.items():
            a, b = _event_players(ekey)
            if a and {a, b} == ids and _date_close(entry.get("settlement_date"), g["start"]):
                return ekey, g, 100.0, "id-cache"

    # 2) names
    best = None
    for ekey, g in games.items():
        names = [n for n, _sid in g["players"].values()]
        if len(names) < 2:
            continue
        if not _date_close(entry.get("settlement_date"), g["start"]):
            continue
        # score both orientations of the two-name tie; take the better pairing
        s1 = min(_name_score(sides[0], names[0]), _name_score(sides[1], names[1]))
        s2 = min(_name_score(sides[0], names[1]), _name_score(sides[1], names[0]))
        sc = max(s1, s2)
        if sc >= threshold and (best is None or sc > best[2]):
            best = (ekey, g, sc, "name")
    return best


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sidecar", default=os.environ.get("HARDVEN_SIDECAR_URL", "http://127.0.0.1:8787"))
    ap.add_argument("--pairs", default=str(Path(__file__).resolve().parent.parent / "cross_pairs_bia.json"))
    ap.add_argument("--sport", default="tennis")
    ap.add_argument("--write", action="store_true", help="write the file (default = dry-run preview)")
    ap.add_argument("--threshold", type=float, default=90.0,
                    help="min per-player name score 0-100; BOTH players must clear it (default 90)")
    ap.add_argument("--no-price-gate", action="store_true",
                    help="skip the price-consistency gate (rejects wrong-game pairs + fixes inverted sides)")
    ap.add_argument("--price-tol", type=float, default=0.25)
    ap.add_argument("--catalog-timeout", type=float,
                    default=float(os.environ.get("HARDVEN_CATALOG_TIMEOUT", "60")))
    ap.add_argument("--reseed-from", default="",
                    help="copy Kalshi fields from this pairs file, blanking hardven_* (bootstrap)")
    args = ap.parse_args()

    if fuzz is None:
        print("[PAIR-BIA] WARNING: rapidfuzz missing - only exact/surname matches will work. "
              "pip install rapidfuzz")

    cat = fetch_catalog(args.sidecar, args.catalog_timeout)
    games = index_catalog(cat, args.sport)
    print(f"[PAIR-BIA] {len(cat)} catalog selections -> {len(games)} {args.sport} moneyline game(s)")
    if not games:
        print("[PAIR-BIA] nothing to pair. Is the sidecar up with HARDVEN_BOOK=betinasia, and has the "
              "page had ~30s to subscribe? (catalog is pushed; PRICES need the sport page open)")
        return

    pairs_path = Path(args.pairs)
    if args.reseed_from:
        src = json.loads(Path(args.reseed_from).read_text(encoding="utf-8"))
        pairs = []
        for e in src:
            n = {k: v for k, v in e.items() if not k.startswith("hardven_")}
            n.pop("three_way", None)
            n.pop("fuzzy", None)
            pairs.append(n)
        print(f"[PAIR-BIA] reseeded {len(pairs)} Kalshi entries from {args.reseed_from}")
    elif pairs_path.exists():
        pairs = json.loads(pairs_path.read_text(encoding="utf-8"))
    else:
        print(f"[PAIR-BIA] {pairs_path} does not exist. Seed it from the Kalshi side, or bootstrap "
              f"with --reseed-from cross_pairs.json")
        return

    cache_path = pairs_path.parent / CACHE_NAME
    cache = json.loads(cache_path.read_text(encoding="utf-8")) if cache_path.exists() else {}

    filled = already = unmatched = via_cache = 0
    misses: list[str] = []
    used: set[str] = set()

    for e in pairs:
        tk = e.get("kalshi_ticker", "?")
        if e.get("hardven_yes_token") and e.get("hardven_no_token"):
            already += 1
            continue
        hit = _match_game(e, games, cache, args.threshold)
        if not hit:
            unmatched += 1
            misses.append(f"{tk}  {e.get('event_title') or e.get('label','')[:60]}")
            continue
        ekey, g, score, how = hit
        if ekey in used:
            # two Kalshi tickers are the two SIDES of one match; the mirror is handled by yes/no below
            pass
        used.add(ekey)

        # Orient: which BIA selection is the Kalshi YES outcome?
        yes_name = e.get("kalshi_outcome") or ""
        scored = sorted(((_name_score(yes_name, nm), tok, sid)
                         for tok, (nm, sid) in g["players"].items()), reverse=True)
        if len(scored) < 2 or scored[0][0] < args.threshold:
            unmatched += 1
            misses.append(f"{tk}  matched game but no side scored >= {args.threshold} for "
                          f"'{yes_name}'")
            continue
        e["hardven_yes_token"] = scored[0][2]
        e["hardven_no_token"] = scored[1][2]
        e["hardven_start_time"] = g["start"]
        e["hardven_league"] = g["league"]
        if g["three_way"]:
            e["three_way"] = True
        if score < 100.0:
            e["fuzzy"] = True

        # cache both players by name so the next run is id-based
        a, b = _event_players(ekey)
        if a and b:
            toks = sorted(g["players"].keys())            # p1, p2
            for tok, pid in zip(toks, (a, b)):
                nm = g["players"].get(tok, ("", ""))[0]
                if nm:
                    cache[_norm(nm)] = pid
        filled += 1
        if how == "id-cache":
            via_cache += 1
        tag = "  [id-cache]" if how == "id-cache" else (f"  [name {score:.0f}]" if score < 100 else "")
        print(f"[PAIR-BIA] {tk:<38} YES={scored[0][1]} -> {scored[0][2].split(':')[-1]}{tag}")

    print(f"\n[PAIR-BIA] filled={filled} (via id-cache={via_cache})  already={already}  "
          f"unmatched={unmatched}")
    for m in misses[:20]:
        print(f"   UNMATCHED: {m}")

    gate = (0, 0, 0, 0)
    if not args.no_price_gate:
        gate = price_validate(pairs, args.sidecar, args.price_tol)
        if any(gate):
            print(f"[PAIR-BIA] price-gate (tol={args.price_tol}): {gate[0]} consistent | "
                  f"{gate[1]} inverted-fixed | {gate[2]} wrong-game rejected | {gate[3]} unvalidated")

    valid = sum(1 for e in pairs if e.get("hardven_yes_token") and e.get("hardven_no_token"))
    if args.write and (filled or gate[1] or gate[2]):
        atomic_write_json(str(pairs_path), pairs)      # atomic: C# hot-reload never sees a partial file
        cache_path.write_text(json.dumps(cache, indent=1, sort_keys=True), encoding="utf-8")
        print(f"\n[PAIR-BIA] wrote {valid} filled pair(s) -> {pairs_path}")
        print(f"[PAIR-BIA] player-id cache: {len(cache)} name(s) -> {cache_path.name}")
    elif not args.write:
        print("\n[PAIR-BIA] dry-run (no file written). Re-run with --write to save.")


if __name__ == "__main__":
    main()
