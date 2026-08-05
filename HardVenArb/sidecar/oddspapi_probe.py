"""
oddspapi_probe.py -- one-shot smoke test for the OddsPAPI integration. Run this FIRST when the API key lands.

    cd HardVenArb/sidecar
    python oddspapi_probe.py                 # full probe (~5 billable requests)
    python oddspapi_probe.py --account-only  # just the key + quota check (0 billable requests)

What it proves, in order:
  1. AUTH + QUOTA   /v4/account (unmetered): key valid, pinnacle in the subscription, requests remaining.
  2. THE JOIN       discovery maps our cross_pairs.json tokens' Pinnacle matchup ids to oddspapi fixtures.
                    Coverage % here is the vendor's real coverage of OUR slate, not their marketing number.
  3. THE ODDS       one odds-by-tournaments call; how many watched tokens resolve to a live quote.
  4. OUTCOME-ID CENSUS  prints every bookmakerOutcomeId format seen on one watched fixture. The docs only
                    SHOW totals ("3.5/under"); moneyline "home"/"away" is inferred from a doc string. This
                    census is how we confirm the real format before trusting the parser.

Reads ODDSPAPI_KEY from the environment / .env (never pass keys as argv -- they end up in shell history).
Budget: at most ~5 billable requests (sports, markets, fixtures x sports, odds x1). Quota is printed
before and after so you see exactly what it cost.
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

from env_util import load_dotenv_upwards

load_dotenv_upwards()

from agg_oddspapi import OddsPapiClient, _parse_token  # noqa: E402


def _load_watched_tokens(limit: int = 400) -> list[str]:
    """Both hardven tokens of every pair in cross_pairs.json -- the real slate, not a synthetic one."""
    path = Path(__file__).parent.parent / "cross_pairs.json"
    toks: list[str] = []
    try:
        for e in json.loads(path.read_text(encoding="utf-8")):
            for k in ("hardven_yes_token", "hardven_no_token"):
                t = e.get(k) or ""
                if t and _parse_token(t):
                    toks.append(t)
    except Exception as e:
        print(f"[PROBE] could not read cross_pairs.json ({type(e).__name__}: {e})")
    return toks[:limit]


def _quota_line(client: OddsPapiClient) -> str:
    q = client._acct or {}
    used, lim = q.get("request_count"), q.get("request_limit")
    return f"quota {used}/{lim}" if used is not None else "quota unknown"


async def main() -> int:
    account_only = "--account-only" in sys.argv
    client = OddsPapiClient()
    if not client._key:
        print("[PROBE] ODDSPAPI_KEY is not set (env or .env). Nothing to test.")
        return 1

    import httpx
    client._http = httpx.AsyncClient(timeout=20.0)
    try:
        # 1. AUTH + QUOTA (unmetered)
        print("=" * 70)
        print("1. ACCOUNT (unmetered)")
        await client._refresh_account()
        if not client._acct:
            print(f"[PROBE] /account FAILED: {client._last_error}")
            print("        Key invalid, or the REST auth style differs from what we send")
            print("        (we send both ?apiKey= and an x-api-key header).")
            return 1
        print(f"   ok: {_quota_line(client)}  ws_access={client._acct.get('websocket_access')}  "
              f"bookmakers={client._acct.get('bookmakers')}")
        if account_only:
            return 0

        watched = _load_watched_tokens()
        print(f"\n2. THE JOIN  ({len(watched)} tokens from cross_pairs.json)")
        if not watched:
            print("   no tokens to test -- run pairing first.")
            return 1
        await client.quotes(watched)              # registers them (no HTTP)

        await client._resolve_sports()
        await client._resolve_markets()
        await client._discover()

        mids = client._watched_mid_map()
        mapped = [m for m in mids if m in client._fixtures]
        print(f"   watched matchups: {len(mids)}   mapped to oddspapi fixtures: {len(mapped)} "
              f"({100.0 * len(mapped) / len(mids):.1f}%)")
        unmapped = [m for m in mids if m not in client._fixtures]
        if unmapped:
            print(f"   unmapped sample: {unmapped[:8]}")
            print("   (a match can be unmapped because it settled, is >7 days out, or the vendor lacks it --")
            print("    compare against how many of these are actually live on the bot before judging)")

        print("\n3. THE ODDS  (one odds-by-tournaments batch)")
        await client._poll_odds()
        served = await client.quotes(watched)
        print(f"   tokens with a live quote: {len(served)}/{len(watched)}")
        for tok, q in list(served.items())[:6]:
            age = f"{time.time() - q.changed_ts:.0f}s ago" if q.changed_ts else "n/a"
            lim = "none" if q.max_stake is None else f"{q.max_stake:g}"
            print(f"     {tok:<42} odds={q.decimal_odds:<7g} limit={lim:<8} "
                  f"status={q.status} live={int(q.live)} changed={age}")

        # 4. OUTCOME-ID CENSUS on one watched fixture (verifies the inferred formats)
        print("\n4. OUTCOME-ID CENSUS (verify 'home'/'away' moneyline format against reality)")
        target_mid = mapped[0] if mapped else None
        if target_mid:
            tid = client._fixtures[target_mid]["tid"]
            data = await client._get("/odds-by-tournaments", {
                "tournamentIds": str(tid), "bookmakers": client._bookmaker, "verbosity": 3})
            fixtures = data if isinstance(data, list) else (
                [data] if isinstance(data, dict) and "fixtureId" in data else list(data.values()))
            for fx in fixtures:
                bo = (fx.get("bookmakerOdds") or {}).get(client._bookmaker) or {}
                if str(bo.get("bookmakerFixtureId") or "") != target_mid:
                    continue
                for market_id, mk in (bo.get("markets") or {}).items():
                    bmid = mk.get("bookmakerMarketId") or ""
                    kind = client._market_kind(market_id, bmid)
                    ids = []
                    for oc in (mk.get("outcomes") or {}).values():
                        for pl in (oc.get("players") or {}).values():
                            ids.append(str(pl.get("bookmakerOutcomeId")))
                    print(f"     market {market_id} kind={kind or 'IGNORED':<9} path={bmid[:60]:<60} ids={ids[:6]}")
                break
        else:
            print("   (no mapped fixture to inspect)")

        await client._refresh_account()
        print(f"\nDONE. billable requests used by this probe: {client._billable}   ({_quota_line(client)})")
        print("If section 4 shows moneyline ids that are NOT 'home'/'away', the parser in agg_oddspapi.py")
        print("(_ingest_odds_payload expected-boid logic) needs adjusting before shadow mode means anything.")
        return 0
    finally:
        await client._http.aclose()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
