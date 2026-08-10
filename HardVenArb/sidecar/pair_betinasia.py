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
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

from pair_auto import fetch_catalog, price_validate, fuzz
from env_util import atomic_write_json
from betinasia_adapter import MONEYLINE_BY_SPORT, is_three_way, parse_selection_id

CACHE_NAME = "bia_player_ids.json"

# 3-WAY MARKETS. Soccer's `wdw` carries THREE selections and the venue orders them h, d, a — so the
# draw sits BETWEEN the two teams. Taking names[0]/names[1] therefore scored the away team against the
# literal string 'd' and matched almost nothing (42 of 540 on the first soccer dry-run). Kalshi splits
# the same event into three markets whose outcome is a team name or "Tie"/"Draw".
DRAW_TOKENS = {"d", "draw", "x"}
DRAW_WORDS = {"tie", "draw"}


def _is_draw_outcome(name: str) -> bool:
    return _norm(name) in DRAW_WORDS


def _team_names(game: dict) -> list[str]:
    """The two TEAM names of a game, draw excluded — what a head-to-head tie must be matched on."""
    return [nm for tok, (nm, _s) in game["players"].items() if tok not in DRAW_TOKENS]


# ── name handling ─────────────────────────────────────────────────────────────
# Letters that NFKD does NOT decompose. `ö` -> o+diaeresis folds fine, but these are DISTINCT letters
# with no ASCII base, so `.encode("ascii","ignore")` DELETES them: "Kasımpaşa" -> "kasmpasa" (vs Kalshi's
# "kasimpasa"), "Nordsjælland" -> "nordsjlland", "Bodø" -> "bod". Every one is a real European club and
# every one scored 85 — just under the threshold. Transliterate before folding.
_TRANSLIT = str.maketrans({
    "ı": "i", "İ": "i", "ø": "o", "Ø": "o", "æ": "ae", "Æ": "ae", "œ": "oe", "Œ": "oe",
    "ß": "ss", "đ": "d", "Đ": "d", "ð": "d", "Ð": "d", "ł": "l", "Ł": "l",
    "þ": "th", "Þ": "th", "ħ": "h", "ŋ": "n", "ʼ": "'", "’": "'",
})


def _norm(s: str) -> str:
    # BOTH VENUES ANNOTATE NAMES, and both annotations break matching in different ways:
    #   Kalshi     "Cezar Cretu (b. 2001)"      disambiguates same-named players
    #   BetInAsia  "Ekaterina Alexandrova [f]"  status/seeding marker
    # Left in, the tokens ('b','2001','f') are name tokens the other side never has. The square-bracket
    # case is the nastier one: 'f' becomes the LAST token, so `_surname()` returns "f" and the whole
    # surname gate compares the wrong thing -- Alexandrova vs Svitolina was a real pair lost this way,
    # and it is the only Pinnacle-paired tie that BIA missed for a fixable reason.
    s = re.sub(r"[\(\[][^)\]]*[\)\]]", " ", s or "")
    s = s.translate(_TRANSLIT)
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
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


# A shorter token is accepted as a PREFIX of a longer one only from this length. Club names vary by
# stem and suffix across sources -- "Karlsruhe"/"Karlsruher SC" (German adjectival), "Corum"/"Corumspor"
# (Turkish -spor) -- and both scored just under the bar. 5 is deliberately conservative: it excludes the
# short generic words that would over-match ("real", "inter", "sport" as a bare word, "san", "new"), and
# a loose token still cannot pair a tie on its own because BOTH sides must clear the threshold.
_PREFIX_MIN = 5

# ...and ONLY for team sports. The rule is safe for clubs and dangerous for people: "Juan Martin" vs
# "Juan Martinez" scored 95 under a blanket prefix rule -- different players, a wrong-side pair, exactly
# the failure the sibling gate exists to stop. Club names inflect ("Karlsruher"), surnames do not.
# Enabled per run from --sport; tennis keeps exact-surname matching and is unchanged.
STEM_MATCHING = False


def _tok_compatible(x: str, y: str) -> bool:
    """Same name token, allowing an INITIAL to stand for the name it abbreviates, and a long-enough
    stem to stand for its inflected/suffixed form."""
    if x == y:
        return True
    if (len(x) == 1 and y.startswith(x)) or (len(y) == 1 and x.startswith(y)):
        return True
    if not STEM_MATCHING:
        return False
    short, long = (x, y) if len(x) <= len(y) else (y, x)
    return len(short) >= _PREFIX_MIN and long.startswith(short)


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

    # TOKEN SUBSET is the primary test, not surname equality. Surnames are the wrong anchor because
    # `_surname()` takes the LAST token, and Spanish/Latin American players carry a double surname that
    # only one venue spells out:
    #     Kalshi "Murkel Dellien"          -> surname 'dellien'
    #     BetInAsia "Murkel Alejandro Dellien Velasco" -> surname 'velasco'
    # Same player, "different" surnames, rejected. Found by reading the unpaired lists by hand rather
    # than trusting the score — it cost the real Dellien/Galan and Merida/Tien ties.
    #
    # Subset handles that, and every case the surname rule handled, in one idea: every token of the
    # SHORTER name must appear in the longer one (initials count). Middle names, dropped second
    # surnames and abbreviations are all just "the longer name has extra tokens".
    # Siblings still fail, which is the property that must not regress: {alexander, zverev} is not a
    # subset of {mischa, zverev} because 'alexander' has no partner.
    ta, tb = na.split(), nb.split()
    short, long = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
    if all(any(_tok_compatible(x, y) for y in long) for x in short):
        # A single shared token is much weaker evidence than two, so score it at the threshold rather
        # than above it: "Griekspoor" vs "Tallon Griekspoor" is fine, but one common token should not
        # outvote a conflicting given name elsewhere.
        return 95.0 if len(short) >= 2 else 90.0
    if _surname(a) and _surname(a) == _surname(b):
        return SAME_SURNAME_DIFFERENT_PERSON     # same surname, conflicting given name = not this person
    if fuzz is None:
        return 0.0
    # Different surnames: never let fuzzy alone carry a pair to a passing score. Two unrelated players
    # can share a first name and token_sort_ratio rewards that far too generously.
    return min(float(fuzz.token_sort_ratio(na, nb)), 85.0)


# ── selection ids ─────────────────────────────────────────────────────────────
def _parse_sid(sid: str):
    """Delegate to the adapter's parser. It DECODES the comma-substitution, so the market key compared
    below is the real `tennis_match,all` and not the wire form -- splitting here by hand silently
    stopped matching the moment ids became comma-free."""
    return parse_selection_id(sid)


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
        names = _team_names(g)
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
    # BIA_SIDECAR_URL, NOT HARDVEN_SIDECAR_URL. This script pairs BetInAsia, and HARDVEN_SIDECAR_URL is
    # whatever the shell last pointed at -- in a two-venue setup that is usually the PINNACLE sidecar on
    # 8787. Defaulting to it silently paired against the wrong venue's catalog (206 tennis selections,
    # 0 football) and reported "nothing to pair" as if the feed were empty.
    ap.add_argument("--sidecar",
                    default=os.environ.get("BIA_SIDECAR_URL", "http://127.0.0.1:8788"))
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
    ap.add_argument("--sync-seeds", default="",
                    help="MERGE new Kalshi tickers from this file, keeping pairs already filled. This is "
                         "the repeat-run form: --reseed-from blanks everything and re-matches from "
                         "scratch, which drops a working pair whenever the venue catalog is momentarily "
                         "missing that game.")
    args = ap.parse_args()

    # Team sports get stem matching; player sports do not (see STEM_MATCHING).
    global STEM_MATCHING
    STEM_MATCHING = args.sport not in ("tennis", "boxing", "mma", "darts", "snooker")
    if STEM_MATCHING:
        print(f"[PAIR-BIA] stem matching ON for '{args.sport}' (club names inflect: "
              f"Karlsruhe/Karlsruher, Corum/Corumspor)")

    if fuzz is None:
        print("[PAIR-BIA] WARNING: rapidfuzz missing - only exact/surname matches will work. "
              "pip install rapidfuzz")

    # Refuse to pair against another book's catalog. Here it produced 0 matches and a confusing
    # "nothing to pair", but a venue whose ids happened to half-match would write GARBAGE TOKENS into
    # the pairs file, and nothing downstream could tell.
    try:
        with urllib.request.urlopen(f"{args.sidecar.rstrip('/')}/health", timeout=15) as r:
            book = (json.loads(r.read().decode()) or {}).get("book")
    except Exception as e:
        print(f"[PAIR-BIA] cannot reach the sidecar at {args.sidecar} ({type(e).__name__}: {e})")
        return
    if book != "betinasia":
        print(f"[PAIR-BIA] REFUSING: {args.sidecar} is serving book '{book}', not 'betinasia'. "
              f"Point --sidecar (or BIA_SIDECAR_URL) at the BetInAsia sidecar.")
        return

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
        if args.sync_seeds:
            # Kalshi lists tennis close to the event while BetInAsia lists a day ahead, so the pairable
            # set is the moving INTERSECTION of the two. A one-shot pair catches only whatever overlaps
            # at that instant (22 of 23 of BIA's same-day slate paired, but only 3 of 68 for tomorrow --
            # Kalshi had not posted those yet). Merging new tickers each cycle is what turns that into
            # full coverage over the day. Pinnacle's scheduler already keeps the scaffold fresh; we read
            # it, never write it.
            try:
                src = json.loads(Path(args.sync_seeds).read_text(encoding="utf-8"))
            except Exception as e:
                print(f"[PAIR-BIA] --sync-seeds unreadable ({type(e).__name__}: {e}) - continuing with "
                      f"the existing file")
                src = []
            have = {e.get("kalshi_ticker") for e in pairs}
            added = 0
            for e in src:
                tk = e.get("kalshi_ticker")
                if not tk or tk in have:
                    continue
                n = {k: v for k, v in e.items() if not k.startswith("hardven_")}
                n.pop("three_way", None)
                n.pop("fuzzy", None)
                pairs.append(n)
                have.add(tk)
                added += 1
            print(f"[PAIR-BIA] synced {added} new Kalshi ticker(s) from {Path(args.sync_seeds).name} "
                  f"({len(pairs)} total, existing fills kept)")
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
        if _is_draw_outcome(yes_name):
            # Kalshi's "Tie" market <-> the venue's draw selection. Scoring it by NAME would compare
            # "Tie" against two team names and pick whichever fuzzed highest -- a wrong-side pair.
            draw = [(tok, sid) for tok, (nm, sid) in g["players"].items() if tok in DRAW_TOKENS]
            if not draw:
                unmatched += 1
                misses.append(f"{tk}  Kalshi TIE market but the venue has no draw selection")
                continue
            others = [(tok, sid) for tok, (nm, sid) in g["players"].items() if tok not in DRAW_TOKENS]
            scored = [(100.0, draw[0][0], draw[0][1])] + [(0.0, t, s2) for t, s2 in others]
        else:
            scored = sorted(((_name_score(yes_name, nm), tok, sid)
                             for tok, (nm, sid) in g["players"].items()
                             if tok not in DRAW_TOKENS), reverse=True)
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
        # A gate that validates NOTHING is not a gate. price_validate needs `kalshi_yes_price` on each
        # entry (written by the Kalshi-side scaffolder); when it is null the pair is counted
        # "unvalidated" and passes through unchecked. Say so out loud rather than let a comforting
        # line of output imply the inversion/wrong-game checks ran.
        if gate[3] and not (gate[0] or gate[1] or gate[2]):
            missing = sum(1 for e in pairs
                          if e.get("hardven_yes_token") and e.get("kalshi_yes_price") is None)
            print(f"[PAIR-BIA] WARNING price-gate DID NOT RUN: all {gate[3]} filled pair(s) are "
                  f"unvalidated ({missing} have kalshi_yes_price=null). Inverted sides and wrong-game "
                  f"pairs are NOT being caught. Re-scaffold the Kalshi side so the field is populated.")

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
