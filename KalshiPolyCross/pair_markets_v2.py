#!/usr/bin/env python3
"""
pair_markets_v2.py - Multi-signal Kalshi <-> Polymarket market discovery pipeline.

A superset of pair_markets.py (v1). v1 found pairs with a single signal (sentence-transformer
embeddings + cosine threshold) and had a silent recall problem: markets that should pair but
scored below the threshold never appeared as candidates and were invisible (e.g. the Brazil/Lula
presidential case). v2 keeps the embedding path as one signal among several and adds:

  Stage 1  Unified MarketRecord index over both venues (+ gzip persistence)
  Stage 2  Multi-signal search: keyword-AND, BM25, phrase, entity (spaCy NER), date, category, regex
  Stage 3  twin_search orchestrator (weighted multi-signal candidate generation w/ provenance)
  Stage 4  Coverage reporting (per-event, silent misses, by-category, daily snapshot)
  Stage 5  Enhanced manual review (/ free-text  t twin  c coverage  e event-pairing)
  Stage 6  LLM judge with confidence-based routing (auto-accept / auto-reject / human queue)
  Stage 7  Daily refresh pipeline, synonyms, search-history cache, notify summary

This file is SELF-CONTAINED: it copies the fetch/judge/save helpers it needs from v1 rather than
importing pair_markets.py, so the two can evolve independently.

Extra dependencies beyond v1:
    pip install rank-bm25 spacy
    python -m spacy download en_core_web_lg
Both are lazy-loaded - coverage / keyword search work without them; an entity search prints a
clear install hint if the model is missing.

Usage:
    python pair_markets_v2.py                       # default: build index -> embedding candidates -> judge (== v1)
    python pair_markets_v2.py --rebuild-index       # Stage 1: force rebuild unified index and exit
    python pair_markets_v2.py --find "lula brazil" --venue poly   # Stage 2/3: search, print ranked
    python pair_markets_v2.py --twin KXBRAZILPRES-26-LULA         # Stage 3: twin search, two-column
    python pair_markets_v2.py --coverage [--snapshot]            # Stage 4: coverage report
    python pair_markets_v2.py --event KXBRAZILPRES-26           # Stage 5: event-level pairing
    python pair_markets_v2.py --discover [--n N]               # Stage 6: batch discovery + routing
    python pair_markets_v2.py --daily                         # Stage 7: full refresh pipeline
    python pair_markets_v2.py --clean [--dry-run]            # prune concluded Kalshi markets -> archive to closed_markets.jsonl
    python pair_markets_v2.py --manual-judge                 # Stage 5: enhanced manual review
  plus all v1 filter flags: --no-cache --dry-run --include --exclude --include-category
    --exclude-category --no-live --wN --n --sync --ollama --verbose-judge --show-prompt
"""

import argparse, base64, gzip, json, os, re, subprocess, sys, time
from dataclasses import dataclass, field


def _load_dotenv(*dirs):
    for d in dirs:
        p = os.path.join(d, ".env")
        if not os.path.isfile(p):
            continue
        with open(p) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("export "):
                    line = line[7:].strip()
                if "=" not in line:
                    continue
                k, _, v = line.partition("=")
                k = k.strip(); v = v.strip().strip('"').strip("'")
                if k and k not in os.environ:
                    os.environ[k] = v
        return


_sd = os.path.dirname(os.path.abspath(__file__))
_load_dotenv(_sd, os.path.dirname(_sd), os.path.expanduser("~"), os.getcwd())
from datetime import datetime, timezone, timedelta
from itertools import groupby
from pathlib import Path

import numpy as np
import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from openai import OpenAI, APIStatusError

# -- Constants -----------------------------------------------------------------
KALSHI_BASE_URL   = "https://api.elections.kalshi.com/trade-api/v2"
POLY_GAMMA_URL    = "https://gamma-api.polymarket.com"
KALSHI_CATEGORY   = ""
POLY_CATEGORY     = ""

SIMILARITY_THRESH = 0.78
TOP_N_CANDIDATES  = 5
DATE_WINDOW_DAYS  = 7

JUDGE_BATCH_SIZE   = 10
JUDGE_DELAY_S      = 5
OPENROUTER_MODEL   = "anthropic/claude-haiku-4.5"
OPENROUTER_HEADERS = {
    "HTTP-Referer": "https://github.com/QuantNightShift",
    "X-Title": "Arbitrage Judge",
}

OLLAMA_URL        = "http://localhost:11434/api/chat"
OLLAMA_MODEL      = "qwen3:8b"
OLLAMA_BATCH_SIZE = 1

SCP_REMOTE        = "jonsi@35.245.182.71:~/PredictionBacktester/KalshiPolyCross/"

SCRIPT_DIR        = Path(__file__).parent
CACHE_PATH        = SCRIPT_DIR / "embeddings_cache_bge.json"
DEFAULT_OUTPUT    = SCRIPT_DIR / "cross_pairs.json"

# v2-specific paths
INDEX_PATH        = SCRIPT_DIR / "unified_index.json.gz"
INDEX_MAX_AGE_H   = 24
COVERAGE_HISTORY  = SCRIPT_DIR / "coverage_history.json"
REVIEW_QUEUE      = SCRIPT_DIR / "review_queue.json"
JUDGE_COST_PATH   = SCRIPT_DIR / "judge_cost.json"
RESOLUTION_CACHE  = SCRIPT_DIR / "resolution_cache.json"

# Twin-search signal weights (Stage 3)
W_BM25     = 0.40
W_ENTITY   = 0.35
W_DATE     = 0.15
W_CATEGORY = 0.10
MIN_DISCOVERY_THRESHOLD = 0.30   # twin combined-score floor for Stage 6 batch discovery
AUTO_CONFIDENCE         = 0.95   # confidence at/above which Stage 6 auto-routes
JUDGE_DAILY_COST_CAP    = 5.00   # USD; batch discovery aborts past this

# Resolution endpoints for the Stage 6 audit (mirrors prod_cross_arb.py)
KALSHI_REST = KALSHI_BASE_URL
POLY_GAMMA  = POLY_GAMMA_URL


# -- Kalshi auth ---------------------------------------------------------------
def _kalshi_headers(method: str, path: str, api_key_id: str, private_key) -> dict:
    ts_ms = str(int(time.time() * 1000))
    msg   = (ts_ms + method.upper() + path).encode()
    sig   = private_key.sign(
        msg,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=hashes.SHA256().digest_size),
        hashes.SHA256(),
    )
    return {
        "KALSHI-ACCESS-KEY":       api_key_id,
        "KALSHI-ACCESS-TIMESTAMP": ts_ms,
        "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode(),
    }


def _kalshi_get(path: str, api_key_id: str, private_key, params=None, _label: str = "") -> dict:
    for attempt in range(1, 6):
        r = requests.get(
            KALSHI_BASE_URL + path,
            headers=_kalshi_headers("GET", path, api_key_id, private_key),
            params=params,
            timeout=30,
        )
        label = f" ({_label})" if _label else ""
        print(f"[KALSHI] GET {path[:60]}{label} status={r.status_code} attempt={attempt}", flush=True)
        if r.status_code == 429:
            wait = int(r.headers.get("retry-after", 10))
            print(f"[KALSHI] Rate limited (429) - waiting {wait}s before retry...", flush=True)
            time.sleep(wait)
            continue
        r.raise_for_status()
        return r.json()
    r.raise_for_status()


def _safe_replace(tmp: Path, target: Path) -> None:
    """Atomic replace with a fallback copy for Windows file-lock edge cases."""
    try:
        tmp.replace(target)
    except PermissionError:
        target.write_bytes(tmp.read_bytes())
        tmp.unlink(missing_ok=True)


# -- Embedding cache ------------------------------------------------------------
def load_cache() -> dict:
    if not CACHE_PATH.exists():
        return {}
    try:
        return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"[CACHE] Warning: could not load ({e}). Starting fresh.")
        return {}


def save_cache(cache: dict) -> None:
    try:
        tmp = CACHE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(cache, separators=(",", ":")), encoding="utf-8")
        _safe_replace(tmp, CACHE_PATH)
        print(f"[CACHE] Saved - {len(cache)} entries.")
    except Exception as e:
        print(f"[CACHE] Warning: could not save ({e}).")


# -- Phase 1: Fetch markets (enriched for the unified index) --------------------
def fetch_kalshi_markets(api_key_id: str, private_key) -> dict:
    """ticker -> {title, close_date, rules, event_ticker, category, yes_sub_title, no_sub_title,
                  event_title, series_title, siblings, is_neg_risk}."""
    print("[KALSHI] Fetching series categories...")
    series_cats, series_titles = {}, {}
    cursor = ""
    series_page = 0
    while True:
        series_page += 1
        path = "/series?limit=1000" + (f"&cursor={cursor}" if cursor else "")
        data = _kalshi_get(path, api_key_id, private_key, _label=f"series page={series_page}")
        page_series = data.get("series", [])
        for s in page_series:
            tk = s.get("ticker")
            if tk:
                if s.get("category"):
                    series_cats[tk] = s["category"]
                if s.get("title"):
                    series_titles[tk] = s["title"]
        cursor = data.get("cursor", "")
        print(f"[KALSHI] series page={series_page} series_in_page={len(page_series)} cursor={'yes' if cursor else 'none'} total_so_far={len(series_cats)}", flush=True)
        if not cursor:
            print(f"[KALSHI] Series pagination complete.", flush=True)
            break
        time.sleep(0.15)
    print(f"[KALSHI] {len(series_cats)} series categories loaded.")

    print("[KALSHI] Fetching open events...")
    markets = {}
    cursor = ""
    total_events = 0
    events_page = 0
    while True:
        events_page += 1
        path = "/events?status=open&with_nested_markets=true&limit=200" + (f"&cursor={cursor}" if cursor else "")
        data = _kalshi_get(path, api_key_id, private_key, _label=f"events page={events_page}")
        for ev in data.get("events", []):
            total_events += 1
            series_ticker = ev.get("series_ticker", "")
            cat = series_cats.get(series_ticker, "")
            if KALSHI_CATEGORY and cat.lower() != KALSHI_CATEGORY.lower():
                continue
            event_ticker = ev.get("event_ticker", "")
            event_title  = ev.get("title", "") or ev.get("sub_title", "") or ""
            ev_markets   = ev.get("markets", [])
            siblings     = [(m.get("yes_sub_title") or m.get("title") or "") for m in ev_markets]
            is_neg_risk  = len(ev_markets) > 1
            for m in ev_markets:
                ticker = m.get("ticker", "")
                if not ticker:
                    continue
                close_date = None
                for fld in ("expected_expiration_time", "close_time"):
                    val = m.get(fld)
                    if val:
                        try:
                            close_date = datetime.fromisoformat(val.replace("Z", "+00:00"))
                            break
                        except Exception:
                            pass
                markets[ticker] = {
                    "ticker":        ticker,
                    "title":         m.get("title", ""),
                    "close_date":    close_date,
                    "rules":         m.get("rules_primary", ""),
                    "event_ticker":  event_ticker,
                    "category":      cat,
                    "yes_sub_title": m.get("yes_sub_title", ""),
                    "no_sub_title":  m.get("no_sub_title",  ""),
                    "event_title":   event_title,
                    "series_ticker": series_ticker,
                    "series_title":  series_titles.get(series_ticker, ""),
                    "siblings":      siblings,
                    "is_neg_risk":   is_neg_risk,
                }
        cursor = data.get("cursor", "")
        if not cursor:
            break
        time.sleep(0.15)
    print(f"[KALSHI] {total_events} events -> {len(markets)} markets in '{KALSHI_CATEGORY}'.")
    return markets


def fetch_poly_markets(no_live: bool = False) -> list:
    """list of {question, yes_token, no_token, end_date, description, outcomes, neg_risk,
               order_min_size, event_title, event_slug, group_item_title, siblings, category}."""
    print("[POLY] Fetching active markets...")
    results = []
    skipped_live = 0
    offset, page_size = 0, 100
    page = 0
    while True:
        page += 1
        url = f"{POLY_GAMMA_URL}/events?active=true&closed=false&limit={page_size}&offset={offset}"
        for attempt in range(1, 6):
            r = requests.get(url, timeout=30)
            print(f"[POLY] page={page} offset={offset} status={r.status_code} attempt={attempt}", flush=True)
            if r.status_code == 429:
                wait = int(r.headers.get("retry-after", 10))
                print(f"[POLY] Rate limited (429) - waiting {wait}s before retry...", flush=True)
                time.sleep(wait)
                continue
            r.raise_for_status()
            break
        arr = r.json()
        if not isinstance(arr, list):
            break
        for ev in arr:
            include = not POLY_CATEGORY
            if not include:
                for tag in ev.get("tags", []):
                    if (POLY_CATEGORY.lower() in tag.get("slug",  "").lower() or
                        POLY_CATEGORY.lower() in tag.get("label", "").lower()):
                        include = True
                        break
                if not include and POLY_CATEGORY.lower() in ev.get("category", "").lower():
                    include = True
            if not include:
                continue
            ev_live         = bool(ev.get("live", False))
            ev_end_date_raw = ev.get("end_date")
            ev_neg_risk     = bool(ev.get("negRisk", False))
            description     = ev.get("description", "") or ""
            event_title     = ev.get("title", "") or ""
            event_slug      = ev.get("slug", "") or ""
            ev_category     = derive_category_from_tags(ev.get("tags", []))
            ev_markets      = ev.get("markets", [])
            siblings        = [(m.get("groupItemTitle") or m.get("question") or "") for m in ev_markets]
            for mkt in ev_markets:
                question = mkt.get("question", "")
                raw = mkt.get("clobTokenIds", [])
                if isinstance(raw, str):
                    try: raw = json.loads(raw)
                    except Exception: raw = []
                tokens = [t for t in raw if t]
                if len(tokens) < 2 or not question:
                    continue
                if no_live:
                    seconds_delay = int(mkt.get("secondsDelay", 0) or 0)
                    mkt_live      = bool(mkt.get("live", False))
                    if ev_live or mkt_live or seconds_delay > 0:
                        skipped_live += 1
                        continue
                end_date_raw = mkt.get("endDate") or ev_end_date_raw
                end_date = None
                if end_date_raw:
                    try:
                        end_date = datetime.fromisoformat(str(end_date_raw).replace("Z", "+00:00"))
                    except Exception:
                        pass
                raw_outcomes = mkt.get("outcomes", "[]")
                if isinstance(raw_outcomes, str):
                    try:    outcomes = json.loads(raw_outcomes)
                    except: outcomes = []
                else:
                    outcomes = raw_outcomes if isinstance(raw_outcomes, list) else []
                results.append({
                    "question":         question,
                    "yes_token":        tokens[0],
                    "no_token":         tokens[1],
                    "end_date":         end_date,
                    "description":      description,
                    "outcomes":         outcomes,
                    "neg_risk":         ev_neg_risk,
                    "order_min_size":   float(mkt.get("orderMinSize") or 0) or 1.0,
                    "event_title":      event_title,
                    "event_slug":       event_slug,
                    "group_item_title": mkt.get("groupItemTitle", "") or "",
                    "siblings":         siblings,
                    "category":         ev_category,
                })
        print(f"[POLY] page={page} events_in_page={len(arr)} markets_so_far={len(results)}", flush=True)
        if len(arr) == 0:
            print(f"[POLY] Empty page - pagination complete.", flush=True)
            break
        offset += page_size
        time.sleep(0.2)
    if no_live and skipped_live:
        print(f"[POLY] Skipped {skipped_live} live/delayed markets (--no-live).")
    print(f"[POLY] {len(results)} markets fetched.")
    return results


# -- Category normalization ----------------------------------------------------
_POLY_TAG_CATEGORY = {
    "politics": "Politics", "elections": "Politics", "us-election": "Politics", "election": "Politics",
    "geopolitics": "World", "world": "World", "foreign-policy": "World",
    "sports": "Sports", "nfl": "Sports", "nba": "Sports", "soccer": "Sports", "epl": "Sports",
    "mlb": "Sports", "nhl": "Sports", "football": "Sports", "tennis": "Sports", "golf": "Sports",
    "crypto": "Crypto", "bitcoin": "Crypto", "ethereum": "Crypto", "defi": "Crypto",
    "economics": "Economics", "econ": "Economics", "fed": "Economics", "inflation": "Economics",
    "business": "Economics", "finance": "Economics",
    "pop-culture": "Entertainment", "entertainment": "Entertainment", "movies": "Entertainment",
    "science": "Science", "tech": "Tech", "ai": "Tech", "technology": "Tech",
    "weather": "Weather", "climate": "Weather",
}

_CATEGORY_ALIASES = {
    "politics": "Politics", "us politics": "Politics", "elections": "Politics",
    "world": "World", "geopolitics": "World",
    "sports": "Sports", "economics": "Economics", "financials": "Economics", "economy": "Economics",
    "crypto": "Crypto", "cryptocurrency": "Crypto", "science and technology": "Tech",
    "technology": "Tech", "science": "Science", "entertainment": "Entertainment",
    "climate and weather": "Weather", "weather": "Weather",
}


def derive_category_from_tags(tags) -> str:
    """Map Polymarket event tags (list of {slug,label}) to a canonical category."""
    for tag in tags or []:
        for fld in ("slug", "label"):
            key = (tag.get(fld, "") or "").lower()
            if key in _POLY_TAG_CATEGORY:
                return _POLY_TAG_CATEGORY[key]
    return ""


def _normalize_category(cat: str) -> str:
    """Collapse Kalshi/Poly category vocabularies to one comparable canonical form."""
    if not cat:
        return ""
    c = cat.strip().lower()
    if c in _CATEGORY_ALIASES:
        return _CATEGORY_ALIASES[c]
    # Already canonical (e.g. 'Politics') or unknown - title-case it for stable comparison.
    return cat.strip().title()


# -- Phase 2: Embedding candidates (one signal of several; copied from v1) ------
def find_candidates(kalshi_markets: dict, poly_markets: list, already_paired: set, use_cache: bool) -> list:
    cache = load_cache() if use_cache else {}

    filtered_poly   = poly_markets
    k_titles_unique = list({m["title"] for m in kalshi_markets.values()})
    print(f"[EMBED] {len(k_titles_unique)} unique Kalshi titles, {len(filtered_poly)} Poly markets.")

    poly_questions = [p["question"] for p in filtered_poly]
    to_encode = [t for t in dict.fromkeys(k_titles_unique + poly_questions) if t not in cache]

    if to_encode:
        from sentence_transformers import SentenceTransformer
        print(f"[EMBED] Loading model BAAI/bge-large-en-v1.5 ...")
        model = SentenceTransformer("BAAI/bge-large-en-v1.5")
        print(f"[EMBED] Encoding {len(to_encode)} texts...")
        vecs = model.encode(to_encode, batch_size=256, show_progress_bar=True, normalize_embeddings=True)
        for text, vec in zip(to_encode, vecs):
            cache[text] = vec.tolist()
        save_cache(cache)
    else:
        print(f"[EMBED] All texts served from cache ({len(cache)} entries).")

    poly_vecs, poly_valid = [], []
    for p in filtered_poly:
        if p["question"] in cache:
            poly_vecs.append(cache[p["question"]])
            poly_valid.append(p)
    if not poly_vecs:
        print("[EMBED] No Polymarket embeddings - no candidates.")
        return []
    poly_mat = np.array(poly_vecs, dtype=np.float32)

    k_tickers, k_vecs = [], []
    for ticker, info in kalshi_markets.items():
        if ticker in already_paired:
            continue
        vec = cache.get(info["title"])
        if vec is not None:
            k_tickers.append((ticker, info))
            k_vecs.append(vec)
    if not k_tickers:
        return []

    k_mat      = np.array(k_vecs, dtype=np.float32)
    CHUNK      = 500
    total_k    = len(k_tickers)
    candidates = []
    print(f"[EMBED] Scoring {total_k} Kalshi tickers against {len(poly_valid)} Poly markets...")

    for chunk_start in range(0, total_k, CHUNK):
        chunk_end  = min(chunk_start + CHUNK, total_k)
        chunk_k    = k_mat[chunk_start:chunk_end]
        scores_mat = poly_mat @ chunk_k.T

        for ki in range(chunk_end - chunk_start):
            ticker, info = k_tickers[chunk_start + ki]
            col  = scores_mat[:, ki]
            hits = np.where(col >= SIMILARITY_THRESH)[0]
            for idx in hits:
                p  = poly_valid[idx]
                kd, pd = info["close_date"], p["end_date"]
                if kd and pd:
                    if kd.tzinfo is None: kd = kd.replace(tzinfo=timezone.utc)
                    if pd.tzinfo is None: pd = pd.replace(tzinfo=timezone.utc)
                    k_years = set(re.findall(r'\b20\d{2}\b', info["title"]))
                    p_years = set(re.findall(r'\b20\d{2}\b', p["question"]))
                    if k_years and p_years and k_years == p_years:
                        effective_window = 366  # shared year token proves same event cycle
                    elif info.get("category") in ("Politics", "Elections"):
                        effective_window = 30
                    else:
                        effective_window = DATE_WINDOW_DAYS
                    if abs((kd - pd).total_seconds()) / 86400 > effective_window:
                        continue
                candidates.append(_candidate_dict(ticker, info, p, float(col[idx])))

        print(f"[EMBED] {chunk_end}/{total_k} tickers scored, {len(candidates)} raw hits so far...", flush=True)

    candidates.sort(key=lambda c: (c["kalshi_ticker"], -c["score"]))
    top = []
    for _ticker, group in groupby(candidates, key=lambda c: c["kalshi_ticker"]):
        top.extend(list(group)[:TOP_N_CANDIDATES])
    top.sort(key=lambda c: -c["score"])
    print(f"[EMBED] {len(top)} candidate pairs (threshold={SIMILARITY_THRESH}, top-{TOP_N_CANDIDATES}/ticker).")
    return top


def _candidate_dict(ticker: str, info: dict, p: dict, score: float) -> dict:
    """Assemble the candidate shape that the judge + save functions expect."""
    return {
        "kalshi_ticker":       ticker,
        "kalshi_title":        info["title"],
        "kalshi_close":        info["close_date"],
        "kalshi_rules":        info["rules"],
        "kalshi_event":        info["event_ticker"],
        "kalshi_category":     info.get("category", ""),
        "kalshi_yes_sub":      info.get("yes_sub_title", ""),
        "kalshi_no_sub":       info.get("no_sub_title", ""),
        "poly_question":       p["question"],
        "poly_yes":            p["yes_token"],
        "poly_no":             p["no_token"],
        "poly_close":          p["end_date"],
        "poly_desc":           p["description"],
        "poly_outcomes":       p.get("outcomes", []),
        "is_neg_risk":         p.get("neg_risk", False),
        "poly_order_min_size": p.get("order_min_size", 1.0),
        "score":               score,
    }


# -- Phase 3: OpenRouter judge (Claude via OpenRouter) --------------------------
_JUDGE_SYSTEM = """\
You are a Lead Quantitative Risk Analyst at a high-frequency trading firm.
Evaluate whether two prediction markets describe the EXACT SAME underlying event for arbitrage purposes.
Be mathematically ruthless on real divergences, but do not invent implausible edge cases.

When in doubt between VALID and CONDITIONAL, prefer VALID. CONDITIONAL requires a specific, named divergence mechanism - not a general unease about timing.

### VERDICT CATEGORIES:
1. **VALID** - Core event, threshold, direction, and resolution methodology are functionally identical.
   - *Trading close timing is irrelevant:* Different order-book close times do not affect arbitrage validity - only resolution timing matters. If both platforms resolve on the same underlying event outcome, ignore differences in when trading stops.
   - *Date/Time leeway:* Different listed close dates or intraday time differences are completely OK. Treat as VALID if the real-world event has a single definitive date.
   - *Oracle strictness depends on data type:*
     - Macro/consensus events (elections, court rulings, awards): different tier-1 sources (AP, NYT, Fox) are equivalent.
     - Volatile/sensor data (weather, crypto prices, API feeds): oracles must be IDENTICAL. NOAA != AccuWeather. Mark INVALID if they differ.
   - *Numerical thresholds must match exactly:* $100,000 != $100,001. "Top 5" != "Top 6". Round numbers are not interchangeable with exact figures.

2. **INVERTED** - Same event and thresholds, phrased as exact opposites. Tradeable as YES/YES across venues.
   - *Dead-middle check:* Boundaries must not leave a gap. "> 1.0" vs "< 1.0" leaves 1.0 resolving both to NO -> INVALID, not INVERTED.

3. **INVALID** - A lethal trap exists:
   - FORMULA_MISMATCH (e.g., Nominal vs Real GDP)
   - INVERSION_GAP (dead-middle boundary gap)
   - OVERTIME_MISMATCH (regulation-only vs includes-OT)
   - DEAD_HEAT_MISMATCH (mathematical split vs alphabetical tiebreak)
   - DEADLINE_MISMATCH (different measurement windows: one month vs full year)
   - CANCELLATION_MISMATCH (one venue voids early on a trigger, the other forces hold-to-term)
   - OTHER (use this if the failure mode doesn't fit above; describe in `explanation`)

4. **CONDITIONAL** - Same core event, but a CONCRETE divergence in how the contracts pay out introduces real risk:
   - Asynchronous *resolution* (not trading close): one venue settles hours/days before the other, exposing you to dispute or revision risk in the gap.
   - Asymmetric early-void triggers that fire on one venue but not the other.
   - Settlement reference time differs (e.g., one uses 4pm UTC close, the other uses end-of-day local).

   Do NOT mark CONDITIONAL for: trading close time differences, slightly different listed close dates when the underlying event is fixed, or theoretical edge cases without a clear divergence mechanism.

### OUTPUT:
Return ONLY a JSON array with one object per input pair, in any order. Echo the `index` field exactly so verdicts can be matched to inputs. No markdown fences, no preamble.

{
  "index": <int - echo from input exactly>,
  "status": "VALID" | "INVERTED" | "INVALID" | "CONDITIONAL",
  "confidence": <float 0.0-1.0 - your calibrated probability that this status is correct>,
  "trap_type": "NONE" | "FORMULA_MISMATCH" | "INVERSION_GAP" | "OVERTIME_MISMATCH" | "DEAD_HEAT_MISMATCH" | "DEADLINE_MISMATCH" | "CANCELLATION_MISMATCH" | "OTHER",
  "earliest_safe_exit_date": "<YYYY-MM-DD if CONDITIONAL or DEADLINE_MISMATCH; null otherwise>",
  "reasoning": "<step-by-step over whichever of these apply: core event identity, oracle source, numerical boundaries, dates/timing, tie-breakers/cancellations. Skip irrelevant steps.>",
  "explanation": "<one technical sentence summarizing the verdict>"
}

Markets are wrapped in <kalshi> and <polymarket> tags in the user message.
"""


def _build_user_prompt(batch: list) -> str:
    parts = []
    for i, c in enumerate(batch):
        kc = c["kalshi_close"].isoformat() if c["kalshi_close"] else "Unknown"
        pc = c["poly_close"].isoformat()   if c["poly_close"]   else "Unknown"
        parts.append(
            f'<pair index="{i}">\n'
            f'<kalshi>\n'
            f'Title: {c["kalshi_title"]}\n'
            f'Close: {kc}\n'
            f'Rules: {(c["kalshi_rules"] or "")[:300]}\n'
            f'</kalshi>\n'
            f'<polymarket>\n'
            f'Title: {c["poly_question"]}\n'
            f'Close: {pc}\n'
            f'Desc:  {(c["poly_desc"] or "")[:300]}\n'
            f'</polymarket>\n'
            f'</pair>'
        )
    return "\n\n".join(parts)


def _build_prompt(batch: list) -> str:
    """Combined system+user prompt for Ollama/dry-run (single-message APIs)."""
    return _JUDGE_SYSTEM + "\n" + _build_user_prompt(batch)


def _parse_judge_response(text: str, batch_size: int) -> list:
    """Parse the judge JSON array response. Returns list of verdict dicts (with confidence)."""
    if text.startswith("```"):
        nl = text.find("\n")
        text = text[nl + 1:] if nl != -1 else text[3:]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()
    if not text.startswith("[") and not text.startswith("{"):
        m = re.search(r"[\[{]", text)
        text = text[m.start():] if m else "[]"
    try:
        verdicts = json.loads(text)
        if isinstance(verdicts, dict):
            verdicts = [verdicts]
    except json.JSONDecodeError:
        verdicts = []
        for obj_text in re.findall(r'\{[^{}]+\}', text, re.DOTALL):
            try:
                verdicts.append(json.loads(obj_text))
            except json.JSONDecodeError:
                pass
        if verdicts:
            print(f"[JUDGE] Partial response - salvaged {len(verdicts)} verdict(s).")
    result = []
    for v in verdicts:
        if not isinstance(v, dict):
            continue
        idx = v.get("index")
        status = str(v.get("status", "INVALID")).upper()
        if not isinstance(idx, int) or not (0 <= idx < batch_size):
            continue
        if status not in ("VALID", "INVALID", "CONDITIONAL", "INVERTED"):
            status = "INVALID"
        try:
            conf = float(v.get("confidence", 0.5))
        except (TypeError, ValueError):
            conf = 0.5
        conf = max(0.0, min(1.0, conf))
        result.append({
            "index":                   idx,
            "status":                  status,
            "confidence":              conf,
            "trap_type":               v.get("trap_type", "NONE"),
            "safe_hours_before_event": v.get("safe_hours_before_event", 0),
            "earliest_cutoff_date":    v.get("earliest_cutoff_date", v.get("earliest_safe_exit_date", "NONE")),
            "reasoning":               v.get("reasoning", ""),
            "explanation":             v.get("explanation", ""),
        })
    return result


_MAX_RETRIES = 3


class BalanceExhaustedError(Exception):
    pass


class JudgeFailedError(Exception):
    pass


def _record_judge_cost(usage) -> None:
    """Accumulate token usage into judge_cost.json keyed by UTC date (Stage 6 cost tracking)."""
    if usage is None:
        return
    try:
        pt = int(getattr(usage, "prompt_tokens", 0) or 0)
        ct = int(getattr(usage, "completion_tokens", 0) or 0)
    except Exception:
        return
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    data = {}
    if JUDGE_COST_PATH.exists():
        try:    data = json.loads(JUDGE_COST_PATH.read_text(encoding="utf-8"))
        except Exception: data = {}
    rec = data.get(day, {"prompt_tokens": 0, "completion_tokens": 0, "calls": 0})
    rec["prompt_tokens"]     += pt
    rec["completion_tokens"] += ct
    rec["calls"]             += 1
    data[day] = rec
    try:
        JUDGE_COST_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception:
        pass


def _judge_batch(batch: list, openrouter_key: str, verbose: bool = False) -> list:
    """Returns list of verdict dicts (one per evaluated pair, INVALID omitted)."""
    client = OpenAI(base_url="https://openrouter.ai/api/v1", api_key=openrouter_key)

    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            response = client.chat.completions.create(
                model=OPENROUTER_MODEL,
                messages=[
                    {"role": "system", "content": _JUDGE_SYSTEM},
                    {"role": "user",   "content": _build_user_prompt(batch)},
                ],
                temperature=0.0,
                max_tokens=16384,
                extra_headers=OPENROUTER_HEADERS,
            )
            _record_judge_cost(getattr(response, "usage", None))
            text = response.choices[0].message.content.strip()
            if verbose:
                print(f"\n[JUDGE RAW] model={OPENROUTER_MODEL}\n{text}\n")
            verdicts = _parse_judge_response(text, len(batch))
            counts = {"VALID": 0, "CONDITIONAL": 0, "INVALID": 0}
            for v in verdicts:
                counts[v["status"]] = counts.get(v["status"], 0) + 1
            counts["INVALID"] += len(batch) - len(verdicts)
            print(f" [V:{counts['VALID']} C:{counts['CONDITIONAL']} X:{counts['INVALID']}]", end="", flush=True)
            non_invalid = [v for v in verdicts if v["status"] != "INVALID"]
            for v in non_invalid:
                c = batch[v["index"]]
                tag = f"[{v['status']}]" + (f" trap={v['trap_type']}" if v["trap_type"] != "NONE" else "")
                print(f"\n    {tag} K: \"{c['kalshi_title']}\" <-> P: \"{c['poly_question']}\"")
                print(f"        {v['explanation']}")
            return non_invalid
        except APIStatusError as e:
            if e.status_code == 402:
                raise BalanceExhaustedError(
                    "OpenRouter balance exhausted (HTTP 402). "
                    "Top up at openrouter.ai - pairs saved so far are intact."
                )
            if e.status_code == 429:
                wait = int(e.response.headers.get("retry-after", 60))
                print(f"\n[JUDGE] Rate limited (429) - waiting {wait}s (attempt {attempt}/{_MAX_RETRIES})...")
                if attempt >= _MAX_RETRIES:
                    raise JudgeFailedError(f"Rate limited {_MAX_RETRIES} times in a row - stopping.")
                time.sleep(wait)
                continue
            print(f"\n[JUDGE] HTTP {e.status_code} attempt {attempt}/{_MAX_RETRIES}: {e.message}")
            if attempt >= _MAX_RETRIES:
                raise JudgeFailedError(f"Judge failed after {_MAX_RETRIES} attempts (HTTP {e.status_code}).")
            time.sleep(10)
        except Exception as e:
            print(f"\n[JUDGE] attempt {attempt}/{_MAX_RETRIES} failed: {e}")
            if attempt >= _MAX_RETRIES:
                raise JudgeFailedError(f"Judge failed after {_MAX_RETRIES} attempts: {e}")
            time.sleep(10)


def run_judge(candidates: list, openrouter_key: str, output_path: Path, verbose: bool = False, sync: bool = False) -> None:
    potential_path = output_path.parent / f"potential_{output_path.name}"
    total  = (len(candidates) + JUDGE_BATCH_SIZE - 1) // JUDGE_BATCH_SIZE
    print(f"[JUDGE] {len(candidates)} candidates -> {total} batch(es) of up to {JUDGE_BATCH_SIZE}")
    print(f"[JUDGE] Model: {OPENROUTER_MODEL} (via OpenRouter)")
    print(f"[JUDGE] VALID/INVERTED -> {output_path}")
    print(f"[JUDGE] CONDITIONAL    -> {potential_path}")

    for bi, i in enumerate(range(0, len(candidates), JUDGE_BATCH_SIZE)):
        batch    = candidates[i:i + JUDGE_BATCH_SIZE]
        print(f"  [Batch {bi+1}/{total}] Evaluating {len(batch)} pairs...", end="", flush=True)
        verdicts = _judge_batch(batch, openrouter_key, verbose=verbose)

        valid       = [batch[v["index"]] for v in verdicts if v["status"] == "VALID"]
        conditional = [(batch[v["index"]], v) for v in verdicts if v["status"] == "CONDITIONAL"]
        inverted    = [batch[v["index"]] for v in verdicts if v["status"] == "INVERTED"]
        print(f"  -> {len(valid)} valid, {len(conditional)} conditional, {len(inverted)} inverted.")

        if valid:
            _save_pairs(valid, output_path)
            if sync:
                _scp_sync(output_path)
        if conditional:
            _save_potential_pairs(conditional, potential_path)
        if inverted:
            _save_pairs(_swap_tokens(inverted), output_path)
            if sync:
                _scp_sync(output_path)

        if i + JUDGE_BATCH_SIZE < len(candidates):
            time.sleep(JUDGE_DELAY_S)


# -- Phase 3b: Ollama judge (local LLM) ----------------------------------------
def _judge_batch_ollama(batch: list) -> list:
    payload = {
        "model":   OLLAMA_MODEL,
        "messages": [{"role": "user", "content": _build_prompt(batch)}],
        "stream":  False,
        "think":   False,
        "options": {"temperature": 0},
    }
    try:
        r = requests.post(OLLAMA_URL, json=payload, timeout=900)
        r.raise_for_status()
        text = r.json().get("message", {}).get("content", "").strip()
        if "<think>" in text:
            end = text.find("</think>")
            text = text[end + 8:].strip() if end != -1 else text
        print(f"\n[OLLAMA]\n{text}\n")
        verdicts = _parse_judge_response(text, len(batch))
        non_invalid = [v for v in verdicts if v["status"] != "INVALID"]
        for v in non_invalid:
            c = batch[v["index"]]
            tag = f"[{v['status']}]" + (f" trap={v['trap_type']}" if v["trap_type"] != "NONE" else "")
            print(f"\n    {tag} K: \"{c['kalshi_title']}\" <-> P: \"{c['poly_question']}\"")
            print(f"        {v['explanation']}")
        return non_invalid
    except requests.exceptions.ConnectionError:
        print("\n[OLLAMA] Connection refused - is Ollama running? (ollama serve)")
        return []
    except Exception as e:
        print(f"\n[OLLAMA] Error: {e}")
        return []


_scp_proc = None


def _scp_sync(local_path: Path) -> None:
    """Fire-and-forget SCP upload; waits for any in-flight transfer to finish first."""
    global _scp_proc
    if _scp_proc is not None:
        _scp_proc.wait()
    remote = SCP_REMOTE + local_path.name
    _scp_proc = subprocess.Popen(
        ["scp", "-o", "ServerAliveInterval=60", "-o", "ConnectTimeout=10", str(local_path), remote],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    print(f"  [SCP] -> {remote}", flush=True)


def run_ollama_judge(candidates: list, output_path: Path, sync: bool = False) -> None:
    potential_path = output_path.parent / f"potential_{output_path.name}"
    total = (len(candidates) + OLLAMA_BATCH_SIZE - 1) // OLLAMA_BATCH_SIZE
    print(f"[OLLAMA] {len(candidates)} candidates -> {total} call(s) of {OLLAMA_BATCH_SIZE}")
    for bi, i in enumerate(range(0, len(candidates), OLLAMA_BATCH_SIZE)):
        batch = candidates[i:i + OLLAMA_BATCH_SIZE]
        print(f"\n{'='*70}")
        for c in batch:
            kc = c["kalshi_close"].strftime("%Y-%m-%d") if c["kalshi_close"] else "?"
            pc = c["poly_close"].strftime("%Y-%m-%d")   if c["poly_close"]   else "?"
            print(f"  [{bi+1}/{total}] score={c['score']:.3f}")
            print(f"  KALSHI  {c['kalshi_ticker']}")
            print(f"          {c['kalshi_title']}  (closes {kc})")
            print(f"  POLY    {c['poly_question']}  (closes {pc})")
        verdicts = _judge_batch_ollama(batch)

        valid       = [batch[v["index"]] for v in verdicts if v["status"] == "VALID"]
        conditional = [(batch[v["index"]], v) for v in verdicts if v["status"] == "CONDITIONAL"]
        inverted    = [batch[v["index"]] for v in verdicts if v["status"] == "INVERTED"]
        if valid:
            _save_pairs(valid, output_path)
            if sync: _scp_sync(output_path)
        if conditional:
            _save_potential_pairs(conditional, potential_path)
        if inverted:
            _save_pairs(_swap_tokens(inverted), output_path)
            if sync: _scp_sync(output_path)


# -- Output ---------------------------------------------------------------------
def _event_root(ticker: str) -> str:
    return ticker.rsplit("-", 1)[0] if "-" in ticker else ticker


def _save_pairs(matched: list, output_path: Path, source: str = "") -> None:
    """Append VALID/INVERTED pairs. Schema is the 8 keys the C# bot requires; `source` is additive."""
    existing = []
    existing_keys = set()
    if output_path.exists():
        try:
            existing = json.loads(output_path.read_text(encoding="utf-8-sig"))
            existing_keys = {f"{e['kalshi_ticker']}|{e['poly_yes_token']}".lower() for e in existing}
        except Exception as e:
            print(f"[SAVE] Warning reading existing pairs: {e}")

    added = 0
    for m in matched:
        key = f"{m['kalshi_ticker']}|{m['poly_yes']}".lower()
        if key in existing_keys:
            print(f"[SAVE] Duplicate skipped: {m['kalshi_ticker']}")
            continue
        existing_keys.add(key)
        row = {
            "kalshi_ticker":   m["kalshi_ticker"],
            "poly_yes_token":  m["poly_yes"],
            "poly_no_token":   m["poly_no"],
            "label":           m["kalshi_title"],
            "event_id":        _event_root(m["kalshi_ticker"]),
            "settlement_date": m["kalshi_close"].strftime("%Y-%m-%d") if m.get("kalshi_close") else "",
            "is_neg_risk":     m.get("is_neg_risk", False),
            "poly_min_size":   m.get("poly_order_min_size", 1.0),
        }
        if source:
            row["source"] = source   # additive only; the C# CrossPair loader ignores unknown keys
        existing.append(row)
        added += 1

    if added == 0:
        print("[SAVE] No new unique pairs.")
        return

    tmp = output_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(existing, indent=2), encoding="utf-8")
    _safe_replace(tmp, output_path)
    print(f"[SAVE] {added} new pair(s) saved to {output_path}.")


def _save_rejected(rejected: list, output_path: Path) -> None:
    existing = []
    existing_keys = set()
    if output_path.exists():
        try:
            existing = json.loads(output_path.read_text(encoding="utf-8-sig"))
            existing_keys = {f"{e['kalshi_ticker']}|{e['poly_yes_token']}".lower() for e in existing}
        except Exception as e:
            print(f"[SAVE] Warning reading rejected pairs: {e}")

    added = 0
    for m, verdict in rejected:
        key = f"{m['kalshi_ticker']}|{m['poly_yes']}".lower()
        if key in existing_keys:
            continue
        existing_keys.add(key)
        existing.append({
            "kalshi_ticker":  m["kalshi_ticker"],
            "poly_yes_token": m["poly_yes"],
            "label":          m["kalshi_title"],
            "poly_question":  m["poly_question"],
            "verdict":        verdict,
        })
        added += 1
    if added == 0:
        return
    tmp = output_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(existing, indent=2), encoding="utf-8")
    _safe_replace(tmp, output_path)


def _save_potential_pairs(conditional: list, output_path: Path) -> None:
    existing = []
    existing_keys = set()
    if output_path.exists():
        try:
            existing = json.loads(output_path.read_text(encoding="utf-8-sig"))
            existing_keys = {f"{e['kalshi_ticker']}|{e['poly_yes_token']}".lower() for e in existing}
        except Exception as e:
            print(f"[SAVE] Warning reading potential pairs: {e}")

    added = 0
    for m, verdict in conditional:
        key = f"{m['kalshi_ticker']}|{m['poly_yes']}".lower()
        if key in existing_keys:
            continue
        existing_keys.add(key)
        existing.append({
            "kalshi_ticker":           m["kalshi_ticker"],
            "poly_yes_token":          m["poly_yes"],
            "poly_no_token":           m["poly_no"],
            "label":                   m["kalshi_title"],
            "event_id":                _event_root(m["kalshi_ticker"]),
            "trap_type":               verdict["trap_type"],
            "safe_hours_before_event": verdict.get("safe_hours_before_event", 0),
            "earliest_cutoff_date":    verdict.get("earliest_cutoff_date", "NONE"),
            "explanation":             verdict["explanation"],
            "is_neg_risk":             m.get("is_neg_risk", False),
        })
        added += 1
    if added == 0:
        return
    tmp = output_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(existing, indent=2), encoding="utf-8")
    _safe_replace(tmp, output_path)
    print(f"[SAVE] {added} conditional pair(s) saved to {output_path}.")


def _swap_tokens(candidates: list) -> list:
    """Return copies with poly YES/NO tokens swapped (for INVERTED pairs)."""
    return [{**c, "poly_yes": c["poly_no"], "poly_no": c["poly_yes"]} for c in candidates]


# ============================================================================
# STAGE 1 - Unified index
# ============================================================================

def _coerce_dt(v):
    """datetime | ISO-string | None -> datetime | None."""
    if v is None or isinstance(v, datetime):
        return v
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except Exception:
        return None


@dataclass
class MarketRecord:
    venue: str                       # "kalshi" | "poly"
    id: str                          # kalshi ticker | full poly yes-token
    title: str
    event_title: str
    event_id: str                    # event_ticker | event_slug
    group_item_title: str
    sibling_outcomes: list
    rules_text: str
    category: str                    # canonical (_normalize_category)
    close_date: object               # datetime | None
    is_neg_risk: bool
    raw_record: dict
    extracted_entities: object = field(default=None, repr=False)   # lazily filled set


def build_unified_index(kalshi_markets: dict, poly_markets: list) -> list:
    """Normalize both venues into one list of MarketRecord."""
    index = []
    for ticker, m in kalshi_markets.items():
        index.append(MarketRecord(
            venue="kalshi",
            id=ticker,
            title=m.get("title", "") or "",
            event_title=m.get("event_title", "") or "",
            event_id=m.get("event_ticker", "") or "",
            group_item_title=m.get("yes_sub_title", "") or "",
            sibling_outcomes=list(m.get("siblings", []) or []),
            rules_text=m.get("rules", "") or "",
            category=_normalize_category(m.get("category", "")),
            close_date=_coerce_dt(m.get("close_date")),
            is_neg_risk=bool(m.get("is_neg_risk", False)),
            raw_record=m,
        ))
    for p in poly_markets:
        index.append(MarketRecord(
            venue="poly",
            id=p.get("yes_token", "") or "",
            title=p.get("question", "") or "",
            event_title=p.get("event_title", "") or "",
            event_id=p.get("event_slug", "") or "",
            group_item_title=p.get("group_item_title", "") or "",
            sibling_outcomes=list(p.get("siblings", []) or []),
            rules_text=p.get("description", "") or "",
            category=_normalize_category(p.get("category", "")),
            close_date=_coerce_dt(p.get("end_date")),
            is_neg_risk=bool(p.get("neg_risk", False)),
            raw_record=p,
        ))
    nk = sum(1 for r in index if r.venue == "kalshi")
    npy = len(index) - nk
    print(f"[INDEX] Built {len(index)} records ({nk} kalshi, {npy} poly).")
    return index


def _record_to_jsonable(r: MarketRecord) -> dict:
    raw = {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in (r.raw_record or {}).items()}
    return {
        "venue": r.venue, "id": r.id, "title": r.title, "event_title": r.event_title,
        "event_id": r.event_id, "group_item_title": r.group_item_title,
        "sibling_outcomes": r.sibling_outcomes, "rules_text": r.rules_text,
        "category": r.category,
        "close_date": r.close_date.isoformat() if isinstance(r.close_date, datetime) else None,
        "is_neg_risk": r.is_neg_risk, "raw_record": raw,
    }


def _record_from_jsonable(d: dict) -> MarketRecord:
    return MarketRecord(
        venue=d.get("venue", ""), id=d.get("id", ""), title=d.get("title", ""),
        event_title=d.get("event_title", ""), event_id=d.get("event_id", ""),
        group_item_title=d.get("group_item_title", ""),
        sibling_outcomes=d.get("sibling_outcomes", []) or [],
        rules_text=d.get("rules_text", ""), category=d.get("category", ""),
        close_date=_coerce_dt(d.get("close_date")), is_neg_risk=bool(d.get("is_neg_risk", False)),
        raw_record=d.get("raw_record", {}) or {},
    )


def persist_index(index: list, path: Path = INDEX_PATH) -> None:
    """Persist as gzipped JSON (pyarrow/Parquet unavailable in this env)."""
    envelope = {"_built_at": datetime.now(timezone.utc).isoformat(),
                "records": [_record_to_jsonable(r) for r in index]}
    tmp = path.with_suffix(".gz.tmp")
    with gzip.open(tmp, "wt", encoding="utf-8") as f:
        json.dump(envelope, f, separators=(",", ":"))
    _safe_replace(tmp, path)
    print(f"[INDEX] Persisted {len(index)} records -> {path.name}")


def load_persisted_index(path: Path = INDEX_PATH):
    """Returns (index, built_at_datetime) or (None, None)."""
    if not path.exists():
        return None, None
    try:
        with gzip.open(path, "rt", encoding="utf-8") as f:
            envelope = json.load(f)
        built_at = _coerce_dt(envelope.get("_built_at"))
        index = [_record_from_jsonable(d) for d in envelope.get("records", [])]
        return index, built_at
    except Exception as e:
        print(f"[INDEX] Could not load {path.name} ({e}).")
        return None, None


def fetch_both(api_key_id: str, private_key, no_live: bool = False):
    """Fetch raw market dicts from both venues (kalshi_markets, poly_markets)."""
    kalshi_markets = fetch_kalshi_markets(api_key_id, private_key)
    poly_markets   = fetch_poly_markets(no_live=no_live)
    return kalshi_markets, poly_markets


def load_or_build_index(api_key_id=None, private_key=None, force_rebuild=False, no_live=False) -> list:
    """Load a fresh (<24h) persisted index, else fetch both venues, build, persist, return."""
    if not force_rebuild:
        index, built_at = load_persisted_index()
        if index is not None and built_at is not None:
            age_h = (datetime.now(timezone.utc) - built_at).total_seconds() / 3600
            if age_h < INDEX_MAX_AGE_H:
                print(f"[INDEX] Loaded {len(index)} records from cache (age {age_h:.1f}h).")
                return index
            print(f"[INDEX] Cache is {age_h:.1f}h old (> {INDEX_MAX_AGE_H}h) - rebuilding.")
    if not api_key_id or private_key is None:
        raise SystemExit("[ERROR] Index rebuild needs Kalshi credentials (KALSHI_API_KEY_ID / KALSHI_PRIVATE_KEY_PATH).")
    kalshi_markets, poly_markets = fetch_both(api_key_id, private_key, no_live=no_live)
    index = build_unified_index(kalshi_markets, poly_markets)
    persist_index(index)
    return index


# ============================================================================
# STAGE 2 - Multi-signal search
# ============================================================================

STOPWORDS = {
    "the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "by", "at", "is", "be", "will",
    "with", "as", "from", "that", "this", "it", "are", "was", "were", "has", "have", "had", "who",
    "what", "when", "which", "than", "then", "any", "all", "before", "after", "between", "during",
    "win", "winner", "next", "first", "their", "his", "her", "its",
}

_WORD_RE = re.compile(r"[a-z0-9]+")


def _tokenize(text: str) -> list:
    return _WORD_RE.findall((text or "").lower())


def _venue_iter(index, venue):
    return (e for e in index if (venue is None or e.venue == venue))


def search_keyword_and(index, keywords, venue=None, limit=20):
    """All keywords must appear in title + rules_text; ranked by total hit count."""
    kws = [k.lower() for k in keywords if k]
    if not kws:
        return []
    results = []
    for e in _venue_iter(index, venue):
        hay = (e.title + " " + e.rules_text).lower()
        if all(kw in hay for kw in kws):
            results.append((sum(hay.count(kw) for kw in kws), e))
    results.sort(key=lambda x: -x[0])
    return [(float(h), e) for h, e in results[:limit]]


# Lazy module-level BM25 (rebuilt only when the index identity changes)
_bm25_obj = None
_bm25_records = None
_bm25_index_id = None


def _bm25_doc(e: MarketRecord) -> str:
    # Field repetition gives title/group_item weighting (Stage 7 "score boosting").
    return " ".join([
        e.title, e.title, e.title,
        e.group_item_title, e.group_item_title,
        e.event_title,
        (e.rules_text or "")[:500],
    ])


def _get_bm25(index):
    global _bm25_obj, _bm25_records, _bm25_index_id
    if _bm25_obj is not None and _bm25_index_id == id(index):
        return _bm25_obj, _bm25_records
    try:
        from rank_bm25 import BM25Okapi
    except ImportError:
        raise SystemExit("[ERROR] rank-bm25 not installed. Run: pip install rank-bm25")
    records = list(index)
    corpus = [_tokenize(_bm25_doc(e)) for e in records]
    _bm25_obj = BM25Okapi(corpus)
    _bm25_records = records
    _bm25_index_id = id(index)
    return _bm25_obj, _bm25_records


def search_bm25(index, query, venue=None, limit=20):
    """Natural-language ranking via BM25 over weighted fields."""
    bm25, records = _get_bm25(index)
    q = _tokenize(query)
    if not q:
        return []
    scores = bm25.get_scores(q)
    order = np.argsort(scores)[::-1]
    results = []
    for idx in order:
        s = float(scores[idx])
        if s <= 0:
            break
        e = records[idx]
        if venue and e.venue != venue:
            continue
        results.append((s, e))
        if len(results) >= limit:
            break
    return results


def search_phrase(index, phrase, venue=None, limit=20):
    """Lowercased substring match - useful for proper-noun disambiguation."""
    ph = (phrase or "").lower()
    if not ph:
        return []
    out = []
    for e in _venue_iter(index, venue):
        if ph in e.title.lower() or ph in (e.rules_text or "").lower():
            out.append(e)
            if len(out) >= limit:
                break
    return out


# spaCy NER (lazy)
_spacy_nlp = None
_NER_LABELS = {"PERSON", "ORG", "GPE", "MONEY", "DATE", "NORP", "FAC", "LOC", "EVENT"}


def _get_spacy():
    global _spacy_nlp
    if _spacy_nlp is None:
        try:
            import spacy
        except ImportError:
            raise SystemExit("[ERROR] spaCy not installed. Run: pip install spacy && python -m spacy download en_core_web_lg")
        try:
            _spacy_nlp = spacy.load("en_core_web_lg")
        except OSError:
            raise SystemExit("[ERROR] spaCy model missing. Run: python -m spacy download en_core_web_lg")
    return _spacy_nlp


# Connector/particle words to drop from entity token sets (so "Will Lula da Silva"
# reduces to {lula, silva} and intersects "Will Lula" -> {lula}).
_ENTITY_DROP = STOPWORDS | {"da", "de", "la", "le", "del", "van", "von", "el", "al", "bin", "dos", "das", "di", "du"}


def normalize_entity(text: str) -> str:
    return (text or "").lower().strip().replace(".", "").replace(",", "")


def extract_entities_spacy(text: str) -> set:
    """Return a set of normalized entity TOKENS (not whole spans) so partial spans still
    intersect - spaCy emits 'Will Lula' on one venue and 'Will Lula da Silva' on the other;
    token-level overlap on {lula} is what catches the Brazil/Lula-class misses."""
    if not text:
        return set()
    doc = _get_spacy()(text[:1000])
    out = set()
    for ent in doc.ents:
        if ent.label_ in _NER_LABELS:
            for tok in _tokenize(ent.text):
                if len(tok) < 2 or tok in _ENTITY_DROP:
                    continue
                out.add(tok)
    return out


def _record_entities(e: MarketRecord) -> set:
    if e.extracted_entities is None:
        e.extracted_entities = extract_entities_spacy(e.title + " " + (e.rules_text or "")[:400])
    return e.extracted_entities


def search_by_entity(index, entities, venue=None, limit=None, restrict_to=None):
    """Match by named-entity token overlap (Jaccard-ish). entities = iterable of raw strings.

    spaCy NER is run lazily per record and cached. Scanning a whole venue (tens of thousands of
    records) is expensive on first call, so the hot path (twin_search) passes `restrict_to` -- an
    iterable of already-surfaced candidate records -- to bound NER to that pool."""
    target = {normalize_entity(x) for x in entities if x}
    target.discard("")
    if not target:
        return []
    pool = restrict_to if restrict_to is not None else _venue_iter(index, venue)
    results = []
    for e in pool:
        if venue and e.venue != venue:
            continue
        ents = _record_entities(e)
        overlap = target & ents
        if overlap:
            score = len(overlap) / max(len(target), len(ents))
            results.append((score, e))
    results.sort(key=lambda x: -x[0])
    return results[:limit] if limit else results


def search_by_date_range(index, target_date, tolerance_days=30, venue=None):
    """Filter to markets closing near target_date; closer = higher score."""
    td = _coerce_dt(target_date)
    if td is None:
        return []
    if td.tzinfo is None:
        td = td.replace(tzinfo=timezone.utc)
    results = []
    for e in _venue_iter(index, venue):
        cd = e.close_date
        if cd is None:
            continue
        if cd.tzinfo is None:
            cd = cd.replace(tzinfo=timezone.utc)
        delta = abs((cd - td).days)
        if delta <= tolerance_days:
            results.append((1.0 - delta / tolerance_days, e))
    results.sort(key=lambda x: -x[0])
    return results


def search_by_category(index, category, venue=None):
    cat = _normalize_category(category)
    if not cat:
        return []
    return [e for e in _venue_iter(index, venue) if e.category == cat]


def search_regex(index, pattern, field="title", venue=None, limit=50):
    """Power-user regex over a chosen field (title|rules_text|id|event_title)."""
    try:
        rx = re.compile(pattern, re.IGNORECASE)
    except re.error as ex:
        print(f"[REGEX] Invalid pattern: {ex}")
        return []
    out = []
    for e in _venue_iter(index, venue):
        val = getattr(e, field, "") or ""
        if rx.search(val):
            out.append(e)
            if len(out) >= limit:
                break
    return out


# ============================================================================
# STAGE 3 - Twin search
# ============================================================================

# Stage 7 synonym map (expanded into keyword/entity sets before search).
SYNONYMS = {
    "pga": ["professional golf association", "golf"],
    "potus": ["president", "presidency", "presidential"],
    "btc": ["bitcoin"], "eth": ["ethereum"],
    "usa": ["united states", "us", "america"],
    "uk": ["united kingdom", "britain"],
    "epl": ["premier league", "english premier league"],
    "gop": ["republican"], "dems": ["democrat", "democratic"],
    "nba": ["basketball"], "nfl": ["football"], "mlb": ["baseball"], "nhl": ["hockey"],
}


def _expand_synonyms(tokens):
    out = set(tokens)
    for t in list(tokens):
        if t in SYNONYMS:
            for syn in SYNONYMS[t]:
                out.update(_tokenize(syn))
    return out


def extract_search_keywords(text: str) -> list:
    """Tokenize, drop stopwords, keep distinctive words; expand known synonyms."""
    toks = [t for t in _tokenize(text) if t not in STOPWORDS and len(t) > 2]
    return sorted(_expand_synonyms(toks))


def auto_suggest_keywords_from_ticker(ticker: str, series_titles: dict = None) -> list:
    """KXPGAWINNER-PGC26-SCHEFFLER -> ['pga','winner','scheffler','2026'] etc."""
    series_titles = series_titles or {}
    kws = set()
    parts = ticker.split("-")
    series = parts[0] if parts else ticker
    if series in series_titles:
        kws.update(extract_search_keywords(series_titles[series]))
    for seg in parts:
        # Split camel/cap runs and pull year codes (e.g. 26 -> 2026, PGC26).
        for m in re.findall(r"[A-Z][a-z]+|[A-Z]{2,}|\d{2,4}", seg):
            if m.isdigit():
                if len(m) == 2:
                    kws.add("20" + m)
                else:
                    kws.add(m)
            elif len(m) > 2:
                kws.add(m.lower())
    return sorted(k for k in kws if k not in STOPWORDS)


def twin_search(source_market: MarketRecord, target_venue: str, index, limit=20, series_titles=None):
    """Multi-signal candidate generation on the opposite venue. Returns ranked dicts with provenance."""
    keywords = extract_search_keywords(source_market.title + " " + source_market.event_title)
    if source_market.venue == "kalshi":
        keywords += auto_suggest_keywords_from_ticker(source_market.id, series_titles)
    entities = _record_entities(source_market)
    target_date = source_market.close_date
    category = source_market.category

    scores = {}     # id -> combined score
    fired  = {}     # id -> set of signals
    rec_by_id = {}

    def _add(entry, signal, contribution):
        scores[entry.id] = scores.get(entry.id, 0.0) + contribution
        fired.setdefault(entry.id, set()).add(signal)
        rec_by_id[entry.id] = entry

    # Cheap signals first - they define the candidate pool.
    bm25_hits = search_bm25(index, " ".join(keywords), venue=target_venue, limit=50)
    if bm25_hits:
        top = bm25_hits[0][0] or 1.0
        for s, e in bm25_hits:
            _add(e, "bm25", W_BM25 * (s / top))
    for s, e in search_by_date_range(index, target_date, tolerance_days=30, venue=target_venue):
        _add(e, "date", W_DATE * s)
    for e in search_by_category(index, category, venue=target_venue):
        _add(e, "category", W_CATEGORY * 1.0)
    # Entity NER is expensive per record - run it only on the already-surfaced pool, not the
    # whole venue (62k+ records). The entity tokens are also in `keywords`, so BM25 already
    # surfaces entity matches into the pool; this step re-ranks them by precise overlap.
    pool = list(rec_by_id.values())
    for s, e in search_by_entity(index, entities, venue=target_venue, restrict_to=pool):
        _add(e, "entity", W_ENTITY * s)

    ranked = sorted(rec_by_id.values(), key=lambda e: -scores[e.id])
    return [{
        "entry": e,
        "combined_score": round(scores[e.id], 4),
        "signals_fired": sorted(fired[e.id]),
    } for e in ranked[:limit]]


def _short(s, n):
    s = s or ""
    return s if len(s) <= n else s[:n - 1] + "…"


def display_two_column(source: MarketRecord, results: list) -> None:
    """SOURCE (one venue) vs CANDIDATES (other) with signal tags."""
    src_venue = source.venue.upper()
    tgt_venue = "POLY" if source.venue == "kalshi" else "KALSHI"
    print(f"\n{'SOURCE ('+src_venue+')':<52}  CANDIDATES ({tgt_venue}):")
    print(f"{_short(source.id, 50):<52}")
    print(f"{_short(source.title, 50):<52}")
    ev = f"event: {_short(source.event_title, 42)}" if source.event_title else ""
    print(f"{ev:<52}")
    if not results:
        print("  (no candidates)")
        return
    print("-" * 100)
    for i, r in enumerate(results, 1):
        e = r["entry"]
        sig = "+".join(r["signals_fired"])
        print(f"  {i:>2}. [{r['combined_score']:.3f} {sig}] {_short(e.title, 70)}")
        if e.event_title:
            print(f"      event: {_short(e.event_title, 70)}")


# ============================================================================
# STAGE 4 - Coverage reporting
# ============================================================================

def _load_json_list(path: Path) -> list:
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return []


def _paired_rejected_ids(pairs_file: Path, rejected_file: Path):
    """Return (paired_ids, rejected_ids) covering BOTH venues' ids."""
    paired, rejected = set(), set()
    for e in _load_json_list(pairs_file):
        if e.get("kalshi_ticker"):  paired.add(e["kalshi_ticker"])
        if e.get("poly_yes_token"): paired.add(e["poly_yes_token"])
    for e in _load_json_list(rejected_file):
        if e.get("kalshi_ticker"):  rejected.add(e["kalshi_ticker"])
        if e.get("poly_yes_token"): rejected.add(e["poly_yes_token"])
    return paired, rejected


def compute_coverage_report(index, pairs_file=DEFAULT_OUTPUT,
                            rejected_file=SCRIPT_DIR / "rejected_pairs.json") -> dict:
    paired, rejected = _paired_rejected_ids(Path(pairs_file), Path(rejected_file))

    events = {}   # (venue, event_id) -> stats
    for e in index:
        key = (e.venue, e.event_id or e.id)
        ev = events.setdefault(key, {
            "venue": e.venue, "event_id": e.event_id or e.id,
            "event_title": e.event_title, "category": e.category,
            "sub_markets": [], "paired": 0, "rejected": 0, "silent": 0,
        })
        ev["sub_markets"].append(e.id)
        if e.id in paired:
            ev["paired"] += 1
        elif e.id in rejected:
            ev["rejected"] += 1
        else:
            ev["silent"] += 1

    silent_misses = [v for v in events.values() if v["paired"] == 0 and v["rejected"] == 0]

    by_cat = {}
    for v in events.values():
        c = by_cat.setdefault(v["category"] or "Unknown",
                              {"events": 0, "fully_paired": 0, "silent_events": 0, "sub_markets": 0})
        c["events"] += 1
        c["sub_markets"] += len(v["sub_markets"])
        if v["silent"] == 0 and v["paired"] > 0:
            c["fully_paired"] += 1
        if v["paired"] == 0 and v["rejected"] == 0:
            c["silent_events"] += 1

    total_markets = len(index)
    paired_count  = sum(1 for e in index if e.id in paired)
    rejected_count = sum(1 for e in index if e.id in rejected)
    silent_count  = total_markets - paired_count - rejected_count

    return {
        "total_markets":  total_markets,
        "paired_count":   paired_count,
        "rejected_count": rejected_count,
        "silent_count":   silent_count,
        "paired_pct":     round(100 * paired_count / total_markets, 1) if total_markets else 0.0,
        "rejected_pct":   round(100 * rejected_count / total_markets, 1) if total_markets else 0.0,
        "silent_pct":     round(100 * silent_count / total_markets, 1) if total_markets else 0.0,
        "per_event":      events,
        "silent_misses":  silent_misses,
        "coverage_by_category": by_cat,
    }


def print_coverage_summary(report: dict) -> None:
    print("\n" + "-" * 80)
    print("COVERAGE REPORT")
    print(f"  Total markets indexed:   {report['total_markets']}")
    print(f"  Paired:                  {report['paired_count']}  ({report['paired_pct']}%)")
    print(f"  Rejected:                {report['rejected_count']}  ({report['rejected_pct']}%)")
    print(f"  Silent (no candidates):  {report['silent_count']}  ({report['silent_pct']}%)")
    print("\n  Coverage by category:")
    for cat, c in sorted(report["coverage_by_category"].items(), key=lambda kv: -kv[1]["silent_events"]):
        print(f"    {cat:<14} events={c['events']:>4}  fully_paired={c['fully_paired']:>4}  silent_events={c['silent_events']:>4}")
    print("\n  Top 20 untouched events (silent; sorted by sub-market count):")
    untouched = sorted(report["silent_misses"], key=lambda v: -len(v["sub_markets"]))[:20]
    for v in untouched:
        print(f"    {v['venue']:<7} {_short(v['event_id'], 28):<30} "
              f"({len(v['sub_markets']):>3} mkts, {v['category'] or 'Unknown'})  {_short(v['event_title'], 30)}")
    if not untouched:
        print("    (none - every event has at least one pairing attempt)")


def daily_snapshot(report: dict) -> None:
    hist = _load_json_list(COVERAGE_HISTORY)
    hist.append({
        "date":          datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "total_markets": report["total_markets"],
        "paired_pct":    report["paired_pct"],
        "silent_count":  report["silent_count"],
        "silent_pct":    report["silent_pct"],
    })
    try:
        COVERAGE_HISTORY.write_text(json.dumps(hist, indent=2), encoding="utf-8")
        print(f"[COVERAGE] Snapshot appended -> {COVERAGE_HISTORY.name} ({len(hist)} total).")
    except Exception as e:
        print(f"[COVERAGE] Snapshot save failed: {e}")


# ============================================================================
# STAGE 5 - Workflow integration (manual UI primitives + enhanced review)
# ============================================================================
_GREEN = "\033[92m"
_RESET = "\033[0m"


def _getch() -> str:
    """Read one character immediately, no Enter required."""
    try:
        import msvcrt
        ch = msvcrt.getwch()
        if ch in ('\x00', '\xe0'):
            msvcrt.getwch()
            return ''
        return ch.lower()
    except ImportError:
        import tty, termios
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        try:
            tty.setraw(fd)
            return sys.stdin.read(1).lower()
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)


def _lookup_poly_token_info(yes_token: str, no_token: str = "") -> dict:
    tokens = [t for t in [yes_token, no_token] if t]
    if not tokens:
        return None
    try:
        params = [("clob_token_ids", t) for t in tokens]
        r = requests.get(f"{POLY_GAMMA_URL}/markets", params=params, timeout=15)
        r.raise_for_status()
        data = r.json()
        markets = data if isinstance(data, list) else data.get("data", data.get("markets", []))
        if not markets:
            return {"error": "no market returned for token(s)"}
        mkt = markets[0]
        clob_ids_raw = mkt.get("clobTokenIds", "[]")
        clob_ids = json.loads(clob_ids_raw) if isinstance(clob_ids_raw, str) else clob_ids_raw
        side = "YES" if (clob_ids and clob_ids[0] == yes_token) else "NO"
        event = (mkt.get("events") or [{}])[0]
        siblings = [
            {"group_item": m.get("groupItemTitle", "") or "", "question": m.get("question", "") or ""}
            for m in event.get("markets", [])
        ]
        return {
            "side": side, "question": mkt.get("question", "") or "",
            "group_item_title": mkt.get("groupItemTitle", "") or "",
            "market_desc": mkt.get("description", "") or "",
            "condition_id": mkt.get("conditionId", "") or "",
            "end_date": mkt.get("endDate", "") or "",
            "volume": mkt.get("volumeNum", 0) or 0, "liquidity": mkt.get("liquidityNum", 0) or 0,
            "resolution_source": mkt.get("resolutionSource", "") or "",
            "event_title": event.get("title", "") or "", "event_slug": event.get("slug", "") or "",
            "neg_risk": bool(event.get("negRisk", False)), "siblings": siblings,
        }
    except Exception as ex:
        return {"error": f"{type(ex).__name__}: {ex}"}


def _print_poly_token_info(c: dict) -> None:
    yes_token = c.get("poly_yes", "")
    no_token  = c.get("poly_no",  "")
    outcomes  = c.get("poly_outcomes", [])
    print(f"\n  --- POLY MARKET INFO ---")
    print(f"  question:    {c.get('poly_question', '(unknown)')}")
    if outcomes:
        print(f"  YES label:   {outcomes[0] if len(outcomes) > 0 else 'Yes'}")
        print(f"  NO label:    {outcomes[1] if len(outcomes) > 1 else 'No'}")
    print(f"  YES token:   {yes_token}")
    print(f"  NO token:    {no_token}")
    info = _lookup_poly_token_info(yes_token, no_token)
    if not info:
        print("  (no token ids available)\n"); return
    if "error" in info:
        print(f"  API note:    {info['error']}\n"); return
    print(f"  condition_id:     {info['condition_id']}")
    print(f"  group_item_title: {info['group_item_title'] or '(none)'}")
    print(f"  volume:           {info['volume']}   liquidity: {info['liquidity']}")
    print(f"  neg_risk:         {info['neg_risk']}")
    print(f"  event_title:      {info['event_title']}")
    if info["siblings"]:
        print(f"  siblings ({len(info['siblings'])}):")
        for s in info["siblings"]:
            print(f"    - [{s['group_item'] or '(no group_item)'}] {s['question'][:80]}")
    print()


def _rec_index_by_id(index) -> dict:
    return {e.id: e for e in index}


def _candidate_from_records(k_rec: MarketRecord, p_rec: MarketRecord, score: float = 1.0) -> dict:
    """Build a judge/save-shaped candidate dict from two MarketRecords."""
    kr, pr = k_rec.raw_record or {}, p_rec.raw_record or {}
    return {
        "kalshi_ticker":       k_rec.id,
        "kalshi_title":        k_rec.title,
        "kalshi_close":        _coerce_dt(k_rec.close_date),
        "kalshi_rules":        k_rec.rules_text,
        "kalshi_event":        k_rec.event_id,
        "kalshi_category":     k_rec.category,
        "kalshi_yes_sub":      k_rec.group_item_title,
        "kalshi_no_sub":       kr.get("no_sub_title", ""),
        "poly_question":       p_rec.title,
        "poly_yes":            pr.get("yes_token", p_rec.id),
        "poly_no":             pr.get("no_token", ""),
        "poly_close":          _coerce_dt(p_rec.close_date),
        "poly_desc":           p_rec.rules_text,
        "poly_outcomes":       pr.get("outcomes", []),
        "is_neg_risk":         p_rec.is_neg_risk,
        "poly_order_min_size": pr.get("order_min_size", 1.0),
        "score":               score,
    }


def manual_override_candidate(kalshi_rec: MarketRecord, poly_rec: MarketRecord) -> dict:
    return _candidate_from_records(kalshi_rec, poly_rec, score=1.0)


def _select_index(prompt: str, n: int):
    """Prompt for a 1..n choice (Enter/blank/q = cancel). Returns 0-based index or None."""
    try:
        raw = input(prompt).strip().lower()
    except (EOFError, KeyboardInterrupt):
        return None
    if not raw or raw == "q":
        return None
    if not raw.isdigit():
        print(f"  (enter a number 1–{n}, or blank to cancel)")
        return None
    i = int(raw) - 1
    return i if 0 <= i < n else None


def find_best_target_by_outcome_name(src: MarketRecord, target_subs: list):
    """Pick the target sub-market whose group_item_title best matches src's, by token overlap."""
    src_toks = set(_tokenize(src.group_item_title or src.title))
    if not src_toks:
        return None
    best, best_score = None, 0.0
    for t in target_subs:
        t_toks = set(_tokenize(t.group_item_title or t.title))
        if not t_toks:
            continue
        score = len(src_toks & t_toks) / len(src_toks | t_toks)
        if score > best_score:
            best, best_score = t, score
    return best if best_score > 0 else None


def enter_event_pairing_mode(source_event_id: str, index, output_path: Path,
                             series_titles=None, sync: bool = False) -> None:
    """Pair all sub-markets of one event at once (Stage 5)."""
    source_subs = [e for e in index if e.event_id == source_event_id]
    if not source_subs:
        print(f"[EVENT] No sub-markets found for event '{source_event_id}'.")
        return
    src_venue = source_subs[0].venue
    target_venue = "poly" if src_venue == "kalshi" else "kalshi"
    print(f"[EVENT] {source_event_id} ({src_venue}) - {len(source_subs)} sub-markets. "
          f"Finding matching {target_venue} event...")

    # Twin-search the first sub-market to locate the matching event on the other venue.
    cand = twin_search(source_subs[0], target_venue, index, limit=10, series_titles=series_titles)
    target_event_ids = list(dict.fromkeys(c["entry"].event_id for c in cand if c["entry"].event_id))
    if not target_event_ids:
        print("[EVENT] No candidate target event found via twin search.")
        return
    print("  Candidate target events:")
    for i, eid in enumerate(target_event_ids[:10], 1):
        sample = next((c["entry"] for c in cand if c["entry"].event_id == eid), None)
        print(f"    {i}. {eid}  ({_short(sample.event_title if sample else '', 50)})")
    choice = _select_index("  Select target event # (blank to cancel): ", len(target_event_ids[:10]))
    if choice is None:
        print("  [EVENT] Cancelled.")
        return
    target_event_id = target_event_ids[choice]
    target_subs = [e for e in index if e.event_id == target_event_id]

    # Auto-suggest pairings by outcome-name similarity.
    suggested = []
    for src in source_subs:
        tgt = find_best_target_by_outcome_name(src, target_subs)
        if tgt:
            suggested.append((src, tgt))
    if not suggested:
        print("  [EVENT] No outcome-name matches found.")
        return
    print(f"\n  Suggested pairings ({len(suggested)}):")
    for src, tgt in suggested:
        k, p = (src, tgt) if src.venue == "kalshi" else (tgt, src)
        print(f"    K[{_short(k.group_item_title or k.title, 34):<34}] <-> P[{_short(p.group_item_title or p.title, 34)}]")
    try:
        confirm = input("  Confirm ALL pairings? (y/N): ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        confirm = "n"
    if confirm != "y":
        print("  [EVENT] Not confirmed.")
        return
    rows = []
    for src, tgt in suggested:
        k, p = (src, tgt) if src.venue == "kalshi" else (tgt, src)
        rows.append(_candidate_from_records(k, p, score=1.0))
    _save_pairs(rows, output_path, source="event")
    if sync:
        _scp_sync(output_path)


def _display_candidate(c: dict, i: int, total: int) -> None:
    kc = c["kalshi_close"].strftime("%Y-%m-%d") if c.get("kalshi_close") else "?"
    pc = c["poly_close"].strftime("%Y-%m-%d")   if c.get("poly_close")   else "?"
    cat = c.get("kalshi_category") or "?"
    print(f"\n--- [{i+1}/{total}] score={c.get('score', 0):.3f}  category={cat} ---")
    print(f"  KALSHI  {c['kalshi_ticker']}")
    print(f"          {_GREEN}{c['kalshi_title']}{_RESET}")
    if c.get("kalshi_yes_sub") or c.get("kalshi_no_sub"):
        print(f"          YES: [{c.get('kalshi_yes_sub') or '?'}]  /  NO: [{c.get('kalshi_no_sub') or '?'}]")
    print(f"          closes: {kc}")
    if c.get("kalshi_rules"):
        print(f"          rules:  {_short(c['kalshi_rules'], 300)}")
    p_out = c.get("poly_outcomes", [])
    print(f"  POLY    {_GREEN}{c['poly_question']}{_RESET}")
    print(f"          YES: [{p_out[0] if len(p_out) > 0 else 'Yes'}]  /  NO: [{p_out[1] if len(p_out) > 1 else 'No'}]")
    print(f"          closes: {pc}")
    if c.get("poly_desc"):
        print(f"          desc:   {_short(c['poly_desc'], 300)}")


def manual_review_session(candidates: list, index, output_path: Path,
                          series_titles=None, sync: bool = False) -> None:
    """Enhanced v1 manual judge: 1-4 verdicts plus / (find) t (twin) c (coverage) e (event)."""
    potential_path = output_path.parent / f"potential_{output_path.name}"
    rejected_path  = output_path.parent / "rejected_pairs.json"
    by_id = _rec_index_by_id(index)
    total = len(candidates)
    print(f"\n[MANUAL] {total} candidates to judge.")
    print("  Keys: 1=VALID 2=CONDITIONAL 3=INVALID 4=INVERTED  i=info  /p=find-poly  /k=find-kalshi  t=twin  c=coverage  e=event  s=skip  q=quit\n")

    i = 0
    while i < len(candidates):
        c = candidates[i]
        _display_candidate(c, i, len(candidates))
        advance = True
        while True:
            try:
                print("  > ", end="", flush=True)
                key = _getch()
            except (EOFError, KeyboardInterrupt):
                print("\n[MANUAL] Interrupted.")
                return
            if key == "i":
                print("i"); _print_poly_token_info(c); continue
            if key == "/":
                # Read the modifier: 'p' = poly search, 'k' = kalshi search
                print("/", end="", flush=True)
                try:
                    mod = _getch()
                except (EOFError, KeyboardInterrupt):
                    print(); continue
                print(mod)
                if mod == "p":
                    newc = _interactive_find_poly(index, c, by_id, output_path, rejected_path)
                elif mod == "k":
                    newc = _interactive_find_kalshi(index, c, by_id, output_path, rejected_path)
                else:
                    print("  (use /p for poly search or /k for kalshi search)")
                    continue
                if newc:
                    _save_rejected([(c, "OVERRIDDEN")], rejected_path)
                    candidates[i] = c = newc
                    _display_candidate(c, i, len(candidates))
                else:
                    print("  (search cancelled — 1/2/3/4/i/t/s/q)")
                continue
            if key == "t":
                print("t")
                newc = _interactive_twin(index, c, by_id, series_titles)
                if newc:
                    _save_rejected([(c, "OVERRIDDEN")], rejected_path)
                    candidates[i] = c = newc
                    _display_candidate(c, i, len(candidates))
                else:
                    print("  (twin search cancelled — 1/2/3/4/i//s/q)")
                continue
            if key == "c":
                print("c")
                report = compute_coverage_report(index, output_path, rejected_path)
                print_coverage_summary(report)
                _explore_silent(report, index, output_path, series_titles, sync)
                continue
            if key == "e":
                print("e")
                try:
                    eid = input("  Event ID: ").strip()
                except (EOFError, KeyboardInterrupt):
                    eid = ""
                if eid:
                    enter_event_pairing_mode(eid, index, output_path, series_titles, sync)
                continue
            if key in ("1", "2", "3", "4", "s", "q"):
                print(key); break
            if key:
                print(f"\n  Invalid key '{key}'.")
        if key == "q":
            print("[MANUAL] Quit."); break
        if key == "1":
            _save_pairs([c], output_path, source="manual")
            if sync: _scp_sync(output_path)
        elif key == "2":
            verdict = {"trap_type": "MANUAL", "safe_hours_before_event": 0,
                       "earliest_cutoff_date": "NONE", "explanation": "Manually flagged as conditional."}
            _save_potential_pairs([(c, verdict)], potential_path)
        elif key == "3":
            print("  [INVALID]"); _save_rejected([(c, "INVALID")], rejected_path)
        elif key == "4":
            _save_pairs(_swap_tokens([c]), output_path, source="manual");
            if sync: _scp_sync(output_path)
        elif key == "s":
            print("  [SKIP]"); _save_rejected([(c, "SKIP")], rejected_path)
        if advance:
            i += 1
        print()


def _annotate_results(results, paired_ids, rejected_ids, id_field=1):
    """Return list of (score, entry, tag_str) where tag is '' / '[paired]' / '[rejected]'."""
    out = []
    for item in results:
        s, e = item[0], item[id_field]
        if e.id in paired_ids:
            tag = " \033[33m[paired]\033[0m"
        elif e.id in rejected_ids:
            tag = " \033[90m[rejected]\033[0m"
        else:
            tag = ""
        out.append((s, e, tag))
    return out


def _interactive_find_poly(index, current_candidate, by_id, output_path, rejected_path):
    """`/p` key: BM25 search on Poly, swap the Poly side of the current candidate."""
    try:
        query = input("  Search query (poly): ").strip()
    except (EOFError, KeyboardInterrupt):
        return None
    if not query:
        return None
    results = search_bm25(index, query, venue="poly", limit=15)
    if not results:
        print("  (no matches)"); return None
    paired, rejected = _paired_rejected_ids(output_path, rejected_path)
    annotated = _annotate_results(results, paired, rejected)
    for n, (s, e, tag) in enumerate(annotated, 1):
        print(f"    {n:>2}. [{s:.2f}] {_short(e.title, 70)}{tag}")
    sel = _select_index("  Pick # (blank to cancel): ", len(annotated))
    if sel is None:
        return None
    poly_rec = annotated[sel][1]
    k_rec = by_id.get(current_candidate["kalshi_ticker"])
    if k_rec is None:
        print("  (kalshi record not in index)"); return None
    print(f"  [OVERRIDE] poly -> {_short(poly_rec.title, 60)}")
    return manual_override_candidate(k_rec, poly_rec)


def _interactive_find_kalshi(index, current_candidate, by_id, output_path, rejected_path):
    """`/k` key: BM25 search on Kalshi, swap the Kalshi side of the current candidate."""
    try:
        query = input("  Search query (kalshi): ").strip()
    except (EOFError, KeyboardInterrupt):
        return None
    if not query:
        return None
    results = search_bm25(index, query, venue="kalshi", limit=15)
    if not results:
        print("  (no matches)"); return None
    paired, rejected = _paired_rejected_ids(output_path, rejected_path)
    annotated = _annotate_results(results, paired, rejected)
    for n, (s, e, tag) in enumerate(annotated, 1):
        print(f"    {n:>2}. [{s:.2f}] {_short(e.title, 70)}{tag}")
    sel = _select_index("  Pick # (blank to cancel): ", len(annotated))
    if sel is None:
        return None
    k_rec = annotated[sel][1]
    p_rec = by_id.get(current_candidate.get("poly_yes", ""))
    if p_rec is None:
        print("  (poly record not in index)"); return None
    print(f"  [OVERRIDE] kalshi -> {_short(k_rec.title, 60)}")
    return manual_override_candidate(k_rec, p_rec)


def _interactive_twin(index, current_candidate, by_id, series_titles):
    """`t` key: twin search using the current Kalshi market as source."""
    k_rec = by_id.get(current_candidate["kalshi_ticker"])
    if k_rec is None:
        print("  (kalshi record not in index)"); return None
    results = twin_search(k_rec, "poly", index, limit=15, series_titles=series_titles)
    display_two_column(k_rec, results)
    if not results:
        return None
    sel = _select_index("  Pick candidate # (blank to cancel): ", len(results))
    if sel is None:
        return None
    poly_rec = results[sel]["entry"]
    print(f"  [OVERRIDE] poly -> {_short(poly_rec.title, 60)}")
    return manual_override_candidate(k_rec, poly_rec)


def _explore_silent(report, index, output_path, series_titles, sync):
    """`c` follow-up: pick a silent-miss event and run event-pairing on it."""
    untouched = sorted(report["silent_misses"], key=lambda v: -len(v["sub_markets"]))[:20]
    if not untouched:
        return
    sel = _select_index("  Explore which untouched event # (blank to skip): ", len(untouched))
    if sel is None:
        return
    enter_event_pairing_mode(untouched[sel]["event_id"], index, output_path, series_titles, sync)


# ============================================================================
# STAGE 6 - LLM judge integration (confidence routing)
# ============================================================================
_PRICE_IN_PER_TOK  = 1.0 / 1_000_000   # rough Haiku-class input  $/token
_PRICE_OUT_PER_TOK = 5.0 / 1_000_000   # rough Haiku-class output $/token


def _estimated_cost_today() -> float:
    if not JUDGE_COST_PATH.exists():
        return 0.0
    try:
        data = json.loads(JUDGE_COST_PATH.read_text(encoding="utf-8"))
    except Exception:
        return 0.0
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    rec = data.get(day, {})
    return rec.get("prompt_tokens", 0) * _PRICE_IN_PER_TOK + rec.get("completion_tokens", 0) * _PRICE_OUT_PER_TOK


def _build_judge_context(candidate: dict, context: dict) -> str:
    signals = context.get("signals", [])
    src = context.get("source")
    tgt = context.get("target")
    lines = ["<context>"]
    if signals:
        lines.append(f"discovery_signals: {', '.join(signals)}")
    if src is not None and src.sibling_outcomes:
        lines.append(f"kalshi_siblings: {', '.join(s for s in src.sibling_outcomes[:12] if s)}")
    if tgt is not None and tgt.sibling_outcomes:
        lines.append(f"poly_siblings: {', '.join(s for s in tgt.sibling_outcomes[:12] if s)}")
    lines.append("(Siblings list the other outcomes in each event - use them to detect neg-risk "
                 "scope mismatches, e.g. one venue's market covers a strictly narrower outcome set.)")
    lines.append("</context>")
    return "\n".join(lines)


def llm_judge_candidate(candidate: dict, context: dict, openrouter_key: str) -> dict:
    """Single-pair judge with rich twin-search context. Returns one verdict dict (or INVALID fallback)."""
    client = OpenAI(base_url="https://openrouter.ai/api/v1", api_key=openrouter_key)
    user = _build_user_prompt([candidate]) + "\n\n" + _build_judge_context(candidate, context)
    try:
        response = client.chat.completions.create(
            model=OPENROUTER_MODEL,
            messages=[{"role": "system", "content": _JUDGE_SYSTEM}, {"role": "user", "content": user}],
            temperature=0.0, max_tokens=2048, extra_headers=OPENROUTER_HEADERS,
        )
        _record_judge_cost(getattr(response, "usage", None))
        verdicts = _parse_judge_response(response.choices[0].message.content.strip(), 1)
        if verdicts:
            return verdicts[0]
    except APIStatusError as e:
        if e.status_code == 402:
            raise BalanceExhaustedError("OpenRouter balance exhausted (HTTP 402).")
        print(f"[DISCOVER] judge HTTP {e.status_code}: {e.message}")
    except Exception as e:
        print(f"[DISCOVER] judge error: {e}")
    return {"index": 0, "status": "INVALID", "confidence": 0.0, "trap_type": "OTHER",
            "earliest_cutoff_date": "NONE", "explanation": "judge call failed or empty"}


def batch_judge_new_discoveries(index, openrouter_key: str, output_path: Path = DEFAULT_OUTPUT,
                                limit_sources=None, series_titles=None, sync: bool = False) -> dict:
    """Iterate unpaired Kalshi markets, twin-search Poly, judge top candidate, route by confidence."""
    rejected_path = output_path.parent / "rejected_pairs.json"
    paired, rejected = _paired_rejected_ids(output_path, rejected_path)
    by_id = _rec_index_by_id(index)

    sources = [e for e in index if e.venue == "kalshi" and e.id not in paired and e.id not in rejected]
    if limit_sources:
        sources = sources[:limit_sources]
    print(f"[DISCOVER] {len(sources)} unpaired Kalshi source markets to evaluate.")

    auto_accepted = auto_rejected = queued = judged = 0
    review = _load_json_list(REVIEW_QUEUE)

    for si, src in enumerate(sources, 1):
        if _estimated_cost_today() >= JUDGE_DAILY_COST_CAP:
            print(f"[DISCOVER] Daily cost cap ${JUDGE_DAILY_COST_CAP:.2f} reached - stopping early.")
            break
        cands = twin_search(src, "poly", index, limit=10, series_titles=series_titles)
        cands = [c for c in cands if c["combined_score"] >= MIN_DISCOVERY_THRESHOLD]
        if not cands:
            continue
        best = cands[0]
        poly_rec = best["entry"]
        candidate = _candidate_from_records(src, poly_rec, score=best["combined_score"])
        try:
            verdict = llm_judge_candidate(
                candidate, {"source": src, "target": poly_rec, "signals": best["signals_fired"]}, openrouter_key)
        except BalanceExhaustedError as e:
            print(f"[DISCOVER] {e}"); break
        judged += 1
        st, conf = verdict["status"], verdict.get("confidence", 0.0)
        print(f"  [{si}/{len(sources)}] {src.id} -> {_short(poly_rec.title, 50)} | {st} conf={conf:.2f}")

        if st in ("VALID", "INVERTED") and conf >= AUTO_CONFIDENCE:
            rows = _swap_tokens([candidate]) if st == "INVERTED" else [candidate]
            _save_pairs(rows, output_path, source="llm_auto")
            if sync: _scp_sync(output_path)
            auto_accepted += 1
        elif st == "INVALID" and conf >= AUTO_CONFIDENCE:
            _save_rejected([(candidate, "INVALID")], rejected_path)
            auto_rejected += 1
        else:
            review.append({
                "kalshi_ticker": candidate["kalshi_ticker"], "poly_yes_token": candidate["poly_yes"],
                "poly_no_token": candidate["poly_no"], "label": candidate["kalshi_title"],
                "poly_question": candidate["poly_question"], "status": st, "confidence": conf,
                "trap_type": verdict.get("trap_type", "NONE"), "explanation": verdict.get("explanation", ""),
                "signals": best["signals_fired"], "combined_score": best["combined_score"],
                "queued_at": datetime.now(timezone.utc).isoformat(),
            })
            queued += 1

    if review:
        try:
            REVIEW_QUEUE.write_text(json.dumps(review, indent=2), encoding="utf-8")
        except Exception as e:
            print(f"[DISCOVER] review queue save failed: {e}")

    stats = {"judged": judged, "auto_accepted": auto_accepted,
             "auto_rejected": auto_rejected, "queued": queued,
             "est_cost_today": round(_estimated_cost_today(), 4)}
    print(f"[DISCOVER] judged={judged} auto_accepted={auto_accepted} "
          f"auto_rejected={auto_rejected} queued={queued} est_cost=${stats['est_cost_today']:.4f}")
    return stats


# Resolution fetchers for the audit (mirror prod_cross_arb.py, using requests).
def _load_resolution_cache() -> dict:
    if RESOLUTION_CACHE.exists():
        try:    return json.loads(RESOLUTION_CACHE.read_text(encoding="utf-8"))
        except Exception: return {}
    return {}


def _save_resolution_cache(cache: dict) -> None:
    try:    RESOLUTION_CACHE.write_text(json.dumps(cache, indent=2), encoding="utf-8")
    except Exception: pass


def fetch_kalshi_resolution(ticker: str, cache: dict):
    """'yes' | 'no' | None (unresolved/error)."""
    key = f"K:{ticker}"
    if cache.get(key) is not None:
        return cache[key]
    try:
        r = requests.get(f"{KALSHI_REST}/markets/{ticker}", timeout=10)
        r.raise_for_status()
        market = r.json().get("market", {})
        result = market.get("result")
        if result:
            cache[key] = result
        return result
    except Exception as e:
        print(f"  [warn] Kalshi resolution fetch failed for {ticker}: {e}")
        return None


def fetch_poly_token_wins(token_id: str, cache: dict):
    """True if token won, False if lost, None if unresolved/error."""
    key = f"P:{token_id}"
    if cache.get(key) is not None:
        return cache[key]
    try:
        r = requests.get(f"{POLY_GAMMA}/markets?clob_token_ids={token_id}", timeout=10)
        r.raise_for_status()
        data = r.json()
        markets = data if isinstance(data, list) else [data]
        for market in markets:
            if not market.get("closed", False):
                continue
            for tok in market.get("tokens", []):
                if str(tok.get("token_id", "")) == str(token_id):
                    if "winner" in tok:
                        result = bool(tok["winner"])
                    else:
                        result = float(tok.get("price", -1)) >= 0.99
                    cache[key] = result
                    return result
        return None
    except Exception as e:
        print(f"  [warn] Poly resolution fetch failed for token {str(token_id)[:12]}...: {e}")
        return None


def audit_recent_llm_verdicts(days_back: int = 7, output_path: Path = DEFAULT_OUTPUT) -> dict:
    """Spot-check settled auto-accepted pairs: a valid hedge resolves with EXACTLY one side paying."""
    rows = [r for r in _load_json_list(output_path) if r.get("source") == "llm_auto"]
    if not rows:
        print("[AUDIT] No llm_auto pairs to audit.")
        return {"checked": 0, "confirmed": 0, "mismatch": 0, "pending": 0}
    cutoff = datetime.now(timezone.utc) - timedelta(days=days_back)

    def _recent(r):
        sd = _coerce_dt(r.get("settlement_date"))
        if sd is None:
            return True   # keep undated rows too
        if sd.tzinfo is None:
            sd = sd.replace(tzinfo=timezone.utc)
        return sd >= cutoff

    rows = [r for r in rows if _recent(r)]
    cache = _load_resolution_cache()
    checked = confirmed = mismatch = pending = 0
    for r in rows:
        k_res = fetch_kalshi_resolution(r["kalshi_ticker"], cache)
        p_win = fetch_poly_token_wins(r["poly_yes_token"], cache)
        if k_res is None or p_win is None:
            pending += 1
            continue
        checked += 1
        # cross_pairs stores the YES token we'd hold on a K_NO_P_YES; pair is valid iff exactly one pays.
        k_yes_pays = (k_res == "yes")
        if k_yes_pays != bool(p_win):
            confirmed += 1   # exactly one side paid -> hedge held
        else:
            mismatch += 1
            print(f"  [AUDIT MISMATCH] {r['kalshi_ticker']} | K={k_res} Pwin={p_win} | {_short(r.get('label',''), 50)}")
    _save_resolution_cache(cache)
    print(f"[AUDIT] checked={checked} confirmed={confirmed} mismatch={mismatch} pending={pending}")
    return {"checked": checked, "confirmed": confirmed, "mismatch": mismatch, "pending": pending}


# ============================================================================
# STAGE 7 - Operations & polish
# ============================================================================
def notify_summary(stats: dict) -> None:
    """Print a summary; also send Telegram if TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID are set."""
    msg = "[v2 daily] " + "  ".join(f"{k}={v}" for k, v in stats.items())
    print(msg)
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat  = os.environ.get("TELEGRAM_CHAT_ID", "")
    if token and chat:
        try:
            requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                          json={"chat_id": chat, "text": msg}, timeout=15)
            print("[NOTIFY] Telegram summary sent.")
        except Exception as e:
            print(f"[NOTIFY] Telegram send failed: {e}")


def daily_refresh_pipeline(api_key_id, private_key, openrouter_key, output_path=DEFAULT_OUTPUT,
                           no_live=False, sync=False, limit_sources=None) -> None:
    """Stage 7: rebuild index -> coverage snapshot -> batch discovery -> audit -> notify."""
    print("=" * 70 + "\n[DAILY] Refresh pipeline starting\n" + "=" * 70)
    index = load_or_build_index(api_key_id, private_key, force_rebuild=True, no_live=no_live)
    report = compute_coverage_report(index, output_path)
    print_coverage_summary(report)
    daily_snapshot(report)
    disc = {"judged": 0, "auto_accepted": 0, "auto_rejected": 0, "queued": 0}
    if openrouter_key:
        try:
            disc = batch_judge_new_discoveries(index, openrouter_key, output_path,
                                               limit_sources=limit_sources, sync=sync)
        except BalanceExhaustedError as e:
            print(f"[DAILY] {e}")
    audit = audit_recent_llm_verdicts(days_back=7, output_path=output_path)
    notify_summary({
        "indexed": report["total_markets"], "paired_pct": report["paired_pct"],
        "silent": report["silent_count"], "new_auto": disc["auto_accepted"],
        "queued": disc["queued"], "audit_mismatch": audit["mismatch"],
    })


# ============================================================================
# Filters (shared with default/v1 flow)
# ============================================================================
def _apply_v1_filters(candidates, args, weeks_limit):
    now_utc = datetime.now(timezone.utc)

    def _is_past(dt):
        if dt is None: return False
        if dt.tzinfo is None: dt = dt.replace(tzinfo=timezone.utc)
        return dt < now_utc

    before = len(candidates)
    candidates = [c for c in candidates
                  if not _is_past(c["kalshi_close"]) and not _is_past(c["poly_close"])]
    if before - len(candidates):
        print(f"[FILTER] Removed {before - len(candidates)} candidates already closed.")

    if candidates:
        from collections import Counter
        cat_counts = Counter(c["kalshi_category"] or "Unknown" for c in candidates)
        print("[CATEGORIES] " + ", ".join(f"{k}({v})" for k, v in cat_counts.most_common()))

    if weeks_limit is not None:
        tomorrow = (now_utc + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        cutoff   = now_utc + timedelta(weeks=weeks_limit)
        def _in_window(dt):
            if dt is None: return True
            if dt.tzinfo is None: dt = dt.replace(tzinfo=timezone.utc)
            return tomorrow <= dt <= cutoff
        before = len(candidates)
        candidates = [c for c in candidates
                      if c["kalshi_close"] is not None and _in_window(c["kalshi_close"]) and _in_window(c["poly_close"])]
        print(f"[--w{weeks_limit}] {len(candidates)}/{before} candidates in window.")

    def _label_match(c, term):
        t = term.lower()
        return t in c["kalshi_title"].lower() or t in c["poly_question"].lower()

    excl = [t.strip() for t in args.exclude.split(",") if t.strip()]
    incl = [t.strip() for t in args.include.split(",") if t.strip()]
    if excl:
        before = len(candidates)
        candidates = [c for c in candidates if not any(_label_match(c, t) for t in excl)]
        print(f"[--exclude] Removed {before - len(candidates)} ({', '.join(excl)})")
    if incl:
        before = len(candidates)
        candidates = [c for c in candidates if any(_label_match(c, t) for t in incl)]
        print(f"[--include] Kept {len(candidates)}/{before} ({', '.join(incl)})")

    ec = [t.strip().lower() for t in args.exclude_category.split(",") if t.strip()]
    ic = [t.strip().lower() for t in args.include_category.split(",") if t.strip()]
    if ec:
        before = len(candidates)
        candidates = [c for c in candidates if c.get("kalshi_category", "").lower() not in ec]
        print(f"[--exclude-category] Removed {before - len(candidates)} ({', '.join(ec)})")
    if ic:
        before = len(candidates)
        candidates = [c for c in candidates if c.get("kalshi_category", "").lower() in ic]
        print(f"[--include-category] Kept {len(candidates)}/{before} ({', '.join(ic)})")

    if args.n is not None:
        candidates = candidates[:args.n]
        print(f"[--n] Capped to {len(candidates)}.")
    return candidates


# ============================================================================
# Main
# ============================================================================
def _load_private_key(key_path):
    with open(key_path, "rb") as f:
        return serialization.load_pem_private_key(f.read(), password=None)


def _resolve_source_record(index, ident: str):
    """Find a MarketRecord by Kalshi ticker or full/prefix poly token."""
    by_id = _rec_index_by_id(index)
    if ident in by_id:
        return by_id[ident]
    for e in index:
        if e.venue == "poly" and e.id.startswith(ident):
            return e
    return None


# ============================================================================
# Maintenance - prune concluded markets from the pairs file (--clean)
# ============================================================================
# Kalshi market lifecycle (GET /markets, .status; see Documetation/Kalshi_manual_docs.txt):
#   initialized  created, not yet open (opens at open_time)      KEEP - will trade
#   active       open for trading                                KEEP - tradeable now
#   inactive     temporarily deactivated, may be reactivated     KEEP - paused, not over
#   closed       past close_time, awaiting determination         DROP - finished trading
#   determined   result known, settlement timer running          DROP - concluded
#   disputed     result challenged, may be re-determined          DROP - concluded
#   amended      re-determined after a dispute                    DROP - concluded
#   finalized    settlement complete, terminal state              DROP - concluded
# A ticker the API does not return at all is delisted or settled past the historical
# cutoff (only on GET /historical/markets) -> also concluded -> DROP.
_KALSHI_LIVE_STATUSES = {"active", "initialized", "inactive"}

CLEAN_BATCH_SIZE = 90   # tickers per GET /markets?tickers= call (URL-length safe; API limit is 1000 results)


def _fetch_kalshi_statuses(tickers: list, api_key_id: str, private_key) -> dict:
    """ticker -> status for every given ticker the API still lists.

    Uses GET /markets?tickers=<csv> (no status filter, so finalized markets are
    included while they remain past the historical cutoff). Tickers absent from the
    result are simply no longer listed. Raises on a hard API failure so a failed read
    is never mistaken for 'concluded' by the caller.
    """
    status_map = {}
    unique = sorted({t for t in tickers if t})
    total = (len(unique) + CLEAN_BATCH_SIZE - 1) // CLEAN_BATCH_SIZE
    for bi in range(total):
        batch = unique[bi * CLEAN_BATCH_SIZE:(bi + 1) * CLEAN_BATCH_SIZE]
        path = "/markets?limit=1000&tickers=" + ",".join(batch)
        data = _kalshi_get(path, api_key_id, private_key, _label=f"clean {bi+1}/{total}")
        for m in data.get("markets", []):
            tk = m.get("ticker")
            if tk:
                status_map[tk] = m.get("status", "") or ""
        time.sleep(0.15)
    return status_map


def _archive_removed(removed: list, archive_path: Path) -> int:
    """Append removed pairs to an append-only JSONL recovery log, one JSON object per line.

    Each line is the original pair row (all keys preserved for trivial recovery) plus
    `kalshi_status` (why it was archived) and `archived_at` (UTC ISO). Append mode means the
    log accumulates across every --clean run and never overwrites prior removals, so a bad
    clean can be recovered without re-pairing from scratch. Raises on write failure so the
    caller aborts before pruning.
    """
    ts = datetime.now(timezone.utc).isoformat()
    lines = []
    for p, reason in removed:
        rec = dict(p)
        rec["kalshi_status"] = reason
        rec["archived_at"]   = ts
        lines.append(json.dumps(rec, ensure_ascii=False))
    with open(archive_path, "a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return len(lines)


def clean_concluded_pairs(api_key_id: str, private_key, output_path: Path, dry_run: bool = False) -> None:
    """Remove pairs whose Kalshi market has closed/finished (or is no longer listed)."""
    if not output_path.exists():
        print(f"[CLEAN] {output_path.name} not found - nothing to do.")
        return
    try:
        pairs = json.loads(output_path.read_text(encoding="utf-8-sig"))
    except Exception as e:
        print(f"[CLEAN] ABORT - could not parse {output_path.name} ({e}). No changes written.")
        return
    if not isinstance(pairs, list) or not pairs:
        print(f"[CLEAN] {output_path.name} is empty - nothing to do.")
        return

    tickers = [p.get("kalshi_ticker", "") for p in pairs if isinstance(p, dict) and p.get("kalshi_ticker")]
    unique = sorted(set(tickers))
    print(f"[CLEAN] {len(pairs)} pairs, {len(unique)} unique Kalshi tickers to check via GET /markets.")

    try:
        status_map = _fetch_kalshi_statuses(unique, api_key_id, private_key)
    except Exception as e:
        print(f"[CLEAN] ABORT - Kalshi status fetch failed ({e}). No changes written.")
        return

    kept, removed = [], []
    for p in pairs:
        tk = p.get("kalshi_ticker", "") if isinstance(p, dict) else ""
        if not tk:
            kept.append(p)                      # can't evaluate without a ticker - leave untouched
            continue
        status = status_map.get(tk)
        if status in _KALSHI_LIVE_STATUSES:
            kept.append(p)
        else:
            removed.append((p, status if status else "not_listed"))

    if not removed:
        print(f"[CLEAN] All {len(kept)} pairs are still live - nothing to remove.")
        return

    reason_counts = {}
    for _, r in removed:
        reason_counts[r] = reason_counts.get(r, 0) + 1
    print(f"\n[CLEAN] {len(removed)} concluded pair(s) to remove, {len(kept)} to keep:")
    for reason, n in sorted(reason_counts.items(), key=lambda kv: -kv[1]):
        print(f"    {reason:<12} {n}")
    print("  Sample:")
    for p, reason in removed[:15]:
        print(f"    [{reason:<11}] {p.get('kalshi_ticker',''):<34} {_short(p.get('label',''), 50)}")
    if len(removed) > 15:
        print(f"    ... and {len(removed) - 15} more.")

    archive_path = output_path.parent / "closed_markets.jsonl"
    if dry_run:
        print(f"\n[CLEAN] --dry-run: no changes written "
              f"(would archive {len(removed)} pair(s) -> {archive_path.name}).")
        return

    # Archive the removed rows FIRST so the pairing work is never lost - even if the prune
    # below fails. Append-only JSONL accumulates every market ever removed, so a bad clean
    # is recoverable without re-pairing from scratch.
    try:
        n_arch = _archive_removed(removed, archive_path)
    except Exception as e:
        print(f"[CLEAN] ABORT - could not archive removed pairs to {archive_path.name} ({e}). "
              f"No changes written.")
        return
    print(f"\n[CLEAN] Archived {n_arch} removed pair(s) -> {archive_path.name} (append-only recovery log).")

    backup = output_path.with_suffix(".json.bak")
    backup.write_bytes(output_path.read_bytes())
    print(f"[CLEAN] Backed up full file -> {backup.name}")
    tmp = output_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(kept, indent=2), encoding="utf-8")
    _safe_replace(tmp, output_path)
    print(f"[CLEAN] Wrote {len(kept)} live pair(s) -> {output_path.name} (removed {len(removed)}).")


def main() -> None:
    weeks_limit = None
    clean_argv = []
    for arg in sys.argv[1:]:
        m = re.match(r'^--w(\d+)$', arg)
        if m:
            weeks_limit = int(m.group(1))
        else:
            clean_argv.append(arg)
    sys.argv = [sys.argv[0]] + clean_argv

    ap = argparse.ArgumentParser(description="Multi-signal Kalshi <-> Polymarket discovery (v2).")
    ap.add_argument("--output", default=str(DEFAULT_OUTPUT))
    # v2 modes
    ap.add_argument("--rebuild-index", action="store_true", help="Stage 1: force rebuild unified index and exit")
    ap.add_argument("--find", metavar="QUERY", help="Stage 2: BM25 + keyword search, print ranked")
    ap.add_argument("--twin", metavar="TICKER", help="Stage 3: twin search from a source market")
    ap.add_argument("--venue", choices=["kalshi", "poly"], default=None, help="restrict --find target venue")
    ap.add_argument("--coverage", action="store_true", help="Stage 4: coverage report")
    ap.add_argument("--snapshot", action="store_true", help="append a coverage history snapshot")
    ap.add_argument("--event", metavar="EVENT_ID", help="Stage 5: event-level pairing")
    ap.add_argument("--discover", action="store_true", help="Stage 6: batch discovery + confidence routing")
    ap.add_argument("--audit", action="store_true", help="Stage 6: audit settled llm_auto pairs")
    ap.add_argument("--daily", action="store_true", help="Stage 7: full refresh pipeline")
    ap.add_argument("--clean", action="store_true",
                    help="Prune concluded (closed/finished/delisted) Kalshi markets from the pairs file and exit")
    # v1 flags
    ap.add_argument("--no-cache", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--show-prompt", type=int, default=0, metavar="N")
    ap.add_argument("--verbose-judge", action="store_true")
    ap.add_argument("--manual-judge", action="store_true")
    ap.add_argument("--ollama", action="store_true")
    ap.add_argument("--sync", action="store_true")
    ap.add_argument("--exclude", default="")
    ap.add_argument("--include", default="")
    ap.add_argument("--no-live", action="store_true")
    ap.add_argument("--exclude-category", default="")
    ap.add_argument("--include-category", default="")
    ap.add_argument("--n", type=int, default=None, metavar="N")
    args = ap.parse_args()

    output_path    = Path(args.output)
    api_key_id     = os.environ.get("KALSHI_API_KEY_ID", "")
    key_path       = os.environ.get("KALSHI_PRIVATE_KEY_PATH", "")
    openrouter_key = os.environ.get("OPENROUTER_API_KEY", "")
    private_key    = _load_private_key(key_path) if (api_key_id and key_path) else None

    # --- index-only modes (use persisted cache; fetch only if stale/missing) ---
    if args.rebuild_index:
        load_or_build_index(api_key_id, private_key, force_rebuild=True, no_live=args.no_live)
        return

    if args.clean:
        if not api_key_id or not key_path:
            sys.exit("[ERROR] --clean needs KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH.")
        clean_concluded_pairs(api_key_id, private_key, output_path, dry_run=args.dry_run)
        return

    if args.find:
        index = load_or_build_index(api_key_id, private_key, no_live=args.no_live)
        kw_hits  = search_keyword_and(index, extract_search_keywords(args.find), venue=args.venue, limit=10)
        bm_hits  = search_bm25(index, args.find, venue=args.venue, limit=15)
        print(f"\n[FIND] '{args.find}' venue={args.venue or 'both'}")
        print("  -- keyword-AND --")
        for s, e in kw_hits:
            print(f"    [{e.venue}] {_short(e.title, 75)}")
        print("  -- BM25 --")
        for s, e in bm_hits:
            print(f"    [{s:.2f} {e.venue}] {_short(e.title, 70)}")
        return

    if args.twin:
        index = load_or_build_index(api_key_id, private_key, no_live=args.no_live)
        src = _resolve_source_record(index, args.twin)
        if src is None:
            print(f"[TWIN] Source '{args.twin}' not found in index.")
            return
        target = "poly" if src.venue == "kalshi" else "kalshi"
        results = twin_search(src, target, index, limit=20)
        display_two_column(src, results)
        return

    if args.coverage:
        index = load_or_build_index(api_key_id, private_key, no_live=args.no_live)
        report = compute_coverage_report(index, output_path)
        print_coverage_summary(report)
        if args.snapshot:
            daily_snapshot(report)
        return

    if args.event:
        index = load_or_build_index(api_key_id, private_key, no_live=args.no_live)
        enter_event_pairing_mode(args.event, index, output_path, sync=args.sync)
        return

    if args.audit:
        audit_recent_llm_verdicts(days_back=7, output_path=output_path)
        return

    if args.daily:
        if not api_key_id or not key_path:
            sys.exit("[ERROR] --daily needs KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH.")
        daily_refresh_pipeline(api_key_id, private_key, openrouter_key, output_path,
                               no_live=args.no_live, sync=args.sync, limit_sources=args.n)
        return

    if args.discover:
        index = load_or_build_index(api_key_id, private_key, no_live=args.no_live)
        if not openrouter_key:
            sys.exit("[ERROR] --discover needs OPENROUTER_API_KEY.")
        batch_judge_new_discoveries(index, openrouter_key, output_path,
                                    limit_sources=args.n, sync=args.sync)
        return

    # --- default flow (== v1): fetch -> embedding candidates -> judge ---
    if not api_key_id or not key_path:
        sys.exit("[ERROR] Set KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH.")
    if not args.dry_run and not args.manual_judge and not args.ollama and not openrouter_key:
        sys.exit("[ERROR] Set OPENROUTER_API_KEY (or use --dry-run / --manual-judge / --ollama).")

    already_paired = set()
    for p in (output_path, output_path.parent / f"potential_{output_path.name}",
              output_path.parent / "rejected_pairs.json"):
        for e in _load_json_list(p):
            if e.get("kalshi_ticker"):
                already_paired.add(e["kalshi_ticker"])
    if already_paired:
        print(f"[INFO] {len(already_paired)} tickers already seen - skipping.")

    kalshi_markets, poly_markets = fetch_both(api_key_id, private_key, no_live=args.no_live)
    # Refresh the unified index as a side effect so search/coverage stay current.
    index = build_unified_index(kalshi_markets, poly_markets)
    persist_index(index)

    candidates = find_candidates(kalshi_markets, poly_markets, already_paired, not args.no_cache)
    candidates = _apply_v1_filters(candidates, args, weeks_limit)

    if args.dry_run:
        print(f"\n[DRY RUN] Top {min(50, len(candidates))} candidates:")
        for c in candidates[:50]:
            print(f"  {c['score']:.3f} | K: {c['kalshi_title']} <-> P: {c['poly_question']}")
        return
    if not candidates:
        print("[INFO] No candidates found.")
        return
    if args.show_prompt:
        for bi in range(min(args.show_prompt, (len(candidates) + JUDGE_BATCH_SIZE - 1) // JUDGE_BATCH_SIZE)):
            batch = candidates[bi * JUDGE_BATCH_SIZE:(bi + 1) * JUDGE_BATCH_SIZE]
            print(f"\n{'='*60}\nBATCH {bi+1}\n{'='*60}")
            print(_build_prompt(batch))
        return

    if args.manual_judge:
        manual_review_session(candidates, index, output_path, sync=args.sync)
    elif args.ollama:
        run_ollama_judge(candidates, output_path, sync=args.sync)
    else:
        try:
            run_judge(candidates, openrouter_key, output_path, verbose=args.verbose_judge, sync=args.sync)
        except BalanceExhaustedError as e:
            print(f"\n[JUDGE] {e}\n[DONE] Stopping early - add credits and re-run.")
            return
        except JudgeFailedError as e:
            print(f"\n[JUDGE] {e}\n[DONE] Stopping early - pairs saved so far are intact.")
            return
    print("\n[DONE] Pairing complete.")


if __name__ == "__main__":
    main()
