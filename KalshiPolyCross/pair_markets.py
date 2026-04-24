#!/usr/bin/env python3
"""
pair_markets.py - Fetch open Kalshi + Polymarket sports markets, find semantic
matches using sentence-transformers (local, free), confirm via Gemini judge,
and save results to cross_pairs.json.

Usage:
    python pair_markets.py                          # full run
    python pair_markets.py --output path/to.json    # custom output path
    python pair_markets.py --no-cache               # ignore embedding cache
    python pair_markets.py --dry-run                # print candidates, skip judge
    python pair_markets.py --w1                     # only markets ending within 1 week (not today)
    python pair_markets.py --w2                     # only markets ending within 2 weeks (not today)
    python pair_markets.py --exclude "NBA,NFL"      # drop candidates containing these terms
    python pair_markets.py --include "EPL,Soccer"   # keep only candidates containing these terms
    python pair_markets.py --no-live                # drop markets where Poly secondsDelay > 0 or live=true
    python pair_markets.py --manual-judge           # judge pairs interactively (no Gemini needed)
"""

import argparse, base64, json, os, re, sys, time

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
from sentence_transformers import SentenceTransformer

# -- Constants -----------------------------------------------------------------
KALSHI_BASE_URL   = "https://api.elections.kalshi.com/trade-api/v2"
POLY_GAMMA_URL    = "https://gamma-api.polymarket.com"
KALSHI_CATEGORY   = ""
POLY_CATEGORY     = ""

SIMILARITY_THRESH = 0.78
TOP_N_CANDIDATES  = 5
DATE_WINDOW_DAYS  = 7

JUDGE_BATCH_SIZE  = 25
JUDGE_DELAY_S     = 13
JUDGE_MODELS      = ["gemini-3-flash-preview", "gemini-2.5-flash", "gemini-2.0-flash"]

SCRIPT_DIR        = Path(__file__).parent
CACHE_PATH        = SCRIPT_DIR / "embeddings_cache_bge.json"
DEFAULT_OUTPUT    = SCRIPT_DIR / "cross_pairs.json"

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


def _kalshi_get(path: str, api_key_id: str, private_key, params=None) -> dict:
    r = requests.get(
        KALSHI_BASE_URL + path,
        headers=_kalshi_headers("GET", path, api_key_id, private_key),
        params=params,
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


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
        tmp.replace(CACHE_PATH)
        print(f"[CACHE] Saved - {len(cache)} entries.")
    except Exception as e:
        print(f"[CACHE] Warning: could not save ({e}).")


# -- Phase 1: Fetch markets -----------------------------------------------------
def fetch_kalshi_markets(api_key_id: str, private_key) -> dict:
    """Returns ticker - {title, close_date, rules, event_ticker}."""
    print("[KALSHI] Fetching series categories...")
    series_cats = {}
    cursor = ""
    while True:
        path = "/series?limit=1000" + (f"&cursor={cursor}" if cursor else "")
        data = _kalshi_get(path, api_key_id, private_key)
        for s in data.get("series", []):
            if s.get("ticker") and s.get("category"):
                series_cats[s["ticker"]] = s["category"]
        cursor = data.get("cursor", "")
        if not cursor:
            break
        time.sleep(0.15)
    print(f"[KALSHI] {len(series_cats)} series categories loaded.")

    print("[KALSHI] Fetching open events...")
    markets = {}
    cursor = ""
    total_events = 0
    while True:
        path = "/events?status=open&with_nested_markets=true&limit=200" + (f"&cursor={cursor}" if cursor else "")
        data = _kalshi_get(path, api_key_id, private_key)
        for ev in data.get("events", []):
            total_events += 1
            cat = series_cats.get(ev.get("series_ticker", ""), "")
            if KALSHI_CATEGORY and cat.lower() != KALSHI_CATEGORY.lower():
                continue
            event_ticker = ev.get("event_ticker", "")
            for m in ev.get("markets", []):
                ticker = m.get("ticker", "")
                if not ticker:
                    continue
                close_date = None
                for field in ("expected_expiration_time", "close_time"):
                    val = m.get(field)
                    if val:
                        try:
                            close_date = datetime.fromisoformat(val.replace("Z", "+00:00"))
                            break
                        except Exception:
                            pass
                markets[ticker] = {
                    "title":        m.get("title", ""),
                    "close_date":   close_date,
                    "rules":        m.get("rules_primary", ""),
                    "event_ticker": event_ticker,
                    "category":     cat,
                }
        cursor = data.get("cursor", "")
        if not cursor:
            break
        time.sleep(0.15)
    print(f"[KALSHI] {total_events} events -> {len(markets)} markets in '{KALSHI_CATEGORY}'.")
    return markets


def fetch_poly_markets(no_live: bool = False) -> list:
    """Returns list of {question, yes_token, no_token, end_date, description}."""
    print("[POLY] Fetching active markets...")
    results = []
    skipped_live = 0
    offset, page_size = 0, 500
    while True:
        r = requests.get(
            f"{POLY_GAMMA_URL}/events?active=true&closed=false&limit={page_size}&offset={offset}",
            timeout=30,
        )
        r.raise_for_status()
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
            ev_live = bool(ev.get("live", False))
            end_date = None
            if ev.get("end_date"):
                try:
                    end_date = datetime.fromisoformat(ev["end_date"].replace("Z", "+00:00"))
                except Exception:
                    pass
            description = ev.get("description", "") or ""
            for mkt in ev.get("markets", []):
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
                results.append({
                    "question":    question,
                    "yes_token":   tokens[0],
                    "no_token":    tokens[1],
                    "end_date":    end_date,
                    "description": description,
                })
        if len(arr) < page_size:
            break
        offset += page_size
        time.sleep(0.2)
    if no_live and skipped_live:
        print(f"[POLY] Skipped {skipped_live} live/delayed markets (--no-live).")
    print(f"[POLY] {len(results)} markets fetched.")
    return results


# -- Phase 2: Embeddings --------------------------------------------------------
def find_candidates(
    kalshi_markets: dict,
    poly_markets: list,
    already_paired: set,
    use_cache: bool,
) -> list:
    cache = load_cache() if use_cache else {}

    filtered_poly   = poly_markets
    k_titles_unique = list({m["title"] for m in kalshi_markets.values()})
    print(f"[EMBED] {len(k_titles_unique)} unique Kalshi titles, {len(filtered_poly)} Poly markets.")

    poly_questions = [p["question"] for p in filtered_poly]
    to_encode = [t for t in dict.fromkeys(k_titles_unique + poly_questions) if t not in cache]

    if to_encode:
        print(f"[EMBED] Loading model BAAI/bge-large-en-v1.5 ...")
        model = SentenceTransformer("BAAI/bge-large-en-v1.5")
        print(f"[EMBED] Encoding {len(to_encode)} texts...")
        vecs = model.encode(to_encode, batch_size=256, show_progress_bar=True, normalize_embeddings=True)
        for text, vec in zip(to_encode, vecs):
            cache[text] = vec.tolist()
        save_cache(cache)
    else:
        print(f"[EMBED] All texts served from cache ({len(cache)} entries).")

    # Build poly matrix (L2-normalized - dot product = cosine similarity)
    poly_vecs, poly_valid = [], []
    for p in filtered_poly:
        if p["question"] in cache:
            poly_vecs.append(cache[p["question"]])
            poly_valid.append(p)
    if not poly_vecs:
        print("[EMBED] No Polymarket embeddings - no candidates.")
        return []
    poly_mat = np.array(poly_vecs, dtype=np.float32)  # (N_poly, dim)

    # Build Kalshi matrix — one row per ticker that has an embedding
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

    k_mat      = np.array(k_vecs, dtype=np.float32)  # (N_kalshi, dim)
    CHUNK      = 500
    total_k    = len(k_tickers)
    candidates = []
    print(f"[EMBED] Scoring {total_k} Kalshi tickers against {len(poly_valid)} Poly markets...")

    for chunk_start in range(0, total_k, CHUNK):
        chunk_end   = min(chunk_start + CHUNK, total_k)
        chunk_k     = k_mat[chunk_start:chunk_end]          # (CHUNK, dim)
        scores_mat  = poly_mat @ chunk_k.T                  # (N_poly, CHUNK)

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
                    if abs((kd - pd).total_seconds()) / 86400 > DATE_WINDOW_DAYS:
                        continue
                candidates.append({
                    "kalshi_ticker":   ticker,
                    "kalshi_title":    info["title"],
                    "kalshi_close":    info["close_date"],
                    "kalshi_rules":    info["rules"],
                    "kalshi_event":    info["event_ticker"],
                    "kalshi_category": info.get("category", ""),
                    "poly_question":   p["question"],
                    "poly_yes":        p["yes_token"],
                    "poly_no":         p["no_token"],
                    "poly_close":      p["end_date"],
                    "poly_desc":       p["description"],
                    "score":           float(col[idx]),
                })

        print(f"[EMBED] {chunk_end}/{total_k} tickers scored, {len(candidates)} raw hits so far...", flush=True)

    # Keep top N per Kalshi ticker, sorted by score descending
    candidates.sort(key=lambda c: (c["kalshi_ticker"], -c["score"]))
    top = []
    for _ticker, group in groupby(candidates, key=lambda c: c["kalshi_ticker"]):
        top.extend(list(group)[:TOP_N_CANDIDATES])
    _now = datetime.now(timezone.utc)
    _MAX_DAYS = 365  # beyond this, date proximity contributes 0
    def _blend_key(c):
        score = c["score"]
        dt = c.get("kalshi_close")
        if dt is None:
            date_score = 0.0  # unknown close = no date credit
        else:
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            days = max((dt - _now).days, 0)
            date_score = 1.0 - min(days, _MAX_DAYS) / _MAX_DAYS  # 1.0 = closes today, 0.0 = ≥365d
        return -(0.5 * score + 0.5 * date_score)
    top.sort(key=_blend_key)
    print(f"[EMBED] {len(top)} candidate pairs (threshold={SIMILARITY_THRESH}, top-{TOP_N_CANDIDATES}/ticker).")
    return top


# -- Phase 3: Gemini judge ------------------------------------------------------
_JUDGE_SYSTEM = """\
You are a Lead Quantitative Risk Analyst for a high-frequency trading firm.
Your job is to evaluate if two prediction market rulebooks describe the EXACT SAME underlying mathematical and temporal event for arbitrage purposes.

You must be strict, but you must also understand practical equivalence (e.g., "democrats.org" and "Official Democratic Sources" are identical).

### EVALUATION RUBRIC:
1. VALID: The core event, the final deadline, and the resolution conditions are functionally identical.
2. INVALID (LETHAL TRAPS):
   - Formula Mismatch: Platform A requires an absolute number (> 1.28C), Platform B requires a rank (#1 hottest).
   - Overtime/Tie Mismatch: Platform A includes extra time, Platform B strictly ends at regulation.
3. CONDITIONAL: The core event is the same, but there is a temporal risk that requires gating.
   - Deadline Mismatch: Platform A closes in July, Platform B closes in December. (This is safe ONLY IF the event resolves before July).
   - Cancellation/Snap Election: Different rules for postponed or early events.

### RESPONSE FORMAT:
Respond ONLY with a valid JSON array, one object per pair, in index order. No markdown, no backticks.
Each object must use this exact schema:
{
  "index": <int>,
  "status": "VALID" | "INVALID" | "CONDITIONAL",
  "trap_type": "NONE" | "FORMULA_MISMATCH" | "OVERTIME_MISMATCH" | "DEADLINE_MISMATCH" | "CANCELLATION_MISMATCH" | "SNAP_ELECTION",
  "safe_hours_before_event": <int, use 2 for cancellations, 0 if not applicable>,
  "earliest_cutoff_date": "<YYYY-MM-DD if DEADLINE_MISMATCH, otherwise NONE>",
  "explanation": "<one ruthless sentence>"
}

### MARKETS TO EVALUATE:
"""


def _build_prompt(batch: list) -> str:
    lines = [_JUDGE_SYSTEM]
    for i, c in enumerate(batch):
        kc = c["kalshi_close"].isoformat() if c["kalshi_close"] else "Unknown"
        pc = c["poly_close"].isoformat()   if c["poly_close"]   else "Unknown"
        lines += [
            f"[{i}]",
            f"KALSHI  | Title: {c['kalshi_title']}",
            f"        | Close: {kc}",
            f"        | Rules: {c['kalshi_rules']}",
            f"POLY    | Title: {c['poly_question']}",
            f"        | Close: {pc}",
            f"        | Desc:  {c['poly_desc']}",
            "",
        ]
    return "\n".join(lines)


def _parse_judge_response(text: str, batch_size: int) -> list:
    """Parse the judge JSON array response. Returns list of verdict dicts."""
    # Strip markdown fences
    if text.startswith("```"):
        nl = text.find("\n")
        text = text[nl + 1:] if nl != -1 else text[3:]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()
    # Ensure it starts with [
    if not text.startswith("["):
        m = re.search(r"\[", text)
        text = text[m.start():] if m else "[]"
    try:
        verdicts = json.loads(text)
    except json.JSONDecodeError:
        # Truncated — parse whatever complete objects we can
        verdicts = []
        for obj_text in re.findall(r'\{[^{}]+\}', text, re.DOTALL):
            try:
                verdicts.append(json.loads(obj_text))
            except json.JSONDecodeError:
                pass
        if verdicts:
            print(f"[JUDGE] Partial response - salvaged {len(verdicts)} verdict(s).")
    # Validate and normalise
    result = []
    for v in verdicts:
        if not isinstance(v, dict):
            continue
        idx = v.get("index")
        status = str(v.get("status", "INVALID")).upper()
        if not isinstance(idx, int) or not (0 <= idx < batch_size):
            continue
        if status not in ("VALID", "INVALID", "CONDITIONAL"):
            status = "INVALID"
        result.append({
            "index":                  idx,
            "status":                 status,
            "trap_type":              v.get("trap_type", "NONE"),
            "safe_hours_before_event": v.get("safe_hours_before_event", 0),
            "earliest_cutoff_date":   v.get("earliest_cutoff_date", "NONE"),
            "explanation":            v.get("explanation", ""),
        })
    return result


_MAX_RETRIES_PER_MODEL = 3  # 503s or timeouts before dropping to next model

def _judge_batch(batch: list, gemini_key: str, models: list, verbose: bool = False) -> list:
    """Returns list of verdict dicts (one per evaluated pair, INVALID omitted)."""
    payload = {
        "contents": [{"parts": [{"text": _build_prompt(batch)}]}],
        "generationConfig": {
            "temperature": 0.0,
            "maxOutputTokens": 16384,
            "responseMimeType": "application/json",
        },
    }

    while models:
        model = models[0]
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={gemini_key}"
        retries = 0
        while retries < _MAX_RETRIES_PER_MODEL:
            try:
                r = requests.post(url, json=payload, timeout=120)
                if not r.ok:
                    body = r.text
                    if r.status_code == 429:
                        if "day" in body.lower() or "perday" in body.lower():
                            print(f"\n[JUDGE] {model} daily quota exhausted - falling back.")
                            models.pop(0)
                            break
                        print(f"\n[JUDGE] {model} per-minute quota hit - waiting 65s...")
                        time.sleep(65)
                        continue
                    if r.status_code == 503:
                        retries += 1
                        print(f"\n[JUDGE] {model} overloaded (503) - retry {retries}/{_MAX_RETRIES_PER_MODEL}...")
                        time.sleep(15)
                        continue
                    if r.status_code == 404:
                        print(f"\n[JUDGE] {model} not found - dropping.")
                        models.pop(0)
                        break
                    print(f"\n[JUDGE] {model} error {r.status_code}: {body[:300]}")
                    models.pop(0)
                    break
                data = r.json()
                text = (data.get("candidates", [{}])[0]
                           .get("content", {})
                           .get("parts", [{}])[0]
                           .get("text", "[]")
                           .strip())
                finish = (data.get("candidates", [{}])[0].get("finishReason", ""))
                if verbose:
                    print(f"\n[JUDGE RAW] model={model} finish={finish}\n{text}\n")
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
            except Exception as e:
                retries += 1
                print(f"\n[JUDGE] {model} exception (retry {retries}/{_MAX_RETRIES_PER_MODEL}): {e}")
                if retries >= _MAX_RETRIES_PER_MODEL:
                    break
                time.sleep(5)
        else:
            # Exhausted retries for this model
            pass
        if models and models[0] == model:
            print(f"\n[JUDGE] {model} failed {_MAX_RETRIES_PER_MODEL} times - dropping.")
            models.pop(0)

    print("\n[JUDGE] All models exhausted.")
    return []


def run_judge(candidates: list, gemini_key: str, output_path: Path, verbose: bool = False) -> None:
    potential_path = output_path.parent / f"potential_{output_path.name}"
    models = list(JUDGE_MODELS)
    total  = (len(candidates) + JUDGE_BATCH_SIZE - 1) // JUDGE_BATCH_SIZE
    print(f"[JUDGE] {len(candidates)} candidates -> {total} batch(es) of up to {JUDGE_BATCH_SIZE}")
    print(f"[JUDGE] VALID       -> {output_path}")
    print(f"[JUDGE] CONDITIONAL -> {potential_path}")

    for bi, i in enumerate(range(0, len(candidates), JUDGE_BATCH_SIZE)):
        batch   = candidates[i:i + JUDGE_BATCH_SIZE]
        print(f"  [Batch {bi+1}/{total}] Evaluating {len(batch)} pairs...", end="", flush=True)
        verdicts = _judge_batch(batch, gemini_key, models, verbose=verbose)

        valid       = [batch[v["index"]] for v in verdicts if v["status"] == "VALID"]
        conditional = [(batch[v["index"]], v) for v in verdicts if v["status"] == "CONDITIONAL"]
        print(f"  -> {len(valid)} valid, {len(conditional)} conditional.")

        if valid:
            _save_pairs(valid, output_path)
        if conditional:
            _save_potential_pairs(conditional, potential_path)

        if not models:
            print("[JUDGE] All models exhausted - waiting 5 min before retry...")
            time.sleep(300)
            models[:] = list(JUDGE_MODELS)
            print("[JUDGE] Models restored, resuming.")
        elif i + JUDGE_BATCH_SIZE < len(candidates):
            time.sleep(JUDGE_DELAY_S)


# -- Output ---------------------------------------------------------------------
def _event_root(ticker: str) -> str:
    """KXFOO-26APR20BAR-YES - KXFOO-26APR20BAR"""
    return ticker.rsplit("-", 1)[0] if "-" in ticker else ticker


def _save_pairs(matched: list, output_path: Path) -> None:
    existing = []
    existing_keys: set = set()
    if output_path.exists():
        try:
            existing = json.loads(output_path.read_text(encoding="utf-8-sig"))
            existing_keys = {
                f"{e['kalshi_ticker']}|{e['poly_yes_token']}".lower()
                for e in existing
            }
        except Exception as e:
            print(f"[SAVE] Warning reading existing pairs: {e}")

    added = 0
    for m in matched:
        key = f"{m['kalshi_ticker']}|{m['poly_yes']}".lower()
        if key in existing_keys:
            print(f"[SAVE] Duplicate skipped: {m['kalshi_ticker']}")
            continue
        existing_keys.add(key)
        existing.append({
            "kalshi_ticker":   m["kalshi_ticker"],
            "poly_yes_token":  m["poly_yes"],
            "poly_no_token":   m["poly_no"],
            "label":           m["kalshi_title"],
            "event_id":        _event_root(m["kalshi_ticker"]),
            "settlement_date": m["kalshi_close"].strftime("%Y-%m-%d") if m.get("kalshi_close") else "",
        })
        added += 1

    if added == 0:
        print("[SAVE] No new unique pairs.")
        return

    tmp = output_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(existing, indent=2), encoding="utf-8")
    tmp.replace(output_path)
    print(f"[SAVE] {added} new pair(s) saved to {output_path}.")


def _save_rejected(rejected: list, output_path: Path) -> None:
    """Append INVALID/SKIP judgments to rejected_pairs.json so they are never shown again."""
    existing = []
    existing_keys: set = set()
    if output_path.exists():
        try:
            existing = json.loads(output_path.read_text(encoding="utf-8-sig"))
            existing_keys = {
                f"{e['kalshi_ticker']}|{e['poly_yes_token']}".lower()
                for e in existing
            }
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
    tmp.replace(output_path)


def _save_potential_pairs(conditional: list, output_path: Path) -> None:
    """Save CONDITIONAL pairs (with verdict metadata) to potential_cross_pairs.json."""
    existing = []
    existing_keys: set = set()
    if output_path.exists():
        try:
            existing = json.loads(output_path.read_text(encoding="utf-8-sig"))
            existing_keys = {
                f"{e['kalshi_ticker']}|{e['poly_yes_token']}".lower()
                for e in existing
            }
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
            "safe_hours_before_event": verdict["safe_hours_before_event"],
            "earliest_cutoff_date":    verdict["earliest_cutoff_date"],
            "explanation":             verdict["explanation"],
        })
        added += 1

    if added == 0:
        return

    tmp = output_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(existing, indent=2), encoding="utf-8")
    tmp.replace(output_path)
    print(f"[SAVE] {added} conditional pair(s) saved to {output_path}.")


# -- Manual judge --------------------------------------------------------------
_GREEN = "\033[92m"
_RESET = "\033[0m"

def _getch() -> str:
    """Read one character immediately, no Enter required."""
    try:
        import msvcrt
        ch = msvcrt.getwch()
        if ch in ('\x00', '\xe0'):   # special/arrow key prefix — discard second byte
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


def run_manual_judge(candidates: list, output_path: Path) -> None:
    potential_path = output_path.parent / f"potential_{output_path.name}"
    rejected_path  = output_path.parent / "rejected_pairs.json"
    total = len(candidates)
    print(f"\n[MANUAL] {total} candidates to judge.")
    print("  Keys: 1=VALID  2=CONDITIONAL  3=INVALID  s=skip  q=quit\n")

    for i, c in enumerate(candidates):
        kc = c["kalshi_close"].strftime("%Y-%m-%d") if c["kalshi_close"] else "?"
        pc = c["poly_close"].strftime("%Y-%m-%d")   if c["poly_close"]   else "?"
        cat = c.get("kalshi_category") or "?"
        print(f"--- [{i+1}/{total}] score={c['score']:.3f}  category={cat} ---")
        print(f"  KALSHI  {c['kalshi_ticker']}")
        print(f"          {_GREEN}{c['kalshi_title']}{_RESET}")
        print(f"          closes: {kc}")
        if c["kalshi_rules"]:
            print(f"          rules:  {c['kalshi_rules']}")
        print(f"  POLY    {_GREEN}{c['poly_question']}{_RESET}")
        print(f"          closes: {pc}")
        if c["poly_desc"]:
            print(f"          desc:   {c['poly_desc']}")

        while True:
            try:
                print("  > ", end="", flush=True)
                key = _getch()
            except (EOFError, KeyboardInterrupt):
                print("\n[MANUAL] Interrupted.")
                return
            if key in ("1", "2", "3", "s", "q"):
                print(key)   # echo the pressed key
                break
            if key:          # ignore empty (special keys)
                print(f"\n  Invalid key '{key}'. Use 1/2/3/s/q.")

        if key == "q":
            print("[MANUAL] Quit.")
            break
        if key == "1":
            _save_pairs([c], output_path)
        elif key == "2":
            verdict = {
                "trap_type": "MANUAL",
                "safe_hours_before_event": 0,
                "earliest_cutoff_date": "NONE",
                "explanation": "Manually flagged as conditional.",
            }
            _save_potential_pairs([(c, verdict)], potential_path)
        elif key == "3":
            print("  [INVALID]")
            _save_rejected([(c, "INVALID")], rejected_path)
        elif key == "s":
            print("  [SKIP]")
            _save_rejected([(c, "SKIP")], rejected_path)
        print()


# -- Main -----------------------------------------------------------------------
def main() -> None:
    # Pre-parse --wN week-window flag (argparse rejects flags starting with a digit after --)
    weeks_limit = None
    clean_argv  = []
    for arg in sys.argv[1:]:
        m = re.match(r'^--w(\d+)$', arg)
        if m:
            weeks_limit = int(m.group(1))
        else:
            clean_argv.append(arg)
    sys.argv = [sys.argv[0]] + clean_argv

    ap = argparse.ArgumentParser(description="Pair Kalshi <-> Polymarket markets.")
    ap.add_argument("--output",   default=str(DEFAULT_OUTPUT))
    ap.add_argument("--no-cache",    action="store_true", help="Ignore embedding cache")
    ap.add_argument("--dry-run",     action="store_true", help="Print candidates, skip judge")
    ap.add_argument("--show-prompt", type=int, default=0, metavar="N",
                    help="Print the judge prompt for the first N batches and exit")
    ap.add_argument("--verbose-judge", action="store_true",
                    help="Print raw judge response for each batch")
    ap.add_argument("--manual-judge", action="store_true",
                    help="Judge pairs interactively instead of calling Gemini")
    ap.add_argument("--exclude", default="",
                    help="Comma-separated terms; candidates whose Kalshi title OR Poly question contains any term are removed (case-insensitive)")
    ap.add_argument("--include", default="",
                    help="Comma-separated terms; only candidates whose Kalshi title OR Poly question contains at least one term are kept")
    ap.add_argument("--no-live", action="store_true",
                    help="Drop Poly markets where secondsDelay > 0 or live=true (games currently in progress)")
    ap.add_argument("--exclude-category", default="",
                    help="Comma-separated Kalshi categories to exclude (e.g. 'Sports'); run once to see available categories")
    ap.add_argument("--include-category", default="",
                    help="Comma-separated Kalshi categories to keep (e.g. 'Politics,Economics')")
    args = ap.parse_args()

    output_path = Path(args.output)

    api_key_id = os.environ.get("KALSHI_API_KEY_ID", "")
    key_path   = os.environ.get("KALSHI_PRIVATE_KEY_PATH", "")
    gemini_key = os.environ.get("GEMINI_API_KEY", "")

    if not api_key_id or not key_path:
        sys.exit("[ERROR] Set KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH.")
    if not args.dry_run and not args.manual_judge and not gemini_key:
        sys.exit("[ERROR] Set GEMINI_API_KEY (needed for judge calls; omit with --dry-run or --manual-judge).")

    with open(key_path, "rb") as f:
        private_key = serialization.load_pem_private_key(f.read(), password=None)

    already_paired: set = set()
    _seen_files = [
        (output_path,                                               "paired"),
        (output_path.parent / f"potential_{output_path.name}",     "conditional"),
        (output_path.parent / "rejected_pairs.json",               "rejected"),
    ]
    for _p, _ in _seen_files:
        if not _p.exists():
            continue
        try:
            for e in json.loads(_p.read_text(encoding="utf-8-sig")):
                already_paired.add(e.get("kalshi_ticker", ""))
        except Exception:
            pass
    if already_paired:
        print(f"[INFO] {len(already_paired)} tickers already seen (paired/conditional/rejected) — skipping.")

    kalshi_markets = fetch_kalshi_markets(api_key_id, private_key)
    poly_markets   = fetch_poly_markets(no_live=args.no_live)

    candidates = find_candidates(kalshi_markets, poly_markets, already_paired, not args.no_cache)

    # Always remove markets that have already closed on either platform
    _now_utc = datetime.now(timezone.utc)
    before_past = len(candidates)
    def _is_past(dt):
        if dt is None: return False
        if dt.tzinfo is None: dt = dt.replace(tzinfo=timezone.utc)
        return dt < _now_utc
    candidates = [c for c in candidates
                  if not _is_past(c["kalshi_close"]) and not _is_past(c["poly_close"])]
    removed_past = before_past - len(candidates)
    if removed_past:
        print(f"[FILTER] Removed {removed_past} candidates whose Kalshi or Poly market has already closed.")

    # Print category breakdown so user knows what values to pass
    if candidates:
        from collections import Counter
        cat_counts = Counter(c["kalshi_category"] or "Unknown" for c in candidates)
        print(f"[CATEGORIES] {len(cat_counts)} Kalshi categories in candidates: "
              + ", ".join(f"{k}({v})" for k, v in cat_counts.most_common()))

    if weeks_limit is not None:
        now      = datetime.now(timezone.utc)
        tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        cutoff   = now + timedelta(weeks=weeks_limit)
        before   = len(candidates)
        def _in_window(dt):
            if dt is None: return True   # date unknown — don't exclude
            if dt.tzinfo is None: dt = dt.replace(tzinfo=timezone.utc)
            return tomorrow <= dt <= cutoff
        # Require kalshi_close to be in window (reliable); poly_close checked only if present.
        candidates = [c for c in candidates
                      if c["kalshi_close"] is not None and _in_window(c["kalshi_close"])
                      and _in_window(c["poly_close"])]
        print(f"[--w{weeks_limit}] {len(candidates)} / {before} candidates with Kalshi closing "
              f"{tomorrow.strftime('%Y-%m-%d')} – {cutoff.strftime('%Y-%m-%d')} (today excluded).")

    def _label_match(c, term):
        t = term.lower()
        return t in c["kalshi_title"].lower() or t in c["poly_question"].lower()

    exclude_terms = [t.strip() for t in args.exclude.split(",") if t.strip()]
    include_terms = [t.strip() for t in args.include.split(",") if t.strip()]

    if exclude_terms:
        before = len(candidates)
        candidates = [c for c in candidates if not any(_label_match(c, t) for t in exclude_terms)]
        print(f"[--exclude] Removed {before - len(candidates)} candidates  ({', '.join(exclude_terms)})")

    if include_terms:
        before = len(candidates)
        candidates = [c for c in candidates if any(_label_match(c, t) for t in include_terms)]
        print(f"[--include] Kept {len(candidates)} / {before} candidates  ({', '.join(include_terms)})")

    excl_cats = [t.strip().lower() for t in args.exclude_category.split(",") if t.strip()]
    incl_cats = [t.strip().lower() for t in args.include_category.split(",") if t.strip()]

    if excl_cats:
        before = len(candidates)
        candidates = [c for c in candidates if c.get("kalshi_category", "").lower() not in excl_cats]
        print(f"[--exclude-category] Removed {before - len(candidates)} candidates  ({', '.join(excl_cats)})")

    if incl_cats:
        before = len(candidates)
        candidates = [c for c in candidates if c.get("kalshi_category", "").lower() in incl_cats]
        print(f"[--include-category] Kept {len(candidates)} / {before} candidates  ({', '.join(incl_cats)})")

    if args.dry_run:
        print(f"\n[DRY RUN] Top {min(50, len(candidates))} candidates (judge skipped):")
        for c in candidates[:50]:
            print(f"  {c['score']:.3f} | K: {c['kalshi_title']} <-> P: {c['poly_question']}")
        return

    if not candidates:
        print("[INFO] No candidates found.")
        return

    if args.show_prompt:
        for bi in range(min(args.show_prompt, (len(candidates) + JUDGE_BATCH_SIZE - 1) // JUDGE_BATCH_SIZE)):
            batch = candidates[bi * JUDGE_BATCH_SIZE:(bi + 1) * JUDGE_BATCH_SIZE]
            print(f"\n{'='*60}")
            print(f"BATCH {bi+1} (score range {batch[-1]['score']:.3f}-{batch[0]['score']:.3f})")
            print('='*60)
            print(_build_prompt(batch))
        return

    if args.manual_judge:
        run_manual_judge(candidates, output_path)
    else:
        run_judge(candidates, gemini_key, output_path, verbose=args.verbose_judge)
    print("\n[DONE] Pairing complete.")


if __name__ == "__main__":
    main()
