"""pair_kept_report.py - audit of the pair-file rows the pairing run KEPT (2026-11-12). Read-only.

Since 2026-11-12 a paired market whose Pinnacle matchup has left the board (an ITF match past its listed start,
waiting for court time) keeps its pairing instead of being emptied, so the bot keeps asking and the sidecar's
in-play redirect can serve the live price the moment play starts. Every pairing run logs each kept or released
row, with what the sidecar held for it, to HardVenArb/pair_kept.jsonl. This report checks that keeping them was
RIGHT:

  EPISODES   each market's run of consecutive kept records: how long it was kept, how far past its listed start,
             how it ended (re-pointed at the live matchup by a later run / released by the age cap / still kept).
  PRICED     did the bot get IN-PLAY prices for it while it was kept (the redirect working), from when.
  CORRECT    on those in-play rows, does Pinnacle's P_true track the Kalshi ask for the SAME side? A wrong match
             or a flipped side shows up as P_true tracking 1 - ask, or the two simply disagreeing. Compared with
             the same numbers for every other in-play row of the period (the baseline).
  FILLS      every fill on a kept market while it was kept, with its 5-minute cushion and result.

    python pair_kept_report.py                     # everything logged
    python pair_kept_report.py --since 2026-11-12  # from a date (UTC)
    python pair_kept_report.py --line              # one summary line (for Discord)
"""
from __future__ import annotations

import argparse
import collections
import csv
import datetime as dt
import glob
import json
import os
import statistics as st
import sys

R = os.path.dirname(os.path.abspath(__file__))
LOG = os.environ.get("PAIR_KEPT_LOG") or os.path.join(R, "HardVenArb", "pair_kept.jsonl")   # env: test copies
MIN_ROWS = 10           # in-play rows needed before judging a market's agreement
WRONG_SIDE_MARGIN = 0.10  # 1-P_true beats P_true at matching the ask by this much -> flag
DISAGREE = 0.15         # mean |P_true - ask| above this -> flag


def ts(s: str) -> dt.datetime:
    return dt.datetime.fromisoformat((s or "")[:19])


def load_log(since: dt.datetime | None) -> list[dict]:
    out = []
    if not os.path.exists(LOG):
        return out
    for line in open(LOG, encoding="utf-8"):
        try:
            j = json.loads(line)
        except ValueError:
            continue
        if since and ts(j.get("at", "")) < since:
            continue
        out.append(j)
    return out


def episodes(recs: list[dict]) -> list[dict]:
    """Consecutive records of one market with the same kept_since = one episode."""
    by = collections.OrderedDict()
    for j in sorted(recs, key=lambda r: r.get("at", "")):
        k = (j.get("ticker"), j.get("kept_since") or j.get("at"))
        e = by.setdefault(k, {"ticker": j.get("ticker"), "token": j.get("token"), "start": j.get("start"),
                              "kept_since": ts(j.get("kept_since") or j.get("at")), "first": ts(j["at"]),
                              "last": ts(j["at"]), "runs": 0, "max_past": 0.0, "end": "still kept",
                              "evidence": []})
        e["last"] = ts(j["at"])
        e["runs"] += 1
        e["max_past"] = max(e["max_past"], float(j.get("hours_past_start") or 0))
        if j.get("decision") == "released":
            e["end"] = "released: " + (j.get("reason") or "")
        if j.get("evidence"):
            e["evidence"].append(j["evidence"])
        if j.get("names_from"):
            e["names_from"] = j["names_from"]          # the latest run's answer
        if str(j.get("final") or "").startswith("cleared"):
            e["cleared_by_check"] = True
    return list(by.values())


def repointed_at(ticker: str, after: dt.datetime, ledger: dict) -> dt.datetime | None:
    for at, tok in ledger.get(ticker, []):
        if at > after:
            return at
    return None


def main() -> None:
    # A Windows console or pipe is cp1252, and the warning line carries a "⚠": without this the ONE line that
    # matters (a flagged kept row) would crash the report and vanish from the Discord post.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--since", default="", help="UTC date or datetime (default: everything logged)")
    ap.add_argument("--hours", type=float, default=0, help="only the last N hours (wins over --since)")
    ap.add_argument("--line", action="store_true", help="one summary line")
    a = ap.parse_args()
    if a.hours > 0:
        since = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) - dt.timedelta(hours=a.hours)
        scope = f" (last {a.hours:g}h)"
    else:
        since = ts(a.since) if a.since else None
        scope = f" since {a.since}" if a.since else ""

    recs = load_log(since)
    if not recs:
        print(f"kept pairs{scope}: none")
        return
    eps = episodes(recs)
    now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)

    ledger = collections.defaultdict(list)
    for line in open(os.path.join(R, "pair_ledger.jsonl"), encoding="utf-8"):
        try:
            j = json.loads(line)
        except ValueError:
            continue
        ledger[j.get("ticker")].append((dt.datetime.fromtimestamp(j["at"], dt.timezone.utc).replace(tzinfo=None), j.get("yes_token")))
    for v in ledger.values():
        v.sort()
    for e in eps:
        e["repointed"] = repointed_at(e["ticker"], e["kept_since"], ledger)
        if e["repointed"] and e["end"] == "still kept":
            e["end"] = f"re-pointed {e['repointed']:%m-%d %H:%M}"
        e["until"] = e["repointed"] or (e["last"] if e["end"].startswith("released") else now)

    # ── the bot's rows for kept markets while kept, and the baseline ──
    want = collections.defaultdict(list)
    for e in eps:
        want[e["ticker"]].append(e)
    lo = min(e["kept_since"] for e in eps)
    rows = collections.defaultdict(list)       # id(episode) -> [(t, side, p_true, ask, decision)]
    base = []
    d = lo.date()
    while d <= now.date():
        fn = os.path.join(R, f"EvTelemetry_{d:%Y%m%d}.csv")
        if os.path.exists(fn):
            with open(fn, newline="", encoding="utf-8") as fh:
                r = csv.reader(fh)
                h = next(r)
                ix = {k: i for i, k in enumerate(h)}
                for x in r:
                    if len(x) < len(h) or x[ix["InPlay"]] != "1":
                        continue
                    try:
                        p, ask = float(x[ix["PTrueUsed"]]), float(x[ix["KalshiWsAsk"]])
                    except ValueError:
                        continue
                    if not (0.02 < ask < 0.98 and 0.0 < p < 1.0):
                        continue                    # no real book on that side: says nothing about the pairing
                    t = ts(x[ix["Timestamp"]])
                    if t < lo:
                        continue
                    hit = None
                    for e in want.get(x[ix["Ticker"]], ()):
                        if e["kept_since"] <= t <= e["until"]:
                            hit = e
                    row = (t, x[ix["Side"]], p, ask, x[ix["Decision"]])
                    if hit is not None:
                        rows[id(hit)].append(row)
                    elif "MATCH" in x[ix["Ticker"]]:
                        base.append(row)
        d += dt.timedelta(days=1)

    def fit(rs):
        if not rs:
            return None
        return (st.mean(abs(p - a) for _, _, p, a, _ in rs), st.mean(abs((1 - p) - a) for _, _, p, a, _ in rs))

    flags = []
    for e in eps:
        rs = rows.get(id(e), [])
        e["n"] = len(rs)
        e["first_live"] = min((t for t, *_ in rs), default=None)
        f = fit(rs)
        e["fit"] = f
        if f and len(rs) >= MIN_ROWS:
            same, inv = f
            if inv + WRONG_SIDE_MARGIN < same:
                flags.append((e, f"POSSIBLE WRONG SIDE/MATCH: 1-P_true matches the ask better ({inv:.3f} vs {same:.3f})"))
            elif same > DISAGREE:
                flags.append((e, f"PRICES DISAGREE: mean |P_true - ask| {same:.3f} > {DISAGREE}"))
        if e["max_past"] > 12:
            flags.append((e, f"kept {e['max_past']:.1f}h past its listed start"))
        if e.get("names_from") == "none" and e["end"] == "still kept":
            flags.append((e, "KEPT WITHOUT NAMES at the latest run - the in-play redirect cannot follow it"))
        if e.get("cleared_by_check"):
            flags.append((e, "a later check of a pairing run (price gate / orientation / mirror) cleared it"))

    # ── fills on kept markets while kept ──
    fills = []
    for fn in sorted(glob.glob(os.path.join(R, "EvLive_2026*.csv"))):
        try:
            fd = dt.datetime.strptime(os.path.basename(fn)[7:15], "%Y%m%d").date()
        except ValueError:
            continue
        if fd < lo.date():
            continue
        for x in csv.DictReader(open(fn, newline="", encoding="utf-8")):
            try:
                n = float(x.get("FillCount") or 0)
            except ValueError:
                n = 0
            if n <= 0:
                continue
            t = ts(x["At"])
            for e in want.get(x["Ticker"], ()):
                if e["kept_since"] <= t <= e["until"]:
                    fills.append((e, t, x, n))
    settled = {}
    sp = os.path.join(R, "ev_settlements.jsonl")
    if os.path.exists(sp):
        for line in open(sp, encoding="utf-8"):
            try:
                j = json.loads(line)
            except ValueError:
                continue
            settled[j.get("ticker")] = j.get("result")

    live_eps = [e for e in eps if e["n"]]
    released = [e for e in eps if e["end"].startswith("released")]
    repointed = [e for e in eps if e["repointed"]]
    if a.line:
        warn = "⚠ " if flags else ""
        print(f"{warn}kept pairs{scope}: {len(eps)} market(s) kept, "
              f"{len(live_eps)} priced in-play while kept ({sum(e['n'] for e in eps)} rows), {len(repointed)} re-pointed "
              f"later, {len(released)} released by the age cap, {len(fills)} fill(s); "
              + (f"{len(flags)} FLAGGED" if flags else "all checks pass"))
        return

    print(f"KEPT PAIRS AUDIT  {len(recs)} log records, {len(eps)} episode(s), from {lo:%m-%d %H:%M} UTC")
    print(f"   priced in-play while kept: {len(live_eps)} market(s), {sum(e['n'] for e in eps)} rows | re-pointed by a "
          f"later run: {len(repointed)} | released by the age cap: {len(released)} | fills while kept: {len(fills)}")
    bf = fit(base)
    if bf:
        print(f"   baseline (every other in-play tennis row, same period, n={len(base)}): mean |P_true - ask| "
              f"{bf[0]:.3f}, |(1-P_true) - ask| {bf[1]:.3f}")
    print("\n   market                              kept since    past start  runs  end                       in-play rows (from)   |P-ask|  |(1-P)-ask|")
    for e in sorted(eps, key=lambda e: e["kept_since"]):
        f = e["fit"]
        print(f"   {e['ticker'][-34:]:34}  {e['kept_since']:%m-%d %H:%M}   {e['max_past']:5.1f}h    {e['runs']:3d}   {e['end'][:24]:24}  "
              f"{e['n']:5d} ({e['first_live']:%H:%M})" if e["first_live"] else
              f"   {e['ticker'][-34:]:34}  {e['kept_since']:%m-%d %H:%M}   {e['max_past']:5.1f}h    {e['runs']:3d}   {e['end'][:24]:24}      0        ",
              end="")
        print(f"    {f[0]:.3f}    {f[1]:.3f}" if f else "")
    if fills:
        print("\n   FILLS while kept:")
        for e, t, x, n in fills:
            res = settled.get(x["Ticker"])
            won = "open" if res not in ("yes", "no") else ("WON" if (x["Side"] == "YES") == (res == "yes") else "lost")
            print(f"      {t:%m-%d %H:%M} {x['Ticker'][-28:]} {x['Side']:3} {n:3.0f} @ {float(x['AvgFillPrice'] or 0):.2f}  "
                  f"P_true {float(x['PTrue'] or 0):.3f}  {won}")
    print("\n   CHECKS: " + ("all pass" if not flags else f"{len(flags)} FLAGGED"))
    for e, why in flags:
        print(f"      ⚠ {e['ticker']}: {why}")
    last_ev = [ev for e in eps for ev in e["evidence"][-1:]]
    if last_ev:
        linked = sum(1 for ev in last_ev if ev.get("live_child"))
        print(f"\n   sidecar at the latest run: {linked} of {len(last_ev)} kept market(s) had their live replacement "
              f"linked, {sum(1 for ev in last_ev if ev.get('target_live'))} being served live")


if __name__ == "__main__":
    main()
