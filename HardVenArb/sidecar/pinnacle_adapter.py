"""
pinnacle_adapter.py — Pinnacle (pinnacle.bet) odds via its private "Arcadia" JSON API.

WHY THIS IS A CLEAN httpx ADAPTER (no Playwright, unlike bookmaker.eu): api.arcadia.pinnacle.com answers a
plain HTTP client — a bare `curl` returns data, so there is NO Cloudflare browser-fingerprint wall. Auth is
three REPLAYED headers captured from a logged-in browser session:
  x-api-key     — the site's STATIC public client key (shared by all users; embedded in their JS).
  x-session     — your PER-USER logged-in token. THIS unlocks REAL-TIME odds (a guest gets DELAYED prices).
                  It is a live credential — it expires/rotates → refresh PINNACLE_SESSION when calls 401.
  x-device-uuid — a client-generated device id.
Pinnacle CLOSED its official betting API, so this replays the website's own private API with a scraped
session token. The traffic is therefore tied to your account → OPERATE GENTLY (account-ban risk): SERIAL
jittered requests, conservative cadence, hard back-off on 429. Concurrency is deliberately NOT used here
(that is what got the bookmaker account banned).

ENDPOINTS (base https://api.arcadia.pinnacle.com/0.1):
  GET /sports                              -> sports [{id,name,primaryMarketType,matchupCount,...}]
  GET /leagues/{leagueId}/matchups         -> games [{id, participants[{alignment,name,order}], league,
                                               startTime(ISO-UTC), status, periods, ...}]  (NO odds)
  GET /leagues/{leagueId}/markets/straight -> ALL straight markets for the league in ONE call:
       [{ matchupId, period, type("moneyline"|"total"|"spread"), key("s;{period};{m|ou|s}"),
          limits[{amount,type:"maxRiskStake"}], cutoffAt,
          prices[{participantId, price(AMERICAN int), points?}], version? }]
       2-way game moneyline = type=="moneyline" & period 0 & exactly 2 prices (3 = 3-way w/ draw);
       many prices = an outright/futures → skip.

SELECTION-ID (the pinnacle/hardven token in cross_pairs.json):
  "{leagueId}:{matchupId}:{participantId}" — leagueId picks the markets/straight poll, matchupId the
  market, participantId the exact price. Map Kalshi YES player -> the participantId that IS that player;
  catalog() pairs by participant ORDER (prices[i] ↔ matchup.participants order i; home=0, away=1).

CONFIG (env): PINNACLE_SESSION (required), PINNACLE_DEVICE_UUID (required), PINNACLE_API_KEY (defaults to
  the observed static site key), PINNACLE_CATALOG_LEAGUES (CSV league ids for catalog()/pairing),
  PINNACLE_REFRESH_SEC (bg refresh cadence, default 5 — GENTLE), PINNACLE_ACTIVE_TTL_SEC (default 120),
  PINNACLE_REQUEST_JITTER_MS (default 250 — random gap between serial league fetches; less robotic).
"""
from __future__ import annotations

import asyncio
import os
import random
import time
from typing import Optional

import httpx

from book_adapter import BookAdapter, BetResult, CatalogEntry, Selection

BASE = os.environ.get("PINNACLE_API_BASE", "https://api.arcadia.pinnacle.com/0.1")
# x-api-key is the website's STATIC public client key (not a per-user secret); default to the observed value,
# override via env if Pinnacle rotates it.
DEFAULT_API_KEY = "CmX2KcMrXuFmNg6YFbmTxE0y9CIrOi0R"
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/149.0.0.0 Safari/537.36")


def american_to_decimal(american) -> float:
    """American odds -> decimal. +135 -> 2.35, -159 -> 1.629. 0/invalid -> 0.0."""
    try:
        a = float(american)
    except (TypeError, ValueError):
        return 0.0
    if a == 0:
        return 0.0
    return 1.0 + (a / 100.0 if a > 0 else 100.0 / abs(a))


def _max_risk(limits) -> float:
    """Pull the maxRiskStake (max cash you may stake) from a market's limits array."""
    for lim in limits or []:
        if lim.get("type") == "maxRiskStake":
            try:
                return float(lim.get("amount") or 0)
            except (TypeError, ValueError):
                return 0.0
    return 0.0


class PinnacleAdapter(BookAdapter):
    name = "pinnacle"

    def __init__(self) -> None:
        self._api_key = os.environ.get("PINNACLE_API_KEY", DEFAULT_API_KEY)
        self._session = os.environ.get("PINNACLE_SESSION", "")
        self._device = os.environ.get("PINNACLE_DEVICE_UUID", "")
        self._client: Optional[httpx.AsyncClient] = None
        self._cache: dict[str, Selection] = {}            # "{lid}:{mid}:{pid}" -> Selection
        self._active_leagues: dict[str, float] = {}       # leagueId -> unix ts last requested via /odds
        self._refresh_task: Optional[asyncio.Task] = None
        self._refresh_sec = float(os.environ.get("PINNACLE_REFRESH_SEC", "5"))
        self._active_ttl = float(os.environ.get("PINNACLE_ACTIVE_TTL_SEC", "120"))
        self._jitter_ms = float(os.environ.get("PINNACLE_REQUEST_JITTER_MS", "250"))
        self._catalog_leagues = [x.strip() for x in
                                 os.environ.get("PINNACLE_CATALOG_LEAGUES", "").split(",") if x.strip()]
        self._backoff_sec = 0.0
        self._rate_limited = False                        # any 429 this refresh cycle? (drives backoff)
        self._rate_limit_total = 0
        self._last_hb = 0.0

    # ── lifecycle ────────────────────────────────────────────────────────────
    async def startup(self) -> None:
        if not self._session or not self._device:
            print("[PINNACLE] WARNING: PINNACLE_SESSION / PINNACLE_DEVICE_UUID not set — real-time odds need a "
                  "logged-in session token. Set them from a browser capture (x-session / x-device-uuid).")
        self._client = httpx.AsyncClient(
            headers={
                "accept": "application/json",
                "content-type": "application/json",
                "origin": "https://www.pinnacle.bet",
                "referer": "https://www.pinnacle.bet/",
                "user-agent": USER_AGENT,
                "x-api-key": self._api_key,
                "x-device-uuid": self._device,
                "x-session": self._session,
            },
            timeout=15.0,
        )
        self._refresh_task = asyncio.create_task(self._refresh_loop())
        print(f"[PINNACLE] ready (httpx, no browser). Gentle background refresh every {self._refresh_sec:g}s, "
              "SERIAL per-league + jitter (account-safe). Odds id = '<leagueId>:<matchupId>:<participantId>'.")

    async def shutdown(self) -> None:
        if self._refresh_task and not self._refresh_task.done():
            self._refresh_task.cancel()
        if self._client:
            try:
                await self._client.aclose()
            except Exception:
                pass

    # ── HTTP ─────────────────────────────────────────────────────────────────
    async def _get(self, path: str):
        if not self._client:
            return None
        try:
            r = await self._client.get(BASE + path)
        except Exception as ex:
            print(f"[PINNACLE] GET {path} error: {type(ex).__name__}: {ex}")
            return None
        sc = r.status_code
        if sc == 429:
            self._rate_limited = True
            self._rate_limit_total += 1
            print(f"[PINNACLE] *** RATE LIMITED (429) on {path} *** (cumulative {self._rate_limit_total}) — "
                  "backing off. Permanent fix: RAISE PINNACLE_REFRESH_SEC or narrow the leagues.")
            return None
        if sc in (401, 403):
            print(f"[PINNACLE] AUTH {sc} on {path} — x-session likely expired; log in again and refresh "
                  "PINNACLE_SESSION (re-capture the token).")
            return None
        if sc != 200:
            print(f"[PINNACLE] GET {path} HTTP {sc}")
            return None
        try:
            return r.json()
        except Exception:
            return None

    # ── M0: odds (decoupled — instant cache read; the bg loop keeps it warm) ──
    @staticmethod
    def _parse_sid(sid: str):
        parts = sid.split(":")
        if len(parts) == 3 and all(parts):
            return parts[0], parts[1], parts[2]   # leagueId, matchupId, participantId (all str)
        return None

    async def odds(self, selection_ids: list[str]) -> dict[str, Selection]:
        # Record the leagues these selections need (the bg loop keeps them warm); return cache instantly.
        now = time.time()
        for sid in selection_ids:
            p = self._parse_sid(sid)
            if p:
                self._active_leagues[p[0]] = now
        return {sid: self._cache[sid] for sid in selection_ids if sid in self._cache}

    async def _refresh_loop(self) -> None:
        """Keep the cache warm for leagues requested in the last PINNACLE_ACTIVE_TTL_SEC, fetched ONE AT A
        TIME with jitter (gentle/account-safe — NO concurrency). Auto-backs-off on 429."""
        while True:
            try:
                await asyncio.sleep(self._backoff_sec if self._backoff_sec > 0 else self._refresh_sec)
            except asyncio.CancelledError:
                break
            now = time.time()
            leagues = [lg for lg, ts in self._active_leagues.items() if now - ts <= self._active_ttl]
            if not leagues:
                continue
            self._rate_limited = False
            t0 = time.perf_counter()
            try:
                for i, lid in enumerate(leagues):
                    await self._refresh_league(lid)
                    if i + 1 < len(leagues) and self._jitter_ms > 0:
                        await asyncio.sleep(random.uniform(0, self._jitter_ms / 1000.0))  # de-robotize
            except Exception as ex:
                print(f"[PINNACLE] refresh error: {type(ex).__name__}: {ex}")
            dt = time.perf_counter() - t0
            # auto-backoff on 429 (×2, cap 60s; reset when clean)
            if self._rate_limited:
                self._backoff_sec = min(max(self._backoff_sec, self._refresh_sec) * 2, 60.0)
                print(f"[PINNACLE] rate-limited → backing off to {self._backoff_sec:.0f}s "
                      f"(normal {self._refresh_sec:g}s).")
            elif self._backoff_sec > 0:
                print(f"[PINNACLE] rate limit cleared — resuming {self._refresh_sec:g}s refresh.")
                self._backoff_sec = 0.0
            if now - self._last_hb >= 30:
                self._last_hb = now
                rl = f"  429s: {self._rate_limit_total}" if self._rate_limit_total else ""
                print(f"[PINNACLE] refresh: {len(leagues)} leagues (serial) in {dt:.2f}s "
                      f"(cache={len(self._cache)} sel){rl}")

    async def _refresh_league(self, lid: str) -> None:
        markets = await self._get(f"/leagues/{lid}/markets/straight")
        if not markets:
            return
        now = time.time()
        for mk in markets:
            if mk.get("type") != "moneyline" or mk.get("period") != 0:
                continue
            prices = mk.get("prices") or []
            if not (2 <= len(prices) <= 3):       # 2-way / 3-way GAME; skip outrights (many prices)
                continue
            mid = mk.get("matchupId")
            max_stake = _max_risk(mk.get("limits"))
            for pr in prices:
                dec = american_to_decimal(pr.get("price"))
                if dec <= 1.0:
                    continue
                token = f"{lid}:{mid}:{pr.get('participantId')}"
                # status="open": a price present = tradeable. A suspended/removed price simply drops out of
                # the response → its cache entry's ts ages → the C# freshness gate clears it (no phantom).
                self._cache[token] = Selection(token, decimal_odds=dec, max_stake=max_stake,
                                               status="open", ts=now)

    # ── pairing catalog (set PINNACLE_CATALOG_LEAGUES; auto-discovery of leagues-per-sport = TODO) ──
    async def catalog(self) -> list[CatalogEntry]:
        if not self._catalog_leagues:
            print("[PINNACLE] catalog(): set PINNACLE_CATALOG_LEAGUES (CSV of league ids, e.g. 246 for MLB) to "
                  "enumerate games for pairing. (Auto-discovery via /sports/{id}/leagues is a TODO.)")
            return []
        out: list[CatalogEntry] = []
        for lid in self._catalog_leagues:
            matchups = await self._get(f"/leagues/{lid}/matchups") or []
            markets = await self._get(f"/leagues/{lid}/markets/straight") or []
            if self._jitter_ms > 0:
                await asyncio.sleep(random.uniform(0, self._jitter_ms / 1000.0))
            # index period-0 moneyline prices (participant-ordered) by matchupId — 2-way only for the join
            ml: dict = {}
            for mk in markets:
                if mk.get("type") == "moneyline" and mk.get("period") == 0:
                    prices = mk.get("prices") or []
                    if len(prices) == 2:           # auto-pair 2-way only; 3-way (draw) mapping is a TODO
                        ml[mk.get("matchupId")] = prices
            for m in matchups:
                mid = m.get("id")
                prices = ml.get(mid)
                if not prices:
                    continue
                parts = sorted((m.get("participants") or []), key=lambda p: p.get("order", 0))
                if len(parts) != 2:
                    continue
                lg = m.get("league") or {}
                sport = ((lg.get("sport") or {}).get("name")) or ""
                league_name = lg.get("name") or str(lid)
                home = next((p.get("name", "") for p in parts if p.get("alignment") == "home"), "")
                away = next((p.get("name", "") for p in parts if p.get("alignment") == "away"), "")
                event = f"{home} vs {away}"
                # ASSUMPTION (verify on a known game): prices are returned in participant ORDER, so
                # prices[0] ↔ parts[0] (home), prices[1] ↔ parts[1] (away). pair_auto's price-consistency
                # gate catches a swap, but get this right.
                for i, p in enumerate(parts):
                    out.append(CatalogEntry(
                        selection_id=f"{lid}:{mid}:{prices[i].get('participantId')}",
                        sport=sport, league=league_name, event=event, market="moneyline",
                        selection_name=p.get("name", ""), start_time=m.get("startTime"),
                        three_way=False))
        return out

    # ── M1 (later): betting + wallet confirmation ──
    async def balance(self) -> float:
        return 0.0  # TODO(M1): read account balance.

    async def place_bet(self, selection_id: str, stake: float, max_odds: float) -> BetResult:
        # TODO(M1): Pinnacle's bet-placement endpoint (irreversible). Accept only if odds <= max_odds.
        return BetResult(accepted=False, reason="place_bet not implemented (M1)")

    async def open_bets(self) -> list[dict]:
        return []  # TODO(M1)

    async def bet(self, bet_id: str) -> Optional[dict]:
        return None  # TODO(M1)
