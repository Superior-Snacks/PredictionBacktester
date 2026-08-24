#!/usr/bin/env python3
"""
pair_all.py — one command for the whole pairing sequence: scaffold -> pair -> ledger.

    python HardVenArb/pair_all.py              # one pass
    python HardVenArb/pair_all.py --loop 1800  # repeat every 30 min (what a live session wants)
    python HardVenArb/pair_all.py --sports tennis --series ""   # override .env for this run
    python HardVenArb/pair_all.py --fresh      # delete cross_pairs.json first (full rebuild)

WHY A WRAPPER, beyond saving keystrokes:

**`pairHard.py` does not read `.env`.** The sidecar does (`load_dotenv_upwards`), `pairHard` reads only
`os.environ`. Invoked from a shell that has not exported anything, it silently falls back to "every enabled
sport, all series" — measured 2026-08-23: it scaffolded 2,239 markets across ~60 series while the sidecar
was on tennis alone, and 2,205 of them could never pair. The console filled with UNMATCHED lines that looked
like a pairing failure and were really a configuration mismatch. This script reads `.env` itself and passes
the values down, so the two halves cannot disagree.

**Order matters and the steps are not independent.** `pairHard` fills the Kalshi side; `pair_pinnacle` fills
the Pinnacle side and runs the price gate plus the orientation audit; `pair_ledger` records the resulting
mapping. Running the ledger BEFORE pairing records the previous state and misses the change entirely, which
is the one thing it exists to capture.

**A failed step must stop the sequence.** If the scaffold fails, pairing against a stale file writes rows
that look current, and the ledger then records them as though they were fresh.
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ENV = HERE.parent / ".env"
PAIRS = HERE / "cross_pairs.json"

# Only the keys that decide SCOPE. Everything else in .env belongs to the sidecar, not to pairing.
SCOPE_KEYS = ("HARDVEN_SPORTS", "HARDVEN_SERIES_ALLOW")


def read_env_scope() -> dict:
    """Pull the scope keys out of .env (bash `export K=V` form, last assignment wins)."""
    out: dict = {}
    try:
        for line in ENV.read_text(encoding="utf-8", errors="replace").splitlines():
            m = re.match(r"\s*export\s+([A-Z_]+)\s*=\s*(.*)$", line)
            if not m or m.group(1) not in SCOPE_KEYS:
                continue
            v = m.group(2).split("#", 1)[0].strip().strip("'\"")
            out[m.group(1)] = v
    except FileNotFoundError:
        pass
    return out


def run(step: str, cmd: list, env: dict) -> bool:
    print(f"\n=== {step} ===", flush=True)
    t0 = time.time()
    rc = subprocess.call(cmd, env=env)
    print(f"--- {step}: exit {rc} in {time.time() - t0:.0f}s", flush=True)
    return rc == 0


def one_pass(a, env: dict) -> bool:
    if a.fresh and PAIRS.exists():
        PAIRS.unlink()
        print(f"[PAIR-ALL] deleted {PAIRS.name} — full rebuild.")
    py = sys.executable
    if not run("1/3 scaffold (pairHard)", [py, str(HERE / "pairHard.py")], env):
        print("[PAIR-ALL] scaffold FAILED — stopping. Pairing a stale file would write rows that look current.")
        return False
    if not run("2/3 pair Pinnacle (+price gate, +orientation audit)",
               [py, str(HERE / "sidecar" / "pair_pinnacle.py"), "--write", "--price-tol", str(a.price_tol)], env):
        print("[PAIR-ALL] pairing FAILED — skipping the ledger so it cannot record a half-written file.")
        return False
    run("3/3 ledger", [py, str(HERE / "sidecar" / "pair_ledger.py")], env)
    return True


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--loop", type=float, default=0, help="repeat every N seconds (0 = one pass)")
    ap.add_argument("--sports", default=None, help="override HARDVEN_SPORTS for this run")
    ap.add_argument("--series", default=None, help="override HARDVEN_SERIES_ALLOW ('' = all series of the sport)")
    ap.add_argument("--price-tol", type=float, default=0.12)
    ap.add_argument("--fresh", action="store_true", help="delete cross_pairs.json first")
    a = ap.parse_args()

    env = dict(os.environ)
    scope = read_env_scope()
    env.update(scope)
    if a.sports is not None: env["HARDVEN_SPORTS"] = a.sports
    if a.series is not None: env["HARDVEN_SERIES_ALLOW"] = a.series
    print(f"[PAIR-ALL] scope: HARDVEN_SPORTS={env.get('HARDVEN_SPORTS','(unset)')!r}  "
          f"HARDVEN_SERIES_ALLOW={env.get('HARDVEN_SERIES_ALLOW','(unset)')!r}"
          + ("  (from .env)" if scope and a.sports is None else ""))
    if not env.get("HARDVEN_SPORTS"):
        print("[PAIR-ALL] WARNING: no HARDVEN_SPORTS — pairHard will scaffold EVERY enabled sport, and "
              "anything the sidecar is not serving cannot pair. That is the 2,205-unmatched case.")

    if a.loop <= 0:
        sys.exit(0 if one_pass(a, env) else 1)
    print(f"[PAIR-ALL] looping every {a.loop:g}s — ctrl-c to stop.")
    while True:
        try:
            one_pass(a, env)
            print(f"\n[PAIR-ALL] sleeping {a.loop:g}s…", flush=True)
            time.sleep(a.loop)
        except KeyboardInterrupt:
            print("\n[PAIR-ALL] stopped."); break


if __name__ == "__main__":
    main()
