r"""What does a HUMAN session look like to BetInAsia's own telemetry — and does the bot fit inside it?

The venue posts `/web/metrics/` with `betslip.duration`, `betslip.closeTime`, `betslip.source` and
`context.tabId`. That is a first-party, server-side record of how betslips get used, and no browser-API
probe can see it (see detect_recon.py). It is also the one place the bot's behaviour is most obviously
NOT human: open a slip, hold it for a fixed interval, never place, close.

So rather than guessing what "looks normal", MEASURE IT — the same method that produced the WS subscribe
pacing envelope (real browser batches: min 1 / median 3 / max 32) instead of a made-up rate.

    # 1. record yourself using the site normally: open some slips, close them, browse.
    #    Runs until you Ctrl+C / close the window; writes betinasia_recon_<timestamp>.jsonl here.
    python betinasia_recon.py --url https://black.betinasia.com/sportsbook/tennis
    # 2. read the envelope back out
    python human_envelope.py betinasia_recon_*.jsonl

Then set the hold range so the bot's slip lifetime sits INSIDE the human distribution rather than outside
it. A bot whose every slip lasts exactly 120.0s is distinguishable no matter how human the mouse path was
on the way in.

── THE PAIRED TEST: does the site record the difference? ─────────────────────────────────────────────
The question "can they tell?" is answerable directly. Do the SAME action twice — once by hand, once with
the bot — capturing each, then diff what actually left the browser:

    # RUN 1 — you, by hand: open one betslip, wait ~20s, close it. Ctrl+C.
    python betinasia_recon.py --url https://black.betinasia.com/sportsbook/tennis
    # RUN 2 — the bot doing the SAME thing: leave it running and fire one probe from another shell
    python betinasia_recon.py --url https://black.betinasia.com/sportsbook/tennis
    #        (other shell)  .\slip_probe.ps1 -Sport tennis
    # Each run writes betinasia_recon_<timestamp>.jsonl; feed the two newest in, oldest first.
    python human_envelope.py --compare <human>.jsonl <bot>.jsonl

Identical traffic means the venue has nothing on the network side to separate them, and the whole question
reduces to the VALUES inside /web/metrics/ (duration, tabId, cadence) rather than to anything about mouse
paths or trusted events. Different traffic names precisely what to fix. Either way it beats reasoning
about it, which is how the last three wrong answers in this project got made.
"""
from __future__ import annotations

import glob
import json
import os
import statistics
import sys


def pct(xs: list[float], p: float) -> float:
    if not xs:
        return 0.0
    xs = sorted(xs)
    i = min(len(xs) - 1, max(0, int(round(p / 100.0 * (len(xs) - 1)))))
    return xs[i]


def _traffic(path: str) -> tuple[set, dict, list]:
    """(endpoints, metrics fields -> values, raw metrics payloads) for one capture.

    RAISES on a missing or empty file rather than returning empties. A typo'd filename used to read as a
    capture in which the venue saw nothing at all — "A only:" against every single endpoint — which is a
    perfectly plausible-looking result and completely wrong. Same failure family as balance() returning
    0.0 for an unreadable wallet: broken and 'nothing happened' must not share a value.
    """
    endpoints, fields, payloads = set(), {}, []
    if not os.path.exists(path):
        raise SystemExit(f"ERROR: no such capture file: {path}\n"
                         f"       (check the extension — the recon writes ONE '.jsonl')")
    if os.path.getsize(path) == 0:
        raise SystemExit(f"ERROR: {path} is 0 bytes — that run recorded nothing. Re-run the capture.")
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                rec = json.loads(line)
            except Exception:
                continue
            url = rec.get("url") or ""
            if not url:
                continue
            from urllib.parse import urlparse
            u = urlparse(url)
            # Collapse ids out of the path so two captures compare on SHAPE, not on generated ids.
            import re as _re
            p = _re.sub(r"/[0-9a-f]{16,}", "/{id}", u.path)
            p = _re.sub(r"/\d{3,}", "/{n}", p)
            if rec.get("kind") == "http":
                endpoints.add(f"{rec.get('method','?')} {u.netloc}{p}")
            elif rec.get("kind") == "ws_frame":
                endpoints.add(f"WS/{rec.get('dir','?')} {rec.get('mtype','?')}")
            if "/web/metrics" in url:
                try:
                    d = json.loads(rec.get("post") or "")
                except Exception:
                    continue
                payloads.append(d)
                for k, v in d.items():
                    fields.setdefault(k, []).append(v)
    return endpoints, fields, payloads


def compare(a_path: str, b_path: str) -> int:
    """Diff two captures: what did the venue see differently?"""
    ae, af, ap = _traffic(a_path)
    be, bf, bp = _traffic(b_path)
    print(f"A = {a_path}   ({len(ae)} endpoint shapes, {len(ap)} metrics posts)")
    print(f"B = {b_path}   ({len(be)} endpoint shapes, {len(bp)} metrics posts)")

    print("\n=== endpoints hit by ONE side only ===")
    only_a, only_b = sorted(ae - be), sorted(be - ae)
    if not only_a and not only_b:
        print("  NONE — both sides produced the same traffic shapes. On the network, they are the same "
              "session; only the VALUES inside /web/metrics/ can separate them.")
    for e in only_a:
        print(f"  A only: {e}")
    for e in only_b:
        print(f"  B only: {e}")

    print("\n=== /web/metrics/ fields present on one side only ===")
    only = (set(af) ^ set(bf))
    if not only:
        print("  NONE — identical field sets.")
    for k in sorted(only):
        side = "A" if k in af else "B"
        print(f"  {side} only: {k}")

    print("\n=== fields present on BOTH — compare the VALUES ===")
    for k in sorted(set(af) & set(bf)):
        av, bv = af[k], bf[k]
        def summ(xs):
            nums = [x for x in xs if isinstance(x, (int, float)) and not isinstance(x, bool)]
            if nums:
                return f"n={len(nums)} min={min(nums)} med={statistics.median(nums):.0f} max={max(nums)}"
            uniq = sorted({str(x) for x in xs})
            return f"n={len(xs)} {uniq[:3]}"
        print(f"  {k:32}\n      A: {summ(av)}\n      B: {summ(bv)}")
    return 0


def main(argv: list[str]) -> int:
    if argv and argv[0] == "--compare":
        if len(argv) != 3:
            print("usage: python human_envelope.py --compare <human.jsonl> <bot.jsonl>")
            return 2
        return compare(argv[1], argv[2])

    paths: list[str] = []
    for a in argv:
        paths.extend(glob.glob(a))
    if not paths:
        print("usage: python human_envelope.py <recon.jsonl> [more.jsonl ...]")
        print("       python human_envelope.py --compare <human.jsonl> <bot.jsonl>")
        return 2

    durations: list[float] = []
    close_times: list[float] = []
    sources: dict[str, int] = {}
    tabs: set = set()
    pages: list[int] = []
    n_metrics = 0

    for p in paths:
        try:
            f = open(p, "r", encoding="utf-8", errors="replace")
        except OSError:
            continue
        with f:
            for line in f:
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                if "/web/metrics" not in (rec.get("url") or ""):
                    continue
                try:
                    d = json.loads(rec.get("post") or "")
                except Exception:
                    continue
                n_metrics += 1
                if "betslip.duration" in d:
                    durations.append(float(d["betslip.duration"]) / 1000.0)
                if "betslip.closeTime" in d:
                    close_times.append(float(d["betslip.closeTime"]))
                if "betslip.source" in d:
                    s = str(d["betslip.source"])
                    sources[s] = sources.get(s, 0) + 1
                if "context.tabId" in d:
                    tabs.add(d["context.tabId"])
                if isinstance(d.get("context.pageNo"), (int, float)):
                    pages.append(int(d["context.pageNo"]))

    print(f"files: {len(paths)}   /web/metrics/ posts: {n_metrics}")
    if not durations:
        print("\nNO betslip.duration SAMPLES. Open and close some betslips by hand while a recon capture "
              "runs, then re-read this. Without human samples there is no envelope to compare against, "
              "and any hold time is a guess.")
        return 1

    print(f"\n=== betslip LIFETIME, seconds (n={len(durations)}) ===")
    print(f"  min {min(durations):8.1f}   p25 {pct(durations,25):8.1f}   median {statistics.median(durations):8.1f}")
    print(f"  p75 {pct(durations,75):8.1f}   p90 {pct(durations,90):8.1f}   max    {max(durations):8.1f}")
    if close_times:
        print(f"\n=== close time, ms (n={len(close_times)}) ===")
        print(f"  min {min(close_times):.0f}  median {statistics.median(close_times):.0f}  max {max(close_times):.0f}")
    if sources:
        print("\n=== where slips were opened from ===")
        for s, c in sorted(sources.items(), key=lambda kv: -kv[1]):
            print(f"  {c:5}x  {s}")
    print(f"\n=== tabs / pages ===")
    print(f"  distinct context.tabId: {len(tabs)}")
    if pages:
        print(f"  context.pageNo max:     {max(pages)}   (navigations in the session)")

    # ── does the bot fit? ────────────────────────────────────────────────────
    hold = float(os.environ.get("HARDVEN_SLIP_HOLD_SEC", "120"))
    lo, hi = pct(durations, 10), pct(durations, 90)
    print(f"\n=== does the bot fit inside this? ===")
    print(f"  HARDVEN_SLIP_HOLD_SEC = {hold:.0f}s   human p10-p90 = {lo:.1f}-{hi:.1f}s")
    if hold > max(durations):
        print(f"  *** OUTSIDE *** every bot slip would outlast the LONGEST human slip seen ({max(durations):.1f}s).")
    elif hold > hi:
        print(f"  *** HIGH *** above the human p90. Fits only the tail, and fits it EVERY time.")
    else:
        print(f"  within range — but a FIXED value is still a signature. Randomise it across the range.")
    print("  NOTE: a constant is the giveaway, not the value. Human slip lifetimes are scattered; a bot "
          "whose slips all last the same duration is separable at any setting.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
