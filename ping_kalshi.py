#!/usr/bin/env python3
"""
ping_kalshi.py — True latency benchmark for the Kalshi API servers.

Measures every layer that matters for arb trading:
  1. DNS resolution + resolved IP
  2. Raw TCP connect time  (≈ network RTT — the floor for everything else)
  3. TLS handshake overhead (extra cost on top of TCP for HTTPS/WSS)
  4. REST GET round-trip    (unauthenticated — lightweight endpoint)
  5. REST orderbook fetch   (what the bot calls for depth verification)
  6. WebSocket connect + first message latency
  7. ICMP ping comparison   (system ping for reference)

Interpretation printed at the end:
  TCP RTT / 2  ≈  one-way network latency
  REST RTT     ≈  order submission round-trip (send → ack)
  WS first msg ≈  how fast a book update reaches you after it happens at Kalshi

Usage:
  python ping_kalshi.py
  python ping_kalshi.py --rounds 20
  python ping_kalshi.py --rounds 20 --ticker KXMLB-25APR12-T4.5
  python ping_kalshi.py --continuous          # loop forever, print stats every 60s
"""

import argparse
import json
import os
import socket
import ssl
import statistics
import subprocess
import sys
import threading
import time
from urllib.parse import urlparse

import requests

try:
    import websocket as _websocket
    HAS_WS = True
except ImportError:
    HAS_WS = False

# ─── Config ───────────────────────────────────────────────────────────────────

HOST    = "api.elections.kalshi.com"
PORT    = 443
BASE    = f"https://{HOST}/trade-api/v2"
WS_URL  = f"wss://{HOST}/trade-api/ws/v2"

# Lightweight unauthenticated endpoints
ENDPOINTS = {
    "exchange/status":  f"{BASE}/exchange/status",
    "markets?limit=1":  f"{BASE}/markets?limit=1",
}

# ─── Low-level timers ─────────────────────────────────────────────────────────

def resolve_dns(host):
    """Returns (ip_str, elapsed_ms). Raises on failure."""
    t0 = time.perf_counter()
    ip = socket.gethostbyname(host)
    return ip, (time.perf_counter() - t0) * 1000


def tcp_connect_ms(host, port):
    """
    TCP SYN → SYN-ACK round trip in ms.
    This is the purest measure of network latency.
    One-way latency ≈ this value / 2.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(10)
    try:
        t0 = time.perf_counter()
        s.connect((host, port))
        elapsed = (time.perf_counter() - t0) * 1000
    finally:
        s.close()
    return elapsed


def tls_overhead_ms(host, port):
    """
    Time spent on TLS handshake AFTER TCP is already connected.
    Represents the extra cost you pay the first time you open an HTTPS/WSS conn.
    (Persistent connections amortise this over many requests.)
    """
    ctx = ssl.create_default_context()
    raw = socket.create_connection((host, port), timeout=10)
    try:
        t0 = time.perf_counter()
        tls = ctx.wrap_socket(raw, server_hostname=host)
        elapsed = (time.perf_counter() - t0) * 1000
        tls.close()
    except Exception:
        raw.close()
        raise
    return elapsed


def http_get_ms(url, session=None):
    """
    Full HTTP GET including DNS + TCP + TLS + request (new connection each time).
    Pass a Session with Connection:keep-alive to measure warm-connection cost.
    Returns (elapsed_ms, status_code).
    """
    s = session or requests.Session()
    if session is None:
        s.headers.update({"Connection": "close"})
    t0 = time.perf_counter()
    r = s.get(url, timeout=10)
    return (time.perf_counter() - t0) * 1000, r.status_code


def ws_latency_ms(ws_url, ticker=None):
    """
    Measures two things:
      handshake_ms — TCP + TLS + HTTP Upgrade to WS
      first_msg_ms — additional time until first message arrives after subscribe

    Subscribes to 'orderbook_delta' for the given ticker (or a dummy if None).
    An error response from Kalshi still counts — it proves the round-trip worked.

    Returns (handshake_ms, first_msg_ms) or raises on connection failure.
    Requires: pip install websocket-client
    """
    result = {}
    error  = {}

    subscribe_msg = json.dumps({
        "id": 1,
        "cmd": "subscribe",
        "params": {
            "channels": ["orderbook_delta"],
            "market_tickers": [ticker or "KALSHI-PING-TEST"],
        },
    })

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
        on_open=on_open,
        on_message=on_message,
        on_error=on_error,
        on_close=on_close,
    )
    # run_forever blocks; use a thread so we can enforce a timeout
    t = threading.Thread(target=ws_app.run_forever, kwargs={"ping_interval": 0})
    t.daemon = True
    t.start()
    t.join(timeout=15)

    if "open_at" not in result:
        raise RuntimeError(f"WS never opened. error={error.get('err','timeout')}")

    handshake_ms  = (result["open_at"] - t_start) * 1000
    first_msg_ms  = (
        (result["first_msg_at"] - t_start) * 1000
        if "first_msg_at" in result else None
    )
    return handshake_ms, first_msg_ms


def icmp_ping(host, count=5):
    """
    Runs the system ping command and parses min/avg/max from its output.
    Works on Linux (ping -c N) and Windows (ping -n N).
    Returns (min_ms, avg_ms, max_ms) or None on failure.
    """
    is_windows = sys.platform.startswith("win")
    cmd = ["ping", f"-{'n' if is_windows else 'c'}", str(count), host]
    try:
        out = subprocess.check_output(cmd, stderr=subprocess.STDOUT,
                                      timeout=count * 3 + 5).decode(errors="replace")
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        return None

    # Linux: "rtt min/avg/max/mdev = 1.234/2.345/3.456/0.567 ms"
    # Windows: "Minimum = 1ms, Maximum = 3ms, Average = 2ms"
    import re
    m = re.search(r"(\d+\.?\d*)/(\d+\.?\d*)/(\d+\.?\d*)/\d+\.?\d* ms", out)
    if m:
        return float(m.group(1)), float(m.group(2)), float(m.group(3))
    m = re.search(r"Minimum\s*=\s*(\d+)ms.*?Maximum\s*=\s*(\d+)ms.*?Average\s*=\s*(\d+)ms", out, re.I)
    if m:
        return float(m.group(1)), float(m.group(3)), float(m.group(2))
    return None


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _stats(times):
    """Returns (min, median, p90, max, stdev) in ms."""
    if not times:
        return None
    s = sorted(times)
    n = len(s)
    p90_idx = min(int(n * 0.90), n - 1)
    return (
        min(s),
        statistics.median(s),
        s[p90_idx],
        max(s),
        statistics.stdev(s) if n > 1 else 0.0,
    )


def _fmt(val, unit="ms"):
    if val is None:
        return "  n/a  "
    return f"{val:6.1f}{unit}"


def _sep(width=68):
    return "─" * width


# ─── Measurement sections ─────────────────────────────────────────────────────

def section_dns_ip(host):
    print(f"\n{'═'*68}")
    print(f"  KALSHI LATENCY BENCHMARK  →  {host}")
    print(f"{'═'*68}")
    try:
        ip, dns_ms = resolve_dns(host)
        print(f"  Resolved  {host}  →  {ip}  ({dns_ms:.1f}ms DNS)")
    except Exception as e:
        ip = None
        print(f"  [DNS FAILED: {e}]")
    return ip


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
    """Measures REST latency on a persistent keep-alive connection."""
    print(f"\n  [4] REST keep-alive (warm connection, {rounds} rounds)")
    print(f"  {_sep()}")
    url = ENDPOINTS["exchange/status"]
    session = requests.Session()
    session.headers.update({"Connection": "keep-alive"})
    times = []
    for i in range(rounds):
        try:
            t, code = http_get_ms(url, session=session)
            times.append(t)
            print(f"      Round {i+1:>2}: {t:6.1f}ms  (HTTP {code})")
        except Exception as e:
            print(f"      Round {i+1:>2}: FAILED ({e})")
    session.close()
    return times


def section_orderbook(ticker, rounds):
    print(f"\n  [5] REST orderbook fetch  /markets/{ticker}/orderbook  ({rounds} rounds)")
    print(f"  {_sep()}")
    url = f"{BASE}/markets/{ticker}/orderbook"
    session = requests.Session()
    session.headers.update({"Connection": "keep-alive"})
    times = []
    for i in range(rounds):
        try:
            t, code = http_get_ms(url, session=session)
            times.append(t)
            print(f"      Round {i+1:>2}: {t:6.1f}ms  (HTTP {code})")
        except Exception as e:
            print(f"      Round {i+1:>2}: FAILED ({e})")
    session.close()
    return times


def section_ws(ws_url, ticker, rounds):
    if not HAS_WS:
        print("\n  [6] WebSocket  — SKIPPED (pip install websocket-client)")
        return [], []
    print(f"\n  [6] WebSocket connect + first message  ({rounds} rounds)")
    print(f"  {_sep()}")
    handshakes, first_msgs = [], []
    for i in range(rounds):
        try:
            hs, fm = ws_latency_ms(ws_url, ticker)
            handshakes.append(hs)
            status = f"handshake={hs:.1f}ms"
            if fm is not None:
                first_msgs.append(fm)
                status += f"  first_msg={fm:.1f}ms"
            else:
                status += "  first_msg=n/a (no response)"
            print(f"      Round {i+1:>2}: {status}")
        except Exception as e:
            print(f"      Round {i+1:>2}: FAILED ({e})")
    return handshakes, first_msgs


def section_icmp(host):
    print(f"\n  [7] ICMP Ping (system ping, 5 packets)")
    print(f"  {_sep()}")
    result = icmp_ping(host, count=5)
    if result:
        mn, avg, mx = result
        print(f"      min={mn:.1f}ms  avg={avg:.1f}ms  max={mx:.1f}ms")
    else:
        print("      n/a (ICMP blocked or ping unavailable)")
    return result


# ─── Summary ──────────────────────────────────────────────────────────────────

def print_summary(tcp_times, tls_times, rest_times, warm_times,
                  ob_times, ws_hs, ws_fm, icmp_result, ticker):

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

    row("TCP connect (RTT)",          tcp_times)
    row("TLS handshake overhead",     tls_times)
    for name, times in rest_times.items():
        row(f"REST GET /{name} (cold)", times)
    row("REST GET (warm conn)",        warm_times)
    if ob_times:
        row(f"REST orderbook (warm)",  ob_times)
    row("WS handshake",               ws_hs)
    row("WS first message",           ws_fm)

    if icmp_result:
        mn, avg, mx = icmp_result
        print(f"  {'ICMP ping':<32} {mn:>7.1f}ms {avg:>7.1f}ms {'':>8} {mx:>7.1f}ms")

    print(f"\n{'═'*68}")
    print("  INTERPRETATION FOR ARB TRADING")
    print(f"{'═'*68}")

    tcp_s   = _stats(tcp_times)
    rest_s  = _stats(warm_times) if warm_times else _stats(list(rest_times.values())[0] if rest_times else [])
    ws_fm_s = _stats(ws_fm) if ws_fm else None
    ob_s    = _stats(ob_times) if ob_times else None

    if tcp_s:
        one_way = tcp_s[0] / 2   # min TCP RTT / 2 = best-case one-way
        print(f"\n  TCP min RTT:        {tcp_s[0]:.1f}ms")
        print(f"  Est. one-way:       {one_way:.1f}ms  (TCP min / 2, assuming symmetric routing)")

        print(f"\n  Arb execution timeline (best case):")
        print(f"    Book change at Kalshi  →  WS msg arrives at you:  ~{one_way:.1f}ms")
        print(f"    You detect arb + logic:                             ~0.1ms  (in-process)")
        print(f"    REST order departs → arrives at Kalshi:            ~{one_way:.1f}ms")
        print(f"    ─────────────────────────────────────────────────────────")
        print(f"    Total from book change to order at Kalshi:         ~{one_way*2:.1f}ms")

        if rest_s:
            print(f"\n  REST warm round-trip (order submission cost):       ~{rest_s[0]:.1f}ms min, {rest_s[1]:.1f}ms median")
            print(f"  (This includes Kalshi's server processing time + network RTT)")

        if ob_s:
            print(f"\n  Orderbook depth fetch:                              ~{ob_s[1]:.1f}ms median")
            print(f"  (REST depth verification before order — bot does this on new arb openings)")

        if ws_fm_s:
            print(f"\n  WS first message (from connect to data):            ~{ws_fm_s[1]:.1f}ms median")

        # Arb window capture assessment
        capturable_threshold_ms = one_way * 2 + 5   # TCP RTT + 5ms processing
        prev_assumption_ms      = 180.0
        print(f"\n  ── Window capture assessment ──────────────────────────────────────")
        print(f"  Previous assumption (Iceland server):   ~{prev_assumption_ms:.0f}ms one-way")
        print(f"  New estimate (this server):             ~{one_way:.1f}ms one-way")
        improvement = prev_assumption_ms / one_way if one_way > 0 else float("inf")
        print(f"  Improvement factor:                     ~{improvement:.0f}x faster")
        print(f"\n  With ~{one_way:.1f}ms one-way latency, arb windows as short as ~{capturable_threshold_ms:.0f}ms")
        print(f"  are realistically capturable (vs ~{int(prev_assumption_ms*2 + 5)}ms minimum before).")
        print(f"\n  For the analyze_kalshi_arb.py script, consider using:")
        print(f"    --min-duration {int(capturable_threshold_ms) + 5}   "
              f"(currently defaults to 500ms)")

    print(f"\n{'═'*68}\n")


# ─── Continuous mode ──────────────────────────────────────────────────────────

def run_continuous(rounds_per_cycle, ticker):
    """Runs latency checks in a loop, printing a rolling stats line every cycle."""
    print(f"Running continuous latency monitor (Ctrl+C to stop)...\n")
    print(f"{'Time':<10} {'TCP min':>8} {'TCP avg':>8} {'REST med':>8} {'WS hs':>8} {'WS msg':>8}")
    print("─" * 58)

    while True:
        now = time.strftime("%H:%M:%S")
        tcp_t, rest_t, ws_h, ws_m = [], [], [], []

        for _ in range(rounds_per_cycle):
            try:
                tcp_t.append(tcp_connect_ms(HOST, PORT))
            except Exception:
                pass

        url = ENDPOINTS["exchange/status"]
        sess = requests.Session()
        sess.headers.update({"Connection": "keep-alive"})
        for _ in range(rounds_per_cycle):
            try:
                t, _ = http_get_ms(url, session=sess)
                rest_t.append(t)
            except Exception:
                pass
        sess.close()

        if HAS_WS:
            try:
                hs, fm = ws_latency_ms(WS_URL, ticker)
                ws_h.append(hs)
                if fm is not None:
                    ws_m.append(fm)
            except Exception:
                pass

        tcp_min  = f"{min(tcp_t):.1f}ms"   if tcp_t  else "n/a"
        tcp_avg  = f"{statistics.mean(tcp_t):.1f}ms" if tcp_t else "n/a"
        rest_med = f"{statistics.median(rest_t):.1f}ms" if rest_t else "n/a"
        ws_hs_s  = f"{statistics.mean(ws_h):.1f}ms"  if ws_h  else "n/a"
        ws_ms_s  = f"{statistics.mean(ws_m):.1f}ms"  if ws_m  else "n/a"

        print(f"{now:<10} {tcp_min:>8} {tcp_avg:>8} {rest_med:>8} {ws_hs_s:>8} {ws_ms_s:>8}")

        try:
            time.sleep(30)
        except KeyboardInterrupt:
            print("\nStopped.")
            break


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Measure true latency to the Kalshi trading API",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("Usage:")[1] if "Usage:" in __doc__ else "",
    )
    parser.add_argument("--rounds",     type=int, default=10,
                        help="Rounds per measurement (default: 10)")
    parser.add_argument("--ticker",     default=None,
                        help="Kalshi market ticker for orderbook test, e.g. KXMLB-25APR12-T4.5")
    parser.add_argument("--continuous", action="store_true",
                        help="Run in continuous monitoring mode (prints rolling stats)")
    parser.add_argument("--no-ws",      action="store_true",
                        help="Skip WebSocket tests")
    parser.add_argument("--no-icmp",    action="store_true",
                        help="Skip ICMP ping")
    args = parser.parse_args()

    if args.continuous:
        run_continuous(rounds_per_cycle=args.rounds, ticker=args.ticker)
        return

    # ── One-shot benchmark ──────────────────────────────────────────────────
    ip = section_dns_ip(HOST)

    tcp_times  = section_tcp(HOST, PORT, args.rounds)
    tls_times  = section_tls(HOST, PORT, args.rounds)
    rest_times = section_rest(args.rounds)
    warm_times = section_rest_warm(args.rounds)

    ob_times = []
    if args.ticker:
        ob_times = section_orderbook(args.ticker, args.rounds)

    ws_hs, ws_fm = [], []
    if not args.no_ws:
        ws_hs, ws_fm = section_ws(WS_URL, args.ticker, rounds=min(args.rounds, 5))

    icmp_result = None
    if not args.no_icmp:
        icmp_result = section_icmp(HOST)

    print_summary(tcp_times, tls_times, rest_times, warm_times,
                  ob_times, ws_hs, ws_fm, icmp_result, args.ticker)


if __name__ == "__main__":
    main()
