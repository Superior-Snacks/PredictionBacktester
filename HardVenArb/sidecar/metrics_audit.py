"""What does BetInAsia's own telemetry actually report about us?

`/web/metrics/` is the venue's first-party analytics channel. `betslip.duration` was found in it on
2026-08-14, which is why slips are now held for a believable 3-12s rather than closed instantly. But
"the one field we happened to notice" is not the same as "the only field", and the difference decides
whether holding the slip is sufficient or merely necessary.

This enumerates EVERY field ever POSTed to that endpoint across all recon captures, so the behavioural
surface is known rather than assumed. Field names and shapes only — no values are printed beyond a
truncated example, and nothing is sent anywhere.

    python metrics_audit.py
    python metrics_audit.py --show-bodies 3      # a few full bodies, for shape
"""
from __future__ import annotations

import argparse
import collections
import glob
import json
import sys

sys.stdout.reconfigure(encoding="utf-8")


# Values kept for the few fields worth a distribution rather than an example.
_collected: dict = collections.defaultdict(list)
_KEEP = ("betslip.duration", "betslip.closeTime", "betslip.source")


def walk(obj, prefix, keys, samples):
    """Flatten nested payloads so `{"betslip": {"duration": 4}}` reports as `betslip.duration`."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = f"{prefix}.{k}" if prefix else str(k)
            if isinstance(v, (dict, list)):
                walk(v, p, keys, samples)
            else:
                keys[p] += 1
                samples.setdefault(p, str(v)[:64])
                if p in _KEEP:
                    _collected[p].append(v)
    elif isinstance(obj, list):
        for it in obj[:20]:
            walk(it, prefix, keys, samples)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--show-bodies", type=int, default=0)
    a = ap.parse_args()

    keys: collections.Counter = collections.Counter()
    samples: dict = {}
    bodies, n_posts, n_files = [], 0, 0

    for f in sorted(glob.glob("betinasia_recon_*.jsonl")):
        hit = False
        try:
            fh = open(f, encoding="utf-8", errors="replace")
        except Exception:
            continue
        with fh:
            for line in fh:
                if "web/metrics" not in line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                if "web/metrics" not in (rec.get("url") or ""):
                    continue
                hit = True
                n_posts += 1
                post = rec.get("post")
                if not post:
                    continue
                if len(bodies) < a.show_bodies:
                    bodies.append((f, str(post)[:400]))
                try:
                    walk(json.loads(post), "", keys, samples)
                except Exception:
                    # not JSON: record the shape so form-encoded payloads are not invisible
                    keys["(non-json body)"] += 1
                    samples.setdefault("(non-json body)", str(post)[:64])
        n_files += 1 if hit else 0

    print(f"/web/metrics/ POSTs: {n_posts} across {n_files} capture(s)\n")
    if not keys:
        print("No decodable bodies. Either the captures predate ALWAYS_BODY (only the first five\n"
              "responses were kept) or the payload is not JSON — re-run a recon and re-check.")
        return 1
    print(f"{'field':38} {'count':>6}  example")
    for k, c in keys.most_common(40):
        print(f"  {k:36} {c:6}  {samples.get(k, '')}")
    # BETSLIP DWELL DISTRIBUTION. The bot now holds slips 3-12s to look believable, but that range was
    # chosen without ever seeing what the venue actually records. These are OUR OWN historical values,
    # including the hand-driven sessions — so the human rows are the target and the bot rows are the
    # thing being judged.
    durs = sorted(v for v in _collected["betslip.duration"] if isinstance(v, (int, float)))
    if durs:
        def pct(p):
            return durs[min(len(durs) - 1, int(len(durs) * p))]
        print(f"\nbetslip.duration over {len(durs)} samples (ms):")
        print(f"  min {durs[0]}   p25 {pct(.25)}   median {pct(.5)}   p75 {pct(.75)}   max {durs[-1]}")
        under3 = sum(1 for d in durs if d < 3000)
        print(f"  under 3s: {under3}/{len(durs)}  ({100 * under3 / len(durs):.0f}%)")
    srcs = collections.Counter(_collected["betslip.source"])
    if srcs:
        print("\nbetslip.source — WHERE the slip was opened from:")
        for s, c in srcs.most_common():
            print(f"  {c:4}  {s}")
        print("  A bot that only ever opens from the sport board reports one value forever; a person\n"
              "  reaches the slip from highlights, an event page and search as well.")

    for f, b in bodies:
        print(f"\n--- body from {f}\n{b}")
    print("\nRead this as the BEHAVIOURAL surface: anything here is reported regardless of how the click\n"
          "was generated, so no amount of input realism affects it. Fields describing timing or dwell are\n"
          "the ones the bot must produce believable values for, not merely avoid.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
