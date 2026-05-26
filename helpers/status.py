"""
KalshiPolyCross live status checker.
Reads CrossArbJournal_*.jsonl + queries Kalshi and Polymarket REST APIs
to show open positions, balances, live prices, and unrealized P&L.

Usage:
  python helpers/status.py                  # auto-discover journal
  python helpers/status.py --dir /path      # specify journal + pairs dir
  python helpers/status.py --watch 30       # refresh every 30 seconds
  python helpers/status.py --no-color       # disable ANSI colors

Required env:  KALSHI_API_KEY_ID, KALSHI_PRIVATE_KEY_PATH
Optional env:  POLY_API_KEY, POLY_API_SECRET, POLY_API_PASSPHRASE,
               POLY_PROXY_ADDRESS  (enable Poly USDC balance)
               POLY_SOCKS_PROXY    (SOCKS5 proxy for Poly REST)
"""
import os, sys, json, glob, argparse, time, hmac, hashlib, base64, datetime
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests
from cryptography.hazmat.primitives import serialization, hashes
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives.asymmetric import padding

# ── Paths ──────────────────────────────────────────────────────────────────────

_SCRIPT_DIR = Path(__file__).resolve().parent
_ROOT       = _SCRIPT_DIR.parent

# ── .env loader (identical to check_kalshi_books.py) ──────────────────────────

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

_load_dotenv(str(_SCRIPT_DIR), str(_ROOT), os.path.expanduser("~"), os.getcwd())

KALSHI_BASE     = "https://api.elections.kalshi.com/trade-api/v2"
POLY_CLOB_BASE  = "https://clob.polymarket.com"
KALSHI_API_KEY  = os.environ.get("KALSHI_API_KEY_ID", "")
KALSHI_KEY_PATH = os.environ.get("KALSHI_PRIVATE_KEY_PATH", "")
POLY_API_KEY    = os.environ.get("POLY_API_KEY", "")
POLY_API_SECRET = os.environ.get("POLY_API_SECRET", "")
POLY_PASSPHRASE = os.environ.get("POLY_API_PASSPHRASE", "")
POLY_ADDRESS    = os.environ.get("POLY_PROXY_ADDRESS", "")
POLY_SOCKS      = os.environ.get("POLY_SOCKS_PROXY", "")

_POLY_HAS_CREDS = all([POLY_API_KEY, POLY_API_SECRET, POLY_PASSPHRASE, POLY_ADDRESS])

# ── Kalshi RSA-PSS auth ────────────────────────────────────────────────────────

def _load_key(path):
    with open(path, "rb") as f:
        return serialization.load_pem_private_key(f.read(), password=None, backend=default_backend())

def _kalshi_sign(private_key, ts, method, full_path):
    msg = f"{ts}{method}{full_path}".encode("utf-8")
    sig = private_key.sign(
        msg,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )
    return base64.b64encode(sig).decode("utf-8")

def _kalshi_get(private_key, rel_path):
    full_path = f"/trade-api/v2{rel_path}"
    ts = str(int(datetime.datetime.now().timestamp() * 1000))
    r = requests.get(KALSHI_BASE + rel_path, headers={
        "KALSHI-ACCESS-KEY":       KALSHI_API_KEY,
        "KALSHI-ACCESS-SIGNATURE": _kalshi_sign(private_key, ts, "GET", full_path),
        "KALSHI-ACCESS-TIMESTAMP": ts,
    }, timeout=10)
    r.raise_for_status()
    return r.json()

# ── Polymarket HMAC L1 auth (for USDC balance) ─────────────────────────────────

def _poly_l1_headers(method, path):
    ts  = str(int(time.time()))
    msg = (ts + method + path).encode("utf-8")
    try:
        key_bytes = base64.b64decode(POLY_API_SECRET + "==")
    except Exception:
        key_bytes = POLY_API_SECRET.encode("utf-8")
    sig = base64.b64encode(hmac.new(key_bytes, msg, hashlib.sha256).digest()).decode("utf-8")
    return {
        "POLY_API_KEY":    POLY_API_KEY,
        "POLY_PASSPHRASE": POLY_PASSPHRASE,
        "POLY_SIGNATURE":  sig,
        "POLY_TIMESTAMP":  ts,
        "POLY_ADDRESS":    POLY_ADDRESS,
    }

# ── Fee model (mirrors CrossArbExecutor.cs) ────────────────────────────────────

def kalshi_fee(p): return 0.07 * p * (1.0 - p)
def poly_fee(p):   return p * 0.04 * p * (1.0 - p)

# ── Journal discovery ──────────────────────────────────────────────────────────

def _find_journals(search_dirs):
    for d in search_dirs:
        if not d:
            continue
        found = sorted(glob.glob(os.path.join(str(d), "CrossArbJournal_*.jsonl")))
        if found:
            return found
    return []

def _find_pairs_file(search_dirs):
    for d in search_dirs:
        if not d:
            continue
        p = os.path.join(str(d), "cross_pairs.json")
        if os.path.isfile(p):
            return p
    return None

# ── Journal parsing ────────────────────────────────────────────────────────────

def _ticker_from_pair_id(pair_id):
    """'MANUAL_KXFEDDECISION-26SEP-C25__57748138'  →  'KXFEDDECISION-26SEP-C25'"""
    return pair_id.split("__")[0].split("_", 1)[1]

def load_journal_state(journal_files):
    """
    Replays journal events to determine:
      open_positions  — {execId: EXECUTION_COMPLETE event dict}
      closed_today    — [EARLY_EXIT_COMPLETE events in last 24h]
    """
    events = []
    for path in journal_files:
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            events.append(json.loads(line))
                        except json.JSONDecodeError:
                            pass
        except OSError:
            pass

    events.sort(key=lambda e: e.get("t", ""))
    cutoff = (datetime.datetime.utcnow() - datetime.timedelta(hours=24)).isoformat()

    open_positions = {}
    closed_today   = []

    for ev in events:
        eid = ev.get("execId", "")
        evt = ev.get("event", "")

        if evt == "EXECUTION_COMPLETE" and ev.get("outcome") == "FILLED" and ev.get("position"):
            open_positions[eid] = ev

        elif evt in ("EARLY_EXIT_COMPLETE", "SETTLEMENT_DETECTED"):
            open_positions.pop(eid, None)
            if ev.get("t", "") >= cutoff:
                closed_today.append(ev)

    return open_positions, closed_today

# ── cross_pairs lookup ─────────────────────────────────────────────────────────

def load_pairs(pairs_file):
    if not pairs_file or not os.path.isfile(pairs_file):
        return {}
    with open(pairs_file, encoding="utf-8") as f:
        data = json.load(f)
    return {p["kalshi_ticker"]: p for p in data if "kalshi_ticker" in p}

# ── API fetchers ───────────────────────────────────────────────────────────────

def fetch_kalshi_market(private_key, ticker):
    try:
        data = _kalshi_get(private_key, f"/markets/{ticker}")
        mkt  = data.get("market", data)
        def _f(k):
            v = mkt.get(k)
            return float(v) if v is not None else None
        return {
            "yes_ask": _f("yes_ask_dollars"),
            "yes_bid": _f("yes_bid_dollars"),
            "no_ask":  _f("no_ask_dollars"),
            "no_bid":  _f("no_bid_dollars"),
            "status":  mkt.get("status", "?"),
        }
    except Exception as e:
        return {"error": str(e)}

def fetch_poly_book(token_id, proxies=None):
    try:
        r = requests.get(f"{POLY_CLOB_BASE}/book", params={"token_id": token_id},
                         timeout=10, proxies=proxies)
        r.raise_for_status()
        book = r.json()
        asks = book.get("asks", [])
        bids = book.get("bids", [])
        # asks sorted descending → best ask (lowest) = last element
        # bids sorted ascending  → best bid (highest) = last element
        best_ask = float(asks[-1]["price"]) if asks else None
        best_bid = float(bids[-1]["price"]) if bids else None
        return {"ask": best_ask, "bid": best_bid}
    except Exception as e:
        return {"error": str(e)}

def fetch_kalshi_balance(private_key):
    try:
        return _kalshi_get(private_key, "/portfolio/balance").get("balance", 0) / 100.0
    except Exception:
        return None

def fetch_kalshi_positions(private_key):
    try:
        data = _kalshi_get(private_key, "/portfolio/positions")
        return {p["ticker"]: p.get("position", 0)
                for p in data.get("market_positions", [])
                if p.get("ticker") and p.get("position", 0) != 0}
    except Exception:
        return {}

def fetch_poly_usdc_balance(proxies=None):
    if not _POLY_HAS_CREDS:
        return None
    path = "/balance-allowance"
    headers = _poly_l1_headers("GET", path)
    try:
        r = requests.get(POLY_CLOB_BASE + path,
                         params={"asset_type": "COLLATERAL", "signature_type": "2"},
                         headers=headers, timeout=10, proxies=proxies)
        r.raise_for_status()
        data = r.json()
        raw = data.get("balance") or data.get("availableBalance") or data.get("allowance") or 0
        return float(raw) / 1_000_000
    except Exception:
        return None

# ── ANSI colors ────────────────────────────────────────────────────────────────

class C:
    RST  = "\033[0m"
    BOLD = "\033[1m"
    GRN  = "\033[92m"
    YLW  = "\033[93m"
    RED  = "\033[91m"
    CYN  = "\033[96m"
    GRY  = "\033[90m"
    WHT  = "\033[97m"

def _c(text, code, use_color):
    return f"{code}{text}{C.RST}" if use_color else text

def _pnl_color(val, use_color):
    if not use_color: return ""
    if val > 0.001:   return C.GRN
    if val < -0.005:  return C.RED
    return C.YLW

# ── Formatting helpers ─────────────────────────────────────────────────────────

def _age(iso_str):
    try:
        t = datetime.datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        s = int((datetime.datetime.now(datetime.timezone.utc) - t).total_seconds())
        if s < 60:    return f"{s}s ago"
        if s < 3600:  return f"{s//60}m ago"
        h, m = divmod(s // 60, 60)
        return f"{h}h {m:02d}m ago"
    except Exception:
        return "?"

def _days_to(date_str):
    try:
        d = (datetime.date.fromisoformat(date_str) - datetime.date.today()).days
        return f"{date_str}  ({d}d away)" if d >= 0 else f"{date_str}  (PAST)"
    except Exception:
        return date_str or "?"

SEP = "─" * 68

# ── Main render ────────────────────────────────────────────────────────────────

def render(open_positions, closed_today, pairs_by_ticker, private_key, proxies, use_color):
    lines = []
    w = lines.append

    now_str = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    w(_c(f"KalshiPolyCross Status  —  {now_str}", C.BOLD, use_color))
    w(SEP)

    # ── Balances ──────────────────────────────────────────────────────────────
    with ThreadPoolExecutor(max_workers=3) as ex:
        f_kb = ex.submit(fetch_kalshi_balance, private_key)
        f_pb = ex.submit(fetch_poly_usdc_balance, proxies)
        f_kp = ex.submit(fetch_kalshi_positions, private_key)
        k_bal = f_kb.result()
        p_bal = f_pb.result()
        k_venue = f_kp.result()

    kb_str = _c(f"${k_bal:.2f}", C.CYN, use_color) if k_bal is not None else "?"
    pb_str = (_c(f"${p_bal:.2f}", C.CYN, use_color) if p_bal is not None
              else _c("(set POLY_* creds to enable)", C.GRY, use_color))
    w(f"Balances:  Kalshi {kb_str}    Poly USDC {pb_str}")
    w(SEP)

    # ── Open positions ─────────────────────────────────────────────────────────
    w("")
    w(_c(f"OPEN POSITIONS ({len(open_positions)})", C.BOLD, use_color))

    if not open_positions:
        w("  (none)")
    else:
        for exec_id, ev in sorted(open_positions.items(), key=lambda x: x[1].get("t", "")):
            pair_id  = ev.get("pairId", "")
            arb_type = ev.get("arbType", "?")
            label    = ev.get("label", pair_id)
            pos      = ev.get("position", {})

            ticker   = _ticker_from_pair_id(pair_id)
            cfg      = pairs_by_ticker.get(ticker, {})
            settle   = cfg.get("settlement_date", "")

            k_entry  = pos.get("kEntryPrice", 0.0)
            p_entry  = pos.get("pAvgPrice",   0.0)
            net_cost = pos.get("actualNetPerSet", k_entry + p_entry)
            sets     = float(pos.get("kHeld", 1))

            if arb_type == "K_YES_P_NO":
                poly_token  = cfg.get("poly_no_token", "")
                k_ask_field = "yes_ask"
                k_bid_field = "yes_bid"
                k_side, p_side = "YES", "NO"
            else:
                poly_token  = cfg.get("poly_yes_token", "")
                k_ask_field = "no_ask"
                k_bid_field = "no_bid"
                k_side, p_side = "NO", "YES"

            # Live price fetch (sequential to avoid Kalshi rate-limit)
            kbook = fetch_kalshi_market(private_key, ticker) if ticker else {}
            time.sleep(0.15)
            pbook = fetch_poly_book(poly_token, proxies) if poly_token else {}

            k_ask_live = kbook.get(k_ask_field)
            k_bid_live = kbook.get(k_bid_field)
            p_ask_live = pbook.get("ask")
            p_bid_live = pbook.get("bid")

            live_net = None
            if k_ask_live is not None and p_ask_live is not None:
                live_net = k_ask_live + p_ask_live + kalshi_fee(k_ask_live) + poly_fee(p_ask_live)

            unrealized = None
            if k_bid_live is not None and p_bid_live is not None:
                entry_fees = kalshi_fee(k_entry)    + poly_fee(p_entry)
                exit_fees  = kalshi_fee(k_bid_live) + poly_fee(p_bid_live)
                unrealized = sets * ((k_bid_live + p_bid_live) - exit_fees - (k_entry + p_entry) - entry_fees)

            proj_profit = sets * (1.0 - net_cost)

            # ── Print position block ─────────────────────────────────────────
            w("")
            w(f"  {_c(ticker, C.BOLD, use_color)}  [{arb_type}]  "
              f"{_c(_age(ev.get('t', '')), C.GRY, use_color)}")
            w(f"  {label}")
            if settle:
                w(f"  Settles: {_days_to(settle)}")
            w("")

            proj_str = _c(f"+${proj_profit:.4f}", C.GRN, use_color)
            w(f"  Entry:     K={sets:.0f}×{k_side} @ ${k_entry:.4f}  +  "
              f"P={sets:.2f}×{p_side} @ ${p_entry:.4f}")
            w(f"  Net cost:  ${net_cost:.4f}/set  →  proj profit {proj_str}  "
              f"(${proj_profit:.4f} total at settlement)")
            w("")

            if k_ask_live is not None or p_ask_live is not None or k_bid_live is not None:
                ka = f"${k_ask_live:.4f}" if k_ask_live is not None else "  ?   "
                pa = f"${p_ask_live:.4f}" if p_ask_live is not None else "  ?   "
                kb = f"${k_bid_live:.4f}" if k_bid_live is not None else "  ?   "
                pb = f"${p_bid_live:.4f}" if p_bid_live is not None else "  ?   "
                w(f"  Live ask:  K={ka}  P={pa}")
                w(f"  Live bid:  K={kb}  P={pb}")

                if live_net is not None:
                    edge = 1.0 - live_net
                    if edge > 0:
                        arb_str = _c(f"arb still open  (+${edge:.4f}/set)", C.GRN, use_color)
                    else:
                        arb_str = _c(f"arb closed  (net ${live_net:.4f})", C.YLW, use_color)
                    w(f"  Live net:  ${live_net:.4f}  →  {arb_str}")

                if unrealized is not None:
                    color = _pnl_color(unrealized, use_color)
                    w(f"  Unrealized:{_c(f' ${unrealized:+.4f}', color, use_color)}"
                      f"  (mark-to-bid if closed now)")

                if kbook.get("error"):
                    w(f"  {_c('[Kalshi book error: ' + kbook['error'] + ']', C.RED, use_color)}")
                if pbook.get("error"):
                    w(f"  {_c('[Poly book error: ' + pbook['error'] + ']', C.RED, use_color)}")

            w("")

            # ── Venue verification ───────────────────────────────────────────
            venue_pos = k_venue.get(ticker)
            if venue_pos is not None:
                held_side = "YES" if venue_pos > 0 else "NO"
                expected  = int(round(sets))
                match     = abs(venue_pos) == expected
                chk = _c("✓ matches journal", C.GRN, use_color) if match \
                      else _c(f"⚠ MISMATCH — journal={expected}, venue={abs(venue_pos)}", C.RED, use_color)
                w(f"  Venue:     Kalshi holds {abs(venue_pos)} {held_side}  {chk}")
            else:
                w(f"  Venue:     {_c('Kalshi position not found for ' + ticker, C.YLW, use_color)}")

            w("")
            w("  " + SEP[2:])

    # ── Closed today ───────────────────────────────────────────────────────────
    w("")
    w(_c(f"CLOSED (last 24h)  ({len(closed_today)})", C.BOLD, use_color))
    if not closed_today:
        w("  (none)")
    else:
        for ev in sorted(closed_today, key=lambda e: e.get("t", ""), reverse=True):
            t_str  = ev.get("t", "")[:16].replace("T", " ")
            ticker = _ticker_from_pair_id(ev.get("pairId", "?"))
            pnl    = ev.get("realizedPnl")
            if pnl is not None:
                pnl_str = "  pnl=" + _c(f"${pnl:+.4f}", _pnl_color(pnl, use_color), use_color)
            else:
                pnl_str = ""
            w(f"  {t_str} UTC  {ticker}{pnl_str}")

    w("")
    w(SEP)
    return "\n".join(lines)

# ── Entry point ────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="KalshiPolyCross live status")
    ap.add_argument("--dir",      default=None,   help="Journal + cross_pairs.json directory")
    ap.add_argument("--watch",    type=int, default=0, metavar="SECS",
                    help="Auto-refresh every N seconds")
    ap.add_argument("--no-color", action="store_true", help="Disable ANSI colors")
    args = ap.parse_args()

    use_color = not args.no_color and sys.stdout.isatty()

    if not KALSHI_API_KEY or not KALSHI_KEY_PATH:
        print("ERROR: set KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH", file=sys.stderr)
        sys.exit(1)
    try:
        private_key = _load_key(KALSHI_KEY_PATH)
    except Exception as e:
        print(f"ERROR loading Kalshi key: {e}", file=sys.stderr)
        sys.exit(1)

    proxies = {"https": POLY_SOCKS, "http": POLY_SOCKS} if POLY_SOCKS else None

    search_dirs = list(filter(None, [
        args.dir,
        os.getcwd(),
        str(_ROOT / "KalshiPolyCross"),
        str(_ROOT),
    ]))

    journal_files = _find_journals(search_dirs)
    if not journal_files:
        print("WARNING: No CrossArbJournal_*.jsonl found — position data unavailable.", file=sys.stderr)

    pairs_file      = _find_pairs_file(search_dirs)
    pairs_by_ticker = load_pairs(pairs_file)

    def run_once():
        open_pos, closed = load_journal_state(journal_files)
        out = render(open_pos, closed, pairs_by_ticker, private_key, proxies, use_color)
        if args.watch and use_color:
            print("\033[2J\033[H", end="")
        print(out)

    if args.watch > 0:
        while True:
            try:
                run_once()
            except KeyboardInterrupt:
                break
            except Exception as e:
                print(f"\n[refresh error: {e}]", file=sys.stderr)
            time.sleep(args.watch)
    else:
        run_once()

if __name__ == "__main__":
    main()
