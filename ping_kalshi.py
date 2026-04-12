#!/usr/bin/env python3
"""
ping_kalshi.py — True latency benchmark for the Kalshi API servers.

Measures every layer that matters for arb trading:
  1. DNS resolution + resolved IP + CloudFront PoP detection
  2. Raw TCP connect time  (to CF PoP if behind CloudFront, or origin if not)
  3. TLS handshake overhead
  4. REST GET round-trip    (unauthenticated)
  5. REST orderbook fetch   (what the bot calls for depth verification)
  6. WebSocket connect + first message latency  (requires auth)
  7. ICMP ping comparison

⚠  CloudFront note:  api.elections.kalshi.com sits behind AWS CloudFront.
   From Iceland your TCP SYN hits the Dublin PoP (DUB56), not the Kalshi
   origin in the US.  REST looks fast because CF caches responses at the PoP.
   WebSocket is proxied all the way to origin, so WS latency is:
       TCP_to_PoP + PoP_to_origin + PoP_to_origin (return) + TCP_to_you
   Running this from a US server near the Kalshi DC gives you the real numbers.

Usage:
  python ping_kalshi.py
  python ping_kalshi.py --rounds 20
  python ping_kalshi.py --rounds 20 --ticker KXMLB-25APR12-T4.5
  python ping_kalshi.py --api-key-id YOUR_KEY_ID --private-key ./kalshi.pem
  python ping_kalshi.py --continuous          # loop forever, print stats every 30s

Auth for WebSocket (reads env vars or explicit args):
  export KALSHI_API_KEY_ID=your-key-id
  export KALSHI_PRIVATE_KEY_PATH=/path/to/key.pem
"""

import argparse
import base64
import datetime
import json
import os
import re
import socket
import ssl
import statistics
import subprocess
import sys
import threading
import time

import requests

try:
    import websocket as _websocket
    HAS_WS = True
except ImportError:
    HAS_WS = False

# ─── .env loader ──────────────────────────────────────────────────────────────

def _load_dotenv(*search_dirs):
    """
    Loads KEY=VALUE pairs from a .env file into os.environ (does not overwrite
    existing env vars).  Handles 'export KEY=VALUE' and bare 'KEY=VALUE' lines.
    Searches each directory in search_dirs, stops at first .env found.
    """
    for d in search_dirs:
        path = os.path.join(d, ".env")
        if not os.path.isfile(path):
            continue
        with open(path) as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("export "):
                    line = line[7:].strip()
                if "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key = key.strip()
                val = val.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = val
        return  # stop at first .env found

# Load .env from script dir then CWD (CWD takes precedence via "not in os.environ" check)
_load_dotenv(os.path.dirname(os.path.abspath(__file__)), os.getcwd())

try:
    from cryptography.hazmat.primitives import serialization, hashes
    from cryptography.hazmat.backends import default_backend
    from cryptography.hazmat.primitives.asymmetric import padding as _padding
    HAS_CRYPTO = True
except ImportError:
    HAS_CRYPTO = False

# ─── Config ───────────────────────────────────────────────────────────────────

HOST    = "api.elections.kalshi.com"
PORT    = 443
BASE    = f"https://{HOST}/trade-api/v2"
WS_URL  = f"wss://{HOST}/trade-api/ws/v2"
WS_PATH = "/trade-api/ws/v2"

ENDPOINTS = {
    "exchange/status":  f"{BASE}/exchange/status",
    "markets?limit=1":  f"{BASE}/markets?limit=1",
}

# Known CloudFront PoP city codes (first 3 chars of the pop code)
_CF_POP_CITIES = {
    "DUB": "Dublin, IE",      "LHR": "London, UK",
    "AMS": "Amsterdam, NL",   "CDG": "Paris, FR",
    "FRA": "Frankfurt, DE",   "ARN": "Stockholm, SE",
    "IAD": "Ashburn, VA, US", "JFK": "New York, NY, US",
    "ORD": "Chicago, IL, US", "DFW": "Dallas, TX, US",
    "LAX": "Los Angeles, US", "SEA": "Seattle, WA, US",
    "ATL": "Atlanta, GA, US", "BOS": "Boston, MA, US",
    "MIA": "Miami, FL, US",   "EWR": "Newark, NJ, US",
}

# ─── Auth helpers ─────────────────────────────────────────────────────────────

def load_kalshi_key(path):
    with open(path, "rb") as f:
        return serialization.load_pem_private_key(
            f.read(), password=None, backend=default_backend())

def _kalshi_sign(private_key, method, path):
    """Returns (timestamp_str, b64_signature)."""
    ts  = str(int(datetime.datetime.now().timestamp() * 1000))
    msg = f"{ts}{method}{path}".encode("utf-8")
    sig = private_key.sign(
        msg,
        _padding.PSS(
            mgf=_padding.MGF1(hashes.SHA256()),
            salt_length=_padding.PSS.DIGEST_LENGTH,
        ),
        hashes.SHA256(),
    )
    return ts, base64.b64encode(sig).decode("utf-8")

def make_ws_auth_headers(api_key_id, private_key):
    """Headers for the WebSocket HTTP upgrade request."""
    ts, sig = _kalshi_sign(private_key, "GET", WS_PATH)
    return {
        "KALSHI-ACCESS-KEY":       api_key_id,
        "KALSHI-ACCESS-SIGNATURE": sig,
        "KALSHI-ACCESS-TIMESTAMP": ts,
    }

# ─── CloudFront detection ─────────────────────────────────────────────────────

def detect_cloudfront(host):
    """
    Makes a HEAD request and checks for CloudFront response headers.
    Returns (is_cf: bool, pop_code: str, city: str).
    """
    try:
        r = requests.head(f"https://{host}/trade-api/v2/exchange/status",
                          timeout=8, allow_redirects=True)
        pop  = r.headers.get("x-amz-cf-pop", "")
        via  = r.headers.get("via", "")
        is_cf = bool(pop or "cloudfront" in via.lower())
        city  = _CF_POP_CITIES.get(pop[:3].upper(), "unknown location") if pop else ""
        return is_cf, pop, city
    except Exception:
        return False, "", ""

# ─── Low-level timers ─────────────────────────────────────────────────────────

def resolve_dns(host):
    t0 = time.perf_counter()
    ip = socket.gethostbyname(host)
    return ip, (time.perf_counter() - t0) * 1000

def tcp_connect_ms(host, port):
    """TCP SYN → SYN-ACK RTT in ms.  One-way ≈ this / 2."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(10)
    try:
        t0 = time.perf_counter()
        s.connect((host, port))
        return (time.perf_counter() - t0) * 1000
    finally:
        s.close()

def tls_overhead_ms(host, port):
    """TLS handshake time AFTER TCP already connected."""
    ctx = ssl.create_default_context()
    raw = socket.create_connection((host, port), timeout=10)
    try:
        t0  = time.perf_counter()
        tls = ctx.wrap_socket(raw, server_hostname=host)
        ms  = (time.perf_counter() - t0) * 1000
        tls.close()
        return ms
    except Exception:
        raw.close()
        raise

def http_get_ms(url, session=None):
    s = session or requests.Session()
    if session is None:
        s.headers.update({"Connection": "close"})
    t0 = time.perf_counter()
    r  = s.get(url, timeout=10)
    return (time.perf_counter() - t0) * 1000, r.status_code

def ws_latency_ms(ws_url, ticker=None, api_key_id=None, private_key=None):
    """
    Measures WS handshake time and time-to-first-message.

    handshake_ms — TCP + TLS + HTTP Upgrade (auth headers sent here)
    first_msg_ms — additional ms until first message arrives after subscribe

    Requires websocket-client.  Pass api_key_id + private_key for auth.
    """
    result, error = {}, {}

    subscribe_msg = json.dumps({
        "id": 1,
        "cmd": "subscribe",
        "params": {
            "channels": ["orderbook_delta"],
            "market_tickers": [ticker or "KALSHI-PING-DUMMY"],
        },
    })

    header = []
    if api_key_id and private_key:
        h = make_ws_auth_headers(api_key_id, private_key)
        header = [f"{k}: {v}" for k, v in h.items()]

    def on_open(ws):
        result["open_at"] = time.perf_counter()
        ws.send(subscribe_msg)

    def on_message(ws, msg):
        if "first_msg_at" not in result:
            result["first_msg_at"] = time.perf_counter()
        ws.close()

    def on_error(ws, err):
        error["err"] = str(err)

    def on_close(ws, code, msg):
        pass

    t_start = time.perf_counter()
    ws_app  = _websocket.WebSocketApp(
        ws_url,
        header=header,
        on_open=on_open,
        on_message=on_message,
        on_error=on_error,
        on_close=on_close,
    )
    t = threading.Thread(target=ws_app.run_forever, kwargs={"ping_interval": 0})
    t.daemon = True
    t.start()
    t.join(timeout=15)

    if "open_at" not in result:
        raise RuntimeError(f"WS never opened. error={error.get('err', 'timeout')}")

    hs_ms = (result["open_at"] - t_start) * 1000
    fm_ms = (
        (result["first_msg_at"] - t_start) * 1000
        if "first_msg_at" in result else None
    )
    return hs_ms, fm_ms

def icmp_ping(host, count=5):
    is_win = sys.platform.startswith("win")
    cmd    = ["ping", f"-{'n' if is_win else 'c'}", str(count), host]
    try:
        out = subprocess.check_output(
            cmd, stderr=subprocess.STDOUT, timeout=count * 3 + 5
        ).decode(errors="replace")
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        return None
    m = re.search(r"(\d+\.?\d*)/(\d+\.?\d*)/(\d+\.?\d*)/\d+\.?\d* ms", out)
    if m:
        return float(m.group(1)), float(m.group(2)), float(m.group(3))
    m = re.search(
        r"Minimum\s*=\s*(\d+)ms.*?Maximum\s*=\s*(\d+)ms.*?Average\s*=\s*(\d+)ms",
        out, re.I | re.S,
    )
    if m:
        return float(m.group(1)), float(m.group(3)), float(m.group(2))
    return None

# ─── Helpers ──────────────────────────────────────────────────────────────────

def _stats(times):
    if not times:
        return None
    s       = sorted(times)
    n       = len(s)
    p90_idx = min(int(n * 0.90), n - 1)
    return (min(s), statistics.median(s), s[p90_idx],
            max(s), statistics.stdev(s) if n > 1 else 0.0)

def _sep(w=68):
    return "─" * w

# ─── Measurement sections ─────────────────────────────────────────────────────

def section_header(host):
    print(f"\n{'═'*68}")
    print(f"  KALSHI LATENCY BENCHMARK  →  {host}")
    print(f"{'═'*68}")
    try:
        ip, dns_ms = resolve_dns(host)
        print(f"  Resolved  {host}  →  {ip}  ({dns_ms:.1f}ms DNS)")
    except Exception as e:
        print(f"  [DNS FAILED: {e}]")

    is_cf, pop, city = detect_cloudfront(host)
    if is_cf:
        print(f"\n  ⚠  CLOUDFRONT DETECTED  (PoP: {pop} = {city})")
        print(f"     TCP RTT below measures YOUR LOCATION → CF PoP, not → Kalshi origin.")
        print(f"     REST looks fast because CF caches it at the PoP.")
        print(f"     WebSocket is proxied through CF all the way to the Kalshi origin.")
        print(f"     See INTERPRETATION section for adjusted estimates.")
    else:
        print(f"  No CloudFront detected — TCP RTT is measuring directly to Kalshi origin.")

    return is_cf, pop, city

def section_tcp(host, port, rounds):
    print(f"\n  [1] TCP Connect  ({rounds} rounds, port {port})")
    print(f"  {_sep()}")
    times = []
    for i in range(rounds):
        try:
            t = tcp_connect_ms(host, port)
            times.append(t)
            print(f"      Round {i+1:>2}: {t:6.2f}ms")
        except Exception as e:
            print(f"      Round {i+1:>2}: FAILED ({e})")
    return times

def section_tls(host, port, rounds):
    print(f"\n  [2] TLS Handshake overhead  ({rounds} rounds)")
    print(f"  {_sep()}")
    times = []
    for i in range(rounds):
        try:
            t = tls_overhead_ms(host, port)
            times.append(t)
            print(f"      Round {i+1:>2}: {t:6.2f}ms")
        except Exception as e:
            print(f"      Round {i+1:>2}: FAILED ({e})")
    return times

def section_rest(rounds):
    all_times = {}
    for name, url in ENDPOINTS.items():
        print(f"\n  [3] REST GET /{name}  ({rounds} rounds, new conn each)")
        print(f"  {_sep()}")
        times = []
        for i in range(rounds):
            try:
                t, code = http_get_ms(url)
                times.append(t)
                print(f"      Round {i+1:>2}: {t:6.1f}ms  (HTTP {code})")
            except Exception as e:
                print(f"      Round {i+1:>2}: FAILED ({e})")
        all_times[name] = times
    return all_times

def section_rest_warm(rounds):
    print(f"\n  [4] REST keep-alive (warm connection, {rounds} rounds)")
    print(f"  {_sep()}")
    url  = ENDPOINTS["exchange/status"]
    sess = requests.Session()
    sess.headers.update({"Connection": "keep-alive"})
    times = []
    for i in range(rounds):
        try:
            t, code = http_get_ms(url, session=sess)
            times.append(t)
            print(f"      Round {i+1:>2}: {t:6.1f}ms  (HTTP {code})")
        except Exception as e:
            print(f"      Round {i+1:>2}: FAILED ({e})")
    sess.close()
    return times

def section_orderbook(ticker, rounds):
    print(f"\n  [5] REST orderbook  /markets/{ticker}/orderbook  ({rounds} rounds, warm)")
    print(f"  {_sep()}")
    url  = f"{BASE}/markets/{ticker}/orderbook"
    sess = requests.Session()
    sess.headers.update({"Connection": "keep-alive"})
    times = []
    for i in range(rounds):
        try:
            t, code = http_get_ms(url, session=sess)
            times.append(t)
            print(f"      Round {i+1:>2}: {t:6.1f}ms  (HTTP {code})")
        except Exception as e:
            print(f"      Round {i+1:>2}: FAILED ({e})")
    sess.close()
    return times

def section_ws(ws_url, ticker, rounds, api_key_id=None, private_key=None):
    if not HAS_WS:
        print("\n  [6] WebSocket  — SKIPPED (pip install websocket-client)")
        return [], []

    has_auth = bool(api_key_id and private_key)
    auth_note = "authenticated" if has_auth else "unauthenticated — will 401, but handshake timing still valid"
    print(f"\n  [6] WebSocket connect + first message  ({rounds} rounds, {auth_note})")
    print(f"  {_sep()}")

    if not has_auth:
        print(f"      NOTE: No auth provided — WS will be rejected with 401 after TCP+TLS.")
        print(f"      Pass --api-key-id and --private-key to measure first-message latency.")

    handshakes, first_msgs = [], []
    for i in range(rounds):
        try:
            hs, fm = ws_latency_ms(ws_url, ticker,
                                   api_key_id=api_key_id,
                                   private_key=private_key)
            handshakes.append(hs)
            line = f"handshake={hs:.1f}ms"
            if fm is not None:
                first_msgs.append(fm)
                line += f"  first_msg={fm:.1f}ms"
            else:
                line += "  first_msg=n/a"
            print(f"      Round {i+1:>2}: {line}")
        except Exception as e:
            # Extract just the key part of a 401 error to keep output clean
            err_str = str(e)
            if "401" in err_str:
                print(f"      Round {i+1:>2}: 401 Unauthorized (need --api-key-id / --private-key)")
            else:
                print(f"      Round {i+1:>2}: FAILED ({err_str[:120]})")
    return handshakes, first_msgs

def section_icmp(host):
    print(f"\n  [7] ICMP Ping (system ping, 5 packets)")
    print(f"  {_sep()}")
    result = icmp_ping(host, count=5)
    if result:
        mn, avg, mx = result
        print(f"      min={mn:.1f}ms  avg={avg:.1f}ms  max={mx:.1f}ms")
    else:
        print("      n/a (ICMP blocked or ping not available)")
    return result

# ─── Summary + Interpretation ─────────────────────────────────────────────────

def print_summary(tcp_times, tls_times, rest_times, warm_times,
                  ob_times, ws_hs, ws_fm, icmp_result, is_cf, pop_code):

    print(f"\n{'═'*68}")
    print("  SUMMARY")
    print(f"{'═'*68}")
    print(f"  {'Measurement':<32} {'Min':>8} {'Median':>8} {'p90':>8} {'Max':>8} {'Jitter':>8}")
    print(f"  {_sep()}")

    def row(label, times):
        if not times:
            print(f"  {label:<32} {'n/a':>8}")
            return
        mn, med, p90, mx, jitter = _stats(times)
        print(f"  {label:<32} {mn:>7.1f}ms {med:>7.1f}ms {p90:>7.1f}ms {mx:>7.1f}ms {jitter:>7.1f}ms")

    row("TCP connect (RTT)",      tcp_times)
    row("TLS handshake overhead", tls_times)
    for name, times in rest_times.items():
        row(f"REST GET /{name} (cold)",  times)
    row("REST GET (warm conn)",   warm_times)
    if ob_times:
        row("REST orderbook (warm)", ob_times)
    row("WS handshake",          ws_hs)
    row("WS first message",      ws_fm)
    if icmp_result:
        mn, avg, mx = icmp_result
        print(f"  {'ICMP ping':<32} {mn:>7.1f}ms {avg:>7.1f}ms {'':>8} {mx:>7.1f}ms")

    # ── Interpretation ──────────────────────────────────────────────────────
    print(f"\n{'═'*68}")
    print("  INTERPRETATION FOR ARB TRADING")
    print(f"{'═'*68}")

    tcp_s  = _stats(tcp_times)
    rest_s = _stats(warm_times) if warm_times else _stats(
        list(rest_times.values())[0] if rest_times else [])
    ws_s   = _stats(ws_fm)  if ws_fm  else None
    ob_s   = _stats(ob_times) if ob_times else None

    if not tcp_s:
        print("\n  [no TCP data to interpret]")
        print(f"\n{'═'*68}\n")
        return

    tcp_rtt = tcp_s[0]   # minimum observed RTT

    if is_cf:
        # Estimate PoP→origin latency from the pop code geography
        pop3 = pop_code[:3].upper()
        if pop3 in ("IAD", "EWR", "JFK", "BOS"):
            pop_to_origin_ms = 5    # US east PoP — basically co-located
        elif pop3 in ("ORD", "ATL", "MIA", "DFW"):
            pop_to_origin_ms = 20   # US mid/south
        elif pop3 in ("LAX", "SEA"):
            pop_to_origin_ms = 70   # US west
        elif pop3 in ("LHR", "DUB", "AMS", "CDG", "FRA", "ARN"):
            pop_to_origin_ms = 75   # Europe → NYC
        else:
            pop_to_origin_ms = 80   # Unknown — assume far

        your_to_pop    = tcp_rtt / 2                           # one-way
        ws_one_way     = your_to_pop + pop_to_origin_ms        # your → pop → origin
        ws_rtt_total   = ws_one_way * 2                        # round trip via pop

        print(f"\n  ⚠  Results are through CloudFront PoP: {pop_code}")
        print(f"\n  What the numbers actually measure:")
        print(f"    TCP RTT {tcp_rtt:.1f}ms  =  your location → CF PoP → back")
        print(f"    REST warm {rest_s[1]:.1f}ms (median)  =  CF PoP serving cached response (not origin)")
        print(f"\n  Estimated true latencies:")
        print(f"    You → CF PoP (one-way):        {your_to_pop:.1f}ms")
        print(f"    CF PoP → Kalshi origin (est):  ~{pop_to_origin_ms}ms  (PoP={pop_code})")
        print(f"    You → Kalshi origin (one-way): ~{ws_one_way:.1f}ms")
        print(f"\n  Arb execution timeline (your actual cost):")
        print(f"    Book change at Kalshi  →  WS msg arrives at you:  ~{ws_one_way:.1f}ms")
        print(f"    You detect arb + logic:                             ~0.1ms")
        print(f"    REST order departs → arrives at Kalshi:            ~{ws_one_way:.1f}ms")
        print(f"    ─────────────────────────────────────────────────────────")
        print(f"    Total from book change to order at Kalshi:         ~{ws_one_way*2:.1f}ms")
        # Estimate Kalshi's own processing time from cold REST (origin-only requests)
        cold_vals = list(rest_times.values())[0] if rest_times else []
        cold_s    = _stats(cold_vals)
        # cold REST = network_rtt + kalshi_processing → isolate processing
        kalshi_proc_ms = (cold_s[1] - tcp_rtt - 2) if cold_s else 75
        kalshi_proc_ms = max(kalshi_proc_ms, 0)
        confirmed_rtt  = ws_rtt_total + kalshi_proc_ms   # network + server processing

        print(f"\n  Order placement breakdown:")
        print(f"    Order departs → arrives at Kalshi matching engine:  ~{ws_one_way:.1f}ms  (network)")
        print(f"    Kalshi processes + writes to DB:                     ~{kalshi_proc_ms:.0f}ms  (measured via cold REST)")
        print(f"    Confirmation arrives back:                           ~{ws_one_way:.1f}ms  (network)")
        print(f"    ─────────────────────────────────────────────────────────────────")
        print(f"    Full order round-trip (send → confirmed):            ~{confirmed_rtt:.0f}ms")
        print(f"    ⚠  The ~{kalshi_proc_ms:.0f}ms Kalshi processing is a FIXED cost — cannot be reduced with better hardware.")

        if ws_s:
            print(f"\n  Measured WS first-message latency: {ws_s[1]:.1f}ms median")

        if ob_s:
            cold_ob = ob_s[1] + kalshi_proc_ms if ob_s else 0
            print(f"\n  Orderbook REST fetch (warm, cached): {ob_s[1]:.1f}ms  |  uncached (origin): ~{cold_ob:.0f}ms")

        # ── Location-aware comparison ──────────────────────────────────────
        capturable_here = ws_one_way * 2 + 5
        is_us_east = pop3 in ("IAD", "EWR", "JFK", "BOS", "ATL", "MIA", "ORD", "DFW")

        if is_us_east:
            print(f"\n  ✓ You are already at an optimal US-East server near Kalshi origin.")
            print(f"    Network is only ~{tcp_rtt:.0f}ms RTT — you cannot improve further with colocation.")
            print(f"\n  Improvement vs Iceland (~62ms one-way):")
            print(f"    WS receive (book updates):   ~{62/ws_one_way:.0f}x faster  ({62:.0f}ms → {ws_one_way:.0f}ms one-way)")
            print(f"    Order confirmation:           ~{(62*2+kalshi_proc_ms)/(confirmed_rtt):.1f}x faster  ({int(62*2+kalshi_proc_ms)}ms → {confirmed_rtt:.0f}ms)")
        else:
            print(f"\n  ── US server comparison ──────────────────────────────────────────────")
            print(f"  Your current one-way to Kalshi:        ~{ws_one_way:.1f}ms")
            print(f"  US-East server (JFK PoP, near origin):   ~6ms")
            print(f"  Improvement:                               ~{ws_one_way/6:.0f}x faster")
            print(f"\n  Iceland baseline (~62ms one-way, 124ms window):  --min-duration 130")

        print(f"\n  For analyze_kalshi_arb.py  (this server):")
        print(f"    --min-duration {int(capturable_here)}   (windows >= {int(capturable_here)}ms are capturable from here)")

    else:
        # Direct connection — straightforward interpretation
        one_way = tcp_rtt / 2
        print(f"\n  TCP min RTT: {tcp_rtt:.1f}ms")
        print(f"  Est. one-way latency: {one_way:.1f}ms  (TCP min / 2)")
        print(f"\n  Arb execution timeline (best case):")
        print(f"    Book change at Kalshi  →  WS msg arrives at you:  ~{one_way:.1f}ms")
        print(f"    You detect arb + logic:                             ~0.1ms")
        print(f"    REST order departs → arrives at Kalshi:            ~{one_way:.1f}ms")
        print(f"    ─────────────────────────────────────────────────────────")
        print(f"    Total from book change to order at Kalshi:         ~{one_way*2:.1f}ms")
        if rest_s:
            print(f"\n  REST warm RTT (order confirmation cost): {rest_s[1]:.1f}ms median")
        if ob_s:
            print(f"  Orderbook REST fetch:                    {ob_s[1]:.1f}ms median")
        capturable = one_way * 2 + 5
        print(f"\n  For analyze_kalshi_arb.py:")
        print(f"    --min-duration {int(capturable)}   (currently defaults to 500ms)")

    print(f"\n{'═'*68}\n")

# ─── Continuous mode ──────────────────────────────────────────────────────────

def run_continuous(rounds, ticker, api_key_id=None, private_key=None):
    print("Running continuous latency monitor (Ctrl+C to stop)...\n")
    print(f"{'Time':<10} {'TCP min':>8} {'TCP avg':>8} {'REST med':>8} {'WS hs':>8} {'WS msg':>8}")
    print("─" * 58)

    while True:
        now  = time.strftime("%H:%M:%S")
        tcp_t, rest_t, ws_h, ws_m = [], [], [], []

        for _ in range(rounds):
            try:
                tcp_t.append(tcp_connect_ms(HOST, PORT))
            except Exception:
                pass

        sess = requests.Session()
        sess.headers.update({"Connection": "keep-alive"})
        for _ in range(rounds):
            try:
                t, _ = http_get_ms(ENDPOINTS["exchange/status"], session=sess)
                rest_t.append(t)
            except Exception:
                pass
        sess.close()

        if HAS_WS:
            try:
                hs, fm = ws_latency_ms(WS_URL, ticker,
                                       api_key_id=api_key_id,
                                       private_key=private_key)
                ws_h.append(hs)
                if fm is not None:
                    ws_m.append(fm)
            except Exception:
                pass

        def _f(lst, fn):
            return f"{fn(lst):.1f}ms" if lst else "n/a"

        print(f"{now:<10}"
              f" {_f(tcp_t, min):>8}"
              f" {_f(tcp_t, statistics.mean):>8}"
              f" {_f(rest_t, statistics.median):>8}"
              f" {_f(ws_h, statistics.mean):>8}"
              f" {_f(ws_m, statistics.mean):>8}")
        try:
            time.sleep(30)
        except KeyboardInterrupt:
            print("\nStopped.")
            break

# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Measure true latency to the Kalshi trading API",
    )
    parser.add_argument("--rounds",      type=int, default=10)
    parser.add_argument("--ticker",      default=None,
                        help="Market ticker for orderbook test, e.g. KXMLB-25APR12-T4.5")
    parser.add_argument("--api-key-id",  default=os.environ.get("KALSHI_API_KEY_ID", ""),
                        help="Kalshi API key ID (or set KALSHI_API_KEY_ID env var)")
    parser.add_argument("--private-key", default=os.environ.get("KALSHI_PRIVATE_KEY_PATH", ""),
                        help="Path to RSA private key PEM (or set KALSHI_PRIVATE_KEY_PATH)")
    parser.add_argument("--continuous",  action="store_true")
    parser.add_argument("--no-ws",       action="store_true")
    parser.add_argument("--no-icmp",     action="store_true")
    args = parser.parse_args()

    # Load private key if provided
    private_key = None
    if args.private_key:
        if not HAS_CRYPTO:
            print("ERROR: cryptography package not installed. pip install cryptography")
            sys.exit(1)
        try:
            private_key = load_kalshi_key(args.private_key)
        except Exception as e:
            print(f"ERROR loading private key: {e}")
            sys.exit(1)

    api_key_id = args.api_key_id or None

    if args.continuous:
        run_continuous(args.rounds, args.ticker,
                       api_key_id=api_key_id, private_key=private_key)
        return

    # ── One-shot benchmark ──────────────────────────────────────────────────
    is_cf, pop_code, pop_city = section_header(HOST)

    tcp_times  = section_tcp(HOST, PORT, args.rounds)
    tls_times  = section_tls(HOST, PORT, args.rounds)
    rest_times = section_rest(args.rounds)
    warm_times = section_rest_warm(args.rounds)

    ob_times = []
    if args.ticker:
        ob_times = section_orderbook(args.ticker, args.rounds)

    ws_hs, ws_fm = [], []
    if not args.no_ws:
        ws_hs, ws_fm = section_ws(
            WS_URL, args.ticker,
            rounds=min(args.rounds, 5),
            api_key_id=api_key_id,
            private_key=private_key,
        )

    icmp_result = None
    if not args.no_icmp:
        icmp_result = section_icmp(HOST)

    print_summary(tcp_times, tls_times, rest_times, warm_times,
                  ob_times, ws_hs, ws_fm, icmp_result, is_cf, pop_code)


if __name__ == "__main__":
    main()
