"""
pinnacle_adapter.py — Pinnacle (pinnacle.bet) odds via its private "Arcadia" API.

TWO odds sources, selectable with PINNACLE_ODDS_MODE:
  "ws"   (DEFAULT) — MQTT-over-WebSocket PUSH feed (real-time, gentle, looks like the real client). This is
                     what the browser uses after the initial REST snapshot. Best for LIVE arbs.
  "rest" (fallback) — serial polling of /leagues/{id}/markets/straight (use if the WS is blocked/awkward).

WHY NO PLAYWRIGHT: api.arcadia.pinnacle.com answers a plain HTTP/WS client — `curl` returns JSON, so there
is NO Cloudflare *browser-challenge* on these endpoints (Cloudflare is just the CDN; the gate is the
x-api-key/x-session headers + the MQTT CONNECT auth). Pinnacle CLOSED its official API, so this replays the
website's own private API with a scraped session → OPERATE GENTLY (account-ban risk): the WS is passive
(no polling), the REST fallback is serial+jittered+backoff. No concurrency (that got the bookmaker acct banned).

WEBSOCKET (mode "ws") — MQTT 3.1.1 over WSS at wss://api.arcadia.pinnacle.com/ws (subprotocol "mqtt"):
  CONNECT   username = ACCOUNT ID (PINNACLE_WS_USERNAME), password = "{x-session}|{suffix}" (PINNACLE_WS_PASSWORD).
  SUBSCRIBE topics "matchups/reg/lg/{leagueId}/{pre|live/ld|live/dz|live/both}" (reg=regular incl moneyline).
  PUBLISH   payload = JSON {op:"upd"|"add"|"del", pk:matchupId, rec:{id, league{id}, participants[...],
            markets:[{key:"s;{period};{type}", period, type, status, prices:[{designation:"home"|"away"|
            "draw", price(AMERICAN), points?}], limits:[{type:"maxRiskStake", amount}]}]}}.
  Full-game 2-way moneyline = market period 0 & type "moneyline" → prices home/away (3 = +draw).

SELECTION-ID (the token in cross_pairs.json): "{leagueId}:{matchupId}:{designation}" (designation = home|
  away|draw — semantic, taken straight from the WS payload; catalog() emits the same from the matchup's
  participant alignment, so WS odds and REST catalog keys MATCH).

FRESHNESS on a PUSH feed: a STABLE price does not re-tick, so we must NOT let its ts age while the WS is
healthy — the live connection IS the freshness guarantee (Pinnacle pushes any change/suspend). So odds()
stamps ts=now WHILE CONNECTED; on disconnect it serves the stored ts → it ages → the C# gate clears the book.

CONFIG (env): PINNACLE_WS_USERNAME, PINNACLE_WS_PASSWORD (WS auth); PINNACLE_API_KEY (defaults to the
  observed static site key); PINNACLE_SESSION, PINNACLE_DEVICE_UUID (REST headers for catalog()/rest mode);
  PINNACLE_CATALOG_LEAGUES (CSV league ids for catalog/pairing); PINNACLE_ODDS_MODE (ws|rest, default ws);
  PINNACLE_REFRESH_SEC (rest mode cadence, default 15 — GENTLE), PINNACLE_ACTIVE_TTL_SEC (default 180),
  PINNACLE_REQUEST_JITTER_MS (rest mode, default 250).

NOTE: requires `paho-mqtt` for ws mode (pip install paho-mqtt). UNTESTED against the live WS — verify the
upgrade isn't Cloudflare-challenged on first connect; if it is, fall back to PINNACLE_ODDS_MODE=rest.
"""
from __future__ import annotations

import asyncio
import json
import math
import os
import random
import re
import threading
import unicodedata
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import httpx

from book_adapter import BookAdapter, BetResult, CatalogEntry, Selection
import sports as sports_cfg   # unified sport catalog (active sport ids default the lifecycle set)
# Shared cursor: notched wheel scrolling, the measured dwell, and recorded-trajectory replay. This
# adapter kept its own `_human_move_page`/`_human_click_loc` (the originals these were ported FROM) and
# so has been missing every improvement since — wheel-instead-of-teleport, off-centre targeting, and the
# corpus replay. Imported at module level on purpose: a lazy import inside a try/except would turn a
# missing name into a silently skipped scroll.
from human_mouse import CURSOR

REST_BASE = os.environ.get("PINNACLE_API_BASE", "https://api.arcadia.pinnacle.com/0.1")
# GUEST API: same board structure (sports/leagues/matchups/markets, incl. price `designation`) served with ONLY
# the public x-api-key — NO user session. Used for catalog/pairing so enumeration never depends on the authed
# x-session (which may be stale, not-yet-captured in browser mode, or logged out). Authed REST is for live odds.
GUEST_BASE = os.environ.get("PINNACLE_GUEST_BASE", "https://guest.api.arcadia.pinnacle.com/0.1")
# My Bets page. `GET /0.1/bets` is PAGE-SPECIFIC — the site only fires it from here, so calling it "off page"
# is a correlation a server can notice (unlike /wallet/balance, which the page polls constantly, or
# /markets/straight, which any league view fires). We navigate here and read the page's OWN response instead.
BETS_URL = os.environ.get("PINNACLE_BETS_URL", "https://www.pinnacle.bet/en/account/bets")
WS_HOST = os.environ.get("PINNACLE_WS_HOST", "api.arcadia.pinnacle.com")
WS_PATH = os.environ.get("PINNACLE_WS_PATH", "/ws")
DEFAULT_API_KEY = "CmX2KcMrXuFmNg6YFbmTxE0y9CIrOi0R"   # static public site client key (not a per-user secret)
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/149.0.0.0 Safari/537.36")
_SIDES = ("home", "away", "draw")

# Is the board's own scroller at its end? The live list scrolls an INNER pane, not the window, so this walks
# up from a rendered row to whichever ancestor actually scrolls and asks that one. Returns true when the
# window is the scroller and it is at the end too, so a layout change cannot make "bottom" unreachable.
_SCROLL_AT_BOTTOM_JS = r"""
() => {
  const btn = document.querySelector('button.market-btn');
  let el = btn ? btn.parentElement : null;
  while (el) {
    const s = getComputedStyle(el);
    if (el.scrollHeight > el.clientHeight + 4 && /auto|scroll/.test(s.overflowY)) {
      return el.scrollTop + el.clientHeight >= el.scrollHeight - 8;
    }
    el = el.parentElement;
  }
  const d = document.scrollingElement || document.documentElement;
  return d.scrollTop + d.clientHeight >= d.scrollHeight - 8;
}
"""


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
    for lim in limits or []:
        if lim.get("type") == "maxRiskStake":
            try:
                return float(lim.get("amount") or 0)
            except (TypeError, ValueError):
                return 0.0
    return 0.0


def _cutoff_ts(cutoff) -> Optional[float]:
    """Parse Pinnacle's ISO-8601 `cutoffAt` (betting-close time, UTC) → unix seconds. None if absent/bad."""
    if not cutoff:
        return None
    try:
        return datetime.fromisoformat(str(cutoff).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def _norm_name(name: str) -> str:
    """Lowercased letters-and-spaces only — the same shape the pairing stores its resolved names in, so a
    redirect compares like with like ('Milena Steinkamp' vs 'milena steinkamp')."""
    return " ".join(re.sub(r"[^a-z ]", " ", (name or "").lower()).split())


def _strip_units(name: str) -> str:
    """Drop Pinnacle's per-matchup unit suffix: 'Zizou Bergs (Sets)' / 'Toby Samuel (Games)' -> the bare name.
    The winner matchup is sometimes labelled '(Sets)' (no clean variant exists), so we keep it but clean the
    name for pairing. Names without a '(' are returned unchanged (baseball etc.)."""
    return (name or "").split("(")[0].strip()


# ── UI bet placement (in-page scripts) ────────────────────────────────────────
# Captured from a real manual bet 2026-07-20. The Quick Bet popover is `#quick-bet-portal`; the stake box is
# `input[aria-label="Currency Input"]`; submit is a button reading "Place Bet". Everything else in that subtree
# is a CSS-module build hash (`matchupName-LaAwbv3B5f`, `placeBet-ljO7MdYdT4`, ...) that rotates on deploy, so
# these scripts match on the STABLE prefix and always fall back to visible text.
_UI_POPOVER = "#quick-bet-portal"

_UI_READ_POPOVER = r"""
  const readPop = () => {
    const p = document.querySelector("#quick-bet-portal");
    if (!p || !(p.textContent || "").trim()) return null;
    const t = (n) => ((n && n.textContent) || "").replace(/\s+/g, " ").trim();
    const cls = (el) => (typeof el.className === "string" ? el.className : "");
    let matchup = "", label = "", price = "";
    for (const el of p.querySelectorAll("div,span")) {
      const c = cls(el);
      if (!matchup && c.includes("matchupName-")) matchup = t(el);
      if (!label && c.includes("priceLabelAlt-")) label = t(el);
      if (!price && /(^|\s)price-/.test(c) && !c.includes("priceLabel")) {
        const m = t(el).match(/\d{1,3}\.\d{2,3}/); if (m) price = m[0];
      }
    }
    // Fallbacks when the hashed class names change: derive from visible text.
    const all = t(p);
    if (!price) { const m = all.match(/\b\d{1,3}\.\d{2,3}\b/); if (m) price = m[0]; }
    if (!label) {
      for (const el of p.querySelectorAll("span,div")) {
        const s = t(el);
        if (s && s.length < 60 && s !== matchup && /[A-Za-z]/.test(s) && !/\d{1,3}\.\d{2,3}/.test(s)) { label = s; break; }
      }
    }
    return {matchup, label, price, all};
  };
"""

# Is THIS odds button in the intended MATCH? Run per ElementHandle (not by index — the live board reorders, so
# an index goes stale between find and click). Climb to the FIRST ancestor holding BOTH participants, then guard
# that it's a single match block (a handful of odds buttons) — NOT a league/board section that merely contains
# both names among 20 other matches. Without that guard a busy board matched EVERY button (114 candidates) and
# the probe never reached the target. From the real DOM: a match block holds ~6 market-btns; a league holds
# dozens. (measured 2026-07-28)
_ROW_MATCH_JS = r"""
(el, a) => {
  const norm = (s) => (s || "").toLowerCase().replace(/\s+/g, " ");
  // odds button = shows a PRICE: decimal (1.396) OR American (-347/+268). The player-NAME / match-title elements
  // carry the same `market-btn` class but no price — clicking them opens the side bet-slip / navigates, not the
  // Quick Bet. Accepting American too means the finder still locates the row when the site is on American odds,
  // so placement fails cleanly at the decimal guard ("not decimal odds") instead of a confusing "no row".
  const isOdds = (b) => { const t = b.textContent || ""; return /\d{1,3}\.\d{2,3}/.test(t) || /[+-]\d{2,4}\b/.test(t); };
  // The TOP CAROUSEL of "Match Winner" featured cards carries market-btns whose click ADDS TO THE BET SLIP (not
  // Quick Bet) — never a valid probe target (it silently pollutes the slip; seen 2026-07-28: Tsitsipas 2.220
  // landed in the slip during a Saito/Perry search) and its buttons also mislead the scroll anchor. Exclude it.
  if (el.closest && el.closest('[class*="carousel"]')) return false;
  if (!isOdds(el)) return false;
  const cap = a.maxBtns || 10;
  let row = el, hops = 0;
  while (row && hops++ < 9) {
    const t = norm(row.textContent || "");
    if (t.includes(a.A) && t.includes(a.B)) {
      // first ancestor with both names → accept only if it's match-sized (count ODDS buttons only, so
      // player-name buttons don't inflate the match-vs-league test), not a multi-match container
      const odds = Array.from(row.querySelectorAll("button.market-btn")).filter(isOdds).length;
      return odds <= cap;
    }
    row = row.parentElement;
  }
  return false;
}
"""

# THE SAME TEST, RUN ONCE FOR EVERY BUTTON AT A TIME. `_ROW_MATCH_JS` is evaluated per ElementHandle, which
# is one CDP round trip PER BUTTON — with ~70-100 odds buttons rendered that is 70-100 round trips for a
# single _find(), repeated on every scroll pass. This returns the matching INDICES from one call instead.
# Document order matches query_selector_all's, so the caller indexes straight into the handles it already has.
_ROWS_MATCH_ALL_JS = r"""
(a) => {
  const norm = (s) => (s || "").toLowerCase().replace(/\s+/g, " ");
  const isOdds = (b) => { const t = b.textContent || ""; return /\d{1,3}\.\d{2,3}/.test(t) || /[+-]\d{2,4}\b/.test(t); };
  const cap = a.maxBtns || 10;
  const out = [];
  const all = document.querySelectorAll("button.market-btn");
  for (let i = 0; i < all.length; i++) {
    const el = all[i];
    if (el.closest && el.closest('[class*="carousel"]')) continue;
    if (!isOdds(el)) continue;
    let row = el, hops = 0, hit = false;
    while (row && hops++ < 9) {
      const t = norm(row.textContent || "");
      if (t.includes(a.A) && t.includes(a.B)) {
        const odds = Array.from(row.querySelectorAll("button.market-btn")).filter(isOdds).length;
        hit = odds <= cap;
        break;
      }
      row = row.parentElement;
    }
    if (hit) out.push(i);
  }
  return out;
}
"""

# Read what the Quick Bet popover currently shows (matchup, side label, decimal price, max bet). Verification of
# matchup/side/market happens in Python (`_verify_pop`).
_UI_READ_POP_JS = r"""
() => {
  const p = document.querySelector("#quick-bet-portal");
  if (!p || !(p.textContent || "").trim()) return null;
  const t = (n) => ((n && n.textContent) || "").replace(/\s+/g, " ").trim();
  const cls = (el) => (typeof el.className === "string" ? el.className : "");
  let matchup = "", label = "", price = "", american = false;
  for (const el of p.querySelectorAll("div,span")) {
    const c = cls(el);
    if (!matchup && c.includes("matchupName-")) matchup = t(el);
    if (!label && c.includes("priceLabelAlt-")) label = t(el);
    if (!price && !american && /(^|\s)price-/.test(c) && !c.includes("priceLabel")) {
      const s = t(el);
      const m = s.match(/\d{1,3}\.\d{2,3}/);
      if (m) price = m[0];
      else if (/^[+-]\d{2,4}$/.test(s)) american = true;   // American odds in the price slot -> not decimal
    }
  }
  const all = t(p);
  // fallback ONLY when no decimal was found AND it isn't American — and strip from 'Max Bet' on, so the max-bet
  // / payout figure (e.g. "Max Bet: EUR 394.87") can't masquerade as the odds and wrongly pass the decimal guard.
  if (!price && !american) {
    const m = all.replace(/Max Bet[\s\S]*/i, "").match(/\b\d{1,3}\.\d{2,3}\b/); if (m) price = m[0];
  }
  if (!label) {
    for (const el of p.querySelectorAll("span,div")) {
      const s = t(el);
      if (s && s.length < 60 && s !== matchup && /[A-Za-z]/.test(s) && !/\d{1,3}\.\d{2,3}/.test(s)) { label = s; break; }
    }
  }
  let maxBet = null;
  const mm = all.match(/Max Bet:?\s*[A-Z]{0,3}\s*([\d,]+(?:\.\d+)?)/i);
  if (mm) maxBet = parseFloat(mm[1].replace(/,/g, ""));
  return {matchup, label, price, american, all, maxBet};
}
"""

_UI_STAKE_JS = r"""
async (args) => {
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  __READ__
  const p = document.querySelector("#quick-bet-portal");
  if (!p) return {ok: false, error: "popover gone"};
  const inp = p.querySelector('input[aria-label="Currency Input"]') || p.querySelector('input[type="text"]');
  if (!inp) return {ok: false, error: "stake input not found"};
  // React controlled input: set through the native setter or the value is reverted on re-render.
  const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "value").set;
  inp.focus();
  setter.call(inp, "");
  inp.dispatchEvent(new Event("input", {bubbles: true}));
  await sleep(60);
  setter.call(inp, String(args.stake));
  inp.dispatchEvent(new Event("input", {bubbles: true}));
  inp.dispatchEvent(new Event("change", {bubbles: true}));
  await sleep(350);
  if (String(inp.value).replace(/[^\d.]/g, "") !== String(args.stake))
    return {ok: false, error: `stake did not take (input reads "${inp.value}")`};
  let maxBet = null;
  const m = ((p.textContent || "").match(/Max Bet:?\s*[A-Z]{0,3}\s*([\d,]+(?:\.\d+)?)/i));
  if (m) maxBet = parseFloat(m[1].replace(/,/g, ""));
  return {ok: true, value: inp.value, maxBet};
}
""".replace("__READ__", _UI_READ_POPOVER)

# After submit, Pinnacle may show an "odds changed -- accept?" confirmation. That flow was NOT present in the
# 2026-07-20 capture, so its markup is unknown. This DETECTS it (a new actionable button appearing in the
# popover while no bet response has arrived) and reports the popover text, rather than clicking something
# unverified. Not clicking = no bet placed = safe. Capture a bet that hits this prompt to learn the markup.
_UI_PROMPT_JS = r"""
() => {
  const p = document.querySelector("#quick-bet-portal");
  if (!p) return {prompt: false};
  const t = (p.textContent || "").replace(/\s+/g, " ").trim();
  const btns = Array.from(p.querySelectorAll("button"))
    .map((b) => (b.textContent || "").replace(/\s+/g, " ").trim())
    .filter(Boolean);
  const actionable = btns.filter((s) => /accept|confirm|changed|new price|ok\b/i.test(s));
  const changed = /odds (have )?changed|price (has )?changed|accept/i.test(t);
  return {prompt: actionable.length > 0 || changed, buttons: btns, text: t.slice(0, 400)};
}
"""

_UI_CLOSE_JS = r"""
() => {
  const p = document.querySelector("#quick-bet-portal");
  if (!p) return true;
  const x = p.querySelector('button[aria-label*="Remove"], button[aria-label*="Close"], i.icon-x');
  if (x) (x.closest("button") || x).click();
  return true;
}
"""

# Read the SIDE betslip's state so _trim_betslip knows whether there is anything to clear and whether the
# confirm modal is showing. Reads only -- clicks are done as REAL mouse actions (Locators), never here, so this
# eval can never remove a selection or dismiss a dialog by itself. Test-ids captured from a real Remove-all flow
# (bet_capture 2026-07-28): Betslip-RemoveAllButton, Betslip-RemoveAllModal(+ -ConfirmButton/-CancelButton).
_BETSLIP_STATE_JS = r"""
() => {
  const q = (s) => document.querySelector(s);
  return {
    hasRemoveAll: !!q('[data-test-id="Betslip-RemoveAllButton"]'),
    cards: document.querySelectorAll('[data-test-id="Betslip-Card"]').length,
    modalOpen: !!q('[data-test-id="Betslip-RemoveAllModal"]'),
    confirmReady: !!q('[data-test-id="Betslip-RemoveAllModal-ConfirmButton"]'),
  };
}
"""

# Find a CURRENTLY-VISIBLE odds row to rest the cursor over before wheel-scrolling. A wheel event scrolls
# whatever element is under the pointer; the featured board scrolls its OWN inner pane (not the page), so we must
# put the cursor over that pane (a visible odds row is guaranteed inside it) or the wheel moves the wrong thing.
# Returns the centre of the first visible `market-btn` (+ viewport dims), or found:false if none are on screen.
_SCROLL_ANCHOR_JS = r"""
() => {
  const w = window.innerWidth, h = window.innerHeight;
  const btns = document.querySelectorAll("button.market-btn");
  for (const b of btns) {
    if (b.closest && b.closest('[class*="carousel"]')) continue;   // skip the top featured strip (own h-scroll)
    const r = b.getBoundingClientRect();
    const cx = r.left + r.width / 2, cy = r.top + r.height / 2;
    if (r.width > 1 && r.height > 1 && cx > 0 && cx < w && cy > 70 && cy < h - 20) {
      return {x: cx, y: cy, w: w, h: h, found: true};
    }
  }
  return {x: null, y: null, w: w, h: h, found: false};
}
"""

# Per-tab organic ONLY. Clicks a random odds button, which merely OPENS the Quick Bet popover (places nothing).
# Deliberately references NO stake input and NO Place Bet control, so the organic open+dismiss gesture cannot
# submit a bet. The popover is closed again by _UI_CLOSE_JS.
_UI_RANDOM_OPEN_JS = r"""
() => {
  const btns = Array.from(document.querySelectorAll("button.market-btn"));
  if (!btns.length) return {ok: false};
  const b = btns[Math.floor(Math.random() * btns.length)];
  try { b.scrollIntoView({block: "center"}); } catch (e) {}
  b.click();
  return {ok: true};
}
"""


class PinnacleAdapter(BookAdapter):
    name = "pinnacle"

    def __init__(self) -> None:
        self._mode = os.environ.get("PINNACLE_ODDS_MODE", "ws").strip().lower()
        # Toggle the sidecar's OWN dedicated paho odds WS (the second, non-browser MQTT-over-WSS connection).
        # Default ON. Set PINNACLE_DEDICATED_WS=0 to stop the sidecar opening it — for the window-WS-reading work
        # (read odds from the browser's own WS instead) or to drop the extra connection entirely. Browser
        # session + catalog + pairing are unaffected; with it off and no alternative odds source wired, the cache
        # serves nothing fresh so the C# books stay empty (expected during the reading-path bring-up).
        self._dedicated_ws = os.environ.get("PINNACLE_DEDICATED_WS", "1") != "0"
        # WINDOW-WS READER (PINNACLE_WINDOW_WS_READ=1): take live odds from the browser's OWN WS (parsed in
        # PinnacleBrowserSession, handed here via _on_browser_odds) instead of the dedicated paho conn — same
        # cache path (_apply). Typically paired with PINNACLE_DEDICATED_WS=0 (browser WS is the only odds source).
        self._window_ws_read = os.environ.get("PINNACLE_WINDOW_WS_READ") == "1"
        self._browser_odds_last = 0.0    # unix ts of the last odds PUBLISH off the browser WS (feeds _feed_live)
        self._browser_odds_msgs = 0      # count of applied browser-WS odds messages (diagnostic)
        self._browser_odds_mid_ts: dict = {}   # "lid:mid" -> last ts the READER actually pushed odds for it
                                               # (coverage truth — distinct from /odds freshness, which _read_cache
                                               # re-stamps for any SERVED token; see /debug/reader + coverage_check)
        self._board_odds_lid_ts: dict = {}     # lid -> last ts seen on a SPORT-LEVEL topic (matchups/…/sp/{id}/…)
                                               # = the FEATURED BOARD's leagues (main page); a league tab uses
                                               # /lg/{lid}/ topics, so this is board-only → the tab manager skips
                                               # opening a DEDICATED tab for a league the board already streams
        try:
            # 30 min, not 5. The board's sport topic subscribes the WHOLE sport (measured 2026-08-06: 12
            # leagues streaming while 2 were rendered), but it is CHANGES-ONLY — a league with stable
            # pre-match prices simply goes quiet. A short TTL therefore reads "quiet" as "uncovered" and
            # opens a redundant dedicated tab for a league the board is already carrying.
            self._board_lid_ttl = float(os.environ.get("PINNACLE_BOARD_LID_TTL") or 1800.0)
        except ValueError:
            self._board_lid_ttl = 1800.0
        try:
            self._browser_odds_ttl = float(os.environ.get("PINNACLE_WINDOW_WS_TTL") or 30.0)
        except ValueError:
            self._browser_odds_ttl = 30.0
        # Connection-heartbeat TTL for the reader's _feed_live: a stable pre-match line stops re-pushing, so the
        # odds-recency TTL above false-deads it. odds_ws_alive() instead tracks ANY Arcadia frame (odds OR MQTT
        # keepalive), staying live through a quiet spell; this bounds how long after a real drop the feed reads
        # live (comfortably above the MQTT keepalive interval so pings keep it fresh).
        try:
            self._browser_ws_heartbeat_ttl = float(os.environ.get("PINNACLE_WINDOW_WS_HEARTBEAT_TTL") or 150.0)
        except ValueError:
            self._browser_ws_heartbeat_ttl = 150.0
        # READER-MODE PRICE BACKSTOP: the browser WS is CHANGES-ONLY, so a STABLE pre-match line never re-pushes
        # and a TAIL league (no tab, not on the board) would freeze at its one-time seed. So in pure-reader mode
        # a loop re-fetches every active league's straight markets from the GUEST API on this cadence. MUST be
        # comfortably < the C# HARDVEN_BOOK_FRESH_SEC gate (120) so a re-seeded stable line never ages out
        # between cycles (accounting for the walk time across all active leagues).
        try:
            self._reader_reseed_sec = float(os.environ.get("PINNACLE_READER_RESEED_SEC") or 90.0)
        except ValueError:
            self._reader_reseed_sec = 90.0
        # In-play does not run the periodic re-seed (see _reader_reseed_loop). Opt back in with =1.
        self._reseed_inplay = os.environ.get("PINNACLE_RESEED_INPLAY", "0") == "1"
        self._reseed_inplay_noted = False
        self._reader_reseed_task: Optional[asyncio.Task] = None
        # Re-seed SOURCE: "authed" (DEFAULT) hits /markets/straight on the logged-in session → REAL, non-delayed
        # prices (the guest feed can lag enough to swamp a 1¢ pre-live edge). It's the single most common request
        # the real web app makes (normal endpoint/headers/cadence), a SHORT authed GET — not the persistent
        # non-Chrome socket we removed — so the footprint is low + mostly pre-existing (balance already authed).
        # "guest" = public API (no session, zero account link) if you prefer that trade. The authed path is made
        # safe in reader mode by _rest_death_check treating the live reader WS as "up" (a re-seed blip won't kill).
        self._reseed_source = os.environ.get("PINNACLE_RESEED_SOURCE", "authed").strip().lower()
        self._api_key = os.environ.get("PINNACLE_API_KEY", DEFAULT_API_KEY)
        self._session = os.environ.get("PINNACLE_SESSION", "")
        self._device = os.environ.get("PINNACLE_DEVICE_UUID", "")
        self._ws_user = os.environ.get("PINNACLE_WS_USERNAME", "")
        self._ws_pass = os.environ.get("PINNACLE_WS_PASSWORD", "")
        self._catalog_leagues = [x.strip() for x in
                                 os.environ.get("PINNACLE_CATALOG_LEAGUES", "").split(",") if x.strip()]
        # sport ids whose leagues are EPHEMERAL (tennis=33: per-tournament-per-round, change daily/intraday) →
        # auto-discover today's leagues via /sports/{id}/leagues each catalog() call, instead of hand-listing.
        # DEFAULT from the unified catalog (sports.py, honors HARDVEN_SPORTS) so adding a sport there flows into
        # pairing with no separate env edit — same single-source-of-truth as _lifecycle_sports below. An explicit
        # PINNACLE_CATALOG_SPORTS still overrides (backward compat / to narrow catalog scope).
        _default_catalog_sports = ",".join(str(i) for i in sports_cfg.pinnacle_ids())
        self._catalog_sports = [x.strip() for x in
                                os.environ.get("PINNACLE_CATALOG_SPORTS", _default_catalog_sports).split(",")
                                if x.strip()]
        # DRIFT GUARD: an explicit PINNACLE_CATALOG_SPORTS that omits an ENABLED sport (HARDVEN_SPORTS) is the
        # silent-0-pairs trap — that sport SCHEDULES + SCAFFOLDS but its games never enter /catalog, so pairing
        # matches them against the wrong board → 0 fills. Warn loudly at startup (only when explicitly set + short).
        _missing_cat = [str(i) for i in sports_cfg.pinnacle_ids() if str(i) not in self._catalog_sports]
        if os.environ.get("PINNACLE_CATALOG_SPORTS") and _missing_cat:
            print(f"[PINNACLE] *** WARNING: PINNACLE_CATALOG_SPORTS={self._catalog_sports} is MISSING enabled sport "
                  f"id(s) {_missing_cat} (from HARDVEN_SPORTS). Those sports will schedule + scaffold but NEVER "
                  f"PAIR (catalog skips them → 0 pairs). Add them, or UNSET PINNACLE_CATALOG_SPORTS to track sports.py.")
        self._cache: dict[str, Selection] = {}            # "{lid}:{mid}:{designation}" -> Selection
        self._cache_lock = threading.Lock()               # paho thread writes; asyncio reads
        self._active_leagues: dict[str, float] = {}       # leagueId -> unix ts last requested via /odds
        self._subscribed: set[str] = set()                # leagueIds subscribed on the WS
        self._seeded: set[str] = set()                    # leagueIds REST-seeded once (pre-match snapshot)
        self._http: Optional[httpx.AsyncClient] = None     # authed (live odds seed + rest mode)
        self._guest_http: Optional[httpx.AsyncClient] = None  # guest (catalog/pairing — no session needed)
        # "{lid}:{parentMid}" -> ("{lid}:{liveMid}", {designation: name}, units). Learned from the WS push's
        # own parentId; see _apply. Lets a pair that holds the PRE-MATCH matchup follow the fixture in-play
        # without re-pairing and without asking the guest API anything.
        self._live_child: dict[str, tuple] = {}
        # ── WS state ──
        self._client = None
        self._connected = False
        self._ws_started = False                          # LAZY: WS connects only on the first /odds w/ a league
        self._ws_gave_up = False                          # SESSION-DEATH latch → stop retrying a DEAD session
        # Give up ONLY on genuine session death, NEVER on a transient drop (a real browser tab retries those
        # forever). Death signals: WS CONNACK auth-reject (rc 4/5) N× in a row, REST 401/403 M× in a row, or a
        # REST guest-redirect. A transient network/server drop keeps auto-reconnecting (paho 1–60s) — see _ws_watchdog.
        self._ws_auth_rejects = 0                         # consecutive WS CONNACK auth rejections (rc 4/5)
        self._rest_auth_fails = 0                         # consecutive REST 401/403 on AUTHED calls
        self._ws_auth_giveup = int(os.environ.get("PINNACLE_WS_AUTH_GIVEUP", "2"))
        self._rest_auth_giveup = int(os.environ.get("PINNACLE_REST_AUTH_GIVEUP", "3"))
        # A WS auth-reject with a still-LOGGED-IN browser is a stale x-session in paho's creds (a session rotation
        # paho reconnected through), NOT a dead login. Recover by forcing a browser RE-MINT (reload → fresh
        # x-session → pushed to paho), letting paho retry — instead of a permanent give-up. Cap the re-mints per
        # outage so a genuinely dead session still ends. See _on_connect (rc 4/5).
        self._loop: Optional[asyncio.AbstractEventLoop] = None   # main loop, captured in _start_ws (paho callbacks are off-loop)
        self._ws_remints = 0                              # re-mints attempted this outage (reset on a clean connect)
        self._ws_remint_cap = int(os.environ.get("PINNACLE_WS_REMINT_CAP", "6"))
        self._last_remint = 0.0
        self._remint_throttle_sec = float(os.environ.get("PINNACLE_WS_REMINT_THROTTLE_SEC", "30"))
        self._ws_watchdog_task: Optional[asyncio.Task] = None
        self._reconciler_task: Optional[asyncio.Task] = None   # staggered league subscribes (organic timing)
        self._status_task: Optional[asyncio.Task] = None       # browser-like /status liveness ping
        self._betslip_task: Optional[asyncio.Task] = None      # periodic betslip sweep (clears stray selections)
        self._betslip_sweep_sec = float(os.environ.get("HARDVEN_BETSLIP_SWEEP_SEC", "25"))
        self._status_ping_sec = float(os.environ.get("PINNACLE_STATUS_PING_SEC", "30"))
        self._subscribe_gap_sec = float(os.environ.get("PINNACLE_SUBSCRIBE_GAP_SEC", "3"))
        self._session_ka_task: Optional[asyncio.Task] = None   # session keepalive (vs inactivity logout)
        self._session_ka_sec = float(os.environ.get("PINNACLE_SESSION_KEEPALIVE_SEC", "240"))
        self._session_expired = False                          # terminal: a guest-redirect → stop everything
        # MASS-LOGOUT DETECTION (K2): a SINGLE authed-REST guest-redirect while the reader WS is live is a stale
        # replay blip (re-synced, not a logout). But a BURST — many leagues guest-redirecting in one window, or a
        # re-seed that suddenly returns 0 tokens — means the ACCOUNT x-session genuinely expired, even though board
        # odds keep streaming off the WS (board data is public, so "odds flowing" ≠ "logged in"). That burst forces
        # a real re-login. (Bet-safety while logged out is already covered: /balance guest-redirects → 0 → the
        # executor can't fund a buy → no bet.)
        self._guest_redirect_ts: list = []
        self._mass_logout_n = int(os.environ.get("PINNACLE_MASS_LOGOUT_REDIRECTS", "4"))
        self._mass_logout_window = float(os.environ.get("PINNACLE_MASS_LOGOUT_WINDOW", "30"))
        self._last_mass_logout = 0.0
        self._mass_logout_throttle = float(os.environ.get("PINNACLE_MASS_LOGOUT_THROTTLE", "120"))
        self._debug_ws = os.environ.get("PINNACLE_DEBUG_WS") == "1"  # log each WS cache update (prove live=WS)
        self._debug_status = os.environ.get("PINNACLE_DEBUG_STATUS") == "1"  # log market OFFLINE/suspend transitions
        if self._debug_ws:
            print("[PINNACLE] WARNING: PINNACLE_DEBUG_WS=1 logs EVERY WS odds update — the log grows ~18MB/day "
                  "(90MB over a week). Unset it for production / long unattended runs.")
        self._ws_dump_path = os.environ.get("PINNACLE_WS_DUMP", "")          # JSONL dump of EVERY incoming WS record
        self._ws_dump_fh = None                                              # (derivative recon: do Games matchups arrive?)
        # ── session SOURCE: "env" (DEFAULT — creds from PINNACLE_SESSION/.env, paste-the-token) or "browser"
        # (a managed, logged-in Playwright window mints + HOLDS the session and feeds creds in LIVE; see
        # pinnacle_session.py). In browser mode the feed stays idle until login is captured (_session_ready).
        self._session_source = os.environ.get("PINNACLE_SESSION_SOURCE", "env").strip().lower()
        self._browser = None                                   # PinnacleBrowserSession when source == "browser"
        self._tab_manager = None                               # LeagueTabManager when HARDVEN_TAB_MANAGER=1 (reader)
        self._tab_organic = None                               # TabOrganic: light per-tab human activity
        self._banking_mode = False                             # operator banking window: all automation frozen
        self._banking_task: Optional[asyncio.Task] = None
        self._camp_nav_page = None                             # page whose navigations invalidate the camp
        self._manual_mode = False                              # operator is driving the browser by hand
        self._manual_until = 0.0                               # 0 = until switched off; else auto-release at this ts
        self._manual_task: Optional[asyncio.Task] = None
        self._validate_task: Optional[asyncio.Task] = None      # proves a fresh capture before advertising it
        # CAPTURED != LOGGED IN. `_session_ready` means "credentials were seen"; a saved Chrome profile
        # replays a DEAD x-session and produces exactly that state, headers and all. `_session_proven` means
        # an authed call has actually COME BACK — the only evidence that distinguishes the two. Authed REST
        # waits on this one, so a startup that begins with a stale profile no longer fires a burst of
        # /leagues/*/markets/straight into a guest redirect before the re-login has even been attempted.
        self._session_proven = False
        self._proven_evt = asyncio.Event()
        self._bet_page = None                                  # cold last-resort bet tab (see _select_bet_tab)
        self._bet_cursor = None                                # tracked mouse pos for human-like placement moves
        self._tab_manager_on = os.environ.get("HARDVEN_TAB_MANAGER") == "1"
        self._session_ready = self._session_source != "browser"  # env mode = ready now; browser waits for login
        self._balance = 0.0                                    # last wallet amount (account currency, e.g. EUR)
        self._balance_currency = ""
        # ── BETTING (M1) SAFETY CONTRACT — established BEFORE any placement code so real money can never fire
        # without the explicit gate. Actual placement goes through the browser UI (bet slip) and is DEFERRED;
        # until then place_bet() previews only. HARDVEN_BET_ENABLE=1 is required for a real send; HARDVEN_MAX_STAKE
        # hard-caps the per-bet stake (account currency); _bet_lock serialises bets (one browser session = one at a
        # time). See HARDVEN_TODO §D/E.
        self._bet_enabled = os.environ.get("HARDVEN_BET_ENABLE") == "1"
        try:
            self._max_stake = float(os.environ.get("HARDVEN_MAX_STAKE") or 10.0)
        except ValueError:
            self._max_stake = 10.0
        # Post-bet hygiene: sweep stray selections out of the SIDE betslip via its 'Remove all' -> confirm flow
        # (probing/misfires can drop selections there). On by default; disable with HARDVEN_BETSLIP_TRIM=0.
        self._betslip_trim = os.environ.get("HARDVEN_BETSLIP_TRIM", "1") != "0"
        # My Bets: read it by NAVIGATING to the account/bets page and capturing the site's own `GET /0.1/bets`,
        # rather than calling that page-specific endpoint off-page (a correlation a server could notice).
        # `0` = direct REST instead. `_bets_page` is the reusable tab for that read.
        self._bets_via_page_on = os.environ.get("HARDVEN_BETS_VIA_PAGE", "1") != "0"
        self._bets_page = None
        self._bet_lock = asyncio.Lock()
        # LIFECYCLE: opt-in schedule-driven open/close of the browser (human session rhythm). Off = hold open.
        self._lifecycle_on = os.environ.get("PINNACLE_LIFECYCLE") == "1"
        # default the lifecycle sport ids from the unified catalog (respects HARDVEN_SPORTS); env still overrides
        _default_sports = ",".join(str(i) for i in sports_cfg.pinnacle_ids())
        self._lifecycle_sports = [int(s) for s in os.environ.get("PINNACLE_LIFECYCLE_SPORTS", _default_sports).split(",")
                                  if s.strip().isdigit()]

        def _cfg_int(name: str, default: int) -> int:
            try:
                return int((os.environ.get(name) or "").strip() or default)
            except ValueError:
                return default
        # window shaping (block selection): open PINNACLE_LEAD_MIN before a block, keep the densest
        # PINNACLE_MAX_BLOCKS (0 = unlimited) with ≥ PINNACLE_MIN_GAMES matches each.
        self._lifecycle_lead = _cfg_int("PINNACLE_LEAD_MIN", 15)
        self._lifecycle_max_blocks = _cfg_int("PINNACLE_MAX_BLOCKS", 4)
        self._lifecycle_min_games = _cfg_int("PINNACLE_MIN_GAMES", 1)
        self._lifecycle_session_hours = float(os.environ.get("PINNACLE_SESSION_HOURS", "0"))  # >0 = discrete Nh density-sessions
        self._lifecycle_manual_plan = os.environ.get("PINNACLE_MANUAL_PLAN", "").strip() or None  # test override (short cycle)
        # Hard bounds on total uptime (0 = off). Nothing else caps how long the bot can stay open: windows are
        # shaped around games, and merge_windows unions overlapping blocks/pins into ever-longer spans.
        self._lifecycle_min_downtime = float(os.environ.get("PINNACLE_MIN_DOWNTIME_MIN", "0") or 0)
        self._lifecycle_max_daily_hours = float(os.environ.get("PINNACLE_MAX_DAILY_HOURS", "0") or 0)
        # Treat the daily cap as a TARGET: stretch windows into unused budget (more pre-live watching time).
        self._lifecycle_fill_to_cap = os.environ.get("PINNACLE_FILL_TO_CAP", "1") != "0"
        self._lifecycle_today_only = os.environ.get("PINNACLE_SESSION_TODAY_ONLY", "1") != "0"  # plan only today's games (default ON)
        # These four were previously stuck at the constructor defaults with no way to tune them from the env.
        self._lifecycle_trail = _cfg_int("PINNACLE_TRAIL_MIN", 45)        # stay open this long past a block
        self._lifecycle_min_gap = _cfg_int("PINNACLE_MIN_GAP_MIN", 60)    # merge blocks closer than this
        self._lifecycle_horizon = _cfg_int("PINNACLE_HORIZON_HOURS", 36)  # how far ahead to plan
        self._lifecycle_recompute = float(os.environ.get("PINNACLE_RECOMPUTE_SEC", "3600"))
        # Schedule only around games PAIRED with a Kalshi market (an unpaired game can't be arbed, so it
        # shouldn't buy a session). Falls back to the full board if the pairing file is empty/stale.
        self._lifecycle_paired_only = os.environ.get("PINNACLE_SCHEDULE_PAIRED", "1") != "0"
        # Human wobble on window edges (deterministic per window, so it can't drift across recomputes).
        self._lifecycle_jitter = float(os.environ.get("PINNACLE_JITTER_MIN", "7"))
        # Operator-pinned LOCAL hours always included in the plan (e.g. "09:00-12:00" or two ranges
        # comma-separated) — "I find more arbs in the morning". Immune to min_games/max_blocks.
        self._lifecycle_pin_hours = os.environ.get("PINNACLE_PIN_HOURS", "").strip()
        # ALLOWED HOURS ("only run 05:00-08:00,10:00-12:00"). PINNACLE_ONLY_HOURS is the canonical name —
        # bare ONLY_HOURS is accepted because the two bots SHARE one .env and it is the obvious thing to
        # type, but the prefixed form should win where both exist.
        self._lifecycle_only_hours = (os.environ.get("PINNACLE_ONLY_HOURS")
                                      or os.environ.get("ONLY_HOURS") or "").strip()
        self._lifecycle = None
        self._lifecycle_task = None
        self._control = None          # ControlState (operator commands); only when the lifecycle runs
        self._balance_guard = None    # BalanceGuard; only when the lifecycle runs
        # LIVE FX (account currency → USD). The currency comes from the session status, so a USD account
        # short-circuits to 1.0 instead of having a EUR rate applied to dollars.
        from fx import FxProvider
        self._fx = FxProvider(currency_fn=lambda: (self.session_status() or {}).get("currency", ""))

        # AUTO-PAIR: opt-in scheduled re-pairing (startup + daily at HARDVEN_PAIR_HOUR local). Account-free
        # (Kalshi public + Pinnacle guest + the sidecar /catalog); the C# bot hot-reloads the result.
        self._auto_pair = os.environ.get("HARDVEN_AUTO_PAIR") == "1"
        self._pair_hour = _cfg_int("HARDVEN_PAIR_HOUR", 5)
        self._pair_startup_delay = _cfg_int("HARDVEN_PAIR_STARTUP_DELAY", 8)
        # intraday re-pair cadence (min): pairs LIVE/late-appearing games that the daily 5am run would miss.
        # Default 90 min — gentle (a handful of guest /catalog calls per run) and merge-safe (pairHard carries
        # filled pairs). Set HARDVEN_PAIR_INTERVAL_MIN=0 to restore daily-only re-pairing.
        self._pair_interval_min = _cfg_int("HARDVEN_PAIR_INTERVAL_MIN", 90)
        self._pairing = None
        self._pairing_task = None
        # in-play diagnostics: count WS messages per topic-class so a run reveals whether /live (in-play) is
        # actually being delivered (in-play arbs went missing after day 1 — see _refresh_league live-preserve fix).
        self._ws_live_msgs = 0
        self._ws_pre_msgs = 0
        self._requested_ids: set = set()   # selection ids the C# bot actually asks for (the PAIRED tokens) — to
                                           # measure how many WATCHED tokens are live vs the whole cache being live
        # ── REST-mode state ──
        self._refresh_task: Optional[asyncio.Task] = None
        self._refresh_sec = float(os.environ.get("PINNACLE_REFRESH_SEC", "15"))
        self._active_ttl = float(os.environ.get("PINNACLE_ACTIVE_TTL_SEC", "180"))
        self._jitter_ms = float(os.environ.get("PINNACLE_REQUEST_JITTER_MS", "250"))
        self._backoff_sec = 0.0
        self._rate_limited = False
        self._rl_total = 0
        self._last_hb = 0.0
        # session-lifetime instrumentation — measures Pinnacle's REAL inactivity-logout window (env
        # PINNACLE_SESSION_AGE_LOG_SEC, default 300s = 5m). A periodic "session held Xm" heartbeat + a final
        # "held Xm before this stop" on give-up turn the 2h keepalive/idle test into a precise measurement.
        self._session_started_at = 0.0                    # unix time the CURRENT session became live (0 = none)
        # POST-LOGIN SETTLE: capture fires the instant the credentials are visible on the wire, but the site is
        # still finishing its load and the account context isn't live server-side yet. An authed call inside
        # that gap answers 401 (2026-08-07: /wallet/balance 401'd seconds after a clean login). Wait it out.
        self._session_settle_sec = float(os.environ.get("PINNACLE_SESSION_SETTLE_SEC", "5"))
        self._session_age_task: Optional[asyncio.Task] = None
        self._session_age_log_sec = float(os.environ.get("PINNACLE_SESSION_AGE_LOG_SEC", "300"))
        self._survive_min = float(os.environ.get("PINNACLE_SESSION_SURVIVE_MIN", "35"))  # milestone: past the ~30m danger zone
        self._survive_logged = False                      # one-time "SURVIVED" flag per session (unattended pass/fail)

    # ── lifecycle ──────────────────────────────────────────────────────────────
    async def _start_fx(self) -> None:
        """Fetch the rate once up front (so the very first bet is sized on a live number), then keep it fresh."""
        try:
            st = await self._fx.refresh()
            print(f"[FX] {st['currency']}/USD = {st['rate']:.4f} ({st['source']}); "
                  f"env HARDVEN_FX_TO_USD={st['env_rate']:.4f}"
                  + (f" — drift {st['env_drift_pct']:+.2f}%" if st.get("env_drift_pct") is not None else ""))
        except Exception as ex:
            print(f"[FX] initial refresh failed ({type(ex).__name__}: {ex}) - using env rate {self._fx.rate}")
        self._fx.start()

    async def startup(self) -> None:
        await self._start_fx()          # size the very first bet on a LIVE rate, not a hand-set env var
        self._http = httpx.AsyncClient(
            headers={"accept": "application/json", "content-type": "application/json",
                     "origin": "https://www.pinnacle.bet", "referer": "https://www.pinnacle.bet/",
                     "user-agent": USER_AGENT, "x-api-key": self._api_key,
                     "x-device-uuid": self._device, "x-session": self._session},
            timeout=15.0)
        if self._session_source == "browser":
            # Launch the managed login window FIRST (non-blocking) and let creds arrive via the callback. The
            # feed (REST seed + WS) gates itself on _session_ready, so startup returns promptly → FastAPI serves
            # /health right away (the C# bot sees the sidecar is up, session_ready=false) while you log in.
            # A browser-launch failure (Chrome missing, profile locked, no display) must NOT kill the sidecar:
            # catalog/pairing still works via the GUEST API, and you can fall back to PINNACLE_SESSION_SOURCE=env.
            try:
                from pinnacle_session import PinnacleBrowserSession
                self._browser = PinnacleBrowserSession(
                    self._on_browser_creds,
                    on_odds=self._on_browser_odds if self._window_ws_read else None,
                    on_idle_trim=self._trim_betslip)
                # Report PAYLOAD-derived coverage on the [WS-READ] line. The topic scan there only sees
                # subscription scopes, and the featured board's is sport-wide ('sp/33'), so it can never name
                # the individual leagues on the main page — rec.league.id can, and that is what this counts.
                self._browser.coverage_fn = self._ws_coverage_stats
                if self._lifecycle_on:
                    # schedule-driven: the lifecycle task opens/closes the browser per the game windows (the
                    # window opens it the first time too — don't start it here). Stays dark until the first one.
                    from lifecycle import PinnacleLifecycle
                    self._lifecycle = PinnacleLifecycle(self._browser, self._lifecycle_sports,
                                                        on_open=self._on_session_opening,
                                                        on_close=self._on_session_closed,
                                                        lead_min=self._lifecycle_lead,
                                                        trail_min=self._lifecycle_trail,
                                                        min_gap_min=self._lifecycle_min_gap,
                                                        horizon_hours=self._lifecycle_horizon,
                                                        recompute_sec=self._lifecycle_recompute,
                                                        min_games=self._lifecycle_min_games,
                                                        max_blocks=(self._lifecycle_max_blocks or None),
                                                        session_hours=self._lifecycle_session_hours,
                                                        manual_plan=self._lifecycle_manual_plan,
                                                        today_only=self._lifecycle_today_only,
                                                        paired_only=self._lifecycle_paired_only,
                                                        jitter_min=self._lifecycle_jitter,
                                                        pin_hours=self._lifecycle_pin_hours,
                                                        only_hours=self._lifecycle_only_hours,
                                                        min_downtime_min=self._lifecycle_min_downtime,
                                                        max_daily_hours=self._lifecycle_max_daily_hours,
                                                        fill_to_cap=self._lifecycle_fill_to_cap,
                                                        on_banking=self._on_banking)
                    # Operator control: restore any persisted pins/override/toggles BEFORE the loop starts,
                    # so a pause or balance-halt set before a restart is honoured on the very first tick.
                    import schedule as _sched
                    from control import ControlState
                    self._control = ControlState()
                    self._lifecycle._control = self._control
                    if self._control.pins:
                        self._lifecycle._pin_ranges = _sched.parse_pin_hours(",".join(self._control.pins))
                    if self._control.schedule:
                        for k, v in self._control.schedule.items():
                            attr = {"lead_min": "_lead_min", "trail_min": "_trail_min",
                                    "min_gap_min": "_min_gap_min", "min_games": "_min_games",
                                    "max_blocks": "_max_blocks", "session_hours": "_session_hours",
                                    "jitter_min": "_jitter_min", "horizon_hours": "_horizon",
                                    "paired_only": "_paired_only", "today_only": "_today_only"}.get(k)
                            if attr:
                                setattr(self._lifecycle, attr, v)
                    self._lifecycle.restore_override(self._control.override, self._control.reason,
                                                     self._control.override_until)
                    self._lifecycle_task = asyncio.create_task(self._lifecycle.run())
                    from balance_guard import BalanceGuard
                    self._balance_guard = BalanceGuard(self, self._lifecycle, self._lifecycle._notify)
                    self._balance_guard.start()
                    mode = (f"MANUAL PLAN {self._lifecycle_manual_plan}" if self._lifecycle_manual_plan
                            else f"{self._lifecycle_session_hours:g}h density-sessions" if self._lifecycle_session_hours > 0
                            else f"gap-merge, densest {self._lifecycle_max_blocks} blocks")
                    print(f"[PINNACLE] session source = BROWSER + LIFECYCLE (sports={self._lifecycle_sports}, "
                          f"{mode}, lead {self._lifecycle_lead}m) — the browser opens/closes on the game "
                          "schedule; dark between sessions.")
                    # Say out loud whether the hours are RESTRICTED, and whether the value actually parsed.
                    # A silently-dropped time spec is how PINNACLE_PIN_HOURS went unnoticed for three days.
                    if self._lifecycle_only_hours:
                        got = self._lifecycle._only_ranges
                        if got:
                            spec = ",".join(f"{h1:02d}:{m1:02d}-{h2:02d}:{m2:02d}" for h1, m1, h2, m2 in got)
                            print(f"[PINNACLE] ONLY-HOURS ACTIVE [{spec}] local — the bot will NEVER be up "
                                  f"outside these ranges. Applied after every other rule; pins are not exempt.")
                        else:
                            print(f"[PINNACLE] WARNING ONLY_HOURS={self._lifecycle_only_hours!r} parsed to "
                                  f"NOTHING — the restriction is OFF and the bot will run its normal schedule. "
                                  f"Want 'HH:MM-HH:MM[,HH:MM-HH:MM]' in LOCAL time.")
                    # TAB MANAGER UNDER LIFECYCLE. This used to be refused ("the browser cycles per block"),
                    # which quietly broke a combination that is now the DEFAULT: without a tab manager there is
                    # no roving tab, so verify_now() answers "no tab manager (rove disabled)" and, with
                    # HARDVEN_REQUIRE_WS_VERIFIED=1, every arb needing verification is SKIPPED — the bot looks
                    # healthy and simply never fires. The manager already tolerates its tabs disappearing, so
                    # it is created here and started/stopped alongside each scheduled session instead.
                    if self._tab_manager_on and self._window_ws_read:
                        from tab_manager import LeagueTabManager
                        pairs_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                                  "cross_pairs.json")
                        self._tab_manager = LeagueTabManager(self._browser, self.reader_live_mids, pairs_path,
                                                             board_lids_fn=self.board_lids,
                                                             board_dom_fn=self.board_all_lids)
                        print("[PINNACLE] tab manager ARMED under lifecycle — starts with each work window, "
                              "stops when the window closes (rove tab is what verify_now drives).")
                        # Per-tab human activity, same as the non-lifecycle path: N reader tabs that never
                        # move is its own tell. Reads tabs live via reader_tabs, so it survives the manager
                        # being stopped and restarted between windows.
                        if os.environ.get("HARDVEN_TAB_ORGANIC", "1") != "0":
                            from organic import TabOrganic
                            try:
                                pop_chance = float(os.environ.get("HARDVEN_TAB_POPOVER_CHANCE", "0.15"))
                            except ValueError:
                                pop_chance = 0.15
                            self._tab_organic = TabOrganic(
                                self._tab_manager.reader_tabs, _UI_CLOSE_JS, _UI_RANDOM_OPEN_JS,
                                popover_chance=pop_chance, trim_fn=self._trim_betslip)
                            self._tab_organic.start()
                    elif self._tab_manager_on:
                        print("[PINNACLE] HARDVEN_TAB_MANAGER=1 needs PINNACLE_WINDOW_WS_READ=1 — not started.")
                else:
                    await self._browser.start()
                    print("[PINNACLE] session source = BROWSER — log in to the window; the feed waits for "
                          "capture, then seeds + connects automatically.")
                    if self._tab_manager_on and self._window_ws_read:
                        from tab_manager import LeagueTabManager
                        pairs_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                                  "cross_pairs.json")
                        self._tab_manager = LeagueTabManager(self._browser, self.reader_live_mids, pairs_path,
                                                             board_lids_fn=self.board_lids,
                                                             board_dom_fn=self.board_all_lids)
                        self._tab_manager.start()
                        # Light per-tab human activity across the reader tabs so the browser isn't 1 live tab +
                        # N dead ones. Off with HARDVEN_TAB_ORGANIC=0. Interlocked with bets via _pause_all_organic.
                        if os.environ.get("HARDVEN_TAB_ORGANIC", "1") != "0":
                            from organic import TabOrganic
                            try:
                                pop_chance = float(os.environ.get("HARDVEN_TAB_POPOVER_CHANCE", "0.15"))
                            except ValueError:
                                pop_chance = 0.15
                            self._tab_organic = TabOrganic(
                                self._tab_manager.reader_tabs, _UI_CLOSE_JS, _UI_RANDOM_OPEN_JS,
                                popover_chance=pop_chance, trim_fn=self._trim_betslip)
                            self._tab_organic.start()
                    elif self._tab_manager_on:
                        print("[PINNACLE] HARDVEN_TAB_MANAGER=1 needs PINNACLE_WINDOW_WS_READ=1 (the reader) to be "
                              "useful — not starting the tab manager.")
            except Exception as ex:
                self._browser = None
                print(f"[PINNACLE] BROWSER session launch FAILED ({type(ex).__name__}: {ex}). Sidecar stays up — "
                      "catalog/pairing still works (guest API); for live odds, fix the browser or set "
                      "PINNACLE_SESSION_SOURCE=env with a fresh PINNACLE_SESSION.")
        if self._mode == "rest":
            self._refresh_task = asyncio.create_task(self._refresh_loop())
            print(f"[PINNACLE] ready — REST poll mode (gentle serial, {self._refresh_sec:g}s). "
                  "Odds id = '<leagueId>:<matchupId>:<designation>'.")
        elif not self._dedicated_ws:
            src = ("odds come from the browser-window WS READER (PINNACLE_WINDOW_WS_READ=1) — keep a sport board "
                   "open so its WS stays subscribed." if self._window_ws_read else
                   "no odds source active — set PINNACLE_WINDOW_WS_READ=1 for the browser-window reader, or =1 here.")
            print("[PINNACLE] ready — WS mode, DEDICATED WS DISABLED (PINNACLE_DEDICATED_WS=0): the sidecar will "
                  f"NOT open its own paho odds connection. Session/catalog/pairing run as normal; {src}")
            if self._window_ws_read:
                self._reader_reseed_task = asyncio.create_task(self._reader_reseed_loop())
                print(f"[PINNACLE] reader price backstop ON — {self._reseed_source} re-seed of every active league "
                      f"every {self._reader_reseed_sec:g}s (keeps stable/tail pre-live lines fresh; the WS gives "
                      "live in-play deltas). _read_cache serves the REAL per-token ts so frozen books age out."
                      + ("" if self._reseed_inplay else
                         " PAUSES while in-play is running (PINNACLE_RESEED_INPLAY=1 to keep it on).")
                      + ("" if self._reseed_source == "guest" else
                         " (authed = real prices; guest can lag a thin edge — PINNACLE_RESEED_SOURCE=guest to switch)"))
        else:
            extra = " + WINDOW-WS READER also on" if self._window_ws_read else ""
            print("[PINNACLE] ready — WS mode (LAZY: connects to Pinnacle on the FIRST /odds that names a "
                  f"league; nothing is sent before that){extra}. Odds id = '<leagueId>:<matchupId>:<designation>'.")

        # session-age heartbeat (persistent across logout/recovery). Env mode is live from startup → mark now;
        # browser mode marks on capture (_on_browser_creds).
        self._session_age_task = asyncio.create_task(self._session_age_heartbeat())
        # Betslip hygiene sweep: clears stray selections on every tab, independent of organic cadence.
        if self._betslip_trim and self._session_source == "browser":
            self._betslip_task = asyncio.create_task(self._betslip_sweep_loop())
            print(f"[PINNACLE] betslip sweep ON — clearing stray side-betslip selections on every tab "
                  f"every {self._betslip_sweep_sec:g}s (HARDVEN_BETSLIP_TRIM=0 to disable).")
        if self._session_source != "browser":
            self._mark_session_started("env token")

        # AUTO-PAIR: schedule the daily re-pairing pipeline (account-free; independent of the session/mode).
        if self._auto_pair:
            from pairing_scheduler import PairingScheduler
            self._pairing = PairingScheduler(
                hour=self._pair_hour, initial_delay=self._pair_startup_delay,
                interval_min=self._pair_interval_min,
                # Only the browser source can be "captured but dead"; env/rest sources are proven by
                # construction, so passing the event only there keeps their behaviour unchanged.
                wait_ready=(self._proven_evt if self._session_source == "browser" else None),
                wait_ready_sec=float(os.environ.get("HARDVEN_PAIR_WAIT_SESSION_SEC", "90")))
            self._pairing_task = asyncio.create_task(self._pairing.run())
            cadence = (f"every {self._pair_interval_min} min (intraday — pairs live/late-appearing games)"
                       if self._pair_interval_min > 0 else f"daily {self._pair_hour:02d}:00 local")
            print(f"[PINNACLE] AUTO-PAIR on — pairing at startup (+{self._pair_startup_delay}s) then {cadence}. "
                  f"cross_pairs.json + derivative_pairs.json hot-reload into the bot.")

    async def shutdown(self) -> None:
        if self._refresh_task and not self._refresh_task.done():
            self._refresh_task.cancel()
        if self._ws_watchdog_task and not self._ws_watchdog_task.done():
            self._ws_watchdog_task.cancel()
        for t in (self._reconciler_task, self._status_task, self._session_ka_task,
                  self._lifecycle_task, self._pairing_task, self._session_age_task,
                  self._reader_reseed_task, self._betslip_task):
            if t and not t.done():
                t.cancel()
        if self._client is not None:
            try:
                self._client.loop_stop()
                self._client.disconnect()
            except Exception:
                pass
        if self._tab_organic is not None:
            try:
                await self._tab_organic.stop()
            except Exception:
                pass
        if self._tab_manager is not None:
            try:
                await self._tab_manager.stop()               # cancel the loop + close its tabs before the browser
            except Exception:
                pass
        if self._browser is not None:
            try:
                await self._browser.stop()
            except Exception:
                pass
        if self._ws_dump_fh is not None:
            try:
                self._ws_dump_fh.close()
            except Exception:
                pass
        if self._guest_http:
            try:
                await self._guest_http.aclose()
            except Exception:
                pass
        if self._http:
            try:
                await self._http.aclose()
            except Exception:
                pass

    @staticmethod
    def _parse_sid(sid: str):
        """Token → tuple whose [1] is the leagueId (used to track active leagues), for BOTH shapes:
        moneyline 3-seg → ('moneyline', lid, mid, designation);
        derivative 5-seg → ('spread'|'total', lid, mid, points: float, side). None if neither."""
        p = sid.split(":")
        if len(p) == 3 and p[0] and p[1] and p[2] in _SIDES:
            return ("moneyline", p[0], p[1], p[2])
        if len(p) == 5 and p[0] and p[1] and p[2] in ("spread", "total"):
            try:
                pts = float(p[3])
            except ValueError:
                return None
            if (p[2] == "spread" and p[4] in ("home", "away")) or (p[2] == "total" and p[4] in ("over", "under")):
                return (p[2], p[0], p[1], pts, p[4])
        return None

    async def odds(self, selection_ids: list[str]) -> dict[str, Selection]:
        now = time.time()
        self._requested_ids.update(selection_ids)   # remember the WATCHED (paired) tokens for the live diagnostic
        for sid in selection_ids:
            p = self._parse_sid(sid)
            if p:
                self._active_leagues[p[1]] = now   # p[1] = leagueId for both moneyline + derivative tokens
        # BROWSER session source, not logged in yet → no valid creds to seed/connect with. Serve the (empty)
        # cache so /odds still answers; the C# freshness gate keeps the books cleared until odds flow.
        if self._session_source == "browser" and not self._session_ready:
            return self._read_cache(selection_ids, now)
        if self._mode == "ws":
            # REST-seed each new league ONCE: the WS streams CHANGES, not an on-subscribe snapshot, so a
            # stable pre-match line never arrives over the WS until it moves. One /markets/straight snapshot
            # populates every current game (pre-match + live); the WS keeps them fresh after. Mimics the
            # browser exactly (initial REST snapshot, then WS — no re-polling).
            for lid in [l for l in list(self._active_leagues.keys()) if l not in self._seeded]:
                self._seeded.add(lid)                     # add before await so concurrent /odds don't double-seed
                if self._window_ws_read and not self._dedicated_ws:
                    await self._reseed_league(lid)        # reader mode: seed from PINNACLE_RESEED_SOURCE (authed
                else:                                     # by default → real prices); loop then keeps it fresh
                    await self._refresh_league(lid)
            if not self._ws_started and self._active_leagues:
                self._start_ws()                          # LAZY connect (the reconciler subscribes leagues gradually)
        return self._read_cache(selection_ids, now)

    def _pair_side_name(self, sid: str) -> str:
        """The Pinnacle name this token BUYS, per the pairing file. Sync twin of _expected_from_pairs's lookup
        (that one is async and this runs inside the cache lock's caller). Empty when unknown."""
        cache = getattr(self, "_pair_names", None)
        if cache is None or time.time() - getattr(self, "_pair_names_ts", 0) > 300:
            cache = {}
            try:
                path = Path(__file__).parent.parent / "cross_pairs.json"
                for e in json.loads(path.read_text(encoding="utf-8")):
                    yn = (e.get("hardven_yes_name") or "").strip()
                    nn = (e.get("hardven_no_name") or "").strip()
                    if not (yn and nn):
                        continue
                    yt, nt = e.get("hardven_yes_token") or "", e.get("hardven_no_token") or ""
                    if yt.count(":") >= 2:
                        cache[yt] = (yn, nn)
                    if nt.count(":") >= 2:
                        cache[nt] = (nn, yn)
            except Exception:
                cache = getattr(self, "_pair_names", None) or {}
            self._pair_names, self._pair_names_ts = cache, time.time()
        got = cache.get(sid)
        return got[0] if got else ""

    def _redirect_sid(self, sid: str) -> str:
        """A retired pre-match token -> the live matchup's token for the SAME side. "" when it cannot be done.

        MATCHED BY NAME, NOT BY DESIGNATION. Carrying ':home' across to the new matchup would be one
        assumption away from buying the wrong player, and that failure does not announce itself — it books
        as a locked arb with both legs on one outcome (see the 2026-08-19 fires). So the side is resolved by
        the name the pairing recorded for this token, checked against the live matchup's own participants.
        A row with no recorded name is not redirected at all: unknown is a safe answer, wrong is not.

        `(Games)` children are refused too. Pinnacle spawns a games-count matchup alongside the sets one with
        the same players, and its moneyline is a different market entirely.
        """
        parts = sid.split(":")
        if len(parts) != 3 or parts[2] not in _SIDES:
            return ""
        got = self._live_child.get(f"{parts[0]}:{parts[1]}")
        if not got:
            return ""
        child, names, units = got
        if "game" in (units or "").lower():
            return ""
        want = _norm_name(self._pair_side_name(sid))
        if not want:
            return ""
        for desig, nm in names.items():
            if _norm_name(nm) == want:
                return f"{child}:{desig}"
        return ""

    def _feed_live(self) -> bool:
        """True only while the Pinnacle feed is GENUINELY live — WS connected, session not expired, and (browser
        source) logged in. Drives the ts-freshness stamp below: while live, a stable price is still fresh (the WS
        would push any change/suspend); when NOT live, we serve the STORED ts so it AGES → the C# freshness gate
        clears the book → NO arb is ever computed on a frozen/stale Pinnacle number after a logout/disconnect."""
        if self._session_expired:
            return False
        if self._session_source == "browser" and not self._session_ready:
            return False
        if self._connected:
            return True                                   # dedicated paho WS connected
        # WINDOW-WS READER path: liveness = the browser's Arcadia WS is still CONNECTED (delivering frames, incl.
        # MQTT keepalive), a connection heartbeat — NOT an odds-recency gate. This keeps a stable pre-match line
        # LIVE through a quiet spell (no line moving) while still ageing out on a real socket drop / logout. Falls
        # back to the odds-recency gate if the session handle is momentarily unavailable.
        if self._window_ws_read:
            if self._browser is not None:
                try:
                    if self._browser.odds_ws_alive(self._browser_ws_heartbeat_ttl):
                        return True
                except Exception:
                    pass
            if self._browser_odds_last and (time.time() - self._browser_odds_last) < self._browser_odds_ttl:
                return True
        return False

    def _read_cache(self, selection_ids: list[str], now: float) -> dict[str, Selection]:
        out: dict[str, Selection] = {}
        live = self._feed_live()
        # PURE-READER MODE serves the REAL per-token ts: the browser WS is changes-only and coverage is PARTIAL
        # (only tab'd/board leagues stream), so a GLOBAL "fresh while connected" stamp would serve a frozen
        # TAIL-league seed as fresh → phantom arb. Instead every active league is re-seeded from the guest API
        # (<gate cadence) so a genuinely-live token's ts stays recent; a token that stops updating (league gone,
        # market pulled) ages out via the C# gate. paho/legacy modes keep the global stamp (they subscribe every
        # active league, so nothing is frozen).
        reader_mode = self._window_ws_read and not self._dedicated_ws
        with self._cache_lock:
            for sid in selection_ids:
                s = self._cache.get(sid)
                if not s:
                    # FOLLOW THE FIXTURE IN-PLAY. Nothing is cached for this token because Pinnacle retired
                    # the matchup when the match went live (see _apply). If the push told us which matchup
                    # replaced it, serve THAT price under the id the caller asked for, so a pair written
                    # pre-match keeps working in-play with no re-pairing and no extra request.
                    red = self._redirect_sid(sid)
                    if red:
                        s = self._cache.get(red)
                    if not s:
                        continue
                # WS push: stamp ts=now WHILE the feed is LIVE (connection = freshness; a stable price won't
                # re-tick but is still live — Pinnacle pushes any change/suspend). Not live (disconnected / given
                # up / logged out) → serve stored ts → it ages → C# clears. REST mode: the poller already stamps
                # ts on each fetch, so serve it as-is.
                ts = s.ts if reader_mode else (now if (self._mode == "ws" and live) else s.ts)
                # Pass through the cached STATUS ("open" / "suspended") so an OFFLINE Pinnacle market reaches
                # the C# as suspended → empty book → no arb. (Was hardcoded "open", which hid suspensions.)
                status = s.status
                # OFFLINE gate (poll-time): a matchup that closes betting STOPS being pushed, so its cached
                # "open" token is never reconciled away, and the GLOBAL-liveness stamp above keeps serving it
                # FRESH (ts=now) even 8 min after Pinnacle went silent on it → phantom arb on a frozen line.
                # cutoffAt is the authoritative betting-close time: once it passes, force suspended here so the
                # C# gets an empty book — independent of push activity, with the WS still connected.
                if s.cutoff and s.cutoff <= now:
                    status = "suspended"
                out[sid] = Selection(s.selection_id, s.decimal_odds, s.max_stake, status, ts, s.live, s.cutoff)
        return out

    async def _validate_capture(self) -> None:
        """Prove a freshly-captured session is ALIVE before the bot is told the venue is up.

        Probes the authed wallet endpoint (the page polls it constantly, so it is not an unusual call) after the
        settle wait. `balance()` already answers None for 401 / guest-redirect / malformed, which is exactly the
        "unreadable" signal we need — a real zero balance returns 0.0 and still proves the session works.

        On failure we do NOT flip `_session_ready` false by hand: the browser owns that flag, and lying in the
        other direction would fight the login watcher. Instead we mark the session expired, which is the same
        state a guest-redirect burst produces — the existing recovery (reload + credential submit) then runs,
        and the next capture re-validates. Net effect: the bot never advertises a session it hasn't used once.
        """
        try:
            bal = await self.balance()
        except Exception as ex:
            print(f"[PINNACLE] session validation errored ({type(ex).__name__}: {ex}) — treating as UNPROVEN.")
            bal = None
        if bal is not None:
            self._session_proven = True
            try:
                self._proven_evt.set()
            except Exception:
                pass
            print(f"[PINNACLE] session VALIDATED — authed call succeeded (wallet {bal:.2f} "
                  f"{self._balance_currency or ''}). Authed REST is now UNBLOCKED. The bot is GO.")
            return
        self._session_proven = False
        try:
            self._proven_evt.clear()
        except Exception:
            pass
        self._session_expired = True
        print("[PINNACLE] *** CAPTURED SESSION FAILED VALIDATION *** — the authed probe did not come back. "
              "This is the saved profile replaying a DEAD x-session, not a live login. NOT advertising the "
              "venue as up; forcing the re-login path instead.")

    # ── browser session source: receive live creds + expose status ────────────────
    def _on_browser_creds(self, creds: dict) -> None:
        """Callback from PinnacleBrowserSession on every credential change. Pushes the freshest x-session /
        device / api-key into the live httpx headers and the WS password into paho (so its next reconnect
        authenticates with the latest token). A NEW x-session (e.g. guest→logged-in, or a rotation after a
        give-up) clears the terminal latches so the feed can come back to life."""
        old_session = self._session
        sess = creds.get("session") or ""
        if sess:
            self._session = sess
            if self._http is not None:
                self._http.headers["x-session"] = sess
        dev = creds.get("device") or ""
        if dev:
            self._device = dev
            if self._http is not None:
                self._http.headers["x-device-uuid"] = dev
        key = creds.get("api_key") or ""
        if key:
            self._api_key = key
            if self._http is not None:
                self._http.headers["x-api-key"] = key
        ws_user = creds.get("ws_user") or ""
        ws_pass = creds.get("ws_pass") or ""
        if ws_user:
            self._ws_user = ws_user
        ws_pass_changed = bool(ws_pass) and ws_pass != self._ws_pass   # a re-captured suffix rotates this
        if ws_pass:
            self._ws_pass = ws_pass
            if self._client is not None:                  # live paho client → use fresh creds on next reconnect
                try:
                    self._client.username_pw_set(self._ws_user, self._ws_pass)
                except Exception:
                    pass
        was_ready = self._session_ready
        self._session_ready = bool(creds.get("ready"))
        # A fresh capture is UNPROVEN until _validate_capture says otherwise, even when it looks ready:
        # "looks ready" is precisely the state a replayed dead x-session produces.
        if not self._session_ready:
            self._session_proven = False
            try:
                self._proven_evt.clear()
            except Exception:
                pass
        # A NEW live session begins on the ready FALSE→TRUE transition (initial login OR recovery-after-logout) OR
        # when the token ROTATES while already live. NB: on initial login the session value is stored above before
        # `ready` flips true, so `sess != old_session` is false by now — the became_ready check is what catches it.
        became_ready = self._session_ready and not was_ready
        rotated = self._session_ready and bool(sess) and sess != old_session
        if became_ready or rotated:
            self._session_expired = False
            self._ws_auth_rejects = self._rest_auth_fails = 0   # fresh creds → clear the death streaks
            self._mark_session_started("browser login" if became_ready else "session rotated")  # (re)start age tracking
            # VALIDATE, don't assume. A page restoring from the SAVED profile replays a STALE x-session before it
            # re-authenticates, and capturing the first auth header we see declared "the bot is GO" on a session
            # that was dead on arrival (2026-08-07: /wallet/balance 401 + ~20 guest-redirects + a WS give-up,
            # recovered only via mass-logout detection). Under --live that false GO is worse than noise: the C#
            # bot fires the REAL Kalshi leg believing the venue is up, HardVen refuses, and recovery has to unwind
            # a naked leg. One cheap authed probe settles it.
            if self._validate_task is None or self._validate_task.done():
                self._validate_task = asyncio.create_task(self._validate_capture())
            if self._ws_gave_up:                          # recover from a terminal give-up so /odds restarts the feed
                self._ws_gave_up = False
                self._ws_started = False                  # next odds() relands _start_ws() with the new creds
                self._seeded.clear()                      # re-seed pre-match snapshots under the new session
        # A re-captured SUFFIX changes the WS password WITHOUT changing the x-session (so neither became_ready nor
        # rotated fires) — but a given-up odds WS must still restart with it, since a stale suffix is a top cause
        # of the CONNACK auth-reject. Restart on any fresh WS creds after a give-up.
        if self._ws_gave_up and self._session_ready and ws_pass_changed:
            self._ws_gave_up = False
            self._ws_started = False
            self._ws_auth_rejects = self._ws_remints = 0
            self._session_expired = False
            self._seeded.clear()
            print("[PINNACLE] re-captured WS creds (suffix) → restarting the odds WS (was given up).")
        if became_ready:
            print("[PINNACLE] OK browser session ready — feed will seed + connect on the next /odds.")

    def session_status(self) -> dict:
        st = {"source": self._session_source, "ready": self._session_ready,
              "mode": self._mode, "ws_connected": self._connected, "cache_sel": len(self._cache),
              "balance": self._balance, "currency": self._balance_currency}
        if self._browser is not None:
            st["browser"] = self._browser.status()
        if self._lifecycle is not None:
            lc = self._lifecycle.status()
            st["lifecycle"] = lc
            # scheduled_dark = the browser is intentionally DOWN for a dark window (not a logout). Lets the C#
            # heartbeat stay quiet on a planned close and alert ONLY on an unexpected drop during an open window.
            st["scheduled_dark"] = (lc.get("state") == "dark")
        return st

    # ── lifecycle hooks (called by PinnacleLifecycle on scheduled open/close) ──────
    def _on_session_opening(self) -> None:
        """A scheduled window is opening the browser → RESET the feed latches so the WS restarts fresh once
        creds arrive — unconditionally, since a reopened profile may re-issue the SAME x-session (the value-
        change check in _on_browser_creds wouldn't fire). session_ready stays False until creds are captured."""
        self._ws_gave_up = False
        self._ws_started = False
        self._session_expired = False
        self._ws_auth_rejects = self._rest_auth_fails = 0
        self._seeded.clear()
        # Bring the league tabs back up with the session. Its own start delay covers the browser launch that
        # follows this hook, and a failed open_tab is handled per-tick, so starting early is safe.
        if self._tab_manager is not None:
            try:
                self._tab_manager.start()
            except Exception as ex:
                print(f"[PINNACLE] tab manager start failed: {type(ex).__name__}: {ex}")

    def _on_session_closed(self) -> None:
        """A scheduled window closed the browser → stand the feed DOWN: gate odds (no creds now) and stop the
        WS/keepalive so we don't poke Pinnacle during the dark stretch. The C# freshness gate clears the books."""
        self._session_ready = False
        self._give_up_ws("scheduled dark window", clean=True)
        # Stand the tab manager down too. The browser is already stopped by the time this hook runs, so its
        # tabs are gone — stop() only needs to cancel the loop and forget them (it tolerates dead pages).
        if self._tab_manager is not None:
            try:
                asyncio.create_task(self._tab_manager.stop())
            except Exception as ex:
                print(f"[PINNACLE] tab manager stop failed: {type(ex).__name__}: {ex}")

    # ── WS (MQTT) odds source ────────────────────────────────────────────────
    def _start_ws(self) -> None:
        self._ws_started = True
        if not self._dedicated_ws:
            return   # dedicated WS disabled (PINNACLE_DEDICATED_WS=0) — no paho connection; announced at startup
        try:
            import paho.mqtt.client as mqtt
        except Exception:
            print("[PINNACLE WS] paho-mqtt not installed (`pip install paho-mqtt`). Falling back to REST mode.")
            self._mode = "rest"
            self._refresh_task = asyncio.create_task(self._refresh_loop())
            return
        if not self._ws_user or not self._ws_pass:
            print("[PINNACLE WS] WARNING: PINNACLE_WS_USERNAME / PINNACLE_WS_PASSWORD not set — set from the WS "
                  "CONNECT frame (username = account id, password = '{x-session}|{suffix}').")
        cid = "sub-" + "".join(random.choices(
            "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789", k=16))
        try:
            self._client = mqtt.Client(client_id=cid, transport="websockets",
                                       callback_api_version=mqtt.CallbackAPIVersion.VERSION1)
        except (TypeError, AttributeError):
            self._client = mqtt.Client(client_id=cid, transport="websockets")   # paho < 2.0
        self._client.username_pw_set(self._ws_user, self._ws_pass)
        # The real browser's WS upgrade carries ONLY Origin + User-Agent (NO x-api-key, NO cookies — auth is
        # entirely in the MQTT CONNECT username/password). Match it exactly to avoid a needless fingerprint diff.
        self._client.ws_set_options(path=WS_PATH, headers={
            "Origin": "https://www.pinnacle.bet", "User-Agent": USER_AGENT})
        try:
            self._client.tls_set()                       # wss
        except Exception:
            pass
        self._client.reconnect_delay_set(min_delay=1, max_delay=60)
        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._client.on_message = self._on_message
        try:
            self._loop = asyncio.get_running_loop()       # so paho's off-loop callbacks can schedule a re-mint
            self._client.connect_async(WS_HOST, 443, keepalive=60)
            self._client.loop_start()                    # background network thread
            self._ws_watchdog_task = asyncio.create_task(self._ws_watchdog())     # WS health monitor (no give-up)
            self._reconciler_task = asyncio.create_task(self._sub_reconciler())   # staggered subscribes
            self._status_task = asyncio.create_task(self._status_ping())          # browser-like liveness ping
            self._session_ka_task = asyncio.create_task(self._session_keepalive()) # vs inactivity logout
            print(f"[PINNACLE WS] connecting wss://{WS_HOST}{WS_PATH} (MQTT). Real-time PUSH; "
                  "id = '<leagueId>:<matchupId>:<designation>'.")
        except Exception as ex:
            print(f"[PINNACLE WS] connect error: {ex}")

    def _topics_for(self, lid: str):
        return [(f"matchups/reg/lg/{lid}/pre", 0),
                (f"matchups/reg/lg/{lid}/live/ld", 0),
                (f"matchups/reg/lg/{lid}/live/dz", 0),
                (f"matchups/reg/lg/{lid}/live/both", 0)]

    def _subscribe_league(self, lid: str) -> None:
        if not self._client or not self._connected or lid in self._subscribed:
            return
        try:
            for topic, qos in self._topics_for(lid):
                self._client.subscribe(topic, qos)
            self._subscribed.add(lid)
            print(f"[PINNACLE WS] subscribed league {lid}")
        except Exception as ex:
            print(f"[PINNACLE WS] subscribe {lid} error: {ex}")

    def _on_connect(self, client, userdata, flags, rc, *a) -> None:
        rc_val = getattr(rc, "value", rc)
        ok = (rc_val == 0)
        self._connected = ok
        if ok:
            self._ws_auth_rejects = 0                      # healthy connect clears the auth-fail streak
            self._ws_remints = 0                           # recovered → re-arm the per-outage re-mint budget
            print("[PINNACLE WS] connected (rc=0).")
            self._subscribed.clear()                      # the reconciler re-subscribes active leagues gradually
        elif rc_val in (4, 5):                            # CONNACK 4=bad user/pass, 5=not authorized
            self._ws_auth_rejects += 1
            print(f"[PINNACLE WS] connect REJECTED (rc={rc}) — session/WS-password invalid "
                  f"({self._ws_auth_rejects}/{self._ws_auth_giveup}).")
            if self._ws_auth_rejects >= self._ws_auth_giveup:
                # The WS password is {x-session}|{suffix}. A reject while the BROWSER is still logged in is almost
                # always a STALE x-session in paho's creds (Pinnacle rotated it and paho reconnected before the
                # browser propagated the new one) — NOT a dead login. Force a browser re-mint (reload → fresh
                # x-session → _on_browser_creds pushes it to paho) and let paho keep retrying. Only give up if the
                # browser has no session, or re-mints keep failing (cap) — then it's a genuine logout.
                if self._browser_has_session() and self._ws_remints < self._ws_remint_cap:
                    self._ws_auth_rejects = 0             # give the re-mint a fresh streak (paho keeps reconnecting)
                    if self._request_remint():           # only counts a re-mint that ACTUALLY fired (else throttled)
                        self._ws_remints += 1
                        print(f"[PINNACLE WS] auth-reject but the browser is LOGGED IN — forcing a WS-cred re-mint "
                              f"({self._ws_remints}/{self._ws_remint_cap}); NOT giving up.")
                else:
                    self._give_up_ws(f"WS auth rejected {self._ws_auth_rejects}x "
                                     f"({'re-mint cap hit' if self._browser_has_session() else 'browser logged out too'})")
        else:                                             # rc=3 server-unavailable etc. → TRANSIENT, let paho retry
            print(f"[PINNACLE WS] connect failed (rc={rc}) — transient, auto-reconnecting.")

    def _browser_has_session(self) -> bool:
        """True if the managed browser still holds a live login (so a WS auth-reject is a stale-token rotation,
        recoverable by a re-mint — not a genuine logout)."""
        if self._browser is None:
            return False
        try:
            return bool(self._browser.status().get("has_session"))
        except Exception:
            return False

    def _request_remint(self) -> bool:
        """Schedule an on-demand browser re-mint (reload → fresh x-session) from the paho callback thread. Throttled
        so overlapping auth-rejects during the reload don't stack reloads. Returns True only when it actually fired
        (so the caller counts it toward the cap); False when throttled or not ready."""
        now = time.time()
        if self._loop is None or self._browser is None or now - self._last_remint < self._remint_throttle_sec:
            return False
        self._last_remint = now
        try:
            asyncio.run_coroutine_threadsafe(self._browser.force_remint(), self._loop)
            return True
        except Exception as ex:
            print(f"[PINNACLE] WS re-mint schedule error: {type(ex).__name__}: {ex}")
            return False

    def _on_disconnect(self, client, userdata, rc, *a) -> None:
        self._connected = False
        print(f"[PINNACLE WS] disconnected (rc={rc}) — auto-reconnecting (books go stale until back).")

    async def _ws_watchdog(self) -> None:
        """WS health MONITOR (no longer a give-up cap). A TRANSIENT drop — network blip, server-unavailable
        (CONNACK rc=3), Cloudflare hiccup, clean disconnect — keeps auto-reconnecting FOREVER via paho's 1–60s
        backoff, exactly like a real browser tab left open; we do NOT give up on it. Only genuine SESSION DEATH
        stops the WS: a CONNACK auth-reject (rc 4/5, in _on_connect) or the REST guest-redirect / repeated
        401-403. This loop just LOGS a prolonged outage once (so an operator knows), then keeps watching."""
        warn_after = float(os.environ.get("PINNACLE_WS_WARN_SEC")
                           or os.environ.get("PINNACLE_WS_GIVEUP_SEC", "120"))   # old env name kept for compat
        last_ok, warned = time.time(), False
        while not self._ws_gave_up:
            try:
                await asyncio.sleep(5)
            except asyncio.CancelledError:
                break
            if self._connected:
                last_ok, warned = time.time(), False
            elif not warned and time.time() - last_ok > warn_after:
                warned = True
                print(f"[PINNACLE WS] down >{warn_after:.0f}s — still auto-reconnecting (transient; a DEAD "
                      "session would have stopped it). Books stay stale until it recovers.")

    def _give_up_ws(self, reason: str = "", clean: bool = False) -> None:
        if self._ws_gave_up:
            return
        # State changes FIRST (before any print) — a print can throw on a cp1252 Windows console, and these must
        # not be skipped. CRITICAL: loop_stop() does NOT fire on_disconnect, so drop _connected ourselves →
        # _read_cache stops stamping ts=now → frozen prices AGE → C# clears the books (no arb on stale numbers).
        self._ws_gave_up = True                       # stops ALL background loops (they check `not _ws_gave_up`)
        self._connected = False
        held_m = (time.time() - self._session_started_at) / 60 if self._session_started_at > 0 else -1.0
        self._session_started_at = 0.0                # session ended → stop age tracking (re-marks on recovery)
        why = f" ({reason})" if reason else ""
        if held_m >= 0:
            print(f"[PINNACLE] *** SESSION HELD {held_m:.0f}m before this stop ***{why}")
        if clean:
            # EXPECTED stop (scheduled dark window / lifecycle close) — the session was fine; nothing to refresh.
            print(f"[PINNACLE] standing the WS + keepalive DOWN{why} — expected close, session was healthy. "
                  "Books go stale -> C# clears them; reopens on the next window.")
        else:
            # TELL THE LOGIN WATCHER. This is the moment we have PROOF of a logout (authed REST is
            # guest-redirecting and the WS is down) — and it is proof the DOM cannot give us on a sport
            # board, which renders no login control at all. Without this hand-off the watcher stays awake
            # and blind, which is exactly how the bot sat logged out on 2026-08-18.
            try:
                if self._browser is not None and hasattr(self._browser, "note_logged_out"):
                    self._browser.note_logged_out(reason or "authed REST guest-redirecting, WS down")
            except Exception:
                pass
            print(f"[PINNACLE] STOPPING the WS + keepalive{why} - a dead/stale session isn't worth re-trying. "
                  "Refresh PINNACLE_SESSION + PINNACLE_WS_PASSWORD (= newsession|dGGR) and restart, or keep a "
                  "browser open to hold the session (or PINNACLE_ODDS_MODE=rest). Books go stale -> C# clears them.")
        c = self._client
        if c is not None:
            # loop_stop() must NOT run inside the paho loop thread (this can be called from a callback) → offload.
            threading.Thread(target=c.loop_stop, daemon=True).start()

    async def _sub_reconciler(self) -> None:
        """Subscribe to pending (active-but-unsubscribed) leagues ONE AT A TIME with a gap, so the WS
        subscribe pattern looks like a user navigating league to league — not a single scripted burst.
        Also re-subscribes after a reconnect (on_connect clears _subscribed; we refill gradually)."""
        while not self._ws_gave_up:
            try:
                await asyncio.sleep(self._subscribe_gap_sec)
            except asyncio.CancelledError:
                break
            if not self._connected:
                continue
            pending = [l for l in list(self._active_leagues.keys()) if l not in self._subscribed]
            if pending:
                self._subscribe_league(pending[0])        # one per tick = staggered, organic-looking

    async def _await_session_settle(self) -> None:
        """Hold until the current session is at least `PINNACLE_SESSION_SETTLE_SEC` old. No-op once it is (so
        this costs nothing on the steady-state poll) or when no session start is stamped. Cheap insurance: a
        premature authed call doesn't just fail, it increments `_rest_auth_fails` toward `_rest_death_check`,
        so sleeping through the gap protects the SESSION as well as the reading."""
        if self._session_started_at <= 0 or self._session_settle_sec <= 0:
            return
        age = time.time() - self._session_started_at
        remaining = self._session_settle_sec - age
        if remaining > 0:
            print(f"[PINNACLE] session only {age:.1f}s old - waiting {remaining:.1f}s for the site to settle "
                  "before the authed call.")
            await asyncio.sleep(min(remaining, self._session_settle_sec))

    def _mark_session_started(self, how: str) -> None:
        """Stamp the moment a session became live (env token at startup, or a fresh browser capture) so the age
        heartbeat + the give-up log can report exactly how long it was held — i.e. Pinnacle's real logout window."""
        self._session_started_at = time.time()
        self._survive_logged = False                      # arm the "SURVIVED past Nm" milestone for this session
        print(f"[PINNACLE] session established ({how}) — age tracking started.")

    async def _session_age_heartbeat(self) -> None:
        """Persistent: every PINNACLE_SESSION_AGE_LOG_SEC, log how long the current session has been held (with
        WS/ready state). Quiet when there's no session. Survives give-up + recovery so a 2h test reads cleanly."""
        while True:
            try:
                await asyncio.sleep(self._session_age_log_sec)
            except asyncio.CancelledError:
                break
            if self._session_started_at > 0:
                held_m = (time.time() - self._session_started_at) / 60
                if self._connected:
                    ws = "connected"
                elif self._window_ws_read and self._feed_live():
                    ws = "window-reader"                  # the browser-WS reader is the live odds source (no paho)
                elif self._ws_gave_up:
                    ws = "GAVE-UP"
                else:
                    ws = "down"
                extra = ""
                if self._window_ws_read:
                    now = time.time()
                    snap = list(self._cache.values())
                    fresh = sum(1 for s in snap if s.ts and now - s.ts < self._browser_odds_ttl)
                    live_n = sum(1 for s in snap if s.live)
                    hb = ""                                # WS-connection heartbeat age (why feed_live holds when quiet)
                    if self._browser is not None:
                        laf = getattr(self._browser, "_arcadia_last_frame", 0.0)
                        hb = f", ws_hb={now - laf:.0f}s" if laf else ", ws_hb=none"
                    extra = (f" | reader: applied={self._browser_odds_msgs}, "
                             f"{fresh}/{len(snap)} fresh, {live_n} live{hb}, feed_live={self._feed_live()}")
                # BOTH AGES, always together. "session held 16m" was read as the account's login age on
                # 2026-08-20 and it is not — it counts from THIS sidecar's capture and resets on restart,
                # while the login can be hours older behind a persistent profile. Printing them side by side
                # makes the difference impossible to misread.
                age_txt = ""
                try:
                    if self._browser is not None and hasattr(self._browser, "login_age_str"):
                        age_txt = " | " + self._browser.login_age_str()
                except Exception:
                    pass
                print(f"[PINNACLE] session held {held_m:.0f}m  (ready={self._session_ready}, ws={ws}, "
                      f"cache={len(self._cache)} sel){age_txt}{extra}")
                if not self._survive_logged and held_m >= self._survive_min:
                    self._survive_logged = True           # unattended pass signal — glance for this line when you're back
                    print(f"[PINNACLE] *** SESSION SURVIVED past {self._survive_min:.0f}m — keepalive is HOLDING "
                          "(was logging out at ~30m). ***")

    async def _status_ping(self) -> None:
        """Browser-like liveness: GET /status periodically (the page does this). Makes the headless session
        emit the same background heartbeat a real tab does. CAMOUFLAGE ONLY — /status carries no session, so
        it does NOT refresh/extend the x-session; that needs the separate token-refresh call (TODO)."""
        while not self._ws_gave_up:
            try:
                await asyncio.sleep(self._status_ping_sec)
            except asyncio.CancelledError:
                break
            # PREFER THE REAL ONE. When a browser tab is open it polls /status itself, and that request has
            # the page's own TLS fingerprint, cookies and timing. Ours adds a second, differently-shaped
            # copy of the same call — the opposite of camouflage. So it only fires when the page has NOT
            # made one recently (or when there is no browser at all, e.g. PINNACLE_SESSION_SOURCE=env).
            br = self._browser
            last = getattr(br, "last_page_status_ts", 0.0) if br is not None else 0.0
            if last and time.time() - last < self._status_ping_sec * 2:
                if not getattr(self, "_status_standby", False):
                    self._status_standby = True
                    print("[PINNACLE] liveness ping STANDING DOWN — the page is polling /status itself.",
                          flush=True)
                continue
            if getattr(self, "_status_standby", False):
                self._status_standby = False
                print("[PINNACLE] the page stopped polling /status — resuming our own liveness ping.",
                      flush=True)
            await self._http_get("/status", authed=False)   # camouflage only — carries no session; never an auth signal

    async def _session_keepalive(self) -> None:
        """Keep the x-session alive vs the ~90-min INACTIVITY logout (an idle session — only /status, no
        AUTHED calls — gets logged out). Every PINNACLE_SESSION_KEEPALIVE_SEC, re-fetch each active league's
        /markets/straight: an AUTHED origin hit (carries x-session, must-revalidate past its 5s cache) that
        should reset the server-side inactivity timer. Doubles as a pre-match RE-SEED to reconcile any drift
        the WS missed. (If the timeout turns out to be UI-activity-based, this won't help → we'd need re-auth.)"""
        while not self._ws_gave_up:
            try:
                await asyncio.sleep(self._session_ka_sec)
            except asyncio.CancelledError:
                break
            # SAME GATE AS THE READER RE-SEED, and for a stronger reason: this one was MEASURED not to
            # work. The docstring's own caveat - "if the timeout turns out to be UI-activity-based, this
            # won't help" - is exactly what happened. Pinnacle's idle logout (~30min) is driven by UI
            # activity; mouse moves, scrolls and authed API hits do NOT reset it, which is why the organic
            # layer had to start doing keyboard-scrolls and sport-nav clicks instead. So in-play was paying
            # a 22-request authed burst every 4 minutes for a session timer it does not touch.
            #
            # Left running pre-live, where it still serves as the drift reconcile its docstring describes
            # and the traffic sits among a browsing session rather than one parked tab.
            if self.mode == "inplay" and not self._reseed_inplay:
                if not getattr(self, "_ka_inplay_noted", False):
                    self._ka_inplay_noted = True
                    print("[PINNACLE] session keepalive PAUSED for in-play - the logout timer is UI-based, "
                          "so this never reset it; organic activity is what holds the session.", flush=True)
                continue
            self._ka_inplay_noted = False
            leagues = list(self._active_leagues.keys())
            for lid in leagues:
                if self._ws_gave_up:
                    break                                     # session died mid-cycle → stop immediately
                await self._refresh_league(lid)
                await asyncio.sleep(random.uniform(0, self._jitter_ms / 1000.0))   # gentle spacing
            if leagues and not self._ws_gave_up:
                live_sel = sum(1 for s in self._cache.values() if getattr(s, "live", False))
                # Of the tokens the C# bot actually WATCHES (paired), how many are live right now? If this stays
                # 0 while `live_sel` (the whole cache) is high, the paired games' live data isn't reaching their
                # tokens — the systematic in-play miss. Sample a few live watched ids (or overall) to compare.
                watched_live = [sid for sid in self._requested_ids
                                if (s := self._cache.get(sid)) is not None and getattr(s, "live", False)]
                sample = watched_live[:6] if watched_live else \
                    [sid for sid, s in self._cache.items() if getattr(s, "live", False)][:6]
                print(f"[PINNACLE] session-keepalive: re-fetched {len(leagues)} league(s) (authed → resets the "
                      f"inactivity timer; cache={len(self._cache)} sel, {live_sel} live) | "
                      f"WS msgs live={self._ws_live_msgs}/pre={self._ws_pre_msgs} | "
                      f"WATCHED-live={len(watched_live)}/{len(self._requested_ids)} | "
                      f"sample live{'(watched)' if watched_live else ''}: {sample}")

    def _on_message(self, client, userdata, msg, *a) -> None:
        try:
            data = json.loads(msg.payload.decode("utf-8"))
        except Exception:
            return
        live = "/live/" in (getattr(msg, "topic", "") or "")   # topic = IN-PLAY (…/live/*) vs pre-match (…/pre)
        if live:
            self._ws_live_msgs += 1
        else:
            self._ws_pre_msgs += 1
        try:
            self._apply(data, live)
        except Exception as ex:
            print(f"[PINNACLE WS] apply error: {type(ex).__name__}: {ex}")

    def _on_browser_odds(self, topic: str, payload: bytes) -> None:
        """WINDOW-WS READER: an odds PUBLISH parsed off the BROWSER's own WS (PinnacleBrowserSession, via CDP) —
        route it into the SAME cache path the dedicated paho feed uses. The payload shape is identical
        ({op, pk, rec:{league{id}, markets...}}); the topic decides pre-match vs in-play. Called on the main
        loop (sync) from the CDP frame handler, so _apply's cache-lock is uncontended. Never raises."""
        try:
            data = json.loads(payload.decode("utf-8"))
        except Exception:
            return
        live = "/live/" in (topic or "")
        if live:
            self._ws_live_msgs += 1
        else:
            self._ws_pre_msgs += 1
        self._browser_odds_last = time.time()      # marks the browser-WS feed LIVE for _feed_live()
        self._browser_odds_msgs += 1
        rec = data.get("rec") or {}
        mid = rec.get("id") if rec.get("id") is not None else data.get("pk")
        lid = (rec.get("league") or {}).get("id")
        if lid is not None and mid is not None:
            self._browser_odds_mid_ts[f"{lid}:{mid}"] = self._browser_odds_last   # coverage truth (per matchup)
            if "/sp/" in (topic or ""):        # SPORT-level topic = the featured board (main page), not a league tab
                self._board_odds_lid_ts[str(lid)] = self._browser_odds_last       # → this league is board-covered
        try:
            self._apply(data, live)
        except Exception as ex:
            print(f"[PINNACLE WINDOW-WS] apply error: {type(ex).__name__}: {ex}")

    def ws_verified_map(self, selection_ids: list, ttl: float | None = None) -> dict:
        """Per-selection: is its league under LIVE WS coverage (a manager tab, OR its matchup pushed over the WS
        recently) vs SCREENING-ONLY (an httpx-re-seed of an untabbed tail league)? The C# side uses this for
        verify-on-detection — an arb on a screening-only leg fires /verify to promote a live tab before it's
        trusted. Only meaningful in pure-reader mode; paho/REST subscribe every active league → all True."""
        if not (self._window_ws_read and not self._dedicated_ws):
            return {sid: True for sid in selection_ids}
        ttl = ttl if ttl is not None else self._browser_ws_heartbeat_ttl
        tab_lids = self._tab_manager.covered_lids() if self._tab_manager is not None else set()
        now = time.time()
        pushed = self._browser_odds_mid_ts
        out = {}
        for sid in selection_ids:
            p = sid.split(":")
            lid = p[0] if p else ""
            mk = f"{p[0]}:{p[1]}" if len(p) >= 2 else ""
            out[sid] = (lid in tab_lids) or (mk in pushed and (now - pushed[mk]) < ttl)
        return out

    def _league_ws_live(self, lid: str, ttl: float | None = None) -> bool:
        """Has the browser WS pushed odds for ANY matchup in this league recently? That is the evidence the
        league's subscription is genuinely up. Checking the ONE matchup we care about is too strict: the WS
        sends deltas, so a stable pre-match line can stay silent for minutes even though the feed is healthy."""
        ttl = ttl if ttl is not None else self._browser_ws_heartbeat_ttl
        now = time.time()
        pre = f"{lid}:"
        for k, ts in list(self._browser_odds_mid_ts.items()):
            if k.startswith(pre) and (now - ts) < ttl:
                return True
        return False

    async def verify_now(self, selection_id: str, timeout: float = 10.0) -> dict:
        """SYNCHRONOUS verify-then-trade: commandeer the ROVING tab, navigate straight to this selection's
        league, and WAIT for its live WS to push a price for that matchup — so the caller can re-check and fire
        on the SAME arb window.

        Replaces the fire-and-forget `/verify`, which had two problems: it only skipped the current arb and hoped
        a LATER window on that league would be covered, and it asked the DEDICATED tab pool for a slot — returning
        `at-cap` whenever the pool was full, which permanently blocked those leagues instead of merely delaying
        them. The rove tab has no such cap.

        Returns {ok, verified, waited_ms, price, decimal_odds}. Declines while a bet is in flight — the rove tab
        may be needed for placement and must not be navigated out from under it."""
        sid = str(selection_id or "")
        parts = sid.split(":")
        if len(parts) < 2:
            return {"ok": False, "verified": False, "error": f"bad selection_id '{sid}'"}
        lid = parts[0]
        if self.ws_verified_map([sid]).get(sid):
            return {"ok": True, "verified": True, "waited_ms": 0, "how": "already-live"}
        if self._bet_lock.locked():
            return {"ok": False, "verified": False, "error": "bet in flight - not borrowing the rove tab"}
        # Nothing opens or re-points a tab while a slip is armed. In-play mode exists to keep ONE tab still,
        # and the camped selection is on the live list this tab is showing — so it takes the already-live
        # short-circuit above and never reaches here. Anything that DOES reach here is a different league,
        # i.e. exactly the case that is not worth a navigation mid-camp.
        if self._manual_mode:
            return {"ok": False, "verified": False,
                    "error": "manual mode - not navigating a tab while the operator is driving"}
        if getattr(self, "_camping", False):
            return {"ok": False, "verified": False,
                    "error": "camping - not navigating a tab while a slip is armed"}
        tm = self._tab_manager
        if tm is None:
            return {"ok": False, "verified": False, "error": "no tab manager (rove disabled)"}
        url = self._league_url_for(lid)
        if not url:
            return {"ok": False, "verified": False, "error": f"no league URL known for lid {lid}"}

        t0 = time.time()
        tm.hold(True)                                  # stop the sweep fighting us for the rove tab
        try:
            page = await tm.acquire_rove_for_bet(url, lid=lid)
            if page is None:
                return {"ok": False, "verified": False, "error": "rove tab unavailable"}
            deadline = t0 + max(1.0, float(timeout))
            while time.time() < deadline:
                await asyncio.sleep(0.25)
                # Require EVIDENCE the league's WS is really live, not just that we navigated. A stable pre-match
                # line may never push its OWN matchup (the WS sends changes, not heartbeats), so accept a push for
                # ANY matchup in this league — that proves the subscription is up and the price is live-sourced.
                if self._league_ws_live(lid):
                    waited = int((time.time() - t0) * 1000)
                    price = odds_dec = None
                    try:
                        s = (await self.odds([sid])).get(sid)
                        if s is not None:
                            price, odds_dec = s.implied_price, s.decimal_odds
                        # leave the rove tab parked here: the bet path's page_for_lid now finds this league
                    except Exception:
                        pass
                    print(f"[PINNACLE] verify-now: {sid} LIVE on the rove tab after {waited}ms "
                          f"(odds {odds_dec})")
                    return {"ok": True, "verified": True, "waited_ms": waited, "how": "rove-nav",
                            "price": price, "decimal_odds": odds_dec}
            waited = int((time.time() - t0) * 1000)
            print(f"[PINNACLE] verify-now: {sid} still not WS-live after {waited}ms - caller should skip.")
            return {"ok": True, "verified": False, "waited_ms": waited, "error": "timeout"}
        except Exception as e:
            return {"ok": False, "verified": False, "error": f"{type(e).__name__}: {e}"}
        finally:
            tm.hold(False)

    async def request_league_verify(self, lid: str) -> dict:
        """VERIFY-ON-DETECTION: promote league `lid` to a live WS tab on demand (the C# bot calls this when it
        spots an arb on a screening-only leg). Delegates to the tab manager; no-op if it isn't running."""
        if self._tab_manager is None:
            return {"status": "no-tab-manager", "lid": str(lid)}
        try:
            status = await self._tab_manager.request_verify(str(lid))
        except Exception as ex:
            return {"status": f"error: {type(ex).__name__}", "lid": str(lid)}
        return {"status": status, "lid": str(lid)}

    def board_lids(self, ttl: float | None = None) -> set:
        """Leagues the FEATURED BOARD (main page) is streaming — those seen on a SPORT-level topic
        (matchups/…/sp/{id}/…) within `ttl`. The tab manager excludes these from its dedicated-tab candidates so
        it never doubles up on a league the board already covers. Prunes as it goes. Featured leagues are the
        active/popular ones (they push often), so a generous TTL keeps a briefly-quiet one from re-appearing."""
        ttl = ttl if ttl is not None else self._board_lid_ttl
        now = time.time()
        out = set()
        for lid in list(self._board_odds_lid_ts.keys()):
            age = now - self._board_odds_lid_ts[lid]
            if age < ttl:
                out.add(lid)
            elif age > max(ttl, 900):
                del self._board_odds_lid_ts[lid]
        return out

    def _ws_coverage_stats(self, ttl: float = 45.0) -> dict:
        """Payload-derived WS coverage for the [WS-READ] diagnostic: how many REAL leagues pushed odds within
        `ttl` (from rec.league.id), split into board-fed vs dedicated-tab-fed, plus the matchup count. This is
        the honest answer to "what is the main tab covering" — the topic scan in pinnacle_session can only see
        that ONE sport-wide board subscription ('sp/33'), never the leagues inside it."""
        now = time.time()
        leagues = {k.split(":")[0] for k, ts in list(self._browser_odds_mid_ts.items()) if now - ts < ttl}
        board = {lid for lid, ts in list(self._board_odds_lid_ts.items()) if now - ts < ttl}
        tabs = set()
        if self._tab_manager is not None:
            try:
                tabs = {str(l) for l in self._tab_manager.covered_lids()}
            except Exception:
                tabs = set()
        return {"leagues": len(leagues), "board": len(board & leagues),
                "tabs": len(tabs & leagues), "matchups": len(leagues and
                [k for k, ts in list(self._browser_odds_mid_ts.items()) if now - ts < ttl])}

    @staticmethod
    def _slug_txt(s: str) -> str:
        """'ATP Montreal - R3 Odds' -> 'atp-montreal-r3-odds' (same shape pair_pinnacle._slugify produces)."""
        s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode("ascii")
        return re.sub(r"[^a-zA-Z0-9]+", "-", s).strip("-").lower()

    async def board_dom_scan(self) -> dict:
        """RAW view of what the board is showing and how it matched — the diagnostic behind board_dom_lids.
        Served on /debug/board_dom so a 'why is this league still getting a tab' question is answerable with
        data instead of guesses."""
        page = self._primary_page()
        if page is None:
            return {"error": "no primary page (browser not open?)"}
        try:
            # Real markup (captured 2026-08-06) drives this:
            #   league header row : <a class="rowLink-…" href="/en/tennis/atp-montreal-r3/matchups/">
            #   game row          : <a href="/en/tennis/atp-montreal-r3/norrie-vs-de-minaur/1633401615/">
            # The list is VIRTUALISED (3272px of content in a 680px scroller; ~13 of 55 rows in the DOM), so
            # only rendered rows exist — and per the 2026-07-16 coverage measurement, only rendered matchups
            # actually stream. That makes the GAME rows the honest signal: collect their matchup ids and let
            # the caller decide per league. A league header alone proves nothing about its games.
            raw = await page.evaluate(
                "() => {const hs = Array.from(document.querySelectorAll('a[href]'))"
                "         .map(a=>a.getAttribute('href')||'');"
                " return {url: location.href,"
                "  league_hrefs: hs.filter(h=>/\\/matchups\\/?$/.test(h)).slice(0,200),"
                "  mids: hs.map(h=>(h.match(/\\/(\\d{6,})\\/?$/)||[])[1]).filter(Boolean).slice(0,400),"
                "  rows: document.querySelectorAll('.scrollbar-item').length};}")
        except Exception as ex:
            return {"error": f"{type(ex).__name__}: {ex}"}
        self._league_url_for("")                       # populate/refresh _lid_urls
        lid_slug = {}
        for lid, u in (getattr(self, "_lid_urls", None) or {}).items():
            parts = [p for p in u.split("#")[0].split("?")[0].rstrip("/").split("/") if p]
            if len(parts) >= 2:
                lid_slug[lid] = parts[-2].lower()      # .../tennis/<league-slug>/matchups/
        # Leagues whose HEADER row is rendered (informational — not sufficient for coverage).
        headers = set()
        for h in raw.get("league_hrefs") or []:
            s = [p for p in h.split("#")[0].split("?")[0].rstrip("/").split("/") if p]
            cand = s[-2].lower() if len(s) >= 2 else ""
            for lid, slug in lid_slug.items():
                if cand and cand == slug:
                    headers.add(lid)
        return {"page_url": raw.get("url"), "rendered_rows": raw.get("rows"),
                "rendered_mids": sorted(set(raw.get("mids") or [])),
                "header_lids": sorted(headers), "paired_slugs": lid_slug,
                "note": "list is virtualised - only rendered rows stream; coverage is judged per MATCHUP"}

    async def board_full_scan(self) -> dict:
        """Scroll the board's virtualised list top-to-bottom to ENUMERATE EVERY league on it.

        Why scroll rather than infer: the list only renders ~13 of N rows, so a single DOM read sees a
        fraction of the board. Everything else we tried was an inference — 'it pushed recently' (misses
        stable leagues, and is empty at startup) or 'it has a game today' (assumes the board carries the
        whole day). Scrolling produces the actual list, which is both correct and CHECKABLE by eye.
        Scrolling changes rendering only; the sport-topic subscription is unaffected (measured 2026-08-06),
        so this cannot alter coverage — it only reveals it. Scroll position is restored afterwards."""
        page = self._primary_page()
        if page is None:
            return {"error": "no primary page"}
        settle = int(float(os.environ.get("PINNACLE_BOARD_SCAN_SETTLE_MS", "250")))
        js = """async (settle) => {
          const sleep = ms => new Promise(r => setTimeout(r, ms));
          const seen = new Set();
          const grab = () => {
            document.querySelectorAll('a[href]').forEach(a => {
              const h = a.getAttribute('href') || '';
              if (/\\/matchups\\/?$/.test(h)) seen.add(h);
            });
            return seen.size;
          };
          // Find the scroller by walking UP FROM THE CONTENT, never from a class name. The previous
          // version anchored on `.scrollbar-item`; when that stopped matching, `el` was null on the
          // first line and the scan silently degraded to "read whatever is rendered, report TRUNCATED
          // forever" -- which is what starved the tab manager (7 paired games instead of 42, tabs=0/12
          // with 12 slots free). The rows themselves are the one thing we can always locate, because
          // finding them is the entire point of the scan.
          grab();
          const anchor = document.querySelector('a[href$="/matchups/"]')
                      || document.querySelector('a[href*="/matchups"]')
                      || document.querySelector('.scrollbar-item');   // legacy, last resort
          let el = anchor;
          while (el && !(el.scrollHeight > el.clientHeight + 40)) el = el.parentElement;
          // Nothing overflowed on the way up: the page itself may be the scroller (layout change).
          if (!el) {
            const doc = document.scrollingElement || document.documentElement;
            if (doc && doc.scrollHeight > doc.clientHeight + 40) el = doc;
          }
          if (!el) {
            // Two very different situations, and conflating them is what hid this for a whole session:
            //   anchor found    -> the list FITS without scrolling, so this read is COMPLETE
            //   no anchor       -> the board did not render; report it as an error, not as truncation
            const fits = !!anchor;
            return {leagues: [...seen], scrolled: false, height: 0, steps: 0,
                    reached_bottom: fits, settled: fits, no_scroller: true, no_rows: !anchor};
          }
          const start = el.scrollTop, step = Math.max(200, el.clientHeight - 80);
          let steps = 0, y = 0, reached = false, settled = false;
          // Scroll to the bottom, THEN keep re-reading until a pass adds nothing new. A fixed step count
          // silently truncates if the virtualised list renders slower than the step delay; "stop when the
          // set stops growing AND we are at the bottom" is self-verifying instead.
          while (steps < 80) {
            el.scrollTop = y; await sleep(settle);
            const before = seen.size; grab(); steps++;
            const atBottom = el.scrollTop + el.clientHeight >= el.scrollHeight - 8;
            if (atBottom) {
              reached = true;
              if (seen.size === before) { settled = true; break; }   // bottom AND nothing new -> complete
            }
            y = atBottom ? y : y + step;
          }
          el.scrollTop = start; await sleep(80);
          return {leagues: [...seen], scrolled: true, height: el.scrollHeight, steps,
                  reached_bottom: reached, settled,
                  events: (document.querySelector('[data-test-id="Events.DateBar"]')||{}).innerText || ''};
        }"""
        try:
            raw = await page.evaluate(js, settle)
        except Exception as ex:
            return {"error": f"{type(ex).__name__}: {ex}"}
        self._league_url_for("")                        # populate lid -> url
        lid_slug = {}
        for lid, u in (getattr(self, "_lid_urls", None) or {}).items():
            parts = [p for p in u.split("#")[0].split("?")[0].rstrip("/").split("/") if p]
            if len(parts) >= 2:
                lid_slug[lid] = parts[-2].lower()
        board_slugs, matched = set(), set()
        for h in raw.get("leagues") or []:
            parts = [p for p in h.split("#")[0].split("?")[0].rstrip("/").split("/") if p]
            if len(parts) >= 2:
                s = parts[-2].lower()
                board_slugs.add(s)
                for lid, slug in lid_slug.items():
                    if s == slug:
                        matched.add(lid)
        return {"scrolled": raw.get("scrolled"), "list_height_px": raw.get("height"),
                "no_scroller": bool(raw.get("no_scroller")), "no_rows": bool(raw.get("no_rows")),
                "scroll_steps": raw.get("steps"),
                # complete = we hit the bottom AND a further read added no new leagues
                "reached_bottom": raw.get("reached_bottom"), "settled": raw.get("settled"),
                "complete": bool(raw.get("reached_bottom") and raw.get("settled")),
                "date_bar": (raw.get("events") or "").replace("\n", " ")[:60],
                "leagues_on_board": sorted(board_slugs),
                "matched_lids": sorted(matched),
                "paired_not_on_board": sorted(l for l in lid_slug if l not in matched)}

    async def board_all_lids(self, ttl: float | None = None) -> set:
        """Paired leagues present on the board's league list, refreshed by a full scroll scan every
        PINNACLE_BOARD_SCAN_MIN minutes (default 20). Cached between scans."""
        ttl = ttl if ttl is not None else float(os.environ.get("PINNACLE_BOARD_SCAN_MIN", "20")) * 60.0
        now = time.time()
        if now - getattr(self, "_board_scan_ts", 0.0) < ttl:
            return getattr(self, "_board_scan_lids", set())
        self._board_scan_ts = now
        try:
            scan = await self.board_full_scan()
        except Exception as ex:
            print(f"[PINNACLE] board scroll-scan failed: {type(ex).__name__}: {ex}")
            return getattr(self, "_board_scan_lids", set())
        if scan.get("error"):
            print(f"[PINNACLE] board scroll-scan: {scan['error']}")
            return getattr(self, "_board_scan_lids", set())
        out = set(scan.get("matched_lids") or [])
        if scan.get("no_rows"):
            # No league rows at all: the board did not render (wrong page, still loading, markup change).
            # This is an ERROR, not a truncated scan -- saying "truncated" sent us hunting a scroll bug
            # for a whole session when the real answer was "the selector matched nothing".
            print("[PINNACLE] board scroll-scan: NO LEAGUE ROWS on the page — the board did not render "
                  "(is the primary tab on a sport board?). Keeping the previous league set.")
            self._board_scan_ts = now - max(0.0, ttl - 120.0)
            return getattr(self, "_board_scan_lids", set())
        state = ("complete (list fits, no scrolling needed)" if scan.get("no_scroller") and scan.get("complete")
                 else "complete" if scan.get("complete") else (
                 "TRUNCATED (never reached the bottom)" if not scan.get("reached_bottom")
                 else "TRUNCATED (list still growing when we stopped)"))
        print(f"[PINNACLE] board scroll-scan {state}: {len(scan.get('leagues_on_board') or [])} league(s) "
              f"listed [{scan.get('date_bar')}] ({scan.get('scroll_steps')} steps, "
              f"{scan.get('list_height_px')}px) - {len(out)} of ours are ON the board: {sorted(out)}")
        if not scan.get("complete"):
            # An incomplete scan UNDER-reports what the board carries. Trusting it would suppress nothing —
            # it would instead leave leagues looking uncovered, which is the SAFE direction (extra tabs, no
            # coverage hole). But do not CACHE it: keep the last good result and retry sooner.
            print("[PINNACLE] scroll-scan incomplete - keeping the previous league set and retrying shortly "
                  "(raise PINNACLE_BOARD_SCAN_SETTLE_MS if this repeats).")
            self._board_scan_ts = now - max(0.0, ttl - 120.0)      # retry in ~2 min instead of the full TTL
            return getattr(self, "_board_scan_lids", out)
        self._board_scan_lids = out
        if scan.get("paired_not_on_board"):
            print(f"[PINNACLE] paired leagues NOT on the board (need tabs): {scan['paired_not_on_board']}")
        return out

    async def board_dom_mids(self, ttl: float = 20.0) -> set:
        """Matchup ids the board is RENDERING right now (and therefore streaming). Cached briefly."""
        now = time.time()
        if now - getattr(self, "_board_dom_ts", 0.0) < ttl:
            return getattr(self, "_board_dom_mids_cache", set())
        self._board_dom_ts = now
        try:
            scan = await self.board_dom_scan()
        except Exception:
            return getattr(self, "_board_dom_mids_cache", set())
        if scan.get("error"):
            return getattr(self, "_board_dom_mids_cache", set())
        out = set(scan.get("rendered_mids") or [])
        prev = getattr(self, "_board_dom_mids_cache", set())
        if out != prev:
            print(f"[PINNACLE] board is rendering {len(out)} matchup(s) "
                  f"({scan.get('rendered_rows')} rows) - only these stream from the board")
        self._board_dom_mids_cache = out
        return out

    async def board_dom_lids(self, ttl: float = 20.0) -> set:
        """Leagues the main board is actually SHOWING, read from its DOM.

        `board_lids()` infers board coverage from WS PUSHES, which silently misses the common case: a league
        sitting on the board with STABLE pre-match prices pushes nothing, so it reads as uncovered and gets a
        redundant dedicated tab ("tabs that are clearly on the main page"). The board subscribes to what it
        RENDERS, so the rendered league links are the direct answer. Cached briefly — this evaluates JS on the
        primary page and the tab manager asks every tick."""
        now = time.time()
        if now - getattr(self, "_board_dom_ts", 0.0) < ttl:
            return getattr(self, "_board_dom_lids", set())
        self._board_dom_ts = now
        try:
            scan = await self.board_dom_scan()
        except Exception:
            return getattr(self, "_board_dom_lids", set())
        if scan.get("error"):
            return getattr(self, "_board_dom_lids", set())
        out = set(scan.get("matched_lids") or [])
        prev = getattr(self, "_board_dom_lids", set())
        if out != prev:
            print(f"[PINNACLE] board is SHOWING {len(out)} paired league(s): {sorted(out)} "
                  "(these get no dedicated tab)")
        self._board_dom_lids = out
        return out

    def reader_live_mids(self, ttl: float = 30.0) -> list:
        """Matchups ('lid:mid') the browser-WS READER has actually pushed odds for within `ttl` seconds — the
        ground truth for coverage (NOT /odds freshness, which _read_cache re-stamps for any served token). Prunes
        stale entries as it goes so the dict stays bounded."""
        now = time.time()
        out = []
        for k in list(self._browser_odds_mid_ts.keys()):
            age = now - self._browser_odds_mid_ts[k]
            if age < ttl:
                out.append(k)
            elif age > 900:                        # forget matchups silent >15 min (settled / off the board)
                del self._browser_odds_mid_ts[k]
        return out

    def _apply(self, data: dict, live: bool = False) -> None:
        rec = data.get("rec") or {}
        mid = rec.get("id") if rec.get("id") is not None else data.get("pk")
        lid = (rec.get("league") or {}).get("id")
        if mid is None or lid is None:
            return
        lid, mid = str(lid), str(mid)
        prefix = f"{lid}:{mid}:"
        if self._ws_dump_path:
            self._dump_ws_record(data, lid, mid)
        # ── WHEN A MATCH GOES LIVE, PINNACLE RE-ISSUES THE MATCHUP ─────────────────────────────────────
        # The pre-match matchup does not go in-play; a NEW one is created with the old id as its `parentId`,
        # and the old one leaves the board. Confirmed 2026-08-20 across three fixtures, e.g. Steur vs
        # Steinkamp: paired as 214887:1634341865, actually trading as 214887:1634373627 (parent 1634341865),
        # and the parent had dropped out of /catalog entirely.
        #
        # Everything downstream held the PARENT: the book went stale, the camp armed a superseded market and
        # found it "offline" three checks later, the row could not be located on the live page, and a press
        # came back HTTP 400. It reads as a dozen unrelated in-play failures and it is one cause.
        #
        # The link is free — `parentId` and the participant names are already in every push, they were only
        # being written to a debug dump. Recording it here needs no extra request to anyone, which is the
        # whole point: the guest endpoint must not be polled to discover something the live feed states.
        #
        # Names are stored per designation so a redirect can be verified rather than assumed. Home/away
        # LOOKS preserved between parent and child, but "looks preserved" is how sides get inverted, and the
        # cost of being wrong is both legs on one outcome.
        parent = rec.get("parentId")
        if parent is not None and live:
            names = {}
            for pt in (rec.get("participants") or []):
                al = pt.get("alignment")
                if al in _SIDES and pt.get("name"):
                    names[al] = _strip_units(pt["name"])
            if names:
                self._live_child[f"{lid}:{parent}"] = (f"{lid}:{mid}", names,
                                                       _strip_units(str(rec.get("units") or "")))
        if data.get("op") == "del":
            with self._cache_lock:
                for k in [k for k in self._cache if k.startswith(prefix)]:
                    del self._cache[k]
            return
        now = time.time()
        # Pinnacle pushes the WHOLE matchup record on any sub-market change, so the markets list is the full
        # current state. Build a token for every OPEN period-0 moneyline / spread / total price (`_market_tokens`
        # mirrors pair_derivatives' keying). RECONCILE: any cached token for this matchup NOT in this push (a
        # line pulled, a market suspended, a side's price gone) is marked SUSPENDED so a stale "open" leg can't
        # sit against a live Kalshi leg (phantom arb). A marketless push (score/clock heartbeat) is ambiguous →
        # leave tokens to the staleness gate.
        markets = rec.get("markets") or []
        updates: dict[str, Selection] = {}
        for mk in markets:
            for token, sel in self._market_tokens(lid, mid, mk, now, live):
                updates[token] = sel
        reconcile = len(markets) > 0
        suspended = 0
        with self._cache_lock:
            if reconcile:
                for k in [k for k in self._cache if k.startswith(prefix) and k not in updates]:
                    old = self._cache[k]
                    if old.status != "suspended":
                        suspended += 1
                    # CARRY `live`/`cutoff` THROUGH THE SUSPENSION. In-play tennis suspends between points, so
                    # a live token passes through here constantly; rebuilding it with the Selection defaults
                    # silently reset live=False, and the next REST re-seed then read THAT as "was pre-match"
                    # (_apply_straight_markets' guard consults this entry) and restored the token as pre-live.
                    self._cache[k] = Selection(old.selection_id, old.decimal_odds, old.max_stake,
                                               status="suspended", ts=now, live=old.live, cutoff=old.cutoff)
            # A `/pre`-topic push must NOT downgrade a token the `/live/*` topic already flagged in-play — the
            # same rule _apply_straight_markets applies to REST snapshots, which this path was missing. Without
            # it the window is logged as PRE-LIVE (HardVenInPlay=0), the favourable regime: ~1s placement in the
            # analyzer and past the executor's pre-live-only gate. The matchup's `del` (above) is what clears
            # the tag when the game actually ends. Audited 2026-08-07 vs Kalshi settlement times: this misfiled
            # ~2.5% of windows, incl. one logged pre-live 6 min before its Kalshi market closed.
            if not live:
                for token, sel in updates.items():
                    old = self._cache.get(token)
                    if old is not None and old.live:
                        sel.live = True
            self._cache.update(updates)
        if updates and self._debug_ws:
            legs = " ".join(f"{k.split(':', 2)[-1]}={v.decimal_odds:.3f}" for k, v in list(updates.items())[:6])
            print(f"[PINNACLE WS-UPD] {data.get('op')} {lid}:{mid}  {legs}")
        if suspended and (self._debug_ws or self._debug_status):
            print(f"[PINNACLE STATUS] {lid}:{mid} → {suspended} leg(s) offline/suspended")

    def _market_tokens(self, lid: str, mid, mk: dict, now: float, live: bool = False):
        """Yield (token, Selection) for each price of an OPEN, full-game (period 0) moneyline / spread / total
        market. `live` = IN-PLAY (came over a …/live/* topic) vs pre-match (…/pre or REST seed). Token keys
        MIRROR pair_derivatives.py: moneyline '{lid}:{mid}:{designation}' (home/away/draw); spread/total
        '{lid}:{mid}:{type}:{points:g}:{designation}'. Skips non-open markets, foreign types, bad prices."""
        t = mk.get("type")
        if mk.get("period") != 0 or t not in ("moneyline", "spread", "total"):
            return
        if mk.get("status") not in (None, "open"):
            return
        # OFFLINE gate: Pinnacle keeps status="open" and shows the last price after betting closes — the
        # `cutoffAt` (betting-close time) is the real "currently offline" tag (confirmed 2026-07-02: 785 open
        # markets sat 20–88 min past cutoff, still displaying a frozen unbettable line). Skip cutoff-passed
        # markets so the reconcile step suspends them → a stale line can't hold a phantom arb. `now` is
        # wall-clock UTC epoch, matching cutoffAt. (limits/max_stake is NOT a signal — never 0 in practice.)
        cutoff = _cutoff_ts(mk.get("cutoffAt"))
        if cutoff is not None and cutoff <= now:
            return
        max_stake = _max_risk(mk.get("limits"))
        for pr in mk.get("prices") or []:
            desig = pr.get("designation")
            dec = american_to_decimal(pr.get("price"))
            if dec <= 1.0:
                continue
            if t == "moneyline":
                if desig not in _SIDES:
                    continue
                token = f"{lid}:{mid}:{desig}"
            else:
                pts = pr.get("points")
                if pts is None:
                    continue
                if not ((t == "spread" and desig in ("home", "away")) or (t == "total" and desig in ("over", "under"))):
                    continue
                token = f"{lid}:{mid}:{t}:{float(pts):g}:{desig}"
            yield token, Selection(token, decimal_odds=dec, max_stake=max_stake, status="open", ts=now,
                                   live=live, cutoff=cutoff or 0.0)

    def _dump_ws_record(self, data: dict, lid: str, mid: str) -> None:
        """RECON (PINNACLE_WS_DUMP=<path>): append a compact summary of EVERY incoming WS record so we can see
        whether the '(Games)' DERIVATIVE matchups (spread/total/team_total) arrive over the current league-level
        subscription, or ride a different topic that needs an extra subscribe. Captures matchupId, the participant
        names (the '(Games)'/'(Sets)' suffix tells us which matchup it is), units/parentId if present, and per
        market its type/period/status/price-count/points-flag/designations. Single-writer (paho thread)."""
        try:
            rec = data.get("rec") or {}
            full = os.environ.get("PINNACLE_WS_DUMP_FULL") == "1"
            mkts = []
            for mk in rec.get("markets") or []:
                prices = mk.get("prices") or []
                mkts.append({"type": mk.get("type"), "period": mk.get("period"), "status": mk.get("status"),
                             # 'la' was a dead guess (never present). The real 'currently offline' suspects are
                             # cutoffAt (betting-cutoff time) and limits→max_stake (0 = pulled) on a status:open market.
                             "cutoffAt": mk.get("cutoffAt"),   # ISO cutoff — if past → offline even when status=open
                             "max_stake": _max_risk(mk.get("limits")),  # 0 = limits pulled → effectively unbettable
                             "keys": sorted(mk.keys()),        # ALL market fields, to spot any OTHER suspend signal
                             "n": len(prices),
                             "pts": any(pr.get("points") is not None for pr in prices),
                             "desig": [pr.get("designation") for pr in prices],
                             "price0": prices[0] if prices else None})  # full first price (spot any per-price suspend field)
            out = {"ts": round(time.time(), 3), "op": data.get("op"), "lid": lid, "mid": mid,
                   "units": rec.get("units"), "parentId": rec.get("parentId"),
                   "names": [p.get("name") for p in (rec.get("participants") or [])],
                   "markets": mkts}
            if full:
                out["raw"] = rec                               # PINNACLE_WS_DUMP_FULL=1: complete record, deep inspection
            line = json.dumps(out, default=str)
            if self._ws_dump_fh is None:
                self._ws_dump_fh = open(self._ws_dump_path, "a", encoding="utf-8")
            self._ws_dump_fh.write(line + "\n")
            self._ws_dump_fh.flush()
        except Exception as ex:
            print(f"[PINNACLE] WS dump error: {type(ex).__name__}: {ex}")

    # ── REST fallback odds source (designation read directly from each price, like the WS) ──
    async def _refresh_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(self._backoff_sec if self._backoff_sec > 0 else self._refresh_sec)
            except asyncio.CancelledError:
                break
            now = time.time()
            leagues = [lg for lg, ts in self._active_leagues.items() if now - ts <= self._active_ttl]
            if not leagues:
                continue
            # Logged out → idle (don't poll Pinnacle with a dead session). The loop keeps sleeping, not calling;
            # it auto-resumes when a fresh login lands (browser source) or the session is restored.
            if self._session_expired or (self._session_source == "browser" and not self._session_ready):
                continue
            self._rate_limited = False
            t0 = time.perf_counter()
            try:
                for i, lid in enumerate(leagues):
                    await self._refresh_league(lid)
                    if i + 1 < len(leagues) and self._jitter_ms > 0:
                        await asyncio.sleep(random.uniform(0, self._jitter_ms / 1000.0))
            except Exception as ex:
                print(f"[PINNACLE] refresh error: {type(ex).__name__}: {ex}")
            dt = time.perf_counter() - t0
            if self._rate_limited:
                self._backoff_sec = min(max(self._backoff_sec, self._refresh_sec) * 2, 120.0)
                print(f"[PINNACLE] rate-limited → backing off to {self._backoff_sec:.0f}s.")
            elif self._backoff_sec > 0:
                print(f"[PINNACLE] rate limit cleared — resuming {self._refresh_sec:g}s.")
                self._backoff_sec = 0.0
            if now - self._last_hb >= 30:
                self._last_hb = now
                rl = f"  429s: {self._rl_total}" if self._rl_total else ""
                print(f"[PINNACLE] refresh: {len(leagues)} leagues (serial) in {dt:.2f}s "
                      f"(cache={len(self._cache)} sel){rl}")

    def _apply_straight_markets(self, lid: str, markets: list, now: float) -> int:
        """Upsert every OPEN period-0 moneyline/spread/total token from a /markets/straight snapshot into the
        cache (shared by the AUTHED seed `_refresh_league` and the GUEST reader re-seed `_reseed_league_guest`).
        Each token's ts=now marks it fresh; a token no longer in the snapshot is left to age out via its ts (no
        reconcile here — the WS `_apply` does the explicit suspend). Returns the token count applied."""
        n = 0
        for mk in markets:
            mid = mk.get("matchupId")
            if mid is None:
                continue
            for token, sel in self._market_tokens(lid, mid, mk, now):
                with self._cache_lock:
                    old = self._cache.get(token)
                    # A /markets/straight snapshot is PRE-MATCH-blind — it must NOT downgrade an IN-PLAY tag the
                    # WS set (this re-seed was clobbering live→pre-live, so in-play arbs vanished after the first
                    # live game). Keep live once the WS has flagged it; the game's `del` clears it when it ends.
                    if old is not None and old.live and not sel.live:
                        sel.live = True
                    self._cache[token] = sel
                    n += 1
        return n

    def _authed_rest_blocked(self, why: str) -> bool:
        """True when an authed REST call must not be made yet, logged ONCE per blocked stretch.

        Startup order is the problem this solves. A saved Chrome profile replays its old x-session the moment
        the page loads, so credentials appear, `_session_ready` flips true, and everything downstream believes
        it is logged in — while the token is dead. Observed 2026-08-17: the startup pairing fill fired
        fourteen /leagues/*/markets/straight calls into guest redirects before the re-login had been
        attempted, which then tripped the mass-logout detector and forced a re-mint. The burst was not a
        symptom of the dead session; it was what escalated it.

        Waiting on `_session_proven` costs a few seconds at startup and removes the whole cascade. The per-
        call log is collapsed to one line because fourteen identical messages is how the actual cause
        (a stale profile) got buried the first time.
        """
        if self._session_source != "browser" or self._session_proven:
            self._authed_block_logged = False
            return False
        if not getattr(self, "_authed_block_logged", False):
            self._authed_block_logged = True
            print(f"[PINNACLE] authed REST HELD ({why}) - the session is captured but has not proven itself "
                  f"with a live call yet. Waiting for validation rather than firing into a guest redirect; "
                  f"this unblocks itself the moment the probe succeeds.", flush=True)
        return True

    async def _refresh_league(self, lid: str) -> None:
        """One-shot pre-match SNAPSHOT seed of a league (moneyline + spread + total) via the AUTHED API. Prices
        carry `designation` (home/away/draw or over/under) and `points` DIRECTLY — exactly like the WS — via the
        SAME `_market_tokens` builder, so seeded tokens key identically to WS tokens and to pair_derivatives."""
        if self._authed_rest_blocked("league seed"):
            return
        markets = await self._http_get(f"/leagues/{lid}/markets/straight", count_429=True)
        if markets:
            self._apply_straight_markets(lid, markets, time.time())

    async def _reseed_league(self, lid: str) -> int:
        """Reader-mode re-seed of one league's straight markets from PINNACLE_RESEED_SOURCE — "authed" (default,
        real logged-in prices) or "guest" (public, no session). Same `_market_tokens` keying either way, so the
        reader's WS tokens and these re-seed tokens are interchangeable in the cache. Returns tokens applied."""
        if self._reseed_source == "guest":
            markets = await self._guest_get(f"/leagues/{lid}/markets/straight")
        else:
            # The guest path is public and always safe; only the authed one needs a proven session.
            if self._authed_rest_blocked("league re-seed"):
                return 0
            markets = await self._http_get(f"/leagues/{lid}/markets/straight", count_429=True)
        return self._apply_straight_markets(lid, markets, time.time()) if markets else 0

    async def refetch_from_venue(self, selection_ids: list[str]) -> dict:
        """Force a LIVE re-read from Pinnacle for the leagues behind `selection_ids`, then report which
        leagues actually refreshed. Returns {"leagues": {lid: applied_token_count|-1}, "ok": bool}.

        WHY THIS EXISTS. `CrossArbRestVerifier` was verifying the HardVen leg with `GET /odds` -- the very
        cache the screening price came from. Re-reading a cache 119ms after writing it cannot disagree with
        itself, so the "verification" agreed 110/110 times and looked like a perfect venue. The Kalshi leg,
        which IS independently checked (a real call to Kalshi), disagreed 76% of the time. That gap was
        instrumentation, not venue quality: we had no independent read of a Pinnacle price at all.

        `_reseed_league` is the honest primitive -- it is an authed REST call to Pinnacle, the same one the
        90s backstop already makes, so it adds no new request shape or fingerprint. Only the execution path
        calls this (a couple of hundred times a day at most), not the 3s poll loop.

        A league that fails to refetch is reported as -1 rather than silently falling through to the cached
        price: the caller must be able to tell "the venue confirmed this" from "we asked and got nothing".
        """
        lids: list[str] = []
        for sid in selection_ids:
            p = self._parse_sid(sid)
            if p and p[1] not in lids:
                lids.append(p[1])
        out: dict[str, int] = {}
        for lid in lids:
            try:
                out[lid] = await self._reseed_league(lid)
            except Exception as ex:
                print(f"[PINNACLE] refetch_from_venue {lid}: {type(ex).__name__}: {ex}")
                out[lid] = -1
        return {"leagues": out, "ok": bool(lids) and all(v >= 0 for v in out.values())}

    def _straight_prices(self, lid: str, markets: list) -> dict:
        """{token: decimal_odds} for a /markets/straight payload — same `_market_tokens` keying as the cache, so
        two sources (authed vs guest) are directly comparable per token. Used by the debug snapshot below."""
        now = time.time()
        out: dict[str, float] = {}
        for mk in markets or []:
            mid = mk.get("matchupId")
            if mid is None:
                continue
            for token, sel in self._market_tokens(str(lid), mid, mk, now):
                out[token] = round(sel.decimal_odds, 4)
        return out

    async def straight_snapshot(self, lid: str, source: str = "authed") -> dict:
        """DEBUG: current /markets/straight prices for a league from `source` as {token: decimal}. Sources:
          "cache"  — read the sidecar's LIVE cache (WS-fed for a covered league) → ZERO extra Pinnacle requests
          "authed" — one logged-in REST call (real-time, any league; adds authed load)
          "guest"  — one public REST call (no session)
        Drives probe_reseed_delay.py, which polls two sources over time to measure the guest-vs-authed lag. The
        "cache" source lets the probe use the already-live WS prices as the authed truth so it only adds the
        (public, low-risk) guest calls."""
        src = str(source).lower()
        if src == "cache":
            prefix = f"{lid}:"
            with self._cache_lock:
                prices = {tok: round(s.decimal_odds, 4) for tok, s in self._cache.items()
                          if tok.startswith(prefix) and s.status == "open"}
            return {"ts": time.time(), "source": "cache", "prices": prices}
        if src == "guest":
            markets = await self._guest_get(f"/leagues/{lid}/markets/straight")
        else:
            markets = await self._http_get(f"/leagues/{lid}/markets/straight")
        return {"ts": time.time(), "source": src, "prices": self._straight_prices(lid, markets)}

    async def browser_fetch_straight_probe(self, lid: str) -> dict:
        """DEBUG (feasibility probe): try fetching the AUTHED /markets/straight from INSIDE the logged-in browser
        page (genuine Chrome TLS + the page's own origin/cookies) rather than the sidecar's httpx. The risk is a
        CORS preflight the page's fetch trips but the app's own fetch doesn't. Returns the browser fetch result
        (ok/status/n_markets/sample or error) plus what the sidecar's httpx sees right now for cross-check. GREEN
        (ok=true, n_markets>0) ⇒ the re-seed can be moved into the browser for zero non-Chrome footprint."""
        if self._browser is None:
            return {"ok": False, "error": "no browser session (needs PINNACLE_SESSION_SOURCE=browser)"}
        url = f"{REST_BASE}/leagues/{lid}/markets/straight"
        headers = {"x-session": self._session, "x-device-uuid": self._device, "x-api-key": self._api_key}
        res = await self._browser.fetch_via_page(url, headers)
        try:
            httpx_markets = await self._http_get(f"/leagues/{lid}/markets/straight")
            res["httpx_n_markets"] = len(httpx_markets or [])   # cross-check vs the sidecar's own authed call
        except Exception:
            res["httpx_n_markets"] = None
        return res

    async def _reader_reseed_loop(self) -> None:
        """Pure-reader-mode price backstop (see _reader_reseed_sec). Every cycle, re-fetch each active league's
        straight markets (authed by default → real prices) so STABLE pre-match lines and TAIL leagues (no tab)
        keep an up-to-date price + fresh ts — which is what lets _read_cache serve the REAL per-token ts (no global
        'fresh' lie) so a league that truly stops updating ages out instead of showing a phantom."""
        while True:
            try:
                await asyncio.sleep(self._reader_reseed_sec)
            except asyncio.CancelledError:
                break
            # OFF IN IN-PLAY. This is a REVERSAL of the 2026-08-17 decision, and the measurement that
            # drove that one still stands - skipping the re-seed then took books from P=180/216 to 6/242
            # and the camper sat roving for 42 minutes with nothing fresh enough to camp on. What changed
            # is not the number, it is what the in-play bot needs from it:
            #
            #   * THE PRICE THAT DECIDES A FIRE IS THE BETSLIP, and camp_fire reads it off the panel
            #     directly, re-reading it immediately before the press. A REST snapshot cannot improve on
            #     the number the venue is contractually showing us.
            #   * 216 fresh PRE-MATCH books served pre-live coverage. In-play camps on LIVE games, and the
            #     live list's own WS is subscribed to exactly those - which is why `matchups=NN` on the
            #     WS-READ heartbeat is the coverage number that now matters, not the total.
            #   * The traffic is the cost, not a side effect. A 90s metronome against the same endpoints
            #     from the same IP, running for hours beside a session that is otherwise one parked tab,
            #     is a trail no hand produces.
            #
            # GUEST IS NOT THE ANSWER and offering it was wrong: it is the delayed feed (thin edges lag),
            # and it leaves from the same IP - so it trades price quality away for nothing.
            #
            # WHAT SURVIVES: the ONE-TIME seed in `odds()` when a league is first subscribed (the WS streams
            # changes, not an on-subscribe snapshot, so a stable line would otherwise never arrive), and
            # catalog/pairing. Both are "find out what exists", which is a thing a browser does too.
            #
            # PINNACLE_RESEED_INPLAY=1 restores the cadence if in-play coverage turns out to need it.
            if self.mode == "inplay" and not self._reseed_inplay:
                if not getattr(self, "_reseed_inplay_noted", False):
                    self._reseed_inplay_noted = True
                    print("[PINNACLE] reader re-seed PAUSED for in-play - the betslip is the price that "
                          "decides a fire and the live list's WS covers the live games. Watch `matchups=` "
                          "on the WS-READ heartbeat for coverage. PINNACLE_RESEED_INPLAY=1 to restore.",
                          flush=True)
                continue
            self._reseed_inplay_noted = False
            if self._manual_mode:
                continue
            # authed re-seed needs a live session; guest is public → runs regardless
            if self._reseed_source != "guest" and self._session_source == "browser" and not self._session_ready:
                continue
            leagues = list(self._active_leagues.keys())
            if not leagues:
                continue
            t0 = time.perf_counter()
            applied = 0
            for lid in leagues:
                try:
                    applied += await self._reseed_league(lid)
                except Exception as ex:
                    print(f"[PINNACLE] reader re-seed error {lid}: {type(ex).__name__}: {ex}")
                if self._jitter_ms > 0:
                    await asyncio.sleep(random.uniform(0, self._jitter_ms / 1000.0))
            if time.time() - self._last_hb >= 30:
                self._last_hb = time.time()
                print(f"[PINNACLE] reader re-seed: {len(leagues)} league(s), {applied} token(s) "
                      f"in {time.perf_counter() - t0:.2f}s ({self._reseed_source}; cadence {self._reader_reseed_sec:g}s).")

    # ── HTTP (catalog + rest mode) ───────────────────────────────────────────
    def _note_guest_redirect(self) -> bool:
        """Record an authed-REST guest-redirect and decide if it's a MASS event = a real logout. Returns True if
        the mass-logout path was taken (so the caller SKIPS the per-blip re-sync). A burst of guest-redirects in a
        short window (default 4 in 30s) means the account x-session expired — the board WS staying live does NOT
        prove otherwise. One re-seed cycle of a dozen dead leagues trips this immediately."""
        now = time.time()
        self._guest_redirect_ts.append(now)
        self._guest_redirect_ts = [t for t in self._guest_redirect_ts if now - t <= self._mass_logout_window]
        if len(self._guest_redirect_ts) >= self._mass_logout_n:
            self._handle_mass_logout(f"{len(self._guest_redirect_ts)} guest-redirects in "
                                     f"{self._mass_logout_window:g}s")
            return True
        return False

    def _handle_mass_logout(self, reason: str) -> None:
        """A real account logout was detected. Force a genuine re-login (reload the main page + submit the login
        form, bypassing the 'recent capture = healthy' guard — that guard is fooled because a logged-out page keeps
        SENDING its dead x-session, refreshing _last_capture). Throttled so a redirect storm fires it once. Loud so
        it's visible in the log; the executor's balance gate keeps money safe until the re-login lands."""
        now = time.time()
        if now - self._last_mass_logout < self._mass_logout_throttle:
            return                                              # already handling this storm
        self._last_mass_logout = now
        self._guest_redirect_ts.clear()
        print(f"[PINNACLE] *** LIKELY LOGOUT: {reason} - board odds still stream (public) but ALL authed REST is "
              "guest-redirecting. Forcing a real re-login (reload + submit). No bets can fund until it lands.")
        # Close the league tabs FIRST. The auto-login watcher only drives the main page, so every other
        # tab keeps showing a logged-out UI: it streams nothing while still holding its slot, and the
        # operator sees several dead windows with no sign the bot is recovering. Dropping them means the
        # re-login happens on one page and the tab manager rebuilds from a known-good session on its
        # next tick (it reopens any league that is still a gap, so nothing is lost).
        mgr = getattr(self, "_tab_manager", None)
        if mgr is not None and hasattr(mgr, "drop_all_tabs"):
            try:
                asyncio.get_event_loop().create_task(mgr.drop_all_tabs("logged out"))
            except Exception as ex:
                print(f"[PINNACLE] could not drop league tabs on logout: {type(ex).__name__}: {ex}")
        if self._browser is not None:
            try:
                asyncio.get_event_loop().create_task(self._browser.force_remint(force_login=True))
            except Exception as ex:
                print(f"[PINNACLE] mass-logout re-login could not be scheduled: {type(ex).__name__}: {ex}")

    def _rest_death_check(self, reason: str) -> None:
        """A REST-replay auth failure (401/403 streak or guest-redirect) wants to declare the session dead. But
        the ODDS WS is the TRUE liveness signal: while paho is CONNECTED, the login is alive and odds are flowing
        (a genuine logout drops/auth-rejects the WS too). So a REST auth failure WHILE THE WS IS UP is just a
        STALE REPLAY x-session — re-sync it from the browser's latest token and DO NOT declare death. This fixes
        the false 'session DOWN' seen while the page stayed logged in (the REST replay 401'd while the WS kept
        streaming odds). Only when the WS is ALSO down is this a real logout. The "WS is up" signal covers BOTH
        the dedicated paho WS (`_connected`) AND the browser-window READER (its Arcadia WS alive) — so an authed
        re-seed blip in reader mode (where `_connected` is always False) can't false-kill a logged-in session."""
        try:
            reader_alive = (self._window_ws_read and self._browser is not None
                            and self._browser.odds_ws_alive(self._browser_ws_heartbeat_ttl))
        except Exception:
            reader_alive = False
        if self._connected or reader_alive:
            self._rest_auth_fails = 0                       # odds WS healthy → not a logout; stop the fail streak
            if self._http is not None and self._session:
                self._http.headers["x-session"] = self._session   # re-apply the browser's freshest x-session
            src = "paho WS" if self._connected else "browser reader WS"
            print(f"[PINNACLE] {reason}, but the odds {src} is LIVE — re-synced the REST x-session; NOT a logout "
                  "(the browser still holds the session).")
            return
        self._session_expired = True
        self._session_ready = False                         # WS is ALSO down → a genuine logout
        self._session_proven = False
        try:
            self._proven_evt.clear()
        except Exception:
            pass
        self._give_up_ws(f"{reason} (session dead — WS also down)")

    async def _http_get(self, path: str, count_429: bool = False, authed: bool = True):
        if not self._http:
            return None
        try:
            r = await self._http.get(REST_BASE + path)
        except Exception as ex:
            print(f"[PINNACLE] GET {path} error: {type(ex).__name__}: {ex}")
            return None    # network error = TRANSIENT — never a session-death signal (don't touch the fail streak)
        if r.status_code == 429:
            if count_429:
                self._rate_limited = True
                self._rl_total += 1
            print(f"[PINNACLE] *** RATE LIMITED (429) on {path} *** — raise PINNACLE_REFRESH_SEC / fewer leagues.")
            return None
        if r.status_code in (401, 403):
            # A single AUTHED 401/403 can be a blip; REPEATED = a dead session (the guest-redirect's sibling for
            # servers that 401 instead of 302). Give up after N in a row so we STOP poking a dead session, but a
            # lone blip just logs and keeps going (transient). Unauthed paths (/status) never count.
            if authed:
                self._rest_auth_fails += 1
                print(f"[PINNACLE] AUTH {r.status_code} on {path} ({self._rest_auth_fails}/{self._rest_auth_giveup}) "
                      "— session may be invalid.")
                if self._rest_auth_fails >= self._rest_auth_giveup:
                    self._rest_death_check(f"REST auth {r.status_code} x{self._rest_auth_fails}")
            else:
                print(f"[PINNACLE] AUTH {r.status_code} on {path}.")
            return None
        if r.status_code in (301, 302, 303, 307, 308):
            loc = r.headers.get("location", "")
            if "guest" in loc.lower():
                print(f"[PINNACLE] {path}: redirected to the GUEST endpoint → the replayed x-session looks EXPIRED.")
                # A BURST of these = a real logout (forces re-login). A lone blip while the WS is live = stale
                # replay → re-sync only. _note_guest_redirect returns True when it took the mass-logout path.
                if not self._note_guest_redirect():
                    self._rest_death_check("session expired — guest redirect")
            else:
                print(f"[PINNACLE] GET {path} HTTP {r.status_code} → {loc}")
            return None
        if r.status_code != 200:
            print(f"[PINNACLE] GET {path} HTTP {r.status_code}")
            return None
        if authed:
            self._rest_auth_fails = 0                       # a good AUTHED response clears the auth-fail streak
        try:
            return r.json()
        except Exception:
            return None

    async def _guest_get(self, path: str):
        """GET structural data from the GUEST API (public key, NO user session) — for catalog/pairing, which is
        just names/leagues/markets and must NOT depend on (or trip the give-up on) the authed x-session. Lazy
        client; follows redirects (the guest host is the redirect target, so it never bounces to itself)."""
        if self._guest_http is None:
            self._guest_http = httpx.AsyncClient(
                headers={"accept": "application/json", "content-type": "application/json",
                         "origin": "https://www.pinnacle.bet", "referer": "https://www.pinnacle.bet/",
                         "user-agent": USER_AGENT, "x-api-key": DEFAULT_API_KEY},
                timeout=20.0, follow_redirects=True)
        try:
            r = await self._guest_http.get(GUEST_BASE + path)
        except Exception as ex:
            print(f"[PINNACLE] GUEST GET {path} error: {type(ex).__name__}: {ex}")
            return None
        if r.status_code != 200:
            print(f"[PINNACLE] GUEST GET {path} HTTP {r.status_code}")
            return None
        try:
            return r.json()
        except Exception:
            return None

    # ── pairing catalog (GUEST API — designation-keyed to match the WS odds; no session needed) ──
    async def _catalog_league_ids(self) -> list[str]:
        """League ids to catalog: explicit PINNACLE_CATALOG_LEAGUES (stable ids — baseball 246/6227/187703)
        PLUS every current league of each PINNACLE_CATALOG_SPORTS (auto-discovery for sports whose 'leagues'
        are ephemeral tournament-rounds — tennis=33 → today's ITF/ATP/WTA events). Doubles leagues are skipped
        (the bot pairs 2-player singles vs Kalshi singles). Re-resolved per catalog() call so it tracks the
        board with no hand-editing."""
        ids = list(self._catalog_leagues)
        for sid in self._catalog_sports:
            for l in (await self._guest_get(f"/sports/{sid}/leagues") or []):
                if (l.get("matchupCount") or 0) > 0 and "doubles" not in (l.get("name", "") or "").lower():
                    ids.append(str(l.get("id")))
        return list(dict.fromkeys(ids))   # dedupe, preserve order

    async def catalog(self) -> list[CatalogEntry]:
        league_ids = await self._catalog_league_ids()
        if not league_ids:
            print("[PINNACLE] catalog(): set PINNACLE_CATALOG_LEAGUES (CSV league ids, e.g. 246=MLB) and/or "
                  "PINNACLE_CATALOG_SPORTS (CSV sport ids, e.g. 33=Tennis) for auto-discovery.")
            return []
        out: list[CatalogEntry] = []
        for i, lid in enumerate(league_ids):
            matchups = await self._guest_get(f"/leagues/{lid}/matchups") or []
            straight = await self._guest_get(f"/leagues/{lid}/markets/straight") or []
            # Catalog ONLY matchups that actually carry an AVAILABLE full-game moneyline — the SAME filter the
            # odds path uses, so every cataloged token is one the odds cache will populate. This is the robust
            # tennis discriminator: Pinnacle lists a "(Games)" (and sometimes a "(Sets)") DERIVATIVE matchup per
            # match with the SAME players, and the /matchups `hasMoneyline` flag is UNRELIABLE live (True even
            # when the market is suspended) → keying off the real market avoids double-pairing. The winner is
            # whichever matchup (clean OR "(Sets)"-labelled) has the live moneyline; the "(Games)" one (no live
            # moneyline) and the tournament outright (a many-way moneyline) are both excluded by this set.
            # matchupId -> the moneyline's price designations. A dict (not a set) so the 3-way DRAW leg can be
            # emitted: soccer matchups expose only 2 PARTICIPANTS (home/away), but the moneyline PRICES carry a
            # third 'draw' designation (which the odds path already tokenises as '{lid}:{mid}:draw' — _SIDES
            # includes 'draw'). Without emitting a draw catalog entry the pairing's Tie leg can never match.
            winner_desigs = {mk.get("matchupId"): [pr.get("designation") for pr in (mk.get("prices") or [])]
                             for mk in straight
                             if mk.get("type") == "moneyline" and mk.get("period") == 0
                             and 2 <= len(mk.get("prices") or []) <= 3}
            for m in matchups:
                if m.get("id") not in winner_desigs:
                    continue
                parts = m.get("participants") or []
                if len(parts) < 2:
                    continue
                if any("/" in (p.get("name") or "") for p in parts):
                    continue   # DOUBLES ("A / B" pairs) sit inside singles leagues too — Kalshi is singles-only
                lg = m.get("league") or {}
                sport = ((lg.get("sport") or {}).get("name")) or ""
                league_name = lg.get("name") or str(lid)
                home = _strip_units(next((p.get("name", "") for p in parts if p.get("alignment") == "home"), ""))
                away = _strip_units(next((p.get("name", "") for p in parts if p.get("alignment") == "away"), ""))
                event = f"{home} vs {away}"
                three_way = sport.strip().lower() == "soccer"
                for p in parts:
                    desig = p.get("alignment")
                    if desig not in _SIDES:
                        continue
                    out.append(CatalogEntry(
                        selection_id=f"{lid}:{m.get('id')}:{desig}",
                        sport=sport, league=league_name, event=event, market="moneyline",
                        selection_name=_strip_units(p.get("name", "")), start_time=m.get("startTime"),
                        three_way=three_way))
                # 3-way DRAW leg: not a participant — synthesise it from the moneyline's 'draw' price so the
                # Tie pairing (Kalshi NO(Tie) + Pinnacle back-Draw) can complete. Odds already serve this token.
                if three_way and "draw" in winner_desigs[m.get("id")]:
                    out.append(CatalogEntry(
                        selection_id=f"{lid}:{m.get('id')}:draw",
                        sport=sport, league=league_name, event=event, market="moneyline",
                        selection_name="Draw", start_time=m.get("startTime"),
                        three_way=three_way))
            if i + 1 < len(league_ids) and self._jitter_ms > 0:
                await asyncio.sleep(random.uniform(0, self._jitter_ms / 1000.0))   # gentle between many leagues
        return out

    # ── M1 (later): betting + wallet confirmation ──
    async def balance(self) -> Optional[float]:
        """Account cash balance via the authed wallet endpoint — same X-Session/X-Device-UUID/X-API-Key headers
        as the odds feed (the page polls this constantly, so it's a normal call). The account currency (EUR here
        — NOT USD; Kalshi is USD) is stashed for /health + the min-balance floor; cross-venue stake sizing must
        FX-convert at M1. Gated on a live session so it can't hit the authed endpoint with a stale token
        pre-login (which would trip the give-up).

        Returns **None when the balance cannot be READ** (pre-login, auth failure, malformed reply) — NOT 0.0.
        That distinction is load-bearing: BalanceGuard halts the SCHEDULE below a floor, and the halt closes the
        browser, which is the very thing that could re-authenticate. Returning 0.0 for "couldn't read" therefore
        DEADLOCKS a healthy bot — observed 2026-08-07, when a 401 on /wallet/balance halted a funded account
        seconds after a successful login. A genuine zero balance still returns 0.0."""
        if self._session_source == "browser" and not self._session_ready:
            return None
        await self._await_session_settle()      # don't poll the wallet while the site is still coming up
        data = await self._http_get("/wallet/balance")
        if not isinstance(data, dict):
            return None                      # 401 / 5xx / non-JSON — unknown, not empty
        amt = data.get("amount")
        if amt is None:
            return None                      # field absent => unreadable (a real zero arrives as 0, not None)
        try:
            self._balance = float(amt)
        except (TypeError, ValueError):
            return None
        self._balance_currency = data.get("currency") or self._balance_currency
        return self._balance

    async def place_bet(self, selection_id: str, stake: float, max_odds: float) -> BetResult:
        """Place a back bet on `selection_id` for `stake` (account currency), accepting only if the offered odds
        are >= max_odds (i.e. price <= requested). SAFETY-GATED SCAFFOLD:

          1. stake > HARDVEN_MAX_STAKE            → reject (hard cap, never overridden).
          2. session not ready                    → reject (can't place without a live login).
          3. HARDVEN_BET_ENABLE != 1 (DEFAULT)    → PREVIEW: log the intended bet, place NOTHING.
          4. enabled                              → serialise on _bet_lock, then _place_via_ui() — the browser
                                                     bet-slip automation, DEFERRED (raises until built).

        This guarantees real money can never fire without the explicit env gate AND an implemented UI path."""
        # 0. the operator is driving — a placement would seize the browser out from under them, and a bet
        #    placed while a human is clicking around the same account is unattributable afterwards.
        if self._manual_mode:
            return BetResult(accepted=False, stake=stake,
                             reason="MANUAL MODE is on — the operator is driving the browser, nothing is "
                                    "placed. Toggle it off ('m' in the sidecar window, or POST "
                                    "/control/manual) to resume trading.")
        # 1. hard stake cap
        if stake > self._max_stake:
            return BetResult(accepted=False, stake=stake,
                             reason=f"stake {stake:.2f} > HARDVEN_MAX_STAKE {self._max_stake:.2f} (hard cap)")
        # 2. must have a live session
        if self._session_source == "browser" and not self._session_ready:
            return BetResult(accepted=False, stake=stake, reason="no live Pinnacle session (login not captured)")
        # 3. preview default — nothing is placed unless explicitly enabled
        if not self._bet_enabled:
            print(f"[PINNACLE BET] PREVIEW (HARDVEN_BET_ENABLE!=1) - WOULD place {stake:.2f} on {selection_id} "
                  f"@ max_odds>={max_odds:.4f}. No bet placed.")
            return BetResult(accepted=False, stake=stake,
                             reason="preview only - set HARDVEN_BET_ENABLE=1 to place real bets")
        # 4. real placement — serialise (one browser session) and go through the UI (deferred)
        async with self._bet_lock:
            print(f"[PINNACLE BET] LIVE - placing {stake:.2f} on {selection_id} @ max_odds>={max_odds:.4f}")
            return await self._place_via_ui(selection_id, stake, max_odds)

    async def slip_quote(self, selection_id: str) -> dict:
        """Open the Quick Bet popover for a selection, read the TRUE offered odds, close it. Places nothing.

        This is the only independent confirmation of a Pinnacle price that exists. `/odds` answers from the
        sidecar cache, and re-reading a cache proves nothing; even the authed re-seed is a different read of
        the same feed. The popover is what the venue will actually honour — and it is where the price the bot
        screened on and the price it can get diverge (observed: screened 1.5102, popover 1.5100).

        Safe by construction: clicking an odds button PLACES NOTHING, it only opens the popover, and
        `_select_bet_tab` already probes and verifies the matchup/side/price BEFORE anything else happens
        (a positional miss opens the wrong popover, it cannot place the wrong bet). This is the same probe
        `_place_via_ui` performs; it simply stops before stake entry.

        Returns {ok, decimal_odds, implied_price, tab, error}. Organic activity is frozen for the duration
        and resumed in the finally, exactly as the bet path does, so nothing re-points the tab mid-read.
        """
        # ⚠ A QUOTE IS A BETSLIP, AND IN-PLAY HAS ONE TAB. `_select_bet_tab` falls through to the primary page
        # in in-play mode (by design — borrowing another tab mid-camp loses the window), so quoting here would
        # open a DIFFERENT market's Quick Bet on top of the armed one and then close it on the way out. The
        # camp would die silently and the C# side would keep believing it was armed. Refuse instead: the
        # caller treats an error as "not sampled" and refunds its budget, which costs a measurement, not a camp.
        blocked = self.manual_blocked("a slip quote")
        if blocked:
            return blocked
        if getattr(self, "_camping", False):
            return {"ok": False, "error": "camping — a slip quote would open a second Quick Bet over the "
                                          "armed one on the single live tab"}
        parts = selection_id.split(":")
        if len(parts) != 3 or parts[2] not in ("home", "away"):
            return {"ok": False, "error": f"slip quotes handle straight moneyline tokens only, got '{selection_id}'"}
        lid, _mid, _desig = parts
        exp = await self._expected_selection(selection_id)
        if not exp:
            return {"ok": False, "error": f"no catalog entry for {selection_id} -- cannot verify the market"}
        url = self._league_url_for(lid)
        if not url:
            return {"ok": False, "error": f"no league URL known for lid {lid}"}

        self._pause_all_organic()
        page = None
        t0 = time.perf_counter()
        try:
            page, tab_kind, sel_ok = await self._select_bet_tab(lid, url, exp)
            ms = round((time.perf_counter() - t0) * 1000, 1)
            # WHICH TIER WE LANDED ON IS THE WHOLE COST STORY, so report it rather than just the total:
            #   dedicated / rove  -> the league was ALREADY open: a click, no navigation (fast path)
            #   rove-nav          -> we had to navigate the roving tab to the league (seconds)
            #   board             -> blind scroll on the big combined list (slow, often misses)
            #   cold              -> a fresh tab, full page load (slowest)
            # If the slow tiers dominate, the fix is tab COVERAGE (park a tab on the league before the arb
            # matters), not a faster click — the click is already the cheap part.
            print(f"[SLIP QUOTE] {selection_id} via {tab_kind} in {ms:.0f}ms"
                  f"{'' if (sel_ok and sel_ok.get('ok')) else ' (FAILED)'}")
            if page is None or not sel_ok or not sel_ok.get("ok"):
                return {"ok": False, "tab": tab_kind, "elapsed_ms": ms,
                        "error": f"could not select the intended market: {sel_ok and sel_ok.get('error')}"}
            shown = float(sel_ok.get("price") or 0)
            if sel_ok.get("american") or shown <= 1.0 or shown > 1000:
                return {"ok": False, "tab": tab_kind, "elapsed_ms": ms,
                        "error": "the site is not on Decimal Odds"
                                 if sel_ok.get("american") else
                                 f"popover price {shown} is not decimal odds"}
            return {"ok": True, "decimal_odds": shown, "implied_price": round(1.0 / shown, 6),
                    "tab": tab_kind, "elapsed_ms": ms, "selection_id": selection_id,
                    # The venue's own name for what this quote is on, e.g. "Sabrina Dias (Sets)".
                    # The caller compares it to the Kalshi outcome to prove the two legs are OPPOSITE
                    # sides — the only check an inverted pairing cannot slip past.
                    "selection_label": sel_ok.get("label") or "",
                    "matchup": sel_ok.get("matchup") or ""}
        except Exception as e:
            return {"ok": False, "error": f"slip quote error: {type(e).__name__}: {e}"}
        finally:
            # Always close the popover and clear any stray side-betslip selection the probe left behind —
            # an open slip is both a detection tell and something the next bet would trip over.
            if page is not None:
                try:
                    await page.evaluate(_UI_CLOSE_JS)
                except Exception:
                    pass
                try:
                    await self._trim_betslip(page, source="post-quote")
                except Exception:
                    pass
            self._resume_all_organic()

    async def _place_via_ui(self, selection_id: str, stake: float, max_odds: float,
                            submit: bool = True, keep_open: bool = False) -> BetResult:
        """Place the bet by driving the real UI: open the league page, click the selection's Money Line button,
        VERIFY the Quick Bet popover really is the intended market, enter the stake, submit.

        THE WRONG-MARKET PROBLEM (why this is written the way it is). Captured from a real bet 2026-07-20: the
        odds button carries NO matchup id, no designation, no data attributes -- only `market-btn`, a set of
        rotating build-hash classes, and an aria-label containing the live price. Nothing in the board DOM ties
        a row to a matchupId. So a button can only be found positionally, and adjacent rows are near-identical
        ("Bicknell (Sets)" sits directly above "Bicknell (Games)" -- same names, different matchup). A
        positional miss would not error; it would silently bet the wrong market with real money.

        THE DEFENCE: clicking an odds button PLACES NOTHING -- it only opens the Quick Bet popover, which
        states exactly what was selected. So we PROBE rather than trust our aim: click a candidate, read the
        popover back, and only proceed when the matchup, the side, and the price all match what the caller
        asked for. A mismatch closes the popover and tries the next candidate; if none match, nothing is placed.

        Requires Quick Bet mode (the popover) and Decimal odds display -- both asserted, not assumed.
        """
        parts = selection_id.split(":")
        if len(parts) != 3:
            return BetResult(accepted=False, stake=stake,
                             reason=f"UI placement handles straight moneyline tokens only, got '{selection_id}'")
        lid, mid, desig = parts
        if desig not in ("home", "away"):
            return BetResult(accepted=False, stake=stake, reason=f"unknown designation '{desig}'")

        exp = await self._expected_selection(selection_id)
        if not exp:
            return BetResult(accepted=False, stake=stake,
                             reason=f"no catalog entry for {selection_id} -- cannot verify the market before betting")
        url = self._league_url_for(lid)
        if not url and self.mode != "inplay":
            return BetResult(accepted=False, stake=stake, reason=f"no league URL known for lid {lid}")
        if not url:
            # IN-PLAY DOES NOT NAVIGATE. The league URL exists so the bot can open a focused league page
            # and find the row there; in-play is already parked on the live list with the match on it,
            # and _select_bet_tab is deliberately restricted to the primary page in this mode. Requiring
            # a URL we will never visit rejected live matches purely because their league had never been
            # mapped — 217192 was on screen at the time.
            print(f"[PINNACLE INPLAY] lid {lid} has no mapped league URL; searching the live list "
                  f"directly (no navigation needed in this mode)", flush=True)

        # WHERE THE ARM ACTUALLY GOES. The 2026-08-20 arm took 9077ms with the row already on screen, and
        # only 2722ms of it was accounted for by the two [PINNACLE CLICK] lines — the other ~6.3s had no
        # owner at all. Each phase is stamped so the next slow arm names its own bottleneck rather than
        # inviting another guess.
        _ph: dict = {}
        _tp = [time.time()]
        def _mark(name: str):
            now = time.time(); _ph[name] = round((now - _tp[0]) * 1000); _tp[0] = now

        # Freeze all human-activity loops for the bet BEFORE touching a tab, so per-tab organic / the tab sweep
        # can't steal focus or re-point the tab mid-bet. Resumed in the finally.
        self._pause_all_organic()
        _mark("pause")

        # Authoritative bet id / accepted price come from the app's own POST response, not from scraping.
        placed: dict = {}
        page = None

        # `bets_seen` collects the app's OWN `GET /0.1/bets` responses. Pinnacle polls that endpoint every
        # ~0.2s while a bet is in flight (measured in the 2026-08-16 recon) precisely because the
        # placement POST does NOT confirm anything — it answers {"requestId": ..., "status":
        # "PENDING_ACCEPTANCE"} with no bet id and no accepted price. Reading the site's own poll costs no
        # extra request, cannot be rate-limited, and is exactly how the page itself learns the outcome.
        bets_seen: list = []

        def _on_resp(resp):
            try:
                if "/bets/straight" in resp.url and resp.url.rstrip("/").endswith("straight") \
                        and resp.request.method == "POST":
                    placed["status"] = resp.status
                    placed["_resp"] = resp
                elif "/0.1/bets" in resp.url and resp.request.method == "GET":
                    bets_seen.append(resp)
                    del bets_seen[:-12]        # only the recent ones matter
            except Exception:
                pass

        try:
            # Choose the tab to bet on and VERIFY the intended market on it, most natural first: the primary
            # board when it's showing the league, else a reader tab already on it, else the roving tail tab.
            # Selection places nothing (only opens the popover), so trying tabs in turn is safe.
            page, tab_kind, sel_ok = await self._select_bet_tab(lid, url, exp)
            _mark("select")
            if page is None or not sel_ok or not sel_ok.get("ok"):
                print("[PINNACLE ARM] " + " ".join(f"{k}={v}ms" for k, v in _ph.items())
                      + f" (FAILED at select)", flush=True)
                # THE ROW WAS NOT THERE — but the log can only say that, not show it. On 2026-08-20 this
                # fired three times on the day's best pair ("no row mentions both 'miron' and 'mazzola',
                # scanned 10 viewports") and nothing recorded whether the row was absent, renamed,
                # collapsed under a league header, or just below the last viewport scanned.
                await self.snap(page or self._primary_page(), f"arm-no-row-{lid}",
                                f"looking for {selection_id}: {sel_ok and sel_ok.get('error')}")
                return BetResult(accepted=False, stake=stake,
                                 reason=f"could not select the intended market: {sel_ok and sel_ok.get('error')}")
            print(f"[PINNACLE BET] using {tab_kind} tab for {selection_id}")

            shown = float(sel_ok.get("price") or 0)
            if sel_ok.get("american") or shown <= 1.0 or shown > 1000:
                await page.evaluate(_UI_CLOSE_JS)
                return BetResult(accepted=False, stake=stake,
                                 reason="the site is on American odds -- set it to Decimal Odds"
                                 if sel_ok.get("american") else
                                 f"popover price {shown} is not decimal odds -- set the site to Decimal Odds")
            if shown < max_odds - 1e-9:
                await page.evaluate(_UI_CLOSE_JS)
                return BetResult(accepted=False, stake=stake,
                                 reason=f"odds moved: offered {shown:.4f} < required {max_odds:.4f}")

            filled = await page.evaluate(_UI_STAKE_JS, {"stake": stake})
            _mark("stake")
            if not filled or not filled.get("ok"):
                await page.evaluate(_UI_CLOSE_JS)
                return BetResult(accepted=False, stake=stake,
                                 reason=f"stake entry failed: {filled and filled.get('error')}")
            max_bet = filled.get("maxBet")
            if max_bet and stake > float(max_bet):
                await page.evaluate(_UI_CLOSE_JS)
                return BetResult(accepted=False, stake=stake,
                                 reason=f"stake {stake:.2f} exceeds the book's max bet {float(max_bet):.2f}")

            # VERIFY-ONLY: everything above is the whole risk surface (navigation, finding the row, confirming
            # the popover really is the intended market, stake entry). Stopping here exercises it for free.
            if not submit:
                # ...AND NORMALLY CLOSES, because a rehearsal must leave nothing behind. CAMPING is the
                # one caller that wants the opposite: the armed popover IS the product, and closing it
                # here is what made the camp look like the slip was being rejected. Two callers, opposite
                # requirements, one flag — the default stays "clean up".
                if not keep_open:
                    await page.evaluate(_UI_CLOSE_JS)
                _mark("verify")
                print("[PINNACLE ARM] " + " ".join(f"{k}={v}ms" for k, v in _ph.items())
                      + f" total={sum(_ph.values())}ms", flush=True)
                print(f"[PINNACLE BET] VERIFY-ONLY OK {selection_id} @ {shown} stake {stake:.2f} "
                      f"(max bet {max_bet}) - popover matched, NOTHING placed"
                      + (" — LEFT OPEN for camping" if keep_open else ""))
                return BetResult(accepted=False, stake=stake, actual_odds=shown,
                                 reason=f"verify-only: would place {stake:.2f} @ {shown} on "
                                        f"'{sel_ok.get('label')}' ({sel_ok.get('matchup')}); max bet {max_bet}")

            # Real human click on Place Bet (a synthetic JS .click misfires the slip, same as the odds button).
            place = page.locator("#quick-bet-portal").get_by_role(
                "button", name=re.compile(r"place\s*bet", re.I)).first
            try:
                if await place.count() == 0:
                    await page.evaluate(_UI_CLOSE_JS)
                    return BetResult(accepted=False, stake=stake, reason="submit failed: Place Bet button not found")
                if await place.is_disabled():
                    await page.evaluate(_UI_CLOSE_JS)
                    return BetResult(accepted=False, stake=stake, reason="submit failed: Place Bet button disabled")
            except Exception:
                pass
            page.on("response", _on_resp)
            await asyncio.sleep(random.uniform(0.3, 0.8))    # a person reads the slip before committing
            # ALWAYS FAST. This is the committing press, and it is the one click in the system where a
            # second of added realism is a second of price movement against a bet already decided on.
            if not await self._human_click_loc(page, place, fast=True):
                await page.evaluate(_UI_CLOSE_JS)
                return BetResult(accepted=False, stake=stake, reason="submit failed: could not click Place Bet")

            # Wait for the app's own POST /bets/straight to come back, watching for an accept-odds prompt.
            body: dict = {}
            prompt: dict = {}
            for _ in range(60):                     # up to ~15s
                await asyncio.sleep(0.25)
                if "_resp" in placed:
                    try:
                        body = await placed["_resp"].json()
                    except Exception:
                        body = {}
                    break
                if not prompt:
                    try:
                        pr = await page.evaluate(_UI_PROMPT_JS)
                        if pr and pr.get("prompt"):
                            prompt = pr
                    except Exception:
                        pass
            if "_resp" not in placed and prompt:
                await page.evaluate(_UI_CLOSE_JS)
                print(f"[PINNACLE BET] ACCEPT-ODDS PROMPT hit on {selection_id} - NOT auto-accepting unknown "
                      f"markup; no bet placed. buttons={prompt.get('buttons')}")
                return BetResult(accepted=False, stake=stake,
                                 reason="odds-changed prompt appeared; markup unverified so it was NOT accepted "
                                        f"(no bet placed). buttons={prompt.get('buttons')} "
                                        f"text={prompt.get('text', '')[:160]}")
            if "_resp" not in placed:
                print(f"[PINNACLE BET] NO CONFIRMATION for {selection_id} - bet MAY have been placed; "
                      f"reconcile against My Bets before retrying")
                return BetResult(accepted=False, stake=stake,
                                 reason="no bet response within 15s -- state UNKNOWN, do not retry blindly")
            if placed.get("status") != 200:
                return BetResult(accepted=False, stake=stake,
                                 reason=f"bet rejected by Pinnacle (HTTP {placed.get('status')})")

            # ── HTTP 200 IS NOT AN ACCEPTED BET ──────────────────────────────────────────────────────
            # The response is {"requestId": "...", "status": "PENDING_ACCEPTANCE"} — captured verbatim
            # 2026-08-16. It carries NO betId and NO accepted price. This block used to read `betId`
            # (absent -> None), fall back to the SCRAPED price, and return accepted=True. So a bet
            # Pinnacle had merely RECEIVED was booked as filled, with nothing to reconcile it by. If it
            # was then rejected, the executor had already hedged Kalshi and was carrying a naked leg
            # while reporting a completed arb. Pre-live acceptance is near-certain so it survived; in-play
            # is exactly where rejections happen, which is why this had to be fixed before camp_fire.
            req_id = str(body.get("requestId") or body.get("requestID") or "") or None
            status = str(body.get("status") or "").upper()
            bet_id = str(body.get("betId") or body.get("id") or "") or None
            got = body.get("price") or shown

            if bet_id and status not in ("PENDING_ACCEPTANCE", "PENDING"):
                # Some responses may carry the id directly; trust that when it happens.
                print(f"[PINNACLE BET] PLACED {stake:.2f} on {selection_id} @ {got} (bet {bet_id})")
                return BetResult(accepted=True, bet_id=bet_id, actual_odds=float(got), stake=stake)

            if not req_id:
                print(f"[PINNACLE BET] response carried neither a betId nor a requestId "
                      f"(status={status or 'none'}) — cannot confirm. Body: {str(body)[:200]}")
                return BetResult(accepted=False, stake=stake,
                                 reason="placement response had no betId and no requestId — state "
                                        "UNKNOWN, reconcile against My Bets before retrying")

            conf = await self._confirm_bet(req_id, bets_seen)
            if conf.get("accepted"):
                print(f"[PINNACLE BET] PLACED {stake:.2f} on {selection_id} @ {conf.get('price') or got} "
                      f"(bet {conf.get('bet_id')}, confirmed in {conf.get('waited_s', 0):.1f}s)")
                return BetResult(accepted=True, bet_id=conf.get("bet_id"),
                                 actual_odds=float(conf.get("price") or got), stake=stake)
            if conf.get("rejected"):
                print(f"[PINNACLE BET] REJECTED by Pinnacle ({conf.get('status')}) — nothing is on. "
                      f"request {req_id}")
                return BetResult(accepted=False, stake=stake,
                                 reason=f"Pinnacle rejected the bet ({conf.get('status')}) — no position "
                                        f"was opened; do NOT hedge against this result")
            # Neither confirmed nor rejected inside the window. NOT an acceptance: saying "accepted"
            # here is what the old code did, and it is the direction that costs money.
            print(f"[PINNACLE BET] UNCONFIRMED after {conf.get('waited_s', 0):.1f}s — request {req_id} "
                  f"never appeared in the account's bet list. The bet MAY be live.")
            return BetResult(accepted=False, stake=stake,
                             reason=f"placement accepted by the API but not confirmed in the bet list "
                                    f"within {conf.get('waited_s', 0):.0f}s (requestId {req_id}) — state "
                                    f"UNKNOWN, reconcile against My Bets; do not retry blindly")
        except Exception as e:
            return BetResult(accepted=False, stake=stake, reason=f"UI placement error: {e}")
        finally:
            if page is not None:
                try:
                    page.remove_listener("response", _on_resp)
                except Exception:
                    pass
                # Post-bet hygiene: clear any stray selection probing left in the side betslip. Runs while organic
                # is STILL paused (resume is below), so nothing fights the trim clicks. Never affects the result.
                try:
                    await self._trim_betslip(page, source="post-bet")
                except Exception as e:
                    print(f"[PINNACLE BET] betslip trim skipped: {e}")
            self._resume_all_organic()

    async def verify_bet_ui(self, selection_id: str, stake: float, max_odds: float,
                            submit: bool = False) -> BetResult:
        """Manual single-bet test harness (sidecar `POST /bet/test`). Runs the REAL placement path so what is
        exercised is what will run live -- but defaults to `submit=False`, which stops just before clicking
        Place Bet. That covers the whole risk surface (navigate, find the row, verify the popover is the
        intended market, enter the stake) for free.

        Deliberately bypasses the `HARDVEN_BET_ENABLE` gate ONLY when submit is False, so verification can be
        rehearsed without ever arming real betting. `submit=True` still requires the gate."""
        # The stake hard-cap guards a REAL placement — it does NOT apply to a verify-only drive (submit=False),
        # which places nothing. Enforcing it on verify-only wrongly failed the DRYRUN_UI drive whenever the sized
        # stake exceeded the cap (e.g. HARDVEN_MAX_STAKE below the 10 EUR ladder min rung).
        if submit and stake > self._max_stake:
            return BetResult(accepted=False, stake=stake,
                             reason=f"stake {stake:.2f} > HARDVEN_MAX_STAKE {self._max_stake:.2f} (hard cap)")
        if self._session_source == "browser" and not self._session_ready:
            return BetResult(accepted=False, stake=stake, reason="no live Pinnacle session")
        if submit and not self._bet_enabled:
            return BetResult(accepted=False, stake=stake,
                             reason="submit=true requires HARDVEN_BET_ENABLE=1")
        async with self._bet_lock:
            mode = "LIVE SUBMIT" if submit else "VERIFY-ONLY"
            print(f"[PINNACLE BET] TEST ({mode}) {selection_id} stake={stake:.2f} max_odds>={max_odds:.4f}")
            return await self._place_via_ui(selection_id, stake, max_odds, submit=submit)

    # ── PLACEMENT CONFIRMATION ────────────────────────────────────────────────
    # Terminal states. Anything else is still in flight and must not be treated as either outcome.
    _BET_OK = {"ACCEPTED", "PLACED", "CONFIRMED", "WON", "LOST", "SETTLED", "OPEN", "RUNNING", "PENDING_SETTLEMENT"}
    _BET_BAD = {"REJECTED", "CANCELLED", "CANCELED", "DECLINED", "FAILED", "VOID", "EXPIRED"}

    # What the BETSLIP COLUMN looks like, sampled while a bet is being confirmed. Deliberately a
    # data-test-id sweep rather than a guess at one selector: the receipt's markup is unknown, and the
    # point of this is to FIND it, not to assume it.
    # Where a placement receipt can land. The camp path fires through the Quick Bet popover, so THAT is
    # the container to watch - the first version only looked at the side Betslip column and saw nothing,
    # which is why the 2026-08-19 fire produced no reading at all.
    _RECEIPT_CONTAINERS = ("#quick-bet-portal",
                           '[data-test-id="Betslip"]',
                           '[class*="betslip" i]')
    # A Pinnacle bet id as it appears in our own records: 2258936819, 2258987331. Guarded on both sides so
    # it cannot latch onto part of a decimal - a stake, a price or a return must never be read as an id.
    _RECEIPT_BETID_RX = re.compile(r"(?<![\d.])(\d{9,12})(?![\d.])")
    _RECEIPT_WORDS_RX = re.compile(r"bet placed|bet accepted|accepted|receipt|your bet|wager placed|"
                                   r"bet id|ticket|confirmation", re.I)

    def suspend_token(self, selection_id: str, why: str) -> bool:
        """Force a token to `suspended` in the odds cache, so the C# book clears and no arb is computed on it.

        WHY THIS IS NEEDED AT ALL: the WS keeps PUSHING a price for a market the site will not take a bet on.
        The adapter already knows two ways a market goes offline — the push stops carrying it (reconcile
        suspends it) and `cutoffAt` passes — but neither covers a line the venue has simply LOCKED while
        still streaming its last price. `mk["status"]` stays "open" through it (confirmed 2026-07-02 on 785
        markets) and `cutoffAt` is absent on in-play tennis, so nothing downstream can tell.

        Result, observed 2026-08-20: the book stayed live, the executor kept detecting arbs on it, the camp
        armed, and the venue answered the placement with HTTP 400. Detection has to learn the market is gone
        or it will keep spending presses on it.

        Deliberately one-way. The next genuine WS push for this token overwrites the entry with a fresh
        `open`, so a market that comes back needs no un-suspending here — and nothing has to decide when a
        lock has lifted, which is not a judgement this has any evidence for.
        """
        with self._cache_lock:
            old = self._cache.get(selection_id)
            if old is None or old.status == "suspended":
                return False
            self._cache[selection_id] = Selection(old.selection_id, old.decimal_odds, old.max_stake,
                                                  status="suspended", ts=time.time(),
                                                  live=old.live, cutoff=old.cutoff)
        print(f"[PINNACLE STATUS] {selection_id} forced SUSPENDED — {why}. The book clears; no arb can be "
              f"computed on it until the feed pushes it open again.", flush=True)
        return True

    async def probe_lock(self, page, selection_id: str) -> dict:
        """Is this selection actually BETTABLE on the page right now? Reads the armed popover.

        WHAT IS PROVEN AND WHAT IS NOT. Two signals here need no knowledge of Pinnacle's markup and are used
        as the verdict: there is no parsable price, or the PLACE BET control is disabled. Everything else —
        the padlock glyph, the greyed cell, whatever class carries it — has NOT been observed, so instead of
        guessing a selector this DUMPS the panel's attributes the first time it sees a lock. Same discipline
        as the receipt: the detector gets sharpened on evidence, not on a plausible-sounding class name.

        Returns {"locked": True|False|None, "why": str}. None means "could not read" and must never be
        treated as locked — in-play markets suspend between points constantly, and a read error is not a
        market state.
        """
        out = {"locked": None, "why": ""}
        try:
            portal = page.locator("#quick-bet-portal")
            if not await portal.count():
                out["why"] = "no popover on the page"
                return out
            txt = (await portal.first.inner_text()).replace(chr(10), " | ")
            price = self._CAMP_PRICE_RX.search(txt)
            pb = portal.get_by_text("PLACE BET", exact=False).last
            disabled = (await pb.is_disabled()) if await pb.count() else True
            if price and not disabled:
                out["locked"] = False
                return out
            out["locked"] = True
            out["why"] = ("no price on the panel" if not price else "PLACE BET is disabled")
            if not getattr(self, "_lock_dumped", False):
                self._lock_dumped = True
                print(f"[PINNACLE LOCK] first locked panel seen for {selection_id} ({out['why']}). "
                      f"Panel text: {txt[:300]}", flush=True)
                await self._dump_panel_controls(page, "#quick-bet-portal")
                await self.snap(page, f"locked-{selection_id.replace(':','-')}", out["why"])
        except Exception as e:
            out["locked"] = None
            out["why"] = f"{type(e).__name__}: {e}"
        return out

    async def snap(self, page, tag: str, note: str = "") -> str:
        """Screenshot the page. Returns the path written, or "" if it could not be taken.

        WHY: the money moments happen in under two seconds and the operator cannot watch every one — an arm
        that could not find its row, a placement the venue refused with an HTTP 400, a panel that would not
        close. All of those leave a log line saying WHAT failed and nothing showing what the page looked
        like when it did. On 2026-08-20 a camp failed to arm three times on the day's best pair with
        "no row mentions both 'miron' and 'mazzola' (scanned 10 viewports)" and there is no way to tell from
        here whether the row was absent, renamed, collapsed under a league header, or simply below the fold.

        Best-effort by construction: a failed screenshot must never turn a recoverable failure into an
        exception on the placement path, so everything here is swallowed.
        """
        try:
            if page is None or page.is_closed():
                return ""
            d = Path(__file__).parent.parent / "shots"
            d.mkdir(exist_ok=True)
            name = f"{time.strftime('%H%M%S')}_{re.sub(r'[^A-Za-z0-9_-]', '', tag)[:40]}.png"
            path = d / name
            await page.screenshot(path=str(path), full_page=False)
            print(f"[PINNACLE SHOT] {name}" + (f" — {note}" if note else ""), flush=True)
            return str(path)
        except Exception as ex:
            print(f"[PINNACLE SHOT] could not capture '{tag}' ({type(ex).__name__}: {ex})", flush=True)
            return ""

    async def _dump_panel_controls(self, page, sel: str) -> None:
        """List every control in the placement panel, once. Four escalating dismiss attempts have now failed
        twice (2026-08-19) and a fifth guessed selector is not a plan - this prints what is ACTUALLY there so
        the close control can be targeted by name instead of hoped for. Reads through locators only."""
        if getattr(self, "_panel_dumped", False):
            return
        self._panel_dumped = True
        try:
            out = []
            for kind in ("button", "[role=button]", "[data-test-id]", "svg", "[class*='close' i]"):
                loc = page.locator(f"{sel} {kind}")
                for i in range(min(await loc.count(), 12)):
                    el = loc.nth(i)
                    try:
                        txt = (await el.inner_text() or "").strip().replace(chr(10), " ")[:40]
                    except Exception:
                        txt = ""
                    attrs = {}
                    for a in ("aria-label", "data-test-id", "class", "title"):
                        try:
                            v = await el.get_attribute(a)
                        except Exception:
                            v = None
                        if v:
                            attrs[a] = v[:60]
                    if txt or attrs:
                        out.append(f"    {kind:22s} text={txt!r} {attrs}")
            body = chr(10).join(out) if out else "    (none)"
            print(f"[PINNACLE PANEL] controls inside {sel}:" + chr(10) + body, flush=True)
        except Exception as ex:
            print(f"[PINNACLE PANEL] could not enumerate {sel} ({type(ex).__name__}: {ex})", flush=True)

    async def _watch_receipt_dom(self, page, t0: float, out: dict = None, budget: float = 40.0,
                                 exclude: set = None) -> dict:
        """Watch the placement panel for a RECEIPT, and publish what it finds into `out` as it goes.

        TWO JOBS, and the second is new. It still measures WHEN the UI knows, so the network path can be
        judged against it. But it now also acts as the confirmation FALLBACK: `_confirm_bet` depends on the
        page choosing to poll GET /0.1/bets, and on 2026-08-19 we could not establish from the logs whether
        that had happened on an earlier fire - a route we cannot verify is a route that needs a backstop.

        WHAT COUNTS AS PROOF. Not "the panel changed" - a panel changes on an error, a re-ask, or simply
        being cleared, and reading any of those as an acceptance would hedge against a bet that is not on,
        which is the naked-leg failure this whole path exists to avoid. The only DOM evidence accepted is a
        BET ID: the venue's own identifier for a bet it has taken, which can also be reconciled afterwards.
        Keywords are recorded but never sufficient on their own.

        READS THROUGH LOCATORS, NOT page.evaluate - locators run in an isolated world; evaluate runs in the
        page's own and is script the site could see. Same reading, nothing injected.

        REPORTS EVEN WHEN CANCELLED. camp_fire cancels this when it returns, which used to kill the task
        mid-sleep and print nothing, leaving "the panel never changed" and "the probe threw every time"
        indistinguishable. The summary is now in a finally and carries the counts, so silence is a RESULT.
        """
        st = out if out is not None else {}
        st.setdefault("first_change_ms", None)
        st.setdefault("bet_id", None)
        st.setdefault("bet_id_ms", None)
        st.setdefault("words", None)
        st.setdefault("text", None)
        base = None
        errors = samples = 0
        which = None
        try:
            while time.time() - t0 < budget:
                txt = None
                for sel in self._RECEIPT_CONTAINERS:
                    try:
                        loc = page.locator(sel).first
                        if await loc.count():
                            txt = (await loc.inner_text()).replace(chr(10), " | ").strip()[:400]
                            which = sel
                            break
                    except Exception:
                        continue
                if txt is None:
                    errors += 1
                    await asyncio.sleep(0.2)
                    continue
                samples += 1
                st["text"] = txt
                st["container"] = which
                if base is None:
                    base = txt
                elif st["first_change_ms"] is None and txt != base:
                    st["first_change_ms"] = round((time.time() - t0) * 1000, 1)
                    print(f"[PINNACLE RECEIPT] {which} changed {st['first_change_ms']:.0f}ms after the press "
                          f"| was: {base[:110]} | now: {txt[:200]}", flush=True)
                if st["bet_id"] is None:
                    # Only look for an id in text that has CHANGED from the armed state. The armed slip
                    # cannot already contain a bet id, but pinning it to post-change text means a stray
                    # long number in the resting panel can never be mistaken for one.
                    if base is not None and txt != base:
                        # EXCLUDE THE IDS WE PUT THERE. A Pinnacle MATCHUP id is the same shape as a bet
                        # id - the camped selection 214120:1634228795:home carries a 10-digit matchup id
                        # that a bare digit-run match reads as a receipt (caught in test, not in
                        # production). The camp already knows its own league and matchup, so they can be
                        # ruled out exactly rather than guessed at with more regex.
                        m = next((g for g in self._RECEIPT_BETID_RX.findall(txt)
                                  if g not in (exclude or set())), None)
                        if m:
                            st["bet_id"] = m
                            st["bet_id_ms"] = round((time.time() - t0) * 1000, 1)
                            st["words"] = bool(self._RECEIPT_WORDS_RX.search(txt))
                            print(f"[PINNACLE RECEIPT] bet id {st['bet_id']} visible in {which} "
                                  f"{st['bet_id_ms']:.0f}ms after the press "
                                  f"(receipt wording {'present' if st['words'] else 'ABSENT'})", flush=True)
                            await self._dump_panel_controls(page, which)
                # "Processing Live Bet..." is the venue HOLDING the bet, not the receipt. The settled
                # receipt replaces it, and THAT is the panel carrying the close control - so keep looking
                # rather than stopping at the first change.
                if st.get("processing_ms") is None and "processing" in txt.lower():
                    st["processing_ms"] = round((time.time() - t0) * 1000, 1)
                elif (st.get("settled_ms") is None and st.get("processing_ms") is not None
                        and "processing" not in txt.lower()):
                    st["settled_ms"] = round((time.time() - t0) * 1000, 1)
                    print(f"[PINNACLE RECEIPT] processing CLEARED at {st['settled_ms']:.0f}ms "
                          f"(held {st['settled_ms'] - st['processing_ms']:.0f}ms) | now: {txt[:220]}",
                          flush=True)
                    await self._dump_panel_controls(page, which)
                await asyncio.sleep(0.2)
        finally:
            if st["first_change_ms"] is None:
                print(f"[PINNACLE RECEIPT] no change seen in {(time.time() - t0) * 1000:.0f}ms "
                      f"({samples} sample(s) of {which or 'NO container matched'}, {errors} unreadable) "
                      f"- the DOM is not a faster signal here, or the receipt lands somewhere else.",
                      flush=True)
            elif st["bet_id"] is None:
                print(f"[PINNACLE RECEIPT] panel changed but NO bet id in it - "
                      f"cannot be used as confirmation. Text: {(st.get('text') or '')[:240]}", flush=True)
            st["samples"], st["errors"] = samples, errors
        return st

    async def _confirm_bet(self, req_id: str, bets_seen: list, timeout: float = None,
                           evt: "asyncio.Event" = None) -> dict:
        """Did `req_id` actually become a bet? Returns {accepted|rejected, bet_id, price, status, waited_s}.

        WHY THIS IS NEEDED AT ALL: `POST /0.1/bets/straight` answers `PENDING_ACCEPTANCE` — the request
        was received, nothing more. Pinnacle's own page discovers the outcome by polling `GET /0.1/bets`
        at ~0.2s, which is why this reads THAT rather than issuing anything: the responses are already
        arriving, so confirmation costs no request and cannot be throttled.

        THREE OUTCOMES, and the third is the one that matters. Confirmed and rejected are both actionable.
        Neither-within-the-window is NOT an acceptance — it is "unknown", and the caller must refuse to
        hedge against it. Reporting unknown as accepted is precisely the bug this replaces.
        """
        import json as _json
        deadline = time.time() + (timeout if timeout is not None
                                  else float(os.environ.get("PINNACLE_BET_CONFIRM_SEC", "12")))
        t0 = time.time()
        seen_ids = set()
        while time.time() < deadline:
            # Cleared BEFORE the scan, never after: a response arriving mid-scan then re-sets it and the
            # wait below returns immediately. Clearing afterwards would swallow exactly that wakeup.
            if evt is not None:
                evt.clear()
            # Walk newest first; the poll that carries our bet is usually the most recent one.
            for resp in list(reversed(bets_seen)):
                if id(resp) in seen_ids:
                    continue
                seen_ids.add(id(resp))
                try:
                    data = await resp.json()
                except Exception:
                    continue
                rows = data if isinstance(data, list) else (data.get("bets") or data.get("data") or [])
                if isinstance(rows, dict):
                    rows = [rows]
                for b in rows or []:
                    if not isinstance(b, dict):
                        continue
                    rid = str(b.get("requestId") or b.get("requestID") or "")
                    if rid != req_id:
                        continue
                    st = str(b.get("status") or b.get("betStatus") or "").upper()
                    price = (b.get("price") or b.get("acceptedPrice")
                             or (b.get("selections") or [{}])[0].get("price"))
                    bid = str(b.get("betId") or b.get("id") or "") or None
                    if st in self._BET_BAD:
                        return {"rejected": True, "status": st, "bet_id": bid,
                                "waited_s": time.time() - t0}
                    # An id with a non-terminal-bad status means Pinnacle owns the bet.
                    if bid or st in self._BET_OK:
                        return {"accepted": True, "bet_id": bid, "price": price, "status": st,
                                "waited_s": time.time() - t0}
            # Wake the instant the next /0.1/bets response lands rather than sitting out a fixed tick. The
            # page polls that endpoint at ~0.2s, so a flat 0.25s sleep was landing roughly a whole poll late
            # on average - and this wait sits directly in front of the Kalshi hedge. Falls back to the same
            # 0.25s as a ceiling when no event is supplied (or none arrives).
            if evt is not None:
                try:
                    await asyncio.wait_for(evt.wait(), timeout=0.25)
                except asyncio.TimeoutError:
                    pass
            else:
                await asyncio.sleep(0.25)
        return {"accepted": False, "rejected": False, "status": "UNCONFIRMED",
                "waited_s": time.time() - t0}

    # ── MODE ──────────────────────────────────────────────────────────────────
    @property
    def mode(self) -> str:
        """'prelive' (default) or 'inplay'. ONE place that answers which personality is running.

        The two are genuinely different bots sharing an adapter, and the differences are not cosmetic:

                            PRE-LIVE                        IN-PLAY
          tabs              tab manager + rove + board      ONE live tab, tab manager HELD
          page navigation   session organic browses         pinned to the live list
          idle activity     per-tab organic                 scroll + random slip peeks, hover when camped
          betslip           trimmed on sight                armed slip PRESERVED while camping
          bet tab choice    reader -> rove -> board         primary page only
          click realism     fast (arb is ticking)           full (nothing is racing)

        Derived from whether in-play is running rather than stored separately, so the two can never
        disagree — a mode flag that drifts out of step with the thing it describes is worse than none.
        """
        return "inplay" if getattr(self, "_inplay", None) is not None else "prelive"

    # ── IN-PLAY MODE ──────────────────────────────────────────────────────────
    async def start_inplay(self) -> dict:
        """One live tab, no tab manager, camp-aware idle. PINNACLE_INPLAY=1.

        REPLACES the tab pool rather than sitting beside it (operator's call): flipping tabs while a slip
        is armed is the one thing guaranteed to lose the window camping exists to catch. The cost is
        real — no pre-live coverage while this runs — so it is a MODE, not a background extra.
        """
        if getattr(self, "_inplay", None) is not None:
            return {"ok": True, "already": True, **self._inplay.status()}
        page = self._primary_page()
        if page is None:
            return {"ok": False, "error": "no primary page"}
        try:
            from inplay import InPlayActivity, LIVE_URL
        except Exception as e:
            return {"ok": False, "error": f"inplay unavailable: {type(e).__name__}: {e}"}
        # ARRIVE THE WAY A PERSON DOES: sport board first, pause, then the Live tab on it. A session that
        # deep-links to /tennis/matchups/live/ as its first navigation after login has an entry pattern no
        # hand produces — nobody types that URL, they click Tennis and then Live. The intermediate hop costs
        # one page load at startup and happens exactly once per session, so there is no reason not to pay it.
        # Best-effort throughout: if any step fails the direct goto below still gets us there, because being
        # on the live list matters more than how we arrived.
        try:
            if LIVE_URL.split("?")[0] not in (page.url or ""):
                board = LIVE_URL.split("/matchups/")[0] + "/matchups/"      # …/en/tennis/matchups/
                try:
                    await page.goto(board, wait_until="domcontentloaded")
                    await asyncio.sleep(random.uniform(1.8, 4.2))           # a beat to look at the board
                    # The Live control is a link on the board. Click it if it is there; fall back to the URL.
                    live_link = page.get_by_role("link", name=re.compile(r"^\s*live\s*$", re.I))
                    if await live_link.count():
                        await self._human_click_loc(page, live_link.first)
                        await page.wait_for_load_state("domcontentloaded")
                except Exception as e:
                    print(f"[PINNACLE INPLAY] board->live nav did not take ({type(e).__name__}: {e}) - "
                          f"going straight to the live list", flush=True)
            if LIVE_URL.split("?")[0] not in (page.url or ""):
                await page.goto(LIVE_URL, wait_until="domcontentloaded")
        except Exception as e:
            return {"ok": False, "error": f"could not open {LIVE_URL}: {type(e).__name__}: {e}"}
        # SILENCE EVERYTHING THAT MOVES THE PAGE OR OPENS TABS. Two separate offenders, both correct
        # pre-live and both wrong here:
        #   * the SESSION's organic navigates the primary page around PINNACLE_BROWSE_URLS, which
        #     includes /tennis/matchups/ — so it kept dragging the camp off the live list.
        #   * the TAB MANAGER's sweep opens and re-points rove tabs on its own cadence. Skipping tab
        #     candidates in _select_bet_tab stops PLACEMENT borrowing one; it does not stop the sweep
        #     creating them.
        #   * the BOARD-DRIFT WATCHDOG drags the primary page back to `_home_url` after board_drift_sec
        #     — a timer that pulled the camp onto /matchups/ no matter what else was silenced. RE-POINTED
        #     at the live list rather than disabled: with one tab and no rove, in-play needs that
        #     recovery more than pre-live does.
        self._inplay_prev_home = None
        try:
            if self._browser is not None:
                self._browser.pause_activity()
                self._inplay_prev_home = self._browser.set_home_url(LIVE_URL)
        except Exception as e:
            print(f"[PINNACLE INPLAY] could not re-point the board watchdog ({type(e).__name__}: {e}) — "
                  f"it will keep pulling the page back to the pre-match board", flush=True)
        if self._tab_manager is not None:
            self._tab_manager.hold(True)
        # on_lost releases the camp the moment the popover dies, so trimming resumes, the idle goes back
        # to browsing, and no stale "armed" state survives to be fired against.
        async def _lost():
            if getattr(self, "_camping", False):
                await self.camp_stop()

        self._inplay = InPlayActivity(page, self._human_click_loc,
                                      lambda m: print(f"[PINNACLE INPLAY] {m}", flush=True),
                                      on_lost=_lost)
        self._inplay.start()
        print("[PINNACLE INPLAY] session organic PAUSED and tab manager HELD — this one tab is the "
              "whole session now. Idle activity comes from the in-play loop instead.", flush=True)
        return {"ok": True, **self._inplay.status()}

    async def stop_inplay(self) -> dict:
        ip = getattr(self, "_inplay", None)
        if ip is None:
            return {"ok": True, "running": False}
        await ip.stop()
        self._inplay = None
        # Hand the browser back: session organic drives the primary page again, the board watchdog goes
        # back to enforcing the trading sport, tab manager resumes. Passing None restores the derived
        # default rather than the value we happened to capture, so a changed PINNACLE_HOME_URL wins.
        try:
            if self._browser is not None:
                self._browser.set_home_url(None)
                self._browser.resume_activity()
        except Exception:
            pass
        self._inplay_prev_home = None
        if self._tab_manager is not None:
            self._tab_manager.hold(False)
        return {"ok": True, "running": False}

    async def camp_inspect(self, wide: bool = False) -> dict:
        """Dump the ARMED Quick Bet exactly as it stands. Reads only; presses nothing.

        camp_fire has to press a popover that has been sitting for minutes, and `_place_via_ui` has only
        ever pressed one it typed into a second earlier. Four things about the idle state decide how fire
        must be written, and none of them are guessable:
          * is the Place control still ENABLED, or does an idle slip need re-confirming?
          * did the stake survive?
          * WHERE is the live price in this panel — fire must re-read it immediately before committing,
            and `_try_select_on` only ever read it at selection time.
          * what does the panel do when the price moves under an armed slip — re-price silently, disable
            Place, or show a changed-odds state?
        Run it right after arming and again a few minutes later; the diff answers all four.
        """
        page = self._primary_page()
        if page is None or page.is_closed():
            return {"ok": False, "error": "no primary page"}
        out: dict = {"ok": True, "camping": bool(getattr(self, "_camping", False)),
                     "url": (page.url or "")[:100]}
        try:
            portal = page.locator("#quick-bet-portal")
            if not await portal.count():
                return {**out, "ok": False, "error": "no Quick Bet on the page"}
            try:
                out["text"] = (await portal.first.inner_text()).replace("\n", " | ")[:600]
            except Exception:
                out["text"] = ""
            inputs = []
            il = portal.locator("input, textarea")
            for i in range(min(await il.count(), 10)):
                el = il.nth(i)
                try:
                    inputs.append({
                        "class": (await el.get_attribute("class") or "")[:44],
                        "name": await el.get_attribute("name"),
                        "type": await el.get_attribute("type"),
                        "value": await el.input_value(),
                        "disabled": await el.is_disabled(),
                        "visible": await el.is_visible()})
                except Exception:
                    continue
            out["inputs"] = inputs
            buttons = []
            bl = portal.locator("button")
            for i in range(min(await bl.count(), 12)):
                el = bl.nth(i)
                try:
                    buttons.append({
                        "text": ((await el.inner_text()) or "").replace("\n", " ")[:44],
                        "disabled": await el.is_disabled(),
                        "visible": await el.is_visible(),
                        "class": (await el.get_attribute("class") or "")[:36]})
                except Exception:
                    continue
            out["buttons"] = buttons
        except Exception as e:
            out.update({"ok": False, "error": f"{type(e).__name__}: {e}"})

        # WIDE: the odds-changed re-prompt. Pinnacle asks for re-confirmation when the price moves
        # against you between pressing and submitting, and that dialog may render OUTSIDE
        # #quick-bet-portal — a portal-scoped dump would miss it entirely and report a healthy slip.
        # Everything visible at document level, so the markup can be identified rather than guessed:
        # _place_via_ui already refuses this prompt precisely because its markup was never captured.
        if wide:
            try:
                dlg = []
                dl = page.locator('[role="dialog"], [aria-modal="true"], [class*="modal"], '
                                  '[class*="Modal"], [class*="dialog"], [class*="confirm"]')
                for i in range(min(await dl.count(), 6)):
                    el = dl.nth(i)
                    try:
                        if not await el.is_visible():
                            continue
                        dlg.append({"class": (await el.get_attribute("class") or "")[:60],
                                    "role": await el.get_attribute("role"),
                                    "text": ((await el.inner_text()) or "").replace("\n", " | ")[:400]})
                    except Exception:
                        continue
                out["dialogs"] = dlg
                pb = []
                bl2 = page.locator("button")
                for i in range(min(await bl2.count(), 40)):
                    el = bl2.nth(i)
                    try:
                        if not await el.is_visible():
                            continue
                        t = ((await el.inner_text()) or "").replace("\n", " ").strip()
                        if not t:
                            continue
                        pb.append({"text": t[:40], "disabled": await el.is_disabled(),
                                   "class": (await el.get_attribute("class") or "")[:44]})
                    except Exception:
                        continue
                out["page_buttons"] = pb
            except Exception as e:
                out["wide_error"] = f"{type(e).__name__}: {e}"
        return out

    # ── IN-PLAY CAMPING ───────────────────────────────────────────────────────
    async def camp_start(self, selection_id: str, stake: float) -> dict:
        """Park on a live game with the Quick Bet OPEN and the stake entered, then hold.

        WHY CAMP RATHER THAN SNIPE. Measured over 206 in-play windows on 2026-08-16: they came from just
        13 pairs, NOT ONE of which produced a single isolated arb, 94% of windows were a repeat on a pair
        already seen, and the median gap to the next window on the same pair was 41s (p25 10s, p90 185s).
        Camping 300s catches 96% of a pair's repeats. So the opportunity is concentrated and recurring,
        and the expensive part of execution — navigate, find the row, click, type, confirm — can be paid
        ONCE up front instead of inside every window.

        That is what made in-play unreachable before: the parallel model fires both legs at detection, and
        the Pinnacle leg's UI drive is seconds long while an in-play line moves under it. Pre-arming turns
        the in-play leg into a single press.

        Arms the DOMINANT side. Repeats on a pair switch sides (only 1 of 13 pairs stayed on one), but a
        dominant side runs 70-88%, and the minority case is cheap anyway: parked on the match page, the
        other cell is right there — a click, not a navigation.

        Nothing is placed. The slip sits armed until camp_fire, camp_stop, or the venue clears it.
        """
        blocked = self.manual_blocked("arming a camp")
        if blocked:
            return blocked
        if self._session_source == "browser" and not self._session_ready:
            return {"ok": False, "error": "no live Pinnacle session"}
        if self._bet_lock.locked():
            return {"ok": False, "error": "a bet is in flight"}
        # LET THE PREVIOUS CAMP FINISH LETTING GO. camp_fire defers its page cleanup (camp_stop's dismiss
        # + trim) so it does not sit in front of the hedge — which means at this point it may still be
        # clicking. Its last resort is a click on empty page, and that would clear the slip armed below.
        # Bounded: the cleanup is best-effort, so a slow one must not block re-arming forever.
        prev = getattr(self, "_camp_cleanup", None)
        if prev is not None and not prev.done():
            try:
                # Covers HARDVEN_RECEIPT_OBSERVE_SEC (10s) plus the dismissal escalation itself.
                await asyncio.wait_for(asyncio.shield(prev), timeout=20.0)
            except Exception:
                print("[PINNACLE CAMP] the previous camp's page cleanup is still running - arming anyway.",
                      flush=True)
        self._camp_cleanup = None
        # Set BEFORE driving the UI: _place_via_ui's own post-quote trim would otherwise clear the very
        # selection this is placing, and the guard lives in _trim_betslip.
        self._camping = True
        # HOW LONG DOES GETTING TO A MONEYLINE ACTUALLY COST? The camp exists to pay this once instead of
        # inside every window, and the case for dropping it entirely rests on this number being small. It
        # has never been measured — so measure it, and let the answer decide rather than an estimate.
        _t_arm = time.time()
        self._camp = {"selection_id": selection_id, "stake": stake, "since": time.time(),
                      "fires": 0, "armed": False}
        try:
            # keep_open: the verify-only path closes the popover on the way out (a rehearsal must leave
            # nothing behind). The camper needs precisely the opposite — the armed popover is the point.
            res = await self._place_via_ui(selection_id, stake, 1.01, submit=False, keep_open=True)
        except Exception as e:
            self._camping = False
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}
        # `verify-only` returns accepted=False by construction (it places nothing); what matters is
        # whether it got far enough to have entered a stake.
        armed = bool(res.actual_odds and res.actual_odds > 1.0)
        self._camp["armed"] = armed
        self._camp["odds_at_arm"] = res.actual_odds
        if not armed:
            self._camping = False
            return {"ok": False, "error": f"could not arm: {res.reason}"}
        # Idle behaviour must change WITH the camp, not alongside it. Browsing while armed would scroll
        # the board under the slip and open a different market's Quick Bet over the one being held.
        # getattr, not self._inplay: the attribute only exists once in-play mode has started, and a bare
        # access inside a swallowing try/except would turn "idle never switched to hover" into silence —
        # the camper would keep browsing and scrolling the board out from under its own armed slip.
        # Ask the session to postpone its re-mint reload: a hard reload unmounts the Quick Bet portal and
        # takes the armed slip with it (a soft SPA nav does not). Bounded inside the session.
        try:
            if self._browser is not None:
                self._browser.set_camp_hold(True)
        except Exception:
            pass
        # A NAVIGATION KILLS THE SLIP, SO IT MUST KILL THE CAMP — AT THE INSTANT IT HAPPENS.
        # The Quick Bet portal does not survive a reload, and every poller that could notice is on a timer:
        # the in-play watcher runs on its 12-45s idle gap and the C# health check every 30s. In between,
        # `_camping` still says armed, which suppresses betslip trimming and lets a fire press Place on
        # whatever the reloaded page happens to be showing. Observed while testing the hold: the page
        # refreshed and the bot went on believing the slip was up.
        #
        # `framenavigated` on the MAIN frame is the earliest possible signal and costs nothing when idle.
        # A soft SPA route change fires it too and does NOT destroy the portal, so the handler verifies
        # against the DOM instead of assuming — otherwise it would tear down healthy camps.
        # BIND THE PAGE ONCE, HERE. camp_start delegates the drive to _place_via_ui and never had a `page`
        # of its own, so both this watcher and the placeability probe referenced an undefined name. The
        # probe surfaced it as a 500; THIS one was inside a bare `except: pass` and simply never attached —
        # meaning the navigation invalidation built to stop a reload leaving a phantom armed camp has been
        # a no-op since it was written. A swallowed NameError is indistinguishable from a working feature.
        page = self._primary_page()
        try:
            if page is not None and not page.is_closed():
                page.on("framenavigated", self._on_camp_nav)
                self._camp_nav_page = page
            else:
                print("[PINNACLE CAMP] no primary page — the navigation watcher is NOT attached, so a "
                      "reload will not invalidate this camp.", flush=True)
        except Exception as ex:
            print(f"[PINNACLE CAMP] could not attach the navigation watcher ({type(ex).__name__}: {ex}) — "
                  f"a reload will not invalidate this camp.", flush=True)
        ip = getattr(self, "_inplay", None)
        if ip is not None:
            ip.set_camping(True)
        else:
            print("[PINNACLE CAMP] note: in-play idle is not running, so nothing was switched to hover. "
                  "Camping still works, but whatever idle IS running may scroll the slip away.",
                  flush=True)
        # IS THIS STAKE EVEN PLACEABLE? Pinnacle enforces a per-price minimum, and below it the panel blanks
        # "Max Bet" and DISABLES Place. The arm stake is only a placeholder — camp_fire re-types the ladder's
        # rung before pressing — but when the two are the same number (HARDVEN_STAKE_MAX pins every bet to one
        # rung, which is the supervised-test configuration) a camp can sit armed for ten minutes and be
        # incapable of firing the whole time. Reading it HERE turns that from a surprise at the first window
        # into a line at arm time, and costs one DOM read.
        # RETRY THE READ - the panel is still settling when _place_via_ui returns. A single immediate
        # probe found no PLACE BET control and reported `place=?` on every arm (2026-08-19), which is the
        # one answer this was added to get: whether the arm stake clears Pinnacle's per-price minimum.
        # Text-first with a class fallback, matching camp_fire, because in some panel states the class
        # changes and only the text is stable.
        # ⚠ EVERYTHING BELOW IS A DIAGNOSTIC AND MUST NEVER FAIL THE ARM. The slip is already open and
        # armed by this point — the expensive part is done. Letting a probe raise turns a SUCCESSFUL arm
        # into `POST /camp/start 500`, and the camper then re-arms in a loop, opening a real betslip every
        # time. Observed 2026-08-19: "VERIFY-ONLY OK … popover matched" immediately followed by a 500, over
        # and over. A measurement that can destroy the thing it measures is worse than no measurement.
        placeable, max_bet = None, None
        try:
            if page is None or page.is_closed():
                raise RuntimeError("no primary page to probe")
            portal_l = page.locator("#quick-bet-portal")
            for _attempt in range(6):                      # ~3s of settling, checked every 0.5s
                try:
                    txt = (await portal_l.first.inner_text()).replace("\n", " | ")
                    mb = self._CAMP_MAXBET_RX.search(txt)
                    if mb:
                        max_bet = float(mb.group(1).replace(",", ""))
                    pb = portal_l.get_by_text("PLACE BET", exact=False).last
                    if not await pb.count():
                        pb = portal_l.locator('button[class*="placeBet-"]').first
                    if await pb.count():
                        placeable = not await pb.is_disabled()
                        break
                except Exception:
                    pass
                await asyncio.sleep(0.5)
            if placeable is None:
                print("[PINNACLE CAMP] could not find a PLACE BET control on the armed slip after 3s - the "
                      "minimum-stake check is UNAVAILABLE for this camp (panel still rendering, or its "
                      "markup changed).", flush=True)
            # `self._camp` can be None by now: the framenavigated watcher attached above may have fired
            # and called camp_stop() during the 3s probe, which clears it. Guard rather than assume.
            if isinstance(getattr(self, "_camp", None), dict):
                self._camp["placeable_at_arm"] = placeable
                self._camp["max_bet"] = max_bet
            if placeable is False:
                print(f"[PINNACLE CAMP] *** {stake:.2f} IS BELOW PINNACLE'S MINIMUM at {res.actual_odds} *** "
                      f"(PLACE BET is disabled and Max Bet is blank). The camp will hold, but it can only "
                      f"fire if the ladder sends a LARGER stake at press time.", flush=True)
            mb_txt = f", max bet {max_bet:g}" if max_bet else ""
            place_txt = "enabled" if placeable else ("DISABLED" if placeable is False else "?")
            print(f"[PINNACLE CAMP] armed on {selection_id} stake={stake:.2f} @ {res.actual_odds} "
                  f"(place={place_txt}{mb_txt}) in {(time.time() - _t_arm) * 1000:.0f}ms "
                  f"— betslip trimming suspended, idle switched to hover", flush=True)
        except Exception as ex:
            print(f"[PINNACLE CAMP] armed on {selection_id} @ {res.actual_odds} — the placeability probe "
                  f"failed ({type(ex).__name__}: {ex}) but the CAMP IS FINE and stands.", flush=True)
        return {"ok": True, "armed": True, "selection_id": selection_id,
                "stake": stake, "odds": res.actual_odds,
                "placeable": placeable, "max_bet": max_bet}

    # The panel renders "... | {selection} | {price} | Max Bet: {limit} | Win | ...", and when the stake
    # is below the minimum the limit is blank ("Max Bet: | Win"). The price is the last decimal BEFORE
    # "Max Bet", which holds in both renderings — anchoring on the selection name would not, because the
    # name also appears in the matchup line above it.
    _CAMP_PRICE_RX = re.compile(r"(\d+\.\d+)\s*\|\s*Max Bet", re.I)
    _CAMP_MAXBET_RX = re.compile(r"Max Bet:\s*(?:EUR|€|\$|£)?\s*([\d,]+\.?\d*)", re.I)

    async def _decline_pause(self, why: str) -> None:
        """Sit on the odds-changed prompt for a beat before pressing DECLINE.

        ACCEPT AND DECLINE ARE NOT THE SAME KIND OF DECISION, so they should not have the same timing.
        Accepting is a race — the price is live and the panel re-quotes under you, so that click stays fast.
        Declining races nothing: the bet is not going to happen either way, and the only thing the timing
        can affect is how it looks. A prompt that says "the price moved, is that still OK?" answered in
        120ms, every single time, is a reaction no hand produces; it is the machine-regularity that gives a
        bot away, not the speed of any one click.

        So this reads the changed price the way a person would — a second or three — and the variance is the
        point, not the mean. PINNACLE_DECLINE_DELAY_MS as "min-max" (default "2000-3000").
        """
        spec = os.environ.get("PINNACLE_DECLINE_DELAY_MS", "2000-3000")
        try:
            lo, _, hi = spec.partition("-")
            lo_ms = float(lo)
            hi_ms = float(hi) if hi else lo_ms
        except Exception:
            lo_ms, hi_ms = 2000.0, 3000.0
        if hi_ms < lo_ms:
            lo_ms, hi_ms = hi_ms, lo_ms
        delay = random.uniform(lo_ms, hi_ms) / 1000.0
        print(f"[PINNACLE CAMP] odds-changed prompt ({why}) - reading it for {delay:.1f}s, then DECLINE",
              flush=True)
        await asyncio.sleep(delay)

    def _on_camp_nav(self, frame) -> None:
        """Main-frame navigation while camped → check whether the armed slip survived it.

        Sync callback (Playwright's event signature), so the DOM check is deferred to a task. Ignores
        sub-frames: ads and widgets navigate constantly and none of them can touch the Quick Bet."""
        try:
            if not getattr(self, "_camping", False):
                return
            pg = getattr(self, "_camp_nav_page", None)
            if pg is None or frame != pg.main_frame:
                return
            asyncio.create_task(self._camp_nav_check())
        except Exception:
            pass

    async def _camp_nav_check(self) -> None:
        await asyncio.sleep(0.8)                   # let the new document mount before judging it
        if not getattr(self, "_camping", False):
            return
        pg = getattr(self, "_camp_nav_page", None)
        if pg is None:
            return
        try:
            if await pg.locator("#quick-bet-portal").count():
                return                             # soft SPA nav: the portal is mounted at app root, it lives
        except Exception:
            pass
        print("[PINNACLE CAMP] the page navigated and the Quick Bet did not survive it - releasing the camp "
              "now rather than waiting for a poll to notice.", flush=True)
        try:
            await self.camp_stop()
        except Exception:
            pass

    async def _dismiss_quick_bet(self, page=None) -> bool:
        """Leave NOTHING loaded on the page. Returns True once the Quick Bet is gone (or was never there).

        THE DEFAULT WAY OUT OF EVERY SITUATION, not just a refused prompt. Whatever the panel is showing —
        an armed slip, a re-ask in either rendering, a placement confirmation, or something not seen before —
        dismissing it is safe and correct, so nothing needs to recognise the state first. That is the whole
        value: the failure this replaces was the bot sitting in front of a panel it could not classify.

        DISMISSING CANNOT CANCEL A PLACED BET. An accepted bet lives server-side and is established by its
        own POST /bets/straight 200 and the account's bet list; the popover is just a control surface. So
        this is safe to run after a SUCCESS too — and it should be, because a leftover confirmation panel is
        what the next arm has to click through.

        Ordered by how much it assumes about the DOM, least first:
          1. DECLINE, when the panel has one. The site's own affordance and the most honest signal to it.
          2. the popover's close control.
          3. Escape.
          4. a click on empty page, well away from the panel — the only step that assumes nothing about the
             markup at all. A click landing on nothing cannot place a bet whatever the slip is rendering,
             which is exactly the property wanted from a last resort.

        Trusts none of them on its own: each is verified against the portal actually disappearing, and a
        panel that survives all four is REPORTED rather than assumed away — a Quick Bet still sitting there
        with a stake in it is the one thing that must never be left behind silently.
        """
        if page is None:
            page = self._primary_page()
        try:
            if page is None or page.is_closed():
                return True
        except Exception:
            return True
        portal = page.locator("#quick-bet-portal")

        async def gone() -> bool:
            try:
                return not await portal.count()
            except Exception:
                return True                       # page/frame went away: nothing is left armed

        if await gone():
            return True

        try:
            dec = portal.get_by_text("DECLINE", exact=False)
            if await dec.count():
                await self._human_click_loc(page, dec.first)
                await asyncio.sleep(0.4)
                if await gone():
                    return True
        except Exception:
            pass
        for sel in ('button[aria-label*="Remove"]', 'button[aria-label*="Close"]',
                    'button[aria-label*="remove"]', 'button[aria-label*="close"]'):
            try:
                x = portal.locator(sel).first
                if await x.count():
                    await self._human_click_loc(page, x)
                    await asyncio.sleep(0.4)
                    if await gone():
                        return True
            except Exception:
                pass
        try:
            await page.keyboard.press("Escape")
            await asyncio.sleep(0.4)
            if await gone():
                return True
        except Exception:
            pass
        # Click away. Top-left of the viewport, offset off the very corner so it is a plausible resting
        # place for a hand rather than a screen edge, and clear of the panel (which renders right/centre).
        try:
            ax, ay = random.uniform(60, 150), random.uniform(180, 320)
            await self._human_move_page(page, ax, ay)
            await page.mouse.click(ax, ay)
            await asyncio.sleep(0.5)
            if await gone():
                return True
        except Exception:
            pass
        print("[PINNACLE CAMP] WARNING: could not dismiss the Quick Bet - it is STILL ON THE PAGE with a "
              "stake in it. Nothing was placed by this call, but clear it by hand before re-arming.",
              flush=True)
        return False

    async def camp_fire(self, min_odds: float, stake: float = None) -> dict:
        """Press PLACE BET on the armed slip. THE ONLY FUNCTION HERE THAT COMMITS MONEY.

        The camp exists so this is one press instead of navigate-find-click-type-confirm. What it must
        still do, because an armed slip is a live thing:

        RE-READ THE PRICE. Measured 2026-08-16: the panel re-quotes continuously and shows
        "Odds changed: | old | new" while Place STAYS ENABLED — so pressing accepts whatever is current,
        not what was armed. `odds_at_arm` is worthless by then (1.155 at arm, 1.335 twenty minutes on).
        `min_odds` is the caller's FLOOR: the price the arb was sized against. Below it, refuse.

        CHECK THE BUTTON. Below Pinnacle's per-price minimum the panel blanks "Max Bet" and DISABLES
        Place — that is what a €5 stake at 1.19 looks like. Clicking a disabled button silently does
        nothing, so this reports the reason instead of returning a false success.

        CONFIRM. The POST answers PENDING_ACCEPTANCE with no bet id; acceptance is established against
        the account's own bet list. See _confirm_bet.
        """
        blocked = self.manual_blocked("firing a camp")
        if blocked:
            return blocked
        if not getattr(self, "_camping", False):
            return {"ok": False, "error": "not camping"}
        if not self._bet_enabled:
            return {"ok": False, "error": "HARDVEN_BET_ENABLE is not set — refusing to place"}
        page = self._primary_page()
        if page is None or page.is_closed():
            return {"ok": False, "error": "no page"}
        portal = page.locator("#quick-bet-portal")
        try:
            if not await portal.count():
                await self.camp_stop()
                return {"ok": False, "error": "the armed slip is gone — nothing to fire"}
            text = (await portal.first.inner_text()).replace("\n", " | ")
        except Exception as e:
            return {"ok": False, "error": f"could not read the slip: {type(e).__name__}: {e}"}

        m = self._CAMP_PRICE_RX.search(text)
        if not m:
            return {"ok": False, "error": "could not read the live price off the slip",
                    "text": text[:240]}
        live = float(m.group(1))
        mb = self._CAMP_MAXBET_RX.search(text)
        max_bet = float(mb.group(1).replace(",", "")) if mb else None

        # THE FLOOR. Higher decimal odds are better for a backer, so anything at or above the price the
        # arb was sized on is fine; below it the edge the trade was justified by no longer exists.
        if live < min_odds - 1e-9:
            return {"ok": False, "fired": False, "live_odds": live, "min_odds": min_odds,
                    "error": f"slip now shows {live}, below the {min_odds} the arb needs — not firing"}

        c = getattr(self, "_camp", {}) or {}
        want_stake = float(stake if stake is not None else c.get("stake") or 0)
        try:
            inp = portal.locator("input[type=text]").first
            cur = (await inp.input_value() or "").strip()
            if want_stake and abs(float(cur or 0) - want_stake) > 1e-6:
                await self._human_click_loc(page, inp, fast=True)
                await inp.fill(f"{want_stake:g}")
                await asyncio.sleep(0.35)          # the panel re-computes Max Bet / enables Place
                text = (await portal.first.inner_text()).replace("\n", " | ")
        except Exception as e:
            return {"ok": False, "error": f"could not set the stake: {type(e).__name__}: {e}"}

        # TEXT FIRST, class second. Captured 2026-08-16: in the odds-changed prompt the button's class
        # changes from `placeBet-…` to `button-…`, and DECLINE carries the IDENTICAL class — so a
        # class-based selector both misses the accept state and cannot tell accept from decline in it.
        # The text is the only thing stable across both renderings.
        place = portal.get_by_text("PLACE BET", exact=False).last
        try:
            if not await place.count():
                place = portal.locator('button[class*="placeBet-"]').first
            if await place.is_disabled():
                return {"ok": False, "fired": False, "live_odds": live, "max_bet": max_bet,
                        "error": f"PLACE BET is disabled at stake {want_stake:g} @ {live} — almost "
                                 f"certainly below Pinnacle's minimum for this price (the panel blanks "
                                 f"'Max Bet' when that happens). Raise the stake or skip this window."}
        except Exception as e:
            return {"ok": False, "error": f"could not evaluate PLACE BET: {type(e).__name__}: {e}"}

        # ── RE-READ THE PRICE IMMEDIATELY BEFORE PRESSING ────────────────────────────────────────────
        # THE SLIP PRICE IS THE ONE THE VENUE HONOURS, so it must be the one the floor is applied to — and
        # until 2026-08-19 it was not. `live` was parsed ONCE at the top, then the stake was typed (0.35s),
        # the panel re-read into `text` WITHOUT re-parsing the price, the Place control was resolved and
        # its disabled state queried. Several hundred milliseconds of an in-play tennis line, and the panel
        # re-quotes continuously (measured: 1.155 at arm -> 1.335 twenty minutes on, Place enabled
        # throughout). So the floor was being checked against a price the slip had already replaced.
        #
        # That is what booked a fill at 1.581 against a 1.745 floor with no odds-changed prompt: there was
        # never a disagreement for the venue to prompt about. The slip said 1.581, we pressed 1.581, and
        # only our own stale variable said otherwise.
        try:
            text = (await portal.first.inner_text()).replace("\n", " | ")
            m3 = self._CAMP_PRICE_RX.search(text)
            if not m3:
                return {"ok": False, "fired": False,
                        "error": "the slip price became unreadable just before the press — not firing"}
            fresh = float(m3.group(1))
        except Exception as e:
            return {"ok": False, "fired": False,
                    "error": f"could not re-read the slip before pressing: {type(e).__name__}: {e}"}
        if fresh != live:
            print(f"[PINNACLE CAMP] slip moved {live} -> {fresh} between the first read and the press "
                  f"(floor {min_odds})", flush=True)
        if fresh < min_odds - 1e-9:
            return {"ok": False, "fired": False, "live_odds": fresh, "min_odds": min_odds,
                    "first_read": live,
                    "error": f"slip shows {fresh} at press time, below the {min_odds} the arb needs — "
                             f"not firing (it read {live} a moment earlier)"}
        live = fresh

        # THE PRICE THE FLOOR WAS CHECKED AGAINST. Recorded separately from the CONFIRMED price so the two
        # can be compared afterwards. On 2026-08-19 a camp fired with min_odds 1.745, and the account's bet
        # list came back at 1.581 - a fill 9% below the floor that every local check had passed. Either the
        # panel moved between the read and the submit, or the venue prices an in-play bet when it CLEARS
        # rather than when it is pressed. Those need different fixes, and only this pair of numbers tells
        # them apart.
        checked_odds = live
        # ── COMMITTING ───────────────────────────────────────────────────────────────────────────────
        placed: dict = {}
        bets_seen: list = []
        # WAKE ON THE RESPONSE, DO NOT POLL FOR IT. Both waits below ran on a flat 0.25s sleep, so a POST
        # that landed 1ms after a tick sat unread for the rest of it - dead time standing in front of an
        # irreversible leg's hedge, on the placement and again on the confirm. The listener already fires
        # the instant the response arrives; these just let it say so. The 0.25s cadence is KEPT as the
        # timeout, because the re-ask check still has to read the portal and there is no event for
        # "the panel re-rendered".
        placed_evt = asyncio.Event()
        bets_evt = asyncio.Event()

        def _on_resp(resp):
            try:
                if "/bets/straight" in resp.url and resp.url.rstrip("/").endswith("straight") \
                        and resp.request.method == "POST":
                    placed["status"] = resp.status
                    placed["_resp"] = resp
                    placed_evt.set()
                elif "/0.1/bets" in resp.url and resp.request.method == "GET":
                    bets_seen.append(resp)
                    del bets_seen[:-12]
                    bets_evt.set()
            except Exception:
                pass

        page.on("response", _on_resp)
        t0 = time.time()
        # Runs CONCURRENTLY with the network wait and gates nothing — see _watch_receipt_dom. If the UI
        # shows a receipt seconds before /bets/straight answers, that is the signal we should be using.
        # STARTED AFTER THE CLICK, NOT BEFORE. Playwright serialises commands over one CDP session, so a
        # watcher sampling the DOM every 200ms is competing with the approach and the press itself for the
        # same pipe - latency bought for a measurement that gates nothing. Its baseline is taken a few ms
        # after the click instead, which is still "before the receipt" by a whole server round trip, and it
        # times from the press rather than from the read that preceded it.
        _receipt = None
        receipt: dict = {}          # filled LIVE by the watcher; readable without awaiting the task
        try:
            # BEFORE THE PRESS, not after. The frame worth having is the panel the venue was SHOWN — a shot
            # taken once the click has landed can already be the confirmation, the re-ask, or an error, and
            # on 2026-08-20 the open question was exactly "what did the panel look like when we pressed a
            # line that came back HTTP 400". Started as a task so the shot itself never sits on the press
            # path: it captures within milliseconds of the click without delaying it.
            _shot = asyncio.create_task(
                self.snap(page, "fire-press", f"about to press {live} on {c.get('selection_id')}"))
            if not await self._human_click_loc(page, place, fast=True):
                return {"ok": False, "fired": False, "error": "could not click PLACE BET"}
            # WHERE press->POST ACTUALLY GOES. That number was 4798ms on bet 2258987331 with nothing to say
            # how much was our approach-and-click and how much was Pinnacle answering. Only one of those is
            # worth paying for - the human click is the point; everything else is overhead to hunt down.
            t_click = time.time()
            _receipt = asyncio.create_task(self._watch_receipt_dom(
                page, t_click, out=receipt,
                exclude={p for p in str(c.get("selection_id") or "").split(":") if p.isdigit()}))
            # ── THE ODDS-CHANGED RE-PROMPT ───────────────────────────────────────────────────────────
            # When the price moves against you between press and submit, Pinnacle does NOT place — it
            # re-renders the panel with DECLINE alongside PLACE BET and waits. No dialog, no new element
            # to find: it happens inside the same portal (verified — `dialogs` came back empty).
            # Left unhandled the press simply never becomes a bet, and the old code sat out its 15s and
            # reported "the bet MAY be live" for something that definitively was not.
            # THE DECISION IS THE SAME ONE AS BEFORE PRESSING: the prompt shows the NEW price in the same
            # position, so re-read it and apply the same floor. At or above it the arb still stands and
            # this accepts; below it the edge is gone and DECLINE is the correct answer, not a retry.
            # THERE ARE TWO RE-ASK RENDERINGS, not one. Captured 2026-08-17 on a fast-moving live tennis
            # line, with the account's own "accept odds changes" setting ON:
            #
            #   A  "… | DECLINE | PLACE BET | …"                  two buttons, an explicit refuse
            #   B  "Odds changed: | 4.000 | 3.720 | ▲ | … | PLACE BET | …"
            #                                                      a BANNER and ONE button — re-press to take
            #
            # Only A was handled, so B fell through the whole 15s wait doing nothing and reported "the bet MAY
            # be live" for a panel that was simply sitting there waiting to be pressed again. Worse than
            # useless: that message means "you may hold an unhedged leg", which halts the bot.
            #
            # ABANDONING IS A CLICK-AWAY, NOT A HUNT FOR A BUTTON. B has no DECLINE at all, and A's DECLINE
            # shares a class with PLACE BET so only its text distinguishes it — text that has now been seen in
            # two forms and could take a third. Dismissing the popover cannot place anything no matter what
            # the panel is rendering, which makes it the safe universal answer; the named control is tried
            # first only because it is the site's own affordance.
            body: dict = {}
            accepts = 0
            max_accepts = int(os.environ.get("PINNACLE_CAMP_MAX_REPRESS", "2"))
            for _ in range(60):
                # Returns the moment /bets/straight answers; falls through on the 0.25s tick to check the
                # portal for a re-ask, which has no event of its own.
                try:
                    await asyncio.wait_for(placed_evt.wait(), timeout=0.25)
                except asyncio.TimeoutError:
                    pass
                if "_resp" in placed:
                    try:
                        body = await placed["_resp"].json()
                    except Exception:
                        body = {}
                    break
                t2 = ""
                try:
                    t2 = (await portal.first.inner_text()).replace("\n", " | ")
                except Exception:
                    continue                       # portal gone mid-read; the checks below need its text
                has_decline = "DECLINE" in t2.upper()
                changed = "ODDS CHANGED" in t2.upper()
                if not has_decline and not changed:
                    continue                       # ordinary in-flight wait
                m2 = self._CAMP_PRICE_RX.search(t2)
                newp = float(m2.group(1)) if m2 else None
                state = "A (decline+place)" if has_decline else "B (odds-changed banner)"

                if newp is None:
                    await self._decline_pause(f"{state}, price unreadable")
                    await self._dismiss_quick_bet(page)
                    return {"ok": False, "fired": False, "declined": True,
                            "error": f"re-ask appeared [{state}] and its price was unreadable — abandoned "
                                     f"rather than accept an unknown price"}
                if newp < min_odds - 1e-9:
                    await self._decline_pause(f"{live} -> {newp} is under the {min_odds} floor")
                    await self._dismiss_quick_bet(page)
                    print(f"[PINNACLE CAMP] odds moved {live} -> {newp}, below the {min_odds} floor "
                          f"[{state}] - abandoned, no bet placed", flush=True)
                    return {"ok": False, "fired": False, "declined": True,
                            "live_odds": newp, "min_odds": min_odds,
                            "error": f"odds changed to {newp}, below the {min_odds} the arb needs — "
                                     f"abandoned, nothing placed"}

                # Still clears the floor. Press again to take the new price — but BOUNDED. On a line moving
                # this fast the site can re-ask on every press, and an unbounded accept loop is a machine
                # chasing a price, which is both a detectable pattern and a way to fill at something well
                # away from what the arb was sized on.
                if accepts >= max_accepts:
                    await self._dismiss_quick_bet(page)
                    print(f"[PINNACLE CAMP] re-asked {accepts + 1}x [{state}] - giving up rather than "
                          f"chasing the price", flush=True)
                    return {"ok": False, "fired": False, "declined": True,
                            "live_odds": newp, "min_odds": min_odds,
                            "error": f"the panel re-asked {accepts + 1} times (last {newp}) — abandoned "
                                     f"rather than chase a moving price; nothing placed"}
                accepts += 1
                print(f"[PINNACLE CAMP] odds moved {live} -> {newp}, still clears {min_odds} [{state}] - "
                      f"accepting (press {accepts}/{max_accepts})", flush=True)
                live = newp
                try:
                    await self._human_click_loc(page, portal.get_by_text("PLACE BET", exact=False).last,
                                                fast=True)
                except Exception as e:
                    await self._dismiss_quick_bet(page)
                    return {"ok": False, "fired": False, "declined": True,
                            "error": f"could not re-press PLACE BET ({type(e).__name__}: {e}) — abandoned"}
            t_post = time.time()
            if "_resp" not in placed:
                # UNCLASSIFIED. 15s of a panel that never produced a POST and never showed a re-ask we
                # recognise. Clear it rather than walk away and leave a slip with a stake in it — that costs
                # nothing if no bet went out, and cannot un-place one if it did.
                #
                # The ambiguity itself does NOT go away, and must not be softened: no POST was seen, so this
                # still reports unconfirmed and the C# side still hard-halts. Clearing the panel is hygiene,
                # not evidence.
                cleared = await self._dismiss_quick_bet(page)
                return {"ok": False, "fired": True, "confirmed": False, "slip_cleared": cleared,
                        "error": "PLACE BET clicked but no /bets/straight response in 15s — the bet MAY "
                                 "be live. Reconcile against My Bets; do NOT hedge against this."
                                 + ("" if cleared else " The Quick Bet also would not close — clear it by hand.")}
            if placed.get("status") != 200:
                # A REFUSAL WITH NO EXPLANATION. Observed 2026-08-20 on Lorenzo Giustino: HTTP 400 from
                # /bets/straight, nothing placed, and no way afterwards to see what the panel objected to.
                # Capture the page AND the body — the response almost certainly says why.
                body_txt = ""
                try:
                    body_txt = (await placed["_resp"].text())[:400]
                except Exception:
                    pass
                await self.snap(page, f"fire-refused-{placed.get('status')}", "venue refused the placement")
                print(f"[PINNACLE CAMP] placement REFUSED (HTTP {placed.get('status')}) — body: "
                      f"{body_txt or '(unreadable)'}", flush=True)
                return {"ok": False, "fired": True, "confirmed": False, "venue_body": body_txt,
                        "error": f"Pinnacle refused the placement (HTTP {placed.get('status')})"
                                 + (f": {body_txt[:160]}" if body_txt else "")}

            # SPLIT THE WAIT. Two different things are being timed and they have different fixes:
            #   press -> POST      the venue accepting the click (their latency)
            #   POST  -> confirmed our polling of the account bet list (OUR latency)
            # The POST answers PENDING_ACCEPTANCE with no bet id, so the second phase is entirely ours -
            # if it dominates, 'the book leg is slow' is a statement about this code, not about Pinnacle.
            post_ms = round((t_post - t0) * 1000, 1)
            click_ms = round((t_click - t0) * 1000, 1)
            confirm_source = "post-body"          # overwritten by whichever route actually establishes it
            req_id = str(body.get("requestId") or body.get("requestID") or "") or None
            bid = str(body.get("betId") or body.get("id") or "") or None
            if not bid and req_id:
                conf = await self._confirm_bet(req_id, bets_seen, evt=bets_evt)
                if conf.get("accepted"):
                    confirm_source = "bets-list"
                if conf.get("rejected"):
                    return {"ok": False, "fired": True, "confirmed": True, "accepted": False,
                            "error": f"Pinnacle REJECTED the bet ({conf.get('status')}) — nothing is on"}
                if not conf.get("accepted"):
                    # THE DOM IS THE BACKSTOP. The bets-list route depends on the page choosing to poll
                    # GET /0.1/bets, and we could not establish from the logs that it always does - so a
                    # timeout there is not evidence of anything, and burning a pressed bet on it would be
                    # the worst reading available.
                    #
                    # ONLY A BET ID COUNTS. The watcher refuses to treat "the panel changed" as acceptance,
                    # because errors, re-asks and a cleared slip all change it too; an id is the venue's own
                    # identifier for a bet it has taken. The PRICE is not recoverable this way, so `live`
                    # stays the panel price the floor was checked against, and the below-floor check below
                    # still runs against it.
                    # OFF BY DEFAULT UNTIL THE RECEIPT HAS BEEN SEEN. Everything about how this reads a
                    # receipt is an ASSUMPTION: that the settled panel contains a bet id, that a Pinnacle
                    # bet id renders as a bare 9-12 digit run, that nothing else in that panel does. Not one
                    # of those has been observed — every fire so far has been cancelled while the panel
                    # still said "Processing Live Bet...", so the settled state has never been captured.
                    #
                    # A wrong reading here is the expensive direction: it reports a bet as confirmed and the
                    # hedge goes out against something that may not exist. So the watcher OBSERVES and logs
                    # on every fire, and only counts as confirmation once HARDVEN_DOM_CONFIRM=1 — which
                    # should be set after a real receipt has been read in the log, not before.
                    dom_ok = os.environ.get("HARDVEN_DOM_CONFIRM") == "1"
                    dom_bid = receipt.get("bet_id") if dom_ok else None
                    if receipt.get("bet_id") and not dom_ok:
                        print(f"[PINNACLE RECEIPT] the panel shows a candidate bet id "
                              f"{receipt['bet_id']} — NOT used as confirmation (HARDVEN_DOM_CONFIRM is not "
                              f"set). Check it against My Bets; if it is the real id, the fallback is ready "
                              f"to enable.", flush=True)
                    # NOT THE LAST FIRE'S RECEIPT. _dismiss_quick_bet can fail (observed 2026-08-19: all
                    # four escalation steps, panel still up), so the next press can start against a panel
                    # that already carries an old id. The watcher only reads ids out of text that CHANGED
                    # from this press's baseline, which covers most of it; refusing an id we have already
                    # returned closes the rest.
                    if dom_bid and dom_bid == getattr(self, "_last_bet_id", None):
                        print(f"[PINNACLE CAMP] the panel shows bet {dom_bid}, but that is the id from the "
                              f"PREVIOUS fire - refusing to read a stale receipt as this press's "
                              f"confirmation.", flush=True)
                        dom_bid = None
                    if dom_bid:
                        print(f"[PINNACLE CAMP] the bet list did not answer in "
                              f"{conf.get('waited_s', 0):.0f}s, but the panel is showing bet {dom_bid} "
                              f"({receipt.get('bet_id_ms', 0):.0f}ms after the press) - CONFIRMED FROM THE "
                              f"DOM. Price is the panel's {live}, not a venue-reported fill; reconcile it.",
                              flush=True)
                        bid, confirm_source = dom_bid, "dom-receipt"
                    else:
                        return {"ok": False, "fired": True, "confirmed": False,
                                "error": f"unconfirmed after {conf.get('waited_s', 0):.0f}s (requestId "
                                         f"{req_id}) — state UNKNOWN, do not hedge against this. The "
                                         f"panel showed no bet id either"
                                         + (f" (it did change at {receipt['first_change_ms']:.0f}ms: "
                                            f"{(receipt.get('text') or '')[:160]})"
                                            if receipt.get("first_change_ms") else " (and never changed)"),
                                "receipt": receipt}
                else:
                    bid, live = conf.get("bet_id") or bid, float(conf.get("price") or live)
            c["fires"] = c.get("fires", 0) + 1
            self._last_bet_id = bid or getattr(self, "_last_bet_id", None)
            below = live < min_odds - 1e-9
            _tot = round((time.time() - t0) * 1000, 1); _conf = round(_tot - post_ms, 1)
            print(f"[PINNACLE CAMP] timing: click {click_ms:.0f}ms (ours) + venue {post_ms - click_ms:.0f}ms "
                  f"= press->POST {post_ms:.0f}ms, POST->confirmed {_conf:.0f}ms, total {_tot:.0f}ms"
                  + ("   <- OUR confirmation polling is the bottleneck" if _conf > post_ms else ""),
                  flush=True)
            print(f"[PINNACLE CAMP] FIRED {want_stake:g} @ {live} on {c.get('selection_id')} "
                  f"(bet {bid}) in {time.time() - t0:.1f}s "
                  f"| floor {min_odds} · panel-at-check {checked_odds} · accepted {live}", flush=True)
            if below:
                print(f"[PINNACLE CAMP] *** ACCEPTED BELOW THE FLOOR *** pressed against a panel showing "
                      f"{checked_odds} with a floor of {min_odds}, and the account booked it at {live}. "
                      f"The pre-press check CANNOT protect this fill - the venue priced it after the click. "
                      f"Treat the local floor as advisory on in-play until this is understood.", flush=True)
            return {"ok": True, "fired": True, "confirmed": True, "accepted": True,
                    "bet_id": bid, "odds": live, "stake": want_stake,
                    "checked_odds": checked_odds, "min_odds": min_odds, "below_floor": below,
                    "confirm_source": confirm_source,
                    "receipt_first_change_ms": receipt.get("first_change_ms"),
                    "receipt_bet_id_ms": receipt.get("bet_id_ms"),
                    "post_ms": post_ms, "confirm_ms": round((time.time()-t0)*1000 - post_ms, 1),
                    "total_ms": round((time.time()-t0)*1000, 1),
                    "elapsed_s": round(time.time() - t0, 2)}
        finally:
            # NOT CANCELLED ANY MORE. The receipt we actually need lands AFTER "Processing Live Bet..."
            # clears, which is after this returns - cancelling here is why every fire so far has only ever
            # shown us the processing state. Its own budget bounds it, and it gates nothing.
            _ = _receipt
            try:
                page.remove_listener("response", _on_resp)
            except Exception:
                pass
            # The slip is consumed by a placement. Release so nothing stale is fired again.
            #
            # NOT ON THE RESPONSE PATH. camp_stop's DOM half (dismiss the Quick Bet, trim the betslip) is
            # four escalating click attempts with ~1.7s of sleeps and human-paced moves between them, and
            # every one of those milliseconds used to land BEFORE this handler answered — which is before
            # the C# side sends the Kalshi hedge. Measured 2026-08-19 on bet 2258987331: the sidecar had
            # the bet confirmed at 11.7s, the executor saw the leg complete at 17.4s. The 5.7s gap was
            # cleanup, held in front of the irreversible leg's hedge, and on that fire the dismissal failed
            # all four steps so it cost the full budget.
            #
            # Clearing a panel cannot cancel a placed bet and nothing downstream reads it, so it has no
            # business gating the hedge. The state release inside camp_stop is instant and still synchronous
            # (see background_dom) — only the page work is deferred, and camp_start waits on it before it
            # arms so a late dismissal can never click away a freshly-armed slip.
            try:
                await self.camp_stop(background_dom=True)
            except Exception:
                pass

    async def camp_status(self) -> dict:
        """State of the camp, VERIFIED against the page — not the flag written when it was armed.

        The first version returned the dict stored by camp_start, so `armed: True` meant "we armed it N
        seconds ago" and nothing more. Observed 2026-08-16: the page navigated to /matchups/, which
        destroys the Quick Bet, and the status kept reporting armed for as long as it was asked. That is
        the worst possible failure on a money path — camp_fire would press Place on a page with no slip,
        and the operator would have been told the camp was healthy the whole time.

        So this READS the popover. If it is gone the camp is over, and saying so is the point.
        """
        c = getattr(self, "_camp", None)
        if not c or not getattr(self, "_camping", False):
            return {"camping": False}
        out = {"camping": True, **c, "held_sec": round(time.time() - c["since"], 1)}
        page = self._primary_page()
        live = False
        url = ""
        try:
            if page is not None and not page.is_closed():
                url = page.url or ""
                live = bool(await page.locator("#quick-bet-portal").count())
        except Exception as e:
            out["check_error"] = f"{type(e).__name__}: {e}"
        out["armed"] = live                      # OVERWRITES the remembered value on purpose
        out["url"] = url[:90]
        if not live:
            out["lost"] = ("the Quick Bet is no longer on the page — "
                           + ("the tab navigated away" if "/live/" not in url
                              else "it was closed or expired in place"))
            return out

        # PRESENT IS NOT THE SAME AS TRADEABLE. A market can go offline — suspended, taken down between
        # points, or pulled when the game state changes — and the popover STAYS on the page showing a dead
        # panel. Observed 2026-08-18: a camp held Figl vs Castagnola for its full 25-minute cap with the
        # moneyline offline, reporting healthy the whole time, because the only health signal was "does
        # #quick-bet-portal exist".
        #
        # So report what the panel can actually do: is there a readable PRICE, and is PLACE BET pressable.
        # Both are reported RAW, per sample — the caller decides how many consecutive bad reads mean dead,
        # because in-play tennis suspends between points constantly and a single sample proves nothing.
        try:
            portal = page.locator("#quick-bet-portal")
            txt = (await portal.first.inner_text()).replace("\n", " | ")
            m = self._CAMP_PRICE_RX.search(txt)
            out["price"] = float(m.group(1)) if m else None
            if out["price"] is None:
                # AN UNREADABLE PRICE IS TWO DIFFERENT PROBLEMS and they need different answers:
                # the market is suspended (normal in-play, wait), or the panel layout changed and the
                # regex no longer matches (a bug that silently disables the floor check on the money
                # path). Only the actual panel text separates them, and it is never needed EXCEPT here —
                # so it is attached only on failure rather than shipped on every poll.
                out["text_head"] = txt[:160]
            mb = self._CAMP_MAXBET_RX.search(txt)
            out["max_bet"] = float(mb.group(1).replace(",", "")) if mb else None
            pb = portal.get_by_text("PLACE BET", exact=False).last
            out["placeable"] = (not await pb.is_disabled()) if await pb.count() else False
            out["tradeable"] = bool(out["price"]) and bool(out["placeable"])

            # DOES THE ARMED SLIP ACTUALLY RE-QUOTE, AND HOW OFTEN? The camp's whole premise is that an
            # open Quick Bet tracks the market, so the next window costs one press. If it instead freezes
            # at the price it was armed at, every press commits to a stale number and no amount of
            # re-reading helps — the design would have to become hover-then-click.
            #
            # Tracked HERE because this is the only thing that looks at the panel on a schedule. A price
            # that has not changed for minutes on a live in-play line is the signal; the counter separates
            # "never updates" from "updates but slowly".
            c2 = getattr(self, "_camp", None) or {}
            now2 = time.time()
            prev = c2.get("last_price")
            if out["price"] and out["price"] != prev:
                c2["last_price"] = out["price"]
                c2["last_price_at"] = now2
                c2["price_updates"] = int(c2.get("price_updates", 0)) + 1
            c2.setdefault("last_price_at", now2)
            out["price_updates"] = int(c2.get("price_updates", 0))
            out["price_static_ms"] = int((now2 - c2["last_price_at"]) * 1000)
            out["odds_at_arm"] = c2.get("odds_at_arm")
            if not out["tradeable"]:
                out["why"] = ("no price on the panel" if not out["price"]
                              else "PLACE BET is disabled (suspended, or the stake is under the minimum)")
                # TELL DETECTION, not just the camper. The C# side releases the camp after 3 bad checks, but
                # nothing was clearing the BOOK — so the WS kept pushing a price, the executor kept finding
                # arbs on a locked line, and a press against it came back HTTP 400 (2026-08-20). Suspending
                # the token here is what stops the next arb from being detected at all.
                #
                # ONE sample is enough for the BOOK even though three are needed to move the CAMP: a
                # suspension is undone by the next genuine push, so the cost of being early is a book that
                # re-opens a second later, while the cost of being late is a press into a market that will
                # refuse it.
                sid = (getattr(self, "_camp", None) or {}).get("selection_id")
                if sid:
                    self.suspend_token(sid, f"panel says not tradeable ({out['why']})")
                    if not getattr(self, "_lock_dumped", False):
                        self._lock_dumped = True
                        print(f"[PINNACLE LOCK] first locked panel seen for {sid} ({out['why']}). "
                              f"Panel text: {txt[:300]}", flush=True)
                        await self._dump_panel_controls(page, "#quick-bet-portal")
                        await self.snap(page, f"locked-{sid.replace(':','-')}", out["why"])
        except Exception as e:
            out["check_error"] = f"{type(e).__name__}: {e}"
            out["tradeable"] = None              # unknown, NOT dead — never kill a camp on a read error
        return out

    async def camp_stop(self, background_dom: bool = False) -> dict:
        """Release the camp and clear the armed selection, so nothing is left loaded.

        `background_dom=True` splits this in two: the STATE release stays synchronous (instant, and the
        thing every caller actually depends on), while the page work — dismiss the Quick Bet, trim the
        betslip — runs as a task. Used by camp_fire, where the cleanup was sitting between an irreversible
        book leg and its hedge; see the note at that call site. camp_start awaits the pending task before
        arming, so a deferred dismissal can never reach a slip that has since been re-armed.
        """
        if not getattr(self, "_camping", False):
            return {"ok": True, "camping": False}
        self._camping = False                      # release FIRST so the trim below is allowed to run
        # Drop the navigation watcher with the camp. Left attached it accumulates a handler per arm on a
        # long-lived page, and each one would fire on every later navigation.
        try:
            pg = getattr(self, "_camp_nav_page", None)
            if pg is not None:
                pg.remove_listener("framenavigated", self._on_camp_nav)
        except Exception:
            pass
        self._camp_nav_page = None
        try:
            if self._browser is not None:
                self._browser.set_camp_hold(False)      # the session may re-mint again
        except Exception:
            pass
        ip = getattr(self, "_inplay", None)
        if ip is not None:
            ip.set_camping(False)
        page = self._primary_page()
        # DISMISS THE QUICK BET FIRST, and unconditionally.
        #
        # This is the failsafe every exit path shares. camp_stop runs on success, on a decline, on a timeout
        # we could not classify, on relocation, on idle release and on shutdown — so doing it here means no
        # caller has to remember, and no unrecognised panel state can leave a loaded slip behind. The old
        # code only ran `_trim_betslip`, which clears the SIDE betslip and does not touch the Quick Bet
        # portal at all, so the popover routinely survived a "stop".
        #
        # It runs after a SUCCESSFUL placement too. Dismissing cannot cancel a bet that was accepted (that
        # lives server-side), and the panel left behind is a confirmation the next arm would otherwise have
        # to click through — so clearing it is how the camper gets a clean page to re-arm on.
        async def _clear_page() -> None:
            try:
                if page is not None and not await self._dismiss_quick_bet(page):
                    print("[PINNACLE CAMP] camp released but the Quick Bet would not close - clear it by "
                          "hand before re-arming.", flush=True)
            except Exception:
                pass
            try:
                if page is not None:
                    await self._trim_betslip(page, source="camp-stop")
            except Exception:
                pass

        if background_dom:
            # LET THE RECEIPT EXIST FIRST. The settled receipt is the panel we still have to learn to close,
            # and dismissing at ~0ms after the fire destroys it before the watcher can read it or enumerate
            # its controls. Deferred by HARDVEN_RECEIPT_OBSERVE_SEC (default 10s) - which costs nothing on
            # the money path, because this whole branch is already off it.
            async def _observe_then_clear():
                try:
                    await asyncio.sleep(float(os.environ.get("HARDVEN_RECEIPT_OBSERVE_SEC", "10")))
                except Exception:
                    pass
                await _clear_page()
            self._camp_cleanup = asyncio.create_task(_observe_then_clear())
        else:
            await _clear_page()
        c = getattr(self, "_camp", {}) or {}
        print(f"[PINNACLE CAMP] stopped after {time.time() - c.get('since', time.time()):.0f}s, "
              f"{c.get('fires', 0)} fire(s)", flush=True)
        self._camp = None
        return {"ok": True, "camping": False}

    # ── UI placement helpers ──────────────────────────────────────────────────
    async def _expected_selection(self, selection_id: str) -> Optional[dict]:
        """What the popover MUST show for this token, from the Pinnacle catalog (authoritative Pinnacle naming,
        not the Kalshi-side label). Cached briefly -- catalog() is a guest call."""
        now = time.time()
        if now - getattr(self, "_cat_cache_ts", 0) > 120:
            try:
                self._cat_cache = {c.selection_id: c for c in await self.catalog()}
                self._cat_cache_ts = now
            except Exception:
                self._cat_cache = getattr(self, "_cat_cache", {})
        entry = getattr(self, "_cat_cache", {}).get(selection_id)
        if not entry:
            return self._expected_from_pairs(selection_id)
        ev = entry.event or ""
        for sep in (" vs ", " - ", " v "):
            if sep in ev:
                a, b = ev.split(sep, 1)
                return {"nameA": a.strip(), "nameB": b.strip(), "side": (entry.selection_name or "").strip()}
        return self._expected_from_pairs(selection_id)

    def _expected_from_pairs(self, selection_id: str) -> Optional[dict]:
        """Fallback expectation from cross_pairs.json when the live catalog has no entry for this token.

        WHY THIS IS NEEDED, and it is not an edge case. `catalog()` is built from the GUEST straight-markets
        feed and keeps only matchups carrying an available full-game moneyline. Measured 2026-08-18 on a live
        ITF league: THREE OF SIX live matchups had no such moneyline in the guest feed at all, persistently.
        The bot still prices those tokens — its odds come from the AUTHED re-seed and the browser WS, which do
        carry them — so it detects arbs on markets the catalog cannot describe, and the camp then refuses to
        arm with "no catalog entry". That cost the second-most-productive game of the run (24 windows).
        Finished leagues drop out the same way once their matchups empty.
        THE SOURCE IN THE PAIR FILE IS `hardven_yes_name` / `hardven_no_name` — the Pinnacle names the
        pairing actually RESOLVED this token to. Not `event_title`.

        This used to read event_title and split it as "{home} vs {away}". That premise was FALSE:
        `pairHard.py` writes `"event_title": ev.get("title")` straight from the KALSHI event, so its A-vs-B
        ordering is Kalshi's naming convention and makes no claim about which side Pinnacle designates home.
        The check therefore confirmed the venue's own home/away against itself and passed on a row whose
        sides were inverted — 2026-08-19, two live fires bought Kalshi-NO on Reyniak alongside
        Pinnacle-Izquierdo, both legs on the same outcome, and this function said the slip was correct.

        The resolved-name fields carry no such assumption: they are what `_pick_book_team` chose out of
        Pinnacle's OWN team list for this exact token. Rows written before those fields existed simply have
        no entry, and get None — which the caller already treats as "cannot verify", the safe answer.

        This is deliberately NOT a weakening of the check. The popover is still verified against real
        participant names before anything is armed; only the SOURCE of those names changed — to the one
        that cannot be right about the match and wrong about the side."""
        parts = (selection_id or "").split(":")
        if len(parts) < 3 or parts[2] not in ("home", "away"):
            return None
        cache = getattr(self, "_pair_names", None)
        if cache is None or time.time() - getattr(self, "_pair_names_ts", 0) > 300:
            cache = {}
            try:
                path = Path(__file__).parent.parent / "cross_pairs.json"
                for e in json.loads(path.read_text(encoding="utf-8")):
                    yn = (e.get("hardven_yes_name") or "").strip()
                    nn = (e.get("hardven_no_name") or "").strip()
                    if not (yn and nn):
                        continue                    # pre-fix row: no resolved names, so nothing to verify against
                    yt = e.get("hardven_yes_token") or ""
                    nt = e.get("hardven_no_token") or ""
                    # Each token maps to (the name IT buys, the opponent). Both directions are stored so the
                    # NO token is verifiable too, not just the YES one.
                    if yt.count(":") >= 2:
                        cache[yt] = (yn, nn)
                    if nt.count(":") >= 2:
                        cache[nt] = (nn, yn)
            except Exception:
                cache = getattr(self, "_pair_names", None) or {}
            self._pair_names = cache
            self._pair_names_ts = time.time()
        got = cache.get(selection_id)
        if got:
            side, other = got
            print(f"[PINNACLE] {selection_id}: not in the live catalog — verifying against the name the "
                  f"pairing resolved for THIS token ('{side}').", flush=True)
            return {"nameA": side, "nameB": other, "side": side}
        return None

    def _league_url_for(self, lid: str) -> str:
        """League page URL for a league id, from the pairing file (pair_pinnacle writes hardven_league_url)."""
        cached = getattr(self, "_lid_urls", None)
        if cached is None or time.time() - getattr(self, "_lid_urls_ts", 0) > 300:
            urls: dict = {}
            try:
                path = Path(__file__).parent.parent / "cross_pairs.json"
                for e in json.loads(path.read_text(encoding="utf-8")):
                    tok, u = e.get("hardven_yes_token") or "", e.get("hardven_league_url") or ""
                    if u and tok.count(":") >= 2:
                        urls.setdefault(tok.split(":")[0], u)
            except Exception:
                pass
            self._lid_urls, self._lid_urls_ts = urls, time.time()
            cached = urls
        return cached.get(lid, "")

    def _primary_page(self):
        """The main board tab (session anchor). Betting here needs no navigation — the placement flow was
        captured on the board — so it neither disturbs the featured-board WS subscription nor the session."""
        br = self._browser
        return getattr(br, "_page", None) if br is not None else None

    def _on_board(self, lid: str, ttl: float = 90.0) -> bool:
        """True when the FEATURED BOARD is actively streaming this league RIGHT NOW (a sport-topic push within
        `ttl`). Since board_lids is fed from the primary page's own sp/ subscription, a FRESH hit means the
        primary board is currently showing the league — the precondition for betting on it without navigating."""
        try:
            return str(lid) in self.board_lids(ttl=ttl)
        except Exception:
            return False

    # ── human-like mouse (placement clicks look like a person, and fire REAL pointer events) ──
    @staticmethod
    def _surname(s: str) -> str:
        parts = (s or "").lower().split()
        return parts[-1] if parts else ""

    async def _human_move_page(self, page, tx: float, ty: float) -> None:
        """Curved, eased mouse move to (tx,ty), tracking our own cursor (Playwright doesn't expose it). Makes a
        placement approach look like a person reaching for the button, not a teleport."""
        start = self._bet_cursor or (tx + random.uniform(-200, 200), ty + random.uniform(-150, 150))
        x0, y0 = start
        dx, dy = tx - x0, ty - y0
        dist = math.hypot(dx, dy)
        if dist < 2:
            self._bet_cursor = (tx, ty)
            return
        pxu, pyu = -dy / dist, dx / dist
        bow = random.uniform(0.05, 0.20) * dist * random.choice((-1.0, 1.0))
        c1 = (x0 + dx * 0.30 + pxu * bow, y0 + dy * 0.30 + pyu * bow)
        c2 = (x0 + dx * 0.65 + pxu * bow, y0 + dy * 0.65 + pyu * bow)
        steps = int(max(10, min(40, dist / 10)))
        total = random.uniform(0.14, 0.34) * (0.6 + dist / 900)
        for i in range(1, steps + 1):
            t = i / steps
            s = t * t * (3 - 2 * t)                       # smoothstep ease
            u = 1 - s
            bx = u*u*u*x0 + 3*u*u*s*c1[0] + 3*u*s*s*c2[0] + s*s*s*tx + random.uniform(-1, 1)
            by = u*u*u*y0 + 3*u*u*s*c1[1] + 3*u*s*s*c2[1] + s*s*s*ty + random.uniform(-1, 1)
            try:
                await page.mouse.move(bx, by)
            except Exception:
                break
            await asyncio.sleep(max(0.004, total / steps * random.uniform(0.6, 1.4)))
        self._bet_cursor = (tx, ty)

    async def _human_click_loc(self, page, loc, fast: bool = False) -> bool:
        """Curved human approach toward the element, then a RELIABLE real click. `loc.click()` re-resolves the
        element's LIVE position at click time — so the constantly-reordering board (odds ticking, rows inserted)
        can't make us land on the button that slid into stale coordinates (a handicap next to the moneyline) —
        and it fires the full pointer-event sequence, so the Quick Bet opens correctly (unlike a synthetic JS
        `.click()` with no pointer events). Works on both an ElementHandle and a Locator."""
        # WHEEL TOWARD IT FIRST. `scroll_into_view_if_needed` teleports the scroll position in one step;
        # this site runs Microsoft Clarity, which records scroll. Wheeling in notches costs nothing and
        # is what the BIA cursor already does — the jump was the last instant-teleport in either path.
        # PHASE TIMING ON THE EXECUTION PATH. The 2026-08-19 fire spent 1709ms here against a venue that
        # answered in 951ms, and nothing said which of the five steps owned it - so the only honest way to
        # get it to 500ms is to see the split first. Costs a few perf_counter calls, and only prints fast.
        _t = time.perf_counter(); _ph = {}
        def _mark(name):
            nonlocal _t
            now = time.perf_counter(); _ph[name] = round((now - _t) * 1000); _t = now
        try:
            box = await loc.bounding_box()
            if box and not (80 <= box["y"] <= 700):
                await CURSOR.scroll(page, box["y"] - 320)
        except Exception:
            pass
        _mark("wheel")
        try:
            await loc.scroll_into_view_if_needed(timeout=4000)   # fallback for virtualised panes
        except Exception:
            pass
        _mark("scrollinto")
        try:
            box = await loc.bounding_box()
        except Exception:
            box = None
        _mark("box")
        if box:
            # OFF-CENTRE. This aimed at the exact geometric centre on every click; people do not, and
            # "always dead centre" is a signature that survives however good the approach path is.
            await self._human_move_page(page,
                                        box["x"] + box["width"] * random.uniform(0.35, 0.65),
                                        box["y"] + box["height"] * random.uniform(0.35, 0.65))
            # Dwell measured against 37 recorded gestures (mouse_record.py): a reach ends in a settle,
            # not an immediate press. Shared with the BIA path so both stay in step.
            _mark("move")
            try:
                from human_mouse import dwell as _dwell
                await asyncio.sleep(await _dwell(fast=fast))
            except Exception:
                await asyncio.sleep(random.uniform(0.15, 0.45))
            _mark("dwell")
        try:
            # 42-92ms measured across the same 37 gestures; the old floor of 30 was below anything real.
            await loc.click(timeout=5000, delay=random.randint(42, 92))
        except Exception:
            return False
        _mark("click")
        if fast:
            print("[PINNACLE CLICK] " + " ".join(f"{k}={v}ms" for k, v in _ph.items())
                  + f" total={sum(_ph.values())}ms", flush=True)
        return True

    async def _position_over_list(self, page) -> bool:
        """Rest the cursor over the MAIN match list (a currently-visible odds row, top carousel excluded) so a
        wheel scrolls THAT list — the featured-board view scrolls its OWN inner pane, not the page, so wheeling
        from a random spot moves the wrong thing (or the horizontal carousel) and the target row never surfaces.
        Done ONCE before a sweep — the list's on-screen position doesn't move as its content scrolls, so the
        cursor stays over it (re-moving each notch is what made scrolling look clunky). Returns True if anchored
        on a row; falls back to the content-area centre when no row is on screen."""
        try:
            a = await page.evaluate(_SCROLL_ANCHOR_JS)
        except Exception:
            a = None
        if a and a.get("found"):
            # rest over the ROW BODY (nudge left of the odds button — still inside the list, over no control)
            tx = max(40.0, float(a["x"]) - random.uniform(20, 90))
            ty = float(a["y"]) + random.uniform(-8, 8)
            await self._human_move_page(page, tx, ty)
            return True
        if a and a.get("w"):
            await self._human_move_page(page, float(a["w"]) * random.uniform(0.35, 0.60),
                                        float(a["h"]) * random.uniform(0.35, 0.60))
        return False

    async def _wheel(self, page, notches: int = 1, hard: float = 1.0) -> None:
        """A few small wheel notches at the CURRENT cursor position (call _position_over_list first).

        `hard` scales how far each notch travels, NOT how many there are. Covering ground by adding notches
        buys nothing — each one carries its own pacing sleep, so the time per pixel stays flat and a deep
        scan costs exactly as much either way (modelled 2026-08-20: 10 passes went 6.0s -> 8.4s that way,
        i.e. worse). A bigger delta per notch is also the more human of the two: someone who has not found
        what they are looking for flicks harder, they do not spin the same tiny amount more times.
        """
        for _ in range(random.randint(2, 4) * max(1, notches)):
            try:
                await page.mouse.wheel(0, int(random.randint(120, 260) * hard))
            except Exception:
                return
            await asyncio.sleep(random.uniform(0.05, 0.16))

    def _verify_pop(self, pop: dict, A: str, B: str, S: str,
                    require_market: str = "money line", reject=("(games)",)) -> dict:
        """Same wrong-market defence the JS used to do, now in Python (the click moved to a real mouse action).
        matchup+side alone is NOT enough — a handicap ('Adam Walton +1.5 (Sets)') or the Games shell also carries
        the side name; require the moneyline text AND reject derivative/Games labels, or a suspended moneyline
        would silently fall through to a handicap at different odds."""
        m = (pop.get("matchup") or pop.get("all") or "").lower()
        lab = (pop.get("label") or "").lower()
        allt = (pop.get("all") or "").lower()
        matchup_ok = A in m and B in m
        side_ok = S in lab
        market_ok = require_market in allt
        derivative = bool(re.search(r"[+-]\s*\d+(\.\d+)?", lab)) or bool(re.search(r"\b(over|under|total)\b", lab))
        rejected = any(r.lower() in lab for r in reject)
        try:
            price = float(pop.get("price") or 0)
        except (TypeError, ValueError):
            price = 0.0
        ok = matchup_ok and side_ok and market_ok and not derivative and not rejected
        why = (f"matchup={matchup_ok} side={side_ok} market={market_ok} deriv={derivative} "
               f"rejected={rejected} label='{pop.get('label')}'")
        # Carry the VENUE'S OWN WORDS for who this bet is on. Everything above proves we clicked the
        # Pinnacle selection we meant to; none of it knows which side KALSHI is on, so an inverted pairing
        # passes every check here and still buys the same outcome twice. Handing the label upward is what
        # lets the caller cross-check it against kalshi_outcome — the one test a bad pairing cannot survive.
        return {"ok": ok, "price": price, "why": why,
                "label": pop.get("label") or "", "matchup": pop.get("matchup") or ""}

    async def _try_select_on(self, page, exp: dict):
        """Bring `page` to front, find the intended moneyline's odds button, and click it with a REAL human mouse
        action (opens the Quick Bet popover correctly — a JS `.click()` misfires it). Probes candidates: click,
        read the popover, verify matchup+side+market; on a miss close and try the next. Selection PLACES NOTHING,
        so it's safe to attempt on several tabs. Returns the sel result (ok/price/…) or None if the page is dead."""
        if page is None:
            return None
        try:
            if page.is_closed():
                return None
        except Exception:
            return None
        try:
            await page.bring_to_front()
        except Exception:
            pass
        A, B, S = self._surname(exp["nameA"]), self._surname(exp["nameB"]), self._surname(exp["side"])
        if not (A and B and S):
            return {"ok": False, "error": "incomplete expected names"}

        # ── DON'T HUNT A PRE-MATCH GAME ON THE LIVE BOARD ────────────────────────────────────────────
        # In-play pins the page to /matchups/live/, which lists LIVE games only — so a token whose match
        # has not started is not on it, and never will be. The scan below cannot know that: it wheels ten
        # viewports and reports "no row mentions both X and Y", which reads like a markup problem.
        #
        # Measured 2026-08-20: six of those, 11.6s to 14.9s EACH — around 75 seconds spent scrolling a list
        # that could not contain the answer, while the arb windows they were meant to catch went past.
        #
        # The feed already knows. `live` comes off the same WS record that prices the token, so this costs
        # nothing and fails in microseconds instead of fifteen seconds. Only applies in-play: pre-live can
        # legitimately reach a pre-match row through a rove tab or the sport board.
        if self.mode == "inplay":
            with self._cache_lock:
                cached = self._cache.get(selection_id)
            if cached is not None and not cached.live:
                return {"ok": False, "not_live": True,
                        "error": f"{selection_id} is a PRE-MATCH market and the board is the live list — "
                                 f"it cannot be on the page. Not scanning for it."}

        # Candidate buttons as STABLE ElementHandles (never positional indices — the live board reorders, so an
        # index lands on the wrong button). Filter to those whose row mentions both players; scroll to surface
        # off-screen/virtualised rows before concluding it's absent.
        async def _find():
            out = []
            try:
                handles = await page.query_selector_all("button.market-btn")
            except Exception:
                return out
            # ONE CALL, NOT ONE PER BUTTON. Same test, evaluated across the whole board in a single round
            # trip; the per-handle path below remains as the fallback if it ever fails. Indices line up
            # because both sides walk document order.
            idx = None
            try:
                idx = set(await page.evaluate(_ROWS_MATCH_ALL_JS, {"A": A, "B": B, "maxBtns": 10}))
            except Exception:
                idx = None
            for i, h in enumerate(handles):
                keep = False
                try:
                    keep = (i in idx) if idx is not None else                         bool(await h.evaluate(_ROW_MATCH_JS, {"A": A, "B": B, "maxBtns": 10}))
                except Exception:
                    keep = False
                if keep:
                    out.append(h)
                else:
                    try:
                        await h.dispose()
                    except Exception:
                        pass
            return out

        cands = await _find()
        positioned = False
        scanned = 0
        t_scan = time.time()
        # THE LIVE LIST IS THREE SCREENS LONG. Measured 2026-08-20 via /debug/board_scan: 2398px, bottom
        # reached in 3 scroll steps, 12 matchups in the DOM. The loop below was willing to wheel TEN times —
        # so a row that genuinely was not there spent seven further passes scrolling a list that had already
        # ended, which is the whole of the 11.6-14.9s that six failed scans cost that day.
        #
        # Reaching the bottom is proof of absence, and it arrives in about a fifth of the time the pass
        # budget does. The count stays as a backstop for a list that never reports a bottom.
        async def _at_bottom() -> bool:
            try:
                return bool(await page.evaluate(_SCROLL_AT_BOTTOM_JS))
            except Exception:
                return False
        while not cands and scanned < 10:
            if not positioned:
                await self._position_over_list(page)   # park the cursor over the LIST once (not 10 curved moves)
                positioned = True
            # SCROLL HARDER, NOT MORE. Each pass travels further than the last (see _wheel's `hard`), so
            # the ground covered grows while the number of wheel events — and therefore the time — does
            # not. The flat 0.2s settle goes too: the wheel already paces itself between notches, and that
            # fixed wait was being paid on every one of up to ten passes.
            await self._wheel(page, hard=1.0 + 0.6 * scanned)
            await asyncio.sleep(0.08)                  # the wheel already paces itself between notches
            cands = await _find()
            scanned += 1
            if not cands and await _at_bottom():
                print(f"[PINNACLE ROW] bottom of the list after {scanned} pass(es) — the row is not on this "
                      f"board, not merely below the fold.", flush=True)
                break
        if scanned:
            print(f"[PINNACLE ROW] found after {scanned} scroll pass(es) in "
                  f"{(time.time() - t_scan) * 1000:.0f}ms" if cands else
                  f"[PINNACLE ROW] gave up after {scanned} pass(es) in {(time.time() - t_scan) * 1000:.0f}ms",
                  flush=True)
        if not cands:
            try:
                await page.evaluate("() => window.scrollTo(0, 0)")
            except Exception:
                pass
            return {"ok": False, "error": f'no row mentions both "{A}" and "{B}" (scanned {scanned} viewport(s))'}

        try:
            await page.evaluate(_UI_CLOSE_JS)                 # start clean
        except Exception:
            pass
        # ORDER BEFORE CLICKING. Every candidate costs a full human approach — 2041ms on the arm measured
        # 2026-08-20, where the FIRST button tried failed verification and the second succeeded. The row text
        # already says which button belongs to the side we want, and reading it is one cheap call against a
        # click that is two orders of magnitude dearer. So score first, click in that order.
        async def _rank(h):
            try:
                txt = (await h.evaluate("e => (e.closest('[class*=row i]') || e.parentElement || e).innerText || ''")
                       or "").lower()
            except Exception:
                return 0
            score = 0
            if S and S in txt:
                score += 2                       # the row names the side we intend to back
            if "money" in txt:
                score += 1                       # a moneyline cell rather than a handicap/total alongside it
            for bad in ("(games)", "(sets)"):
                if bad in txt:
                    score -= 1                   # derivative shells carry the same player names
            return score
        try:
            scored = [(await _rank(h), i, h) for i, h in enumerate(cands[:20])]
            scored.sort(key=lambda t: (-t[0], t[1]))     # best first, original order as the tiebreak
            cands = [t[2] for t in scored] + cands[20:]
        except Exception:
            pass
        tried = []
        result = {"ok": False, "error": "no candidate matched"}
        try:
            for h in cands[:20]:
                # FAST IN BOTH MODES NOW. This used to take the full human settle in-play on the reasoning
                # that "nothing is racing, the fire comes later" — true when a camp was armed once and held
                # for twenty minutes. It is not true any more: 2026-08-20 saw 25 arm attempts in a session,
                # a third of them retries after a failure, and the loop below will try up to 20 candidates
                # — so the slow settle was being paid over and over while windows went past. The click is
                # still a real one (curved approach, off-centre aim, 42-92ms button delay); only the pause
                # between arriving and pressing shortens.
                if not await self._human_click_loc(page, h, fast=True):
                    tried.append("no box")
                    continue
                pop = None
                # READ FIRST, THEN WAIT. This slept 80ms before its first look, every time, on a popover
                # that renders in ~300ms — so the cheapest possible outcome still cost a tick. Same 1.5s
                # ceiling, finer granularity, and the common case returns as soon as it is actually there.
                for _i in range(30):
                    try:
                        pop = await page.evaluate(_UI_READ_POP_JS)
                    except Exception:
                        pop = None
                    if pop:
                        break
                    await asyncio.sleep(0.05)
                if not pop:
                    tried.append("no popover")
                    continue
                v = self._verify_pop(pop, A, B, S)
                if v["ok"]:
                    result = {"ok": True, "price": v["price"], "matchup": pop.get("matchup"),
                              "label": pop.get("label"), "maxBet": pop.get("maxBet"),
                              "american": bool(pop.get("american"))}
                    break
                tried.append(v["why"])
                try:
                    await page.evaluate(_UI_CLOSE_JS)
                except Exception:
                    pass
                await asyncio.sleep(random.uniform(0.15, 0.35))
            if not result["ok"]:
                result["error"] = f"no candidate matched ({len(cands)}): " + " | ".join(str(x) for x in tried[:4])
        finally:
            for h in cands:
                try:
                    await h.dispose()
                except Exception:
                    pass
        return result

    async def _select_bet_tab(self, lid: str, url: str, exp: dict):
        """Pick the tab to bet on and verify the market on it (returns (page, kind, sel_ok)). A FOCUSED single-
        league page is preferred, because on it the target row is always present and the find is fast + reliable;
        the featured BOARD is a big combined list where a specific match is slow and often un-findable (a blind
        scroll-and-search), so it's demoted to a fallback (was tried first until 2026-07-30 — it wasted ~10s
        scroll-missing on the board then fell to rove-nav anyway, and blew the C# 15s timeout doing it):
          1. a reader tab already showing the league (a dedicated tab, or the rove parked there) — focused, no nav.
          2. the roving TAIL tab, navigated to the league — a focused league page (fast, reliable find).
          3. the PRIMARY BOARD page, when it's streaming this league — no navigation, but a blind scroll on the
             big board list, so only if there's no focused tab (e.g. tab manager off / rove busy).
          4. a cold bet tab (tab manager off).
        Selection places nothing, so a miss on one candidate just closes the popover and tries the next. Returns
        the last failure if none verify, so the caller can report why."""
        last = (None, None, None)
        tm = self._tab_manager

        # IN-PLAY MODE STAYS ON ITS ONE TAB. Candidates 1 and 2 borrow or NAVIGATE a reader/rove tab,
        # which is right pre-live (a focused league page finds the row fast) and wrong here: the camper's
        # whole premise is that it is already parked on the live list with a slip armed, and flipping to
        # another tab mid-camp loses the window it is camped for. Fall through to the primary page, which
        # IS the live list. The tab manager object is left intact so stopping in-play restores normal
        # behaviour without rebuilding it.
        # THE PRIMARY PAGE IS THE ONLY CANDIDATE, and it must be the LAST word too. Nulling `tm` skipped
        # candidates 1-2, but 3 was gated on `_on_board(lid)` — which is fed by the FEATURED BOARD's sport
        # topic and is routinely False on the live list — so the miss fell straight through to candidate 4,
        # `_bet_tab()`. That opens (or re-points) a separate tab at the league's /matchups/ URL and calls
        # bring_to_front(), which is exactly the "the page keeps going back to the matchup page" symptom:
        # the visible window switches to a matchups tab, and any slip armed there is invisible to
        # camp_status / the in-play watcher, which both read the PRIMARY page.
        #
        # Failing here is the CORRECT outcome, not a limitation: a market that cannot be found on the live
        # list is not in-play, so there is nothing to camp on and navigating to find it would be answering
        # the wrong question at the cost of the camp.
        if self.mode == "inplay":
            primary = self._primary_page()
            if primary is not None:
                r = await self._try_select_on(primary, exp)
                return primary, ("live" if (r and r.get("ok")) else "live-miss"), r
            return None, "live-nopage", {"ok": False, "error": "in-play mode has no primary page"}

        # 1. a reader tab already on the league (gap leagues: dedicated tab / rove parked here) — focused, no nav
        if tm is not None:
            page, kind = tm.page_for_lid(lid)
            if page is not None:
                r = await self._try_select_on(page, exp)
                if r and r.get("ok"):
                    return page, kind, r
                last = (page, kind, r)

        # 2. borrow the roving tail tab and navigate it to the league — a focused league page, reliable find
        if tm is not None:
            rpage = await tm.acquire_rove_for_bet(url, lid=lid)   # mark the league covered while we're parked here
            if rpage is not None:
                r = await self._try_select_on(rpage, exp)
                if r and r.get("ok"):
                    return rpage, "rove-nav", r
                last = (rpage, "rove-nav", r)

        # 3. the primary board page (blind scroll on the big combined list) — fallback only
        if self._on_board(lid):
            primary = self._primary_page()
            if primary is not None:
                r = await self._try_select_on(primary, exp)
                if r and r.get("ok"):
                    return primary, "board", r
                last = (primary, "board", r)

        # 4. cold last-resort tab
        cold = await self._bet_tab(url)
        if cold is not None:
            r = await self._try_select_on(cold, exp)
            return cold, "cold", r

        return last

    async def _bet_tab(self, url: str):
        """Cold last-resort tab for placing bets (tab manager off / roving disabled)."""
        if self._browser is None:
            return None
        page = self._bet_page
        try:
            if page is not None and not page.is_closed():
                if not url.rstrip("/") in (page.url or "").rstrip("/"):
                    await self._browser.navigate_tab(page, url)
                await page.bring_to_front()
                return page
        except Exception:
            page = None
        page = await self._browser.open_tab(url)
        self._bet_page = page
        return page

    async def _betslip_sweep_loop(self) -> None:
        """Periodically clear the side betslip on EVERY open tab — 'if the Remove-all button is visible, press
        it'. The organic-tick trim only fires when a gesture happens to land on the tab holding the stray
        selections (one random reader tab every 30-90s, primary every 20-150s), so a manual slip could sit for
        minutes; this sweep is independent of organic cadence and covers all tabs each pass.

        Skips entirely while a bet is in flight (`_bet_lock` held) so it can never fight a placement — the
        post-bet trim handles that case. Read-only when the slip is empty (no Remove-all button = no clicks)."""
        while True:
            try:
                await asyncio.sleep(self._betslip_sweep_sec)
                # ⚠ AN ARMED SLIP IS INDISTINGUISHABLE FROM A STRAY ONE. The in-play camper deliberately
                # leaves a Quick Bet open with a stake entered, so that the next arb on the game it is
                # parked on costs one press instead of navigate-find-click-type. This loop would clear it
                # within 25s and the camper would look like it simply never worked — a silent conflict
                # between two correct behaviours. `_camping` is held for the life of a camp.
                if getattr(self, "_camping", False):
                    continue
                # The operator's own selections are indistinguishable from strays, and this loop's whole job
                # is deleting anything it does not recognise. Off while they are driving.
                if self._manual_mode:
                    continue
                if not self._betslip_trim or self._bet_lock.locked():
                    continue
                pages = []
                primary = self._primary_page()
                if primary is not None:
                    pages.append(primary)
                if self._tab_manager is not None:
                    try:
                        pages.extend(p for p, _ in (self._tab_manager.reader_tabs() or []) if p is not None)
                    except Exception:
                        pass
                for pg in pages:
                    if self._bet_lock.locked():        # a bet started mid-sweep -> stop touching the browser
                        break
                    try:
                        await self._trim_betslip(pg, source="sweep")
                    except Exception:
                        pass
            except asyncio.CancelledError:
                break
            except Exception:
                pass                                   # pure hygiene: never let a hiccup kill the loop

    async def _trim_betslip(self, page, source: str = "post-bet") -> dict:
        """Sweep stray selections out of the SIDE betslip via its own 'Remove all' -> confirm-modal flow, then
        verify the slip emptied. Called after every placement attempt (`source='post-bet'`) AND while the bot is
        idly roaming tabs (`source='idle'`), so a stray selection never lingers -- probing / misfires / a manual
        stray click can drop selections into the side slip, which then clutter it.

        SAFE against real money. 'Remove all' only discards PENDING (unsubmitted) selection cards -- an accepted
        bet lives server-side and is confirmed by its own POST /bets/straight 200, not by anything in this slip,
        so trimming cannot cancel a placed bet. The button only exists while there ARE removable selections, so
        an empty / accepted-only slip is a clean no-op. Real mouse clicks (not JS .click) for the same reason
        odds buttons need them: the site's handlers expect a full pointer sequence. Pure cleanup -- every caller
        swallows any exception so a trim hiccup can never change a BetResult or crash the organic loop."""
        # ⚠ CAMPING HOLDS A SELECTION ON PURPOSE. Gated HERE rather than at the call sites because there
        # are five of them — the idle organic trim (two constructions), the 25s sweep, the post-quote
        # trim and the post-bet trim — and a guard added to some but not all would disarm the camper
        # intermittently, which is the hardest possible thing to diagnose. `post-bet` is deliberately
        # still allowed: after a placement the slip SHOULD be cleared, and the camper re-arms.
        if getattr(self, "_camping", False) and source != "post-bet":
            return {"trimmed": False, "reason": f"camping — armed slip preserved (source={source})"}
        if not self._betslip_trim:
            return {"trimmed": False, "reason": "disabled"}
        if page is None:
            return {"trimmed": False, "reason": "no page"}
        try:
            if page.is_closed():
                return {"trimmed": False, "reason": "page closed"}
        except Exception:
            return {"trimmed": False, "reason": "page closed"}
        try:
            st = await page.evaluate(_BETSLIP_STATE_JS)
        except Exception as e:
            return {"trimmed": False, "reason": f"state read failed: {e}"}
        if not st.get("hasRemoveAll"):
            return {"trimmed": False, "reason": "nothing to remove", "cards": st.get("cards", 0)}

        before = int(st.get("cards", 0) or 0)
        # 1. click 'Remove all' (a real mouse action, consistent with the odds/Place Bet clicks)
        ra = page.locator('[data-test-id="Betslip-RemoveAllButton"]').first
        try:
            if await ra.count() == 0:
                return {"trimmed": False, "reason": "remove-all vanished"}
        except Exception:
            return {"trimmed": False, "reason": "remove-all query failed"}
        if not await self._human_click_loc(page, ra):
            return {"trimmed": False, "reason": "remove-all click failed"}

        # 2. wait for the confirm modal, then click its Confirm (NEVER Cancel)
        confirm = page.locator('[data-test-id="Betslip-RemoveAllModal-ConfirmButton"]').first
        appeared = False
        for _ in range(15):                     # modal renders fast; ~1.2s ceiling
            await asyncio.sleep(0.08)
            try:
                if await confirm.count() > 0:
                    appeared = True
                    break
            except Exception:
                pass
        if not appeared:
            return {"trimmed": False, "reason": "confirm modal did not appear", "before": before}
        if not await self._human_click_loc(page, confirm):
            return {"trimmed": False, "reason": "confirm click failed", "before": before}

        # 3. verify the slip actually emptied (modal gone AND cards cleared)
        after = before
        for _ in range(15):
            await asyncio.sleep(0.08)
            try:
                st2 = await page.evaluate(_BETSLIP_STATE_JS)
            except Exception:
                break
            after = int(st2.get("cards", after) or 0)
            if not st2.get("modalOpen") and after == 0:
                break
        ok = after < before
        print(f"[PINNACLE] betslip trim ({source}): cards {before} -> {after}"
              + ("" if ok else " (WARNING: not cleared)"))
        return {"trimmed": ok, "before": before, "after": after}

    def _pause_all_organic(self) -> None:
        """Freeze ALL human-activity loops for a bet: the primary tab's organic, the per-tab organic, and the
        tab manager's open/close/navigate churn. So nothing steals focus (bring_to_front) or fights a click
        while money is being placed."""
        try:
            if self._browser is not None:
                self._browser.pause_activity()
        except Exception:
            pass
        if self._tab_organic is not None:
            self._tab_organic.pause()
        if self._tab_manager is not None:
            self._tab_manager.hold(True)
        # IN-PLAY IDLE TOO. It was added after this function and would otherwise keep scrolling and
        # opening random slips DURING an arm or a placement — moving the board under the click, or
        # opening a competing Quick Bet over the one being armed. Every loop that touches the browser
        # has to be listed here; that is the whole contract of this function.
        ip = getattr(self, "_inplay", None)
        if ip is not None:
            ip.pause()

    # ── MANUAL MODE (operator is driving) ─────────────────────────────────────
    def manual_blocked(self, what: str) -> Optional[dict]:
        """One refusal used by every automated action. Returns a dict to return, or None to carry on.

        Centralised deliberately. The lesson from the betslip-trim guard is that a hold added at some call
        sites and not others is worse than no hold, because the remaining ones fire rarely and look like
        random misbehaviour rather than a missing check."""
        if not self._manual_mode:
            return None
        return {"ok": False, "accepted": False, "manual": True,
                "error": f"MANUAL MODE is on — {what} refused. The operator is driving the browser. "
                         f"Toggle it off (press 'm' in the sidecar window, or POST /control/manual) to resume."}

    async def manual_mode(self, on: bool, minutes: float = 0.0) -> dict:
        """Freeze / unfreeze EVERY automation that touches the browser, so the site can be used by hand.

        Distinct from the banking window, which shares the freeze but also overrides the schedule, opens the
        cashier in its own tab, and auto-reverts into a halt. This does none of that: it changes no trading
        state, navigates nowhere, and leaves the page exactly where the operator put it. It is purely "stop
        touching things".

        WHAT IT HOLDS, and each of these was found by asking what would still move the page:
          * session organic (mouse/keyboard/nav) and the per-tab organic
          * the tab manager's open/close/re-point sweep
          * the in-play idle loop (scroll, random slip peeks, hover)
          * the periodic session-refresh RELOAD               (session.set_manual)
          * the board-drift watchdog, which is the sneaky one (session.set_manual)
          * the 25s betslip sweep, which would delete the operator's own selections
          * the reader re-seed's REST traffic
          * every placement path: /bet, camp arm, camp fire, slip quotes, WS verify

        `minutes` sets a safety auto-release; 0 means it stays on until switched off. The default is 0 on
        purpose — an operator halfway through something does not want a timer deciding they are finished —
        but the state is reported everywhere so a forgotten toggle is visible rather than mysterious.
        """
        on = bool(on)
        was = self._manual_mode
        self._manual_mode = on
        self._manual_until = (time.time() + minutes * 60.0) if (on and minutes > 0) else 0.0
        try:
            if self._browser is not None:
                self._browser.set_manual(on)
        except Exception:
            pass
        if on:
            self._pause_all_organic()
            if self._manual_task is None or self._manual_task.done():
                self._manual_task = asyncio.create_task(self._manual_expiry_loop())
            until = ("until you switch it off" if not self._manual_until
                     else f"for {minutes:g}m")
            print(f"[PINNACLE MANUAL] ON {until} - organic, tab churn, in-play idle, session reloads, the "
                  f"board-drift watchdog, the betslip sweep and REST re-seeds are all OFF. Nothing will be "
                  f"placed. The browser is yours.", flush=True)
        else:
            # Do NOT resume in-play idle here if in-play is not running; _resume_all_organic is already
            # mode-aware, so this restores exactly what was running before.
            self._resume_all_organic()
            if was:
                print("[PINNACLE MANUAL] OFF - automation resumed.", flush=True)
        return self.manual_status()

    def manual_status(self) -> dict:
        left = max(0.0, self._manual_until - time.time()) if self._manual_until else 0.0
        return {"ok": True, "manual": self._manual_mode,
                "expires_in_sec": round(left, 1) if self._manual_until else None,
                "mode": self.mode}

    async def _manual_expiry_loop(self) -> None:
        while self._manual_mode:
            await asyncio.sleep(2.0)
            if self._manual_until and time.time() >= self._manual_until:
                print("[PINNACLE MANUAL] timer expired - resuming automation.", flush=True)
                await self.manual_mode(False)
                return

    def _on_banking(self, on: bool) -> None:
        """Lifecycle hook for the operator's banking window: freeze every automation that touches the browser,
        and put the site in front of them in the bot's OWN profile (same cookies/account that place the bets).
        Deferred to a task because banking OPENS the browser on top of a halt — at call time it may not be up
        yet — and re-applied there because the tab manager is recreated on session-open and would start unheld."""
        self._banking_mode = bool(on)
        if on:
            self._pause_all_organic()
            if self._banking_task is None or self._banking_task.done():
                self._banking_task = asyncio.create_task(self._banking_window())
        else:
            try:
                if self._browser is not None:
                    self._browser.set_banking(False)
            except Exception:
                pass
            self._resume_all_organic()

    async def _banking_window(self) -> None:
        for _ in range(120):                                  # up to ~60s for a cold browser start
            if not self._banking_mode:
                return
            if self._browser is not None and getattr(self._browser, "_page", None) is not None:
                break
            await asyncio.sleep(0.5)
        if not self._banking_mode or self._browser is None:
            return
        self._pause_all_organic()                             # AFTER session-open recreated the tab manager
        try:
            self._browser.set_banking(True)                   # no session-refresh reload over a deposit form
            url = os.environ.get("PINNACLE_CASHIER_URL", "").strip() or self._browser._derive_home_url()
            await self._browser.open_banking_tab(url)
            print(f"[PINNACLE] banking window: opened {url} — automation frozen; the bot will not touch the "
                  "browser until it expires.")
        except Exception as ex:
            print(f"[PINNACLE] banking window: could not open the site ({type(ex).__name__}: {ex}) — the "
                  "browser IS up, navigate to the cashier manually.")

    def _resume_all_organic(self) -> None:
        """Undo _pause_all_organic — EXCEPT where in-play mode owns the pause.

        ⚠ TWO OWNERS, DIFFERENT LIFETIMES. The placement path pauses and resumes for the duration of ONE
        bet. In-play mode pauses for the lifetime of the MODE. Because placement's `finally` ran last, the
        first camp_start un-paused exactly what start_inplay had just silenced — and the session organic
        woke up and nav-clicked the board to /tennis/matchups/ moments after arming. Observed 2026-08-16.
        The armed slip survived only because Pinnacle is an SPA and the Quick Bet portal is mounted at the
        app root, so a soft navigation does not unmount it — luck, not design.
        So: the short-lived owner may not release the long-lived owner's hold.
        """
        inplay = self.mode == "inplay"
        try:
            if self._browser is not None and not inplay:
                self._browser.resume_activity()
        except Exception:
            pass
        if self._tab_organic is not None:
            self._tab_organic.resume()
        if self._tab_manager is not None and not inplay:
            self._tab_manager.hold(False)
        ip = getattr(self, "_inplay", None)
        if ip is not None:
            ip.resume()

    async def probe_bet_endpoints(self) -> dict:
        """DISCOVERY (read-only): find the authed endpoint that LISTS bets, so `open_bets()` can be a clean REST
        read instead of scraping My Bets in the browser.

        We know the namespace from the captured placement calls (`POST /bets/straight`, `/bets/straight/quote`,
        `/bets/parlay/quote`, plus `/wallet/balance` and `/sessions/{id}`), but the bet_capture recorder skipped
        GETs, so a listing endpoint was never recorded. This just tries the plausible paths.

        Deliberately uses the RAW http client, NOT `_http_get`: a 401/403 on a wrong guess must not increment
        `_rest_auth_fails`, which would otherwise trip the session-death give-up. Places nothing, changes nothing."""
        if not self._http:
            return {"error": "no authed http client (session not ready)"}
        candidates = [
            "/bets", "/bets/open", "/bets/running", "/bets/pending", "/bets/history", "/bets/settled",
            "/bets/straight", "/bets/list", "/bets?status=open", "/bets?state=running",
            "/wagers", "/wagers/open", "/tickets", "/account/bets", "/wallet/bets",
        ]
        out = []
        for path in candidates:
            try:
                r = await self._http.get(REST_BASE + path)
                body = ""
                try:
                    body = r.text[:220].replace("\n", " ")
                except Exception:
                    pass
                out.append({"path": path, "status": r.status_code, "len": len(r.content or b""), "body": body})
            except Exception as e:
                out.append({"path": path, "error": f"{type(e).__name__}: {e}"})
            await asyncio.sleep(0.25)          # gentle: this is the same authed session the feed uses
        hits = [o for o in out if o.get("status") == 200]
        print(f"[PINNACLE] bet-endpoint probe: {len(hits)} of {len(candidates)} returned 200 "
              + (", ".join(h["path"] for h in hits) if hits else "(none — the listing is likely UI-only)"))
        return {"base": REST_BASE, "results": out, "hits": [h["path"] for h in hits]}

    # ── My Bets (crash recovery + settlement) — authed REST, no browser ────────
    @staticmethod
    def _iso_z(dt: datetime) -> str:
        """Pinnacle's date params: '2026-08-04T20:15:09.973Z' (millisecond precision, literal Z)."""
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"

    @staticmethod
    def _selection_token(sel: dict) -> Optional[str]:
        """Rebuild the bot's selection id from a bet's selection. Straight moneyline is `lid:mid:designation`
        (exactly the token the executor trades); spread/total carry the line, matching pair_derivatives' format."""
        mu   = sel.get("matchup") or {}
        lid  = ((mu.get("league") or {}).get("id"))
        mid  = mu.get("id")
        desig = sel.get("designation")
        if lid is None or mid is None or not desig:
            return None
        mkt  = (sel.get("market") or {})
        mtype = (mkt.get("type") or "moneyline").lower()
        if mtype == "moneyline":
            return f"{lid}:{mid}:{desig}"
        pts = sel.get("points")
        return f"{lid}:{mid}:{mtype}:{pts}:{desig}" if pts is not None else f"{lid}:{mid}:{mtype}:{desig}"

    def _map_bet(self, b: dict) -> Optional[dict]:
        """One API bet -> the shape HardVenOrderClient expects (`selection_id`, `stake`, `odds`) plus status
        fields for settlement. PARLAYS ARE SKIPPED: they carry several selections under one stake, so they can't
        be attributed to a single token — and the bot only ever places straights."""
        sels = b.get("selections") or []
        if len(sels) != 1:
            return None
        sel = sels[0]
        token = self._selection_token(sel)
        if not token:
            return None
        try:
            stake = float(b.get("stake") or 0)
        except (TypeError, ValueError):
            stake = 0.0
        try:                                   # the ACCEPTED price: prefer the selection's, fall back to the bet's
            odds = float(sel.get("price") or b.get("price") or 0)
        except (TypeError, ValueError):
            odds = 0.0
        return {
            "bet_id":       str(b.get("id") or ""),
            "selection_id": token,
            "stake":        stake,
            "odds":         odds,
            "status":       (b.get("status") or sel.get("status") or "").lower(),
            "outcome":      (b.get("outcome") or "").lower(),
            "win_loss":     b.get("winLoss"),
            "to_win":       b.get("toWin"),
            "created_at":   b.get("createdAt"),
            "settled_at":   b.get("settledAt"),
        }

    async def _fetch_bets(self, status: str, days: int = 35) -> list[dict]:
        """GET /bets?status=…&startDate=…&endDate=… — the same call the My Bets page makes."""
        now = datetime.now(timezone.utc)
        path = (f"/bets?status={status}"
                f"&startDate={self._iso_z(now - timedelta(days=days))}"
                f"&endDate={self._iso_z(now)}")
        data = await self._http_get(path)
        if not isinstance(data, dict):
            return []
        out = []
        for b in (data.get("bets") or []):
            m = self._map_bet(b)
            if m:
                out.append(m)
        return out

    async def _bets_via_page(self) -> Optional[list[dict]]:
        """Read My Bets the way a PERSON does: navigate a tab to the account/bets page and capture the page's
        OWN `GET /0.1/bets` response. One request, fired by the site itself, perfectly correlated with a real
        page load — instead of a lone off-page API call that no UI action explains.

        Returns None (not []) when the browser route is unavailable/failed, so the caller can fall back to REST
        rather than mistake a page problem for 'no open bets'. Skipped while a bet is in flight — never navigate
        tabs mid-placement."""
        if self._browser is None or self._bet_lock.locked():
            return None
        # ⚠ NOT WHILE CAMPING. This opens (or re-navigates) a separate account/bets tab and brings it up.
        # `_bet_lock` guards the pre-live placement path, but camp_fire does NOT take that lock, so during
        # an in-play press this could fire concurrently — which is what put the open-bets tab on screen
        # right after a fire (2026-08-19), with nothing on it because the bet had not landed yet.
        #
        # Two things break if it runs: the receipt watcher is sampling the CAMPED page's betslip column and
        # a foreground tab switch is exactly the disturbance it cannot tolerate, and in-play mode exists to
        # keep ONE tab still. The caller treats None as "no reading", which is the safe answer — it falls
        # back to the in-memory fill record rather than inventing a position.
        if getattr(self, "_camping", False) or self.mode == "inplay":
            return None
        captured: dict = {}

        def _on_resp(resp):
            try:                                        # the page's own listing call (GET, /0.1/bets?…)
                if "/bets?" in resp.url and "/0.1/bets" in resp.url and resp.request.method == "GET":
                    captured["resp"] = resp
            except Exception:
                pass

        page = None
        try:
            page = self._bets_page
            if page is None or page.is_closed():
                page = await self._browser.open_tab(BETS_URL)
                self._bets_page = page
                if page is None:
                    return None
                page.on("response", _on_resp)
            else:
                page.on("response", _on_resp)
                await self._browser.navigate_tab(page, BETS_URL)   # re-navigate = the user refreshing their bets
            for _ in range(40):                          # the SPA fetches within a couple of seconds
                await asyncio.sleep(0.25)
                if "resp" in captured:
                    break
            if "resp" not in captured:
                return None
            data = await captured["resp"].json()
            if not isinstance(data, dict):
                return None
            out = [m for m in (self._map_bet(b) for b in (data.get("bets") or [])) if m]
            print(f"[PINNACLE] My Bets read via the PAGE ({len(out)} unsettled) — no off-page API call.")
            return out
        except Exception as e:
            print(f"[PINNACLE] My Bets page read failed ({type(e).__name__}: {e}) — falling back to REST.")
            return None
        finally:
            if page is not None:
                try:
                    page.remove_listener("response", _on_resp)
                except Exception:
                    pass

    async def open_bets(self) -> list[dict]:
        """UNSETTLED bets = the real Pinnacle position, for crash recovery + reconcile. This is the book-side
        answer to 'what do I actually hold?' — the counterpart of Kalshi's GetPositionsAsync. Until this existed
        the adapter returned [] ('flat'), so the executor could only trust its own in-memory fill record.

        PAGE-FIRST: `/0.1/bets` is only ever fired by the My Bets page, so we navigate there and read the site's
        own response (HARDVEN_BETS_VIA_PAGE=0 to force the direct REST call instead). REST is the fallback when
        the browser route isn't available — still correct, just less well-correlated traffic.

        Returns [] on any error, which the C# client reads as flat — the SAFE reading (reconcile then falls back
        to the in-memory record rather than believing a phantom position)."""
        if self._session_source == "browser" and not self._session_ready:
            return []
        try:
            if self._bets_via_page_on:
                via_page = await self._bets_via_page()
                if via_page is not None:
                    return via_page
            return await self._fetch_bets("unsettled")
        except Exception as e:
            print(f"[PINNACLE] open_bets failed ({type(e).__name__}: {e}) — reporting flat.")
            return []

    async def find_bet(self, selection_id: str, since_iso: str = "") -> Optional[dict]:
        """Find the bet placed on `selection_id` at/after `since_iso` — SETTLED first, since this is called when
        a position resolves. Lets the bot learn how its Pinnacle leg actually finished (win / loss / **VOID**)
        without having to have threaded a bet id through the whole execution path.

        Unambiguous in practice: the executor enforces one open position per pair plus a 120s per-pair cooldown,
        so two bets on the SAME selection inside one settlement window can't happen. A 5-minute grace before
        `since_iso` absorbs clock skew between the bot's entry timestamp and Pinnacle's createdAt."""
        sid = str(selection_id or "")
        if not sid or (self._session_source == "browser" and not self._session_ready):
            return None
        cutoff = None
        if since_iso:
            try:
                cutoff = datetime.fromisoformat(str(since_iso).replace("Z", "+00:00")) - timedelta(minutes=5)
            except Exception:
                cutoff = None
        try:
            best = None
            for status in ("settled", "unsettled"):
                for b in await self._fetch_bets(status):
                    if b.get("selection_id") != sid:
                        continue
                    if cutoff is not None:
                        try:
                            created = datetime.fromisoformat(str(b.get("created_at") or "").replace("Z", "+00:00"))
                            if created < cutoff:
                                continue
                        except Exception:
                            pass
                    # newest wins if several somehow match
                    if best is None or str(b.get("created_at") or "") > str(best.get("created_at") or ""):
                        best = b
                if best is not None:
                    return best
        except Exception as e:
            print(f"[PINNACLE] find_bet({sid}) failed ({type(e).__name__}: {e})")
        return None

    async def bet(self, bet_id: str) -> Optional[dict]:
        """One bet by id — confirmation right after placing, and settlement later. Checks UNSETTLED first (the
        common case just after a bet), then SETTLED so a resolved bet still resolves to its outcome."""
        if not bet_id or (self._session_source == "browser" and not self._session_ready):
            return None
        want = str(bet_id)
        try:
            # UNSETTLED first via the normal open_bets() route (page-first, so no off-page call) — that's the
            # common case: confirming a bet moments after placing it. Only a SETTLED lookup needs direct REST,
            # since the settled view is behind a different page filter.
            for b in await self.open_bets():
                if b.get("bet_id") == want:
                    return b
            for b in await self._fetch_bets("settled"):
                if b.get("bet_id") == want:
                    return b
        except Exception as e:
            print(f"[PINNACLE] bet({bet_id}) failed ({type(e).__name__}: {e})")
        return None
