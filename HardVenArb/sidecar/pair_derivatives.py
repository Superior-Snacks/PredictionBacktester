#!/usr/bin/env python3
"""
pair_derivatives.py — pair Kalshi SPREAD / TOTAL markets to Pinnacle handicap / over-under.

STANDALONE + ACCOUNT-FREE: reads Kalshi's public API and Pinnacle's GUEST API (public key, no session, no
sidecar, no login) and writes derivative_pairs.json. Companion to pair_pinnacle.py (which pairs the
MONEYLINE). Game-matching (team-set + date) reuses pair_auto / pair_pinnacle so the two can't drift.

WHAT MAPS TO WHAT (decoded from both live APIs, 2026-06-27):
  Kalshi TOTAL  (KXMLBTOTAL / KXKBOTOTAL / KXATPGTOTAL): one market per line, floor_strike = L,
                YES = "Over L", NO = Under.  <->  Pinnacle `total`, points = L, designation over/under.
                MATCH: same game AND L == points.   yes = :total:L:over    no = :total:L:under
  Kalshi SPREAD (KXMLBSPREAD / KXKBOSPREAD / KXATPGSPREAD): one market per team+line, floor_strike = L,
                YES = "Team T wins by over L" = T at -L.  <->  Pinnacle `spread`, T's side has points -L
                (opponent +L).  MATCH: same game AND T's side carries (-L).
                yes = :spread:-L:{sideT}    no = :spread:+L:{otherSide}

TOKEN FORMAT (extends the moneyline "{lid}:{matchupId}:{designation}"):
  "{leagueId}:{matchupId}:{type}:{signedPoints}:{side}"  e.g. "246:1632046290:total:7.5:over",
  "246:1632046290:spread:-1.5:home". matchupId = the matchup that CARRIES the derivative (tennis: the
  "(Games)" child; baseball: the main game). Resolvable later by the odds path (that matchup's {type}
  market, the price whose designation == {side} and points == {signedPoints}).

Coverage (verified live 2026-08-31): tennis = ATP games spread+total, WTA games total (no WTA spread,
no ITF markets at all — and ITF is the biggest moneyline source, so derivatives skew to ATP/WTA);
baseball = MLB + KBO (NPB none). The dormant KXATPGAMESPREAD/KXATPGAMETOTAL duplicates are NOT used.

SAFEGUARDS, mirrored from the moneyline pairer. An inverted derivative is WORSE than an inverted
moneyline: backing Over when you meant Under, or the wrong player's handicap, both look entirely
plausible by name, and no name check can see it. So every pair carries `kalshi_yes_price` and goes
through a REJECT-ONLY price gate: live book prices drop wrong-game pairs, but orientation is never
swapped (see the gate itself for why a swap is always wrong here). Fuzzy-matched games are tagged
`fuzzy` for re-verification before real money.

  python pair_derivatives.py            # dry-run preview
  python pair_derivatives.py --write    # write derivative_pairs.json
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import sys
import time
from pathlib import Path

import httpx
import requests

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from pair_auto import _norm, _book_name, _team_sim, _kalshi_dt, fuzz, _fetch_implied  # proven primitives
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))       # pairHard lives beside the sidecar dir
from pairHard import _yes_price                                       # noqa: E402  the SAME yes-price the                                                                      # moneyline gate uses
from env_util import atomic_write_json
from pair_pinnacle import _canon, _pin_dt                              # baseball aliases + ISO start parse
import sports as sports_cfg                                           # unified sport catalog (spread/total series)

KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"
GUEST_BASE = os.environ.get("PINNACLE_GUEST_BASE", "https://guest.api.arcadia.pinnacle.com/0.1")
GUEST_KEY = os.environ.get("PINNACLE_API_KEY", "CmX2KcMrXuFmNg6YFbmTxE0y9CIrOi0R")
HERE = Path(__file__).resolve().parent
OUT = HERE.parent / "derivative_pairs.json"

# Kalshi spread/total series -> ("spread"|"total", Pinnacle sport id), from the unified catalog (sports.py /
# HARDVEN_SPORTS). Baseball=3, Tennis=33.
KALSHI_SERIES = sports_cfg.derivative_series()
SPORT_IDS = sorted({sid for _, sid in KALSHI_SERIES.values()})


def _strip_units(name: str) -> str:
    """'Berrettini (Games)' -> 'Berrettini'; baseball names unchanged."""
    return (name or "").split("(")[0].strip()


def _fmt(p: float) -> str:
    """Clean points for the token: -1.5 / 1.5 / 8.5 (no trailing zeros)."""
    return f"{p:g}"


# ── Pinnacle side (GUEST API): {frozenset(teamKeys): [game,...]} carrying spread/total lines ─────────────
def _guest(client: httpx.Client, path: str):
    try:
        r = client.get(GUEST_BASE + path)
    except Exception as ex:
        print(f"[GUEST] {path} error: {type(ex).__name__}: {ex}")
        return None
    if r.status_code != 200:
        print(f"[GUEST] {path} HTTP {r.status_code}")
        return None
    try:
        return r.json()
    except Exception:
        return None


def build_pinnacle_index(jitter: float = 0.2) -> dict:
    """Enumerate Pinnacle derivative markets for the baseball + tennis leagues. Returns
    {frozenset({home_key, away_key}): [game, ...]} where a game = matchup that CARRIES a spread/total:
       {mid, lid, start, home_name, away_name, home_key, away_key, sport,
        totals:set(points), spreads:{'home':set(points), 'away':set(points)}}."""
    client = httpx.Client(headers={"accept": "application/json", "x-api-key": GUEST_KEY,
                                   "origin": "https://www.pinnacle.bet", "referer": "https://www.pinnacle.bet/",
                                   "user-agent": "Mozilla/5.0"}, timeout=20.0, follow_redirects=True)
    league_ids: list[tuple[str, str]] = []     # (leagueId, sportName)
    for sid in SPORT_IDS:
        for lg in (_guest(client, f"/sports/{sid}/leagues") or []):
            if (lg.get("matchupCount") or 0) > 0 and "doubles" not in (lg.get("name", "") or "").lower():
                league_ids.append((str(lg.get("id")), "tennis" if sid == 33 else "baseball"))
            time.sleep(0)
    index: dict = {}
    n_games = 0
    for lid, sport in league_ids:
        matchups = _guest(client, f"/leagues/{lid}/matchups") or []
        straight = _guest(client, f"/leagues/{lid}/markets/straight") or []
        # gather spread/total lines per matchupId (full-game period 0)
        deriv: dict = {}
        for mk in straight:
            if mk.get("period") != 0:
                continue
            t, mid = mk.get("type"), mk.get("matchupId")
            if t not in ("spread", "total") or mid is None:
                continue
            d = deriv.setdefault(mid, {"totals": set(), "spreads": {"home": set(), "away": set()}})
            for pr in mk.get("prices") or []:
                desig, pts = pr.get("designation"), pr.get("points")
                if pts is None:
                    continue
                if t == "total" and desig in ("over", "under"):
                    d["totals"].add(float(pts))
                elif t == "spread" and desig in ("home", "away"):
                    d["spreads"][desig].add(float(pts))
        # join matchup names onto the derivative lines (only matchups that actually carry a spread/total)
        for m in matchups:
            mid = m.get("id")
            if mid not in deriv:
                continue
            parts = m.get("participants") or []
            if len(parts) < 2 or any("/" in (p.get("name") or "") for p in parts):
                continue   # need 2 sides; skip doubles ("A / B")
            home = _strip_units(next((p.get("name", "") for p in parts if p.get("alignment") == "home"), ""))
            away = _strip_units(next((p.get("name", "") for p in parts if p.get("alignment") == "away"), ""))
            if not home or not away:
                continue
            hk, ak = _book_name(home), _book_name(away)
            game = {"mid": str(mid), "lid": lid, "start": m.get("startTime") or "", "sport": sport,
                    "units": m.get("units"),   # tennis: "Games" vs "Sets" — disambiguates same-number lines
                    "home_name": home, "away_name": away, "home_key": hk, "away_key": ak,
                    "totals": deriv[mid]["totals"], "spreads": deriv[mid]["spreads"]}
            index.setdefault(frozenset({hk, ak}), []).append(game)
            n_games += 1
        time.sleep(jitter)
    client.close()
    print(f"[PINNACLE] {n_games} games with spread/total across {len(league_ids)} leagues "
          f"({len(index)} matchups)")
    return index


# ── Kalshi side (public API): one "leg" per spread/total market ──────────────────────────────────────────
def kalshi_events(series: str) -> list[dict]:
    out, cursor = [], ""
    while True:
        p = {"series_ticker": series, "status": "open", "with_nested_markets": "true", "limit": 200}
        if cursor:
            p["cursor"] = cursor
        d = requests.get(f"{KALSHI_BASE}/events", params=p, timeout=30).json()
        out += d.get("events", [])
        cursor = d.get("cursor", "")
        if not cursor:
            break
    return out


def _teams_from_title(title: str) -> tuple[str, str] | None:
    """'A's vs Los Angeles A: Spread' -> ('A's', 'Los Angeles A'); '... : Total Games' too. None if no 'vs'."""
    base = (title or "").split(":")[0]
    m = re.split(r"\s+vs\.?\s+", base, maxsplit=1, flags=re.IGNORECASE)
    return (m[0].strip(), m[1].strip()) if len(m) == 2 else None


def _spread_team(sub: str) -> str:
    """Kalshi spread YES subtitle -> the team it's ON. 'Los Angeles A wins by over 3.5 runs' -> 'Los Angeles A';
    'Matteo Berrettini -8.5 games' -> 'Matteo Berrettini'."""
    m = re.match(r"^(.+?)\s+(?:wins by|[-+]\d)", sub or "")
    return (m.group(1) if m else sub or "").strip()


def _close_date(m: dict):
    for fld in ("expected_expiration_time", "close_time"):
        v = m.get(fld)
        if v:
            try:
                return dt.datetime.fromisoformat(v.replace("Z", "+00:00"))
            except ValueError:
                pass
    return None


# ── matching ─────────────────────────────────────────────────────────────────────────────────────────────
_CANDIDATE_FUZZY: set = set()      # marks that the last _candidate_games call used the fuzzy name path


def _candidate_games(teams: frozenset, kdt, sett: str, index: dict, thr: int) -> list:
    """ALL Pinnacle matchups for a Kalshi team-set + date (exact set, else bipartite containment >= thr). Returns
    a LIST (not one) because tennis lists the SAME players twice — the "(Games)" matchup (games handicap/total,
    what KXATPG* needs) AND the "(Sets)" winner matchup (set handicap ±1.5 / total 2.5 sets). The caller's
    line-search then picks whichever matchup actually offers the Kalshi line, self-disambiguating the two."""
    cands = index.get(teams)
    if not cands and fuzz is not None:
        kt = list(teams)
        if len(kt) == 2:
            best, best_min = None, 0
            for bset, games in index.items():
                bt = list(bset)
                if len(bt) != 2:
                    continue
                a = min(_team_sim(kt[0], bt[0]), _team_sim(kt[1], bt[1]))
                b = min(_team_sim(kt[0], bt[1]), _team_sim(kt[1], bt[0]))
                worse = max(a, b)
                if worse > best_min:
                    best_min, best = worse, games
            if best_min >= thr:
                cands = best
                _CANDIDATE_FUZZY.add(1)            # sub-100 name match: tag the pairs it produces
    if not cands:
        return []
    if kdt is not None:   # ticker time authoritative (baseball): within 6h (doubleheader-safe)
        return [g for g in cands if (pd := _pin_dt(g.get("start", ""))) and abs((pd - kdt).total_seconds()) <= 6 * 3600]
    try:                  # tennis: date-only, within +/-1 day
        kd = dt.datetime.strptime((sett or "")[:10], "%Y-%m-%d").date()
    except ValueError:
        return list(cands)
    return [g for g in cands if (pd := _pin_dt(g.get("start", ""))) and abs((kd - pd.date()).days) <= 1]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true", help="write derivative_pairs.json (default = dry run)")
    ap.add_argument("--days", type=int, default=10, help="keep markets settling within N days (default 10)")
    ap.add_argument("--threshold", type=int, default=85, help="min team-name score for a fuzzy game match")
    ap.add_argument("--sidecar", default=os.environ.get("HARDVEN_SIDECAR_URL", "http://127.0.0.1:8787"),
                    help="sidecar base URL — the price gate reads live book prices from its /odds")
    ap.add_argument("--no-price-gate", action="store_true",
                    help="skip the price gate (which rejects wrong-game pairs)")
    ap.add_argument("--price-tol", type=float, default=0.25,
                    help="price-gate tolerance 0-1: book vs Kalshi implied prob must agree within this")
    args = ap.parse_args()

    if fuzz is None:
        print("[WARN] rapidfuzz not installed — only exact team-set matches (Kalshi city != Pinnacle full "
              "name will miss). pip install rapidfuzz")

    index = build_pinnacle_index()
    horizon = dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=args.days)

    pairs: list[dict] = []
    filled = no_game = no_line = 0
    unmatched: list[str] = []
    for series, (mtype, sport_id) in KALSHI_SERIES.items():
        for ev in kalshi_events(series):
            tt = _teams_from_title(ev.get("title", ""))
            if not tt:
                continue
            teams = frozenset(_canon(_book_name(t)) for t in tt)
            ev_tk = ev.get("event_ticker", "")
            kdt = _kalshi_dt({"kalshi_ticker": ev_tk})
            # representative settlement date from the first market
            mkts = [m for m in (ev.get("markets") or []) if m.get("ticker")]
            sett = ""
            for m in mkts:
                c = _close_date(m)
                if c:
                    sett = c.date().isoformat()
                    break
            _CANDIDATE_FUZZY.clear()
            cands = _candidate_games(teams, kdt, sett, index, args.threshold)
            _was_fuzzy = bool(_CANDIDATE_FUZZY)
            if sport_id == 33:   # tennis: KXATPG* are GAMES markets → only the "(Games)" matchup, never "(Sets)"
                cands = [c for c in cands if c.get("units") == "Games"]
            if not cands:
                no_game += 1
                unmatched.append(f"{ev_tk}  {sorted(teams)} (no Pinnacle game)")
                continue
            for m in mkts:
                c = _close_date(m)
                if c is None or c > horizon:
                    continue
                L = m.get("floor_strike")
                if L is None:
                    continue
                L = float(L)
                tk = m.get("ticker")
                if mtype == "total":
                    g = next((x for x in cands if L in x["totals"]), None)   # the matchup that offers this line
                    if g is None:
                        no_line += 1
                        continue
                    yes = f'{g["lid"]}:{g["mid"]}:total:{_fmt(L)}:over'
                    no = f'{g["lid"]}:{g["mid"]}:total:{_fmt(L)}:under'
                    label = f'{g["home_name"]} vs {g["away_name"]} — Over {_fmt(L)}'
                    yes_name = k_out = ""      # over/under names nobody -- nothing to check
                else:  # spread
                    T = _spread_team(m.get("yes_sub_title", ""))
                    Tk = _canon(_book_name(T))
                    g = side = None
                    for x in cands:   # first candidate matchup whose T-side carries (-L)
                        s = "home" if _team_sim(Tk, x["home_key"]) >= _team_sim(Tk, x["away_key"]) else "away"
                        if (-L) in x["spreads"][s]:
                            g, side = x, s
                            break
                    if g is None:
                        no_line += 1
                        continue
                    other = "away" if side == "home" else "home"
                    yes = f'{g["lid"]}:{g["mid"]}:spread:{_fmt(-L)}:{side}'
                    no = f'{g["lid"]}:{g["mid"]}:spread:{_fmt(L)}:{other}'
                    label = f'{g["home_name"]} vs {g["away_name"]} — {T} {_fmt(-L)}'
                    yes_name, k_out = g[f"{side}_name"], T
                # kalshi_yes_price IS WHAT MAKES THE GATE WORK. The gate compares it against the
                # book's implied probability to catch a pair matched to the WRONG GAME; without it
                # every pair counts as `unvalidated` and the gate passes everything untested.
                entry = {
                    "kalshi_ticker": tk, "market_type": mtype, "line": L, "label": label,
                    "event_id": ev_tk, "settlement_date": c.date().isoformat(),
                    "is_neg_risk": False, "hardven_min_size": 1.0,
                    "hardven_yes_token": yes, "hardven_no_token": no,
                    "kalshi_yes_price": _yes_price(m),
                    "hardven_yes_name": yes_name, "kalshi_outcome": k_out,
                }
                if _was_fuzzy:
                    entry["fuzzy"] = True          # verify before real money
                pairs.append(entry)
                filled += 1

    # -- NAME SELF-CHECK (SPREADS ONLY) -----------------------------------------------------------
    # The side of a spread is chosen by ARGMAX over _team_sim, and an argmax ALWAYS returns a side --
    # including when NEITHER candidate is the right player (a fuzzy game match, or a subtitle this
    # parser read wrong). Token overlap can answer "neither", which is the one failure argmax cannot
    # report, so this is not circular the way re-running _team_sim would be.
    #
    # Name TOKENS, not substrings: "Felipe Meligeni Alves" vs "Felipe Meligeni Rodrigues Alves" is the
    # same player and a substring test strips both (it did, on the moneyline side).
    #
    # Totals are exempt on purpose: over/under names nobody, so no name check can judge one. Their
    # orientation is structural instead -- see the price gate below for why that is stronger, not weaker.
    # Canonicalise FIRST. The two venues write the same club differently ("A's" vs "Athletics"), and
    # _canon is the alias table the pairer already matches on, so skipping it here would have this check
    # disagree with the very matcher it is auditing.
    def _ntok(x: str) -> set:
        return {w for w in re.sub(r"[^a-z ]", " ", _canon(_book_name(x or "")).lower()).split()
                if len(w) >= 3}

    misoriented, kept = 0, []
    for e in pairs:
        out, pin = e.get("kalshi_outcome") or "", e.get("hardven_yes_name") or ""
        a, b = _ntok(out), _ntok(pin)
        # AN EMPTY TOKEN SET IS NO EVIDENCE, NOT A CONTRADICTION -- short names tokenise to nothing and a
        # bare `not (a & b)` reads that as a mismatch. Caught live on 2026-08-31: Kalshi's "A's" lost both
        # fragments to the >=3-char filter, so a correct Athletics pair was dropped as mis-oriented.
        if e.get("market_type") == "spread" and a and b and not (a & b):
            misoriented += 1
            print(f"[DERIV] MIS-ORIENTED {e.get('kalshi_ticker','?')}: Kalshi names '{out}' but the YES "
                  f"token is Pinnacle's '{pin}' -- dropped (its EV would be a phantom).")
            continue
        kept.append(e)
    pairs = kept
    if misoriented:
        print(f"[DERIV] *** {misoriented} spread row(s) named the WRONG PLAYER and were dropped. ***")


    # -- PRICE GATE: REJECT-ONLY. IT MUST NEVER SWAP. ---------------------------------------------
    # pair_auto.price_validate does two jobs: reject wrong-game pairs, and FIX inverted sides by
    # swapping the tokens. The first is wanted here. The second is actively harmful, for two reasons
    # that compound:
    #
    #   1. A derivative's orientation is STRUCTURAL, not inferred. Kalshi's YES on a KXATPGTOTAL
    #      market IS "Over L", so yes -> total:L:over is a definition, not a guess. There is nothing
    #      for a price to correct, so any swap it makes can only break a pair that was already right.
    #   2. Totals sit near 50/50 by construction, which is exactly where that gate says it is blind:
    #      "picking wrong pairs Kalshi-YES with the book's SAME side, which the executor then buys
    #      twice instead of hedging". The moneyline pairer survives that because sibling_validate
    #      catches inversions structurally first; a derivative has no sibling market, so nothing would.
    #
    # Seen on the first dry run of this change (2026-08-31): price_validate swapped a share of the
    # totals, so two identically-labelled "Over 39.5" markets came out with OPPOSITE tokens -- ARNDUC
    # got yes=under while BERWAW got yes=over. Every one of those swaps was wrong.
    #
    # What a price CAN still settle is whether we paired the WRONG GAME: there neither orientation
    # fits, which is unambiguous at any price level. So compare, drop the gross mismatches, and leave
    # orientation exactly as the structural mapping built it.
    if not args.no_price_gate and pairs:
        toks = {e[k] for e in pairs for k in ("hardven_yes_token", "hardven_no_token") if e.get(k)}
        # The odds WS is LAZY -- it connects on the first /odds that names a league, so a cold first
        # pass comes back empty and an unretried gate silently passes everything. Same wake-and-wait
        # the moneyline gate does, bounded so a genuinely dead feed still lets pairing finish.
        implied = _fetch_implied(args.sidecar, toks)
        want, tries, wait = max(1, len(toks) // 2), 6, 10.0
        while tries > 0 and len(implied) < want:
            print(f"[DERIV] price gate: only {len(implied)}/{len(toks)} token(s) priced -- the odds "
                  f"feed is still waking. Waiting {wait:.0f}s, {tries} attempt(s) left.")
            time.sleep(wait)
            implied = _fetch_implied(args.sidecar, toks)
            tries -= 1
        priced = sum(1 for e in pairs if e.get("kalshi_yes_price") is not None)
        print(f"[DERIV] price gate (reject-only): {priced}/{len(pairs)} pair(s) have a Kalshi price, "
              f"{len(implied)}/{len(toks)} token(s) priced by the book")
        ok = rej = unval = 0
        kept = []
        for e in pairs:
            ky, by = e.get("kalshi_yes_price"), implied.get(e.get("hardven_yes_token"))
            if ky is None or by is None:
                e["price_unvalidated"] = True   # consumers can tell a checked pair from an unchecked one
                unval += 1
                kept.append(e)
                continue
            e.pop("price_unvalidated", None)
            # WRONG-GAME ONLY: BOTH orientations must miss before this throws a pair away.
            if min(abs(ky - by), abs(ky - (1.0 - by))) > args.price_tol:
                print(f"[DERIV] PRICE-REJECT {e['kalshi_ticker']}  kalshi={ky:.2f} book={by:.2f} "
                      f"(neither orientation within {args.price_tol}) -> wrong game")
                rej += 1
                continue
            ok += 1
            kept.append(e)
        pairs = kept
        print(f"[DERIV] price gate: {ok} plausible | {rej} wrong-game rejected | {unval} unvalidated "
              f"(orientation NEVER altered)")
    else:
        print("[DERIV] price gate SKIPPED -- wrong-game pairs will NOT be caught.")

    pairs.sort(key=lambda e: (e["settlement_date"], e["kalshi_ticker"]))
    print(f"\n[DERIV] filled={filled}  no-Pinnacle-game={no_game}  line-not-offered={no_line}")
    for p in pairs[:25]:
        print(f"  {p['kalshi_ticker']:<34} {p['label']:<46} YES={p['hardven_yes_token']}  NO={p['hardven_no_token']}")
    if len(pairs) > 25:
        print(f"  … and {len(pairs) - 25} more")
    if unmatched[:15]:
        print("  -- sample no-game events --")
        for u in unmatched[:15]:
            print(f"   {u}")

    if args.write:
        if OUT.exists():
            shutil.copy2(OUT, OUT.with_suffix(".json.bak"))   # COPY (not move) → OUT stays present during backup
        atomic_write_json(OUT, pairs)                         # atomic → the C# hot-reload never reads a partial file
        print(f"\n[DERIV] wrote {len(pairs)} derivative pair(s) -> {OUT}")
    else:
        print("\n[DERIV] dry-run (no file written). Re-run with --write to save.")


if __name__ == "__main__":
    main()
