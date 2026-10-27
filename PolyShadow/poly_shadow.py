#!/usr/bin/env python
"""Polymarket SHADOW telemetry - would the Kalshi +EV strategy work on Polymarket?

OBSERVE-ONLY. Watches Polymarket's order books for the matches the Kalshi bot already pairs with Pinnacle, values
them against the same Pinnacle fair price with the bot's own gates, and logs what the bot WOULD have bought: the
same decision names, the same follow-up checkpoints, the same column meanings where they carry over. It holds no
credentials and has no order path.

TOTALLY SEPARATE FROM THE KALSHI BOT: its own process, its own folder (PolyShadow/data), no code shared with
KalshiEvBot, nothing the bot reads. What it touches of the running stack, and why that is safe:
  * HardVenArb/cross_pairs.json - read only.
  * the sidecar's GET /odds - read only, NEVER fresh=1 (that one is a request to Pinnacle). /odds is not free of
    side effects, though: every call stamps its leagues ACTIVE for PINNACLE_ACTIVE_TTL_SEC (180s), and an active
    league gets a Pinnacle WS subscription and a REST seed. So this asks only for selections the bot is polling at
    that moment, and only while the bot is running - it can never open a league, or keep one open, on its own.
  * Polymarket's public Gamma API, CLOB /book and market WebSocket - unauthenticated reads.

Polymarket facts this relies on (docs.polymarket.com, read 2026-10-27):
  * sports taker fee = shares x rate x p(1-p), rate 0.05 since 2026-07-10, per market in `feeSchedule`
    {rate, exponent}; makers pay nothing.
  * sports markets carry a TAKER DELAY (`secondsDelay`, 1s): a marketable order waits that long before it can
    match, so every candidate that clears is re-read one delay later and its would-be order walked through THAT
    book (Shadow.after_delay) - a price seen is not a price taken.
  * markets are v1 (`clobTokenIds`) or v2 (`positionIds`); the market WS takes either as `assets_ids`.
  * the market WS needs a text "PING" every 10s; `custom_feature_enabled` adds best_bid_ask / market_resolved.
  * each outcome's book already contains the mirrored orders of the other outcome (A's ask = 1 - B's bid).

Run:   python PolyShadow/poly_shadow.py            keeps running; Ctrl+C stops it
       python PolyShadow/poly_shadow.py --check    discover + pair + print, then exit (no sockets, no sidecar)
Read:  python PolyShadow/poly_shadow_report.py
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import datetime as dt
import json
import math
import os
import re
import sys
import time
import unicodedata
from collections import Counter, defaultdict, deque
from pathlib import Path

import httpx
import psutil
import websockets

try:
    from rapidfuzz import fuzz
except Exception:            # optional: only the last-resort name score uses it
    fuzz = None

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")   # accented player names on a Windows console
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

# Polymarket tags to search per sport. Tennis has one umbrella tag; 61 of 67 basketball leagues carry the
# Basketball tag (28) and the six that do not are listed by their own; same idea for football and baseball.
DISCOVERY_TAGS = {
    "tennis":     ["864"],
    "basketball": ["28", "745", "102669", "100254", "100149", "101178", "105577"],
    "football":   ["450", "100351", "1186"],
    "baseball":   ["678", "100381"],
}


# ── config ────────────────────────────────────────────────────────────────────────────────────────────────

# The bot's knobs this mirrors, read from the same .env so one edit moves both. A WHITELIST: that file also holds
# credentials, and none of them is any business of this process.
_ENV_KEYS = (
    "EV_MIN", "EV_PRESCREEN_SLACK", "EV_MIN_PRICE", "EV_MAX_PRICE", "EV_MAX_DISAGREE", "EV_DEVIG",
    "EV_KINETIC_WINDOW_SEC", "EV_KINETIC_MIN_RISE", "EV_REQUIRE_PINNACLE_RISING", "EV_REQUIRE_DEVIG_AGREE",
    "EV_REQUIRE_IN_PLAY", "EV_LED_MOVE_MIN", "EV_LED_RATIO", "EV_MAX_WS_REST_GAP",
    "EV_ORACLE_MAX_AGE_MS", "EV_ORACLE_MAX_AGE_INPLAY_MS", "EV_RECHECK_COOLDOWN_MS", "EV_FOLLOWUP_SEC",
    "EV_SNAPSHOT_MIN", "EV_LIVE_SPORTS", "EV_SHADOW_NEAR_ONLY", "EV_SHADOW_LEAD_MIN", "EV_SHADOW_MAX_LIVE_MIN",
    "EV_PAIRS_FILE", "HARDVEN_SIDECAR_URL",
)


def read_env() -> dict:
    vals = {}
    p = ROOT / ".env"
    if p.exists():
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
            s = line.strip()
            if s.startswith("export "):
                s = s[7:].strip()
            k, sep, v = s.partition("=")
            k = k.strip()
            if sep and k in _ENV_KEYS:
                vals[k] = re.split(r"\s+#", v, maxsplit=1)[0].strip().strip('"').strip("'")
    for k in _ENV_KEYS:                       # a real environment variable wins
        if os.environ.get(k):
            vals[k] = os.environ[k]
    return vals


class Cfg:
    def __init__(self, env: dict):
        def f(k, d):
            try:
                return float(env.get(k, d))
            except ValueError:
                return float(d)
        # the bot's gates - defaults are the bot's own (EvEvaluator.cs EvConfig)
        self.ev_min = f("EV_MIN", 0.01)
        self.prescreen_slack = f("EV_PRESCREEN_SLACK", 0.02)
        self.min_price = f("EV_MIN_PRICE", 0.20)
        self.max_price = f("EV_MAX_PRICE", 0.80)
        self.max_disagree = f("EV_MAX_DISAGREE", 0.15)
        self.devig = (env.get("EV_DEVIG") or "proportional").lower()
        self.kinetic_window = f("EV_KINETIC_WINDOW_SEC", 5)
        self.kinetic_min_rise = f("EV_KINETIC_MIN_RISE", 0.005)
        self.require_rising = f("EV_REQUIRE_PINNACLE_RISING", 1) != 0
        self.require_devig_agree = f("EV_REQUIRE_DEVIG_AGREE", 1) != 0
        self.require_in_play = f("EV_REQUIRE_IN_PLAY", 1) != 0
        self.led_move_min = f("EV_LED_MOVE_MIN", 0.03)
        self.led_ratio = f("EV_LED_RATIO", 0.34)
        self.max_source_gap = f("EV_MAX_WS_REST_GAP", 0.03)
        self.max_age_pre = f("EV_ORACLE_MAX_AGE_MS", 30_000) / 1000
        self.max_age_live = f("EV_ORACLE_MAX_AGE_INPLAY_MS", 5_000) / 1000
        self.cooldown = f("EV_RECHECK_COOLDOWN_MS", 15_000) / 1000
        self.followups = sorted(x for x in (_num(v) for v in (env.get("EV_FOLLOWUP_SEC")
                                or "5,10,20,40,60,120,180,300").split(",")) if x and x > 0)
        self.snapshot_min = f("EV_SNAPSHOT_MIN", 5)
        self.live_sports = {s.strip().lower() for s in (env.get("EV_LIVE_SPORTS") or "tennis").split(",")
                            if s.strip()} or {"tennis"}
        self.shadow_near_only = f("EV_SHADOW_NEAR_ONLY", 1) > 0.5
        self.shadow_lead = f("EV_SHADOW_LEAD_MIN", 30)
        self.shadow_max_live = f("EV_SHADOW_MAX_LIVE_MIN", 300)
        self.sidecar = (env.get("HARDVEN_SIDECAR_URL") or "http://127.0.0.1:8787").rstrip("/")
        self.pairs_file = Path(env.get("EV_PAIRS_FILE") or ROOT / "HardVenArb" / "cross_pairs.json")
        # this process's own knobs (POLY_*; environment only)
        self.poll_ms = float(os.environ.get("POLY_ORACLE_POLL_MS", 500))
        self.discover_min = float(os.environ.get("POLY_DISCOVER_MIN", 15))
        self.lead_min = float(os.environ.get("POLY_LEAD_MIN", 30))           # subscribe from start - this
        self.tennis_max_live_h = float(os.environ.get("POLY_TENNIS_MAX_LIVE_H", 8))
        self.require_bot = os.environ.get("POLY_REQUIRE_BOT", "1") != "0"
        self.data = Path(os.environ.get("POLY_DIR") or ROOT / "PolyShadow" / "data")
        self.deep = 15.0                                                      # the bot's "deep" cut, shares
        # the would-be order walked through the book one taker delay later: the bot's deep cut, and about what
        # its $5-15 Kelly stakes buy at mid prices
        self.order_shares = float(os.environ.get("POLY_ORDER_SHARES", 15))


def _num(s):
    try:
        return float(str(s).strip())
    except ValueError:
        return None


# ── small helpers ─────────────────────────────────────────────────────────────────────────────────────────

def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def iso(t: dt.datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def parse_time(s) -> dt.datetime | None:
    """'2026-10-27T15:00:00Z', '2026-10-27 15:00:00+00' (Gamma's gameStartTime) -> aware UTC."""
    if not s:
        return None
    s = str(s).strip().replace(" ", "T", 1).replace("Z", "+00:00")
    if re.search(r"[+-]\d\d$", s):
        s += ":00"
    try:
        t = dt.datetime.fromisoformat(s)
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)


def norm(s: str) -> str:
    """Lowercase, strip accents and punctuation, collapse spaces (pair_auto._norm)."""
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
    return " ".join(re.sub(r"[^a-z0-9 ]", " ", s.lower()).split())


_PARTICLES = {"del", "dela", "della", "van", "von", "der", "den", "dos", "das", "santos",
              "junior", "senior", "sousa", "silva"}


def name_score(a: str, b: str) -> int:
    """How strongly two normalised names claim to be one competitor, 0-100. The scale of pair_auto's
    _score_book_team, so a pairing here means what a pairing there means: the SURNAME carries the identity, and a
    shared particle or first name decides nothing ('Maria Fernanda Lopes' vs 'Maria Sousa Salazar')."""
    if not a or not b:
        return 0
    if a == b:
        return 100
    if f" {a} " in f" {b} " or f" {b} " in f" {a} ":      # whole-word containment: 'chiefs' in 'kansas city chiefs'
        return 95
    at, bt = a.split(), b.split()
    shared = set(at) & set(bt)
    if shared:
        strong = {t for t in shared if len(t) >= 4 and t not in _PARTICLES}
        if not strong:
            return 62
        if at[-1] == bt[-1]:
            return 88
        return 85 if len(strong) >= 2 else 70
    return int(fuzz.token_set_ratio(a, b)) if fuzz is not None else 0


def fee_per_share(p: float, rate: float, exp: float) -> float:
    """Polymarket taker fee per share: rate x (p(1-p))^exponent (docs: Trading > Fees, exponent from feeSchedule)."""
    if rate <= 0 or not (0 < p < 1):
        return 0.0
    return rate * (p * (1.0 - p)) ** exp


def break_even_limit(p_true: float, ev_min: float, rate: float, exp: float) -> float:
    """The highest price that still clears ev_min after the fee - EvMath.BreakEvenLimit for Polymarket's fee
    shape. price + fee(price) only rises with price, so a bisection is exact enough."""
    t = p_true - ev_min
    if t <= 0:
        return 0.0
    lo, hi = 0.0, min(t, 0.9999)
    if hi + fee_per_share(hi, rate, exp) <= t:
        return hi
    for _ in range(50):
        mid = (lo + hi) / 2
        if mid + fee_per_share(mid, rate, exp) <= t:
            lo = mid
        else:
            hi = mid
    return lo


def walk(asks: list[tuple[float, float]], limit: float, shares: float, rate: float, exp: float):
    """Fill `shares` from ascending asks up to `limit`: (shares got, VWAP, fee per share). A FAK order at `limit`."""
    got = cost = fees = 0.0
    for p, s in asks:
        if p > limit + 1e-9 or got >= shares:
            break
        take = min(s, shares - got)
        got += take
        cost += take * p
        fees += take * fee_per_share(p, rate, exp)
    return got, (cost / got if got else None), (fees / got if got else None)


def devig_prop(odds: list[float]):
    q = [1.0 / o for o in odds]
    s = sum(q)
    return [x / s for x in q], s - 1.0, 0.0


def devig_shin(odds: list[float]):
    """DeVig.ShinN, line for line."""
    q = [1.0 / o for o in odds]
    s = sum(q)
    if s <= 1.0:
        return [x / s for x in q], s - 1.0, 0.0

    def pi(qi, z):
        return (math.sqrt(z * z + 4.0 * (1.0 - z) * qi * qi / s) - z) / (2.0 * (1.0 - z))

    def g(z):
        return math.sqrt(s) - 1.0 if z <= 0 else sum(pi(qi, z) for qi in q) - 1.0

    lo, hi = 0.0, 0.999
    if g(hi) > 0:
        return [x / s for x in q], s - 1.0, 0.0
    for _ in range(100):
        mid = 0.5 * (lo + hi)
        if g(mid) > 0:
            lo = mid
        else:
            hi = mid
    z = 0.5 * (lo + hi)
    return [pi(qi, z) for qi in q], s - 1.0, z


# Kalshi series -> sport, as KalshiEvBot/EvSports.cs files them (the explicit team-sport map, then the fallbacks).
_BASKETBALL = {
    "KXNBAGAME", "KXNCAABGAME", "KXWNBAGAME", "KXEUROLEAGUEGAME", "KXNCAAMBGAME", "KXNCAAWBGAME", "KXEUROCUPGAME",
    "KXACBGAME", "KXBSLGAME", "KXLNBELITEGAME", "KXLNBELITE2GAME", "KXVTBGAME", "KXSLBGAME", "KXCZENBLGAME",
    "KXAUTBSLGAME", "KXKORISLIIGAGAME", "KXGBLGAME", "KXBBLGAME", "KXBBSERIEAGAME", "KXBBSERIEBGAME", "KXPLKGAME",
    "KXISLGAME", "KXJBLEAGUEGAME", "KXCBAGAME", "KXKBLGAME", "KXNBLGAME", "KXNZNBLGAME", "KXPBAGAME", "KXNBBGAME",
    "KXLBPGAME", "KXSPBGAME", "KXLNBPGAME", "KXBSNGAME", "KXCEBLGAME", "KXVBAGAME", "KXBIG3GAME",
}
_HOCKEY = {"KXNHLGAME", "KXKHLGAME", "KXSHLGAME", "KXLIIGAGAME", "KXDELGAME", "KXAHLGAME", "KXNCAAHGAME",
           "KXNCAAHOCKEYGAME", "KXEUCHLGAME", "KXIIHFGAME"}
_FOOTBALL = {"KXNFLGAME", "KXNCAAFGAME", "KXCFLGAME"}


def sport_of(ticker: str) -> str:
    series = (ticker or "").split("-")[0].upper()
    if series in _BASKETBALL:
        return "basketball"
    if series in _HOCKEY:
        return "hockey"
    if series in _FOOTBALL:
        return "football"
    if series == "KXEBASKETBALLGAME":
        return "other"
    if any(x in series for x in ("ATP", "WTA", "ITF")):
        return "tennis"
    if any(x in series for x in ("MLB", "KBO", "NPB", "LMB")):
        return "baseball"
    if "NFL" in series or "NCAAF" in series:
        return "football"
    if "GAME" in series:
        return "soccer"
    return "other"


def fmt(v) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, float):
        if not math.isfinite(v):
            return ""
        s = f"{v:.6f}".rstrip("0").rstrip(".")
        return "0" if s in ("-0", "") else s
    return str(v)


class DailyCsv:
    """One file per UTC day under the data folder. A header that differs from today's existing file (a column was
    added) continues in <name>_v2.csv rather than mixing two layouts in one file."""

    def __init__(self, directory: Path, prefix: str, columns: list[str]):
        self.dir, self.prefix, self.cols = directory, prefix, columns
        self._day = None
        self._f = None
        self._w = None
        self.rows = 0

    def _open(self, day: str):
        self.close()
        header = ",".join(self.cols)
        for n in range(1, 50):
            path = self.dir / (f"{self.prefix}_{day}.csv" if n == 1 else f"{self.prefix}_{day}_v{n}.csv")
            if path.exists() and path.stat().st_size > 0:
                with open(path, encoding="utf-8", newline="") as fh:
                    if fh.readline().rstrip("\r\n") != header:
                        continue
                new = False
            else:
                new = True
            self._f = open(path, "a", encoding="utf-8", newline="")
            self._w = csv.writer(self._f)
            if new:
                self._w.writerow(self.cols)
            self._day = day
            self.path = path
            return
        raise RuntimeError(f"no free file name for {self.prefix}_{day}")

    def write(self, row: dict):
        day = utcnow().strftime("%Y%m%d")
        if day != self._day:
            self._open(day)
        self._w.writerow([fmt(row.get(c)) for c in self.cols])
        self.rows += 1

    def flush(self):
        if self._f:
            self._f.flush()

    def close(self):
        if self._f:
            self._f.close()
        self._f = self._w = None


# ── Pinnacle side: the bot's pairs and the sidecar's quotes ──────────────────────────────────────────────

class Matchup:
    """One Pinnacle two-way from cross_pairs.json: both selection tokens, both names, the listed start."""
    __slots__ = ("mid", "sport", "home_tok", "away_tok", "home_name", "away_name", "start", "tickers")

    def __init__(self, mid, sport, start):
        self.mid, self.sport, self.start = mid, sport, start
        self.home_tok = self.away_tok = self.home_name = self.away_name = ""
        self.tickers: set[str] = set()


def load_matchups(path: Path) -> dict[str, Matchup]:
    """Moneyline two-ways only, through the bot's own drop rule: the two markets of one Kalshi event must name ONE
    Pinnacle matchup on OPPOSITE sides. The bot drops both when they contradict and so never polls them - and a
    selection the bot does not poll is one this process must never ask the sidecar for."""
    rows = json.loads(path.read_text(encoding="utf-8"))
    by_event = defaultdict(list)
    for r in rows:
        if r.get("market_type") or r.get("three_way"):
            continue
        yt, nt = (r.get("hardven_yes_token") or "").split(":"), (r.get("hardven_no_token") or "").split(":")
        if len(yt) != 3 or len(nt) != 3 or yt[1] != nt[1] or {yt[2], nt[2]} != {"home", "away"}:
            continue
        if not (r.get("hardven_yes_name") and r.get("hardven_no_name")):
            continue
        by_event[r.get("event_id") or r.get("kalshi_ticker")].append(r)
    out: dict[str, Matchup] = {}
    for rs in by_event.values():
        segs = [r["hardven_yes_token"].split(":") for r in rs]
        if len(rs) == 2 and (segs[0][1] != segs[1][1] or segs[0][2] == segs[1][2]):
            continue
        for r in rs:
            ys = r["hardven_yes_token"].split(":")
            m = out.get(ys[1])
            if m is None:
                m = out[ys[1]] = Matchup(ys[1], sport_of(r.get("kalshi_ticker")), parse_time(r.get("hardven_start_time")))
            mine, other = (norm(r["hardven_yes_name"]), norm(r["hardven_no_name"]))
            if ys[2] == "home":
                m.home_tok, m.away_tok, m.home_name, m.away_name = r["hardven_yes_token"], r["hardven_no_token"], mine, other
            else:
                m.away_tok, m.home_tok, m.away_name, m.home_name = r["hardven_yes_token"], r["hardven_no_token"], mine, other
            m.tickers.add(r.get("kalshi_ticker") or "")
    return out


def bot_polls(m: Matchup, cfg: Cfg, now: dt.datetime) -> bool:
    """Is the bot polling this matchup's selections right now? EvSports.OracleTokens: every live-sport moneyline,
    and a shadow sport from EV_SHADOW_LEAD_MIN before its listed start to EV_SHADOW_MAX_LIVE_MIN after. Two minutes
    are shaved off each end of the shadow window, so a small clock or reload difference can only make THIS process
    ask for less than the bot, never more."""
    if m.sport in cfg.live_sports or not cfg.shadow_near_only or m.start is None:
        return True
    lo = m.start - dt.timedelta(minutes=cfg.shadow_lead - 2)
    hi = m.start + dt.timedelta(minutes=cfg.shadow_max_live - 2)
    return lo <= now <= hi


class Oracle:
    """A thin reader of the sidecar's /odds - the same request the bot makes, never with fresh=1."""

    def __init__(self, cfg: Cfg):
        self.cfg = cfg
        self.quotes: dict[str, dict] = {}
        self.feed_policy = False
        self.feed_alive = True
        self.session_ready = True
        self.poll_ms = deque(maxlen=120)
        self.polls = self.errors = 0
        self.interval = cfg.poll_ms / 1000.0
        self.last_error = ""

    async def poll(self, http: httpx.AsyncClient, tokens: list[str]) -> bool:
        t0 = time.perf_counter()
        ok = False
        for i in range(0, len(tokens), 200):
            try:
                r = await http.get(self.cfg.sidecar + "/odds", params={"selections": ",".join(tokens[i:i + 200])},
                                   timeout=5.0)
                if r.status_code != 200:
                    self.errors += 1
                    self.last_error = f"HTTP {r.status_code}"
                    continue
                self._apply(r.json())
                ok = True
            except Exception as e:                 # a sidecar that is down fails every poll; say so in the status
                self.errors += 1
                self.last_error = f"{type(e).__name__}: {e}"[:120]
        if ok:
            self.polls += 1
            self.poll_ms.append((time.perf_counter() - t0) * 1000)
        return ok

    def _apply(self, body: dict):
        now = time.time()
        if isinstance(body.get("session_ready"), bool):
            self.session_ready = body["session_ready"]
        fe = body.get("feed")
        if isinstance(fe, dict):
            self.feed_policy = str(fe.get("quote_age_policy") or "").lower() == "feed"
            self.feed_alive = fe.get("alive") is not False
        for tok, s in (body.get("selections") or {}).items():
            odds = float(s.get("decimal_odds") or 0)
            status = s.get("status") or "open"
            is_open = status == "open" and odds > 1.0
            prev = self.quotes.get(tok)
            q = {"odds": odds, "status": status, "live": s.get("live") is True, "ts": float(s.get("ts") or 0),
                 "wv": s.get("wv") is not False, "depth": float(s.get("max_contracts") or 0), "recv": now,
                 "open": is_open, "last_open_odds": prev["last_open_odds"] if prev else 0.0,
                 "odds_changed": prev["odds_changed"] if prev else 0.0,
                 "status_changed": prev["status_changed"] if prev else 0.0}
            # When did the OPEN price last move, and when did it last flip open/suspended - as PinnacleOracle
            # tracks them. A suspension is a status change, not a move.
            if is_open:
                if prev and prev["last_open_odds"] > 0 and odds != prev["last_open_odds"]:
                    q["odds_changed"] = now
                q["last_open_odds"] = odds
            if prev and prev["open"] != is_open:
                q["status_changed"] = now
            self.quotes[tok] = q

    def fresh(self, q: dict, now: float) -> bool:
        """PinnacleOracle.Fresh, with one adjustment for polling more slowly than the bot: the quote's age is
        judged at the moment WE received it, and our copy may then be at most 1.5 polls old. The bot polls every
        250ms; holding a 500ms poll to the bot's 1s in-play gate measured at evaluation time would call quotes
        stale that the bot, a poll later, would have called fresh."""
        if self.feed_policy:
            return self.feed_alive and q["ts"] > 0
        lim = self.cfg.max_age_live if q["live"] else self.cfg.max_age_pre
        return (q["recv"] - q["ts"]) <= lim and (now - q["recv"]) <= max(0.75, 1.5 * self.interval)


# ── Polymarket side ──────────────────────────────────────────────────────────────────────────────────────

class Book:
    __slots__ = ("bids", "asks", "recv", "tick", "last_trade", "ask_hist", "seeded")

    def __init__(self):
        self.bids: dict[float, float] = {}
        self.asks: dict[float, float] = {}
        self.recv = 0.0
        self.tick = None
        self.last_trade = None
        self.ask_hist: deque = deque(maxlen=200)      # (time, best ask) on every change, for the move columns
        # Only a full snapshot (a `book` frame or a REST seed) makes a book readable. Deltas applied to an empty
        # dict would read their own few levels as the whole book - a "best ask" that is really just the last
        # level somebody touched.
        self.seeded = False

    def load(self, bids, asks, now: float):
        self.bids = {float(x["price"]): float(x["size"]) for x in bids or []}
        self.asks = {float(x["price"]): float(x["size"]) for x in asks or []}
        self.recv = now
        self.seeded = True

    def best_ask(self):
        return min(self.asks) if self.asks else None

    def best_bid(self):
        return max(self.bids) if self.bids else None

    def ask_move(self, now: float, secs: float):
        """How far the best ask moved over the last `secs` (KalshiMove2sCents/10s for this venue)."""
        if not self.ask_hist:
            return None
        cur = self.ask_hist[-1][1]
        before = None
        for t, a in reversed(self.ask_hist):
            if t <= now - secs:
                before = a
                break
        if before is None:
            before = self.ask_hist[0][1] if self.ask_hist[0][0] <= now - secs else None
        return None if before is None or cur is None else cur - before


class PmMarket:
    def __init__(self, mk: dict, ev: dict, sport: str):
        self.id = str(mk.get("id"))
        self.slug = mk.get("slug") or ""
        self.condition = mk.get("conditionId") or ""
        self.question = mk.get("question") or ev.get("title") or ""
        self.event_id = str(ev.get("id") or "")
        self.sport = sport
        self.start = parse_time(ev.get("startTime")) or parse_time(mk.get("gameStartTime"))
        if (mk.get("version") or "v1") == "v2":
            self.assets = [str(x) for x in (mk.get("positionIds") or [])]
        else:
            self.assets = [str(x) for x in json.loads(mk.get("clobTokenIds") or "[]")]
        self.outcomes = json.loads(mk.get("outcomes") or "[]")
        fs = mk.get("feeSchedule") or {}
        self.fee_rate = float(fs.get("rate") or 0) if mk.get("feesEnabled") else 0.0
        self.fee_exp = float(fs.get("exponent") or 1)
        self.tick = float(mk.get("orderPriceMinTickSize") or 0.01)
        self.min_size = float(mk.get("orderMinSize") or 0)
        self.delay = float(mk.get("secondsDelay") or 0)
        self.accepting = bool(mk.get("acceptingOrders"))
        self.matchup: Matchup | None = None
        self.legs: list[str] = []          # per outcome: "home" | "away" (the Pinnacle selection it pays on)
        self.score = 0
        self.check = "unchecked"          # unchecked | ok | suspect - the live price check, see Shadow.price_check
        self.resolved = None


class PmSide:
    """One Polymarket outcome = one thing we could buy."""

    def __init__(self, market: PmMarket, idx: int):
        self.market = market
        self.idx = idx
        self.asset = market.assets[idx]
        self.outcome = market.outcomes[idx]
        self.leg = market.legs[idx]
        self.last_rest = -1e9
        self.last_seen = None              # (ask, p_true) at the last REST check - the led-regime baseline
        self.track: deque = deque()        # (time, p_true) samples, >= 250ms apart, kept 30s

    def add_sample(self, now: float, p: float):
        if self.track and now - self.track[-1][0] < 0.25:
            return
        self.track.append((now, p))
        while self.track and self.track[0][0] < now - 30.0:
            self.track.popleft()

    def rise(self, now: float, window: float):
        """PTrueTrack.TryRise: latest minus the last sample at or before now - window; None = no history yet."""
        at = None
        for t, p in reversed(self.track):
            if t <= now - window:
                at = p
                break
        return None if at is None else self.track[-1][1] - at


def pair_markets(events: list[dict], sport: str, matchups: dict[str, Matchup], log) -> list[PmMarket]:
    """Polymarket moneylines -> the bot's Pinnacle matchups, by BOTH names and the start time.

    An orientation is accepted only when it is clearly better than the swapped one, and a match only when it is
    clearly better than the runner-up matchup: an inverted pair puts both of our 'sides' on one player and still
    looks like a perfectly sane price, so ambiguity is a refusal, never a coin flip (pair_auto's lesson). The live
    price check in Shadow.price_check is the second line."""
    tol_h = 12 if sport == "tennis" else 4
    cands = [m for m in matchups.values() if m.sport == sport and m.home_name and m.away_name]
    out = []
    for ev in events:
        if "doubles" in (ev.get("seriesSlug") or "").lower():
            continue
        for mk in ev.get("markets") or []:
            if mk.get("sportsMarketType") != "moneyline" or mk.get("closed"):
                continue
            pm = PmMarket(mk, ev, sport)
            if len(pm.outcomes) != 2 or len(pm.assets) != 2 or any("/" in o for o in pm.outcomes):
                continue
            o0, o1 = norm(pm.outcomes[0]), norm(pm.outcomes[1])
            scored = []
            for m in cands:
                if pm.start and m.start and abs((pm.start - m.start).total_seconds()) > tol_h * 3600:
                    continue
                direct = (name_score(o0, m.home_name), name_score(o1, m.away_name))
                swap = (name_score(o0, m.away_name), name_score(o1, m.home_name))
                if min(direct) >= min(swap):
                    best, other, legs = min(direct), min(swap), ["home", "away"]
                else:
                    best, other, legs = min(swap), min(direct), ["away", "home"]
                if best >= 70 and best - other >= 12:
                    scored.append((best, sum(direct) if legs[0] == "home" else sum(swap), m, legs))
            if not scored:
                continue
            scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
            if len(scored) > 1 and scored[0][0] - scored[1][0] < 10:
                log(f"[pair] AMBIGUOUS, skipped: {pm.question} - {scored[0][2].home_name} v {scored[0][2].away_name} "
                    f"or {scored[1][2].home_name} v {scored[1][2].away_name}")
                continue
            pm.score, _, pm.matchup, pm.legs = scored[0]
            out.append(pm)
    return out


# ── the shadow ───────────────────────────────────────────────────────────────────────────────────────────

TEL_COLS = [
    "Timestamp", "Ticker", "Side", "AssetId", "MarketId", "EventId", "EventTitle", "Sport", "StartUtc",
    "KalshiTickers", "PinMatchup", "PinLeg", "InPlay", "PinOddsMine", "PinOddsOther", "PinSumS", "Vig", "ShinZ",
    "PTrueProp", "PTrueShin", "PTrueUsed", "DeVigMethod", "OracleAgeMs", "OracleCacheAgeMs", "OracleDepth",
    "WsAsk", "WsAskSize", "WsBid", "WsBookAgeMs", "RestAsk", "RestAskSize", "RestBid", "RestMs", "WsRestGapCents",
    "FeeRate", "FeePerShare", "CostPerShare", "EvProp", "EvShin", "Ev", "EvWs", "LimitPrice", "DepthToLimit",
    "CapacityUsd", "InPriceWindow", "Decision", "MoveRegime", "PinnacleRiseCents", "DeVigAgree", "PairCheck",
    "TickSize", "MinSize", "TakerDelaySec", "PinChangeAgeMs", "PinStatusAgeMs", "PolyMove2sCents",
    "PolyMove10sCents", "LastTrade",
    # THE TAKER DELAY (see Shadow.after_delay): the would-be order, with no delay and then one delay later
    "OrderShares", "FillSharesNow", "VwapNow", "AfterDelay", "DelayMs", "DelayedAsk", "DelayedAskSize",
    "DelayedBid", "DelayAskMoveCents", "DelayedDepthAtSeenAsk", "DelayedDepthToLimit", "DelayedFillShares",
    "DelayedVwap", "DelayedFeePerShare", "DelayedPTrue", "DelayedPinState", "DelayedEvAtEntryP",
    "DelayedEvAtFillP",
]
FU_COLS = [
    "CheckUtc", "EntryUtc", "AgeSec", "Ticker", "Side", "AssetId", "Sport", "Decision", "MoveRegime", "EntryAsk",
    "EntryPTrue", "EntryEvCents", "NowAsk", "NowPTrue", "PolyDriftCents", "PinnacleDriftCents", "GapEntryCents",
    "GapNowCents", "GapClosedCents", "WhoClosed", "NowEvCents", "NowBid", "EntryDepth", "PinState", "PinLegs",
    "NowPTrueRaw", "BookAgeMs",
]
SNAP_COLS = [
    "Timestamp", "Ticker", "Side", "AssetId", "Sport", "StartUtc", "KalshiTickers", "InPlay", "PinState",
    "PTrueUsed", "PTrueRaw", "PinOddsMine", "PinOddsOther", "Ask", "AskSize", "Bid", "BidSize", "BookAgeMs",
    "LastTrade", "PairCheck",
]


class Shadow:
    def __init__(self, cfg: Cfg, verbose: bool = False):
        self.cfg = cfg
        self.verbose = verbose
        cfg.data.mkdir(parents=True, exist_ok=True)
        self.tel = DailyCsv(cfg.data, "PolyShadowTelemetry", TEL_COLS)
        self.fu = DailyCsv(cfg.data, "PolyShadowFollowUp", FU_COLS)
        self.snap = DailyCsv(cfg.data, "PolyShadowSnap", SNAP_COLS)
        self.oracle = Oracle(cfg)
        self.matchups: dict[str, Matchup] = {}
        self.pairs_mtime = 0.0
        self.markets: dict[str, PmMarket] = {}          # market id -> market (every paired one)
        self.sides: dict[str, PmSide] = {}              # asset -> side (watched markets only)
        self.books: dict[str, Book] = {}
        self.want: set[str] = set()                     # assets the WebSocket should carry
        self.subscribed: set[str] = set()
        self.ws = None
        self.ws_last_rx = 0.0
        self.bot_alive = not cfg.require_bot
        self.stats = Counter()
        self.msgs = 0
        self.bytes = 0
        self.rest_sem = asyncio.Semaphore(4)
        # Its own gate, so a busy moment on the first read can never push the second one past its due time.
        self.delay_sem = asyncio.Semaphore(4)
        self.tasks: set = set()
        self.stopping = False
        self.http: httpx.AsyncClient | None = None
        self.started = time.time()
        self._last_status = (time.time(), 0, 0)
        self.proc = psutil.Process()
        self.proc.cpu_percent(None)

    # ── logging ──
    def log(self, msg: str):
        line = f"[{utcnow():%H:%M:%S}] {msg}"
        print(line, flush=True)
        try:
            with open(self.cfg.data / f"poly_shadow_{utcnow():%Y%m%d}.log", "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError:
            pass

    # ── pairs + discovery ──
    def reload_pairs(self) -> bool:
        try:
            mt = self.cfg.pairs_file.stat().st_mtime
        except OSError:
            return False
        if mt == self.pairs_mtime:
            return False
        try:
            self.matchups = load_matchups(self.cfg.pairs_file)
        except (OSError, ValueError) as e:        # mid-rewrite by the pairing job: try again next pass
            self.log(f"[pairs] could not read {self.cfg.pairs_file.name}: {e}")
            return False
        self.pairs_mtime = mt
        return True

    async def _events(self, tag: str, lo: dt.datetime, hi: dt.datetime) -> list[dict]:
        out, cursor = [], None
        for _ in range(20):
            params = {"tag_id": tag, "closed": "false", "limit": "100",
                      "start_time_min": lo.strftime("%Y-%m-%dT%H:%M:%SZ"),
                      "start_time_max": hi.strftime("%Y-%m-%dT%H:%M:%SZ")}
            if cursor:
                params["after_cursor"] = cursor
            r = await self.http.get(GAMMA + "/events/keyset", params=params, timeout=30.0)
            r.raise_for_status()
            d = r.json()
            out += d.get("events") or []
            cursor = d.get("next_cursor")
            if not cursor:
                break
            await asyncio.sleep(0.2)
        return out

    async def discover(self) -> tuple[list[PmMarket], set[str]]:
        """Every Polymarket moneyline that pairs with one of the bot's matchups, plus the sports whose search
        did not complete (their markets must not be dropped on the strength of a failed request)."""
        now = utcnow()
        sports = {m.sport for m in self.matchups.values()} & set(DISCOVERY_TAGS)
        found: dict[str, PmMarket] = {}
        failed: set[str] = set()
        for sport in sorted(sports):
            back = 12 if sport == "tennis" else 6
            events, seen = [], set()
            for tag in DISCOVERY_TAGS[sport]:
                try:
                    for e in await self._events(tag, now - dt.timedelta(hours=back), now + dt.timedelta(hours=24)):
                        if e.get("id") not in seen:
                            seen.add(e.get("id"))
                            events.append(e)
                except Exception as e:
                    failed.add(sport)
                    self.log(f"[discover] {sport} tag {tag}: {type(e).__name__}: {e}")
                await asyncio.sleep(0.2)
            # ONE Polymarket market per Pinnacle matchup - a relisted or duplicated event must not double every
            # row of that match.
            best: dict[str, PmMarket] = {}
            for pm in pair_markets(events, sport, self.matchups, self.log):
                cur = best.get(pm.matchup.mid)
                if cur is None or pm.score > cur.score:
                    best[pm.matchup.mid] = pm
            for pm in best.values():
                found[pm.id] = pm
        return list(found.values()), failed

    def adopt(self, found: list[PmMarket], failed: set[str] = frozenset()):
        """Merge a discovery pass: keep what we already watch (its state), add the new, forget the vanished -
        except in a sport whose search failed this time."""
        new = 0
        for pm in found:
            old = self.markets.get(pm.id)
            if old is None:
                self.markets[pm.id] = pm
                new += 1
                if self.verbose or len(self.markets) <= 400:
                    mu = pm.matchup
                    self.log(f"[pair] {pm.sport:10} {pm.question[:60]:60} = {mu.home_name} (home) v {mu.away_name}"
                             f"  score {pm.score}  {sorted(mu.tickers)[0] if mu.tickers else ''}")
            else:
                old.matchup = pm.matchup if pm.matchup is not None else old.matchup
                old.accepting = pm.accepting
        ids = {pm.id for pm in found}
        gone = [k for k, pm in self.markets.items() if k not in ids and pm.sport not in failed]
        for k in gone:
            self.markets.pop(k, None)
        self.write_pairs_file()
        return new, len(gone)

    def write_pairs_file(self):
        rows = []
        for pm in self.markets.values():
            mu = pm.matchup
            rows.append({"market_id": pm.id, "slug": pm.slug, "question": pm.question, "sport": pm.sport,
                         "start": iso(pm.start) if pm.start else None, "assets": pm.assets, "outcomes": pm.outcomes,
                         "legs": pm.legs, "pin_matchup": mu.mid if mu else None,
                         "pin_home": mu.home_name if mu else None, "pin_away": mu.away_name if mu else None,
                         "kalshi_tickers": sorted(mu.tickers) if mu else [], "score": pm.score, "check": pm.check,
                         "fee_rate": pm.fee_rate, "taker_delay": pm.delay})
        tmp = self.cfg.data / "poly_pairs.json.tmp"
        tmp.write_text(json.dumps(rows, indent=1), encoding="utf-8")
        os.replace(tmp, self.cfg.data / "poly_pairs.json")

    def in_window(self, pm: PmMarket, now: dt.datetime) -> bool:
        """Watch a market from POLY_LEAD_MIN before its start until the bot stops polling the matchup (team sports)
        or POLY_TENNIS_MAX_LIVE_H after it (tennis, whose listed starts are nominal)."""
        mu = pm.matchup
        if mu is None or pm.resolved or pm.check == "suspect":
            return False
        if mu.mid not in self.matchups or not bot_polls(mu, self.cfg, now):
            return False
        start = pm.start or mu.start
        if start is None:
            return True
        if now < start - dt.timedelta(minutes=self.cfg.lead_min):
            return False
        if pm.sport == "tennis":
            return now <= start + dt.timedelta(hours=self.cfg.tennis_max_live_h)
        return now <= start + dt.timedelta(minutes=self.cfg.shadow_max_live)

    def refresh_watch(self):
        now = utcnow()
        want_sides: dict[str, PmSide] = {}
        for pm in self.markets.values():
            if self.bot_alive and self.in_window(pm, now):
                for i in range(2):
                    a = pm.assets[i]
                    want_sides[a] = self.sides.get(a) or PmSide(pm, i)
        self.sides = want_sides
        self.want = set(want_sides)
        for a in [a for a in self.books if a not in self.want]:
            del self.books[a]

    def poll_tokens(self) -> list[str]:
        now = utcnow()
        toks = set()
        for s in self.sides.values():
            mu = s.market.matchup
            if mu and bot_polls(mu, self.cfg, now):
                toks.update((mu.home_tok, mu.away_tok))
        return sorted(toks)

    # ── valuation (the bot's Screen + Record) ──
    def screen(self, side: PmSide, now: float):
        mu = side.market.matchup
        qs = []
        for tok in (mu.home_tok, mu.away_tok):
            q = self.oracle.quotes.get(tok)
            if q is None:
                self.stats["noquote"] += 1
                return None
            if not q["open"]:
                self.stats["suspended"] += 1
                return None
            if not self.oracle.fresh(q, now):
                self.stats["stale"] += 1
                return None
            qs.append(q)
        odds = [q["odds"] for q in qs]
        pp, over, _ = devig_prop(odds)
        if over < -0.005:                          # a book never offers a negative margin: a missing leg
            self.stats["incomplete"] += 1
            return None
        ps, _, z = devig_shin(odds)
        yi = 0 if side.leg == "home" else 1
        p_prop, p_shin = pp[yi], ps[yi]
        if not 0 < p_prop < 1:
            return None
        p = p_shin if self.cfg.devig == "shin" else p_prop
        side.add_sample(now, p)
        mine, other = qs[yi], qs[1 - yi]
        return {"p_prop": p_prop, "p_shin": p_shin, "p": p, "over": over, "z": z, "odds_mine": mine["odds"],
                "odds_other": other["odds"], "sum_s": over + 1.0, "age_ms": (now - mine["ts"]) * 1000,
                "cache_ms": (now - mine["recv"]) * 1000, "depth": mine["depth"], "live": mine["live"],
                "wv": all(q["wv"] for q in qs),
                "chg": max(q["odds_changed"] for q in qs), "stchg": max(q["status_changed"] for q in qs)}

    def consider(self, side: PmSide, now: float):
        pm = side.market
        if pm.check == "suspect" or pm.resolved or pm.matchup is None:
            return
        book = self.books.get(side.asset)
        if book is None or not book.seeded or not book.asks:
            return
        c = self.screen(side, now)
        if c is None:
            return
        ask = book.best_ask()
        if not (0 < ask < 1):
            return
        ev_ws = c["p"] - ask - fee_per_share(ask, pm.fee_rate, pm.fee_exp)
        if ev_ws < self.cfg.ev_min - self.cfg.prescreen_slack:
            self.stats["below_prescreen"] += 1
            return
        mono = time.monotonic()
        if mono - side.last_rest < self.cfg.cooldown:
            self.stats["cooldown"] += 1
            return
        side.last_rest = mono
        ws = {"ask": ask, "ask_size": book.asks.get(ask), "bid": book.best_bid(),
              "age_ms": (now - book.recv) * 1000 if book.recv else None,
              "m2": book.ask_move(now, 2), "m10": book.ask_move(now, 10), "last": book.last_trade}
        self._spawn(self.rest_check(side, c, ev_ws, ws))

    def _spawn(self, coro):
        t = asyncio.create_task(coro)
        self.tasks.add(t)
        t.add_done_callback(self.tasks.discard)

    async def rest_check(self, side: PmSide, c: dict, ev_ws: float, ws: dict):
        """The bot's REST confirmation: the WS book found the candidate, an independent read prices it."""
        async with self.rest_sem:
            t0 = time.perf_counter()
            try:
                r = await self.http.get(CLOB + "/book", params={"token_id": side.asset}, timeout=5.0)
                r.raise_for_status()
                b = r.json()
            except Exception as e:
                self.stats["rest_failed"] += 1
                if self.verbose:
                    self.log(f"[rest] {side.market.slug}: {type(e).__name__}: {e}")
                return
            rest_ms = (time.perf_counter() - t0) * 1000
        t_read = time.monotonic()
        asks = sorted((float(x["price"]), float(x["size"])) for x in b.get("asks") or [])
        bids = sorted(((float(x["price"]), float(x["size"])) for x in b.get("bids") or []), reverse=True)
        if not asks:
            return
        self.stats["rest_checks"] += 1
        await self.record(side, c, ev_ws, ws, asks, bids, rest_ms, t_read)

    async def after_delay(self, side: PmSide, c: dict, seen_ask: float, limit: float, t_read: float) -> dict:
        """THE TAKER DELAY. Polymarket holds a marketable order on a sports market for `secondsDelay` (1s) before it
        may match, so the book that fills it is the book one delay AFTER we decided - not the one we priced. A maker
        who saw what we saw has that second to pull or reprice the quote, and that is exactly when it matters.

        So re-read the book when our order would actually reach it and walk the would-be order (POLY_ORDER_SHARES,
        a FAK at the break-even limit) through it: did it fill, how much, at what price, worth what. FULL_AT_SEEN =
        the whole order still available at or under the ask we saw; FULL_WORSE = only by paying up (still inside the
        limit); PARTIAL = some; GONE = nothing left under the limit. It cannot see other takers queued in the same
        second, so it is an upper bound on what we would have got."""
        pm = side.market
        if pm.delay <= 0:
            return {"AfterDelay": "NO_DELAY", "DelayMs": 0}
        await asyncio.sleep(max(0.0, t_read + pm.delay - time.monotonic()))
        async with self.delay_sem:
            try:
                r = await self.http.get(CLOB + "/book", params={"token_id": side.asset}, timeout=5.0)
                r.raise_for_status()
                b = r.json()
            except Exception:
                self.stats["delay_recheck_failed"] += 1
                return {"AfterDelay": "RECHECK_FAILED"}
        t2 = time.monotonic()
        asks = sorted((float(x["price"]), float(x["size"])) for x in b.get("asks") or [])
        bids = sorted(((float(x["price"]), float(x["size"])) for x in b.get("bids") or []), reverse=True)
        n = self.cfg.order_shares
        depth_seen = sum(s for p, s in asks if p <= seen_ask + 1e-9)
        got, vwap, fee_avg = walk(asks, limit, n, pm.fee_rate, pm.fee_exp)
        status = ("FULL_AT_SEEN" if depth_seen >= n else "FULL_WORSE" if got >= n
                  else "PARTIAL" if got > 0 else "GONE")
        self.stats["after_delay:" + status] += 1
        p2, _, state2, _ = self.read_oracle(side, time.time())
        out = {"AfterDelay": status, "DelayMs": round((t2 - t_read) * 1000),
               "DelayedAsk": asks[0][0] if asks else None, "DelayedAskSize": asks[0][1] if asks else None,
               "DelayedBid": bids[0][0] if bids else None,
               "DelayAskMoveCents": (asks[0][0] - seen_ask) * 100 if asks else None,
               "DelayedDepthAtSeenAsk": depth_seen,
               "DelayedDepthToLimit": sum(s for p, s in asks if p <= limit + 1e-9),
               "DelayedFillShares": got, "DelayedVwap": vwap, "DelayedFeePerShare": fee_avg,
               "DelayedPTrue": p2, "DelayedPinState": state2}
        if vwap is not None:
            out["DelayedEvAtEntryP"] = c["p"] - vwap - fee_avg           # what we decided on, at the price we got
            if p2 is not None:
                out["DelayedEvAtFillP"] = p2 - vwap - fee_avg            # the fair value when it actually filled
        return out

    async def record(self, side: PmSide, c: dict, ev_ws: float, ws: dict, asks, bids, rest_ms: float,
                     t_read: float):
        cfg, pm, mu = self.cfg, side.market, side.market.matchup
        now = time.time()
        px, px_size = asks[0]
        fee = fee_per_share(px, pm.fee_rate, pm.fee_exp)
        cost = px + fee
        ev, ev_prop, ev_shin = c["p"] - cost, c["p_prop"] - cost, c["p_shin"] - cost
        limit = break_even_limit(c["p"], cfg.ev_min, pm.fee_rate, pm.fee_exp)
        depth = sum(s for p, s in asks if p <= limit + 1e-9)
        in_win = cfg.min_price <= px <= cfg.max_price
        clears = ev >= cfg.ev_min
        implausible = abs(c["p"] - px) > cfg.max_disagree
        # who moved since this side was last REST-checked (the bot's _lastSeen; "KALSHI_LED" is POLY_LED here)
        poly_led = pin_led = False
        if side.last_seen is not None:
            mv_k, mv_p = abs(px - side.last_seen[0]), abs(c["p"] - side.last_seen[1])
            poly_led = mv_k >= cfg.led_move_min and mv_p <= mv_k * cfg.led_ratio
            pin_led = mv_p >= cfg.led_move_min and mv_k <= mv_p * cfg.led_ratio
        regime = ("FIRST_LOOK" if side.last_seen is None else "PINNACLE_LED" if pin_led
                  else "POLY_LED" if poly_led else "STANDING")
        side.last_seen = (px, c["p"])
        rise = side.rise(now, cfg.kinetic_window)
        not_rising = cfg.require_rising and not (rise is not None and rise >= cfg.kinetic_min_rise)
        devig_agree = ev_prop >= cfg.ev_min and ev_shin >= cfg.ev_min
        devig_split = cfg.require_devig_agree and not devig_agree
        source_gap = abs(px - ws["ask"]) > cfg.max_source_gap
        prematch = cfg.require_in_play and not c["live"]
        unverified = not c["wv"]
        signal = (clears and not unverified and not implausible and not prematch and not poly_led and in_win
                  and not not_rising and not devig_split and not source_gap)
        decision = ("SIGNAL" if signal else "IMPLAUSIBLE" if implausible else "POLY_LED" if poly_led
                    else "SIGNAL_PREMATCH" if prematch else "OUT_OF_BAND" if not in_win
                    else "SOURCE_DISAGREE" if source_gap else "DEVIG_DISAGREE" if devig_split
                    else ("NOT_RISING" if rise is not None else "NO_KINETIC_HISTORY") if not_rising
                    else "SIGNAL_UNVERIFIED" if clears else "BELOW_EV")
        self.stats["decision:" + decision] += 1
        if signal:
            self.stats[f"signals:{pm.sport}"] += 1
        t = utcnow()
        fill_now, vwap_now, _ = walk(asks, limit, cfg.order_shares, pm.fee_rate, pm.fee_exp)
        row = {
            "Timestamp": iso(t), "Ticker": pm.slug, "Side": side.outcome, "AssetId": side.asset, "MarketId": pm.id,
            "EventId": pm.event_id, "EventTitle": pm.question, "Sport": pm.sport,
            "StartUtc": iso(pm.start) if pm.start else "", "KalshiTickers": ";".join(sorted(mu.tickers)),
            "PinMatchup": mu.mid, "PinLeg": side.leg, "InPlay": c["live"], "PinOddsMine": c["odds_mine"],
            "PinOddsOther": c["odds_other"], "PinSumS": c["sum_s"], "Vig": c["over"], "ShinZ": c["z"],
            "PTrueProp": c["p_prop"], "PTrueShin": c["p_shin"], "PTrueUsed": c["p"], "DeVigMethod": cfg.devig,
            "OracleAgeMs": round(c["age_ms"]), "OracleCacheAgeMs": round(c["cache_ms"]), "OracleDepth": c["depth"],
            "WsAsk": ws["ask"], "WsAskSize": ws["ask_size"], "WsBid": ws["bid"],
            "WsBookAgeMs": round(ws["age_ms"]) if ws["age_ms"] is not None else None,
            "RestAsk": px, "RestAskSize": px_size, "RestBid": bids[0][0] if bids else None, "RestMs": round(rest_ms),
            "WsRestGapCents": (px - ws["ask"]) * 100, "FeeRate": pm.fee_rate, "FeePerShare": fee,
            "CostPerShare": cost, "EvProp": ev_prop, "EvShin": ev_shin, "Ev": ev, "EvWs": ev_ws,
            "LimitPrice": limit, "DepthToLimit": depth, "CapacityUsd": depth * px, "InPriceWindow": in_win,
            "Decision": decision, "MoveRegime": regime,
            "PinnacleRiseCents": rise * 100 if rise is not None else None, "DeVigAgree": devig_agree,
            "PairCheck": pm.check, "TickSize": pm.tick, "MinSize": pm.min_size, "TakerDelaySec": pm.delay,
            "PinChangeAgeMs": round((now - c["chg"]) * 1000) if c["chg"] else None,
            "PinStatusAgeMs": round((now - c["stchg"]) * 1000) if c["stchg"] else None,
            "PolyMove2sCents": ws["m2"] * 100 if ws["m2"] is not None else None,
            "PolyMove10sCents": ws["m10"] * 100 if ws["m10"] is not None else None,
            "LastTrade": ws["last"],
            "OrderShares": cfg.order_shares, "FillSharesNow": fill_now, "VwapNow": vwap_now,
        }
        if clears:
            # FOLLOW EVERY CANDIDATE THAT CLEARED, including the suppressed ones - grading a guard means watching
            # what it threw away (the bot's rule). The clock starts at the DECISION, as the bot's does; the taker
            # delay is measured separately, so the two venues' curves stay comparable.
            self._spawn(self.follow(side, {"t": t, "mono": t_read, "ask": px, "p": c["p"], "ev": ev,
                                           "depth": depth, "decision": decision, "regime": regime}))
            row.update(await self.after_delay(side, c, px, limit, t_read))
        self.tel.write(row)
        if signal or self.verbose:
            ad = row.get("AfterDelay") or "-"
            tail = (f" | +{pm.delay:g}s: {ad}"
                    + (f" {row['DelayedFillShares']:.0f} sh @{row['DelayedVwap']:.3f} ev {row['DelayedEvAtEntryP'] * 100:+.2f}c"
                       if row.get("DelayedVwap") is not None else "")) if clears else ""
            self.log(f"[{decision}] {pm.sport} {pm.question[:50]} | {side.outcome}: ask {px:.3f} p_true {c['p']:.4f} "
                     f"ev {ev * 100:+.2f}c depth {depth:.0f} {'in-play' if c['live'] else 'pre-match'}{tail}")

    def read_oracle(self, side: PmSide, now: float):
        """FollowUpTracker.ReadOracle: the fair value now, and why it is blank when it is."""
        mu = side.market.matchup
        legs, odds = [], []
        missing = suspended = stale = False
        for tok in (mu.home_tok, mu.away_tok):
            q = self.oracle.quotes.get(tok)
            if q is None:
                legs.append("-")
                missing = True
                continue
            moved = f"{now - q['odds_changed']:.1f}s" if q["odds_changed"] else "?"
            legs.append(f"{q['odds']:.3f}/{q['status']}/{'live' if q['live'] else 'pre'}/{moved}")
            odds.append(q["odds"])
            if q["odds"] <= 1.0:
                missing = True
            elif not q["open"]:
                suspended = True
            elif not self.oracle.fresh(q, now):
                stale = True
        raw = None
        if not missing and len(odds) == 2:
            pv = (devig_shin(odds) if self.cfg.devig == "shin" else devig_prop(odds))[0]
            raw = pv[0 if side.leg == "home" else 1]
        state = "gone" if missing or raw is None else "suspended" if suspended else "stale" if stale else "open"
        return (raw if state == "open" else None), raw, state, "|".join(legs)

    async def follow(self, side: PmSide, e: dict):
        pm = side.market
        for sec in self.cfg.followups:
            await asyncio.sleep(max(0.0, e["mono"] + sec - time.monotonic()))
            if self.stopping:
                return
            now = time.time()
            book = self.books.get(side.asset)
            ask = book.best_ask() if book else None
            bid = book.best_bid() if book else None
            p, raw, state, legs = self.read_oracle(side, now)
            gap0 = e["p"] - e["ask"]
            row = {"CheckUtc": iso(utcnow()), "EntryUtc": iso(e["t"]), "AgeSec": round(sec, 1), "Ticker": pm.slug,
                   "Side": side.outcome, "AssetId": side.asset, "Sport": pm.sport, "Decision": e["decision"],
                   "MoveRegime": e["regime"], "EntryAsk": e["ask"], "EntryPTrue": e["p"], "EntryEvCents": e["ev"] * 100,
                   "NowAsk": ask, "NowPTrue": p, "GapEntryCents": gap0 * 100, "NowBid": bid,
                   "EntryDepth": e["depth"], "PinState": state, "PinLegs": legs, "NowPTrueRaw": raw,
                   "BookAgeMs": round((now - book.recv) * 1000) if book and book.recv else None}
            if ask is not None:
                row["PolyDriftCents"] = (ask - e["ask"]) * 100
            if p is not None:
                row["PinnacleDriftCents"] = (p - e["p"]) * 100
            if ask is not None and p is not None and 0 < ask < 1:
                k_drift, p_drift, gap1 = ask - e["ask"], p - e["p"], p - ask
                closed = abs(gap0) - abs(gap1)
                mover = ("poly" if abs(k_drift) >= abs(p_drift) * 2 else "us" if abs(p_drift) >= abs(k_drift) * 2
                         else "both")
                row["WhoClosed"] = (({"poly": "poly-came-to-us", "us": "we-went-to-poly"}.get(mover, "both-converged"))
                                    if closed > 0.005 else
                                    ({"poly": "diverged-poly-away", "us": "diverged-we-moved"}.get(mover, "both-diverged"))
                                    if closed < -0.005 else "neither")
                row["GapNowCents"] = gap1 * 100
                row["GapClosedCents"] = closed * 100
                row["NowEvCents"] = (p - ask - fee_per_share(ask, pm.fee_rate, pm.fee_exp)) * 100
            else:
                row["WhoClosed"] = f"oracle-{state}" if p is None else "book-gone"
            self.fu.write(row)

    def price_check(self, now: float):
        """The pairing's second line, on live prices: a matched market whose Polymarket mid sits far from the
        de-vigged Pinnacle price is a mispair (wrong match, inverted sides) until shown otherwise. Within 0.10 on
        a tight book = confirmed; beyond 0.25 = SUSPECT, dropped and never valued. The bot's 0.15 implausibility
        band still applies per signal on top."""
        for pm in {s.market for s in self.sides.values()}:
            if pm.check != "unchecked":
                continue
            side = self.sides.get(pm.assets[0])
            book = self.books.get(pm.assets[0])
            if side is None or book is None or not book.seeded or not book.asks or not book.bids:
                continue
            a, b = book.best_ask(), book.best_bid()
            if a - b > 0.10:
                continue
            p, _, state, _ = self.read_oracle(side, now)
            if p is None:
                continue
            diff = abs((a + b) / 2 - p)
            if diff <= 0.10:
                pm.check = "ok"
            elif diff > 0.25:
                pm.check = "suspect"
                self.log(f"[pair] SUSPECT, dropped: {pm.question} - Polymarket mid {(a + b) / 2:.2f} for "
                         f"{pm.outcomes[0]} vs Pinnacle {p:.2f}. Wrong match or inverted sides.")
            if pm.check != "unchecked":
                self.write_pairs_file()

    # ── websocket ──
    def on_ws(self, raw: str):
        self.ws_last_rx = time.time()
        if raw == "PONG":
            return
        self.msgs += 1
        self.bytes += len(raw)
        try:
            data = json.loads(raw)
        except ValueError:
            return
        now = time.time()
        touched = set()
        for m in data if isinstance(data, list) else [data]:
            et = m.get("event_type")
            if et == "price_change":
                for ch in m.get("price_changes") or []:
                    a = ch.get("asset_id")
                    book = self.books.get(a)
                    if book is None:
                        if a not in self.want:
                            continue
                        book = self.books[a] = Book()
                    levels = book.bids if ch.get("side") == "BUY" else book.asks
                    p, s = float(ch["price"]), float(ch["size"])
                    if s <= 0:
                        levels.pop(p, None)
                    else:
                        levels[p] = s
                    book.recv = now
                    touched.add(a)
            elif et == "book":
                a = m.get("asset_id")
                if a not in self.want:
                    continue
                book = self.books.get(a) or Book()
                book.load(m.get("bids"), m.get("asks"), now)
                self.books[a] = book
                touched.add(a)
            elif et == "last_trade_price":
                book = self.books.get(m.get("asset_id"))
                if book is not None:
                    book.last_trade = _num(m.get("price"))
            elif et == "tick_size_change":
                book = self.books.get(m.get("asset_id"))
                if book is not None:
                    book.tick = _num(m.get("new_tick_size"))
            elif et == "market_resolved":
                self.on_resolved(m)
        for a in touched:
            book = self.books[a]
            if not book.seeded:
                continue
            ba = book.best_ask()
            if not book.ask_hist or book.ask_hist[-1][1] != ba:
                book.ask_hist.append((now, ba))
                side = self.sides.get(a)
                if side is not None and ba is not None:
                    self.consider(side, now)

    def on_resolved(self, m: dict):
        win = m.get("winning_asset_id")
        hit = [pm for pm in self.markets.values() if pm.condition == m.get("market") or win in pm.assets]
        for pm in hit:
            pm.resolved = win
        rec = {"ts": iso(utcnow()), "market": m.get("market"), "id": m.get("id"), "winning_asset_id": win,
               "winning_outcome": m.get("winning_outcome"), "assets_ids": m.get("assets_ids"),
               "slug": hit[0].slug if hit else None}
        with open(self.cfg.data / "poly_settlements.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec) + "\n")

    async def ws_loop(self):
        backoff = 1.0
        while not self.stopping:
            if not self.want:
                await asyncio.sleep(2)
                continue
            try:
                async with websockets.connect(WS_URL, max_size=2 ** 24, ping_interval=None, open_timeout=20,
                                              close_timeout=5) as ws:
                    self.ws = ws
                    self.subscribed = set(self.want)
                    await ws.send(json.dumps({"assets_ids": sorted(self.subscribed), "type": "market",
                                              "custom_feature_enabled": True}))
                    self.ws_last_rx = time.time()
                    backoff = 1.0
                    self.log(f"[ws] connected, {len(self.subscribed)} asset(s)")
                    pinger = asyncio.create_task(self._pinger(ws))
                    try:
                        async for raw in ws:
                            self.on_ws(raw if isinstance(raw, str) else raw.decode("utf-8", "replace"))
                    finally:
                        pinger.cancel()
            except Exception as e:
                if not self.stopping:
                    self.log(f"[ws] {type(e).__name__}: {e}")
            self.ws = None
            self.subscribed = set()
            self.books.clear()               # a fresh connection re-sends every book; a stale one must not linger
            if not self.stopping:
                await asyncio.sleep(backoff)
                backoff = min(30.0, backoff * 2)

    async def _pinger(self, ws):
        while True:
            await asyncio.sleep(10)
            await ws.send("PING")
            if time.time() - self.ws_last_rx > 35:     # no PONG for three rounds: the socket is dead, rebuild
                self.log("[ws] silent for 35s, reconnecting")
                await ws.close()
                return

    async def sync_subscriptions(self):
        ws = self.ws
        if ws is None:
            return
        if not self.want:
            await ws.close()
            return
        add, drop = sorted(self.want - self.subscribed), sorted(self.subscribed - self.want)
        try:
            if add:
                await ws.send(json.dumps({"assets_ids": add, "operation": "subscribe", "custom_feature_enabled": True}))
            if drop:
                await ws.send(json.dumps({"assets_ids": drop, "operation": "unsubscribe"}))
            self.subscribed = set(self.want)
        except Exception as e:
            self.log(f"[ws] subscription update failed: {e}")
        if add:
            await self.seed_books(add)

    async def seed_books(self, assets: list[str]):
        """A snapshot for assets added to an open socket, in case the subscribe frame does not send one."""
        await asyncio.sleep(3)
        missing = [a for a in assets if a in self.want and (a not in self.books or not self.books[a].seeded)]
        if missing:
            self.stats["rest_seeded"] += len(missing)
        for i in range(0, len(missing), 500):
            try:
                r = await self.http.post(CLOB + "/books", json=[{"token_id": a} for a in missing[i:i + 500]],
                                         timeout=10.0)
                r.raise_for_status()
                now = time.time()
                for b in r.json():
                    a = b.get("asset_id")
                    if a in self.want and (a not in self.books or not self.books[a].seeded):
                        book = self.books.get(a) or Book()
                        book.load(b.get("bids"), b.get("asks"), now)
                        self.books[a] = book
            except Exception as e:
                self.log(f"[ws] seed failed: {type(e).__name__}: {e}")

    # ── loops ──
    async def oracle_loop(self):
        base = self.cfg.poll_ms / 1000.0
        while not self.stopping:
            t0 = time.monotonic()
            toks = self.poll_tokens() if self.bot_alive else []
            if toks and await self.oracle.poll(self.http, toks):
                now = time.time()
                for side in list(self.sides.values()):
                    self.consider(side, now)
                self.price_check(now)
                # BACK OFF IF THE SIDECAR IS SLOW. Our polls share its event loop with the Pinnacle WS reader, and
                # the bot's oracle comes first: a slow poll is the sign we are crowding it.
                recent = list(self.oracle.poll_ms)[-20:]
                if len(recent) >= 10:
                    p50 = sorted(recent)[len(recent) // 2]
                    if p50 > 100 and self.oracle.interval < 5.0:
                        self.oracle.interval = min(5.0, self.oracle.interval * 1.5)
                        self.log(f"[oracle] polls take {p50:.0f}ms - backing off to {self.oracle.interval * 1000:.0f}ms")
                    elif p50 < 30 and self.oracle.interval > base:
                        self.oracle.interval = max(base, self.oracle.interval / 1.5)
            await asyncio.sleep(max(0.05, self.oracle.interval - (time.monotonic() - t0)))

    async def bot_loop(self):
        """Is the Kalshi bot running? Its process, not its report build (bin/report runs --resolve and exits)."""
        while not self.stopping:
            alive = not self.cfg.require_bot
            if not alive:
                for p in psutil.process_iter(["name", "exe"]):
                    try:
                        if (p.info["name"] or "").lower() == "kalshievbot.exe" and \
                                "\\bin\\report\\" not in (p.info["exe"] or "").lower():
                            alive = True
                            break
                    except (psutil.Error, OSError):
                        continue
            if alive != self.bot_alive:
                self.bot_alive = alive
                self.log("[bot] KalshiEvBot is running - watching." if alive else
                         "[bot] KalshiEvBot is NOT running - paused: no sidecar calls, no Polymarket socket.")
                self.refresh_watch()
                await self.sync_subscriptions()
            await asyncio.sleep(10)

    async def pairs_loop(self):
        last_discover = 0.0
        while not self.stopping:
            changed = self.reload_pairs()
            if changed or time.time() - last_discover > self.cfg.discover_min * 60:
                try:
                    found, failed = await self.discover()
                    new, gone = self.adopt(found, failed)
                    by = Counter(pm.sport for pm in self.markets.values())
                    self.log(f"[discover] {len(self.matchups)} Pinnacle matchup(s) from the bot's pairs; "
                             f"{len(self.markets)} on Polymarket ({', '.join(f'{k} {v}' for k, v in sorted(by.items()))})"
                             f" +{new} -{gone}")
                    last_discover = time.time()
                except Exception as e:
                    self.log(f"[discover] failed: {type(e).__name__}: {e}")
                    last_discover = time.time() - self.cfg.discover_min * 60 + 120   # retry in two minutes
            self.refresh_watch()
            await self.sync_subscriptions()
            await asyncio.sleep(30)

    async def snapshot_loop(self):
        await asyncio.sleep(60)
        while not self.stopping:
            now = time.time()
            for side in list(self.sides.values()):
                pm, mu = side.market, side.market.matchup
                p, raw, state, _ = self.read_oracle(side, now)
                q = self.oracle.quotes.get(mu.home_tok if side.leg == "home" else mu.away_tok)
                qo = self.oracle.quotes.get(mu.away_tok if side.leg == "home" else mu.home_tok)
                book = self.books.get(side.asset)
                a = book.best_ask() if book else None
                b = book.best_bid() if book else None
                self.snap.write({"Timestamp": iso(utcnow()), "Ticker": pm.slug, "Side": side.outcome,
                                 "AssetId": side.asset, "Sport": pm.sport,
                                 "StartUtc": iso(pm.start) if pm.start else "", "KalshiTickers": ";".join(sorted(mu.tickers)),
                                 "InPlay": q["live"] if q else None, "PinState": state, "PTrueUsed": p, "PTrueRaw": raw,
                                 "PinOddsMine": q["odds"] if q else None, "PinOddsOther": qo["odds"] if qo else None,
                                 "Ask": a, "AskSize": book.asks.get(a) if book and a else None, "Bid": b,
                                 "BidSize": book.bids.get(b) if book and b else None,
                                 "BookAgeMs": round((now - book.recv) * 1000) if book and book.recv else None,
                                 "LastTrade": book.last_trade if book else None, "PairCheck": pm.check})
            await asyncio.sleep(self.cfg.snapshot_min * 60)

    async def status_loop(self):
        while not self.stopping:
            await asyncio.sleep(60)
            for w in (self.tel, self.fu, self.snap):
                w.flush()
            t, m0, b0 = self._last_status
            now = time.time()
            rate, kbs = (self.msgs - m0) / (now - t), (self.bytes - b0) / 1024 / (now - t)
            self._last_status = (now, self.msgs, self.bytes)
            pm_live = {s.market.id for s in self.sides.values()}
            pms = sorted(self.oracle.poll_ms)
            p50 = pms[len(pms) // 2] if pms else float("nan")
            sig = sum(v for k, v in self.stats.items() if k.startswith("signals:"))
            cand = self.stats["rest_checks"]
            self.log(f"[status] bot {'up' if self.bot_alive else 'DOWN'} | {len(self.markets)} paired, watching "
                     f"{len(pm_live)} mkt / {len(self.sides)} outcome(s) | ws "
                     f"{'up' if self.ws is not None else 'down'} {rate:.0f} msg/s {kbs:.0f} KB/s | oracle "
                     f"{len(self.poll_tokens())} sel, poll p50 {p50:.0f}ms every {self.oracle.interval * 1000:.0f}ms"
                     f"{'' if self.oracle.session_ready else ' SESSION-DOWN'}"
                     f"{f' err {self.oracle.errors}' if self.oracle.errors else ''} | candidates {cand} signals {sig} "
                     f"| cpu {self.proc.cpu_percent(None) / 100:.2f} cores, "
                     f"{self.proc.memory_info().rss / 2 ** 20:.0f} MB")

    async def run(self, minutes: float | None):
        limits = httpx.Limits(max_connections=8, max_keepalive_connections=8)
        async with httpx.AsyncClient(limits=limits, headers={"User-Agent": "poly-shadow/1.0"}) as http:
            self.http = http
            self.reload_pairs()
            self.log(f"[start] pairs {self.cfg.pairs_file} ({len(self.matchups)} matchup(s)); sidecar {self.cfg.sidecar}"
                     f" every {self.cfg.poll_ms:.0f}ms, never fresh=1; data {self.cfg.data}")
            self.log(f"[start] bot gates: ev>={self.cfg.ev_min * 100:.2f}c band {self.cfg.min_price:.2f}-"
                     f"{self.cfg.max_price:.2f} de-vig {self.cfg.devig} in-play {'required' if self.cfg.require_in_play else 'optional'}"
                     f" follow-ups {'/'.join(f'{x:g}' for x in self.cfg.followups)}s")
            loops = [self.bot_loop(), self.pairs_loop(), self.ws_loop(), self.oracle_loop(), self.snapshot_loop(),
                     self.status_loop()]
            tasks = [asyncio.create_task(c) for c in loops]
            try:
                if minutes:
                    await asyncio.sleep(minutes * 60)
                else:
                    await asyncio.gather(*tasks)
            finally:
                self.stopping = True
                for t in tasks + list(self.tasks):
                    t.cancel()
                await asyncio.gather(*tasks, *self.tasks, return_exceptions=True)
                for w in (self.tel, self.fu, self.snap):
                    w.close()


# ── entry ────────────────────────────────────────────────────────────────────────────────────────────────

def take_lock(path: Path) -> bool:
    """One recorder at a time: two would double every row and every sidecar poll."""
    if path.exists():
        try:
            pid = int(path.read_text().strip() or 0)
            if pid and psutil.pid_exists(pid) and pid != os.getpid():
                cmd = " ".join(psutil.Process(pid).cmdline())
                if "poly_shadow.py" in cmd:
                    return False
        except (ValueError, psutil.Error, OSError):
            pass
    path.write_text(str(os.getpid()))
    return True


async def check_only(cfg: Cfg):
    sh = Shadow(cfg, verbose=True)
    async with httpx.AsyncClient(headers={"User-Agent": "poly-shadow/1.0"}) as http:
        sh.http = http
        sh.reload_pairs()
        by_sport = Counter(m.sport for m in sh.matchups.values())
        sh.log(f"[check] {len(sh.matchups)} Pinnacle matchup(s) in {cfg.pairs_file.name}: "
               + ", ".join(f"{k} {v}" for k, v in sorted(by_sport.items())))
        found, _ = await sh.discover()
        sh.adopt(found)
        now = utcnow()
        by = Counter(pm.sport for pm in found)
        win = [pm for pm in found if sh.in_window(pm, now)]
        sh.log(f"[check] {len(found)} paired on Polymarket ({', '.join(f'{k} {v}' for k, v in sorted(by.items()))}); "
               f"{len(win)} in their watch window now. Written: {cfg.data / 'poly_pairs.json'}")
        cover = Counter()
        for m in sh.matchups.values():
            cover[(m.sport, any(pm.matchup is m for pm in found))] += 1
        for sp in sorted({k[0] for k in cover}):
            n_all = cover[(sp, True)] + cover[(sp, False)]
            sh.log(f"[check] coverage {sp:10} {cover[(sp, True)]:4} of {n_all:4} bot matchups are also on Polymarket")


def main():
    ap = argparse.ArgumentParser(description="Polymarket shadow telemetry (observe-only).")
    ap.add_argument("--check", action="store_true", help="discover + pair + print, then exit (no sockets, no sidecar)")
    ap.add_argument("--minutes", type=float, default=None, help="stop after N minutes (testing)")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()
    cfg = Cfg(read_env())
    cfg.data.mkdir(parents=True, exist_ok=True)
    if args.check:
        asyncio.run(check_only(cfg))
        return
    lock = cfg.data / "poly_shadow.lock"
    if not take_lock(lock):
        print("[poly] another poly_shadow.py is already running - one recorder at a time.")
        sys.exit(1)
    if not cfg.require_bot:
        print("[poly] POLY_REQUIRE_BOT=0: polling the sidecar without the bot running. That ACTIVATES Pinnacle "
              "leagues on its own - testing only.")
    try:
        asyncio.run(Shadow(cfg, verbose=args.verbose).run(args.minutes))
    except KeyboardInterrupt:
        pass
    finally:
        try:
            if lock.read_text().strip() == str(os.getpid()):
                lock.unlink()
        except OSError:
            pass


if __name__ == "__main__":
    main()
