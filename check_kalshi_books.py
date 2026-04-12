"""
Verifies Kalshi arb events from the latest ArbTelemetry_*.csv by fetching
their real order books from the REST API.

For each clean (no fraud flags) event in the telemetry, fetches:
  - Current market status (active / finalized / halted)
  - REST yes_ask / no_bid snapshot prices
  - Sum of YES asks across all legs
  - Actual NO bid depth stack (= available YES ask liquidity)

This confirms whether WS-detected arbs had real, executable depth.

Usage:
  python check_kalshi_books.py
  python check_kalshi_books.py --file path/to/ArbTelemetry_xxx.csv
  python check_kalshi_books.py --all     # include flagged events too
"""
import os, sys, datetime, base64, glob, csv, time, argparse, json
import requests
from collections import defaultdict
from cryptography.hazmat.primitives import serialization, hashes
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives.asymmetric import padding

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

BASE_URL         = "https://api.elections.kalshi.com/trade-api/v2"
API_KEY_ID       = os.environ.get("KALSHI_API_KEY_ID", "")
PRIVATE_KEY_PATH = os.environ.get("KALSHI_PRIVATE_KEY_PATH", "")

PRICE_SUM_LOW_THRESHOLD  = 0.70
PRICE_SUM_HIGH_THRESHOLD = 1.20
REPEAT_SPAM_THRESHOLD    = 10
THIN_DEPTH_THRESHOLD     = 2.0

# ─── AUTH ─────────────────────────────────────────────────────────────────────

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

def api_get(private_key, rel_path):
    full_path = f"/trade-api/v2{rel_path}"
    ts = str(int(datetime.datetime.now().timestamp() * 1000))
    sig = sign(private_key, ts, "GET", full_path)
    r = requests.get(BASE_URL + rel_path, headers={
        "KALSHI-ACCESS-KEY":       API_KEY_ID,
        "KALSHI-ACCESS-SIGNATURE": sig,
        "KALSHI-ACCESS-TIMESTAMP": ts,
    }, timeout=10)
    r.raise_for_status()
    return r.json()

# ─── CSV DISCOVERY + LOADING ──────────────────────────────────────────────────

def find_latest_csv():
    candidates = (
        glob.glob("ArbTelemetry_*.csv") +
        glob.glob("KalshiPaperTrader/ArbTelemetry_*.csv")
    )
    if not candidates:
        return None
    return max(candidates, key=os.path.getctime)

def load_events_from_csv(path, include_flagged):
    """Returns dict: eventId -> {windows, avg_net_cost, avg_depth, total_potential, flags}"""
    rows = []
    event_counts = defaultdict(int)
    with open(path, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            try:
                raw_prices = r["LegPrices"].strip('"')
                leg_prices = [float(p) for p in raw_prices.split("|") if p.strip()]
                event_counts[r["EventId"].strip('"').strip()] += 1
                rows.append({
                    "event":         r["EventId"].strip('"').strip(),
                    "num_legs":      int(r["NumLegs"]),
                    "leg_prices":    leg_prices,
                    "leg_sum":       sum(leg_prices),
                    "duration_ms":   float(r["DurationMs"]),
                    "best_net_cost": float(r["BestNetCost"]),
                    "max_volume":    float(r["MaxVolume"]),
                    "total_potential": float(r["TotalPotentialProfit"]),
                    "net_profit_per_share": float(r["NetProfitPerShare"]),
                })
            except (KeyError, ValueError):
                continue

    # Compute flags
    for r in rows:
        flags = []
        if r["leg_sum"] < PRICE_SUM_LOW_THRESHOLD:   flags.append("PRICE_SUM_LOW")
        if r["leg_sum"] > PRICE_SUM_HIGH_THRESHOLD:  flags.append("PRICE_SUM_HIGH")
        if r["net_profit_per_share"] <= 0:            flags.append("ZERO_PROFIT")
        if r["duration_ms"] < 10:                    flags.append("INSTANT_OPEN_CLOSE")
        if r["duration_ms"] > 3_600_000:             flags.append("IMPLAUSIBLE_DURATION")
        if r["max_volume"] < THIN_DEPTH_THRESHOLD:   flags.append("THIN_DEPTH")
        if r["best_net_cost"] > 1.00:                flags.append("COST_EXCEEDS_1")
        if event_counts[r["event"]] > REPEAT_SPAM_THRESHOLD:
            flags.append("REPEAT_SPAM")
        r["flags"] = flags

    # Aggregate by event
    by_event = defaultdict(list)
    for r in rows:
        by_event[r["event"]].append(r)

    result = {}
    for event, evrows in by_event.items():
        all_flags = set()
        for r in evrows:
            all_flags.update(r["flags"])
        if not include_flagged and all_flags:
            continue
        result[event] = {
            "windows":         len(evrows),
            "avg_net_cost":    sum(r["best_net_cost"] for r in evrows) / len(evrows),
            "avg_depth":       sum(r["max_volume"] for r in evrows) / len(evrows),
            "total_potential": sum(r["total_potential"] for r in evrows),
            "flags":           sorted(all_flags),
            "num_legs":        evrows[0]["num_legs"],
        }
    return result

# ─── BOOK CHECK ───────────────────────────────────────────────────────────────

def check_event(private_key, event_ticker, telemetry_summary):
    print(f"\n{'='*70}")
    ws_cost  = telemetry_summary["avg_net_cost"]
    ws_depth = telemetry_summary["avg_depth"]
    ws_pot   = telemetry_summary["total_potential"]
    ws_wins  = telemetry_summary["windows"]
    flags    = telemetry_summary["flags"]
    flag_str = ",".join(flags) if flags else "OK"

    print(f"  {event_ticker}")
    print(f"  WS telemetry: {ws_wins} windows | avg cost ${ws_cost:.4f} | avg depth {ws_depth:.1f} | total potential ${ws_pot:.2f} | flags={flag_str}")
    print(f"{'='*70}")

    try:
        data = api_get(private_key, f"/events/{event_ticker}?with_nested_markets=true")
        event = data.get("event", {})
        markets = event.get("markets", [])
    except Exception as e:
        print(f"  [ERROR fetching event: {e}]")
        return

    if not markets:
        print("  [No markets returned — event may not exist or ticker is wrong]")
        return

    active_markets = [m for m in markets if m.get("status") == "active"]
    other_markets  = [m for m in markets if m.get("status") != "active"]

    print(f"  Markets: {len(active_markets)} active, {len(other_markets)} non-active "
          f"({', '.join(set(m.get('status','?') for m in other_markets)) or 'none'})")

    # Print raw price-related keys from first market so we can see actual field names
    if markets:
        price_keys = {k: v for k, v in markets[0].items()
                      if any(x in k.lower() for x in ["ask", "bid", "price", "last"])}
        if price_keys:
            print(f"  [RAW price fields on first market]: {price_keys}")
        else:
            print(f"  [RAW first market keys]: {list(markets[0].keys())}")

    if not active_markets:
        print("  *** ALL MARKETS RESOLVED/INACTIVE — arb opportunity no longer exists ***")
        return

    # REST snapshot prices
    rest_yes_ask_sum = 0.0
    print(f"\n  {'Ticker':<45} {'Status':<10} {'YesAsk':>7} {'YesBid':>7} {'NoAsk':>7} {'NoBid':>7}")
    print(f"  {'-'*45} {'-'*10} {'-'*7} {'-'*7} {'-'*7} {'-'*7}")
    for mkt in markets:
        ticker   = mkt.get("ticker", "?")
        status   = mkt.get("status", "?")
        # Kalshi REST uses _dollars suffix for price fields (string, e.g. "0.54")
        def parse_price(key):
            v = mkt.get(key, None)
            if v is None: return 0.0
            try: return float(v)
            except: return 0.0
        yes_ask  = parse_price("yes_ask_dollars") or parse_price("yes_ask_price") or parse_price("yes_ask") / 100
        yes_bid  = parse_price("yes_bid_dollars") or parse_price("yes_bid_price") or parse_price("yes_bid") / 100
        no_ask   = parse_price("no_ask_dollars")  or parse_price("no_ask_price")  or parse_price("no_ask")  / 100
        no_bid   = parse_price("no_bid_dollars")  or parse_price("no_bid_price")  or parse_price("no_bid")  / 100
        print(f"  {ticker:<45} {status:<10} ${yes_ask:>5.2f}  ${yes_bid:>5.2f}  ${no_ask:>5.2f}  ${no_bid:>5.2f}")
        if status == "active" and yes_ask > 0:
            rest_yes_ask_sum += yes_ask

    print(f"\n  REST sum of active YES asks: ${rest_yes_ask_sum:.4f}  "
          f"({'*** REST CONFIRMS ARB ***' if rest_yes_ask_sum < 1.0 else 'no arb at REST snapshot time'})")
    print(f"  WS telemetry avg cost:       ${ws_cost:.4f}")
    delta = abs(rest_yes_ask_sum - ws_cost)
    print(f"  Delta (REST vs WS):          ${delta:.4f}  "
          f"({'MATCH' if delta < 0.05 else 'MISMATCH — book may have moved'})")

    # Fetch actual order book depth for each active market
    print(f"\n  Order book depth (from REST /orderbook):")
    total_rest_depth = None
    first_book = True
    for mkt in active_markets:
        ticker = mkt.get("ticker", "?")
        try:
            book_data = api_get(private_key, f"/markets/{ticker}/orderbook")
            if first_book:
                print(f"  [RAW orderbook response for {ticker}]: {json.dumps(book_data)[:500]}")
                first_book = False
            book = book_data.get("orderbook", book_data)
            no_bids  = book.get("no",  [])   # [[price_cents, size], ...]
            yes_bids = book.get("yes", [])

            yes_ask_depth = sum(float(l[1]) for l in no_bids)   # NO bids = YES ask supply
            yes_bid_depth = sum(float(l[1]) for l in yes_bids)

            # Best YES ask = 1 - highest NO bid
            if no_bids:
                best_no_bid  = max(int(l[0]) for l in no_bids) / 100
                best_yes_ask = round(1.0 - best_no_bid, 2)
                depth_str = f"YES ask depth={yes_ask_depth:.1f} contracts @ best ${best_yes_ask:.2f}"
            else:
                depth_str = "NO BIDS (no YES ask depth)"

            print(f"    {ticker:<45} {depth_str}  | YES bid depth={yes_bid_depth:.1f}")
            time.sleep(0.15)  # polite rate limiting
        except Exception as e:
            print(f"    {ticker:<45} [book fetch failed: {e}]")

# ─── MAIN ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--file", default=None)
    parser.add_argument("--all", action="store_true", help="Include flagged events")
    args = parser.parse_args()

    if not API_KEY_ID or not PRIVATE_KEY_PATH:
        print("ERROR: Set KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH")
        sys.exit(1)

    path = args.file or find_latest_csv()
    if not path:
        print("ERROR: No ArbTelemetry_*.csv found.")
        sys.exit(1)

    print(f"Loading telemetry from: {path}")
    events = load_events_from_csv(path, include_flagged=args.all)

    if not events:
        print("No events to check (all flagged). Use --all to include flagged events.")
        sys.exit(0)

    # Sort by total potential profit descending
    sorted_events = sorted(events.items(), key=lambda x: x[1]["total_potential"], reverse=True)
    print(f"Checking {len(sorted_events)} event(s) via REST API...\n")

    private_key = load_key(PRIVATE_KEY_PATH)

    for event_id, summary in sorted_events:
        check_event(private_key, event_id, summary)
        time.sleep(0.3)

    print(f"\n{'='*70}")
    print("Done.")

if __name__ == "__main__":
    main()
