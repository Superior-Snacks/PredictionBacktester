"""
pair_oddspapi.py -- fill the Pinnacle side of cross_pairs.json by TICKER JOIN via oddspapi, not name matching.

Replaces pair_pinnacle.py's fuzzy team-name containment with a deterministic lookup: oddspapi carries BOTH
books under the same normalized fixture, and its Kalshi `bookmakerOutcomeId` IS the full Kalshi market ticker
(verified live 2026-08-05: 'KXATPCHALLENGERMATCH-26AUG05HARLAJ-HAR'). So the join is:

    scaffold.kalshi_ticker == kalshi boid  ->  normalized outcomeId  ->  pinnacle boid (home/away/draw)
                                               (121 = participant1,       -> token lid:mid:designation
                                                122 = participant2, ...)     (lid+mid from the market path)

No team names anywhere. Orientation comes from the vendor's normalized outcome ids and is then price-checked
(same-vendor, same-instant Kalshi vs Pinnacle implied probabilities), so a wrong join cannot slip through
silently. pair_pinnacle.py stays as the backstop for whatever oddspapi's Kalshi feed lacks.

THREE-WAY sports (soccer 1X2): each Kalshi outcome market (TeamA / Tie / TeamB) pairs against that outcome's
own Pinnacle leg with `three_way: true` -- the C# then forces K_NO_P_YES (Kalshi NO + Pinnacle back-this-
outcome), the only complete 2-way hedge when a draw exists. The Tie market pairs the Pinnacle DRAW leg.
The sidecar odds path already tokenises draw; UI PLACEMENT of a draw is still unexercised -- run one
/bet/test on a draw selection before trusting a live fire on it.

Flow (scaffold first: `python pairHard.py --classic` fills the Kalshi side for EVERY sport's game series):

    python pair_oddspapi.py                     # dry-run preview
    python pair_oddspapi.py --write             # fill cross_pairs.json (atomic; C# hot-reload safe)
    python pair_oddspapi.py --sports soccer,tennis,baseball --days 9
    python pair_oddspapi.py --max-requests 40   # hard quota ceiling for one run

QUOTA (the trial plan is 250/month TOTAL): one run costs ~ N_sports (fixtures) + 2 x ceil(tournaments/5)
(odds: one call per book -- the API rejects multi-book requests despite the docs). Budget is enforced by
--max-requests: tournaments are ranked by how many scaffold tickers they can fill and the tail is dropped,
loudly, when the ceiling would be crossed. /v4/account is polled free before/after so the cost is visible.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import re
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

sys.stdout.reconfigure(encoding="utf-8")     # accented team names on a cp1252 console

from agg_oddspapi import OddsPapiClient, _iso_ts
from env_util import atomic_write_json, load_dotenv_upwards

load_dotenv_upwards()

_TICKER_RE = re.compile(r"^KX[A-Z0-9]+-")     # a Kalshi boid is a full market ticker


def _pinnacle_moneyline(bo: dict):
    """Pinnacle bookmakerOdds block -> (lid, mid, {ocId: (designation, price, limit)}) for the FULLTIME
    moneyline, or None. Period 0 in the market path is the full match -- set/half winners are rejected."""
    mid = str(bo.get("bookmakerFixtureId") or "")
    for mk in (bo.get("markets") or {}).values():
        path = str(mk.get("bookmakerMarketId") or "")
        parts = path.split("/")
        if len(parts) < 3 or parts[-1] != "moneyline" or parts[-2] != "0":
            continue
        lid = parts[2] if len(parts) > 2 else ""
        outs = {}
        for oc_id, oc in (mk.get("outcomes") or {}).items():
            for pl in (oc.get("players") or {}).values():
                desig = str(pl.get("bookmakerOutcomeId") or "").lower()
                if desig in ("home", "away", "draw"):
                    try:
                        price = float(pl.get("price") or 0.0)
                    except (TypeError, ValueError):
                        price = 0.0
                    outs[str(oc_id)] = (desig, price, pl.get("limit"))
        if outs and lid and mid:
            return lid, mid, outs
    return None


def _kalshi_tickers(bo: dict):
    """Kalshi bookmakerOdds block -> {ticker: (ocId, price)} across its markets (boids ARE market tickers)."""
    out = {}
    for mk in (bo.get("markets") or {}).values():
        for oc_id, oc in (mk.get("outcomes") or {}).items():
            for pl in (oc.get("players") or {}).values():
                boid = str(pl.get("bookmakerOutcomeId") or "")
                if _TICKER_RE.match(boid):
                    try:
                        price = float(pl.get("price") or 0.0)
                    except (TypeError, ValueError):
                        price = 0.0
                    out[boid] = (str(oc_id), price)
    return out


async def main() -> int:
    ap = argparse.ArgumentParser(description="Fill cross_pairs.json via the oddspapi ticker join.")
    ap.add_argument("--pairs", default=str(Path(__file__).resolve().parent.parent / "cross_pairs.json"))
    ap.add_argument("--write", action="store_true", help="write the file (default = dry-run preview)")
    ap.add_argument("--both", action="store_true", help="fill BOTH 2-way mirror markets (default: one per event)")
    ap.add_argument("--sports", default="tennis,baseball,soccer,basketball,american-football,ice-hockey,mma",
                    help="oddspapi sport slugs to scan (comma-separated)")
    ap.add_argument("--days", type=float, default=9.0, help="fixtures window: now-6h .. now+days (default 9)")
    ap.add_argument("--price-tol", type=float, default=0.25,
                    help="max |kalshi - pinnacle| implied-prob gap before a join is REJECTED (default 0.25)")
    ap.add_argument("--max-requests", type=int, default=40,
                    help="hard billable-request ceiling for this run (default 40)")
    ap.add_argument("--sidecar", default=os.environ.get("HARDVEN_SIDECAR_URL", "http://127.0.0.1:8787"),
                    help="sidecar for league-URL backfill (optional; skipped if down)")
    args = ap.parse_args()

    pairs = json.loads(Path(args.pairs).read_text(encoding="utf-8"))
    targets = {e.get("kalshi_ticker"): e for e in pairs
               if e.get("kalshi_ticker") and not (e.get("hardven_yes_token") and e.get("hardven_no_token"))}
    already = sum(1 for e in pairs if e.get("hardven_yes_token") and e.get("hardven_no_token"))
    print(f"[PAIR-API] {len(pairs)} entries in {Path(args.pairs).name}: {already} already filled, "
          f"{len(targets)} to fill")
    if not targets:
        print("[PAIR-API] nothing to fill. Scaffold more first:  python ../pairHard.py --classic")
        return 0

    client = OddsPapiClient()
    if not client._key:
        print("[PAIR-API] no ODDSPAPI key in env/.env")
        return 1
    client._http = httpx.AsyncClient(timeout=25.0)
    try:
        await client._refresh_account()
        q0 = dict(client._acct or {})
        print(f"[PAIR-API] quota before: {q0.get('request_count')}/{q0.get('request_limit')}")

        # ── sports + fixtures discovery (bookmakers=kalshi narrows to Kalshi-covered fixtures) ──
        client._slugs = [s.strip().lower() for s in args.sports.split(",") if s.strip()]
        await client._resolve_sports()
        now = datetime.now(timezone.utc)
        frm = (now - timedelta(hours=6)).strftime("%Y-%m-%dT%H:%M:%SZ")
        to  = (now + timedelta(days=args.days)).strftime("%Y-%m-%dT%H:%M:%SZ")
        fixtures: dict[str, dict] = {}               # fixtureId -> {tid, start, sport}
        for sid in client._sport_ids:
            if client._billable >= args.max_requests:
                print("[PAIR-API] request ceiling hit during discovery - stopping early")
                break
            try:
                data = await client._get("/fixtures", {"sportId": sid, "from": frm, "to": to,
                                                       "bookmakers": "kalshi", "hasOdds": "true"})
            except RuntimeError as ex:
                # 404 FIXTURE_NOT_FOUND = "this sport has no kalshi-covered fixtures in the window" -- a
                # normal empty result (offseason etc.), not a failure. Anything else is real; re-raise.
                if "FIXTURE_NOT_FOUND" in str(ex):
                    continue
                raise
            for fx in (data or []):
                fid = fx.get("fixtureId")
                if fid:
                    fixtures[fid] = {"tid": fx.get("tournamentId"), "start": fx.get("startTime"),
                                     "sport": fx.get("sportName") or ""}
        by_tid: dict = defaultdict(list)
        for fid, m in fixtures.items():
            if m["tid"] is not None:
                by_tid[m["tid"]].append(fid)
        print(f"[PAIR-API] discovery: {len(fixtures)} kalshi-covered fixtures across {len(by_tid)} tournaments")

        # ── budget: rank tournaments by fixture count, drop the tail if 2*ceil(T/5) would bust the ceiling ──
        tids = sorted(by_tid, key=lambda t: -len(by_tid[t]))
        room = max(0, args.max_requests - client._billable)
        keep = min(len(tids), (room // 2) * 5)       # each chunk of 5 costs 2 requests (kalshi + pinnacle)
        if keep < len(tids):
            dropped = tids[keep:]
            print(f"[PAIR-API] BUDGET: keeping {keep}/{len(tids)} tournaments "
                  f"({sum(len(by_tid[t]) for t in dropped)} fixtures dropped; raise --max-requests to widen)")
            tids = tids[:keep]

        # ── odds: one call per book per chunk (API limit: exactly one bookmaker, max 5 tournamentIds) ──
        k_blocks: dict[str, dict] = {}
        p_blocks: dict[str, dict] = {}
        fx_meta:  dict[str, dict] = {}
        for i in range(0, len(tids), 5):
            chunk = ",".join(str(t) for t in tids[i:i + 5])
            for book, store in (("kalshi", k_blocks), ("pinnacle", p_blocks)):
                data = await client._get("/odds-by-tournaments",
                                         {"tournamentIds": chunk, "bookmakers": book, "verbosity": 3})
                if isinstance(data, dict):
                    data = [data] if "fixtureId" in data else list(data.values())
                for fx in (data or []):
                    fid = fx.get("fixtureId")
                    bo = (fx.get("bookmakerOdds") or {}).get(book)
                    if fid and bo:
                        store[fid] = bo
                        fx_meta.setdefault(fid, {"start": fx.get("startTime"),
                                                 "p1": fx.get("participant1Name"),
                                                 "p2": fx.get("participant2Name")})
        both = set(k_blocks) & set(p_blocks)
        print(f"[PAIR-API] odds: kalshi on {len(k_blocks)} fixtures, pinnacle on {len(p_blocks)}, BOTH on {len(both)}")

        # ── the join ──
        filled = price_rejected = 0
        done_events: set = set()
        found_tickers: set = set()
        for fid in both:
            pm = _pinnacle_moneyline(p_blocks[fid])
            if pm is None:
                continue
            lid, mid, p_outs = pm                                      # {ocId: (desig, price, limit)}
            k_ticks = _kalshi_tickers(k_blocks[fid])                   # {ticker: (ocId, price)}
            three_way = any(d == "draw" for d, _, _ in p_outs.values())
            tok = {oc: f"{lid}:{mid}:{d}" for oc, (d, _, _) in p_outs.items()}

            for ticker, (oc_id, k_price) in k_ticks.items():
                found_tickers.add(ticker)
                e = targets.get(ticker)
                if e is None or oc_id not in tok:
                    continue
                # price sanity: same vendor, same instant -- a wrong join or flipped orientation shows up as
                # a large implied-probability gap between the two books on the SAME outcome.
                p_price = p_outs[oc_id][1]
                if k_price > 1.0 and p_price > 1.0 and abs(1.0 / k_price - 1.0 / p_price) > args.price_tol:
                    price_rejected += 1
                    print(f"[PAIR-API] PRICE-REJECT {ticker}: kalshi {1.0/k_price:.2f} vs pinnacle "
                          f"{1.0/p_price:.2f} implied - join looks wrong, left unfilled")
                    continue
                if not three_way and not args.both and e.get("event_id") in done_events:
                    continue                                           # 2-way mirror: one entry per event
                other = next((tok[oc] for oc in tok if oc != oc_id), "")
                e["hardven_yes_token"] = tok[oc_id]
                e["hardven_no_token"]  = other if not three_way else (other or tok[oc_id])
                if three_way:
                    e["three_way"] = True                              # C# forces K_NO_P_YES on these
                e["oddspapi"] = True                                   # provenance: ticker-join, not name-match
                if fx_meta.get(fid, {}).get("start"):
                    e.setdefault("hardven_start_time", fx_meta[fid]["start"])
                done_events.add(e.get("event_id"))
                filled += 1
                tag = "  [3-way NO-only]" if three_way else ""
                print(f"[PAIR-API] {ticker:<40} -> YES {e['hardven_yes_token']} | NO {e['hardven_no_token']}{tag}")

        # ── league-URL backfill via the sidecar catalog (optional; the tab manager needs it) ──
        url_n = 0
        try:
            from pair_auto import fetch_catalog
            from pair_pinnacle import _league_url
            cat = fetch_catalog(args.sidecar, float(os.environ.get("HARDVEN_CATALOG_TIMEOUT", "60")))
            lid_meta = {}
            for s in cat:
                p = (s.get("selection_id") or "").split(":")
                if p and p[0] and p[0] not in lid_meta and (s.get("league") or s.get("sport")):
                    lid_meta[p[0]] = (s.get("sport") or "", s.get("league") or "")
            for e in pairs:
                t = e.get("hardven_yes_token") or ""
                if t.count(":") >= 2 and not e.get("hardven_league_url"):
                    meta = lid_meta.get(t.split(":")[0])
                    if meta and (u := _league_url(*meta)):
                        e["hardven_league_url"] = u
                        url_n += 1
        except Exception as ex:
            print(f"[PAIR-API] league-URL backfill skipped (sidecar catalog unavailable: {type(ex).__name__}) - "
                  "run pair_pinnacle.py later to tag URLs, or leave to its next scheduled run")

        missing = [t for t in targets if t not in found_tickers]
        await client._refresh_account()
        q1 = dict(client._acct or {})
        print(f"\n[PAIR-API] filled={filled}  price-rejected={price_rejected}  league-urls={url_n}  "
              f"not-in-oddspapi={len(missing)}")
        if missing:
            print(f"[PAIR-API] sample not found (settled / outside window / vendor lacks them - "
                  f"pair_pinnacle.py is the backstop): {missing[:6]}")
        print(f"[PAIR-API] this run used {client._billable} billable requests "
              f"(quota now {q1.get('request_count')}/{q1.get('request_limit')})")

        if args.write and filled:
            atomic_write_json(args.pairs, pairs)     # atomic -> the C# hot-reload never sees a partial file
            total = sum(1 for e in pairs if e.get("hardven_yes_token") and e.get("hardven_no_token"))
            print(f"[PAIR-API] wrote {total} filled pair(s) -> {args.pairs}")
        elif not args.write:
            print("[PAIR-API] dry-run (nothing written). Re-run with --write to save.")
        return 0
    finally:
        await client._http.aclose()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
