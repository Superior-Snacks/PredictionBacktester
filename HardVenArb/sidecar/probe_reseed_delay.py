#!/usr/bin/env python3
"""
probe_reseed_delay.py — measure how far the PUBLIC GUEST feed lags the AUTHED feed for /markets/straight, so
"guest is delayed" becomes a measured number instead of an assumption.

GENTLE BY DESIGN (a rushed run could rate-limit/flag the account): it defaults to ONE league, a 5s interval,
and a hard request-rate cap, and it PRINTS THE PROJECTED LOAD and waits for you to confirm before sending a
single request. Two authed-truth sources:

  --authed-source cache   (DEFAULT, SAFEST) read the sidecar's LIVE WS cache as the authed truth → the probe
                          adds ONLY the public GUEST calls, ZERO extra logged-in requests. Requires the league
                          to be WS-covered (has a tab / on the board) so the cache is real-time — pick a LIVE
                          match. If the cache authed side looks static, it warns.
  --authed-source rest    one logged-in /markets/straight per sample (real-time for ANY league, but adds authed
                          load — kept tiny by the gentle defaults + rate cap).

Run the sidecar (pinnacle, logged in), then e.g.:

    python probe_reseed_delay.py --lid 3649 --secs 300                 # 1 live league, cache-authed + guest
    python probe_reseed_delay.py --lid 3649 --authed-source rest       # real-time authed REST (a bit more load)
    python probe_reseed_delay.py --lid 3649,214126 --max-rps 0.5       # even gentler

At ~0.4 req/s this is browsing-level traffic, far below the bot's own re-seed. Reports median/p90/max guest
catch-up lag + the typical disagreement in implied-prob cents (what eats an edge).
"""
import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.request

try:
    sys.stdout.reconfigure(encoding="utf-8")   # Windows console: tolerate ¢ and any accented names
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
TENNIS_SERIES = ("KXATP", "KXWTA", "KXITF")


class Pacer:
    """Hard rate cap: never issue requests faster than max_rps (spreads them so no burst looks anomalous)."""
    def __init__(self, max_rps: float):
        self._min_gap = 1.0 / max_rps if max_rps > 0 else 0.0
        self._last = 0.0

    def wait(self) -> None:
        if self._min_gap:
            dt = time.time() - self._last
            if dt < self._min_gap:
                time.sleep(self._min_gap - dt)
        self._last = time.time()


def _get(sidecar: str, lid: str, source: str, pacer: Pacer, timeout: float = 20.0):
    pacer.wait()
    q = urllib.parse.urlencode({"lid": lid, "source": source})
    with urllib.request.urlopen(f"{sidecar.rstrip('/')}/debug/straight?{q}", timeout=timeout) as r:
        return json.load(r)


def _leagues_from_pairs() -> list:
    path = os.path.join(os.path.dirname(HERE), "cross_pairs.json")
    try:
        data = json.load(open(path, encoding="utf-8-sig"))
    except Exception:
        return []
    out = []
    for e in data:
        tok = e.get("hardven_yes_token") or ""
        if tok.count(":") == 2 and any((e.get("kalshi_ticker") or "").startswith(s) for s in TENNIS_SERIES):
            lid = tok.split(":")[0]
            if lid not in out:
                out.append(lid)
    return out


def _pct(xs: list, p: float) -> float:
    if not xs:
        return float("nan")
    s = sorted(xs)
    return s[min(len(s) - 1, int(round((p / 100.0) * (len(s) - 1))))]


def _cents(dec_a: float, dec_g: float) -> float:
    if dec_a <= 1.0 or dec_g <= 1.0:
        return 0.0
    return abs(1.0 / dec_a - 1.0 / dec_g) * 100.0


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sidecar", default=os.environ.get("HARDVEN_SIDECAR_URL", "http://127.0.0.1:8787"))
    ap.add_argument("--lid", default="", help="comma-separated Pinnacle league id(s) — pick LIVE matches")
    ap.add_argument("--from-pairs", action="store_true", help="sample paired tennis leagues (capped by --max-leagues)")
    ap.add_argument("--max-leagues", type=int, default=1, help="cap leagues sampled (default 1 — the delay is a "
                    "feed property, so 1-2 live leagues is representative)")
    ap.add_argument("--authed-source", choices=("cache", "rest"), default="cache",
                    help="'cache' (default) = read the WS cache as authed truth → ZERO extra authed requests; "
                         "'rest' = a logged-in REST call per sample")
    ap.add_argument("--secs", type=float, default=300.0, help="how long to sample (default 300)")
    ap.add_argument("--interval", type=float, default=5.0, help="seconds between rounds (default 5)")
    ap.add_argument("--max-rps", type=float, default=1.0, help="hard cap on requests/sec (default 1.0)")
    ap.add_argument("--yes", action="store_true", help="skip the confirm prompt")
    args = ap.parse_args()

    leagues = _leagues_from_pairs() if args.from_pairs else [x.strip() for x in args.lid.split(",") if x.strip()]
    leagues = leagues[:max(1, args.max_leagues)]
    if not leagues:
        print("no leagues — pass --lid 3649 (a LIVE match) or --from-pairs")
        return
    a_src = "authed" if args.authed_source == "rest" else "cache"   # source name the sidecar understands
    reqs_per_round = len(leagues) * (2 if a_src == "authed" else 1)  # cache = no request; guest always 1
    round_time = max(args.interval, reqs_per_round / args.max_rps if args.max_rps > 0 else 0)
    rounds = max(1, int(args.secs / round_time))
    total = rounds * reqs_per_round
    authed_total = rounds * len(leagues) if a_src == "authed" else 0
    guest_total = rounds * len(leagues)

    print(f"PLAN: {len(leagues)} league(s) {leagues}, authed-truth={args.authed_source}, "
          f"{args.secs:g}s @ ~{round_time:.1f}s/round, cap {args.max_rps:g} req/s")
    print(f"  -> ~{total} sidecar calls total, ~{total / args.secs:.2f} req/s "
          f"(AUTHED Pinnacle calls: {authed_total}, GUEST Pinnacle calls: {guest_total})")
    if a_src == "cache":
        print("  authed truth comes from the WS cache → this run adds ZERO extra authed requests (only public "
              "guest). The league MUST be WS-covered (a tab / on the board) or the cache side is stale.")
    print("  (for reference, the bot's own re-seed already does ~1 authed call / league / 90s.)")
    if not args.yes:
        try:
            if input("proceed? [y/N] ").strip().lower() not in ("y", "yes"):
                print("aborted."); return
        except EOFError:
            print("non-interactive; pass --yes to run."); return

    pacer = Pacer(args.max_rps)
    a_series: dict = {}
    g_series: dict = {}
    tick_agree = []
    a_static_guard = {}                      # detect a static authed-cache (league not WS-covered)
    t_end = time.time() + args.secs
    rounds_done = 0
    while time.time() < t_end:
        rounds_done += 1
        for lid in leagues:
            try:
                a = _get(args.sidecar, lid, a_src, pacer)
                g = _get(args.sidecar, lid, "guest", pacer)
            except Exception as ex:
                print(f"  fetch error lid={lid}: {ex}")
                continue
            ap_, gp_ = a.get("prices") or {}, g.get("prices") or {}
            ats, gts = a.get("ts") or time.time(), g.get("ts") or time.time()
            for tok, price in ap_.items():
                a_series.setdefault(tok, []).append((ats, price))
                a_static_guard.setdefault(tok, set()).add(price)
            for tok, price in gp_.items():
                g_series.setdefault(tok, []).append((gts, price))
            m = d = 0
            cents = 0.0
            for tok in set(ap_) & set(gp_):
                if abs(ap_[tok] - gp_[tok]) < 1e-6:
                    m += 1
                else:
                    d += 1
                    cents += _cents(ap_[tok], gp_[tok])
            tick_agree.append((m, d, cents / d if d else 0.0))
        time.sleep(args.interval)

    # lag: for each AUTHED change to V at t_a, time until GUEST shows V
    lags, uncaught, changes = [], 0, 0
    for tok, aser in a_series.items():
        gser = g_series.get(tok)
        if not gser or len(aser) < 2:
            continue
        prev = aser[0][1]
        for ts_a, val in aser[1:]:
            if abs(val - prev) < 1e-6:
                continue
            prev = val
            changes += 1
            hit = next((tg for tg, gv in gser if tg >= ts_a and abs(gv - val) < 1e-6), None)
            if hit is None:
                uncaught += 1
            else:
                lags.append(max(0.0, hit - ts_a))

    tot_tok = len(set(a_series) & set(g_series))
    tm = sum(x[0] for x in tick_agree); td = sum(x[1] for x in tick_agree)
    dis_cents = [c for _, d, c in tick_agree if d]
    print(f"\n=== RESULT ({rounds_done} rounds, {tot_tok} tokens in both feeds) ===")
    if a_src == "cache" and changes == 0 and all(len(v) <= 1 for v in a_static_guard.values()):
        print("WARNING: the authed CACHE side never changed — the league is likely NOT WS-covered (no tab / not "
              "on the board), so the cache isn't a live truth. Re-run with a LIVE, tab-covered league or "
              "--authed-source rest.")
    print(f"instantaneous agreement : {tm}/{tm + td} ({100 * tm / max(1, tm + td):.1f}%) matched exactly")
    if dis_cents:
        print(f"  when they DISAGREED   : guest off by ~{sum(dis_cents) / len(dis_cents):.2f}¢ "
              f"(max {max(dis_cents):.2f}¢) implied-prob — this is what eats an edge")
    print(f"authed price CHANGES    : {changes}")
    # A FAST market (authed changing far quicker than we poll) confounds the lag + "never caught" numbers: the
    # authed truth updates between guest samples, and with authed=cache the change TS is the read time (not the
    # WS-update time), so lags collapse to ~one request-gap. Those two lines are then unreliable — the clean
    # signal is the instantaneous agreement / disagreement-¢, and a STABLE PRE-MATCH league is the honest test.
    fast_market = changes > 2 * max(1, rounds_done)
    lag_degenerate = len(lags) >= 3 and (max(lags) - min(lags)) < 0.5
    if lags:
        print(f"  guest catch-up lag    : median {_pct(lags, 50):.1f}s | p90 {_pct(lags, 90):.1f}s | "
              f"max {max(lags):.1f}s (n={len(lags)})")
    if changes:
        print(f"  never caught in-window: {uncaught}/{changes} ({100 * uncaught / changes:.0f}%)")
    if fast_market or lag_degenerate:
        print("  NOTE: FAST market (authed changed much faster than the poll rate) — the lag / never-caught lines "
              "above are UNRELIABLE (sample-rate + read-skew artifacts). Trust the agreement / disagreement-¢, "
              "and for a clean PRE-LIVE answer re-run on a STABLE pre-match league (few changes).")
    mean_c = (sum(dis_cents) / len(dis_cents)) if dis_cents else 0.0
    agree_pct = 100 * tm / max(1, tm + td)
    if fast_market:
        verdict = (f"INCONCLUSIVE for PRE-LIVE (fast/in-play market) — but guest disagrees ~{mean_c:.1f}¢, which "
                   "leans keep-`authed`. Re-run on a stable pre-match league for a clean read.")
    elif changes == 0:
        verdict = "INCONCLUSIVE — no price changes seen (use a livelier league / longer --secs)"
    elif agree_pct >= 90 and mean_c < 0.3:
        verdict = "GUEST LOOKS FINE — high agreement + tiny disagreement on a stable market"
    else:
        verdict = f"GUEST IS OFF (~{mean_c:.1f}¢ disagreement, {agree_pct:.0f}% agree) — keep the re-seed on `authed`"
    print(f"\nVERDICT: {verdict}")


if __name__ == "__main__":
    main()
