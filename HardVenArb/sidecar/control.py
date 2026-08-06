"""
control.py — PERSISTENT operator control state for remote (Discord) commands.

Everything an operator can change at runtime lives here and is written to control_state.json, so it survives
a sidecar restart. That persistence is a SAFETY property, not a convenience: if you pause the bot from your
phone and the sidecar restarts an hour later, it must come back paused. A pause that quietly forgets itself
is worse than no pause at all — the same reasoning applies to a balance-guard halt.

Holds three things:
  pins      — operator-pinned local hour ranges ("09:00-12:00"), merged into the schedule
  override  — None | "paused" | "halted" (balance guard) | "forced" (open outside the schedule, until a time)
  toggles   — env-backed flags flipped at runtime (HARDVEN_BET_ENABLE etc.)

TOGGLE SCOPE (read this before promising an operator a switch): a toggle here only works for flags read
LIVE by the sidecar, e.g. HARDVEN_BET_ENABLE (checked on every placement). Flags the C# executor reads ONCE
at construction into readonly fields (HARDVEN_REQUIRE_WS_VERIFIED, HARDVEN_MONEYLINE_ONLY, HARDVEN_PRELIVE_ONLY,
HARDVEN_EXEC_NET_FLOOR, HARDVEN_MAX_STAKE, …) cannot be changed by writing os.environ here — they need a bot
restart. LIVE_TOGGLES below is the honest allowlist; anything else is rejected with that explanation rather
than silently accepted.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from env_util import atomic_write_json

STATE_PATH = Path(__file__).resolve().parent.parent / "control_state.json"

# Sidecar-side flags that take effect IMMEDIATELY (re-read per use). Value = one-line description for `toggles`.
LIVE_TOGGLES = {
    "HARDVEN_BET_ENABLE":     "arm/disarm REAL bet placement (sidecar refuses to place when 0)",
    "HARDVEN_BETSLIP_TRIM":   "auto-clear the bet slip (post-bet + idle sweeps)",
    "HARDVEN_LOG_ODDS":       "log the /odds access lines",
    "HARDVEN_TAB_ORGANIC":    "organic activity on reader tabs",
    "HARDVEN_ORGANIC_FOCUS":  "raise windows on organic focus (taskbar flash)",
    "PINNACLE_DEBUG_WS":      "verbose WS odds-update logging",
}

# Flags the C# bot reads once at startup — listed so the operator gets a real answer, not a silent no-op.
RESTART_TOGGLES = {
    "HARDVEN_REQUIRE_WS_VERIFIED", "HARDVEN_MONEYLINE_ONLY", "HARDVEN_PRELIVE_ONLY", "HARDVEN_EXEC_NET_FLOOR",
    "HARDVEN_MAX_STAKE", "HARDVEN_STAKE_MAX", "HARDVEN_STAKE_MIN_RUNG", "HARDVEN_FAVORITE_KALSHI_SPORTS",
    "HARDVEN_MAX_DEPTH_FRACTION", "HARDVEN_EARLY_EXIT", "HARDVEN_HEDGE_MONITOR_SECS",
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat() + "Z"


class ControlState:
    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path else STATE_PATH
        self.pins: list[str] = []
        self.override: str | None = None          # None | paused | halted | forced
        self.override_until: str | None = None    # ISO-Z, for "forced"
        self.reason: str = ""
        self.since: str = ""
        self.toggles: dict[str, str] = {}
        self.schedule: dict = {}                  # runtime schedule-knob overrides
        self.load()

    # ── persistence ───────────────────────────────────────────────────────────
    def load(self) -> None:
        try:
            d = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except Exception as ex:
            print(f"[CONTROL] {self.path.name} unreadable ({type(ex).__name__}: {ex}) - starting clean")
            return
        self.pins = list(d.get("pins") or [])
        self.override = d.get("override")
        self.override_until = d.get("override_until")
        self.reason = d.get("reason", "")
        self.since = d.get("since", "")
        self.toggles = dict(d.get("toggles") or {})
        self.schedule = dict(d.get("schedule") or {})
        if self.override:
            print(f"[CONTROL] restored override={self.override!r} (since {self.since}) reason={self.reason!r} "
                  "- an operator pause/halt SURVIVES a restart by design; send 'resume' to clear it.")
        self.apply_toggles()

    def save(self) -> None:
        try:
            atomic_write_json(self.path, {
                "pins": self.pins, "override": self.override, "override_until": self.override_until,
                "reason": self.reason, "since": self.since, "toggles": self.toggles,
                "schedule": self.schedule, "saved_at": _now_iso(),
            })
        except Exception as ex:
            print(f"[CONTROL] could not save {self.path.name}: {type(ex).__name__}: {ex}")

    # ── toggles ───────────────────────────────────────────────────────────────
    def apply_toggles(self) -> None:
        for k, v in self.toggles.items():
            if k in LIVE_TOGGLES:
                os.environ[k] = str(v)

    def set_toggle(self, key: str, value: str) -> dict:
        key = (key or "").strip().upper()
        value = str(value).strip()
        if key in LIVE_TOGGLES:
            os.environ[key] = value
            self.toggles[key] = value
            self.save()
            return {"ok": True, "key": key, "value": value, "effect": "immediate"}
        if key in RESTART_TOGGLES:
            # Record it so the operator can see intent, but be explicit that it is NOT live.
            self.toggles[key] = value
            self.save()
            return {"ok": False, "key": key, "value": value, "effect": "needs-restart",
                    "detail": f"{key} is read once by the C# bot at startup — saved, but it will not take "
                              "effect until the bot is restarted."}
        return {"ok": False, "key": key, "error": "unknown toggle",
                "live": sorted(LIVE_TOGGLES), "restart_only": sorted(RESTART_TOGGLES)}

    def toggle_view(self) -> dict:
        return {
            "live": {k: {"value": os.environ.get(k, ""), "desc": d} for k, d in sorted(LIVE_TOGGLES.items())},
            "restart_only": {k: os.environ.get(k, "") for k in sorted(RESTART_TOGGLES)},
            "overridden": dict(self.toggles),
        }

    # ── override ──────────────────────────────────────────────────────────────
    def set_override(self, mode: str | None, reason: str = "", until_iso: str | None = None) -> None:
        self.override = mode
        self.reason = reason
        self.override_until = until_iso
        self.since = _now_iso() if mode else ""
        self.save()

    def view(self) -> dict:
        return {"pins": self.pins, "override": self.override, "override_until": self.override_until,
                "reason": self.reason, "since": self.since, "schedule": self.schedule,
                "state_file": str(self.path)}
