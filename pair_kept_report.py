"""pair_kept_report.py - audit of the pair-file rows the pairing run KEPT (2026-11-12). Read-only.

Since 2026-11-12 a paired market whose Pinnacle matchup has left the board (an ITF match past its listed start,
waiting for court time; a moneyline Pinnacle has taken down for a while; a rescheduled match) keeps its pairing
instead of being emptied, so the bot keeps asking and the sidecar's redirect can serve the replacement's price the
moment there is one. Every pairing run logs one run record plus each kept or released row, with what the sidecar
held for it, to HardVenArb/pair_kept.jsonl. This report checks that keeping them was RIGHT:

  RUNS       per pairing run: rows re-checked against the board, re-filled from it, kept, released, named, and
             how many rows kept until that run ended at it.
  EPISODES   each market's run of consecutive kept records: how long it was kept, hours from its listed start
             (negative = still before it), and how it ended - re-pointed at a new matchup / listed again (the same
             matchup back on the board) / re-paired through its other market / market closed (Kalshi closed it, so
             it left the pair file - the usual end of a match that was played while kept) / released by the age
             cap / still kept. Since 2026-11-13 the pairing run logs each ending itself; before that it is worked
             out from the ledger and the files, and "dropped" = could not tell.
  PRICED     did the bot get IN-PLAY prices for it while it was kept (the redirect working), from when.
  CORRECT    on those in-play rows, does Pinnacle's P_true track the Kalshi ask for the SAME side? A wrong match
             or a flipped side shows up as P_true tracking 1 - ask, or the two simply disagreeing. Compared with
             the same numbers for every other in-play tennis row of the period (the baseline).
  FILLS      every fill on a kept market while it was kept, with its result.

    python pair_kept_report.py                     # everything logged
    python pair_kept_report.py --since 2026-11-12  # from a date (UTC)
    python pair_kept_report.py --hours 24 --line   # one summary line (for Discord)
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
PAIRS = os.environ.get("PAIR_KEPT_PAIRS") or os.path.join(R, "HardVenArb", "cross_pairs.json")
LEDGER = os.environ.get("PAIR_KEPT_LEDGER") or os.path.join(R, "pair_ledger.jsonl")
MIN_ROWS = 10           # in-play rows needed before judging a market's agreement
WRONG_SIDE_MARGIN = 0.10  # 1-P_true beats P_true at matching the ask by this much -> flag
DISAGREE = 0.15         # mean |P_true - ask| above this -> flag
LEDGER_LAG = dt.timedelta(minutes=20)   # the ledger step runs after the fill step of the same run


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
        e = by.setdefault(k, {"ticker": j.get("ticker"), "event_id": j.get("event_id"), "token": j.get("token"),
                              "start": j.get("start"), "kept_since_raw": j.get("kept_since") or j.get("at"),
                              "kept_since": ts(j.get("kept_since") or j.get("at")), "first": ts(j["at"]),
                              "last": ts(j["at"]), "runs": 0, "vs_start": None, "end": "still kept",
                              "kind": "still kept", "evidence": []})
        e["last"] = ts(j["at"])
        e["runs"] += 1
        if j.get("hours_past_start") is not None:
            e["vs_start"] = float(j["hours_past_start"])   # the latest run's (time only moves on)
        if j.get("decision") == "released":
            e["kind"] = "released"
            e["end"] = "released: " + (j.get("reason") or "")
        if j.get("evidence"):
            e["evidence"].append(j["evidence"])
        if j.get("names_from"):
            e["names_from"] = j["names_from"]          # the latest run's answer
        if str(j.get("final") or "").startswith("cleared"):
            e["cleared_by_check"] = ts(j["at"])
    return list(by.values())


def how_it_ended(e: dict, run_times: list, ledger: dict, pairs_now: dict | None, by_event: dict,
                 now: dt.datetime, endings: dict, logs_endings: set, eps_by_ticker: dict) -> None:
    """Sets e["kind"], e["end"], e["until"]. A kept row is re-checked at every run; the first run after its last
    record that did NOT keep it is when the keeping ended. Since 2026-11-13 that run logs how (an "ended" record),
    and a kept row with no record at all there had left the pair file: Kalshi closed the market. Older runs logged
    no endings, so for those the ledger + the files say how, as well as they can."""
    if e["kind"] == "released":
        e["until"] = e["last"]
        return
    if e.get("cleared_by_check"):                    # a check of its last run took the pairing away: ended there
        e["until"] = e["last"]
        e["kind"], e["end"] = "cleared", f"cleared {e['last']:%H:%M}"
        return
    nxt = next((t for t in run_times if t > e["last"]), None)
    cur = (pairs_now or {}).get(e["ticker"]) or {}
    if nxt is None:                                  # the latest logged run kept it
        e["until"] = now
        if pairs_now is None or (cur.get("hardven_kept_since") or "")[:19] == (e["kept_since_raw"] or "")[:19]:
            e["kind"], e["end"] = "still kept", "still kept"
        else:                                        # a run is mid-way, or the file was edited by hand
            e["kind"], e["end"] = "no longer kept", "no longer kept"
        return
    e["until"] = nxt
    if nxt in logs_endings:
        rec = endings.get((e["ticker"], nxt))
        kind = "market closed" if rec is None else (rec.get("how") or "ended")
        fin = str((rec or {}).get("final") or "")
        if fin.startswith(("cleared", "swapped")):
            e["after_end"] = fin.split(" (")[0]      # re-filled, then a check of the same run took it away
        e["kind"], e["end"] = kind, f"{kind} {nxt:%H:%M}"
        return
    moved = any(e["last"] < at <= nxt + LEDGER_LAG and tok != e["token"] for at, tok in ledger.get(e["ticker"], []))
    sib = [r for r in by_event.get(e.get("event_id") or "", ()) if r.get("kalshi_ticker") != e["ticker"]
           and r.get("hardven_yes_token")]
    if moved:
        kind = "re-pointed"                          # a new matchup (the live replacement, a re-listing)
    elif any(o["first"] > e["last"] for o in eps_by_ticker.get(e["ticker"], ())):
        kind = "listed again"                        # kept again later, so it was paired in between: re-filled
    elif cur.get("hardven_yes_token"):
        kind = "listed again"                        # the same matchup back on the board
    elif sib:
        kind = "other market"                        # its event re-paired through the sibling market
    elif pairs_now is not None and e["ticker"] not in pairs_now:
        kind = "market closed"                       # gone from the pair file (when exactly, an old run can't say)
    else:
        kind = "dropped"                             # still listed by Kalshi, unpaired, and no record says why
    e["kind"], e["end"] = kind, f"{kind} {nxt:%H:%M}"


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
    runs = [j for j in recs if "run" in j]
    rowrecs = [j for j in recs if "run" not in j and j.get("decision") != "ended"]
    if not rowrecs:
        n = len(runs)
        print(f"kept pairs{scope}: none kept" + (f" ({n} pairing run(s) logged, every paired row found on the board)"
                                                 if n else ""))
        return
    eps = episodes(rowrecs)
    now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    every = load_log(None)                                          # every run ever: an episode may end after --since
    run_times = sorted({ts(j["at"]) for j in every})
    logs_endings = {ts(j["at"]) for j in every if "ended" in (j.get("run") or {})}
    endings = {(j.get("ticker"), ts(j["at"])): j for j in every if j.get("decision") == "ended"}
    eps_by_ticker = collections.defaultdict(list)
    for e in eps:
        eps_by_ticker[e["ticker"]].append(e)

    ledger = collections.defaultdict(list)
    for line in open(LEDGER, encoding="utf-8"):
        try:
            j = json.loads(line)
        except ValueError:
            continue
        ledger[j.get("ticker")].append((dt.datetime.fromtimestamp(j["at"], dt.timezone.utc).replace(tzinfo=None), j.get("yes_token")))
    for v in ledger.values():
        v.sort()
    try:
        cur_rows = json.load(open(PAIRS, encoding="utf-8"))
        pairs_now = {r.get("kalshi_ticker"): r for r in cur_rows}
        by_event = collections.defaultdict(list)
        for r in cur_rows:
            by_event[r.get("event_id") or ""].append(r)
    except Exception:
        pairs_now, by_event = None, {}
    for e in eps:
        how_it_ended(e, run_times, ledger, pairs_now, by_event, now, endings, logs_endings, eps_by_ticker)

    # ── the bot's rows for kept markets while kept (tennis + the shadow sports), and the tennis baseline ──
    want = collections.defaultdict(list)
    for e in eps:
        want[e["ticker"]].append(e)
    lo = min(e["kept_since"] for e in eps)
    rows = collections.defaultdict(list)       # id(episode) -> [(t, side, p_true, ask, decision)]
    base = []
    d = lo.date()
    while d <= now.date():
        for prefix in ("EvTelemetry", "EvShadowTelemetry"):
            fn = os.path.join(R, f"{prefix}_{d:%Y%m%d}.csv")
            if not os.path.exists(fn):
                continue
            with open(fn, newline="", encoding="utf-8") as fh:
                r = csv.reader(fh)
                h = next(r, None)
                if not h:
                    continue
                ix = {k: i for i, k in enumerate(h)}
                for x in r:
                    if len(x) < len(h) or x[ix["InPlay"]] != "1":
                        continue
                    tk = x[ix["Ticker"]]
                    if prefix != "EvTelemetry" and tk not in want:
                        continue                    # shadow rows only matter for a kept market
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
                    for e in want.get(tk, ()):
                        if e["kept_since"] <= t <= e["until"]:
                            hit = e
                    row = (t, x[ix["Side"]], p, ask, x[ix["Decision"]])
                    if hit is not None:
                        rows[id(hit)].append(row)
                    elif prefix == "EvTelemetry" and "MATCH" in tk:
                        base.append(row)
        d += dt.timedelta(days=1)

    def fit(rs):
        if not rs:
            return None
        return (st.mean(abs(p - a) for _, _, p, a, _ in rs), st.mean(abs((1 - p) - a) for _, _, p, a, _ in rs))

    flags = []
    pre_fix = set()             # flags from runs before 2026-11-13's fix (known, explained; kept until they age out)
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
        if (e["vs_start"] or 0) > 12:
            flags.append((e, f"kept {e['vs_start']:.1f}h past its listed start"))
        if e.get("names_from") == "none" and e["kind"] == "still kept":
            flags.append((e, "KEPT WITHOUT NAMES at the latest run - the redirect cannot follow it"))
        if e.get("cleared_by_check"):
            if e["cleared_by_check"] in logs_endings:
                flags.append((e, "a later check of a pairing run (the name audit) cleared it while kept"))
            else:               # the price gate, which read kept rows until 11-13 - between session windows a frozen price
                pre_fix.add(len(flags))
                flags.append((e, "the price gate cleared it while kept (before the 11-13 fix: between session "
                                 "windows it compared a FROZEN price with Kalshi's live one; kept rows skip it now)"))
        if e.get("after_end"):
            flags.append((e, f"{e['end']}, then {e['after_end']} - the market lost its pairing"))
        if e["kind"] in ("re-filled on the OTHER side", "mirror conflict"):
            flags.append((e, f"{e['end']} - the board disagrees with the side it was kept on"
                             if e["kind"] != "mirror conflict" else f"{e['end']} - both markets of its event unpaired"))

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
    kinds = collections.Counter(e["kind"] for e in eps)
    order = ("still kept", "re-pointed", "listed again", "other market", "market closed", "released", "cleared",
             "re-filled on the OTHER side", "mirror conflict", "dropped", "no longer kept")
    ends = ", ".join(f"{kinds[k]} {k if k != 'released' else 'released by the age cap'}" for k in order
                     if kinds[k] or k in ("still kept", "released"))
    if a.line:
        warn = "⚠ " if flags else ""
        print(f"{warn}kept pairs{scope}: {len(eps)} market(s) kept - {ends}; {len(live_eps)} priced in-play while "
              f"kept ({sum(e['n'] for e in eps)} rows), {len(fills)} fill(s); "
              + (f"{len(flags)} FLAGGED" + (f" ({'all' if len(pre_fix) == len(flags) else len(pre_fix)} from before "
                                             f"the 11-13 fix)" if pre_fix else "") if flags else "all checks pass"))
        return

    print(f"KEPT PAIRS AUDIT  {len(rowrecs)} row record(s), {len(eps)} episode(s), from {lo:%m-%d %H:%M} UTC")
    print(f"   ended: {ends}")
    print(f"   priced in-play while kept: {len(live_eps)} market(s), {sum(e['n'] for e in eps)} rows | fills while kept: "
          f"{len(fills)}")
    bf = fit(base)
    if bf:
        print(f"   baseline (every other in-play tennis row, same period, n={len(base)}): mean |P_true - ask| "
              f"{bf[0]:.3f}, |(1-P_true) - ask| {bf[1]:.3f}")
    if runs:
        print("\n   RUNS (latest 10)   re-checked = paired rows held against the board")
        for j in runs[-10:]:
            r = j["run"]
            print(f"      {ts(j['at']):%m-%d %H:%M}  re-checked {r.get('held', 0):4d}: re-filled {r.get('refilled', 0):4d}, "
                  f"kept {r.get('kept', 0):3d}, released {r.get('released', 0)}, cleared later {r.get('cleared_later', 0)}, "
                  f"named from the sidecar {r.get('named', 0)}"
                  + (f", endings logged {r['ended']}" if "ended" in r else "")
                  + (f" | near-even rows kept on their named side {r['names_kept']}" if "names_kept" in r else ""))
    print("\n   vs start = hours from the listed start at the latest run (negative = still before it)")
    print("   market                              kept since    vs start  runs  end                       in-play rows (from)   |P-ask|  |(1-P)-ask|")
    for e in sorted(eps, key=lambda e: e["kept_since"]):
        f = e["fit"]
        vs = f"{e['vs_start']:+6.1f}h" if e["vs_start"] is not None else "     ? "
        head = f"   {e['ticker'][-34:]:34}  {e['kept_since']:%m-%d %H:%M}   {vs}  {e['runs']:3d}   {e['end'][:24]:24}  "
        print(head + (f"{e['n']:5d} ({e['first_live']:%H:%M})" if e["first_live"] else "    0        "), end="")
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
    if run_times:
        latest = run_times[-1]
        at_latest = [j for j in rowrecs if ts(j["at"]) == latest and j.get("decision") == "kept"]
        if at_latest:
            evs = [j.get("evidence") or {} for j in at_latest]
            print(f"\n   sidecar at the latest run ({latest:%m-%d %H:%M}): {sum(1 for ev in evs if ev.get('live_child'))} of "
                  f"{len(evs)} kept market(s) had a replacement linked, {sum(1 for ev in evs if ev.get('redirect'))} "
                  f"redirected, {sum(1 for ev in evs if ev.get('target_live'))} being served live")
        else:
            print(f"\n   the latest run ({latest:%m-%d %H:%M}) kept nothing")


if __name__ == "__main__":
    main()
