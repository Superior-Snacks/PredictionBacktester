"""
Search Kalshi and Polymarket for markets matching a keyword.
Prints tickers/token IDs ready to copy into cross_pairs.json.

Usage:
  python search_markets.py "Stanley Cup"
  python search_markets.py "Chiefs"
"""
import os, sys, datetime, base64, time, json
import requests
from cryptography.hazmat.primitives import serialization, hashes
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives.asymmetric import padding

# ─── .env loading ─────────────────────────────────────────────────────────────

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

_sd = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_load_dotenv(_sd, os.path.dirname(_sd), os.path.expanduser("~"), os.getcwd())

KALSHI_BASE      = "https://api.elections.kalshi.com/trade-api/v2"
POLY_GAMMA       = "https://gamma-api.polymarket.com"
API_KEY_ID       = os.environ.get("KALSHI_API_KEY_ID", "")
PRIVATE_KEY_PATH = os.environ.get("KALSHI_PRIVATE_KEY_PATH", "")

# ─── Kalshi auth ──────────────────────────────────────────────────────────────

def load_key(path):
    with open(path, "rb") as f:
        return serialization.load_pem_private_key(f.read(), password=None, backend=default_backend())

def sign(private_key, ts, method, full_path):
    msg = f"{ts}{method}{full_path}".encode("utf-8")
    sig = private_key.sign(
        msg,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256()
    )
    return base64.b64encode(sig).decode("utf-8")

def kalshi_get(private_key, rel_path):
    full_path = f"/trade-api/v2{rel_path}"
    ts = str(int(datetime.datetime.now().timestamp() * 1000))
    sig = sign(private_key, ts, "GET", full_path)
    r = requests.get(KALSHI_BASE + rel_path, headers={
        "KALSHI-ACCESS-KEY":       API_KEY_ID,
        "KALSHI-ACCESS-SIGNATURE": sig,
        "KALSHI-ACCESS-TIMESTAMP": ts,
    }, timeout=10)
    r.raise_for_status()
    return r.json()

# ─── Fetch Kalshi markets ─────────────────────────────────────────────────────

def fetch_kalshi(private_key, keyword):
    results = []
    cursor = ""
    total = 0
    kw = keyword.lower()
    print("[KALSHI] Fetching open markets...", end="", flush=True)
    while True:
        path = f"/events?status=open&with_nested_markets=true&limit=200"
        if cursor:
            path += f"&cursor={cursor}"
        data = kalshi_get(private_key, path)
        events = data.get("events", [])
        cursor = data.get("cursor", "")
        for ev in events:
            for mkt in ev.get("markets", []):
                total += 1
                ticker = mkt.get("ticker", "")
                title  = mkt.get("title", "")
                if kw in title.lower():
                    close = mkt.get("expected_expiration_time") or mkt.get("close_time") or ""
                    if close:
                        try:
                            close = datetime.datetime.fromisoformat(close.replace("Z", "+00:00")).strftime("%Y-%m-%d")
                        except Exception:
                            pass
                    results.append({"ticker": ticker, "title": title, "closes": close})
        print(f"\r[KALSHI] Scanned {total} markets, {len(results)} matches...   ", end="", flush=True)
        if not cursor:
            break
        time.sleep(0.15)
    print()
    return results

# ─── Fetch Polymarket markets ─────────────────────────────────────────────────

def fetch_poly(keyword):
    results = []
    offset  = 0
    limit   = 500
    total   = 0
    dry_pages = 0  # consecutive pages with no new matches
    kw = keyword.lower()
    print("[POLY] Fetching active markets...", end="", flush=True)
    while True:
        r = requests.get(f"{POLY_GAMMA}/events",
                         params={"active": "true", "closed": "false", "limit": limit, "offset": offset},
                         timeout=15)
        r.raise_for_status()
        events = r.json()
        if not events:
            break
        page_matches = 0
        for ev in events:
            for mkt in ev.get("markets", []):
                total += 1
                question = mkt.get("question", "")
                if kw not in question.lower():
                    continue
                raw = mkt.get("clobTokenIds")
                tokens = json.loads(raw) if isinstance(raw, str) else (raw or [])
                if len(tokens) < 2:
                    continue
                close = mkt.get("endDate") or ev.get("end_date") or ""
                if close:
                    try:
                        close = datetime.datetime.fromisoformat(close.replace("Z", "+00:00")).strftime("%Y-%m-%d")
                    except Exception:
                        pass
                results.append({"yes": tokens[0], "no": tokens[1], "question": question, "closes": close})
                page_matches += 1
        print(f"\r[POLY] Scanned {total} markets, {len(results)} matches...   ", end="", flush=True)
        if len(events) < limit:
            break
        # Stop early if we already have results and haven't seen a new match in 3 pages
        if results:
            dry_pages = 0 if page_matches > 0 else dry_pages + 1
            if dry_pages >= 3:
                break
        offset += limit
        time.sleep(0.20)
    print()
    return results

# ─── cross_pairs.json helpers ────────────────────────────────────────────────

def find_cross_pairs_path():
    """Locate cross_pairs.json — check bin output dirs first, then script dir."""
    candidates = [
        os.path.join(_sd, "KalshiPolyCross", "bin", "Debug",   "net10.0", "cross_pairs.json"),
        os.path.join(_sd, "KalshiPolyCross", "bin", "Release",  "net10.0", "cross_pairs.json"),
        os.path.join(_sd, "cross_pairs.json"),
    ]
    for p in candidates:
        if os.path.isfile(p):
            return p
    # Default: next to the script (solution root)
    return candidates[-1]

def load_pairs(path):
    if not os.path.isfile(path):
        return []
    with open(path) as f:
        return json.load(f)

def save_pairs(path, pairs):
    with open(path, "w") as f:
        json.dump(pairs, f, indent=2)

# ─── URL resolution ───────────────────────────────────────────────────────────

def resolve_kalshi_url(private_key, url):
    """Extract ticker from a kalshi.com URL and fetch market details.
    Tries /markets/{ticker} first, then /events/{ticker} as fallback
    (Kalshi URLs sometimes point to an event, not a single market)."""
    ticker = url.rstrip("/").split("/")[-1].upper()
    print(f"[KALSHI] Resolving ticker: {ticker}")

    # Try as a single market first
    r = requests.get(f"{KALSHI_BASE}/markets/{ticker}", headers=_kalshi_headers(private_key, f"/markets/{ticker}"), timeout=10)
    if r.status_code == 200:
        mkt = r.json().get("market", r.json())
        return _parse_kalshi_market(mkt, ticker)

    # Fall back to event (URL may point to an event that contains multiple markets)
    print(f"[KALSHI] Not a market ticker — trying as event ticker...")
    r2 = requests.get(f"{KALSHI_BASE}/events/{ticker}",
                      params={"with_nested_markets": "true"},
                      headers=_kalshi_headers(private_key, f"/events/{ticker}"), timeout=10)
    r2.raise_for_status()
    ev   = r2.json().get("event", r2.json())
    mkts = ev.get("markets", [])
    if not mkts:
        raise ValueError(f"Event {ticker} has no nested markets")
    return [_parse_kalshi_market(m, ticker) for m in mkts]

def _kalshi_headers(private_key, path):
    full_path = f"/trade-api/v2{path}"
    ts  = str(int(datetime.datetime.now().timestamp() * 1000))
    sig = sign(private_key, ts, "GET", full_path)
    return {"KALSHI-ACCESS-KEY": API_KEY_ID, "KALSHI-ACCESS-SIGNATURE": sig, "KALSHI-ACCESS-TIMESTAMP": ts}

def _parse_kalshi_market(mkt, fallback_ticker):
    close = mkt.get("expected_expiration_time") or mkt.get("close_time") or ""
    if close:
        try:
            close = datetime.datetime.fromisoformat(close.replace("Z", "+00:00")).strftime("%Y-%m-%d")
        except Exception:
            pass
    return {"ticker": mkt.get("ticker", fallback_ticker), "title": mkt.get("title", ""), "closes": close}

def resolve_poly_url(url):
    """Extract slug from a polymarket.com URL and fetch token IDs."""
    slug = url.rstrip("/").split("/")[-1]
    print(f"[POLY] Resolving slug: {slug}")
    r = requests.get(f"{POLY_GAMMA}/events", params={"slug": slug}, timeout=15)
    r.raise_for_status()
    events = r.json()
    if not events:
        raise ValueError(f"No Polymarket event found for slug '{slug}'")
    ev = events[0]
    markets = ev.get("markets", [])
    if not markets:
        raise ValueError(f"Event '{slug}' has no markets")
    # Return all markets in the event (there may be multiple outcomes)
    results = []
    for mkt in markets:
        question = mkt.get("question", "")
        raw      = mkt.get("clobTokenIds")
        tokens   = json.loads(raw) if isinstance(raw, str) else (raw or [])
        if len(tokens) >= 2:
            close = mkt.get("endDate") or ev.get("end_date") or ""
            if close:
                try:
                    close = datetime.datetime.fromisoformat(close.replace("Z", "+00:00")).strftime("%Y-%m-%d")
                except Exception:
                    pass
            results.append({"yes": tokens[0], "no": tokens[1], "question": question, "closes": close})
    return results

# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    if len(sys.argv) < 2:
        print("Usage:")
        print("  python search_markets.py <keyword>")
        print("  python search_markets.py <kalshi-url> <polymarket-url>")
        sys.exit(1)

    if not API_KEY_ID or not PRIVATE_KEY_PATH:
        print("[ERROR] KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH must be set in .env")
        sys.exit(1)

    private_key = load_key(PRIVATE_KEY_PATH)

    # URL mode: resolve both URLs directly and print the snippet
    args = sys.argv[1:]
    kalshi_url = next((a for a in args if "kalshi.com" in a), None)
    poly_url   = next((a for a in args if "polymarket.com" in a), None)

    if kalshi_url or poly_url:
        W = 55
        k_list = resolve_kalshi_url(private_key, kalshi_url) if kalshi_url else None
        if isinstance(k_list, dict):
            k_list = [k_list]
        p_list = resolve_poly_url(poly_url) if poly_url else None

        if k_list:
            print(f"\n{'─' * W}")
            print(f"  KALSHI — {len(k_list)} outcome(s)")
            print(f"{'─' * W}")
            for i, k in enumerate(k_list):
                print(f"  [{i}] {k['ticker']}")
                print(f"      {k['title']}  (closes {k['closes']})\n")

        if p_list:
            print(f"{'─' * W}")
            print(f"  POLYMARKET — {len(p_list)} outcome(s)")
            print(f"{'─' * W}")
            for i, p in enumerate(p_list):
                print(f"  [{i}] YES: {p['yes']}")
                print(f"       NO:  {p['no']}")
                print(f"      {p['question']}  (closes {p['closes']})\n")

        if k_list and p_list:
            pairs_path = find_cross_pairs_path()
            pairs      = load_pairs(pairs_path)
            saved = 0
            for p in p_list:
                # Match each Poly outcome to the best Kalshi outcome.
                # First try ticker-suffix hints (BRI/CFC/TIE etc), then fall back to title word overlap.
                q = p['question'].lower()
                def match_score(k):
                    suffix = k['ticker'].split("-")[-1].lower()  # e.g. "bri", "cfc", "tie"
                    suffix_hit = suffix in q or any(suffix in w for w in q.split())
                    word_hits  = sum(w in k['title'].lower() for w in q.split() if len(w) > 3)
                    return (suffix_hit * 100) + word_hits
                matched = sorted(k_list, key=match_score, reverse=True)[0]
                ticker = matched['ticker']
                if any(pr.get("kalshi_ticker", "").upper() == ticker.upper() for pr in pairs):
                    print(f"  [SKIP] {ticker} already in file")
                else:
                    entry = {"kalshi_ticker": ticker, "poly_yes_token": p['yes'],
                             "poly_no_token": p['no'], "label": p['question']}
                    pairs.append(entry)
                    print(f"  [SAVED] {ticker}  ←→  {p['question']}")
                    saved += 1
            if saved:
                save_pairs(pairs_path, pairs)
                print(f"\n  {saved} pair(s) added to {pairs_path}  ({len(pairs)} total)\n")
        return

    keyword = " ".join(args)
    kalshi_results = fetch_kalshi(private_key, keyword)
    poly_results   = fetch_poly(keyword)

    W = 55
    print(f"\n{'─' * W}")
    print(f"  KALSHI — {len(kalshi_results)} result(s)  [{keyword}]")
    print(f"{'─' * W}")
    if kalshi_results:
        for i, m in enumerate(kalshi_results):
            print(f"  [{i}] {m['ticker']}")
            print(f"      {m['title']}")
            print(f"      Closes: {m['closes'] or 'unknown'}\n")
    else:
        print("  (none)\n")

    print(f"{'─' * W}")
    print(f"  POLYMARKET — {len(poly_results)} result(s)  [{keyword}]")
    print(f"{'─' * W}")
    if poly_results:
        for i, m in enumerate(poly_results):
            print(f"  [{i}] YES: {m['yes']}")
            print(f"       NO:  {m['no']}")
            print(f"      {m['question']}")
            print(f"      Closes: {m['closes'] or 'unknown'}\n")
    else:
        print("  (none)\n")

    print(f"{'─' * W}")
    print(f"  cross_pairs.json snippet — fill in and add to the file")
    print(f"{'─' * W}")
    snippet = {"kalshi_ticker": "<ticker>", "poly_yes_token": "<yes_token>", "poly_no_token": "<no_token>", "label": keyword}
    print(f"  {json.dumps(snippet)}\n")

if __name__ == "__main__":
    main()
