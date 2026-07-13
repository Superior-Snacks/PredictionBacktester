"""
pairing_scheduler.py — re-run the Kalshi <-> Pinnacle pairing pipeline at startup + once per day.

A continuously-running bot needs FRESH pairs each day (yesterday's games are over, today's have appeared).
This runs the existing standalone pairers as subprocesses, in order:
  1. pairHard.py              — Kalshi scaffold -> cross_pairs.json (Pinnacle tokens BLANK)        [HardVenArb/]
  2. pair_pinnacle.py --write — fill the moneyline Pinnacle tokens against the sidecar /catalog     [sidecar/]
  3. pair_derivatives.py --write — spread/total -> derivative_pairs.json (account-free guest API)   [sidecar/]

Steps 1+2 are a chain (the fill reads the scaffold); step 3 is independent (its own file). The C# bot
HOT-RELOADS both files (~15 min), so new pairs appear live without a restart. The ACTIVE sports come from
sports.py (HARDVEN_SPORTS), inherited by every subprocess, so this stays in lockstep with schedule/lifecycle.

Opt-in: HARDVEN_AUTO_PAIR=1. Daily run at HARDVEN_PAIR_HOUR (local hour, default 5). The startup run waits
HARDVEN_PAIR_STARTUP_DELAY seconds first so the sidecar's own HTTP server (which pair_pinnacle calls for
/catalog) is serving. Every step is best-effort — a failure logs and never crashes the sidecar.
"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

SIDECAR = Path(__file__).resolve().parent
ROOT = SIDECAR.parent                     # HardVenArb/ (where pairHard.py + the *_pairs.json live)


class PairingScheduler:
    def __init__(self, hour: int = 5, initial_delay: float = 8.0, interval_min: int = 0):
        self._hour = hour % 24
        self._initial_delay = max(0.0, initial_delay)
        # >0 = re-pair every N minutes (subsumes the daily cadence). Intraday re-pairing is what actually gets
        # LIVE games paired: Pinnacle adds matchups (esp. tennis ITF/challenger) all day, and a match that
        # appears after the daily 5am run would otherwise never pair until the next 5am — by then it's over.
        # The re-pair is account-free (Kalshi public + Pinnacle guest /catalog) and MERGE-safe (pairHard carries
        # over filled pairs), so a frequent re-run can't drop a working live pairing.
        self._interval_min = max(0, interval_min)

    async def run(self) -> None:
        try:
            await asyncio.sleep(self._initial_delay)     # let the sidecar HTTP server come up (/catalog)
            await self._pair_once("startup")
            while True:
                if self._interval_min > 0:
                    print(f"[PAIR SCHED] next re-pair in {self._interval_min} min (intraday cadence).")
                    await asyncio.sleep(self._interval_min * 60)
                    await self._pair_once("interval")
                else:
                    secs = self._secs_until_next()
                    print(f"[PAIR SCHED] next daily re-pair at {self._hour:02d}:00 local (in {secs / 3600:.1f}h).")
                    await asyncio.sleep(secs)
                    await self._pair_once("daily")
        except asyncio.CancelledError:
            pass

    def _secs_until_next(self) -> float:
        """Seconds until the next HARDVEN_PAIR_HOUR:00 in LOCAL time (tomorrow if today's has passed)."""
        now = datetime.now()
        target = now.replace(hour=self._hour, minute=0, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        return (target - now).total_seconds()

    async def _pair_once(self, reason: str) -> None:
        sports = os.environ.get("HARDVEN_SPORTS") or "<all enabled>"
        print(f"[PAIR SCHED] {reason} pairing run — sports={sports}")
        # moneyline: scaffold -> fill (the fill reads the scaffold, so skip it if the scaffold failed)
        if await self._run_step("scaffold (Kalshi)", ["pairHard.py"], ROOT):
            await self._run_step("moneyline fill (Pinnacle)", ["pair_pinnacle.py", "--write"], SIDECAR)
        else:
            print("[PAIR SCHED]   scaffold failed — skipping the moneyline fill (nothing to fill).")
        # derivatives: independent (own file, guest API), so always attempt it
        await self._run_step("derivatives (spread/total)", ["pair_derivatives.py", "--write"], SIDECAR)
        print(f"[PAIR SCHED] {reason} pairing run complete.")

    async def _run_step(self, name: str, script_args: list[str], cwd: Path) -> bool:
        """Run one pairer as a subprocess (inherits the env → HARDVEN_SPORTS etc). Returns True on exit 0."""
        cmd = [sys.executable, *script_args]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, cwd=str(cwd),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
            out, _ = await proc.communicate()
            tail = (out or b"").decode("utf-8", "replace").strip().splitlines()
            if proc.returncode == 0:
                last = f" — {tail[-1]}" if tail else ""
                print(f"[PAIR SCHED]   {name}: OK{last}")
                return True
            print(f"[PAIR SCHED]   {name}: FAILED (exit {proc.returncode})")
            for ln in tail[-4:]:
                print(f"[PAIR SCHED]     {ln}")
            return False
        except Exception as ex:
            print(f"[PAIR SCHED]   {name}: ERROR {type(ex).__name__}: {ex}")
            return False
