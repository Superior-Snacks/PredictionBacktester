#!/usr/bin/env python3
"""
pairHard.py — scaffold cross_pairs.json with the KALSHI side of classic sports-game markets.

Fetches Kalshi open events (public API, no auth/keys needed; same /events?with_nested_markets pattern
as pair_markets_v2.py, minus the auth since market data is public), keeps **classic sports games** —
single, full-match 2-way WINNER markets in real-sport leagues (the moneylines a sportsbook offers) —
and writes cross_pairs.json entries with the Kalshi side filled and the **bookmaker.eu side left blank**
for you to add by hand.

Kalshi's "Sports" category is mostly props/futures/esports (World Cup goalscorers, season win totals,
draft picks, spreads, set/half winners, CS2/Valorant maps). So instead of the whole category, this
allowlists the per-game match-winner series. DEFAULT scope = the ACTIVE sports' moneyline series from the
unified catalog (sidecar/sports.py, honoring HARDVEN_SPORTS) — the same sports the schedule/lifecycle run.
Use --classic for the broad built-in CLASSIC_SERIES set (every sport), or --series / --all to override.

The HardVenArb bot SKIPS any entry whose hardven_yes_token/hardven_no_token is empty (the loader
requires non-empty tokens), so it's safe to run with blanks and fill the bookmaker ids per pair as you
go (see sidecar/README "Recon" for where the bookmaker selection id comes from).

Usage:
  python pairHard.py                      # ACTIVE sports (sports.py / HARDVEN_SPORTS), settling within 10 days
  python pairHard.py --classic            # broad built-in allowlist (every sport)
  python pairHard.py --days 21            # widen the window
  python pairHard.py --series KXMLBGAME   # only these series (prefix match)
  python pairHard.py --all                # every Sports market (no allowlist) — noisy, for exploring

────────────────────────────────────────────────────────────────────────────────────────────────
OUTLINE — a FULLY-FILLED pair (this script fills everything EXCEPT the two hardven_* fields, which you
paste in by hand from bookmaker.eu):

  {
    "kalshi_ticker":     "KXMLBGAME-26JUN16TORCHC-TOR",   # Kalshi binary market; YES = this team wins
    "label":             "Toronto vs Chicago C — Toronto win",
    "event_id":          "KXMLBGAME-26JUN16TORCHC",
    "settlement_date":   "2026-06-16",
    "is_neg_risk":       false,
    "hardven_min_size":  1.0,
    # ── you fill these two (bookmaker.eu selection ids from the odds JSON; see sidecar README) ──
    "hardven_yes_token": "<bookmaker id for the SAME outcome as Kalshi YES, e.g. Toronto moneyline>",
    "hardven_no_token":  "<bookmaker id for the OPPOSITE outcome (= Kalshi NO), e.g. Chicago moneyline>"
  }

Mapping rule: Kalshi YES ↔ hardven_yes_token must be the SAME real-world outcome; hardven_no_token is
the opposite side. The bot backs whichever side makes the cross-venue net < $1.
────────────────────────────────────────────────────────────────────────────────────────────────
"""
import argparse
import datetime as dt
import json
import shutil
import sys
from pathlib import Path


import requests

sys.path.insert(0, str(Path(__file__).parent / "sidecar"))   # share the unified sport catalog with the sidecar
import sports as sports_cfg
from env_util import atomic_write_json

if hasattr(sys.stdout, "reconfigure"):   # Windows console: allow non-ASCII in prints
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"
HERE = Path(__file__).parent
OUT = HERE / "cross_pairs.json"
DEFAULT_MAX_DAYS_OUT = 10

# Match-WINNER (moneyline) series only — the "…GAME"/"…MATCH"/"…FIGHT" tickers, NOT the sub-markets
# (…SPREAD/…TOTAL/…RFI/…F5/…SETWINNER/…1H/…2H/…BTTS/…EXACTMATCH = totals/spreads/props, future work).
# Exact series_ticker match. Out-of-season ones are harmless (they just produce no games). Discovered
# from the Kalshi public API — far more per-match leagues than the obvious North-American ones. A series
# only PAIRS if bookmaker.eu also lists that league; extras simply stay unmatched.
CLASSIC_SERIES = {
    # Baseball — MLB / Korea KBO / Japan NPB
    "KXMLBGAME", "KXKBOGAME", "KXNPBGAME",
    # Basketball — WNBA/NBA + international club leagues (Spain ACB, Germany BBL, Italy Serie A/B,
    # Turkey BSL, France Elite, NZ NBL, Poland, Venezuela, Vietnam, Puerto Rico BSN, Israel, BIG3, NCAA)
    "KXNBAGAME", "KXWNBAGAME", "KXACBGAME", "KXBBLGAME", "KXBBSERIEAGAME", "KXBBSERIEBGAME",
    "KXBSLGAME", "KXLNBELITEGAME", "KXNZNBLGAME", "KXPLKGAME", "KXSPBGAME", "KXVBAGAME",
    "KXBSNGAME", "KXBIG3GAME", "KXISLGAME", "KXNCAABBGAME",
    # Soccer — World Cup (3-way moneyline; pairs NO-only via three_way vs home/draw/visitor) + club
    # leagues (USL + USL Cup, Spain La Liga 2, Chile, Bolivia). Club soccer is also 3-way (auto-detected
    # from the bookmaker draw price). Sub-markets KXWC{SPREAD,TOTAL,BTTS,1H*,2H*} are future totals/spreads.
    "KXWCGAME", "KXUSLGAME", "KXUSLCUPGAME", "KXLALIGA2GAME", "KXCHLLDPGAME", "KXBOLPDIVGAME",
    # Football — NFL / NCAA (off-season) + Canadian CFL (in-season)
    "KXNFLGAME", "KXNCAAFGAME", "KXCFLGAME",
    # Aussie Rules — AFL
    "KXAFLGAME",
    # Tennis — full match winner (NOT set/exact-match/spread sub-markets)
    "KXATPMATCH", "KXWTAMATCH", "KXITFMATCH", "KXITFWMATCH",
    "KXATPCHALLENGERMATCH", "KXWTACHALLENGERMATCH",
    # Cricket — T20 / Women's T20 / ODI / Test / County Championship
    "KXT20MATCH", "KXWT20MATCH", "KXODIMATCH", "KXTESTMATCH", "KXCOUNTYCHAMPMATCH",
    # Combat sports — boxing / UFC
    "KXBOXING", "KXUFCFIGHT",
}


def fetch_open_events() -> list[dict]:
    """All open events (paginated), with nested markets. Public endpoint — no auth."""
    events, cursor = [], ""
    while True:
        params = {"status": "open", "with_nested_markets": "true", "limit": 200}
        if cursor:
            params["cursor"] = cursor
        r = requests.get(f"{KALSHI_BASE}/events", params=params, timeout=30)
        r.raise_for_status()
        data = r.json()
        events.extend(data.get("events", []))
        cursor = data.get("cursor", "")
        if not cursor:
            break
    return events


def _close_date(market: dict):
    for fld in ("expected_expiration_time", "close_time"):
        val = market.get(fld)
        if val:
            try:
                return dt.datetime.fromisoformat(val.replace("Z", "+00:00"))
            except ValueError:
                pass
    return None


def _yes_price(m: dict):
    """Kalshi YES probability (0-1) for the price-consistency gate: mid of yes_bid/yes_ask (cents) if both
    present, else last_price; None if the market has no price yet. Lets pair_auto verify each pair's two
    sides AGREE on the favorite (catches inverted sides + wrong-game mispairs)."""
    yb, ya = m.get("yes_bid"), m.get("yes_ask")
    if isinstance(yb, (int, float)) and isinstance(ya, (int, float)) and yb > 0 and ya > 0:
        return round((yb + ya) / 200.0, 4)
    lp = m.get("last_price")
    if isinstance(lp, (int, float)) and lp > 0:
        return round(lp / 100.0, 4)
    return None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=DEFAULT_MAX_DAYS_OUT,
                    help="keep markets settling within N days (default 10)")
    ap.add_argument("--series", default="",
                    help="comma-separated series-ticker prefixes to keep (overrides the allowlist)")
    ap.add_argument("--all", action="store_true",
                    help="keep ALL Sports markets (no series allowlist) — noisy")
    ap.add_argument("--classic", action="store_true",
                    help="use the broad built-in CLASSIC_SERIES allowlist (every sport) instead of the ACTIVE "
                         "sports from sports.py / HARDVEN_SPORTS")
    ap.add_argument("--fresh", action="store_true",
                    help="force a full rebuild with BLANK Pinnacle tokens (default: MERGE — carry over already-"
                         "filled hardven_*_token for tickers that are still open, so an intraday re-pair can't drop "
                         "a working pairing whose live moneyline is momentarily suspended at catalog time)")
    args = ap.parse_args()

    custom = [s.strip().upper() for s in args.series.split(",") if s.strip()]
    # default scope = the ACTIVE sports' moneyline series (unified config); --classic = the broad built-in set
    allowlist = CLASSIC_SERIES if args.classic else sports_cfg.moneyline_series()
    horizon = dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=args.days)

    print("[KALSHI] fetching open events…")
    events = fetch_open_events()
    sports = [e for e in events if (e.get("category", "") or "").lower() == "sports"]
    print(f"[KALSHI] {len(events)} open events, {len(sports)} in Sports.")

    def keep_series(st: str) -> bool:
        st = (st or "").upper()
        if args.all:
            return True
        if custom:
            return any(st.startswith(p) for p in custom)
        return st in allowlist

    entries, kept_series = [], {}
    for ev in sports:
        st = ev.get("series_ticker", "")
        if not keep_series(st):
            continue
        for m in ev.get("markets", []) or []:
            ticker = m.get("ticker", "")
            if not ticker:
                continue
            close = _close_date(m)
            if close is None or close > horizon:
                continue
            kept_series[st] = kept_series.get(st, 0) + 1
            entries.append({
                "kalshi_ticker":     ticker,
                "label":             m.get("title", "") or ev.get("title", ""),
                # event_title = the matchup ("A vs B"); kalshi_outcome = THIS market's YES side
                # ("Jordan"/"Tie"/"Hijikata"). pair_auto matches on these (C# ignores unknown keys).
                "event_title":       ev.get("title", ""),
                "kalshi_outcome":    m.get("yes_sub_title") or m.get("subtitle") or "",
                "event_id":          ev.get("event_ticker", ""),
                "settlement_date":   close.date().isoformat(),
                # P(this market's YES) — pair_auto's price gate checks the book side agrees on the favorite.
                "kalshi_yes_price":  _yes_price(m),
                "is_neg_risk":       False,   # sportsbook bets have no neg-risk concept
                "hardven_min_size":  1.0,
                # ── FILL FROM bookmaker.eu (see sidecar/README "Recon"); empty = bot skips this pair ──
                "hardven_yes_token": "",
                "hardven_no_token":  "",
            })

    # MERGE (default): carry over already-filled Pinnacle tokens for tickers that are STILL open, so an intraday
    # re-pair (which rebuilds this scaffold from scratch) can't drop a working live pairing whose moneyline is
    # momentarily suspended on Pinnacle's board at catalog time. --fresh forces the old blank rebuild. Only the
    # two hardven_* tokens + the three_way/fuzzy tags are carried; all Kalshi fields stay FRESH from this fetch.
    carried = 0
    if not args.fresh and OUT.exists():
        try:
            prev = {e.get("kalshi_ticker"): e for e in json.loads(OUT.read_text(encoding="utf-8"))}
        except (ValueError, OSError):
            prev = {}
        for e in entries:
            p = prev.get(e["kalshi_ticker"])
            if p and p.get("hardven_yes_token") and p.get("hardven_no_token"):
                e["hardven_yes_token"] = p["hardven_yes_token"]
                e["hardven_no_token"] = p["hardven_no_token"]
                for flag in ("three_way", "fuzzy"):
                    if p.get(flag):
                        e[flag] = p[flag]
                carried += 1

    entries.sort(key=lambda e: (e["settlement_date"], e["kalshi_ticker"]))

    if OUT.exists():
        shutil.copy2(OUT, OUT.with_suffix(".json.bak"))   # COPY (not move) → OUT stays present during the backup
    atomic_write_json(OUT, entries)                        # atomic overwrite → the C# hot-reload never reads a partial file

    mode = "fresh rebuild" if args.fresh else f"merged ({carried} filled pair(s) carried over)"
    print(f"[OK] wrote {len(entries)} classic sports-game markets to {OUT.name} — {mode} "
          f"(Kalshi side filled; Pinnacle tokens filled by pair_pinnacle).")
    if kept_series:
        print("     kept series: " + ", ".join(f"{k}={v}" for k, v in sorted(kept_series.items(), key=lambda x: -x[1])))
    else:
        print("     (0 kept — try --days larger, --series, or --all; or update CLASSIC_SERIES.)")


if __name__ == "__main__":
    main()
