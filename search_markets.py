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

_sd = os.path.dirname(os.path.abspath(__file__))
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
        print(f"\r[POLY] Scanned {total} markets, {len(results)} matches...   ", end="", flush=True)
        if len(events) < limit:
            break
        offset += limit
        time.sleep(0.20)
    print()
    return results

# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    if len(sys.argv) < 2:
        print("Usage: python search_markets.py <keyword>")
        sys.exit(1)

    keyword = " ".join(sys.argv[1:])

    if not API_KEY_ID or not PRIVATE_KEY_PATH:
        print("[ERROR] KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH must be set in .env")
        sys.exit(1)

    private_key = load_key(PRIVATE_KEY_PATH)

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
