"""
sports.py — the SINGLE source of truth for which sports the bot runs and how each maps across the two venues.

"The sports we run may change each time" used to mean editing 4 places (schedule --sports, PINNACLE_LIFECYCLE_
SPORTS, pairHard's CLASSIC_SERIES, pair_derivatives' KALSHI_SERIES). Now every consumer reads from here:
  - schedule.py / lifecycle / adapter  → Pinnacle sport ids, display names, per-sport game DURATION
  - pair_derivatives.py                → Kalshi spread/total series -> ("spread"|"total", Pinnacle sport id)
  - pairHard.py                        → Kalshi moneyline series allowlist (the default scaffold scope)

ADD a sport = add one entry to CATALOG. CHANGE the active set = set env HARDVEN_SPORTS (comma keys, e.g.
"baseball,tennis" or just "tennis"), or flip `enabled=` in the catalog. Unset HARDVEN_SPORTS = every enabled
entry below. Everything downstream follows from that one choice.
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass


@dataclass(frozen=True)
class Sport:
    key: str                      # our name, e.g. "baseball"
    pinnacle_id: int              # Pinnacle sport id (3 = baseball, 33 = tennis)
    duration_min: int             # typical match length (minutes) — the window's post-game tail
    moneyline: tuple[str, ...]    # Kalshi per-game WINNER series (pairHard allowlist)
    spread: tuple[str, ...]       # Kalshi SPREAD series (pair_derivatives)
    total: tuple[str, ...]        # Kalshi TOTAL series (pair_derivatives)
    enabled: bool = True


# ── the catalog (add a sport = add an entry) ──────────────────────────────────────────────────────────────
CATALOG: dict[str, Sport] = {
    "baseball": Sport(
        key="baseball", pinnacle_id=3, duration_min=210,
        moneyline=("KXMLBGAME", "KXKBOGAME", "KXNPBGAME"),   # MLB / Korea KBO / Japan NPB
        spread=("KXMLBSPREAD", "KXKBOSPREAD"),               # NPB has no Kalshi spread/total
        total=("KXMLBTOTAL", "KXKBOTOTAL"),
    ),
    "tennis": Sport(
        key="tennis", pinnacle_id=33, duration_min=180,
        moneyline=("KXATPMATCH", "KXWTAMATCH", "KXITFMATCH", "KXITFWMATCH",
                   "KXATPCHALLENGERMATCH", "KXWTACHALLENGERMATCH"),
        spread=("KXATPGSPREAD",),   # games handicap — ATP only (WTA/ITF have no Kalshi games markets)
        total=("KXATPGTOTAL",),
    ),
    "soccer": Sport(
        key="soccer", pinnacle_id=29, duration_min=150,   # 90' + half + stoppage + settle tail
        # 3-way (home/draw/away) — the catalog + pair_pinnacle tag soccer three_way and pair NO-only
        # (Kalshi NO + Pinnacle back-this-outcome). All confirmed "Team/Team/Tie" per-game winner series.
        # In-season drivers: MLS, Liga MX, UCL qualifiers (July), World Cup (live now, "Regulation Time
        # Moneyline"). Plus USL/USL Cup + off-season club leagues (La Liga 2, Chile Primera, Bolivia Primera)
        # that fill when they run.
        moneyline=("KXWCGAME", "KXMLSGAME", "KXLIGAMXGAME", "KXUCLGAME",
                   "KXUSLGAME", "KXUSLCUPGAME", "KXLALIGA2GAME", "KXCHLLDPGAME", "KXBOLPDIVGAME"),
        spread=(),   # soccer Asian-handicap / goal-totals derivatives = future work (3-way base needs its own pairing)
        total=(),
    ),
}


# ── the active set ────────────────────────────────────────────────────────────────────────────────────────
def enabled_sports() -> list[Sport]:
    """The sports the bot runs THIS session: env HARDVEN_SPORTS (comma keys) if set to any known key, else
    every entry marked enabled in the catalog. Unknown keys are warned + skipped (never crash)."""
    sel = (os.environ.get("HARDVEN_SPORTS") or "").strip()
    if sel:
        keys = [k.strip().lower() for k in sel.split(",") if k.strip()]
        unknown = [k for k in keys if k not in CATALOG]
        if unknown:
            print(f"[SPORTS] HARDVEN_SPORTS: unknown key(s) {unknown}; known: {sorted(CATALOG)}", file=sys.stderr)
        chosen = [CATALOG[k] for k in keys if k in CATALOG]
        if chosen:
            return chosen
    return [s for s in CATALOG.values() if s.enabled]


def pinnacle_ids() -> list[int]:
    """Pinnacle sport ids for the active sports (schedule / lifecycle / adapter)."""
    return [s.pinnacle_id for s in enabled_sports()]


def name_by_id() -> dict[int, str]:
    """Pinnacle id -> our name, for the WHOLE catalog (display + duration lookup never miss on an id)."""
    return {s.pinnacle_id: s.key for s in CATALOG.values()}


def duration_by_name() -> dict[str, int]:
    """Our name -> game-length minutes, WHOLE catalog (the window tail)."""
    return {s.key: s.duration_min for s in CATALOG.values()}


def moneyline_series() -> set[str]:
    """Kalshi per-game winner series for the active sports — pairHard's default scaffold allowlist."""
    return {m for s in enabled_sports() for m in s.moneyline}


def derivative_series() -> dict[str, tuple[str, int]]:
    """Kalshi spread/total series -> ('spread'|'total', pinnacle_id) for the active sports — pair_derivatives."""
    out: dict[str, tuple[str, int]] = {}
    for s in enabled_sports():
        for ser in s.spread:
            out[ser] = ("spread", s.pinnacle_id)
        for ser in s.total:
            out[ser] = ("total", s.pinnacle_id)
    return out


if __name__ == "__main__":
    act = enabled_sports()
    print(f"[SPORTS] active: {[s.key for s in act]}  (HARDVEN_SPORTS={os.environ.get('HARDVEN_SPORTS') or '<all enabled>'})")
    print(f"  pinnacle ids   : {pinnacle_ids()}")
    print(f"  moneyline      : {sorted(moneyline_series())}")
    print(f"  derivatives    : {derivative_series()}")
