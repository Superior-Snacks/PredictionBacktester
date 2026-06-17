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


def parse_gameview(data: dict, pre_match_only: bool = True) -> dict:
    """
    GetGameView JSON → {selection_id: {...}} for every MAIN match-winner moneyline (idgm == famGame).
    selection_id = "<idgm>:H" (home/htm) and "<idgm>:V" (visitor/vtm).
    Value: decimal_odds, implied_price (1/decimal), max_stake, name, side, status, start (gmdt+gmtm),
    category, idgm. Suspended/missing legs get status="suspended" and no usable price so no arb can fire.
    """
    out: dict[str, dict] = {}
    games = ((data or {}).get("GameView") or {}).get("game") or []
    for game in games:
        idgm = game.get("idgm")
        if not idgm or idgm != game.get("famGame"):
            continue  # only the main match-winner market (derivatives/props excluded)
        if pre_match_only and game.get("LiveGame", False):
            continue
        line = _primary_ml_line(game)
        if not line:
            continue
        tradeable = _is_tradeable(game)
        start = f"{game.get('gmdt','')}{game.get('gmtm','')}"          # e.g. "2026061811:40:00"
        max_stake = parse_max_stake(game.get("descgmtyp"))
        category = game.get("categoryEn", "")
        for side, name_key, odds_key in (("H", "htm", "hoddst"), ("V", "vtm", "voddst")):
            sid = f"{idgm}:{side}"
            dec = american_to_decimal(line.get(odds_key))
            ok = tradeable and dec and dec > 1.0
            out[sid] = {
                "idgm": idgm,
                "idlg": game.get("idlg"),
                "side": side,
                "name": game.get(name_key, ""),
                "decimal_odds": float(dec) if ok else None,
                "implied_price": round(1.0 / dec, 6) if ok else None,
                "max_stake": max_stake if ok else 0.0,
                "status": "open" if ok else "suspended",
                "start": start,
                "category": category,
            }
    return out


if __name__ == "__main__":
    # Validate against a captured GetGameView response: python bookmaker_gameview.py path/to/gameview.json
    import json
    import sys
    path = sys.argv[1] if len(sys.argv) > 1 else "gameview_sample.json"
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    parsed = parse_gameview(data)
    for sid, s in parsed.items():
        print(f"{sid:>16}  {s['name']:<28} dec={s['decimal_odds']}  implied={s['implied_price']}  "
              f"max=${s['max_stake']}  {s['status']}  start={s['start']}  {s['category']}")
