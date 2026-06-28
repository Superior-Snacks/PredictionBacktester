"""
organic.py — human-like idle behaviour for the managed Pinnacle tab.

Replaces the robotic "tiny mouse nudge every 200s" keepalive (a perfectly periodic micro-movement is itself a
faint tell) with VARIED, NON-PERIODIC activity: irregular idle gaps (occasionally a long "stepped away" gap),
short mouse paths, scrolls (down then sometimes back up), and the odd navigation to another sport page. The
goal on PINNACLE is modest — Pinnacle is sharp-friendly and doesn't limit/ban winners, so this is about not
looking like an obvious headless scraper + holding the session, NOT impersonating every human twitch (the
heavy-mimicry payoff comes later with soft books). Pragmatic, not paranoid.

INTERRUPTIBLE FOR EXECUTION: organic activity and a sub-second bet do NOT conflict — they're separate modes. A
sharp human is also fast; the tell is REGULARITY, not speed. Call pause() before placing a bet (so an in-flight
scroll/move can't fight the bet click on the single page), execute, then resume(). While paused the loop blocks.
"""
from __future__ import annotations

import asyncio
import random
from collections import Counter


class OrganicActivity:
    def __init__(self, page, browse_urls: list[str] | None = None,
                 min_gap: float = 20.0, max_gap: float = 150.0,
                 long_gap_chance: float = 0.15, long_gap_max: float = 900.0):
        self._page = page
        self._urls = list(browse_urls or [])
        self._min_gap, self._max_gap = min_gap, max_gap
        self._long_gap_chance, self._long_gap_max = long_gap_chance, long_gap_max
        self._gate = asyncio.Event()
        self._gate.set()                 # set = active; cleared = paused (during a bet)
        self.actions = Counter()         # telemetry / test visibility

    # ── execution interlock ───────────────────────────────────────────────────────
    def pause(self) -> None:
        self._gate.clear()

    def resume(self) -> None:
        self._gate.set()

    # ── the loop ──────────────────────────────────────────────────────────────────
    def _next_gap(self) -> float:
        """Mostly short irregular gaps; occasionally a long 'stepped away' one. Never a fixed cadence."""
        if random.random() < self._long_gap_chance:
            return random.uniform(self._max_gap, self._long_gap_max)
        return random.uniform(self._min_gap, self._max_gap)

    async def tick(self) -> str:
        """Do ONE organic action; returns its name (so a test can drive it deterministically). All actions are
        best-effort — a failure on a weird page must never crash the loop."""
        roll = random.random()
        try:
            if roll < 0.40:
                name = "idle"                       # most ticks: do nothing (a human isn't always acting)
            elif roll < 0.65:
                name = "mouse"
                await self._page.mouse.move(random.randint(80, 1280), random.randint(80, 760),
                                            steps=random.randint(3, 14))   # a short path, not a teleport
            elif roll < 0.88:
                name = "scroll"
                await self._page.mouse.wheel(0, random.randint(200, 850))
                if random.random() < 0.5:
                    await asyncio.sleep(random.uniform(0.4, 2.0))
                    await self._page.mouse.wheel(0, -random.randint(100, 450))   # glance back up
            elif self._urls:
                name = "navigate"
                await self._page.goto(random.choice(self._urls), wait_until="domcontentloaded", timeout=30_000)
            else:
                name = "mouse"
                await self._page.mouse.move(random.randint(80, 1280), random.randint(80, 760),
                                            steps=random.randint(3, 14))
        except Exception:
            name = "error"
        self.actions[name] += 1
        return name

    async def run(self) -> None:
        """Background loop: wait an irregular gap, then one action — pausing entirely while a bet is in flight.
        Cancel the task to stop (matches the adapter's other background tasks)."""
        while True:
            try:
                await self._gate.wait()                      # block while paused for execution
                await asyncio.sleep(self._next_gap())
            except asyncio.CancelledError:
                break
            if not self._gate.is_set():                      # paused during the sleep → don't act this tick
                continue
            await self.tick()
