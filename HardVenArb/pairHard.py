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
allowlists the per-game match-winner series (KXMLBGAME, KXNFLGAME, tennis KX*MATCH, KXBOXING,
KXUFCFIGHT, …). Tune CLASSIC_SERIES below, or use --series / --all.

The HardVenArb bot SKIPS any entry whose hardven_yes_token/hardven_no_token is empty (the loader
requires non-empty tokens), so it's safe to run with blanks and fill the bookmaker ids per pair as you
go (see sidecar/README "Recon" for where the bookmaker selection id comes from).

Usage:
  python pairHard.py                      # classic-sports allowlist, settling within 10 days
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
import sys
from pathlib import Path

import requests

if hasattr(sys.stdout, "reconfigure"):   # Windows console: allow non-ASCII in prints
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"
HERE = Path(__file__).parent
OUT = HERE / "cross_pairs.json"
DEFAULT_MAX_DAYS_OUT = 10

# Classic full-match, 2-way WINNER series (moneylines). Exact series_ticker match. Edit freely.
# (Some are listed even when out of season — harmless; they simply won't appear until games are posted.)
CLASSIC_SERIES = {
    # North American leagues — game winner
    "KXNBAGAME", "KXWNBAGAME", "KXNFLGAME", "KXMLBGAME", "KXNHLGAME",
    "KXNCAAFGAME", "KXNCAABGAME", "KXKBOGAME",
    # Tennis — full match winner (NOT set/half winners)
    "KXATPMATCH", "KXWTAMATCH", "KXITFMATCH", "KXITFWMATCH",
    "KXATPCHALLENGERMATCH", "KXWTACHALLENGERMATCH",
    # Cricket
    "KXT20MATCH", "KXWT20MATCH",
    # Combat sports
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


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=DEFAULT_MAX_DAYS_OUT,
                    help="keep markets settling within N days (default 10)")
    ap.add_argument("--series", default="",
                    help="comma-separated series-ticker prefixes to keep (overrides the allowlist)")
    ap.add_argument("--all", action="store_true",
                    help="keep ALL Sports markets (no series allowlist) — noisy")
    args = ap.parse_args()

    custom = [s.strip().upper() for s in args.series.split(",") if s.strip()]
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
        return st in CLASSIC_SERIES

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
                "event_id":          ev.get("event_ticker", ""),
                "settlement_date":   close.date().isoformat(),
                "is_neg_risk":       False,   # sportsbook bets have no neg-risk concept
                "hardven_min_size":  1.0,
                # ── FILL FROM bookmaker.eu (see sidecar/README "Recon"); empty = bot skips this pair ──
                "hardven_yes_token": "",
                "hardven_no_token":  "",
            })

    entries.sort(key=lambda e: (e["settlement_date"], e["kalshi_ticker"]))

    if OUT.exists():
        OUT.replace(OUT.with_suffix(".json.bak"))
    OUT.write_text(json.dumps(entries, indent=2), encoding="utf-8")

    print(f"[OK] wrote {len(entries)} classic sports-game markets to {OUT.name} "
          f"(Kalshi side filled; add hardven_*_token by hand).")
    if kept_series:
        print("     kept series: " + ", ".join(f"{k}={v}" for k, v in sorted(kept_series.items(), key=lambda x: -x[1])))
    else:
        print("     (0 kept — try --days larger, --series, or --all; or update CLASSIC_SERIES.)")


if __name__ == "__main__":
    main()
