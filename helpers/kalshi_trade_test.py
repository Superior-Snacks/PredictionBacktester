#!/usr/bin/env python3
"""
kalshi_trade_test.py — Real-money round-trip trade test on Kalshi.

Reads wallet balance, buys N contracts of a specified market (IOC at best ask),
polls for fill confirmation, immediately re-fetches the book, then sells the
same contracts (IOC at best bid).  Every step is timed and printed.

Usage:
    # Find a ticker first:
    python kalshi_trade_test.py --search "NBA Finals"
    python kalshi_trade_test.py --search "Lakers"

    # Then trade:
    python kalshi_trade_test.py --ticker KXSOMETHING-25-YES --dry-run
    python kalshi_trade_test.py --ticker KXSOMETHING-25-YES
    python kalshi_trade_test.py --ticker KXSOMETHING-25-YES --side no
    python kalshi_trade_test.py --ticker KXSOMETHING-25-YES --contracts 2 --poll-ms 25

WARNING: --dry-run is strongly recommended first.
         A real run costs real money (spread + fees).
"""

import argparse
import base64
import os
import sys
import time
from pathlib import Path

# ─── .env loader ──────────────────────────────────────────────────────────────

_HERE = Path(__file__).parent
_ROOT = _HERE.parent


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


_load_dotenv(str(_HERE), str(_ROOT), str(_ROOT.parent), os.path.expanduser("~"), os.getcwd())

# ─── CONFIG ───────────────────────────────────────────────────────────────────

KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"

# ─── AUTH ─────────────────────────────────────────────────────────────────────

def _load_key(path):
    from cryptography.hazmat.primitives import serialization
    with open(path, "rb") as f:
        return serialization.load_pem_private_key(f.read(), password=None)


def _sign(private_key, ts_ms: str, method: str, path: str) -> str:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding as _pad
    sign_path = path.split("?")[0]
    msg = (ts_ms + method + sign_path).encode("utf-8")
    sig = private_key.sign(
        msg,
        _pad.PSS(mgf=_pad.MGF1(hashes.SHA256()), salt_length=hashes.SHA256().digest_size),
        hashes.SHA256(),
    )
    return base64.b64encode(sig).decode("utf-8")


def _auth_headers(private_key, api_key_id: str, method: str, rel_path: str) -> dict:
    ts_ms     = str(int(time.time() * 1000))
    full_path = f"/trade-api/v2{rel_path.split('?')[0]}"
    return {
        "KALSHI-ACCESS-KEY":       api_key_id,
        "KALSHI-ACCESS-TIMESTAMP": ts_ms,
        "KALSHI-ACCESS-SIGNATURE": _sign(private_key, ts_ms, method, full_path),
        "Content-Type":            "application/json",
    }

# ─── HTTP HELPERS ─────────────────────────────────────────────────────────────

def _get(session, private_key, api_key_id, rel_path) -> tuple:
    """Returns (json_body, elapsed_ms). Raises on HTTP error."""
    hdrs = _auth_headers(private_key, api_key_id, "GET", rel_path)
    t0   = time.perf_counter()
    r    = session.get(KALSHI_BASE + rel_path, headers=hdrs, timeout=10)
    ms   = (time.perf_counter() - t0) * 1000
    r.raise_for_status()
    return r.json(), ms


def _post(session, private_key, api_key_id, rel_path, body: dict) -> tuple:
    """Returns (json_body, elapsed_ms). Raises on HTTP error."""
    hdrs = _auth_headers(private_key, api_key_id, "POST", rel_path)
    t0   = time.perf_counter()
    r    = session.post(KALSHI_BASE + rel_path, headers=hdrs, json=body, timeout=10)
    ms   = (time.perf_counter() - t0) * 1000
    try:
        r.raise_for_status()
    except Exception:
        print(f"\n  HTTP {r.status_code}: {r.text[:400]}")
        raise
    return r.json(), ms

# ─── ORDERBOOK HELPERS ────────────────────────────────────────────────────────

def _parse_book(raw) -> tuple:
    """
    Returns (yes_bids, no_bids) — each is a list of [price_cents, size],
    sorted descending (highest bid first).
    Normalises prices to integer cents regardless of whether the API returned
    integers (e.g. 56) or decimals (e.g. 0.56).
    """
    def _norm(levels):
        out = []
        for price, size in (levels or []):
            p = float(price)
            p = round(p * 100) if p <= 1.0 else int(round(p))
            out.append([p, int(size)])
        return sorted(out, key=lambda x: -x[0])

    book = raw.get("orderbook", raw)
    return _norm(book.get("yes", [])), _norm(book.get("no", []))


def _best_ask_cents(side: str, yes_bids, no_bids):
    """Cheapest available ask for `side` in cents, or None if no liquidity."""
    if side == "yes":
        return (100 - no_bids[0][0]) if no_bids else None
    else:
        return (100 - yes_bids[0][0]) if yes_bids else None


def _best_bid_cents(side: str, yes_bids, no_bids):
    """Best bid for `side` in cents, or None."""
    if side == "yes":
        return yes_bids[0][0] if yes_bids else None
    else:
        return no_bids[0][0] if no_bids else None

# ─── FILL POLLING ─────────────────────────────────────────────────────────────

def _poll_fill(session, private_key, api_key_id, order_id,
               poll_ms: float, timeout_s: float) -> dict:
    """
    Polls GET /portfolio/orders/{order_id} until the order resolves.
    All times are ms elapsed since this function was called (i.e. from POST
    response receipt, not from POST send).
    Returns dict: t_first_fill_ms, t_full_fill_ms, fill_count, status, polls.
    """
    t0              = time.perf_counter()
    t_first_fill_ms = None
    polls           = 0
    status          = "unknown"
    fill_count      = 0.0

    while True:
        elapsed_ms = (time.perf_counter() - t0) * 1000
        if elapsed_ms >= timeout_s * 1000:
            break

        data, _ = _get(session, private_key, api_key_id, f"/portfolio/orders/{order_id}")
        order   = data.get("order", data)
        polls  += 1

        fill_count = float(order.get("fill_count_fp", 0) or 0)
        status     = order.get("status", "unknown")

        if t_first_fill_ms is None and fill_count > 0:
            t_first_fill_ms = (time.perf_counter() - t0) * 1000

        if status in ("executed", "canceled"):
            break

        time.sleep(poll_ms / 1000)

    return {
        "t_first_fill_ms": t_first_fill_ms,
        "t_full_fill_ms":  (time.perf_counter() - t0) * 1000,
        "fill_count":      fill_count,
        "status":          status,
        "polls":           polls,
    }

# ─── MARKET SEARCH ───────────────────────────────────────────────────────────

def run_search(session, private_key, api_key_id, term: str) -> None:
    """
    Fetch all open Kalshi events with nested markets and print every market
    whose ticker, event ticker, or title contains `term` (case-insensitive).
    """
    term_lo  = term.lower()
    matches  = []
    cursor   = ""
    fetched  = 0

    print(f"  Searching open markets for '{term}' ...", flush=True)
    while True:
        path = "/events?status=open&with_nested_markets=true&limit=200"
        if cursor:
            path += f"&cursor={cursor}"
        try:
            data, _ = _get(session, private_key, api_key_id, path)
        except Exception as e:
            sys.exit(f"  ERROR fetching events: {e}")

        for ev in data.get("events", []):
            ev_ticker = ev.get("event_ticker", "")
            ev_title  = ev.get("title", "") or ev.get("sub_title", "")
            for m in ev.get("markets", []):
                ticker    = m.get("ticker", "")
                mkt_title = m.get("title", "") or m.get("yes_sub_title", "")
                close_raw = m.get("expected_expiration_time") or m.get("close_time") or ""
                close_str = close_raw[:10] if close_raw else "?"
                yes_ask   = m.get("yes_ask_dollars") or m.get("yes_ask_price") or "?"
                no_ask    = m.get("no_ask_dollars")  or m.get("no_ask_price")  or "?"
                display   = mkt_title or ev_title

                haystack = f"{ticker} {ev_ticker} {display}".lower()
                if term_lo in haystack:
                    matches.append({
                        "ticker":    ticker,
                        "title":     display,
                        "closes":    close_str,
                        "yes_ask":   yes_ask,
                        "no_ask":    no_ask,
                    })
            fetched += 1

        cursor = data.get("cursor", "")
        if not cursor:
            break

    if not matches:
        print(f"  No open markets found matching '{term}'.")
        return

    print(f"  {len(matches)} match(es):\n")
    print(f"  {'Ticker':<44} {'Closes':>10}  {'YES ask':>7}  {'NO ask':>6}  Title")
    print(f"  {'-'*44} {'-'*10}  {'-'*7}  {'-'*6}  -----")
    for m in matches:
        ya = f"${float(m['yes_ask']):.2f}" if m["yes_ask"] != "?" else "?"
        na = f"${float(m['no_ask']):.2f}"  if m["no_ask"]  != "?" else "?"
        print(f"  {m['ticker']:<44} {m['closes']:>10}  {ya:>7}  {na:>6}  {m['title'][:50]}")
    print()


# ─── DISPLAY ──────────────────────────────────────────────────────────────────

_W = 56


def _sep(ch="═"):
    print(ch * _W)


def _row(label, value):
    print(f"  {label:<30} {value}")


def _ms(v):
    return f"{v:.1f}ms" if v is not None else "—"

# ─── MAIN ─────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="Kalshi real-money round-trip trade test")
    ap.add_argument("--search",    default=None,   help="Search open markets by keyword and exit (e.g. --search 'NBA Finals')")
    ap.add_argument("--ticker",    default=None,   help="Market ticker (e.g. KXNBA-25-BOS-YES)")
    ap.add_argument("--side",      default="yes",  choices=["yes", "no"], help="Side to trade (default: yes)")
    ap.add_argument("--contracts", type=int, default=1, help="Contracts per leg (default: 1)")
    ap.add_argument("--dry-run",   action="store_true", help="Fetch book only — no orders placed")
    ap.add_argument("--poll-ms",   type=float, default=50,  help="REST poll interval ms (default: 50)")
    ap.add_argument("--timeout",   type=float, default=10,  help="Max seconds to wait for fill (default: 10)")
    args = ap.parse_args()

    try:
        import requests as _req
    except ImportError:
        sys.exit("ERROR: pip install requests")

    api_key_id = os.environ.get("KALSHI_API_KEY_ID", "")
    key_path   = os.environ.get("KALSHI_PRIVATE_KEY_PATH", "")

    if not args.search and not args.ticker:
        ap.error("--ticker is required unless using --search")

    if not api_key_id or not key_path:
        sys.exit("ERROR: Set KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH in .env")

    if not os.path.isfile(key_path):
        fname = Path(key_path).name
        for candidate in [_HERE / fname, _ROOT / fname, Path(fname)]:
            if candidate.is_file():
                key_path = str(candidate)
                break

    try:
        private_key = _load_key(key_path)
    except Exception as e:
        sys.exit(f"ERROR loading private key ({key_path}): {e}")

    session = _req.Session()
    session.headers["User-Agent"] = "kalshi-trade-test/1.0"

    if args.search:
        run_search(session, private_key, api_key_id, args.search)
        return

    t_script_start = time.perf_counter()

    # ── Header ────────────────────────────────────────────────────────────────
    _sep()
    print("  KALSHI ROUND-TRIP TRADE TEST")
    _sep()
    _row("Ticker",    args.ticker)
    _row("Side",      args.side.upper())
    _row("Contracts", args.contracts)
    _row("Mode",      "DRY RUN — no orders" if args.dry_run else "LIVE — real money")
    _sep("─")

    # ── T1: Balance ───────────────────────────────────────────────────────────
    print("  [T1] Fetching balance ...", end="", flush=True)
    try:
        bal_data, t1_ms = _get(session, private_key, api_key_id, "/portfolio/balance")
    except Exception as e:
        sys.exit(f"\n  ERROR: {e}")
    balance_before_cents = bal_data.get("balance", 0)
    print(f"  {t1_ms:.1f}ms")
    _row("Balance before", f"${balance_before_cents / 100:.2f}")

    # ── T2: Orderbook ─────────────────────────────────────────────────────────
    print(f"  [T2] Fetching orderbook ...", end="", flush=True)
    try:
        book_data, t2_ms = _get(session, private_key, api_key_id,
                                 f"/markets/{args.ticker}/orderbook")
    except Exception as e:
        sys.exit(f"\n  ERROR: {e}")
    print(f"  {t2_ms:.1f}ms")

    yes_bids, no_bids = _parse_book(book_data)
    ask_cents = _best_ask_cents(args.side, yes_bids, no_bids)
    bid_cents = _best_bid_cents(args.side, yes_bids, no_bids)

    if ask_cents is None:
        sys.exit(f"  ERROR: No liquidity on {args.side.upper()} ask side — cannot buy.")

    spread_cents = ask_cents - (bid_cents or ask_cents)
    _row("Best ask",    f"${ask_cents / 100:.2f}  ({ask_cents}¢)")
    _row("Best bid",    f"${bid_cents / 100:.2f}  ({bid_cents}¢)" if bid_cents else "—")
    _row("Spread",      f"${spread_cents / 100:.2f}  ({spread_cents}¢)")
    _row("Est. cost",   f"${ask_cents * args.contracts / 100:.4f}  (excl. fees)")

    if args.dry_run:
        _sep("─")
        print("  DRY RUN complete — no orders placed.")
        _sep()
        return

    # ── T3: Buy ───────────────────────────────────────────────────────────────
    _sep("─")
    print("  ── BUY")
    _sep("─")

    price_field = "yes_price" if args.side == "yes" else "no_price"
    buy_body = {
        "ticker":        args.ticker,
        "side":          args.side,
        "action":        "buy",
        "count":         args.contracts,
        price_field:     ask_cents,
        "time_in_force": "immediate_or_cancel",
    }

    print(f"  [T3] Submitting buy @ {ask_cents}¢ ...", end="", flush=True)
    try:
        buy_resp, t3_ms = _post(session, private_key, api_key_id, "/portfolio/orders", buy_body)
    except Exception as e:
        sys.exit(f"\n  ERROR placing buy: {e}")

    buy_order    = buy_resp.get("order", buy_resp)
    buy_order_id = buy_order.get("order_id", "")
    print(f"  {t3_ms:.1f}ms")
    _row("Order ID",        buy_order_id)
    _row("Submit→resp",     _ms(t3_ms))

    # Check for immediate fill in POST response
    imm_fill   = float(buy_order.get("fill_count_fp", 0) or 0)
    imm_status = buy_order.get("status", "")

    if imm_status in ("executed", "canceled") or imm_fill > 0:
        buy_fill = {
            "t_first_fill_ms": 0.0 if imm_fill > 0 else None,
            "t_full_fill_ms":  0.0,
            "fill_count":      imm_fill,
            "status":          imm_status,
            "polls":           0,
        }
        _row("Fill (immediate)",  f"{imm_fill:.0f} contracts  [{imm_status}]")
    else:
        print(f"  Polling (every {args.poll_ms:.0f}ms, timeout {args.timeout:.0f}s) ...", flush=True)
        buy_fill = _poll_fill(session, private_key, api_key_id,
                              buy_order_id, args.poll_ms, args.timeout)
        _row("Resp→1st fill",    _ms(buy_fill["t_first_fill_ms"]))
        _row("Resp→full fill",   _ms(buy_fill["t_full_fill_ms"]))
        _row("Poll count",       buy_fill["polls"])
        _row("Fill status",      buy_fill["status"])
        _row("Fill count",       f"{buy_fill['fill_count']:.0f} contracts")

    filled_count = int(buy_fill["fill_count"])
    if filled_count == 0:
        print("\n  No fill received — aborting to avoid orphan position.")
        _sep()
        sys.exit(1)

    # ── T4: Sell ──────────────────────────────────────────────────────────────
    _sep("─")
    print("  ── SELL")
    _sep("─")

    print(f"  [T4] Re-fetching orderbook ...", end="", flush=True)
    try:
        book_data2, t_rebook_ms = _get(session, private_key, api_key_id,
                                        f"/markets/{args.ticker}/orderbook")
    except Exception as e:
        print(f"  WARNING: re-fetch failed ({e}), using stale book")
        t_rebook_ms = 0.0
        book_data2  = book_data
    print(f"  {t_rebook_ms:.1f}ms")
    _row("Book re-fetch", _ms(t_rebook_ms))

    yes_bids2, no_bids2 = _parse_book(book_data2)
    sell_bid = _best_bid_cents(args.side, yes_bids2, no_bids2)
    if sell_bid is None:
        sell_bid = bid_cents or max(1, ask_cents - 1)
        print(f"  WARNING: No {args.side.upper()} bids visible — using fallback {sell_bid}¢")
    _row("Sell price", f"${sell_bid / 100:.2f}  ({sell_bid}¢)")

    sell_body = {
        "ticker":        args.ticker,
        "side":          args.side,
        "action":        "sell",
        "count":         filled_count,
        price_field:     sell_bid,
        "time_in_force": "immediate_or_cancel",
    }

    print(f"  [T4] Submitting sell @ {sell_bid}¢ ...", end="", flush=True)
    try:
        sell_resp, t4_ms = _post(session, private_key, api_key_id, "/portfolio/orders", sell_body)
    except Exception as e:
        print(f"\n  ERROR placing sell: {e}")
        print(f"  !!! OPEN POSITION: {filled_count} {args.side.upper()} on {args.ticker}")
        sys.exit(1)

    sell_order    = sell_resp.get("order", sell_resp)
    sell_order_id = sell_order.get("order_id", "")
    print(f"  {t4_ms:.1f}ms")
    _row("Order ID",       sell_order_id)
    _row("Submit→resp",    _ms(t4_ms))

    imm_sell_fill   = float(sell_order.get("fill_count_fp", 0) or 0)
    imm_sell_status = sell_order.get("status", "")

    if imm_sell_status in ("executed", "canceled") or imm_sell_fill > 0:
        sell_fill = {
            "t_first_fill_ms": 0.0 if imm_sell_fill > 0 else None,
            "t_full_fill_ms":  0.0,
            "fill_count":      imm_sell_fill,
            "status":          imm_sell_status,
            "polls":           0,
        }
        _row("Fill (immediate)", f"{imm_sell_fill:.0f} contracts  [{imm_sell_status}]")
    else:
        print(f"  Polling ...", flush=True)
        sell_fill = _poll_fill(session, private_key, api_key_id,
                               sell_order_id, args.poll_ms, args.timeout)
        _row("Resp→1st fill",   _ms(sell_fill["t_first_fill_ms"]))
        _row("Resp→full fill",  _ms(sell_fill["t_full_fill_ms"]))
        _row("Poll count",      sell_fill["polls"])
        _row("Fill status",     sell_fill["status"])
        _row("Fill count",      f"{sell_fill['fill_count']:.0f} contracts")

    unfilled = filled_count - int(sell_fill["fill_count"])
    if unfilled > 0:
        print(f"\n  WARNING: Sell partially filled — {unfilled} contract(s) remain open.")
        print(f"  !!! OPEN POSITION: {unfilled} {args.side.upper()} on {args.ticker}")

    # ── T5: Final balance ─────────────────────────────────────────────────────
    _sep("─")
    print("  [T5] Fetching final balance ...", end="", flush=True)
    try:
        bal_data2, t5_ms = _get(session, private_key, api_key_id, "/portfolio/balance")
        balance_after_cents = bal_data2.get("balance", 0)
        print(f"  {t5_ms:.1f}ms")
    except Exception as e:
        print(f"  WARNING: {e}")
        balance_after_cents = None
        t5_ms = 0.0

    # ── Summary ───────────────────────────────────────────────────────────────
    t_total_ms   = (time.perf_counter() - t_script_start) * 1000
    rt_fill_ms   = (buy_fill["t_full_fill_ms"]
                    + t_rebook_ms
                    + t4_ms
                    + sell_fill["t_full_fill_ms"])

    _sep("─")
    print("  ── TIMING SUMMARY")
    _sep("─")
    _row("T1  Balance fetch",       _ms(t1_ms))
    _row("T2  Book fetch",          _ms(t2_ms))
    _row("T3  Buy submit→resp",     _ms(t3_ms))
    _row("T3  Buy resp→full fill",  _ms(buy_fill["t_full_fill_ms"]))
    _row("T4  Book re-fetch",       _ms(t_rebook_ms))
    _row("T4  Sell submit→resp",    _ms(t4_ms))
    _row("T4  Sell resp→full fill", _ms(sell_fill["t_full_fill_ms"]))
    _row("T5  Final balance",       _ms(t5_ms))
    _sep("─")
    _row("Total script elapsed",    _ms(t_total_ms))
    _row("Round-trip fill time",    f"{rt_fill_ms:.1f}ms  (buy fill → book → sell fill)")
    _sep("─")

    if balance_after_cents is not None:
        pnl = (balance_after_cents - balance_before_cents) / 100
        _row("Balance before",      f"${balance_before_cents / 100:.2f}")
        _row("Balance after",       f"${balance_after_cents / 100:.2f}")
        _row("P&L  (spread+fees)",  f"${pnl:+.4f}")
    _sep()


if __name__ == "__main__":
    main()
