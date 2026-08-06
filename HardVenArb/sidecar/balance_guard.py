"""
balance_guard.py — stop trading when EITHER leg's bankroll runs low.

A cross-venue arb needs BOTH legs funded. If the Pinnacle wallet empties, every detected arb fires a naked
Kalshi leg (directional exposure, not arbitrage); if the Kalshi balance empties, the same in reverse. So the
guard halts the SCHEDULE (browser closes, bot stays alive) and alerts, rather than letting the bot keep
finding arbs it cannot complete.

BOTH sides are judged here, in one place, even though only one is locally visible:
  * Pinnacle  — polled directly via adapter.balance() (account currency, e.g. EUR)
  * Kalshi    — PUSHED by the C# bot to POST /control/balance (the sidecar has no Kalshi credentials)

A Kalshi figure that stops arriving is treated as UNKNOWN, never as zero — a dead push path must not look
like an empty account and halt a healthy bot. It goes stale after KALSHI_BALANCE_STALE_SEC and is reported
as stale in the status instead of tripping the guard.

    HARDVEN_MIN_BALANCE       floor for the Pinnacle wallet, in ACCOUNT currency (default 10)
    HARDVEN_MIN_BALANCE_USD   floor for the Kalshi balance, USD (default 10)
    HARDVEN_BALANCE_CHECK_SEC how often to poll Pinnacle (default 300)
    HARDVEN_BALANCE_GUARD     0 = monitor + report but never halt (default 1 = halt)
"""
from __future__ import annotations

import asyncio
import os
import time


class BalanceGuard:
    def __init__(self, adapter, lifecycle, notifier) -> None:
        self._adapter = adapter
        self._lifecycle = lifecycle
        self._notify = notifier
        self._min_book = float(os.environ.get("HARDVEN_MIN_BALANCE", "10") or 10)
        self._min_kalshi = float(os.environ.get("HARDVEN_MIN_BALANCE_USD", "10") or 10)
        self._check_sec = float(os.environ.get("HARDVEN_BALANCE_CHECK_SEC", "300") or 300)
        self._stale_sec = float(os.environ.get("KALSHI_BALANCE_STALE_SEC", "900") or 900)
        self._enabled = os.environ.get("HARDVEN_BALANCE_GUARD", "1") != "0"
        self._book_bal: float | None = None
        self._book_ts = 0.0
        self._kalshi_bal: float | None = None
        self._kalshi_ts = 0.0
        self._task: asyncio.Task | None = None
        self._low_warned = False

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run())
            mode = "HALT" if self._enabled else "report-only"
            print(f"[BALANCE] guard on ({mode}): book floor {self._min_book:g}, "
                  f"kalshi floor ${self._min_kalshi:g}, every {self._check_sec:g}s")

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass

    def push_kalshi(self, balance: float) -> dict:
        """The C# bot reports its Kalshi cash here (it owns those credentials)."""
        self._kalshi_bal = float(balance)
        self._kalshi_ts = time.time()
        return self.status()

    def _kalshi_fresh(self) -> bool:
        return self._kalshi_bal is not None and (time.time() - self._kalshi_ts) < self._stale_sec

    def _breaches(self) -> list[str]:
        """Which floors are breached RIGHT NOW. Unknown/stale figures are never a breach."""
        out = []
        if self._book_bal is not None and self._book_bal < self._min_book:
            out.append(f"{self._adapter.name} wallet {self._book_bal:.2f} < floor {self._min_book:g}")
        if self._kalshi_fresh() and self._kalshi_bal < self._min_kalshi:
            out.append(f"Kalshi ${self._kalshi_bal:.2f} < floor ${self._min_kalshi:g}")
        return out

    async def check_now(self) -> dict:
        try:
            self._book_bal = float(await self._adapter.balance())
            self._book_ts = time.time()
        except Exception as ex:
            print(f"[BALANCE] book balance read failed: {type(ex).__name__}: {ex}")   # unknown ≠ zero
        breaches = self._breaches()
        if breaches and self._enabled and self._lifecycle is not None:
            await self._lifecycle.halt("low balance: " + "; ".join(breaches))
        elif breaches and not self._low_warned:
            self._low_warned = True
            msg = "⚠️ **LOW BALANCE** (guard is report-only): " + "; ".join(breaches)
            print(f"[BALANCE] {msg}")
            if self._notify is not None and self._notify.enabled:
                self._notify.send_bg(msg)
        elif not breaches:
            self._low_warned = False
        return self.status()

    async def _run(self) -> None:
        await asyncio.sleep(30)                       # let the session/login settle before the first read
        while True:
            try:
                await self.check_now()
            except asyncio.CancelledError:
                return
            except Exception as ex:
                print(f"[BALANCE] check error: {type(ex).__name__}: {ex}")
            try:
                await asyncio.sleep(self._check_sec)
            except asyncio.CancelledError:
                return

    def status(self) -> dict:
        return {
            "enabled": self._enabled,
            "book": {"balance": self._book_bal, "floor": self._min_book,
                     "age_sec": round(time.time() - self._book_ts) if self._book_ts else None},
            "kalshi": {"balance": self._kalshi_bal, "floor": self._min_kalshi,
                       "age_sec": round(time.time() - self._kalshi_ts) if self._kalshi_ts else None,
                       "fresh": self._kalshi_fresh()},
            "breaches": self._breaches(),
        }
