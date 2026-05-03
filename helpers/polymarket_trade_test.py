#!/usr/bin/env python3
"""
polymarket_trade_test.py — Real-money round-trip trade test on Polymarket.

Reads wallet balance, buys N shares of a specified token (FAK at best ask),
polls for fill confirmation, immediately re-fetches the book, then sells the
same shares (FAK at best bid).  Every step is timed and printed.

Usage:
    # Find a token first:
    python polymarket_trade_test.py --search "NBA Finals"
    python polymarket_trade_test.py --search "Lakers"

    # Then trade:
    python polymarket_trade_test.py --token <TOKEN_ID> --dry-run
    python polymarket_trade_test.py --token <TOKEN_ID>
    python polymarket_trade_test.py --token <TOKEN_ID> --shares 2 --poll-ms 25
    python polymarket_trade_test.py --token <TOKEN_ID> --neg-risk  # sports / NegRisk markets

WARNING: --dry-run is strongly recommended first.
         A real run costs real money (spread + fees).
         Sports/NegRisk markets require --neg-risk for correct order signing.
"""

import argparse
import json
import os
import sys
import time
import uuid
from datetime import datetime, timezone
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

CLOB_HOST      = "https://clob.polymarket.com"
GAMMA_HOST     = "https://gamma-api.polymarket.com"
CHAIN_ID       = 137
LOG_PATH       = _ROOT / "polymarket_trade_test.log"
USDC_CONTRACT  = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"  # USDC on Polygon
USDC_BALANCEOF = [{"constant": True, "inputs": [{"name": "account", "type": "address"}],
                   "name": "balanceOf", "outputs": [{"name": "", "type": "uint256"}],
                   "type": "function"}]

# ─── TRACE LOGGER ─────────────────────────────────────────────────────────────

def _log(trace_id: str, event: str, **fields) -> None:
    """Append one JSONL line to LOG_PATH. grep <trace_id> to see a full lifecycle."""
    entry = {
        "ts":    datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        "trace": trace_id,
        "event": event,
        **fields,
    }
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")

# ─── CLOB CLIENT ──────────────────────────────────────────────────────────────

def _build_client():
    try:
        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import ApiCreds
    except ImportError:
        sys.exit("ERROR: pip install py_clob_client")

    private_key    = os.environ.get("POLY_PRIVATE_KEY", "")
    proxy_address  = os.environ.get("POLY_PROXY_ADDRESS", "")
    api_key        = os.environ.get("POLY_API_KEY", "")
    api_secret     = os.environ.get("POLY_API_SECRET", "")
    api_passphrase = os.environ.get("POLY_API_PASSPHRASE", "")

    if not all([private_key, proxy_address, api_key, api_secret, api_passphrase]):
        sys.exit(
            "ERROR: Set POLY_PRIVATE_KEY, POLY_PROXY_ADDRESS, POLY_API_KEY, "
            "POLY_API_SECRET, POLY_API_PASSPHRASE in .env"
        )

    creds = ApiCreds(api_key=api_key, api_secret=api_secret, api_passphrase=api_passphrase)
    return ClobClient(
        host=CLOB_HOST,
        key=private_key,
        chain_id=CHAIN_ID,
        creds=creds,
        funder=proxy_address,
        signature_type=2,  # POLY_GNOSIS_SAFE
    )

# ─── BOOK HELPERS ─────────────────────────────────────────────────────────────

def _price_of(entry) -> float | None:
    try:
        v = entry.get("price") if isinstance(entry, dict) else getattr(entry, "price", None)
        f = float(v) if v is not None else None
        return f if f is not None and 0 < f < 1 else None
    except (ValueError, TypeError):
        return None


def _parse_book(book) -> dict:
    """Return best_ask and best_bid from get_order_book() response."""
    def entries(field):
        if isinstance(book, dict):
            return book.get(field) or []
        return getattr(book, field, None) or []

    ask_prices = [p for e in entries("asks") if (p := _price_of(e)) is not None]
    bid_prices = [p for e in entries("bids") if (p := _price_of(e)) is not None]
    return {
        "best_ask": min(ask_prices) if ask_prices else None,
        "best_bid": max(bid_prices) if bid_prices else None,
    }

# ─── BALANCE HELPER ───────────────────────────────────────────────────────────

def _get_balance() -> tuple:
    """
    Returns (balance_usd, elapsed_ms).
    Reads USDC balance on-chain from the proxy wallet — same approach as
    ProductionBroker.GetUsdcBalanceAsync() and check_proxy.py.
    """
    t0         = time.perf_counter()
    proxy_addr = os.environ.get("POLY_PROXY_ADDRESS", "")
    if not proxy_addr:
        return None, (time.perf_counter() - t0) * 1000
    try:
        from web3 import Web3
        rpc_url = os.environ.get("POLY_RPC_URL", "https://polygon-rpc.com")
        w3      = Web3(Web3.HTTPProvider(rpc_url))
        usdc    = w3.eth.contract(
            address=Web3.to_checksum_address(USDC_CONTRACT),
            abi=USDC_BALANCEOF,
        )
        raw = usdc.functions.balanceOf(Web3.to_checksum_address(proxy_addr)).call()
        ms  = (time.perf_counter() - t0) * 1000
        return float(raw) / 1e6, ms
    except Exception:
        return None, (time.perf_counter() - t0) * 1000

# ─── FILL POLLING ─────────────────────────────────────────────────────────────

def _poll_fill(client, order_id: str,
               poll_ms: float, timeout_s: float,
               trace_id: str = "", leg: str = "") -> dict:
    """
    Polls client.get_order(order_id) until resolved.
    Returns dict: t_first_fill_ms, t_full_fill_ms, size_matched, status, polls.
    """
    t0              = time.perf_counter()
    t_first_fill_ms = None
    polls           = 0
    status          = "unknown"
    size_matched    = 0.0

    while True:
        elapsed_ms = (time.perf_counter() - t0) * 1000
        if elapsed_ms >= timeout_s * 1000:
            if trace_id:
                _log(trace_id, "POLL_TIMEOUT", leg=leg, order_id=order_id,
                     polls=polls, size_matched=size_matched, elapsed_ms=round(elapsed_ms, 1))
            break

        try:
            order = client.get_order(order_id)
            if not isinstance(order, dict):
                order = order.__dict__ if hasattr(order, "__dict__") else {}
        except Exception:
            order = {}

        polls += 1

        raw_matched = (order.get("size_matched") or order.get("sizeFilled")
                       or order.get("filled") or 0)
        try:
            size_matched = float(raw_matched)
        except (ValueError, TypeError):
            size_matched = 0.0

        status = order.get("status", "unknown")

        if t_first_fill_ms is None and size_matched > 0:
            t_first_fill_ms = (time.perf_counter() - t0) * 1000
            if trace_id:
                _log(trace_id, "FIRST_FILL", leg=leg, order_id=order_id,
                     size_matched=size_matched,
                     elapsed_ms=round(t_first_fill_ms, 1), poll_n=polls)

        if status in ("matched", "canceled"):
            break

        time.sleep(poll_ms / 1000)

    t_full_ms = (time.perf_counter() - t0) * 1000
    if trace_id:
        _log(trace_id, "FILL_RESOLVED", leg=leg, order_id=order_id,
             status=status, size_matched=size_matched,
             elapsed_ms=round(t_full_ms, 1), polls=polls)

    return {
        "t_first_fill_ms": t_first_fill_ms,
        "t_full_fill_ms":  t_full_ms,
        "size_matched":    size_matched,
        "status":          status,
        "polls":           polls,
    }

# ─── MARKET SEARCH ────────────────────────────────────────────────────────────

def run_search(term: str) -> None:
    try:
        import requests
    except ImportError:
        sys.exit("ERROR: pip install requests")

    term_lo = term.lower()
    matches = []
    offset  = 0
    limit   = 100
    fetched = 0

    print(f"  Searching active markets for '{term}' ...", flush=True)
    while True:
        try:
            r = requests.get(f"{GAMMA_HOST}/events", params={
                "active": "true", "closed": "false",
                "limit": limit, "offset": offset,
            }, timeout=15)
            r.raise_for_status()
            events = r.json()
        except Exception as e:
            sys.exit(f"  ERROR fetching events: {e}")

        if not events:
            break

        for ev in events:
            ev_title = ev.get("title", "") or ""
            for mkt in ev.get("markets", []):
                question = mkt.get("question", "") or ""
                slug     = mkt.get("slug", "") or ""
                neg_risk = mkt.get("negRisk") or mkt.get("neg_risk") or False
                end_date = (mkt.get("endDate") or "")[:10]

                token_ids_raw = mkt.get("clobTokenIds")
                if isinstance(token_ids_raw, str):
                    try:
                        token_ids = json.loads(token_ids_raw)
                    except Exception:
                        token_ids = []
                else:
                    token_ids = token_ids_raw or []

                raw_prices = mkt.get("outcomePrices")
                if isinstance(raw_prices, str):
                    try:
                        raw_prices = json.loads(raw_prices)
                    except Exception:
                        raw_prices = []
                raw_prices = raw_prices or []
                try:
                    yes_price = float(raw_prices[0]) if len(raw_prices) > 0 else None
                    no_price  = float(raw_prices[1]) if len(raw_prices) > 1 else None
                except (ValueError, TypeError):
                    yes_price = no_price = None

                haystack = f"{question} {slug} {ev_title}".lower()
                if term_lo in haystack and len(token_ids) >= 2:
                    matches.append({
                        "question":  question,
                        "end_date":  end_date,
                        "neg_risk":  neg_risk,
                        "yes_token": token_ids[0],
                        "no_token":  token_ids[1],
                        "yes_price": yes_price,
                        "no_price":  no_price,
                    })

        fetched += len(events)
        print(f"  Scanned {fetched} events, {len(matches)} match(es) ...", end="\r", flush=True)
        offset += limit

    print(f"  Scanned {fetched} events total.                          ")

    if not matches:
        print(f"  No active markets found matching '{term}'.")
        return

    print(f"  {len(matches)} match(es):\n")
    for m in matches:
        nr    = "  [use --neg-risk]" if m["neg_risk"] else ""
        yp    = f"${m['yes_price']:.2f}" if m["yes_price"] is not None else "?"
        np_   = f"${m['no_price']:.2f}"  if m["no_price"]  is not None else "?"
        print(f"  Closes: {m['end_date']}   YES: {yp}  NO: {np_}{nr}")
        print(f"  Q:   {m['question'][:80]}")
        print(f"  YES: {m['yes_token']}")
        print(f"  NO:  {m['no_token']}")
        print()

# ─── DISPLAY ──────────────────────────────────────────────────────────────────

_W = 56


def _sep(ch="═"):
    print(ch * _W)


def _row(label, value):
    print(f"  {label:<30} {value}")


def _ms(v):
    return f"{v:.1f}ms" if v is not None else "—"

# ─── ORDER HELPERS ────────────────────────────────────────────────────────────

def _order_id(resp: dict) -> str:
    return (resp.get("orderID") or resp.get("order_id")
            or resp.get("id") or "")


def _imm_matched(resp: dict) -> float:
    raw = resp.get("size_matched") or resp.get("sizeFilled") or resp.get("filled") or 0
    try:
        return float(raw)
    except (ValueError, TypeError):
        return 0.0

# ─── MAIN ─────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="Polymarket real-money round-trip trade test")
    ap.add_argument("--search",   default=None, help="Search active markets by keyword and exit")
    ap.add_argument("--token",    default=None, help="CLOB token ID to trade (YES or NO side)")
    ap.add_argument("--shares",   type=float, default=1.0, help="Shares per leg (default: 1)")
    ap.add_argument("--dry-run",  action="store_true", help="Fetch book only — no orders placed")
    ap.add_argument("--poll-ms",  type=float, default=50,  help="REST poll interval ms (default: 50)")
    ap.add_argument("--timeout",  type=float, default=30,  help="Max seconds to wait for fill (default: 30)")
    ap.add_argument("--neg-risk", action="store_true",
                    help="Use NegRisk exchange signing (required for sports/NegRisk markets)")
    args = ap.parse_args()

    if not args.search and not args.token:
        ap.error("--token is required unless using --search")

    if args.search:
        run_search(args.search)
        return

    try:
        from py_clob_client.clob_types import OrderArgs, OrderType, PartialCreateOrderOptions
        from py_clob_client.order_builder.constants import BUY, SELL
    except ImportError:
        sys.exit("ERROR: pip install py_clob_client")

    client = _build_client()

    trace_id       = str(uuid.uuid4())
    t_script_start = time.perf_counter()

    _log(trace_id, "SCRIPT_START",
         token=args.token, shares=args.shares,
         neg_risk=args.neg_risk, dry_run=args.dry_run)

    # ── Header ────────────────────────────────────────────────────────────────
    tok_display = args.token[:40] + "..." if len(args.token) > 43 else args.token
    _sep()
    print("  POLYMARKET ROUND-TRIP TRADE TEST")
    _sep()
    _row("Token",    tok_display)
    _row("Shares",   args.shares)
    _row("NegRisk",  args.neg_risk)
    _row("Mode",     "DRY RUN — no orders" if args.dry_run else "LIVE — real money")
    _row("Trace ID", trace_id)
    _sep("─")

    order_opts = PartialCreateOrderOptions(tick_size="0.01", neg_risk=args.neg_risk)

    # ── T1: Balance ───────────────────────────────────────────────────────────
    print("  [T1] Fetching balance ...", end="", flush=True)
    balance_before, t1_ms = _get_balance()
    _log(trace_id, "BALANCE_FETCH",
         balance_usd=balance_before, elapsed_ms=round(t1_ms, 1))
    print(f"  {t1_ms:.1f}ms")
    _row("Balance before", f"${balance_before:.2f}" if balance_before is not None else "—")

    # ── T2: Book ──────────────────────────────────────────────────────────────
    print(f"  [T2] Fetching order book ...", end="", flush=True)
    try:
        t0   = time.perf_counter()
        book = client.get_order_book(args.token)
        t2_ms = (time.perf_counter() - t0) * 1000
    except Exception as e:
        _log(trace_id, "MARKET_FETCH_ERROR", error=str(e))
        sys.exit(f"\n  ERROR: {e}")
    print(f"  {t2_ms:.1f}ms")

    prices    = _parse_book(book)
    ask_price = prices["best_ask"]
    bid_price = prices["best_bid"]

    _log(trace_id, "MARKET_FETCH",
         token=args.token, best_ask=ask_price, best_bid=bid_price,
         elapsed_ms=round(t2_ms, 1))

    if ask_price is None:
        _log(trace_id, "NO_LIQUIDITY", token=args.token)
        sys.exit("  ERROR: No ask-side liquidity — cannot buy.")

    spread = ask_price - (bid_price or ask_price)
    _row("Best ask",  f"${ask_price:.4f}")
    _row("Best bid",  f"${bid_price:.4f}" if bid_price else "—")
    _row("Spread",    f"${spread:.4f}")
    _row("Est. cost", f"${ask_price * args.shares:.4f}  (excl. fees)")

    if args.dry_run:
        _log(trace_id, "DRY_RUN_COMPLETE")
        _sep("─")
        print("  DRY RUN complete — no orders placed.")
        _sep()
        return

    # ── T3: Buy ───────────────────────────────────────────────────────────────
    _sep("─")
    print("  ── BUY")
    _sep("─")

    _log(trace_id, "ORDER_SUBMIT", leg="buy",
         token=args.token, price=ask_price, size=args.shares)
    print(f"  [T3] Submitting buy @ ${ask_price:.4f} ...", end="", flush=True)

    try:
        signed_buy = client.create_order(
            OrderArgs(price=ask_price, size=args.shares, side=BUY, token_id=args.token),
            options=order_opts,
        )
        t0       = time.perf_counter()
        buy_resp = client.post_order(signed_buy, OrderType.FAK)
        t3_ms    = (time.perf_counter() - t0) * 1000
    except Exception as e:
        _log(trace_id, "ORDER_ERROR", leg="buy", error=str(e))
        sys.exit(f"\n  ERROR placing buy: {e}")

    buy_order_id = _order_id(buy_resp)
    imm_status   = buy_resp.get("status", "")
    imm_matched  = _imm_matched(buy_resp)

    _log(trace_id, "ORDER_RESP", leg="buy",
         order_id=buy_order_id, elapsed_ms=round(t3_ms, 1),
         status=imm_status, size_matched=imm_matched,
         success=buy_resp.get("success"))
    print(f"  {t3_ms:.1f}ms")
    _row("Order ID",    buy_order_id or "(none)")
    _row("Submit→resp", _ms(t3_ms))
    _row("Status",      imm_status)

    if imm_status == "delayed" and buy_order_id:
        print(f"  Polymarket placement delay (sports market) — polling until resolved ...")
        buy_fill   = _poll_fill(client, buy_order_id, args.poll_ms, args.timeout,
                                trace_id=trace_id, leg="buy")
        filled_size = buy_fill["size_matched"]
        _row("Resp→1st fill",  _ms(buy_fill["t_first_fill_ms"]))
        _row("Resp→full fill", _ms(buy_fill["t_full_fill_ms"]))
        _row("Poll count",     buy_fill["polls"])
        _row("Fill status",    buy_fill["status"])
        _row("Size matched",   f"{filled_size:.4f} shares")
    elif imm_status in ("matched", "canceled") or imm_matched > 0:
        if imm_matched > 0:
            filled_size = imm_matched
        elif imm_status == "matched":
            filled_size = args.shares
        else:
            filled_size = 0.0
        buy_fill = {
            "t_first_fill_ms": 0.0 if filled_size > 0 else None,
            "t_full_fill_ms":  0.0,
            "size_matched":    filled_size,
            "status":          imm_status,
            "polls":           0,
        }
        _row("Fill (immediate)", f"{filled_size:.4f} shares  [{imm_status}]")
    else:
        print(f"  Polling (every {args.poll_ms:.0f}ms, timeout {args.timeout:.0f}s) ...", flush=True)
        buy_fill    = _poll_fill(client, buy_order_id, args.poll_ms, args.timeout,
                                 trace_id=trace_id, leg="buy")
        filled_size = buy_fill["size_matched"]
        _row("Resp→1st fill",  _ms(buy_fill["t_first_fill_ms"]))
        _row("Resp→full fill", _ms(buy_fill["t_full_fill_ms"]))
        _row("Poll count",     buy_fill["polls"])
        _row("Fill status",    buy_fill["status"])
        _row("Size matched",   f"{filled_size:.4f} shares")

    if filled_size <= 0:
        _log(trace_id, "NO_FILL_ABORT", leg="buy",
             order_id=buy_order_id, buy_status=buy_fill["status"])
        print("\n  No fill received — aborting to avoid orphan position.")
        _sep()
        sys.exit(1)

    # ── T4: Sell ──────────────────────────────────────────────────────────────
    _sep("─")
    print("  ── SELL")
    _sep("─")

    print(f"  [T4] Re-fetching order book ...", end="", flush=True)
    try:
        t0    = time.perf_counter()
        book2 = client.get_order_book(args.token)
        t_rebook_ms = (time.perf_counter() - t0) * 1000
        _log(trace_id, "PRICE_REFETCH", elapsed_ms=round(t_rebook_ms, 1), stale=False)
    except Exception as e:
        print(f"  WARNING: re-fetch failed ({e}), using stale book")
        _log(trace_id, "PRICE_REFETCH", stale=True, error=str(e))
        t_rebook_ms = 0.0
        book2 = book
    print(f"  {t_rebook_ms:.1f}ms")
    _row("Book re-fetch", _ms(t_rebook_ms))

    prices2    = _parse_book(book2)
    sell_price = prices2["best_bid"]
    if sell_price is None:
        sell_price = bid_price or max(0.01, ask_price - 0.01)
        print(f"  WARNING: No bids visible — using fallback ${sell_price:.4f}")
    _row("Sell price", f"${sell_price:.4f}")

    _log(trace_id, "ORDER_SUBMIT", leg="sell",
         token=args.token, price=sell_price, size=filled_size)
    print(f"  [T4] Submitting sell @ ${sell_price:.4f} ...", end="", flush=True)

    try:
        signed_sell = client.create_order(
            OrderArgs(price=sell_price, size=filled_size, side=SELL, token_id=args.token),
            options=order_opts,
        )
        t0        = time.perf_counter()
        sell_resp = client.post_order(signed_sell, OrderType.FAK)
        t4_ms     = (time.perf_counter() - t0) * 1000
    except Exception as e:
        _log(trace_id, "SELL_FAILED", error=str(e),
             open_size=filled_size, token=args.token)
        print(f"\n  ERROR placing sell: {e}")
        print(f"  !!! OPEN POSITION: {filled_size:.4f} shares on {args.token}")
        sys.exit(1)

    sell_order_id    = _order_id(sell_resp)
    imm_sell_status  = sell_resp.get("status", "")
    imm_sell_matched = _imm_matched(sell_resp)

    _log(trace_id, "ORDER_RESP", leg="sell",
         order_id=sell_order_id, elapsed_ms=round(t4_ms, 1),
         status=imm_sell_status, size_matched=imm_sell_matched,
         success=sell_resp.get("success"))
    print(f"  {t4_ms:.1f}ms")
    _row("Order ID",    sell_order_id or "(none)")
    _row("Submit→resp", _ms(t4_ms))
    _row("Status",      imm_sell_status)

    if imm_sell_status == "delayed" and sell_order_id:
        print(f"  Polymarket placement delay — polling ...")
        sell_fill    = _poll_fill(client, sell_order_id, args.poll_ms, args.timeout,
                                  trace_id=trace_id, leg="sell")
        sell_matched = sell_fill["size_matched"]
        _row("Resp→1st fill",  _ms(sell_fill["t_first_fill_ms"]))
        _row("Resp→full fill", _ms(sell_fill["t_full_fill_ms"]))
        _row("Poll count",     sell_fill["polls"])
        _row("Fill status",    sell_fill["status"])
        _row("Size matched",   f"{sell_matched:.4f} shares")
    elif imm_sell_status in ("matched", "canceled") or imm_sell_matched > 0:
        if imm_sell_matched > 0:
            sell_matched = imm_sell_matched
        elif imm_sell_status == "matched":
            sell_matched = filled_size
        else:
            sell_matched = 0.0
        sell_fill = {
            "t_first_fill_ms": 0.0 if sell_matched > 0 else None,
            "t_full_fill_ms":  0.0,
            "size_matched":    sell_matched,
            "status":          imm_sell_status,
            "polls":           0,
        }
        _row("Fill (immediate)", f"{sell_matched:.4f} shares  [{imm_sell_status}]")
    else:
        print(f"  Polling ...", flush=True)
        sell_fill    = _poll_fill(client, sell_order_id, args.poll_ms, args.timeout,
                                  trace_id=trace_id, leg="sell")
        sell_matched = sell_fill["size_matched"]
        _row("Resp→1st fill",  _ms(sell_fill["t_first_fill_ms"]))
        _row("Resp→full fill", _ms(sell_fill["t_full_fill_ms"]))
        _row("Poll count",     sell_fill["polls"])
        _row("Fill status",    sell_fill["status"])
        _row("Size matched",   f"{sell_matched:.4f} shares")

    unfilled = filled_size - sell_matched
    if unfilled > 0.001:
        _log(trace_id, "OPEN_POSITION",
             open_size=unfilled, token=args.token,
             sell_order_id=sell_order_id, sell_status=sell_fill["status"])
        print(f"\n  WARNING: Sell partially filled — {unfilled:.4f} share(s) remain open.")
        print(f"  !!! OPEN POSITION: {unfilled:.4f} shares on {args.token}")

    # ── T5: Final balance ─────────────────────────────────────────────────────
    _sep("─")
    print("  [T5] Fetching final balance ...", end="", flush=True)
    balance_after, t5_ms = _get_balance()
    if balance_after is not None:
        _log(trace_id, "FINAL_BALANCE",
             balance_usd=balance_after,
             delta_usd=(balance_after - balance_before) if balance_before is not None else None,
             elapsed_ms=round(t5_ms, 1))
        print(f"  {t5_ms:.1f}ms")
    else:
        _log(trace_id, "FINAL_BALANCE_ERROR", elapsed_ms=round(t5_ms, 1))
        print(f"  WARNING: balance fetch failed")

    # ── Summary ───────────────────────────────────────────────────────────────
    t_total_ms = (time.perf_counter() - t_script_start) * 1000
    rt_fill_ms = (buy_fill["t_full_fill_ms"]
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

    if balance_before is not None and balance_after is not None:
        pnl = balance_after - balance_before
        _row("Balance before",     f"${balance_before:.2f}")
        _row("Balance after",      f"${balance_after:.2f}")
        _row("P&L  (spread+fees)", f"${pnl:+.4f}")
    _sep()

    pnl_usd = (balance_after - balance_before) if (
        balance_before is not None and balance_after is not None
    ) else None
    _log(trace_id, "SCRIPT_END",
         total_elapsed_ms=round((time.perf_counter() - t_script_start) * 1000, 1),
         pnl_usd=pnl_usd,
         buy_order_id=buy_order_id,
         sell_order_id=sell_order_id,
         filled_size=filled_size,
         sell_matched=sell_matched)


if __name__ == "__main__":
    main()
