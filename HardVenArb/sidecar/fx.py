"""
fx.py — live FX rate for the book's account currency → USD.

WHY THIS EXISTS: the rate used to be a hand-set env var (HARDVEN_FX_TO_USD). On 2026-08-06 it read 1.08
while EUR/USD was 1.1542 — 6.9% stale. That error goes straight into stake sizing (stakeEUR = stakeUSD / fx),
so the Pinnacle leg was over-staked by ~7%, which silently turns a hedged arb into a DIRECTIONAL position:
the two legs no longer pay the same amount, and the "risk-free" property is gone even though the detection
maths still looks fine. A number that must be right and is maintained by hand will eventually be wrong.

DESIGN — fail toward the configured value, never toward a guess:
  * two independent public sources; first good answer wins (no key, no quota)
  * a SANITY BAND around the env baseline (default ±25%): a mangled/garbage response can never resize bets
  * on total failure the env value stands and the state says `stale` — the bot keeps trading on the last
    good number rather than halting or silently drifting
  * the account currency comes from the ADAPTER (Pinnacle reports it on /balance), so a USD account
    short-circuits to 1.0 instead of applying a EUR rate to dollars
"""
from __future__ import annotations

import asyncio
import os
import time

import httpx

SOURCES = [
    ("frankfurter", "https://api.frankfurter.app/latest?from={base}&to=USD",
     lambda d: float((d.get("rates") or {}).get("USD"))),
    ("er-api", "https://open.er-api.com/v6/latest/{base}",
     lambda d: float((d.get("rates") or {}).get("USD"))),
]


class FxProvider:
    def __init__(self, currency_fn=None) -> None:
        self._currency_fn = currency_fn          # () -> account currency code, e.g. "EUR"
        self._env = _read_env_rate()
        self._rate = self._env
        self._source = "env"
        self._ts = 0.0
        self._err = ""
        self._band = float(os.environ.get("HARDVEN_FX_MAX_DEVIATION", "0.25") or 0.25)
        self._refresh_sec = float(os.environ.get("HARDVEN_FX_REFRESH_SEC", "3600") or 3600)
        self._task: asyncio.Task | None = None

    def currency(self) -> str:
        try:
            c = (self._currency_fn() or "").strip().upper() if self._currency_fn else ""
        except Exception:
            c = ""
        return c or (os.environ.get("HARDVEN_CURRENCY") or "EUR").strip().upper()

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass

    async def refresh(self) -> dict:
        base = self.currency()
        if base == "USD":                         # a USD account needs no conversion at all
            self._rate, self._source, self._ts, self._err = 1.0, "usd-account", time.time(), ""
            return self.status()
        for name, url, pick in SOURCES:
            try:
                async with httpx.AsyncClient(timeout=12.0) as c:
                    r = await c.get(url.format(base=base), follow_redirects=True)
                if r.status_code != 200:
                    self._err = f"{name} HTTP {r.status_code}"
                    continue
                rate = pick(r.json())
                if not (rate and rate > 0):
                    self._err = f"{name} returned no usable rate"
                    continue
                # SANITY BAND: a wire glitch or a changed schema must not be allowed to resize real bets.
                if self._env > 0 and abs(rate - self._env) / self._env > self._band:
                    self._err = (f"{name} rate {rate:.4f} deviates >{self._band:.0%} from the configured "
                                 f"{self._env:.4f} - REJECTED (raise HARDVEN_FX_MAX_DEVIATION if the "
                                 "configured value is simply very old)")
                    print(f"[FX] {self._err}")
                    continue
                drift = (rate / self._env - 1.0) if self._env > 0 else 0.0
                self._rate, self._source, self._ts, self._err = rate, name, time.time(), ""
                if abs(drift) > 0.02:
                    print(f"[FX] {base}/USD = {rate:.4f} via {name} - the configured HARDVEN_FX_TO_USD "
                          f"({self._env:.4f}) is off by {drift:+.1%}. Using the LIVE rate; update the env so "
                          "a fetch outage falls back to something current.")
                return self.status()
            except Exception as ex:
                self._err = f"{name}: {type(ex).__name__}: {ex}"
        print(f"[FX] all sources failed ({self._err}) - keeping {self._rate:.4f} from {self._source}")
        return self.status()

    async def _run(self) -> None:
        while True:
            try:
                await self.refresh()
            except asyncio.CancelledError:
                return
            except Exception as ex:
                print(f"[FX] refresh error: {type(ex).__name__}: {ex}")
            try:
                await asyncio.sleep(self._refresh_sec)
            except asyncio.CancelledError:
                return

    @property
    def rate(self) -> float:
        return self._rate

    def status(self) -> dict:
        age = (time.time() - self._ts) if self._ts else None
        return {
            "currency": self.currency(), "rate": round(self._rate, 6), "source": self._source,
            "age_sec": round(age) if age is not None else None,
            "stale": bool(age is None or age > self._refresh_sec * 3),
            "env_rate": self._env,
            "env_drift_pct": round((self._rate / self._env - 1.0) * 100, 3) if self._env > 0 else None,
            "last_error": self._err,
            "max_deviation": self._band,
        }


def _read_env_rate() -> float:
    try:
        v = float(os.environ.get("HARDVEN_FX_TO_USD", "1.0") or 1.0)
        return v if v > 0 else 1.0
    except ValueError:
        return 1.0
