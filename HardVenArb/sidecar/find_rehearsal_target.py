"""Find a LIVE selection_id worth rehearsing a bet on, right now.

Selection ids are not in the DOM -- they are synthesised from the WS `event` frames
(`make_selection_id`), so the only way to get a current one is to ask the running sidecar. Ids from
yesterday's telemetry CSVs are settled games and will fail at the first step.

Applies the three filters that actually matter, in the order that costs least:

  1. PRE-LIVE, with a margin. A game starting in 5 minutes can tip in-play mid-rehearsal and change
     the layout under the bot, which reads as a selector failure that isn't one.
  2. PRICED. /catalog lists everything the feed knows; /odds only answers for what is SUBSCRIBED.
     An unsubscribed id is not a bug, it just cannot be quoted.
  3. available_for_accas. The venue refuses to put a flagged event on a betslip at all -- no UI path
     works around it. This is what killed every cricket and soccer row in the 08-14 slip-verify CSV.

Nothing here clicks anything: /catalog and /odds are cache reads.

    python find_rehearsal_target.py                     # any sport, 40min-8h out
    python find_rehearsal_target.py --sport tennis
    python find_rehearsal_target.py --min-mins 90 --max-mins 600
    python find_rehearsal_target.py --stake 4 --json     # emit a ready /bet/test body
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import urllib.parse
import urllib.request

sys.stdout.reconfigure(encoding="utf-8")

# The venue rejects orders returning less than about $5 (observed "Less than min order of $3.80" at
# ~1.32 odds). Mirrors BIA_MIN_ORDER_RETURN so this never suggests a target that place_bet would refuse.
MIN_RETURN = 5.0


def get(url: str, timeout: int = 60):
    return json.load(urllib.request.urlopen(url, timeout=timeout))


def start_of(e: dict):
    s = e.get("start_time")
    if not s:
        return None
    try:
        return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8788)
    ap.add_argument("--sport", default=None, help="tennis, baseball, af, basket, ...")
    ap.add_argument("--min-mins", type=float, default=40.0)
    ap.add_argument("--max-mins", type=float, default=480.0)
    ap.add_argument("--stake", type=float, default=4.0)
    ap.add_argument("--probe", type=int, default=60, help="how many candidates to price-check")
    ap.add_argument("--json", action="store_true", help="print a /bet/test body for the top pick")
    a = ap.parse_args()

    base = f"http://127.0.0.1:{a.port}"
    try:
        get(f"{base}/health", timeout=5)
    except Exception as e:
        print(f"SIDECAR NOT REACHABLE on {a.port}: {type(e).__name__}. Start it first.")
        return 2

    cat = get(f"{base}/catalog")["selections"]
    now = dt.datetime.now(dt.timezone.utc)
    cand = []
    for e in cat:
        t = start_of(e)
        if not t or e.get("three_way"):
            continue
        mins = (t - now).total_seconds() / 60.0
        if not (a.min_mins < mins < a.max_mins):
            continue
        if a.sport and e.get("sport") != a.sport:
            continue
        cand.append((t, mins, e))
    cand.sort(key=lambda r: r[0])
    print(f"catalog {len(cat)}  ->  pre-live two-way in "
          f"[{a.min_mins:.0f}m, {a.max_mins:.0f}m]: {len(cand)}")
    if not cand:
        print("Nothing in that window. Widen --max-mins, or the feed is only subscribed to one sport\n"
              "(the bot subscribes what it is pointed at -- an empty sport here is scope, not a fault).")
        return 1

    batch = cand[: a.probe]
    ids = [e["selection_id"] for _, _, e in batch]
    url = f"{base}/odds?selections=" + urllib.parse.quote(",".join(ids), safe="")
    sels = get(url)["selections"]
    info = {e["selection_id"]: (t, m, e) for t, m, e in batch}

    rows = []
    for sid, s in sels.items():
        odds = s.get("decimal_odds") or 0.0
        # `acca` is only published when the venue said False, so absence means fine -- do not invert this.
        if s.get("acca") is False or odds <= 1.0:
            continue
        t, mins, e = info[sid]
        rows.append((mins, e, s, odds))
    rows.sort(key=lambda r: r[0])
    print(f"probed {len(ids)}  ->  priced {len(sels)}  ->  priced AND acca-ok: {len(rows)}\n")
    if not rows:
        print("All probed candidates were unpriced or acca-blocked. Try another sport/window.")
        return 1

    for mins, e, s, odds in rows[:16]:
        ret = a.stake * odds
        flag = "" if ret >= MIN_RETURN else f"  (!! returns {ret:.2f}, under the ~{MIN_RETURN:.0f} min)"
        print(f"  +{mins:5.0f}m {e['sport']:9} {e['event'][:38]:38} | "
              f"{e['selection_name'][:18]:18} @ {odds:>7}{flag}")
        print(f"          {e['selection_id']}")

    # Prefer a pick whose stake clears the venue minimum, so the same target also works for submit=true.
    good = [r for r in rows if a.stake * r[3] >= MIN_RETURN] or rows
    mins, e, s, odds = good[0]
    # Ask BELOW the board: max_odds is both the floor and the price typed into the slip, and the venue
    # improves you to what is actually available (1.496 asked -> 1.526 filled, 2026-08-15). Asking under
    # guarantees the order is marketable, so a slow fill means a slow BOOK and not a resting order.
    ask = round(odds * 0.97, 3)
    body = {"selection_id": e["selection_id"], "stake": round(a.stake, 2),
            "max_odds": ask, "submit": False}
    print(f"\nPICK: {e['event']} -- {e['selection_name']}  (starts in {mins:.0f}m, board {odds})")
    print(f"      asking {ask} = 3% under the board, so the slip is marketable and the floor passes")
    if a.json:
        print(json.dumps(body))
    else:
        print("\nRehearse it (drives the real UI, stops before Place, places nothing):\n")
        print(f"  Invoke-RestMethod -Method Post -Uri {base}/bet/test `\n"
              f"    -ContentType application/json `\n"
              f"    -Body '{json.dumps(body)}'")
    return 0


if __name__ == "__main__":
    sys.exit(main())
