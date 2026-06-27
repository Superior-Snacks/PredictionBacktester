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

Coverage: tennis = ATP only (WTA/ITF have no Kalshi games-spread/total); baseball = MLB + KBO (NPB none).

  python pair_derivatives.py            # dry-run preview
  python pair_derivatives.py --write    # write derivative_pairs.json
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import time
from pathlib import Path

import httpx
import requests

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from pair_auto import _norm, _book_name, _team_sim, _kalshi_dt, fuzz   # proven name/date primitives
from pair_pinnacle import _canon, _pin_dt                              # baseball aliases + ISO start parse

KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"
GUEST_BASE = os.environ.get("PINNACLE_GUEST_BASE", "https://guest.api.arcadia.pinnacle.com/0.1")
GUEST_KEY = os.environ.get("PINNACLE_API_KEY", "CmX2KcMrXuFmNg6YFbmTxE0y9CIrOi0R")
HERE = Path(__file__).resolve().parent
OUT = HERE.parent / "derivative_pairs.json"

# Kalshi series -> ("spread"|"total", Pinnacle sport id). Baseball=3, Tennis=33.
KALSHI_SERIES = {
    "KXMLBSPREAD": ("spread", 3), "KXMLBTOTAL": ("total", 3),
    "KXKBOSPREAD": ("spread", 3), "KXKBOTOTAL": ("total", 3),
    "KXATPGSPREAD": ("spread", 33), "KXATPGTOTAL": ("total", 33),
}
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
            cands = _candidate_games(teams, kdt, sett, index, args.threshold)
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
                pairs.append({
                    "kalshi_ticker": tk, "market_type": mtype, "line": L, "label": label,
                    "event_id": ev_tk, "settlement_date": c.date().isoformat(),
                    "is_neg_risk": False, "hardven_min_size": 1.0,
                    "hardven_yes_token": yes, "hardven_no_token": no,
                })
                filled += 1

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
            OUT.replace(OUT.with_suffix(".json.bak"))
        OUT.write_text(json.dumps(pairs, indent=2), encoding="utf-8")
        print(f"\n[DERIV] wrote {len(pairs)} derivative pair(s) -> {OUT}")
    else:
        print("\n[DERIV] dry-run (no file written). Re-run with --write to save.")


if __name__ == "__main__":
    main()
