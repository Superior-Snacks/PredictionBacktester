"""
Arb Event Verifier
Fetches event metadata, live order books, and trading activity for Polymarket events.
Used to verify whether telemetry-detected arbs are real or stale-book artifacts.

Usage: python fetch_event_data.py EVENT_ID [EVENT_ID ...]
"""

import requests
import json
import sys
import os

# Fix Windows console encoding for unicode characters
if sys.platform == "win32":
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

GAMMA_URL = "https://gamma-api.polymarket.com"
CLOB_URL = "https://clob.polymarket.com"
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) QuantBot/1.0"}


def calc_fee(price):
    """Polymarket taker fee: price * 0.04 * (price * (1 - price))"""
    return price * 0.04 * (price * (1.0 - price))


def fetch_event(event_id):
    """Fetch event by ID from Gamma API using direct /events/{id} endpoint."""
    try:
        resp = requests.get(f"{GAMMA_URL}/events/{event_id}", headers=HEADERS, timeout=15)
        if resp.status_code == 200:
            return resp.json()
    except Exception as e:
        print(f"  API error: {e}")
    return None


def fetch_order_book(token_id):
    """Fetch live order book from CLOB API."""
    try:
        resp = requests.get(f"{CLOB_URL}/book", params={"token_id": token_id}, headers=HEADERS, timeout=10)
        resp.raise_for_status()
        return resp.json()
    except Exception:
        return None


def fetch_last_trade(token_id):
    """Fetch last trade price from CLOB API."""
    try:
        resp = requests.get(f"{CLOB_URL}/last-trade-price", params={"token_id": token_id}, headers=HEADERS, timeout=10)
        resp.raise_for_status()
        return resp.json()
    except Exception:
        return None


def parse_token_ids(raw):
    """Handle clobTokenIds being either a JSON array or a JSON-encoded string."""
    if isinstance(raw, list):
        return raw
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return []
    return []


def sum_book_depth(levels, top_n=5):
    """Sum share depth across top N price levels."""
    total = 0.0
    for level in (levels or [])[:top_n]:
        try:
            total += float(level.get("size", 0))
        except (ValueError, TypeError):
            pass
    return total


def analyze_event(event_id):
    print(f"\nLooking up event {event_id}...")
    evt = fetch_event(event_id)

    if not evt:
        print(f"  Event {event_id} not found (searched active + closed events).")
        return

    title = evt.get("title", "Unknown")
    neg_risk = evt.get("negRisk", False)
    markets = evt.get("markets", [])

    # Check if any market is closed
    any_closed = any(m.get("closed", False) for m in markets)
    status = "Closed" if any_closed else "Active"

    print(f'\nEvent {event_id}: "{title}"')
    print(f"  Legs: {len(markets)} | Status: {status} | negRisk: {neg_risk}")

    sum_best_asks = 0.0
    sum_fees = 0.0
    bottleneck_depth = float("inf")
    dead_legs = []
    all_have_books = True

    for i, mkt in enumerate(markets):
        question = mkt.get("question", "?")
        token_ids = parse_token_ids(mkt.get("clobTokenIds", []))
        yes_token = token_ids[0] if token_ids else None

        # Parse mid prices
        outcome_prices = mkt.get("outcomePrices", [])
        if isinstance(outcome_prices, str):
            try:
                outcome_prices = json.loads(outcome_prices)
            except (json.JSONDecodeError, TypeError):
                outcome_prices = []
        mid_price = float(outcome_prices[0]) if outcome_prices else 0.0

        volume = float(mkt.get("volume", 0) or 0)
        end_date = (mkt.get("endDate") or "N/A")[:16]
        closed = mkt.get("closed", False)

        print(f"\n  Leg {i+1}: \"{question}\"")
        print(f"    Token: {yes_token or 'N/A'}")

        if not yes_token:
            print(f"    No token ID found — skipping")
            all_have_books = False
            continue

        # Fetch live order book
        book = fetch_order_book(yes_token)
        if book:
            asks = book.get("asks", [])
            bids = book.get("bids", [])
            best_ask = float(asks[0]["price"]) if asks else 0.0
            best_bid = float(bids[0]["price"]) if bids else 0.0
            ask_depth = sum_book_depth(asks)
            bid_depth = sum_book_depth(bids)

            print(f"    Mid: ${mid_price:.2f} | Ask: ${best_ask:.2f} ({ask_depth:,.0f} shares) | Bid: ${best_bid:.2f} ({bid_depth:,.0f} shares)")

            if best_ask > 0:
                sum_best_asks += best_ask
                sum_fees += calc_fee(best_ask)
                bottleneck_depth = min(bottleneck_depth, ask_depth)
            else:
                all_have_books = False
        else:
            print(f"    Mid: ${mid_price:.2f} | Book: unavailable")
            all_have_books = False

        # Fetch last trade
        last_trade = fetch_last_trade(yes_token)
        lt_price = ""
        lt_side = ""
        if last_trade and last_trade.get("price"):
            lt_price = f"${float(last_trade['price']):.2f}"
            lt_side = last_trade.get("side", "")

        # Activity verdict
        vol_str = f"${volume:,.0f}" if volume > 0 else "$0"
        lt_str = f"{lt_price} {lt_side}" if lt_price else "N/A"

        is_dead = volume == 0 and not lt_price
        is_closed = closed
        marker = ""
        if is_closed:
            marker = "CLOSED"
        elif is_dead:
            marker = "DEAD"
            dead_legs.append(i + 1)
        elif volume == 0:
            marker = "LOW"
            dead_legs.append(i + 1)
        else:
            marker = "ACTIVE"

        print(f"    24h vol: {vol_str} | Last trade: {lt_str} | End: {end_date}    {'✓' if marker == 'ACTIVE' else '⚠'} {marker}")

    # Arb check
    print(f"\n  ── ARB CHECK ──")
    if all_have_books and sum_best_asks > 0:
        net_cost = sum_best_asks + sum_fees
        profit = 1.0 - net_cost
        arb_now = "YES" if profit > 0 else "NO"
        depth_str = f"{bottleneck_depth:,.0f}" if bottleneck_depth < float("inf") else "N/A"

        print(f"  Sum of best asks:  ${sum_best_asks:.4f}")
        print(f"  Estimated fees:    ${sum_fees:.4f}")
        print(f"  Net cost per set:  ${net_cost:.4f}")
        print(f"  Profit per set:    ${profit:.4f}")
        print(f"  Arb exists now?    {arb_now}")
        print(f"  Bottleneck depth:  {depth_str} shares")
    else:
        print(f"  Cannot calculate — missing book data on one or more legs")

    # Verdict
    if any_closed:
        verdict = "DEAD — MARKET CLOSED"
    elif dead_legs:
        leg_list = ", ".join(f"Leg {l}" for l in dead_legs)
        verdict = f"SUSPECT — STALE LEG ({leg_list} has no/low volume)"
    elif all_have_books and sum_best_asks + sum_fees >= 1.0:
        verdict = "NO ARB — cost >= $1.00 at current prices"
    elif all_have_books:
        verdict = "LIKELY REAL — all legs active with depth"
    else:
        verdict = "INCOMPLETE — missing book data"

    print(f"\n  ── VERDICT: {verdict} ──")


def main():
    if len(sys.argv) < 2:
        print("Usage: python fetch_event_data.py EVENT_ID [EVENT_ID ...]")
        print("Example: python fetch_event_data.py 325482")
        print("Example: python fetch_event_data.py 325482 305318")
        sys.exit(1)

    for event_id in sys.argv[1:]:
        analyze_event(event_id.strip())
        print()


if __name__ == "__main__":
    main()
