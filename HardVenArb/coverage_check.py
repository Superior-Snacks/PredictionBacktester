#!/usr/bin/env python3
"""
coverage_check.py — is the browser-WS READER's coverage as wide as a paho run would be?

Run this WHILE the sidecar is up with the reader on (PINNACLE_DEDICATED_WS=0 PINNACLE_WINDOW_WS_READ=1) and the
tennis board open, and after it's been running a few minutes (so the one-time REST-seed has aged out and only the
reader keeps tokens live). For every FILLED tennis pair it compares:

  - SIDECAR-LIVE : the sidecar is serving that matchup fresh + open + priced  (what the C# bot counts as a book)
  - REST-BETTABLE: the guest board (ground truth) currently lists a period-0 moneyline with prices for it

  covered      = SIDECAR-LIVE                          -> the reader is delivering it            (good)
  GAP          = REST-BETTABLE and NOT sidecar-live    -> bettable but the reader isn't feeding  (the concern)
  not-bettable = NOT REST-BETTABLE                     -> settled / suspended / off the board    (correctly not live)

If GAP ~= 0, the sport-level browser feed covers the full slate and 122/256-style numbers are just the bettable
subset. A large GAP means the browser WS is quietly narrower than the league-level paho subscription.

    python coverage_check.py            # sidecar at 127.0.0.1:8787
    python coverage_check.py --fresh 15 # freshness threshold seconds (default 12)
"""
import argparse
import json
import os
import urllib.request

import httpx

HERE = os.path.dirname(os.path.abspath(__file__))
GUEST = "https://guest.api.arcadia.pinnacle.com/0.1"
KEY = "CmX2KcMrXuFmNg6YFbmTxE0y9CIrOi0R"
TENNIS_SERIES = ("KXATP", "KXWTA", "KXITF")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sidecar", default=os.environ.get("HARDVEN_SIDECAR_URL", "http://127.0.0.1:8787"))
    ap.add_argument("--ttl", type=float, default=30.0, help="seconds; a matchup the reader pushed within this is 'live'")
    args = ap.parse_args()

    d = json.load(open(os.path.join(HERE, "cross_pairs.json"), encoding="utf-8-sig"))
    pairs = [e for e in d if (e.get("hardven_yes_token") or "").count(":") == 2
             and any((e.get("kalshi_ticker") or "").startswith(s) for s in TENNIS_SERIES)]
    matchups = {}                                   # matchups: lid -> {mid: label}
    for e in pairs:
        t = e.get("hardven_yes_token")
        if t and t.count(":") == 2:
            lid, mid, _ = t.split(":")
            matchups.setdefault(lid, {})[mid] = (e.get("label") or "")[:44]
    n_matchups = sum(len(v) for v in matchups.values())
    print(f"{len(pairs)} filled tennis pairs -> {n_matchups} distinct matchups across {len(matchups)} leagues")

    # 1. the matchups the READER has actually pushed odds for (coverage truth, not /odds freshness)
    try:
        resp = json.load(urllib.request.urlopen(
            args.sidecar.rstrip("/") + f"/debug/reader?ttl={args.ttl}", timeout=15))
    except Exception as ex:
        print(f"sidecar /debug/reader failed ({ex}) — is the sidecar up with PINNACLE_WINDOW_WS_READ=1?")
        return
    live_mid = set(resp.get("live_mids", []))
    print(f"reader is live on {len(live_mid)} matchups (all sports) in the last {args.ttl:g}s")

    # 2. ground truth: which paired matchups the guest board currently prices
    gc = httpx.Client(headers={"accept": "application/json", "x-api-key": KEY,
                               "origin": "https://www.pinnacle.bet", "user-agent": "Mozilla/5.0"},
                      timeout=25, follow_redirects=True)
    rest_bettable = set()
    for lid in matchups:
        try:
            st = gc.get(f"{GUEST}/leagues/{lid}/markets/straight").json()
        except Exception:
            st = []
        for mk in (st or []):
            if mk.get("type") == "moneyline" and mk.get("period") == 0 and 2 <= len(mk.get("prices") or []) <= 3:
                rest_bettable.add(f"{lid}:{mk.get('matchupId')}")

    # 3. classify every paired matchup
    covered = gap = not_bettable = 0
    gaps = []
    for lid, mids in matchups.items():
        for mid, label in mids.items():
            key = f"{lid}:{mid}"
            if key in live_mid:
                covered += 1
            elif key in rest_bettable:
                gap += 1
                if len(gaps) < 15:
                    gaps.append(f"{key}  {label}")
            else:
                not_bettable += 1

    print(f"\nCOVERAGE of {n_matchups} paired tennis matchups:")
    print(f"  covered (reader serving live)     : {covered}")
    print(f"  GAP (REST-bettable, reader stale) : {gap}   <-- the concern")
    print(f"  not-bettable (settled/suspended)  : {not_bettable}")
    rest_n = covered + gap
    if rest_n:
        print(f"\n  of the {rest_n} currently-bettable-per-REST matchups, reader covers "
              f"{covered} ({100*covered/rest_n:.0f}%)")
    if gaps:
        print("\n  GAP examples (bettable per REST but the reader isn't feeding):")
        for g in gaps:
            print("   ", g)
    print("\nVERDICT:", "FULL COVERAGE (gap ~0) — 122/256-style counts are just the bettable subset"
          if gap <= max(2, 0.05 * max(1, rest_n)) else
          "COVERAGE GAP — the browser WS is narrower than REST; the board may not subscribe the whole slate")


if __name__ == "__main__":
    main()
