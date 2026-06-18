"""
bookmaker_gameview.py — parse bookmaker.eu's `GetGameView` HTTP response into match-winner selections.

THIS is the pre-match odds path (decided 2026-06-17). bookmaker.eu serves a clean JSON snapshot of a
match's whole board from a `GetGameView` XHR when you open the game; pre-match prices come from HERE, not
the WebSocket (the WS only streams in-play deltas). For a pre-match-only POC we just poll this endpoint.

Response shape (the parts we use):
  GameView.game[]  — one entry per market. The MAIN match-winner is the entry where idgm == famGame;
                     the rest are derivatives (set winner, handicap, correct score idspt="TNT", …).
  game.Derivatives.line[]  — price lines; the PRIMARY line is index == "0".
    on the primary line:  hoddst / voddst  = home / visitor MONEYLINE (American);
                          htm / vtm        = home / visitor names;
  game.descgmtyp  = max-stake label, e.g. "1K USD" → 1000, "500 USD" → 500.
  game.gmdt + gmtm = start date/time;  LiveGame=false = pre-match;  MoneyLineStatus=1 = tradeable.

Each match → two selections, keyed by the bookmaker game id `idgm`:
    "<idgm>:H"  = home  (htm)  moneyline
    "<idgm>:V"  = visitor(vtm) moneyline
Those ids go in cross_pairs.json as hardven_yes_token / hardven_no_token (map by the actual PLAYER:
Kalshi "Will Paul win?" YES → whichever of :H/:V IS Paul).
"""
from __future__ import annotations

import re
from typing import Optional

from bookmaker_stomp import american_to_decimal


def parse_max_stake(descgmtyp: Optional[str]) -> Optional[float]:
    """'1K USD' → 1000.0, '500 USD' → 500.0, '2.5K' → 2500.0. None/unparseable → None."""
    if not descgmtyp:
        return None
    m = re.match(r"\s*([\d.]+)\s*([KkMm]?)", descgmtyp)
    if not m:
        return None
    try:
        val = float(m.group(1))
    except ValueError:
        return None
    mult = {"k": 1_000.0, "m": 1_000_000.0}.get(m.group(2).lower(), 1.0)
    return val * mult


def _primary_ml_line(game: dict) -> Optional[dict]:
    """The Derivatives line with index=='0' that actually carries a moneyline (hoddst/voddst)."""
    lines = (game.get("Derivatives") or {}).get("line") or []
    primary = None
    for ln in lines:
        if ln.get("index") == "0":
            primary = ln
            if ln.get("hoddst") or ln.get("voddst"):
                return ln
    # fall back to any line that has a moneyline if index 0 didn't carry one
    if primary and (primary.get("hoddst") or primary.get("voddst")):
        return primary
    for ln in lines:
        if ln.get("hoddst") or ln.get("voddst"):
            return ln
    return None


def _is_tradeable(game: dict) -> bool:
    return (str(game.get("MoneyLineStatus")) == "1"
            and not game.get("MarketsClosed", False)
            and not game.get("FreezeMoneyLine", False))


def _emit_game(game: dict, out: dict, pre_match_only: bool) -> None:
    """Extract the main match-winner moneyline from ONE game object → out["<idgm>:H/V"].
    Shared by parse_gameview (single match) and parse_schedule (whole league)."""
    idgm = game.get("idgm")
    if not idgm or idgm != game.get("famGame"):
        return  # only the main match-winner market (derivatives/props excluded)
    if pre_match_only and game.get("LiveGame", False):
        return
    line = _primary_ml_line(game)
    if not line:
        return
    tradeable = _is_tradeable(game)
    start = f"{game.get('gmdt','')}{game.get('gmtm','')}"          # e.g. "2026061811:40:00"
    max_stake = parse_max_stake(game.get("descgmtyp"))
    category = game.get("categoryEn", "")
    htm, vtm = game.get("htm", ""), game.get("vtm", "")
    event = f"{htm} vs {vtm}"
    # 3-way market (soccer 1X2, cricket tie, …): the primary line carries a non-empty draw price. Such a
    # market must be paired NO-only (Kalshi NO + book back-team); see CrossPair.ThreeWay.
    three_way = bool((line.get("drawoddst") or "").strip())
    # H = home, V = visitor; D = draw, emitted ONLY for 3-way markets (soccer 1X2) so the Kalshi "Tie"
    # binary can pair against it (Kalshi Tie-NO + book back-draw).
    sides = [("H", "htm", "hoddst"), ("V", "vtm", "voddst")]
    if three_way:
        sides.append(("D", None, "drawoddst"))
    for side, name_key, odds_key in sides:
        sid = f"{idgm}:{side}"
        dec = american_to_decimal(line.get(odds_key))
        ok = tradeable and dec and dec > 1.0
        out[sid] = {
            "idgm": idgm,
            "idlg": game.get("idlg"),
            "side": side,
            "name": "Draw" if side == "D" else game.get(name_key, ""),
            "event": event,
            "live": bool(game.get("LiveGame", False)),
            "decimal_odds": float(dec) if ok else None,
            "implied_price": round(1.0 / dec, 6) if ok else None,
            "max_stake": max_stake if ok else 0.0,
            "status": "open" if ok else "suspended",
            "start": start,
            "category": category,
            "three_way": three_way,
        }


def parse_gameview(data: dict, pre_match_only: bool = True) -> dict:
    """
    GetGameView JSON (single match, all its markets) → {selection_id: {...}} for the MAIN match-winner
    moneyline. selection_id = "<idgm>:H" (home/htm) / "<idgm>:V" (visitor/vtm). Value: decimal_odds,
    implied_price (1/decimal), max_stake, name, side, status, start, category, idgm, idlg. Suspended/
    missing legs get status="suspended" and no usable price so no arb can fire.
    """
    out: dict[str, dict] = {}
    for game in ((data or {}).get("GameView") or {}).get("game") or []:
        _emit_game(game, out, pre_match_only)
    return out


def parse_leagues(data: dict, sports=None) -> list[dict]:
    """
    GetLeagues response (root key "ActiveLeagues") → flat list of match-winner leagues:
        [{ "id", "idsport", "desc", "sport", "region", "game_count" }, ...]
    Structure: ActiveLeagues.Data.Leagues.index[] (per SPORT, valueEn) → region[] → league[] (id/desc/idsport).
    Excludes idsport "TNT" (futures/props/multi-runner) and DOUBLES leagues. If `sports` (a set of UPPER
    sport names) is given, keeps only those sports. This is the dynamic league discovery — no hardcoded ids.
    """
    root = (((data or {}).get("ActiveLeagues") or {}).get("Data") or {}).get("Leagues") or {}
    out: list[dict] = []
    for node in root.get("index") or []:
        sport = (node.get("valueEn") or node.get("value") or "").strip()
        if sports and sport.upper() not in sports:
            continue
        for region in node.get("region") or []:
            region_name = (region.get("value", "") or "").upper()
            for lg in region.get("league") or []:
                idsport = lg.get("idsport", "")
                desc = (lg.get("desc") or lg.get("descEn") or "")
                du = desc.upper()
                lid = lg.get("id")
                # Drop non-match-winner leagues: TNT feed, doubles, and the futures/props/outright leagues
                # that share idsport "MU" with real matches (e.g. WC "Special Props"=17146, "To Finish
                # Higher"=20204). They collide with the real moneyline by team name → mixed-game pairs.
                if (not lid or idsport == "TNT" or "DOUBLES" in du
                        or any(k in du for k in _NON_MATCH_DESC)
                        or any(k in region_name for k in _NON_MATCH_REGION)):
                    continue
                out.append({"id": str(lid), "idsport": idsport, "desc": desc, "sport": sport,
                            "region": region.get("value", ""), "game_count": int(lg.get("gameCount") or 0)})
    return out


_NON_MATCH_DESC = ("FUTURE", "PROP", "OUTRIGHT", "TO WIN", "ODDS TO", "TO FINISH", "TO REACH",
                   "TO QUALIFY", "SPECIAL", "AWARD", "WINNER", "STAGE OF", "NAME THE")
_NON_MATCH_REGION = ("FUTURES", "PROPOSITION", "AWARDS")


def parse_schedule(data: dict, pre_match_only: bool = True) -> dict:
    """
    Schedule JSON (per-league BULK: Schedule.Data.Leagues.League[].dateGroup[].game[]) → the SAME
    {selection_id: {...}} dict as parse_gameview, for EVERY match across the returned league(s). This is
    the bulk odds source — one Schedule call covers a whole league's matches in one shot.
    """
    out: dict[str, dict] = {}
    leagues = (((data or {}).get("Schedule") or {}).get("Data") or {}).get("Leagues") or {}
    for league in leagues.get("League") or []:
        for dg in league.get("dateGroup") or []:
            for game in dg.get("game") or []:
                _emit_game(game, out, pre_match_only)
    return out


if __name__ == "__main__":
    # Validate against a captured GetGameView OR Schedule response (auto-detected):
    #   python bookmaker_gameview.py path/to/response.json
    import json
    import sys
    path = sys.argv[1] if len(sys.argv) > 1 else "gameview_sample.json"
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    parsed = parse_schedule(data) if "Schedule" in data else parse_gameview(data)
    for sid, s in parsed.items():
        print(f"{sid:>18}  {s['name']:<28} dec={s['decimal_odds']}  implied={s['implied_price']}  "
              f"max=${s['max_stake']}  {s['status']}  start={s['start']}")
