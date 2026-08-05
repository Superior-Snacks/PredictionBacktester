"""
OddsPapiClient -- AggregatorClient for oddspapi.io (v4 REST). See HardVenArb/OddspapiDocks.txt for the docs
this was written against, and AGGREGATOR.md for how it slots into the composite adapter.

THE JOIN (why this vendor needs no agg_map.json)
------------------------------------------------
oddspapi exposes Pinnacle's OWN ids: `externalProviders.pinnacleId` / `bookmakerFixtureId` is our token's
matchupId (`mid`), and each selection's `bookmakerOutcomeId` is Pinnacle's own designation -- "home"/"away"
for moneylines, "3.5/under" for totals. Our token `lid:mid:designation` therefore resolves mechanically:
find the fixture with pinnacleId == mid, take the fulltime market, match bookmakerOutcomeId. The lid rides
along from our own token; the vendor never needs to know it.

CALL SHAPE (why polling is 1 request, not 644)
----------------------------------------------
1 request = 1 call to a billable endpoint regardless of response size, and /v4/odds-by-tournaments takes a
comma-separated list of tournamentIds. So: a discovery pass maps watched mids -> oddspapi tournamentIds
(1 fixtures call per sport, every ODDSPAPI_DISCOVERY_MIN), then the poll loop fetches the WHOLE slate in one
odds-by-tournaments call per ODDSPAPI_POLL_SEC. Budget math at defaults (10s poll): ~8.6k requests/day --
check that against the plan's request_limit (the trial in the docs shows 500 TOTAL). /v4/account is unmetered
and is polled for quota; the client backs off 30 min on REQUEST_LIMIT_EXCEEDED. WS exists but is b2b-only.

FRESHNESS (the one place the generic contract needed vendor thinking)
---------------------------------------------------------------------
`changedAt` is a line-CHANGE clock, not a liveness heartbeat: a healthy stable pre-match line can sit
unchanged for 20+ minutes, so stamping ts=changedAt would age every quiet line out of the C# 30s gate (no
arbs, ever) while stamping fetch time blindly would let a frozen oddspapi scrape serve phantoms forever.
Same problem pinnacle_adapter solves with _feed_live(), same fix: serve ts = poll time WHILE the slate
heartbeat is alive (ANY watched selection's changedAt within ODDSPAPI_HEARTBEAT_TTL_SEC), else serve the
stale changedAt so everything ages out. A frozen scrape freezes every changedAt -> heartbeat dies -> safe.

VERIFY ON FIRST REAL CALL (documented shapes we could not see in the examples)
------------------------------------------------------------------------------
- moneyline bookmakerOutcomeId "home"/"away" is from the field's doc text; the examples only show totals and
  a numeric special-market id. oddspapi_probe.py prints a census of real outcome ids to confirm.
- spread outcome id format is inferred as "{points}/{side}" from the totals pattern; unverified.
- `limit` currency is unstated (HARDVEN_AGG_LIMITS=inner keeps it diagnostic-only).
- statusId enums conflict between two doc pages (1=Live vs 1=Scheduled); we treat now>=startTime as the
  reliable in-play signal and statusId==1 as merely suggestive.
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx

from agg_client import AggQuote, AggregatorClient


def _iso_ts(s) -> float:
    """Tolerant ISO-8601 -> epoch seconds (handles 'Z' and '+00:00'). 0.0 on anything unparseable."""
    if not s:
        return 0.0
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


def _parse_token(tok: str):
    """BOOK token -> (kind, lid, mid, points|None, side). Mirrors pinnacle_adapter._parse_sid (not imported:
    that module drags in the whole browser stack)."""
    p = tok.split(":")
    if len(p) == 3 and p[0] and p[1] and p[2] in ("home", "away", "draw"):
        return ("moneyline", p[0], p[1], None, p[2])
    if len(p) == 5 and p[0] and p[1] and p[2] in ("spread", "total"):
        try:
            pts = float(p[3])
        except ValueError:
            return None
        if (p[2] == "spread" and p[4] in ("home", "away")) or (p[2] == "total" and p[4] in ("over", "under")):
            return (p[2], p[0], p[1], pts, p[4])
    return None


def _fmt_pts(p: float) -> str:
    """8.5 -> '8.5', -2.0 -> '-2' -- matches the '3.5/under' style in bookmakerOutcomeId."""
    return f"{p:g}"


# Endpoint cooldowns from the docs (ms), enforced client-side so we never trip the server's limiter.
_COOLDOWN_SEC = {
    "/fixtures": 2.0, "/odds-by-tournaments": 1.0, "/odds": 0.5, "/fixture": 0.5,
    "/sports": 1.0, "/markets": 1.0, "/account": 1.0, "/bookmakers": 1.0, "/tournaments": 1.0,
}


class OddsPapiClient(AggregatorClient):
    name = "oddspapi"

    def __init__(self) -> None:
        self._base      = (os.environ.get("ODDSPAPI_BASE") or "https://api.oddspapi.io/v4").rstrip("/")
        # Both names accepted: ODDSPAPI_KEY (docs here) and ODDSPAPI_API_KEY (what the .env uses).
        self._key       = (os.environ.get("ODDSPAPI_KEY") or os.environ.get("ODDSPAPI_API_KEY") or "").strip()
        self._bookmaker = (os.environ.get("ODDSPAPI_BOOKMAKER") or "pinnacle").lower()
        self._poll_sec  = max(2.0, float(os.environ.get("ODDSPAPI_POLL_SEC", "60") or 60))
        self._disc_min  = max(5.0, float(os.environ.get("ODDSPAPI_DISCOVERY_MIN", "45") or 45))
        # HOT/COLD tiers -- the quota lever. A tournament is HOT when a watched PRE-LIVE fixture starts within
        # ODDSPAPI_HOT_HORIZON_H (bets happen near start); hot tournaments poll every ODDSPAPI_POLL_SEC, the
        # rest only every ODDSPAPI_COLD_POLL_SEC. Fixtures already STARTED are not polled at all (the bot is
        # pre-live only; their lines are dead to us).
        self._hot_h     = float(os.environ.get("ODDSPAPI_HOT_HORIZON_H", "8") or 8)
        self._cold_poll = max(self._poll_sec, float(os.environ.get("ODDSPAPI_COLD_POLL_SEC", "600") or 600))
        self._last_cold = 0.0
        self._n_hot = self._n_cold = 0                # last-poll tier sizes, surfaced in health()
        # Optional pause hook: the composite adapter points this at the inner book's session-readiness so a
        # scheduled dark window (browser closed, bot can't bet anyway) stops burning quota. Returns True = pause.
        self.pause_check = None
        self._hb_ttl    = float(os.environ.get("ODDSPAPI_HEARTBEAT_TTL_SEC", "900") or 900)
        # API hard limit, discovered empirically 2026-08-05: >5 tournamentIds -> 400 INVALID_PARAMETER
        # "Please provide a maximum of 5 tournament IDs". Each chunk is 1 billable request, so the number of
        # watched tournaments DIVIDED BY 5 is the real per-poll quota cost.
        self._tids_per_call = max(1, int(os.environ.get("ODDSPAPI_TIDS_PER_CALL", "5") or 5))
        # Only poll tournaments that hold a watched match starting inside this horizon. The executor's
        # pre-live gate only fires on matches settling soon anyway, so polling a tournament whose next
        # watched match is 5 days out is pure quota burn.
        self._poll_horizon_h = float(os.environ.get("ODDSPAPI_POLL_HORIZON_H", "48") or 48)
        # Default = every sport the pairing pipeline covers, so a token from ANY filled pair can be mapped and
        # polled. An out-of-season sport costs 1 billed 404 per discovery cycle; trim the list to save it.
        self._slugs     = [s.strip().lower() for s in
                           (os.environ.get("ODDSPAPI_SPORTS")
                            or "tennis,baseball,soccer,basketball,american-football,mma,boxing,cricket,aussie-rules"
                            ).split(",") if s.strip()]

        self._http: Optional[httpx.AsyncClient] = None
        self._task: Optional[asyncio.Task] = None
        self._stopping = False

        self._watched: dict[str, float] = {}          # token -> last-requested ts (forgotten after 1h)
        self._fixtures: dict[str, dict] = {}          # mid -> {tid, start_ts, status_id}
        self._sport_ids: list[int] = []
        self._ml_market_ids: set[str] = set()         # oddspapi marketIds judged fulltime-moneyline (metadata path)
        self._cache: dict[str, AggQuote] = {}         # token -> quote (ts filled at serve time, hb-gated)
        self._hb_last_change = 0.0                    # newest changedAt seen across the whole watched slate
        self._last_call: dict[str, float] = {}        # endpoint -> last call ts (cooldown spacing)
        self._last_disc = 0.0

        self._billable = 0
        self._errors = 0
        self._last_error = ""
        self._last_poll_ms = 0.0
        self._acct: dict = {}                         # latest /account subscription snapshot (quota)
        self._backoff_until = 0.0
        self._err_streak = 0

    # ── lifecycle ─────────────────────────────────────────────────────────────
    async def startup(self) -> None:
        if not self._key:
            print("[ODDSPAPI] ODDSPAPI_KEY is not set - client is IDLE (quotes() returns {}, which the "
                  "adapter treats as 'no vendor opinion'). Set the key and restart to activate.")
            return
        self._http = httpx.AsyncClient(timeout=15.0)
        await self._refresh_account()                 # unmetered; validates the key before anything billable
        self._task = asyncio.create_task(self._run())
        print(f"[ODDSPAPI] started: bookmaker={self._bookmaker} sports={','.join(self._slugs)} "
              f"poll={self._poll_sec:.0f}s discovery={self._disc_min:.0f}min")

    async def shutdown(self) -> None:
        self._stopping = True
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        if self._http:
            await self._http.aclose()

    # ── HTTP with cooldown spacing + quota accounting ─────────────────────────
    async def _get(self, path: str, params: dict, billable: bool = True):
        assert self._http is not None
        # +0.3s over the documented cooldown: the server measures the gap on ITS side, so spacing measured
        # from our request start can land a few ms early (observed: 429 RATE_LIMITED retryMs=6).
        cd = _COOLDOWN_SEC.get(path, 1.0) + 0.3
        # Auth style is not shown for REST in the docs (the WS uses ?apiKey=). Send both the query param and
        # a key header; trim to whichever the probe proves once a real key answers.
        p = dict(params or {})
        p["apiKey"] = self._key
        for attempt in range(4):
            wait = self._last_call.get(path, 0.0) + cd - time.time()
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_call[path] = time.time()
            r = await self._http.get(f"{self._base}{path}", params=p, headers={"x-api-key": self._key})
            if r.status_code != 429:
                break
            body = self._redact(r)
            if "REQUEST_LIMIT_EXCEEDED" in body:
                self._backoff_until = time.time() + 1800
                print("[ODDSPAPI] MONTHLY QUOTA EXHAUSTED (429 REQUEST_LIMIT_EXCEEDED) - "
                      "pausing all polling for 30 min. Quotes will age out and the books will clear.")
                raise RuntimeError(f"429 {body}")
            # Cooldown 429 (RATE_LIMITED): rejected BEFORE the endpoint -> not billed. Honor retryMs + retry.
            retry_s = 0.5
            try:
                retry_s = max(float(json.loads(r.text)["error"].get("retryMs", 500)) / 1000.0, 0.05)
            except Exception:
                pass
            await asyncio.sleep(retry_s + 0.25)
        if billable:
            self._billable += 1                       # 4xx/5xx count against quota too, per the docs
        if r.status_code == 429:
            raise RuntimeError(f"429 after retries: {self._redact(r)}")
        if r.status_code >= 400:
            # NEVER raise httpx's own error: its message embeds the full request URL INCLUDING the apiKey
            # query param, which then lands in logs/tracebacks. Raise a redacted error with the response
            # body instead (the body is what actually says what was wrong).
            raise RuntimeError(f"HTTP {r.status_code} on {path}: {self._redact(r)}")
        return r.json()

    def _redact(self, r) -> str:
        """First 300 chars of a response body with the API key scrubbed wherever it appears."""
        try:
            body = r.text[:300]
        except Exception:
            return ""
        return body.replace(self._key, "***KEY***") if self._key else body

    async def _refresh_account(self) -> None:
        """Quota snapshot from the unmetered /account endpoint; never raises."""
        try:
            data = await self._get("/account", {}, billable=False)
            subs = data.get("subscriptions") or []
            cur = next((s for s in subs if s.get("is_active")), subs[0] if subs else {})
            self._acct = {"request_count": cur.get("request_count"), "request_limit": cur.get("request_limit"),
                          "websocket_access": cur.get("websocket_access"),
                          "bookmakers": sorted((cur.get("bookmakers") or {}).keys())}
            if self._bookmaker not in (cur.get("bookmakers") or {}):
                print(f"[ODDSPAPI] WARNING: bookmaker '{self._bookmaker}' is NOT in this subscription "
                      f"({self._acct['bookmakers']}) - odds responses will be empty.")
            lim, used = cur.get("request_limit") or 0, cur.get("request_count") or 0
            if lim and used / lim >= 0.9:
                print(f"[ODDSPAPI] WARNING: quota {used}/{lim} used ({100.0*used/lim:.0f}%).")
        except Exception as e:
            self._last_error = f"account: {type(e).__name__}: {e}"

    # ── background loop: resolve -> discover -> poll ──────────────────────────
    async def _run(self) -> None:
        while not self._stopping:
            try:
                now = time.time()
                if now < self._backoff_until:
                    await asyncio.sleep(15)
                    continue
                if self.pause_check is not None and self.pause_check():
                    await asyncio.sleep(30)           # dark window: the book can't bet, so don't spend quota
                    continue
                if not self._sport_ids:
                    await self._resolve_sports()
                if not self._ml_market_ids:
                    await self._resolve_markets()     # tolerated failure: the /0/moneyline path filter still works
                if now - self._last_disc > self._disc_min * 60:
                    await self._discover()
                    await self._refresh_account()     # unmetered; keeps the quota numbers in health() current
                await self._poll_odds()
                self._err_streak = 0
            except asyncio.CancelledError:
                return
            except Exception as e:
                self._errors += 1
                self._err_streak += 1
                self._last_error = f"{type(e).__name__}: {e}"
                if self._err_streak in (1, 5) or self._err_streak % 50 == 0:
                    print(f"[ODDSPAPI] loop error x{self._err_streak}: {self._last_error}")
                # 4xx errors are BILLED, so an error loop burns quota: back off progressively to 5 min.
                await asyncio.sleep(min(self._poll_sec * (2 ** min(self._err_streak, 5)), 300))
                continue
            await asyncio.sleep(self._poll_sec)

    async def _resolve_sports(self) -> None:
        data = await self._get("/sports", {})
        by_slug = {str(s.get("slug", "")).lower(): s.get("sportId") for s in (data or [])}
        self._sport_ids = [by_slug[s] for s in self._slugs if s in by_slug and by_slug[s] is not None]
        missing = [s for s in self._slugs if s not in by_slug]
        if missing:
            print(f"[ODDSPAPI] sports not found: {missing} (have: {sorted(by_slug)[:20]}...)")
        if not self._sport_ids:
            raise RuntimeError(f"no sportIds resolved for {self._slugs}")

    async def _resolve_markets(self) -> None:
        """Metadata path for identifying fulltime moneyline markets, complementing the bookmakerMarketId
        '/0/moneyline' suffix filter. Either alone admitting a market is enough; both are precise."""
        try:
            data = await self._get("/markets", {})
        except Exception as e:
            print(f"[ODDSPAPI] /markets failed ({type(e).__name__}) - relying on the path-suffix filter only.")
            self._ml_market_ids = {"__none__"}        # sentinel: tried, do not retry every loop
            return
        ids = set()
        for m in (data or []):
            if m.get("playerProp"):
                continue
            if str(m.get("period", "")).lower().replace(" ", "").replace("_", "") not in ("fulltime", "match"):
                continue
            if m.get("handicap") not in (0, 0.0, None):
                continue
            mt = str(m.get("marketType", "")).lower().replace(" ", "").replace("-", "")
            nm = str(m.get("marketName", "")).lower()
            if mt in ("moneyline", "h2h", "matchwinner", "1x2") or nm in ("money line", "moneyline", "match winner"):
                ids.add(str(m.get("marketId")))
        self._ml_market_ids = ids or {"__none__"}
        print(f"[ODDSPAPI] moneyline market metadata: {len(ids)} marketIds identified")

    async def _discover(self) -> None:
        """mid -> (tournamentId, startTime) map via /fixtures, one call per sport. This is what lets the poll
        loop batch the whole slate into a single odds-by-tournaments request."""
        self._last_disc = time.time()
        now = datetime.now(timezone.utc)
        frm = (now - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        to  = (now + timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")   # sportId+from/to must span < 10 days
        found, no_pid = 0, 0
        for sid in self._sport_ids:
            try:
                data = await self._get("/fixtures", {
                    "sportId": sid, "from": frm, "to": to,
                    "bookmakers": self._bookmaker, "hasOdds": "true",
                })
            except RuntimeError as ex:
                if "FIXTURE_NOT_FOUND" in str(ex):
                    continue                          # out-of-season sport: a normal empty result, not an error
                raise
            for fx in (data or []):
                pid = (fx.get("externalProviders") or {}).get("pinnacleId")
                if pid is None:
                    no_pid += 1
                    continue
                self._fixtures[str(pid)] = {
                    "tid": fx.get("tournamentId"),
                    "start_ts": _iso_ts(fx.get("startTime")),
                    "status_id": fx.get("statusId"),
                }
                found += 1
        print(f"[ODDSPAPI] discovery: {found} fixtures mapped ({no_pid} without a pinnacleId), "
              f"{len(self._watched_mid_map())} watched mids")

    def _watched_mid_map(self) -> dict[str, list]:
        """{mid: [(token, parsed), ...]} for still-fresh watched tokens (forgotten after 1h unrequested)."""
        cut = time.time() - 3600
        out: dict[str, list] = {}
        for tok, ts in list(self._watched.items()):
            if ts < cut:
                del self._watched[tok]
                continue
            p = _parse_token(tok)
            if p:
                out.setdefault(p[2], []).append((tok, p))
        return out

    async def _poll_odds(self) -> None:
        now = time.time()
        horizon = now + self._poll_horizon_h * 3600.0
        hot_edge = now + self._hot_h * 3600.0
        hot: set = set()
        cold: set = set()
        for mid, _toks in self._watched_mid_map().items():
            fx = self._fixtures.get(mid)
            if fx is None or fx.get("tid") is None:
                continue
            start = fx.get("start_ts") or 0.0
            if start and start <= now:
                continue                              # already started: pre-live only, its line is dead to us
            if start and start > horizon:
                continue                              # too far out to act on
            (hot if (start and start <= hot_edge) else cold).add(fx["tid"])
        cold -= hot                                   # a tournament with any near game polls at the hot cadence
        tids = sorted(hot)
        if cold and (now - self._last_cold) >= self._cold_poll:
            tids += sorted(cold)
            self._last_cold = now
        self._n_hot, self._n_cold = len(hot), len(cold)
        if not tids:
            return                                    # nothing pollable right now -> no billable call
        t0 = time.time()
        # Hard API limit: 5 tournamentIds per call, each chunk = 1 billable request.
        for i in range(0, len(tids), self._tids_per_call):
            data = await self._get("/odds-by-tournaments", {
                "tournamentIds": ",".join(str(t) for t in tids[i:i + self._tids_per_call]),
                "bookmakers": self._bookmaker, "verbosity": 3,
            })
            self._ingest_odds_payload(data)
        self._last_poll_ms = (time.time() - t0) * 1000.0

    # ── payload -> cache (pure function of the response; unit-testable without HTTP) ──
    def _market_kind(self, market_id, bmid: str) -> Optional[str]:
        """'moneyline' | 'total' | 'spread' for FULLTIME straight markets, None otherwise. Primary signal is
        Pinnacle's own market path ('line/<sport>/<league>/<mid>/<x>/0/moneyline' -- the '0' is period 0 =
        full match, so period markets like 1st-set winner are rejected here). Metadata ids are the backup."""
        parts = (bmid or "").split("/")
        if len(parts) >= 2 and parts[-2] == "0":
            last = parts[-1].lower()
            if last == "moneyline":
                return "moneyline"
            if last in ("totals", "total"):
                return "total"
            if last in ("spread", "spreads", "handicap"):
                return "spread"
        if str(market_id) in self._ml_market_ids:
            return "moneyline"
        return None

    def _ingest_odds_payload(self, payload) -> int:
        """Parse an odds-by-tournaments (or single /odds) response into the token-keyed cache."""
        if payload is None:
            return 0
        if isinstance(payload, dict):
            fixtures = list(payload.values()) if "fixtureId" not in payload else [payload]
        else:
            fixtures = list(payload)
        watched = self._watched_mid_map()
        poll_ts = time.time()
        n = 0
        for fx in fixtures:
            if not isinstance(fx, dict):
                continue
            bo = (fx.get("bookmakerOdds") or {}).get(self._bookmaker)
            if not bo:
                continue
            mid = str(bo.get("bookmakerFixtureId")
                      or (fx.get("externalProviders") or {}).get("pinnacleId") or "")
            tokens_here = watched.get(mid)
            if not tokens_here:
                continue
            disc = self._fixtures.get(mid, {})
            start_ts = _iso_ts(fx.get("startTime")) or disc.get("start_ts") or 0.0
            status_id = fx.get("statusId", disc.get("status_id"))
            suspended = bool(bo.get("suspended"))

            # Index every fulltime straight selection: (kind, bookmakerOutcomeId) -> (entry, marketActive)
            index: dict[tuple, tuple] = {}
            for market_id, mk in (bo.get("markets") or {}).items():
                kind = self._market_kind(market_id, mk.get("bookmakerMarketId") or "")
                if kind is None:
                    continue
                mk_active = mk.get("marketActive", True)
                for oc in (mk.get("outcomes") or {}).values():
                    for pl in (oc.get("players") or {}).values():
                        if pl.get("playerName"):
                            continue                  # player props are never our token
                        boid = str(pl.get("bookmakerOutcomeId") or "").lower()
                        if boid:
                            index[(kind, boid)] = (pl, mk_active)

            for tok, parsed in tokens_here:
                kind, _lid, _mid, pts, side = parsed
                boid = side if kind == "moneyline" else f"{_fmt_pts(pts)}/{side}"
                hit = index.get((kind, boid))
                if hit is None:
                    continue                          # not offered -> token silently ages out (honest)
                pl, mk_active = hit
                try:
                    price = float(pl.get("price") or 0.0)
                except (TypeError, ValueError):
                    continue
                if price <= 1.0:
                    continue
                changed = max(_iso_ts(pl.get("changedAt")), _iso_ts(pl.get("bookmakerChangedAt")))
                if changed > self._hb_last_change:
                    self._hb_last_change = changed    # slate heartbeat: ANY line moving proves the scrape lives
                limit = pl.get("limit")
                self._cache[tok] = AggQuote(
                    token=tok, decimal_odds=price, book=self._bookmaker,
                    max_stake=float(limit) if limit is not None else None,
                    ts=0.0,                           # filled at serve time (heartbeat-gated poll stamp)
                    fetched_ts=poll_ts, changed_ts=changed,
                    # statusId enums conflict between doc pages; now>=startTime is the reliable in-play signal.
                    live=bool(status_id == 1 or (start_ts and poll_ts >= start_ts)),
                    status="open" if (pl.get("active") and mk_active and not suspended) else "suspended",
                    cutoff=start_ts,
                )
                n += 1
        return n

    # ── the AggregatorClient contract ─────────────────────────────────────────
    async def quotes(self, tokens: list[str]) -> dict[str, AggQuote]:
        now = time.time()
        for t in tokens:
            self._watched[t] = now                    # registration drives discovery + the tournament batch
        alive = self._hb_last_change > 0 and (now - self._hb_last_change) <= self._hb_ttl
        out: dict[str, AggQuote] = {}
        for t in tokens:
            q = self._cache.get(t)
            if q is None:
                continue
            # Freshness contract (see module docstring): poll stamp while the slate heartbeat lives, else the
            # stale changedAt so a frozen scrape ages out of the C# gate instead of serving phantoms.
            ts = q.fetched_ts if alive else min(q.fetched_ts, self._hb_last_change or q.changed_ts)
            out[t] = dataclasses.replace(q, ts=ts)
        return out

    def health(self) -> dict:
        watched = self._watched_mid_map()
        unmapped = [m for m in watched if m not in self._fixtures]
        return {
            "key_set": bool(self._key), "poll_sec": self._poll_sec,
            "hot_tournaments": self._n_hot, "cold_tournaments": self._n_cold,
            "cold_poll_sec": self._cold_poll, "hot_horizon_h": self._hot_h,
            "billable_session": self._billable, "errors": self._errors, "last_error": self._last_error,
            "last_poll_ms": round(self._last_poll_ms, 1),
            "watched_tokens": len(self._watched), "watched_mids": len(watched),
            "unmapped_mids": len(unmapped), "unmapped_sample": unmapped[:5],
            "cached_quotes": len(self._cache),
            "hb_age_sec": round(time.time() - self._hb_last_change, 1) if self._hb_last_change else None,
            "quota": self._acct or None,
            "backoff_until": self._backoff_until or None,
        }
