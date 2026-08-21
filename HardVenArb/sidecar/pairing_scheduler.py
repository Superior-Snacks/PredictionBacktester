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
import time
import sys
from datetime import datetime, timedelta
from pathlib import Path

SIDECAR = Path(__file__).resolve().parent
ROOT = SIDECAR.parent                     # HardVenArb/ (where pairHard.py + the *_pairs.json live)


class PairingScheduler:
    def __init__(self, hour: int = 5, initial_delay: float = 8.0, interval_min: int = 0,
                 steps: "list[tuple[str, list[str], object]] | None" = None,
                 wait_ready: "object | None" = None, wait_ready_sec: float = 90.0,
                 reader_probe: "object | None" = None):
        # `steps` = [(label, [script, *args], cwd)]. None keeps the historic Pinnacle chain, so nothing
        # changes for the venue that already uses this. A second book supplies its own chain instead of
        # getting a scheduler of its own -- the cadence logic is the part worth sharing.
        self._steps = steps
        self._hour = hour % 24
        self._initial_delay = max(0.0, initial_delay)
        # >0 = re-pair every N minutes (subsumes the daily cadence). Intraday re-pairing is what actually gets
        # LIVE games paired: Pinnacle adds matchups (esp. tennis ITF/challenger) all day, and a match that
        # appears after the daily 5am run would otherwise never pair until the next 5am — by then it's over.
        # The re-pair is account-free (Kalshi public + Pinnacle guest /catalog) and MERGE-safe (pairHard carries
        # over filled pairs), so a frequent re-run can't drop a working live pairing.
        self._interval_min = max(0, interval_min)
        # An asyncio.Event set once the venue session has PROVEN itself with a live authed call. The Pinnacle
        # fill step makes authed /leagues/*/markets/straight requests, and running it against a captured-but-
        # dead session is not merely useless: on 2026-08-17 the startup run fired fourteen of them into guest
        # redirects, which tripped the mass-logout detector and forced a re-mint the login was already
        # handling. Waiting costs seconds and removes the cascade.
        self._wait_ready = wait_ready
        self._wait_ready_sec = max(0.0, wait_ready_sec)
        # Callable returning the matchups the odds READER has actually seen. The startup run waits for it to
        # report something — see _wait_for_reader.
        self._reader_probe = reader_probe

    async def _wait_for_reader(self) -> None:
        """Hold the STARTUP run until the odds reader has actually seen the board.

        The guest feed does not list in-play matchups, so the only description of the live board comes from
        the reader — and at startup+8s the reader has seen nothing. Measured 2026-08-21: the startup pairing
        finished BEFORE the reader's first 15s heartbeat, the catalog could describe 5 live matchups, and 8
        of 16 live ones stayed unpairable until the next cadence 90 minutes later. On a board whose games
        last about two hours, ninety minutes late is most of the way to never.

        Bounded and non-fatal: a reader that never reports (pre-live-only setups, a dark browser) must not
        block pairing at all — the pre-match half of the board pairs perfectly well without it.
        """
        probe = self._reader_probe
        if probe is None:
            return
        try:
            cap = float(os.environ.get("HARDVEN_PAIR_WAIT_READER_SEC", "150") or 150)
        except ValueError:
            cap = 150.0
        if cap <= 0:
            return
        deadline = time.time() + cap
        said = False
        while time.time() < deadline:
            try:
                n = len(probe() or [])
            except Exception:
                return                        # no probe to speak of; do not hold up pairing over it
            if n:
                if said:
                    print(f"[PAIR SCHED] reader is seeing {n} matchup(s) - pairing now.")
                return
            if not said:
                said = True
                print(f"[PAIR SCHED] waiting up to {cap:.0f}s for the odds reader to see the board "
                      f"(the guest feed does not list in-play matchups; HARDVEN_PAIR_WAIT_READER_SEC=0 "
                      f"to skip).")
            await asyncio.sleep(3)
        print(f"[PAIR SCHED] reader still quiet after {cap:.0f}s - pairing on the guest board alone "
              f"(in-play matchups will fill on the next cadence).")

    async def run(self) -> None:
        try:
            await asyncio.sleep(self._initial_delay)     # let the sidecar HTTP server come up (/catalog)
            await self._wait_for_reader()
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

    async def _wait_for_session(self, reason: str) -> None:
        """DISABLED BY DEFAULT — pairing does not need a session, and waiting for one is actively harmful.

        Added 2026-08-17 to stop the startup pairing run firing authed /leagues/*/markets/straight calls into
        guest redirects. That diagnosis was wrong: pair_pinnacle.py talks ONLY to the sidecar's /catalog, which
        is `_guest_get` — account-free. The redirect burst came from the reseed / league-seed path, which is
        now gated properly at `_authed_rest_blocked`. Every pairing step (Kalshi scaffold, Pinnacle catalog
        fill, derivatives) works with no session at all.

        The cost of the wait was not merely a wasted 90s. Under PINNACLE_LIFECYCLE the browser is DARK between
        scheduled windows, so the session cannot prove itself for hours — and the lifecycle PLAN is built from
        the paired games this run produces. Delaying pairing therefore delays the very file the schedule reads,
        which is the opposite of what a startup run is for.

        Kept behind an explicit opt-in (`wait_ready_sec` > 0 AND HARDVEN_PAIR_WAIT_SESSION=1) rather than
        deleted, so a venue whose catalog IS authed can turn it back on."""
        evt = self._wait_ready
        if evt is None or self._wait_ready_sec <= 0:
            return
        if os.environ.get("HARDVEN_PAIR_WAIT_SESSION") != "1":
            return
        try:
            if evt.is_set():
                return
            print(f"[PAIR SCHED] {reason}: waiting up to {self._wait_ready_sec:.0f}s for the venue session "
                  f"(HARDVEN_PAIR_WAIT_SESSION=1).")
            await asyncio.wait_for(evt.wait(), timeout=self._wait_ready_sec)
            print("[PAIR SCHED] session proven - pairing now.")
        except asyncio.TimeoutError:
            print(f"[PAIR SCHED] session still unproven after {self._wait_ready_sec:.0f}s - pairing anyway.")
        except Exception:
            pass

    async def _pair_once(self, reason: str) -> None:
        sports = os.environ.get("HARDVEN_SPORTS") or "<all enabled>"
        print(f"[PAIR SCHED] {reason} pairing run — sports={sports}")
        await self._wait_for_session(reason)
        if self._steps is not None:
            for label, script_args, cwd in self._steps:
                await self._run_step(label, script_args, cwd)
            print(f"[PAIR SCHED] {reason} pairing run complete.")
            return
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
