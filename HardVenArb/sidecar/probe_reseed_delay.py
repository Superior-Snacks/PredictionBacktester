#!/usr/bin/env python3
"""
probe_reseed_delay.py — measure how far the PUBLIC GUEST feed lags the LOGGED-IN AUTHED feed for
/markets/straight, so "guest is delayed" becomes a measured number instead of an assumption.

WHY: the reader re-seed defaults to `authed` because a delayed guest price can swamp a ~1¢ pre-live edge.
This quantifies that delay for YOUR account/region so you can decide.

HOW: run the sidecar (HARDVEN_BOOK=pinnacle, browser session logged in — it holds the authed session). Then:

    python probe_reseed_delay.py --lid 3649 --secs 300
    python probe_reseed_delay.py --lid 3649,214126 --secs 300 --interval 2
    python probe_reseed_delay.py --from-pairs --secs 300         # every paired tennis league in cross_pairs.json

Every --interval it snapshots BOTH sources via the sidecar's /debug/straight, builds a per-token price
time-series for each, and for every AUTHED price CHANGE measures the lag until the GUEST feed shows the same
value. Reports median / p90 / max guest lag, how often guest never caught up within the window, and the
typical disagreement size in implied-probability cents (what actually eats an edge).

Read it as: small median lag + tiny disagreement ⇒ guest is fine; multi-second lag or fat disagreement ⇒
keep the re-seed on `authed` (the default).
"""
import argparse
import json
import os
import time
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
TENNIS_SERIES = ("KXATP", "KXWTA", "KXITF")


def _get(sidecar: str, lid: str, source: str, timeout: float = 20.0):
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
    i = min(len(s) - 1, int(round((p / 100.0) * (len(s) - 1))))
    return s[i]


def _cents(dec_a: float, dec_g: float) -> float:
    """|implied-prob difference| in cents between two decimal odds (what an edge is actually measured in)."""
    if dec_a <= 1.0 or dec_g <= 1.0:
        return 0.0
    return abs(1.0 / dec_a - 1.0 / dec_g) * 100.0


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sidecar", default=os.environ.get("HARDVEN_SIDECAR_URL", "http://127.0.0.1:8787"))
    ap.add_argument("--lid", default="", help="comma-separated Pinnacle league id(s)")
    ap.add_argument("--from-pairs", action="store_true", help="use every paired tennis league in cross_pairs.json")
    ap.add_argument("--secs", type=float, default=300.0, help="how long to sample (default 300)")
    ap.add_argument("--interval", type=float, default=2.0, help="seconds between snapshots (default 2)")
    args = ap.parse_args()

    leagues = _leagues_from_pairs() if args.from_pairs else [x.strip() for x in args.lid.split(",") if x.strip()]
    if not leagues:
        print("no leagues — pass --lid 3649[,214126] or --from-pairs (needs cross_pairs.json)")
        return
    print(f"probing {len(leagues)} league(s) for {args.secs:g}s @ {args.interval:g}s: {leagues}")
    print("(authed vs guest /markets/straight via the sidecar — the sidecar holds the logged-in session)\n")

    # token -> list of (ts, decimal) samples, per source
    a_series: dict = {}
    g_series: dict = {}
    tick_agree = []                         # per-tick (matched, disagree, mean_cents_when_disagree)
    t_end = time.time() + args.secs
    ticks = 0
    while time.time() < t_end:
        ticks += 1
        matched = disagree = 0
        cents_acc = 0.0
        for lid in leagues:
            try:
                a = _get(args.sidecar, lid, "authed")
                g = _get(args.sidecar, lid, "guest")
            except Exception as ex:
                print(f"  fetch error lid={lid}: {ex}")
                continue
            ap_, gp_ = a.get("prices") or {}, g.get("prices") or {}
            ats, gts = a.get("ts") or time.time(), g.get("ts") or time.time()
            for tok, price in ap_.items():
                a_series.setdefault(tok, []).append((ats, price))
            for tok, price in gp_.items():
                g_series.setdefault(tok, []).append((gts, price))
            for tok in set(ap_) & set(gp_):             # instantaneous agreement snapshot
                if abs(ap_[tok] - gp_[tok]) < 1e-6:
                    matched += 1
                else:
                    disagree += 1
                    cents_acc += _cents(ap_[tok], gp_[tok])
        tick_agree.append((matched, disagree, cents_acc / disagree if disagree else 0.0))
        time.sleep(args.interval)

    # ── lag: for each AUTHED change to value V at t_a, time until GUEST shows V ──
    lags = []
    uncaught = 0
    changes = 0
    for tok, aser in a_series.items():
        gser = g_series.get(tok)
        if not gser or len(aser) < 2:
            continue
        prev = aser[0][1]
        for ts_a, val in aser[1:]:
            if abs(val - prev) < 1e-6:
                continue                                # no change this sample
            prev = val
            changes += 1
            hit = next((tg for tg, gv in gser if tg >= ts_a and abs(gv - val) < 1e-6), None)
            if hit is None:
                uncaught += 1
            else:
                lags.append(max(0.0, hit - ts_a))

    tot_tok = len(set(a_series) & set(g_series))
    tot_matched = sum(m for m, _, _ in tick_agree)
    tot_dis = sum(d for _, d, _ in tick_agree)
    dis_cents = [c for _, d, c in tick_agree if d]
    print(f"\n=== RESULT ({ticks} ticks, {tot_tok} tokens in both feeds) ===")
    print(f"instantaneous agreement : {tot_matched}/{tot_matched + tot_dis} "
          f"({100 * tot_matched / max(1, tot_matched + tot_dis):.1f}%) of token-samples matched exactly")
    if dis_cents:
        print(f"  when they DISAGREED     : guest off by ~{sum(dis_cents) / len(dis_cents):.2f}¢ "
              f"(max {max(dis_cents):.2f}¢) implied-prob — this is what eats an edge")
    print(f"authed price CHANGES seen : {changes}")
    if lags:
        print(f"  guest catch-up lag      : median {_pct(lags, 50):.1f}s | p90 {_pct(lags, 90):.1f}s | "
              f"max {max(lags):.1f}s  (n={len(lags)})")
    if changes:
        print(f"  never caught in-window  : {uncaught}/{changes} ({100 * uncaught / changes:.0f}%)")
    verdict = ("GUEST LOOKS FINE — small lag + tiny disagreement" if lags and _pct(lags, 50) <= args.interval
               and (not dis_cents or sum(dis_cents) / len(dis_cents) < 0.3)
               else "GUEST IS DELAYED — keep the re-seed on `authed` (the default)" if (lags or changes)
               else "INCONCLUSIVE — no price changes observed (try a longer --secs or livelier leagues)")
    print(f"\nVERDICT: {verdict}")


if __name__ == "__main__":
    main()
