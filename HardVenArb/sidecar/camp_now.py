#!/usr/bin/env python3
"""camp_now.py — pick a game that is ACTUALLY on the live board and camp on it, to time the arm.

    python camp_now.py                 # pick the best candidate and arm it
    python camp_now.py --list          # show candidates, arm nothing
    python camp_now.py --stake 10
    python camp_now.py --stop          # release whatever is camped

WHY THIS EXISTS. Timing the arm by hand kept failing on the wrong thing: tokens picked from
`cross_pairs.json` were quoted `open` but PRE-MATCH, and the in-play board only lists live games — so the
row scan hunted a list that could not contain them and reported "no row mentions both X and Y" after ~13s.
That reads like a markup bug and is nothing of the sort.

So this picks from the intersection that can actually work:
    the reader's LIVE matchup topics  ∩  /catalog (so the popover can be verified before betting)
and refuses to guess when that intersection is empty, rather than arming something that cannot be found.

Nothing is placed. `camp/start` opens the slip and types a stake; only `camp/fire` buys.
"""
from __future__ import annotations
import argparse, json, sys, urllib.request

SIDE = "http://127.0.0.1:8787"


def _get(path: str, timeout: float = 60.0):
    with urllib.request.urlopen(f"{SIDE}{path}", timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _post(path: str, body: dict | None = None, timeout: float = 120.0):
    data = json.dumps(body or {}).encode("utf-8")
    req = urllib.request.Request(f"{SIDE}{path}", data=data,
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def candidates() -> list[dict]:
    """Live topics that /catalog can also describe — the only ones a camp can verify AND find."""
    live = _get("/debug/reader").get("live_mids") or []
    cat: dict[str, list] = {}
    for row in _get("/catalog").get("selections") or []:
        cat.setdefault(row["selection_id"].rsplit(":", 1)[0], []).append(row)
    out = []
    for mid in live:
        for row in cat.get(mid, []):
            # '(Games)' shells carry the same players on a different market — never a camp target.
            if "(games)" in (row.get("event") or "").lower():
                continue
            out.append(row)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stake", type=float, default=10.0)
    ap.add_argument("--list", action="store_true", help="show candidates, arm nothing")
    ap.add_argument("--stop", action="store_true", help="release the current camp")
    a = ap.parse_args()

    try:
        if a.stop:
            print(json.dumps(_post("/camp/stop"), indent=1))
            return 0
        h = _get("/health", timeout=10)
        if not h.get("session_ready"):
            print("[camp_now] the sidecar has no live Pinnacle session — log in first; nothing to arm.")
            return 2
        cands = candidates()
    except Exception as ex:
        print(f"[camp_now] could not reach the sidecar at {SIDE} ({type(ex).__name__}: {ex}). Is it running?")
        return 2

    if not cands:
        print("[camp_now] NOTHING ARMABLE. No live matchup is also in /catalog right now, so any arm would "
              "hunt a row the board does not have. This is a real state, not an error — wait for a live game "
              "whose matchup the catalog has caught up on.")
        return 1

    print(f"[camp_now] {len(cands)} armable live selection(s):")
    for c in cands:
        print(f"    {c['selection_id']:30s} {c['selection_name'][:26]:28s} {c['event'][:44]}")
    if a.list:
        return 0

    pick = cands[0]
    print(f"\n[camp_now] arming {pick['selection_id']} ({pick['event']}) at stake {a.stake:g} — nothing is placed.")
    res = _post("/camp/start", {"selection_id": pick["selection_id"], "stake": a.stake})
    print(json.dumps(res, indent=1))
    if res.get("ok"):
        print("\n[camp_now] armed. The timing is in the SIDECAR console:")
        print("    [PINNACLE ROW]   found after N pass(es) in Xms      <- hunting the row")
        print("    [PINNACLE CLICK] wheel/scrollinto/box/move/dwell/click")
        print("    [PINNACLE ARM]   pause/select/stake/verify total    <- the whole arm")
        print("    ...then: python camp_now.py --stop")
    return 0 if res.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
