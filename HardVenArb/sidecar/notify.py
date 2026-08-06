"""
notify.py — fire-and-forget Discord webhook poster for the SIDECAR.

Mirrors the C# DiscordNotifier (same DISCORD_WEBHOOK_URL, same best-effort contract: never raises, never
retries, never blocks trading) so schedule alerts land in the SAME channel as the bot's trade alerts.

Why the sidecar posts these rather than the C# bot: the lifecycle OWNS the open/close transition and is the
only place that knows WHY — which games a window is for and which are being skipped. The C# side only sees a
`scheduled_dark` flag on /odds, which can say "dark" but never "dark because the next 4 matches are ITF games
we have no Kalshi pair for".

Disabled (no-op) when DISCORD_WEBHOOK_URL is unset, so nothing here is required for the bot to run.
"""
from __future__ import annotations

import asyncio
import os

import httpx

_MAX = 1900          # Discord hard-caps message content at 2000 chars


class Notifier:
    def __init__(self, prefix: str = "HardVen/sidecar") -> None:
        self._url = (os.environ.get("DISCORD_WEBHOOK_URL") or "").strip()
        self._prefix = f"**[{prefix}]** "
        self._fails = 0

    @property
    def enabled(self) -> bool:
        return bool(self._url)

    async def send(self, message: str) -> bool:
        """Post one message. Returns False on any failure — callers ignore it (alerting must never be able to
        disturb the session)."""
        if not self._url:
            return False
        content = self._prefix + message
        if len(content) > _MAX:
            content = content[:_MAX] + "…"
        try:
            async with httpx.AsyncClient(timeout=10.0) as c:
                r = await c.post(self._url, json={"content": content})
            if r.status_code >= 300:
                self._fails += 1
                if self._fails in (1, 10) or self._fails % 50 == 0:
                    print(f"[NOTIFY] discord HTTP {r.status_code} (x{self._fails})")
                return False
            return True
        except Exception as ex:
            self._fails += 1
            if self._fails in (1, 10) or self._fails % 50 == 0:
                print(f"[NOTIFY] discord send failed x{self._fails}: {type(ex).__name__}: {ex}")
            return False

    def send_bg(self, message: str) -> None:
        """Fire-and-forget from sync code (the lifecycle's transition path). Schedules the POST on the running
        loop and returns immediately; if there is no loop, drops the message rather than blocking."""
        try:
            asyncio.get_running_loop().create_task(self.send(message))
        except RuntimeError:
            pass
