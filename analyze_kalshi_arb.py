"""
Kalshi Arb Telemetry Analyzer
Reads ArbTelemetry_*.csv from the Kalshi paper trader and produces:
  1. Session summary
  2. All markets detected (every event — clean and flagged)
  3. Fraud / sanity checks (flags bad rows)
  4. Duration histogram
  5. Profit analysis
  6. REST verification analysis
  7. Actual resolution check (Kalshi API — win/loss/active per event)
  8. Series loss tracker (auto-updates event_blocklist.json)
  9. Production sim (US server, competition-adjusted, clean categoricals only)

Usage:
  python analyze_kalshi_arb.py
  python analyze_kalshi_arb.py --file path/to/ArbTelemetry_xxx.csv
  python analyze_kalshi_arb.py --min-duration 200
  python analyze_kalshi_arb.py --exclude KXEPLSPREAD-26APR11BREEVE,KXHIGHTSATX-26APR11
  python analyze_kalshi_arb.py --clean   # only non-flagged rows
"""

import csv
import sys
import glob
import os
import re
import json
import time
import argparse
from collections import defaultdict
from datetime import datetime, timedelta

# ─── CONFIG ───────────────────────────────────────────────────────────────────
DEFAULT_MIN_DURATION_MS = 17    # US server: ~6ms one-way + ~5ms processing = ~17ms floor
DEFAULT_LATENCY_MS      = 6     # US server (JFK50-P1) → Kalshi origin, one-way
DEFAULT_CAPITAL_PER_ARB = 50.0  # max $ per arb attempt
DEFAULT_CAPTURE_RATE    = 0.60  # realistic fill rate

PROD_LATENCY_MS         = 17    # US server min capturable window (6ms one-way × 2 + 5ms)

PRICE_SUM_LOW_THRESHOLD  = 0.70   # below this = likely NOT mutually exclusive
PRICE_SUM_HIGH_THRESHOLD = 1.20   # above this = likely missing legs
REPEAT_SPAM_THRESHOLD    = 100    # >N windows for same event = spam (100 = ~1 window/5min over 9h)
THIN_DEPTH_THRESHOLD     = 2.0    # below this = 1-contract resting order noise

# ─── CSV DISCOVERY ────────────────────────────────────────────────────────────

def find_latest_csv():
    candidates = (
        glob.glob("ArbTelemetry_*.csv") +
        glob.glob("KalshiPaperTrader/ArbTelemetry_*.csv") +
        glob.glob("KalshiPaperTrader/bin/**/ArbTelemetry_*.csv", recursive=True)
    )
    if not candidates:
        return None
    return max(candidates, key=os.path.getctime)

# ─── SESSION DURATION ─────────────────────────────────────────────────────────

_DT_FORMATS = [
    "%Y-%m-%d %H:%M:%S.%f",   # new: full date  "2026-04-12 14:23:45.123"
    "%Y-%m-%d %H:%M:%S",      # new: no millis  "2026-04-12 14:23:45"
]

def _parse_dt(s):
    """Try to parse s as a full datetime. Returns datetime or None."""
    for fmt in _DT_FORMATS:
        try:
            return datetime.strptime(s.strip(), fmt)
        except ValueError:
            pass
    return None

def _hms_to_secs(s):
    """'HH:MM:SS.fff' → float seconds since midnight (legacy time-only format)."""
    try:
        parts = s.strip().replace('.', ':').split(':')
        h, m, sec = int(parts[0]), int(parts[1]), int(parts[2])
        ms = int(parts[3]) if len(parts) > 3 else 0
        return h * 3600 + m * 60 + sec + ms / 1000.0
    except (ValueError, IndexError):
        return None

def compute_session_hours(rows, path):
    """
    Total elapsed wall-clock time of the session in hours.

    New CSV format (yyyy-MM-dd HH:mm:ss.fff): parse datetimes directly — exact
    for any session length including multi-day.

    Legacy format (HH:mm:ss.fff): anchor on the filename date and advance the
    last end-time by whole days until it is past the first start time.
    Falls back to a 24h assumption if no filename date is available.

    Returns 0.0 if timestamps cannot be parsed.
    """
    if len(rows) < 2:
        return 0.0

    # ── New format: full datetime strings ─────────────────────────────────────
    dt_start = _parse_dt(rows[0]["start"])
    dt_end   = _parse_dt(rows[-1]["end"])
    if dt_start is not None and dt_end is not None:
        secs = (dt_end - dt_start).total_seconds()
        if secs <= 0:
            # Rows are in close-time order; if last close < first open the session
            # wrapped midnight. The end timestamp being < start means we need to
            # find the actual max end across all rows.
            all_ends = [_parse_dt(r["end"]) for r in rows]
            all_ends = [d for d in all_ends if d is not None]
            if all_ends:
                dt_end = max(all_ends)
                secs   = (dt_end - dt_start).total_seconds()
        return max(secs / 3600.0, 1 / 3600.0)

    # ── Legacy format: time-only strings ──────────────────────────────────────
    t_start = _hms_to_secs(rows[0]["start"])
    t_end   = _hms_to_secs(rows[-1]["end"])
    if t_start is None or t_end is None:
        return 0.0

    m = re.search(r'ArbTelemetry_(\d{8})_(\d{6})', os.path.basename(path))
    if m:
        session_start_dt = datetime.strptime(m.group(1) + m.group(2), '%Y%m%d%H%M%S')
        base    = session_start_dt.replace(hour=0, minute=0, second=0, microsecond=0)
        dt_s    = base + timedelta(seconds=t_start)
        dt_e    = base + timedelta(seconds=t_end)
        for _ in range(30):          # advance by days until end > start
            if dt_e > dt_s:
                break
            dt_e += timedelta(days=1)
        secs = (dt_e - dt_s).total_seconds()
    else:
        secs = t_end - t_start
        if secs <= 0:
            secs += 86400             # single midnight crossing

    return max(secs / 3600.0, 1 / 3600.0)

def _per_hr(amount, hours):
    """Format 'amount' with a trailing '/hr' annotation."""
    if hours <= 0:
        return f"${amount:.4f}"
    return f"${amount:.4f}  (${amount/hours:.2f}/hr over {hours:.1f}h)"

def _hr(amount, hours):
    """Compact $/hr string for table columns."""
    if hours <= 0:
        return f"${amount:+.2f}"
    return f"${amount/hours:+.2f}/hr"

# ─── DATA LOADING ─────────────────────────────────────────────────────────────

def _try_float(r, key, default=None):
    v = r.get(key, "").strip()
    if v in ("", "N/A", "n/a"):
        return default
    try:
        return float(v)
    except ValueError:
        return default

def _try_bool(r, key, default=None):
    v = r.get(key, "").strip().lower()
    if v in ("true", "1"):
        return True
    if v in ("false", "0"):
        return False
    return default

def load_csv(path):
    rows = []
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for r in reader:
            try:
                raw_prices  = r["LegPrices"].strip('"')
                leg_prices  = [float(p) for p in raw_prices.split("|") if p.strip()]
                # LegTickers added in a later version — optional for backward compat
                raw_tickers = r.get("LegTickers", "").strip('"')
                leg_tickers = [t.strip() for t in raw_tickers.split("|") if t.strip()]
                rows.append({
                    "start":               r["StartTime"].strip(),
                    "end":                 r["EndTime"].strip(),
                    "duration_ms":         float(r["DurationMs"]),
                    "event":               r["EventId"].strip('"').strip(),
                    "num_legs":            int(r["NumLegs"]),
                    "leg_tickers":         leg_tickers,
                    "leg_prices":          leg_prices,
                    "leg_sum":             sum(leg_prices),
                    "entry_net_cost":      float(r["EntryNetCost"]),
                    "best_gross_cost":     float(r["BestGrossCost"]),
                    "total_fees":          float(r["TotalFees"]),
                    "best_net_cost":       float(r["BestNetCost"]),
                    "net_profit_per_share":float(r["NetProfitPerShare"]),
                    "max_volume":          float(r["MaxVolume"]),
                    "total_capital_req":   float(r["TotalCapitalRequired"]),
                    "total_potential":     float(r["TotalPotentialProfit"]),
                    # REST verification columns (None if not in file / not yet checked)
                    "rest_checked":        _try_bool(r,  "RestChecked"),
                    "rest_confirmed":      _try_bool(r,  "RestConfirmed"),
                    "rest_yes_ask_sum":    _try_float(r, "RestYesAskSum"),
                    "rest_min_depth":      _try_float(r, "RestMinDepth"),
                    "rest_delay_ms":       _try_float(r, "RestCheckDelayMs"),
                })
            except (KeyError, ValueError):
                continue
    return rows

# ─── DATE-SERIES MEE DETECTION ───────────────────────────────────────────────

_DATE_SUFFIX_RE = re.compile(
    r'-\d{2}(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)\d{1,2}$',
    re.IGNORECASE)

def _is_date_series_monotonic(leg_tickers, leg_prices):
    """
    Returns True when the legs appear to form a cumulative date-series rather than
    true mutually-exclusive outcomes — detected by two signals that must both be
    present:

      Tier 3 (ticker pattern) — ≥2/3 of tickers end in a Kalshi date suffix
          (YYMONDD, e.g. -26MAY1).  When leg_tickers is empty (old CSV format),
          this signal is skipped and only Tier 2 is evaluated.

      Tier 2 (price monotonicity) — prices are non-decreasing when tickers are
          sorted alphabetically (= chronological for date suffixes).  With no
          ticker data, checks whether the stored price sequence itself is
          non-decreasing (weaker but still useful).

    Both signals must pass (or Tier 3 is unavailable and Tier 2 passes alone).
    """
    if len(leg_prices) < 2:
        return False

    if leg_tickers:
        # Tier 3: check date suffix coverage
        date_count = sum(1 for t in leg_tickers if _DATE_SUFFIX_RE.search(t))
        if date_count * 3 < len(leg_tickers) * 2:   # < 2/3 have date suffixes
            return False
        # Tier 2: sort by ticker (= chronological), check monotonicity
        pairs = sorted(zip(leg_tickers, leg_prices), key=lambda x: x[0])
        prices_ordered = [p for _, p in pairs]
    else:
        # No ticker data (old CSV) — Tier 3 unavailable, check Tier 2 only
        prices_ordered = leg_prices

    return all(
        prices_ordered[i] >= prices_ordered[i - 1] - 0.03   # 3-cent tolerance
        for i in range(1, len(prices_ordered))
    )

# ─── FRAUD FLAGS ──────────────────────────────────────────────────────────────

def compute_flags(rows, spam_threshold=REPEAT_SPAM_THRESHOLD):
    # First pass: per-row flags
    event_counts = defaultdict(int)
    for r in rows:
        event_counts[r["event"]] += 1

    for r in rows:
        flags = []
        if r["leg_sum"] < PRICE_SUM_LOW_THRESHOLD:
            flags.append("PRICE_SUM_LOW")
        if r["leg_sum"] > PRICE_SUM_HIGH_THRESHOLD:
            flags.append("PRICE_SUM_HIGH")
        if r["net_profit_per_share"] <= 0:
            flags.append("ZERO_PROFIT")
        if r["duration_ms"] < 10:
            flags.append("INSTANT_OPEN_CLOSE")
        if r["duration_ms"] > 3_600_000:
            flags.append("IMPLAUSIBLE_DURATION")
        if r["max_volume"] < THIN_DEPTH_THRESHOLD:
            flags.append("THIN_DEPTH")
        if r["best_net_cost"] > 1.00:
            flags.append("COST_EXCEEDS_1")
        if event_counts[r["event"]] > spam_threshold:
            flags.append("REPEAT_SPAM")
        # PARTIAL_LEGS_REST: REST check fetched more active markets than we subscribed to,
        # meaning our WS "arb" is only a subset of the full event's legs. The event is
        # NOT a true categorical — buying our tracked legs does not guarantee a $1 payout.
        if (r.get("rest_checked") is True
                and r.get("rest_yes_ask_sum") is not None
                and r["rest_yes_ask_sum"] > 1.50):
            flags.append("PARTIAL_LEGS_REST")
        # DATE_SERIES_MONOTONIC: legs form a cumulative "before date X" series —
        # all legs can simultaneously resolve NO, making this structurally unsafe.
        # Tier 3: ≥2/3 of tickers end in YYMONDD date suffix (e.g. -26MAY1)
        # Tier 2: prices are monotonically non-decreasing sorted by ticker
        if _is_date_series_monotonic(r.get("leg_tickers", []), r["leg_prices"]):
            flags.append("DATE_SERIES_MONOTONIC")
        r["flags"] = flags

    # Second pass: propagate event-level disqualifiers.
    # If ANY window for an event gets PARTIAL_LEGS_REST, the whole event is
    # non-exhaustive (WS tracked fewer legs than exist). Flag every row.
    partial_rest_events = {r["event"] for r in rows if "PARTIAL_LEGS_REST" in r["flags"]}
    for r in rows:
        if r["event"] in partial_rest_events and "PARTIAL_LEGS_REST" not in r["flags"]:
            r["flags"].append("PARTIAL_LEGS_REST")

# ─── SECTION 1: SESSION SUMMARY ───────────────────────────────────────────────

def print_session_summary(rows, path, session_hours):
    print("=" * 80)
    print("  KALSHI ARB TELEMETRY ANALYSIS")
    print("=" * 80)
    print(f"  File     : {path}")
    print(f"  Rows     : {len(rows)}")
    if not rows:
        return

    unique_events = len(set(r["event"] for r in rows))
    avg_legs = sum(r["num_legs"] for r in rows) / len(rows)
    total_potential = sum(r["total_potential"] for r in rows)

    starts = [r["start"] for r in rows]
    ends   = [r["end"]   for r in rows]
    print(f"  Events   : {unique_events} unique")
    print(f"  Avg legs : {avg_legs:.1f}")
    print(f"  Duration : {session_hours:.1f}h  ({starts[0]} — {ends[-1]})")
    print(f"  Total potential profit (all windows): {_per_hr(total_potential, session_hours)}")
    print()

# ─── SECTION 2: FRAUD / SANITY CHECKS ────────────────────────────────────────

def print_fraud_checks(rows, spam_threshold=REPEAT_SPAM_THRESHOLD):
    print("=" * 80)
    print("  FRAUD / SANITY CHECKS")
    print("=" * 80)
    if not rows:
        print("  (no data)")
        print()
        return

    all_flag_names = [
        "PRICE_SUM_LOW", "PRICE_SUM_HIGH", "ZERO_PROFIT",
        "INSTANT_OPEN_CLOSE", "IMPLAUSIBLE_DURATION",
        "THIN_DEPTH", "REPEAT_SPAM", "COST_EXCEEDS_1",
        "PARTIAL_LEGS_REST",
    ]

    flag_descriptions = {
        "PRICE_SUM_LOW":        f"Leg prices sum < {PRICE_SUM_LOW_THRESHOLD} — likely NOT mutually exclusive (spread/correlated)",
        "PRICE_SUM_HIGH":       f"Leg prices sum > {PRICE_SUM_HIGH_THRESHOLD} — missing legs or bad data",
        "ZERO_PROFIT":          "NetProfitPerShare <= 0 — should not be in file",
        "INSTANT_OPEN_CLOSE":   "Duration < 10ms — single-tick glitch",
        "IMPLAUSIBLE_DURATION": "Duration > 1 hour — likely stale book across reconnect",
        "THIN_DEPTH":           f"MaxVolume < {THIN_DEPTH_THRESHOLD} — resting 1-contract order noise",
        "REPEAT_SPAM":          f"Same EventId appears > {spam_threshold}x — cycling open/close",
        "COST_EXCEEDS_1":       "BestNetCost > $1.00 — should have been filtered by strategy",
        "PARTIAL_LEGS_REST":    "REST confirmed > 1.50 YES-ask sum — WS only tracked a subset of legs (not true categorical)",
    }

    total = len(rows)
    any_flags = False
    for flag in all_flag_names:
        count = sum(1 for r in rows if flag in r["flags"])
        if count > 0:
            any_flags = True
            pct = count / total * 100
            print(f"  [{flag:<22}]  {count:4d} rows ({pct:5.1f}%)  — {flag_descriptions[flag]}")

    if not any_flags:
        print("  No fraud flags found.")

    # Detail rows for PRICE_SUM_LOW
    low_sum_rows = [r for r in rows if "PRICE_SUM_LOW" in r["flags"]]
    if low_sum_rows:
        print(f"\n  PRICE_SUM_LOW detail (first 10 unique events):")
        seen = set()
        printed = 0
        for r in low_sum_rows:
            if r["event"] not in seen:
                seen.add(r["event"])
                prices_str = " | ".join(f"${p:.2f}" for p in r["leg_prices"])
                print(f"    {r['event']:<40} sum=${r['leg_sum']:.4f}  legs=[{prices_str}]")
                printed += 1
                if printed >= 10:
                    break

    # Detail rows for INSTANT_OPEN_CLOSE
    instant_rows = [r for r in rows if "INSTANT_OPEN_CLOSE" in r["flags"]]
    if instant_rows:
        print(f"\n  INSTANT_OPEN_CLOSE detail ({len(instant_rows)} rows):")
        for r in instant_rows[:10]:
            print(f"    {r['event']:<40} {r['duration_ms']:.0f}ms  cost=${r['best_net_cost']:.4f}")

    print()

# ─── SECTION 3: DURATION ANALYSIS ────────────────────────────────────────────

def print_duration_analysis(rows, min_duration_ms):
    print("=" * 80)
    print("  DURATION ANALYSIS")
    print("=" * 80)
    if not rows:
        print("  (no data)")
        print()
        return

    buckets = [
        ("< 50ms",       0,       50),
        ("50-200ms",     50,      200),
        ("200-500ms",    200,     500),
        ("500ms-2s",     500,     2000),
        ("2s-10s",       2000,    10000),
        ("10s-60s",      10000,   60000),
        ("> 60s",        60000,   float("inf")),
    ]

    total = len(rows)
    durations = sorted(r["duration_ms"] for r in rows)

    print(f"  {'Bucket':<16} {'Count':>6}  {'%':>6}")
    print(f"  {'-'*16} {'-'*6}  {'-'*6}")
    for label, lo, hi in buckets:
        count = sum(1 for d in durations if lo <= d < hi)
        pct = count / total * 100 if total else 0
        marker = " <-- capturable threshold" if lo == min_duration_ms or (lo < min_duration_ms <= hi) else ""
        print(f"  {label:<16} {count:>6}  {pct:>5.1f}%{marker}")

    capturable = sum(1 for r in rows if r["duration_ms"] >= min_duration_ms)
    median = durations[len(durations) // 2]
    p90    = durations[int(len(durations) * 0.90)]
    maxd   = durations[-1]

    print()
    print(f"  Capturable (>= {min_duration_ms}ms): {capturable} / {total}  ({capturable/total*100:.1f}%)")
    print(f"  Median: {median:.0f}ms   p90: {p90:.0f}ms   Max: {maxd:.0f}ms")
    print()

# ─── SECTION 4: PROFIT ANALYSIS ───────────────────────────────────────────────

def print_profit_analysis(rows, min_duration_ms, capital_per_arb, capture_rate, session_hours):
    print("=" * 80)
    print("  PROFIT ANALYSIS")
    print("=" * 80)
    if not rows:
        print("  (no data)")
        print()
        return

    total_potential = sum(r["total_potential"] for r in rows)
    capturable_rows = [r for r in rows if r["duration_ms"] >= min_duration_ms]
    cap_potential   = sum(r["total_potential"] for r in capturable_rows)

    # Realistic profit: capped capital, capture rate applied
    realistic_profit = 0.0
    for r in capturable_rows:
        if r["total_capital_req"] > 0:
            fill_fraction = min(1.0, capital_per_arb / r["total_capital_req"])
        else:
            fill_fraction = 1.0
        realistic_profit += r["total_potential"] * fill_fraction * capture_rate

    avg_profit_per_window = total_potential / len(rows) if rows else 0
    avg_profit_per_share  = sum(r["net_profit_per_share"] for r in rows) / len(rows) if rows else 0
    best_window = max(rows, key=lambda r: r["total_potential"])

    print(f"  Total potential profit (ALL windows):        {_per_hr(total_potential, session_hours)}")
    print(f"  Total potential profit (capturable windows): {_per_hr(cap_potential,   session_hours)}")
    print(f"  Realistic (capped ${capital_per_arb:.0f}, {capture_rate*100:.0f}% capture):       {_per_hr(realistic_profit, session_hours)}")
    print()
    print(f"  Avg profit / window:  ${avg_profit_per_window:.4f}")
    print(f"  Avg profit / share:   ${avg_profit_per_share:.4f}")
    print(f"  Best single window:   ${best_window['total_potential']:.4f}  ({best_window['event']}  {best_window['duration_ms']:.0f}ms)")
    print()

    # Distribution buckets
    buckets = [
        ("< $0.01",      0,     0.01),
        ("$0.01-0.05",   0.01,  0.05),
        ("$0.05-0.25",   0.05,  0.25),
        ("$0.25-1.00",   0.25,  1.00),
        ("> $1.00",      1.00,  float("inf")),
    ]
    total = len(rows)
    print(f"  Potential profit distribution:")
    print(f"  {'Bucket':<16} {'Count':>6}  {'%':>6}")
    print(f"  {'-'*16} {'-'*6}  {'-'*6}")
    for label, lo, hi in buckets:
        count = sum(1 for r in rows if lo <= r["total_potential"] < hi)
        pct = count / total * 100 if total else 0
        print(f"  {label:<16} {count:>6}  {pct:>5.1f}%")
    print()

# ─── SECTION 5: ALL MARKETS DETECTED ────────────────────────────────────────

def print_all_markets(rows):
    """
    Lists every unique event the telemetry observed, clean and flagged alike.
    Clean events (no flags) are listed first sorted by total potential profit,
    then flagged events sorted the same way.
    """
    print("=" * 80)
    print("  ALL MARKETS DETECTED")
    print("=" * 80)
    if not rows:
        print("  (no data)")
        print()
        return

    by_event = defaultdict(list)
    for r in rows:
        by_event[r["event"]].append(r)

    summary = []
    for event, evrows in by_event.items():
        all_flags = set()
        for r in evrows:
            all_flags.update(r["flags"])
        summary.append({
            "event":        event,
            "legs":         evrows[0]["num_legs"],
            "windows":      len(evrows),
            "avg_duration": sum(r["duration_ms"] for r in evrows) / len(evrows),
            "avg_net_cost": sum(r["best_net_cost"] for r in evrows) / len(evrows),
            "avg_depth":    sum(r["max_volume"] for r in evrows) / len(evrows),
            "total_pot":    sum(r["total_potential"] for r in evrows),
            "flags":        sorted(all_flags),
        })

    clean   = sorted([e for e in summary if not e["flags"]],   key=lambda x: -x["total_pot"])
    flagged = sorted([e for e in summary if     e["flags"]],   key=lambda x: -x["total_pot"])

    print(f"  {len(summary)} total events — {len(clean)} clean, {len(flagged)} flagged\n")

    hdr = f"  {'EventId':<42} {'L':>2} {'Win':>4} {'AvgMs':>7} {'AvgCost':>8} {'TotProfit':>10}  Flags"
    sep = f"  {'-'*42} {'-'*2} {'-'*4} {'-'*7} {'-'*8} {'-'*10}  -----"

    def _row(e):
        flag_str = ",".join(e["flags"]) if e["flags"] else "OK"
        print(f"  {e['event']:<42} {e['legs']:>2} {e['windows']:>4} "
              f"{e['avg_duration']:>7.0f} {e['avg_net_cost']:>8.4f} "
              f"${e['total_pot']:>9.4f}  {flag_str}")

    if clean:
        print(f"  CLEAN ({len(clean)})")
        print(hdr)
        print(sep)
        for e in clean:
            _row(e)
        print()

    if flagged:
        print(f"  FLAGGED ({len(flagged)})")
        print(hdr)
        print(sep)
        for e in flagged:
            _row(e)
    print()

# ─── SECTION 6: REST VERIFICATION ANALYSIS ───────────────────────────────────

def print_rest_verification(rows):
    print("=" * 80)
    print("  REST VERIFICATION ANALYSIS")
    print("=" * 80)

    # Only rows where REST check was attempted
    checked = [r for r in rows if r.get("rest_checked") is True]
    not_checked = [r for r in rows if r.get("rest_checked") is False]
    no_data     = [r for r in rows if r.get("rest_checked") is None]

    total = len(rows)
    print(f"  Total arb windows:          {total}")
    print(f"  REST-checked:               {len(checked)}  ({len(checked)/total*100:.1f}%)")
    print(f"  REST not triggered:         {len(not_checked)}  ({len(not_checked)/total*100:.1f}%)")
    if no_data:
        print(f"  No REST column in CSV:      {len(no_data)}  (older CSV, run bot again)")
    print()

    if not checked:
        print("  No REST-checked windows to analyze.")
        print("  (REST verification fires when a new arb OPENS — if the arb was already open")
        print("   from a previous window, it may not fire again.)")
        print()
        return

    confirmed   = [r for r in checked if r.get("rest_confirmed") is True]
    unconfirmed = [r for r in checked if r.get("rest_confirmed") is False]
    print(f"  REST-confirmed (sum < $1.00): {len(confirmed)}  ({len(confirmed)/len(checked)*100:.1f}% of checked)")
    print(f"  REST-unconfirmed:             {len(unconfirmed)}  ({len(unconfirmed)/len(checked)*100:.1f}% of checked)")
    print()

    # WS vs REST cost comparison for confirmed arbs
    if confirmed:
        deltas = []
        for r in confirmed:
            if r["rest_yes_ask_sum"] is not None and r["rest_yes_ask_sum"] >= 0:
                deltas.append(abs(r["rest_yes_ask_sum"] - r["best_net_cost"]))

        if deltas:
            avg_delta = sum(deltas) / len(deltas)
            max_delta = max(deltas)
            close = sum(1 for d in deltas if d < 0.05)
            print(f"  WS vs REST cost delta (confirmed arbs):")
            print(f"    Avg delta:   ${avg_delta:.4f}")
            print(f"    Max delta:   ${max_delta:.4f}")
            print(f"    Close match (< $0.05):  {close} / {len(deltas)}  ({close/len(deltas)*100:.1f}%)")
            print()

    # REST check delay distribution
    delays = [r["rest_delay_ms"] for r in checked if r.get("rest_delay_ms") is not None and r["rest_delay_ms"] >= 0]
    if delays:
        delays.sort()
        median_d = delays[len(delays) // 2]
        p90_d    = delays[int(len(delays) * 0.90)]
        max_d    = delays[-1]
        print(f"  REST check delay (ms from arb open to verification):")
        print(f"    Median: {median_d:.0f}ms   p90: {p90_d:.0f}ms   Max: {max_d:.0f}ms")

        buckets = [
            ("< 200ms",     0,      200),
            ("200-500ms",   200,    500),
            ("500ms-1s",    500,    1000),
            ("1s-3s",       1000,   3000),
            ("> 3s",        3000,   float("inf")),
        ]
        print(f"  {'Delay bucket':<16} {'Count':>6}  {'%':>6}")
        for label, lo, hi in buckets:
            cnt = sum(1 for d in delays if lo <= d < hi)
            pct = cnt / len(delays) * 100
            print(f"  {label:<16} {cnt:>6}  {pct:>5.1f}%")
    print()

    # Per-event REST confirmation breakdown
    by_event = defaultdict(list)
    for r in checked:
        by_event[r["event"]].append(r)

    if by_event:
        print(f"  Per-event REST results (events with >= 1 checked window):")
        print(f"  {'EventId':<42} {'Chk':>4} {'Conf':>4} {'AvgWSCost':>10} {'AvgRESTSum':>11} {'AvgDelay':>9}")
        print(f"  {'-'*42} {'-'*4} {'-'*4} {'-'*10} {'-'*11} {'-'*9}")
        rows_by_pot = sorted(by_event.items(),
                             key=lambda x: sum(r["total_potential"] for r in x[1]), reverse=True)
        for event, evrows in rows_by_pot[:20]:
            conf_count = sum(1 for r in evrows if r.get("rest_confirmed") is True)
            rest_sums  = [r["rest_yes_ask_sum"] for r in evrows
                          if r["rest_yes_ask_sum"] is not None and r["rest_yes_ask_sum"] >= 0]
            delays_ev  = [r["rest_delay_ms"] for r in evrows
                          if r.get("rest_delay_ms") is not None and r["rest_delay_ms"] >= 0]
            avg_ws   = sum(r["best_net_cost"] for r in evrows) / len(evrows)
            avg_rest = sum(rest_sums) / len(rest_sums) if rest_sums else -1
            avg_dly  = sum(delays_ev) / len(delays_ev) if delays_ev else -1
            rest_str = f"${avg_rest:.4f}" if avg_rest >= 0 else "  N/A  "
            dly_str  = f"{avg_dly:.0f}ms" if avg_dly >= 0 else "  N/A"
            print(f"  {event:<42} {len(evrows):>4} {conf_count:>4} ${avg_ws:>8.4f} {rest_str:>11} {dly_str:>9}")
    print()


# ─── SECTION 9+: KALSHI API RESOLUTION ───────────────────────────────────────

KALSHI_API_BASE = "https://api.elections.kalshi.com/trade-api/v2"


def _resolution_cache_path(csv_path):
    return os.path.splitext(csv_path)[0] + "_resolution_cache.json"


def _load_resolution_cache(csv_path):
    try:
        p = _resolution_cache_path(csv_path)
        if os.path.exists(p):
            with open(p) as f:
                return json.load(f)
    except Exception:
        pass
    return {}


def _save_resolution_cache(csv_path, cache):
    try:
        with open(_resolution_cache_path(csv_path), "w") as f:
            json.dump(cache, f, indent=2)
    except Exception:
        pass


def _fetch_event_resolution(event_ticker, session):
    """
    GET /events/{ticker}?with_nested_markets=true
    Returns a normalised dict:
      status         : "win" | "loss" | "active" | "not_found" | "error"
      winning_ticker : ticker of the YES leg (win only)
      winning_title  : yes_sub_title of the YES leg (win only)
      all_resolved   : True when every leg is finalized
      markets        : [{ticker, status, result, yes_sub_title}]
    """
    url = f"{KALSHI_API_BASE}/events/{event_ticker}?with_nested_markets=true"
    try:
        resp = session.get(url, timeout=8)
        if resp.status_code == 404:
            return {"status": "not_found", "all_resolved": False, "markets": []}
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        return {"status": "error", "error": str(e), "all_resolved": False, "markets": []}

    event_data  = data.get("event", data)
    markets_raw = event_data.get("markets", [])
    if not markets_raw:
        return {"status": "not_found", "all_resolved": False, "markets": []}

    mkt_list = [
        {
            "ticker":        m.get("ticker", ""),
            "status":        (m.get("status")  or "").lower(),
            "result":        (m.get("result")   or "").lower(),
            "yes_sub_title": m.get("yes_sub_title", ""),
        }
        for m in markets_raw
    ]

    yes_legs   = [m for m in mkt_list if m["result"] == "yes"]
    all_final  = all(m["status"] == "finalized" for m in mkt_list)
    any_active = any(m["status"] == "active"    for m in mkt_list)

    if yes_legs:
        return {
            "status":         "win",
            "winning_ticker": yes_legs[0]["ticker"],
            "winning_title":  yes_legs[0]["yes_sub_title"],
            "all_resolved":   all_final,
            "markets":        mkt_list,
        }
    elif all_final:
        # All finalized but no YES → every leg resolved NO (spread zero)
        return {
            "status":       "loss",
            "all_resolved": True,
            "markets":      mkt_list,
        }
    elif any_active:
        return {"status": "active",  "all_resolved": False, "markets": mkt_list}
    else:
        return {"status": "unknown", "all_resolved": False, "markets": mkt_list}


def _build_entries(rows, capital_per_arb, min_duration_ms):
    """One entry per unique event at the best (lowest cost) capturable window."""
    by_event = defaultdict(list)
    for r in rows:
        by_event[r["event"]].append(r)

    entries = []
    for event, evrows in by_event.items():
        pool = [r for r in evrows if r["duration_ms"] >= min_duration_ms] or evrows
        best = min(pool, key=lambda r: r["best_net_cost"])
        capital  = min(best["total_capital_req"], capital_per_arb) \
                   if best["total_capital_req"] > 0 else capital_per_arb
        shares   = capital / best["best_net_cost"] if best["best_net_cost"] > 0 else 0
        win_pnl  = shares * best["net_profit_per_share"]
        entries.append({
            "event":       event,
            "windows":     len(evrows),
            "capital":     capital,
            "best_cost":   best["best_net_cost"],
            "win_pnl":     win_pnl,
            "lose_pnl":    -capital,
            "spread_risk": _is_spread_market(event),
            "flags":       set().union(*[set(r["flags"]) for r in evrows]),
        })
    return entries


# ─── SECTION 7: ACTUAL RESOLUTION (Kalshi API) ──────────────────────────────

# Event ticker substrings that indicate sports spread/total markets.
# These are NOT true categoricals: all legs can simultaneously resolve $0
# when the game result falls outside every listed threshold (e.g. a 1-goal
# win when every leg is "wins by 1.5+" or "wins by 2.5+").
STRUCTURAL_ZERO_PATTERNS = ("SPREAD", "TOTAL")

def _is_spread_market(event_id):
    return any(p in event_id.upper() for p in STRUCTURAL_ZERO_PATTERNS)


def _is_blocked(event_id, full_blocklist):
    """Return True if this event's series prefix is in the blocklist."""
    series = _event_series(event_id)
    return any(
        series.upper().startswith(b.upper())
        for b in full_blocklist
    )



def print_actual_resolution(rows, capital_per_arb, min_duration_ms, session_hours, csv_path):
    """
    Fetches the real resolution status of every event from the Kalshi public API.
    Caches fully-resolved events so re-runs don't re-fetch them.

    Shows per-event: capital deployed, actual outcome (WIN/LOSS/ACTIVE), real P&L.
    Summarises spread vs categorical performance separately.
    """
    print("=" * 80)
    print("  ACTUAL RESOLUTION  (Kalshi API — WIN / LOSS / ACTIVE per event)")
    print("=" * 80)

    try:
        import requests
        session = requests.Session()
        session.headers["User-Agent"] = "kalshi-arb-analyzer/1.0"
    except ImportError:
        print("  requests library not installed.  Run: pip install requests")
        print()
        return

    if not rows:
        print("  (no data)")
        print()
        return

    entries = _build_entries(rows, capital_per_arb, min_duration_ms)
    cache   = _load_resolution_cache(csv_path)

    print(f"  Fetching resolution for {len(entries)} events "
          f"({sum(1 for e in entries if cache.get(e['event'], {}).get('all_resolved'))} cached)...")

    resolved     = {}
    cache_dirty  = False
    for i, e in enumerate(entries):
        eid    = e["event"]
        cached = cache.get(eid)
        if cached and cached.get("all_resolved"):
            resolved[eid] = cached
        else:
            if i > 0:
                time.sleep(0.12)            # ~8 req/s — stay well under rate limits
            res = _fetch_event_resolution(eid, session)
            resolved[eid]   = res
            cache[eid]      = res
            cache_dirty     = True
        status_char = {"win": "W", "loss": "L", "active": ".",
                       "not_found": "?", "error": "!"}.get(
                       resolved[eid].get("status", "?"), "?")
        print(f"\r  [{i+1:2d}/{len(entries)}] {status_char}", end="", flush=True)

    if cache_dirty:
        _save_resolution_cache(csv_path, cache)
    print(f"\r  {'Done.':<60}")
    print()

    # ── Per-event table ────────────────────────────────────────────────────────
    win_events   = []
    loss_events  = []
    open_events  = []

    print(f"  {'EventId':<42} {'Capital':>8} {'Result':<14} {'ActualPnL':>10}  Note")
    print(f"  {'-'*42} {'-'*8} {'-'*14} {'-'*10}  ----")

    for e in sorted(entries, key=lambda x: x["capital"], reverse=True):
        res    = resolved.get(e["event"], {})
        status = res.get("status", "unknown")

        if status == "win":
            actual_pnl = e["win_pnl"]
            result_str = "WIN"
            pnl_str    = f"${actual_pnl:+.2f}"
            note       = (res.get("winning_title") or res.get("winning_ticker", ""))[:38]
            win_events.append(e)
        elif status == "loss":
            actual_pnl = e["lose_pnl"]
            result_str = "LOSS (all->$0)"
            pnl_str    = f"${actual_pnl:+.2f}"
            note       = "all legs resolved NO"
            loss_events.append(e)
        elif status == "active":
            actual_pnl = None
            result_str = "ACTIVE"
            pnl_str    = f"(proj ${e['win_pnl']:+.2f})"
            note       = "still trading"
            open_events.append(e)
        elif status == "not_found":
            actual_pnl = None
            result_str = "NOT FOUND"
            pnl_str    = "--"
            note       = ""
            open_events.append(e)
        else:
            actual_pnl = None
            result_str = status.upper()[:14]
            pnl_str    = "--"
            note       = (res.get("error") or "")[:38]
            open_events.append(e)

        print(f"  {e['event']:<42} ${e['capital']:>7.2f} {result_str:<14} {pnl_str:>10}  {note}")

    print()

    # ── Summary ────────────────────────────────────────────────────────────────
    wins_pnl  = sum(e["win_pnl"]  for e in win_events)
    loss_pnl  = sum(e["lose_pnl"] for e in loss_events)
    open_proj = sum(e["win_pnl"]  for e in open_events)
    net_resolved = wins_pnl + loss_pnl

    print("  RESOLUTION SUMMARY:")
    print(f"    Won  (one leg YES):     {len(win_events):2d} events   ${wins_pnl:+.2f}")
    print(f"    Lost (all legs NO):     {len(loss_events):2d} events   ${loss_pnl:+.2f}")
    print(f"    Active / unresolved:    {len(open_events):2d} events   "
          f"(projected ${open_proj:+.2f} if all win)")
    print()
    print(f"    ACTUAL NET P&L (resolved):  ${net_resolved:+.2f}  "
          f"({_per_hr(net_resolved, session_hours)})")
    if open_events:
        best_case = net_resolved + open_proj
        print(f"    BEST CASE (open all win):   ${best_case:+.2f}")
    print()

    # ── Spread vs categorical breakdown ───────────────────────────────────────
    def _group(ev_list):
        sp = [e for e in ev_list if e["spread_risk"]]
        cl = [e for e in ev_list if not e["spread_risk"]]
        return sp, cl

    sp_win,  cl_win  = _group(win_events)
    sp_loss, cl_loss = _group(loss_events)
    sp_open, cl_open = _group(open_events)

    sp_net = sum(e["win_pnl"]  for e in sp_win) + sum(e["lose_pnl"] for e in sp_loss)
    cl_net = sum(e["win_pnl"]  for e in cl_win) + sum(e["lose_pnl"] for e in cl_loss)
    sp_cnt = len(sp_win) + len(sp_loss) + len(sp_open)
    cl_cnt = len(cl_win) + len(cl_loss) + len(cl_open)

    print(f"  SPREAD / TOTAL MARKETS  ({sp_cnt} total):")
    print(f"    Won: {len(sp_win)}   Lost: {len(sp_loss)}   Open: {len(sp_open)}")
    if sp_win or sp_loss:
        print(f"    Net on resolved: ${sp_net:+.2f}")
    print()
    print(f"  CLEAN CATEGORICAL MARKETS  ({cl_cnt} total):")
    print(f"    Won: {len(cl_win)}   Lost: {len(cl_loss)}   Open: {len(cl_open)}")
    if cl_win or cl_loss:
        print(f"    Net on resolved: ${cl_net:+.2f}")
    print()


# ─── PRODUCTION SIM ──────────────────────────────────────────────────────────

def _duration_participation(duration_ms):
    """
    Duration-tiered participation rate.
    A window that stays open for a long time implies low competition —
    no aggressive arb desk would leave free money uncaptured for 60+ seconds.
    Short windows suggest others are racing; long windows suggest you're alone.
    """
    if duration_ms <   500: return 0.15   # < 0.5s  — fast close, others racing
    if duration_ms <  2000: return 0.30   # 0.5–2s  — moderate competition
    if duration_ms < 60000: return 0.60   # 2–60s   — slow market, low competition
    return 0.85                            # > 60s   — likely uncontested


def _sim_entries(by_event, participation_rate):
    """
    Build per-event entry list.
    If participation_rate is None, use duration-tiered rates per window.
    Otherwise apply a flat rate to all entries.
    """
    entries = []
    for event, evrows in by_event.items():
        pool  = [r for r in evrows if r["duration_ms"] >= PROD_LATENCY_MS] or evrows
        best  = min(pool, key=lambda r: r["best_net_cost"])
        cap_w = len([r for r in evrows if r["duration_ms"] >= PROD_LATENCY_MS])
        rate  = (participation_rate if participation_rate is not None
                 else _duration_participation(best["duration_ms"]))
        capital = best["total_capital_req"] * rate
        shares  = capital / best["best_net_cost"] if best["best_net_cost"] > 0 else 0
        entries.append({
            "event":    event,
            "cap_wins": cap_w,
            "capital":  capital,
            "cost":     best["best_net_cost"],
            "rate":     rate,
            "win_pnl":  shares * best["net_profit_per_share"],
            "lose_pnl": -capital,
        })
    entries.sort(key=lambda x: x["win_pnl"], reverse=True)
    return entries


def print_production_sim(rows, session_hours, csv_path, participation_rate=None):
    """
    Section 11 — Production Sim.

    Models the bot running from a US server near the Kalshi datacenter:
      • Min capturable window  : 17ms  (6ms one-way · measured JFK50-P1)
      • Participation rate     : fraction of available depth the bot captures.
                                 Default (None) = duration-tiered: long windows imply
                                 low competition; short windows imply others are racing.
      • Entry model            : one entry per event at the best capturable window
      • Scope                  : clean categorical events only (blocklist + SPREAD/TOTAL excluded)

    P&L shown at the tiered rate (default) or the specified flat rate, plus a
    competition sensitivity table across flat-rate scenarios.
    """
    print("=" * 80)
    print("  PRODUCTION SIM  (US server · competition-adjusted · clean categoricals)")
    print("=" * 80)
    # ── Load blocklist (hardcoded + learned) ─────────────────────────────────
    dynamic   = _load_blocklist(csv_path) if csv_path else set()
    blocklist = HARDCODED_BLOCKED | dynamic

    print(f"  Latency model : US server JFK50-P1  (~6ms one-way, {PROD_LATENCY_MS}ms min window · measured)")
    if participation_rate is None:
        print(f"  Participation : duration-tiered  (<0.5s=15%  0.5-2s=30%  2-60s=60%  >60s=85%)")
        print(f"                  (long windows imply low competition; latency already filters fast closes)")
    else:
        print(f"  Participation : {participation_rate*100:.0f}% flat  (override via --participation-rate)")
    print(f"  Entry model   : one entry per event at best (lowest-cost) capturable window")
    print(f"  Scope         : clean categoricals — non-exhaustive series excluded")
    print(f"  Blocklist     : {len(HARDCODED_BLOCKED)} hardcoded + {len(blocklist) - len(HARDCODED_BLOCKED)} learned  ({', '.join(sorted(HARDCODED_BLOCKED)[:3])}...)")
    if blocklist:
        print(f"  Blocklist     : {', '.join(sorted(blocklist))}")
    print()

    # ── Build clean categorical rows (blocklist + spread filter) ─────────────
    clean_cat = [r for r in rows
                 if not r["flags"]
                 and not _is_spread_market(r["event"])
                 and not _is_blocked(r["event"], blocklist)]

    spread_removed  = sum(1 for r in rows if _is_spread_market(r["event"]))
    blocked_removed = sum(1 for r in rows if _is_blocked(r["event"], blocklist))
    flagged_removed = sum(1 for r in rows if r["flags"])
    print(f"  Rows excluded : {flagged_removed} flagged, {spread_removed} SPREAD/TOTAL, {blocked_removed} blocklisted")
    print()

    if not clean_cat:
        print("  No clean categorical rows remain after all filters.")
        print()
        return

    by_event = defaultdict(list)
    for r in clean_cat:
        by_event[r["event"]].append(r)

    entries = _sim_entries(by_event, participation_rate)

    total_capital = sum(e["capital"] for e in entries)
    total_proj    = sum(e["win_pnl"] for e in entries)

    # Multi-entry: every capturable clean window, each at its own participation rate
    def _multi(rate_or_none):
        total = 0.0
        for r in clean_cat:
            if r["duration_ms"] < PROD_LATENCY_MS:
                continue
            rate = (_duration_participation(r["duration_ms"]) if rate_or_none is None
                    else rate_or_none)
            cap = r["total_capital_req"] * rate
            if r["best_net_cost"] > 0:
                total += (cap / r["best_net_cost"]) * r["net_profit_per_share"]
        return total

    multi_proj = _multi(participation_rate)

    # ── Load resolution cache ─────────────────────────────────────────────────
    cache = _load_resolution_cache(csv_path) if csv_path else {}

    # ── Per-event table ────────────────────────────────────────────────────────
    rate_hdr = "Part%" if participation_rate is None else f"{participation_rate*100:.0f}%"
    print(f"  {'EventId':<42} {rate_hdr:>5} {'Capital':>8} {'ProjProfit':>10} {'Settlement':>12}  ActualPnL")
    print(f"  {'-'*42} {'-'*5} {'-'*8} {'-'*10} {'-'*12}  ---------")

    settled_win_pnl  = 0.0
    settled_loss_pnl = 0.0
    open_proj_pnl    = 0.0
    win_count = loss_count = open_count = 0

    for e in entries:
        res    = cache.get(e["event"], {})
        status = res.get("status", "unknown")

        if status == "win":
            settle_str  = "WIN"
            actual_str  = f"${e['win_pnl']:+.2f}"
            settled_win_pnl += e["win_pnl"]
            win_count += 1
        elif status == "loss":
            settle_str  = "LOSS (all->$0)"
            actual_str  = f"${e['lose_pnl']:+.2f}"
            settled_loss_pnl += e["lose_pnl"]
            loss_count += 1
        elif not cache:
            settle_str  = "no cache"
            actual_str  = "--"
            open_proj_pnl += e["win_pnl"]
            open_count += 1
        else:
            settle_str  = "ACTIVE"
            actual_str  = f"(proj ${e['win_pnl']:+.2f})"
            open_proj_pnl += e["win_pnl"]
            open_count += 1

        rate_str = f"{e['rate']*100:.0f}%" if participation_rate is None else ""
        print(f"  {e['event']:<42} {rate_str:>5} ${e['capital']:>7.2f} ${e['win_pnl']:>+9.2f} {settle_str:>12}  {actual_str}")

    print()

    net_settled = settled_win_pnl + settled_loss_pnl
    best_case   = net_settled + open_proj_pnl

    rate_label = "duration-tiered" if participation_rate is None else f"{participation_rate*100:.0f}% flat"
    print(f"  Events:             {len(entries)}  ({win_count} won, {loss_count} lost, {open_count} open/active)")
    print(f"  Total capital used: ${total_capital:.2f}  ({rate_label} participation)")
    print()
    print(f"  PROJECTED P&L   single entry  :  {_per_hr(total_proj,  session_hours)}")
    print(f"  PROJECTED P&L   multi-entry   :  {_per_hr(multi_proj,  session_hours)}")
    print()
    if win_count + loss_count > 0:
        # Settled multi-entry: sum all wins/losses across every capturable window
        # (not just the best-window entry) scaled by participation rate
        settled_multi = sum(
            (_duration_participation(r["duration_ms"]) if participation_rate is None
             else participation_rate)
            * r["total_capital_req"] / r["best_net_cost"] * r["net_profit_per_share"]
            if cache.get(r["event"], {}).get("status") == "win" and r["best_net_cost"] > 0
            else (
                -(_duration_participation(r["duration_ms"]) if participation_rate is None
                  else participation_rate) * r["total_capital_req"]
                if cache.get(r["event"], {}).get("status") == "loss" else 0
            )
            for r in clean_cat
            if r["duration_ms"] >= PROD_LATENCY_MS
        )
        print(f"  SETTLED P&L     single entry  ({win_count}W/{loss_count}L):  {_per_hr(net_settled,    session_hours)}")
        print(f"  SETTLED P&L     multi-entry   ({win_count}W/{loss_count}L):  {_per_hr(settled_multi,  session_hours)}")
    if open_count > 0:
        # Multi-entry open projection = multi_proj minus already-settled windows
        multi_settled_proj = sum(
            ((_duration_participation(r["duration_ms"]) if participation_rate is None
              else participation_rate)
             * r["total_capital_req"] / r["best_net_cost"] * r["net_profit_per_share"])
            if cache.get(r["event"], {}).get("status") in ("win", "loss") and r["best_net_cost"] > 0
            else 0
            for r in clean_cat if r["duration_ms"] >= PROD_LATENCY_MS
        )
        multi_best_case = (settled_multi if win_count + loss_count > 0 else 0) + (multi_proj - multi_settled_proj)
        print(f"  BEST CASE       single entry (settled + all open win):  {_per_hr(best_case,       session_hours)}")
        print(f"  BEST CASE       multi-entry  (settled + all open win):  {_per_hr(multi_best_case, session_hours)}")
    print()
    if not cache:
        print("  Tip: run without --no-api to fetch resolution data and fill in settled P&L.")

    # ── Competition sensitivity table ─────────────────────────────────────────
    print()
    capturable_clean = [r for r in clean_cat if r["duration_ms"] >= PROD_LATENCY_MS]
    print(f"  COMPETITION SENSITIVITY  ({len(entries)} events · {len(capturable_clean)} capturable windows)")
    print(f"  {'Model':<16}  {'Assumption':<28}  {'1x Capital':>10}  {'1x /hr':>9}  {'Multi /hr':>10}  {'Settled 1x':>10}  {'Settled Mx':>10}")
    print(f"  {'-'*16}  {'-'*28}  {'-'*10}  {'-'*9}  {'-'*10}  {'-'*10}  {'-'*10}")

    def _row(rate_or_none, label, marker=""):
        sc_entries  = _sim_entries(by_event, rate_or_none)
        sc_capital  = sum(e["capital"] for e in sc_entries)
        sc_1x       = sum(e["win_pnl"]  for e in sc_entries)
        sc_multi    = _multi(rate_or_none)
        sc_net_1x   = sum(
            e["win_pnl"]  for e in sc_entries if cache.get(e["event"], {}).get("status") == "win"
        ) + sum(
            e["lose_pnl"] for e in sc_entries if cache.get(e["event"], {}).get("status") == "loss"
        )
        sc_net_multi = sum(
            ((_duration_participation(r["duration_ms"]) if rate_or_none is None else rate_or_none)
             * r["total_capital_req"] / r["best_net_cost"] * r["net_profit_per_share"])
            if cache.get(r["event"], {}).get("status") == "win" and r["best_net_cost"] > 0
            else (
                -((_duration_participation(r["duration_ms"]) if rate_or_none is None else rate_or_none)
                  * r["total_capital_req"])
                if cache.get(r["event"], {}).get("status") == "loss" else 0
            )
            for r in capturable_clean
        )
        model_str    = "duration-tiered" if rate_or_none is None else f"flat {rate_or_none*100:.0f}%"
        settled_1x_s = _hr(sc_net_1x,   session_hours) if (win_count + loss_count > 0) else "n/a"
        settled_mx_s = _hr(sc_net_multi, session_hours) if (win_count + loss_count > 0) else "n/a"
        print(f"  {model_str:<16}  {label:<28}  ${sc_capital:>9.2f}  {_hr(sc_1x, session_hours):>9}  "
              f"{_hr(sc_multi, session_hours):>10}  {settled_1x_s:>10}  {settled_mx_s:>10}{marker}")

    _row(None,  "tiered by duration (default)", " <--" if participation_rate is None else "")
    _row(1.00,  "sole actor / no competition",  " <--" if participation_rate == 1.0  else "")
    _row(0.50,  "1 competitor  (~2 desks)",     " <--" if participation_rate == 0.5  else "")
    _row(0.25,  "3 competitors (~4 desks)",     " <--" if participation_rate == 0.25 else "")
    _row(0.10,  "9 competitors (~10 desks)",    " <--" if participation_rate == 0.10 else "")

    print()
    print(f"  Note: multi-entry = every capturable re-entry per event at the same participation rate.")
    print(f"  Window duration is a proxy for competition — long windows imply fewer competing desks.")
    print()


# ─── SERIES LOSS TRACKER ─────────────────────────────────────────────────────

BLOCKLIST_FILENAME = "event_blocklist.json"

# Hardcoded series that are always blocked regardless of the learned file.
# These are kept here so the blocklist file only needs to track *new* discoveries.
HARDCODED_BLOCKED = {
    "KXUFCVICROUND",       # UFC/boxing round: fight-to-decision → all legs $0
    "KXMLBHR",             # MLB HR by inning: no HR hit → all legs $0
    "KXHEGSETHANNOUNCEOUT",# "Before date" series: Hegseth may never leave
    "KXIMPEACH",           # "Will X be impeached?": conditional, may never happen
    "KXJUULFLAVOR",        # "Will Juul offer flavor X?": flavors may not launch
    "KXMLBSTATCOUNT",      # MLB stat cumulative counts: stat may never reach threshold
}


def _event_series(event_id):
    """
    Extract the series prefix from an event ticker by stripping the trailing
    date/matchup slug (everything from the first hyphen-digit pattern onward).
      "KXUFCVICROUND-26APR11HOLBRO" -> "KXUFCVICROUND"
      "KXMLSGAME-26APR11DALSTL"     -> "KXMLSGAME"
      "KXIMPEACH"                   -> "KXIMPEACH"
    """
    return re.split(r'-\d', event_id)[0]


def _blocklist_path(csv_path):
    """Place event_blocklist.json alongside the CSV (or cwd if no path)."""
    folder = os.path.dirname(os.path.abspath(csv_path)) if csv_path else "."
    return os.path.join(folder, BLOCKLIST_FILENAME)


def _load_blocklist(csv_path):
    """Load the current learned blocklist, return a set of series prefixes."""
    path = _blocklist_path(csv_path)
    if not os.path.exists(path):
        return set()
    try:
        with open(path) as f:
            data = json.load(f)
        entries = data if isinstance(data, list) else data.get("blocked", [])
        return set(entries)
    except Exception:
        return set()


def _save_blocklist(csv_path, blocked_series):
    """Write the full merged blocklist back to disk."""
    path = _blocklist_path(csv_path)
    try:
        with open(path, "w") as f:
            json.dump(sorted(blocked_series), f, indent=2)
    except Exception as e:
        print(f"  Warning: could not write blocklist: {e}")


def print_series_loss_tracker(csv_path):
    """
    Groups resolution cache outcomes by event-series prefix.

    Any series with at least one all-NO resolution (LOSS) is automatically
    written to event_blocklist.json so the C# scanner blocks it on the next
    startup — no code change or rebuild required.
    """
    print("=" * 80)
    print("  SERIES LOSS TRACKER  (auto-updates event_blocklist.json)")
    print("=" * 80)

    cache = _load_resolution_cache(csv_path) if csv_path else {}
    if not cache:
        print("  No resolution cache — run without --no-api first.")
        print()
        return

    by_series = defaultdict(lambda: {"wins": 0, "losses": 0, "active": 0, "events": []})
    for event_id, res in cache.items():
        s = _event_series(event_id)
        status = res.get("status", "unknown")
        by_series[s]["events"].append(event_id)
        if status == "win":
            by_series[s]["wins"] += 1
        elif status == "loss":
            by_series[s]["losses"] += 1
        else:
            by_series[s]["active"] += 1

    losers  = {s: d for s, d in by_series.items() if d["losses"] > 0}
    clean   = {s: d for s, d in by_series.items() if d["losses"] == 0 and d["wins"] > 0}
    unknown = {s: d for s, d in by_series.items() if d["losses"] == 0 and d["wins"] == 0}

    # ── Auto-update the blocklist file ────────────────────────────────────────
    existing   = _load_blocklist(csv_path)
    # Exclude hardcoded ones — they're handled in C# code directly
    learned    = existing - HARDCODED_BLOCKED
    new_losers = {s for s in losers if s not in HARDCODED_BLOCKED and s not in learned}
    merged     = learned | {s for s in losers if s not in HARDCODED_BLOCKED}

    if merged != learned:
        _save_blocklist(csv_path, merged)
        print(f"  event_blocklist.json updated: {len(merged)} series blocked "
              f"(+{len(new_losers)} new).")
    else:
        print(f"  event_blocklist.json unchanged: {len(merged)} series blocked.")
    print()

    # ── Report ────────────────────────────────────────────────────────────────
    if losers:
        print("  *** SERIES WITH ALL-NO RESOLUTIONS (auto-blocked):")
        print(f"  {'Series':<35} {'W':>4} {'L':>4} {'Open':>6}  Status")
        print(f"  {'-'*35} {'-'*4} {'-'*4} {'-'*6}  ------")
        for s, d in sorted(losers.items(), key=lambda x: -x[1]["losses"]):
            tag = "hardcoded" if s in HARDCODED_BLOCKED else ("new" if s in new_losers else "known")
            print(f"  {s:<35} {d['wins']:>4} {d['losses']:>4} {d['active']:>6}  {tag}")
        print()
    else:
        print("  No series losses detected.")
        print()

    if clean:
        print(f"  Confirmed clean series ({len(clean)} types, "
              f"{sum(d['wins'] for d in clean.values())} wins, 0 losses):")
        for s, d in sorted(clean.items(), key=lambda x: -x[1]["wins"]):
            print(f"    {s:<35}  {d['wins']}W  {d['active']} open")
        print()

    if unknown:
        print(f"  Unresolved (active only): {', '.join(sorted(unknown.keys()))}")
        print()


# ─── MAIN ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Analyze Kalshi ArbTelemetry CSV")
    parser.add_argument("--file", default=None, help="Path to CSV (auto-discovers if omitted)")
    parser.add_argument("--min-duration", type=int, default=DEFAULT_MIN_DURATION_MS,
                        help=f"Min ms to count as capturable (default {DEFAULT_MIN_DURATION_MS})")
    parser.add_argument("--spam-threshold", type=int, default=REPEAT_SPAM_THRESHOLD,
                        help=f"Windows per event above which REPEAT_SPAM fires (default {REPEAT_SPAM_THRESHOLD})")
    parser.add_argument("--exclude", default="", help="Comma-separated EventIds to exclude")
    parser.add_argument("--clean", action="store_true", help="Only analyze rows with no fraud flags")
    parser.add_argument("--no-api", action="store_true",
                        help="Skip Kalshi API resolution check (Section 10)")
    parser.add_argument("--participation-rate", type=float, default=None,
                        help="Flat participation rate 0.0-1.0 (default: duration-tiered model)")
    args = parser.parse_args()

    path = args.file or find_latest_csv()
    if not path:
        print("ERROR: No ArbTelemetry_*.csv found. Run the Kalshi paper trader first.")
        sys.exit(1)

    rows = load_csv(path)
    if not rows:
        print(f"ERROR: No valid rows loaded from {path}")
        sys.exit(1)

    # Apply exclusions
    excluded = {e.strip() for e in args.exclude.split(",") if e.strip()}
    if excluded:
        rows = [r for r in rows if r["event"] not in excluded]
        print(f"Excluded {len(excluded)} event(s): {', '.join(excluded)}\n")

    # Compute session duration before flagging (uses raw row timestamps)
    session_hours = compute_session_hours(rows, path)

    # Compute flags on full dataset first (REPEAT_SPAM needs full counts)
    compute_flags(rows, spam_threshold=args.spam_threshold)

    # Apply --clean filter after flagging
    analysis_rows = [r for r in rows if not r["flags"]] if args.clean else rows
    if args.clean:
        print(f"[--clean] Analyzing {len(analysis_rows)} / {len(rows)} rows (no fraud flags)\n")

    # ── Analysis sections ─────────────────────────────────────────────────────
    # Fraud report always runs on the full (pre-clean-filter) dataset so you see
    # what was flagged even when --clean is active.
    print_session_summary(analysis_rows, path, session_hours)
    print_all_markets(rows)                                           # ALL events: clean + flagged
    print_fraud_checks(rows, spam_threshold=args.spam_threshold)
    print_duration_analysis(analysis_rows, args.min_duration)
    print_profit_analysis(analysis_rows, args.min_duration, DEFAULT_CAPITAL_PER_ARB, DEFAULT_CAPTURE_RATE, session_hours)
    print_rest_verification(rows)
    if not args.no_api:
        print_actual_resolution(rows, DEFAULT_CAPITAL_PER_ARB, args.min_duration, session_hours, path)
    print_series_loss_tracker(path)
    print_production_sim(rows, session_hours, path,                   # LAST — most important
                         participation_rate=args.participation_rate)

if __name__ == "__main__":
    main()
