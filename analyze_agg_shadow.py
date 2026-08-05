#!/usr/bin/env python3
"""
analyze_agg_shadow.py -- is this odds aggregator good enough to drive the bot?

Reads the AggShadow_*.csv tape written by AggregatorAdapter in HARDVEN_AGG_MODE=shadow, where the sidecar
served the Pinnacle WS quotes it already trusts while logging the aggregator's quotes for the same tokens at
the same instants. That makes the WS the reference and the aggregator the thing under test.

Four questions decide a vendor, and they are answered in this order because a failure at any level makes the
next one moot:

  1. COVERAGE   Does it quote the tokens we actually watch? A vendor missing half the slate halves the tape.
  2. AGREEMENT  When both quote, do they agree? Systematic bias (median != 0) means a different price source
                or a stale mirror; wide spread means noise we would trade against.
  3. FOLLOW LAG When the book moves, how long until the vendor shows it? THE number that decides whether an
                aggregator can drive DETECTION. Arb windows in this book's tape last seconds.
  4. FRESHNESS  Does it publish a per-line update time, and is that line young? The C# feed gates on it
                (HARDVEN_QUOTE_MAX_AGE_MS, default 30s) -- no clock means no staleness gate means phantoms.

Usage:
    python analyze_agg_shadow.py                     # newest AggShadow_*.csv under HardVenArb/
    python analyze_agg_shadow.py --file path.csv
    python analyze_agg_shadow.py --tol 0.002         # odds-match tolerance for follow-lag (default 0.1%)
    python analyze_agg_shadow.py --max-age-ms 30000  # mirror the bot's freshness gate
    python analyze_agg_shadow.py --token 221309:1633332341:home   # drill into one selection
"""
from __future__ import annotations

import argparse
import csv
import glob
import os
import sys
from collections import defaultdict
from datetime import datetime


# ── helpers ───────────────────────────────────────────────────────────────────
def _f(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _pct(part, whole):
    return (100.0 * part / whole) if whole else 0.0


def _quantile(xs, q):
    """Linear-interpolated quantile of a sorted-able list. Empty -> None."""
    if not xs:
        return None
    s = sorted(xs)
    if len(s) == 1:
        return s[0]
    pos = q * (len(s) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (pos - lo)


def _fmt(v, nd=3, suffix=""):
    return "n/a" if v is None else f"{v:.{nd}f}{suffix}"


def _parse_time(s):
    try:
        return datetime.fromisoformat(s).timestamp()
    except Exception:
        return None


def find_csv(explicit=""):
    if explicit:
        return explicit if os.path.exists(explicit) else ""
    here = os.path.dirname(os.path.abspath(__file__))
    hits = glob.glob(os.path.join(here, "HardVenArb", "AggShadow_*.csv"))
    hits += glob.glob(os.path.join(here, "AggShadow_*.csv"))
    return max(hits, key=os.path.getmtime) if hits else ""


# ── load ──────────────────────────────────────────────────────────────────────
def load(path):
    rows = []
    with open(path, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            t = _parse_time(r.get("Time", ""))
            if t is None:
                continue
            rows.append({
                "t": t,
                "token": r.get("Token", ""),
                "mode": r.get("Mode", ""),
                "provider": r.get("Provider", ""),
                "b": _f(r.get("BookOdds")),
                "a": _f(r.get("AggOdds")),
                "diff_pct": _f(r.get("DiffPct"), None) if r.get("DiffPct") not in ("", None) else None,
                "b_ts": _f(r.get("BookTs")),
                "a_ts": _f(r.get("AggTs")),
                "a_age": _f(r.get("AggAgeSec"), None) if r.get("AggAgeSec") not in ("", None) else None,
                "a_chg_age": _f(r.get("AggChangedAgeSec"), None) if r.get("AggChangedAgeSec") not in ("", None) else None,
                "b_live": r.get("BookLive", ""),
                "a_live": r.get("AggLive", ""),
                "b_status": r.get("BookStatus", ""),
                "a_status": r.get("AggStatus", ""),
                "b_max": _f(r.get("BookMaxStake"), None) if r.get("BookMaxStake") not in ("", None) else None,
                "a_max": _f(r.get("AggMaxStake"), None) if r.get("AggMaxStake") not in ("", None) else None,
                "present": r.get("Present", ""),
            })
    rows.sort(key=lambda r: r["t"])
    return rows


# ── 2. agreement ──────────────────────────────────────────────────────────────
def settled_diffs(series):
    """Percent differences measured at SETTLED moments only.

    The shadow tape is change-driven, so a row exists mostly *at* a transition -- the one instant the vendor is
    guaranteed to still be catching up. Scoring agreement over raw rows therefore reports ~50% disagreement even
    for a flawless vendor, which measures our logging, not the feed. Instead: split each token's rows into
    epochs of constant BOOK odds and take the LAST row of each epoch, where the vendor has had the full epoch to
    converge. 'Do they agree once things settle?' is the real question; 'how long until they settle?' is the
    follow-lag section, and the two together describe the feed completely.
    """
    out, epoch_last, cur = [], None, None
    for r in series:
        if r["b"] <= 0:
            continue
        if cur is None or r["b"] != cur:
            if epoch_last is not None and epoch_last["a"] > 0:
                out.append((epoch_last["a"] / epoch_last["b"] - 1.0) * 100.0)
            cur, epoch_last = r["b"], r
        else:
            epoch_last = r
    if epoch_last is not None and epoch_last["a"] > 0:
        out.append((epoch_last["a"] / epoch_last["b"] - 1.0) * 100.0)
    return out


# ── 3. follow lag ─────────────────────────────────────────────────────────────
def follow_lags(series, tol):
    """For each BOOK move b0 -> b1, seconds until the aggregator first shows b1 (within `tol` relative).

    Returns (lags, followed, censored). CENSORED = the book moved again before the vendor caught up, so the
    lag is unknown-but-at-least-this-long; counting those as zero would flatter a slow vendor, so they are
    reported separately instead.

    RESOLUTION CAVEAT: the tape is change-driven at the sidecar's poll cadence (~9s default), so a measured
    lag is accurate to about one poll. Read 'median 1.2s' as 'inside one poll' and 'median 40s' as real.
    """
    lags, censored = [], 0
    for i in range(1, len(series)):
        b_prev, b_now = series[i - 1]["b"], series[i]["b"]
        if b_prev <= 0 or b_now <= 0 or b_prev == b_now:
            continue
        t_move = series[i]["t"]
        # Vendor already showing the new value at the move row = sub-poll follow (or it led the book).
        hit = None
        for j in range(i, len(series)):
            if series[j]["b"] > 0 and series[j]["b"] != b_now:
                censored += 1                       # book moved on before the vendor arrived
                break
            a = series[j]["a"]
            if a > 0 and abs(a / b_now - 1.0) <= tol:
                hit = series[j]["t"] - t_move
                break
        if hit is not None:
            lags.append(max(0.0, hit))
    return lags, len(lags), censored


# ── report ────────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(description="Evaluate an odds aggregator against the Pinnacle WS tape.")
    ap.add_argument("--file", default="", help="AggShadow CSV (default: newest found)")
    ap.add_argument("--tol", type=float, default=0.001, help="relative odds-match tolerance (default 0.001 = 0.1%%)")
    ap.add_argument("--max-age-ms", type=float, default=30000, help="the bot's freshness gate (default 30000)")
    ap.add_argument("--token", default="", help="drill into a single token")
    args = ap.parse_args()

    path = find_csv(args.file)
    if not path:
        print("No AggShadow_*.csv found. Run the sidecar with HARDVEN_BOOK=aggregator "
              "HARDVEN_AGG_MODE=shadow for a slate first.")
        return 1
    rows = load(path)
    if not rows:
        print(f"{os.path.basename(path)} has no parseable rows.")
        return 1
    if args.token:
        rows = [r for r in rows if r["token"] == args.token]
        if not rows:
            print(f"No rows for token {args.token}")
            return 1

    span_h = (rows[-1]["t"] - rows[0]["t"]) / 3600.0
    providers = sorted({r["provider"] for r in rows if r["provider"]})
    modes = sorted({r["mode"] for r in rows if r["mode"]})
    by_token = defaultdict(list)
    for r in rows:
        by_token[r["token"]].append(r)

    print("=" * 78)
    print(f"AGGREGATOR SHADOW REPORT  --  {os.path.basename(path)}")
    print("=" * 78)
    print(f"provider(s): {', '.join(providers) or 'n/a'}    mode(s): {', '.join(modes) or 'n/a'}")
    print(f"rows: {len(rows)}   tokens: {len(by_token)}   span: {span_h:.2f} h")
    if "live" in modes:
        print("NOTE: rows logged in LIVE mode -- the bot was already trading the aggregator's prices there.")

    # 1. COVERAGE
    print("\n" + "-" * 78)
    print("1. COVERAGE  -- does the vendor quote what we watch?")
    print("-" * 78)
    n_both = sum(1 for r in rows if r["present"] == "both")
    n_book = sum(1 for r in rows if r["present"] == "book_only")
    n_agg  = sum(1 for r in rows if r["present"] == "agg_only")
    tok_any_agg = sum(1 for t, s in by_token.items() if any(r["a"] > 0 for r in s))
    print(f"  rows both sides      : {n_both:>7}  ({_pct(n_both, len(rows)):5.1f}%)")
    print(f"  rows book only       : {n_book:>7}  ({_pct(n_book, len(rows)):5.1f}%)   <- vendor silent")
    print(f"  rows aggregator only : {n_agg:>7}  ({_pct(n_agg, len(rows)):5.1f}%)   <- book silent/suspended")
    print(f"  tokens ever quoted   : {tok_any_agg}/{len(by_token)}  ({_pct(tok_any_agg, len(by_token)):.1f}%)")

    # 2. AGREEMENT
    print("\n" + "-" * 78)
    print("2. AGREEMENT -- when both quote, do they agree?")
    print("-" * 78)
    diffs = []
    for tok, s in by_token.items():
        diffs.extend(settled_diffs(s))
    raw = [r["diff_pct"] for r in rows if r["diff_pct"] is not None and r["present"] == "both"]
    if diffs:
        med = _quantile(diffs, 0.5)
        within = lambda x: _pct(sum(1 for d in diffs if abs(d) <= x), len(diffs))
        print(f"  settled samples      : {len(diffs)}   (one per constant-book-price epoch, per token)")
        print(f"  median diff          : {_fmt(med, 4, '%')}   <- bias; away from 0 = different price source")
        print(f"  p10 / p90            : {_fmt(_quantile(diffs, 0.10), 4, '%')} / {_fmt(_quantile(diffs, 0.90), 4, '%')}")
        print(f"  |diff| <= 0.10%      : {within(0.10):5.1f}%")
        print(f"  |diff| <= 0.50%      : {within(0.50):5.1f}%")
        print(f"  |diff| <= 1.00%      : {within(1.00):5.1f}%")
        if raw:
            rw = _pct(sum(1 for d in raw if abs(d) <= 0.5), len(raw))
            print(f"  (raw all-row basis   : {rw:5.1f}% within 0.50% over {len(raw)} rows -- lower by construction,")
            print( "   since a change-driven tape samples mostly mid-transition. Judge on the settled figure.)")
    else:
        print("  no settled samples with both sides present -- nothing to compare.")

    # 3. FOLLOW LAG
    print("\n" + "-" * 78)
    print("3. FOLLOW LAG -- when the book moves, when does the vendor show it?")
    print("-" * 78)
    all_lags, tot_followed, tot_censored = [], 0, 0
    for tok, s in by_token.items():
        lags, followed, censored = follow_lags(s, args.tol)
        all_lags.extend(lags)
        tot_followed += followed
        tot_censored += censored
    moves = tot_followed + tot_censored
    if all_lags:
        print(f"  book moves observed  : {moves}   followed: {tot_followed}   "
              f"censored (book moved again first): {tot_censored}")
        print(f"  median lag           : {_fmt(_quantile(all_lags, 0.5), 2, ' s')}")
        print(f"  p90 lag              : {_fmt(_quantile(all_lags, 0.9), 2, ' s')}")
        print(f"  max lag              : {_fmt(max(all_lags), 2, ' s')}")
        print(f"  followed <= 2s       : {_pct(sum(1 for l in all_lags if l <= 2), len(all_lags)):5.1f}%")
        print(f"  followed <= 10s      : {_pct(sum(1 for l in all_lags if l <= 10), len(all_lags)):5.1f}%")
        print("  (resolution ~= the sidecar poll interval; 'median 1s' means 'inside one poll')")
    else:
        print(f"  no completed follows out of {moves} book move(s) -- "
              "either the tape is too short or the vendor never catches up.")

    # 4. FRESHNESS + LIMITS
    print("\n" + "-" * 78)
    print("4. FRESHNESS + LIMITS -- can the staleness gate and the stake ladder work?")
    print("-" * 78)
    ages = [r["a_age"] for r in rows if r["a_age"] is not None and r["a"] > 0]
    n_no_ts = sum(1 for r in rows if r["a"] > 0 and not r["a_ts"])
    n_agg_rows = sum(1 for r in rows if r["a"] > 0)
    gate_s = args.max_age_ms / 1000.0
    if ages:
        over = sum(1 for a in ages if a > gate_s)
        print(f"  vendor line age  med : {_fmt(_quantile(ages, 0.5), 2, ' s')}   "
              f"p90: {_fmt(_quantile(ages, 0.9), 2, ' s')}   max: {_fmt(max(ages), 2, ' s')}")
        print(f"  older than the gate  : {over}/{len(ages)}  ({_pct(over, len(ages)):.1f}%)  (gate = {gate_s:.0f}s)")
    else:
        print("  vendor published NO per-line update time on any row.")
    print(f"  rows with no vendor ts: {n_no_ts}/{n_agg_rows}  ({_pct(n_no_ts, n_agg_rows):.1f}%)"
          + ("   <- staleness gate CANNOT work on these" if n_no_ts else ""))
    chg = [r["a_chg_age"] for r in rows if r["a_chg_age"] is not None and r["a"] > 0]
    if chg:
        print(f"  line-change age (changedAt)  med: {_fmt(_quantile(chg, 0.5), 1, ' s')}   "
              f"p90: {_fmt(_quantile(chg, 0.9), 1, ' s')}   max: {_fmt(max(chg), 1, ' s')}")
        print("  (informational: how long lines sit between moves. Large values on stable pre-match lines are")
        print("   normal -- this is why the client's freshness stamp is heartbeat-gated poll time, not changedAt.)")
    n_lim = sum(1 for r in rows if r["a_max"] is not None)
    print(f"  rows with vendor limit: {n_lim}/{n_agg_rows}  ({_pct(n_lim, n_agg_rows):.1f}%)"
          + ("" if n_lim else "   <- no limits: depth must come from the book (HARDVEN_AGG_LIMITS=inner)"))

    # VERDICT
    print("\n" + "=" * 78)
    print("VERDICT")
    print("=" * 78)
    checks = []
    checks.append(("coverage >= 90% of watched tokens", _pct(tok_any_agg, len(by_token)) >= 90.0))
    if diffs:
        checks.append(("no systematic bias (|median diff| <= 0.1%)", abs(_quantile(diffs, 0.5)) <= 0.1))
        checks.append(("agreement (>= 90% within 0.5%)",
                       _pct(sum(1 for d in diffs if abs(d) <= 0.5), len(diffs)) >= 90.0))
    if all_lags:
        checks.append(("median follow lag <= 5s", _quantile(all_lags, 0.5) <= 5.0))
        checks.append(("p90 follow lag <= 15s", _quantile(all_lags, 0.9) <= 15.0))
        checks.append(("most moves actually followed (>= 80%)", _pct(tot_followed, moves) >= 80.0))
    checks.append(("publishes a per-line update time", n_agg_rows > 0 and n_no_ts == 0))
    if ages:
        checks.append((f"lines younger than the {gate_s:.0f}s gate (>= 95%)",
                       _pct(sum(1 for a in ages if a <= gate_s), len(ages)) >= 95.0))
    for label, ok in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}]  {label}")
    n_fail = sum(1 for _, ok in checks if not ok)
    print()
    if not checks:
        print("  Not enough data to judge -- let shadow mode run through a real slate.")
    elif n_fail == 0:
        print("  All checks pass. This vendor can drive DETECTION: set HARDVEN_AGG_MODE=live.")
        print("  Placement, balance and bet monitoring stay on the book's UI path either way.")
    else:
        print(f"  {n_fail} check(s) failed -- do NOT switch HARDVEN_AGG_MODE=live yet.")
        print("  A failing follow-lag or freshness check means the vendor would hand the bot prices the book")
        print("  has already moved away from: the exact phantom-arb shape the WS-verify gate exists to stop.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
