"""In-play personality for the Pinnacle bot: ONE live tab, no tab manager, camp-aware idle behaviour.

WHY A SEPARATE MODE RATHER THAN A FLAG ON THE EXISTING ONE. Pre-live and in-play want opposite things.
Pre-live spreads across many leagues, so it needs the tab manager, roving and per-tab organic. In-play
concentrates: 206 windows measured on 2026-08-16 came from 13 pairs, none produced a single isolated
arb, 94% were repeats, median gap 41s. The winning move there is to park on the live list and stay —
and a tab flip mid-camp is exactly what loses the window the camp exists to catch.

Three behaviours, and the third is the one that does not exist anywhere else:

  1. SCROLL, UP-BIASED. Down a little, up more. Never come to rest at the bottom of the list, because a
     session parked at the end of a scroll region is neither useful (the live games are at the top) nor
     human (people return to what they are watching).
  2. PEEK AT SLIPS, RANDOMLY. Open a Quick Bet on an arbitrary visible market, hold it a few seconds,
     close it. Cover for the real thing, and it keeps the interaction warm. Deliberately RANDOM rather
     than concentrated on the camped game: repeatedly opening one match's slip and no other is a far
     more distinctive pattern than browsing.
  3. HOVER WHILE CAMPED. Once a slip is armed the cursor must STAY on it — a person holding a mouse over
     a betslip they are about to click drifts by a pixel or two, they do not park at a fixed coordinate
     and they do not wander off to scroll something else. So camping swaps browsing for drift.

Nothing here places a bet. `peek` opens and closes; the armed slip belongs to the camper.
"""
from __future__ import annotations

import asyncio
import os
import random
import time
from typing import Callable, Optional

# Established by the existing organic layer: `market-btn` is the odds-button set ONLY, never a Place Bet
# control, and the Quick Bet renders into `#quick-bet-portal`.
ODDS_BTN = "button.market-btn"
PORTAL = "#quick-bet-portal"
CLOSE_BTN = (f'{PORTAL} button[aria-label*="Remove"], {PORTAL} button[aria-label*="Close"]')

def _derive_live_url() -> str:
    """The in-play board FOR THE SPORT WE TRADE, from the one place that decides it: HARDVEN_SPORTS.

    This was hardcoded to tennis. Everything else in the stack already followed `sports.active()` — the
    catalog, the scheduler, the pairing allowlist — so switching HARDVEN_SPORTS to soccer moved every one of
    them and left the browser sitting on the tennis live board, which the drift watchdog then dutifully
    enforced every few minutes. The page and the feed were watching different sports.

    Pinnacle spells the sport the same way `sports.py` keys it (`/en/tennis/`, `/en/soccer/`), which is the
    assumption `_derive_home_url` already makes. `PINNACLE_INPLAY_URL` still overrides for anything else.
    """
    explicit = (os.environ.get("PINNACLE_INPLAY_URL") or "").strip()
    if explicit:
        return explicit
    try:
        import sports as _sports_cfg
        keys = [s.key for s in _sports_cfg.enabled_sports()]
        if keys:
            return f"https://www.pinnacle.bet/en/{keys[0]}/matchups/live/"
    except Exception:
        pass
    return "https://www.pinnacle.bet/en/tennis/matchups/live/"


LIVE_URL = _derive_live_url()


class InPlayActivity:
    """Idle behaviour for a single parked live tab.

    `click_fn(page, locator) -> bool` is injected rather than reimplemented, so this benefits from every
    improvement to the shared humanised click (off-centre targeting, wheel-to-view, dwell, and the
    recorded-trajectory replay when that lands) instead of drifting into a second implementation.
    """

    def __init__(self, page, click_fn: Callable, log: Callable[[str], None],
                 gate: Optional[asyncio.Event] = None,
                 on_lost: Optional[Callable] = None):
        self._page = page
        self._click = click_fn
        self._log = log
        self._on_lost = on_lost          # async callback: the armed popover vanished
        self._gate = gate or asyncio.Event()
        self._gate.set()
        self._camping = False
        self._task: Optional[asyncio.Task] = None
        self._scroll_pos = 0.0          # our own estimate; the page is not asked (that would be main-world)
        self._peeks = 0
        self._min_gap = float(os.environ.get("PINNACLE_INPLAY_MIN_GAP", "12"))
        self._max_gap = float(os.environ.get("PINNACLE_INPLAY_MAX_GAP", "45"))
        self._peek_chance = float(os.environ.get("PINNACLE_INPLAY_PEEK_CHANCE", "0.35"))

    # ── lifecycle ────────────────────────────────────────────────────────────
    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop())
            self._log(f"in-play idle ON (scroll + random slip peeks; gap {self._min_gap:.0f}-"
                      f"{self._max_gap:.0f}s, peek chance {self._peek_chance:.0%})")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    def pause(self) -> None:
        self._gate.clear()

    def resume(self) -> None:
        self._gate.set()

    def set_camping(self, on: bool) -> None:
        """Camping swaps browsing for hovering. Called by the camper, not decided here."""
        if on != self._camping:
            self._log(f"in-play idle -> {'HOVER (camped)' if on else 'BROWSE'}")
        self._camping = on

    # ── behaviours ───────────────────────────────────────────────────────────
    async def _keyboard_scroll(self) -> None:
        """Scroll with the KEYBOARD, not the wheel — this is the part that keeps the session alive.

        Pinnacle's ~30-minute idle logout is UI-based, and it was established during the keepalive work
        that mouse movement, wheel scrolling and authed API calls do NOT reset it; keyboard input and
        navigation clicks do. In-play mode pauses the session's own organic (it was dragging the page
        off the live list), so this loop has to carry the keepalive itself or the camp gets logged out
        mid-session — which would look like the venue killing a long-lived slip.
        """
        keys = ["PageDown"] * random.randint(1, 3) + ["PageUp"] * random.randint(1, 4)
        if random.random() < 0.4:
            keys.append("Home")                      # back to the top, where the live games are
        for k in keys:
            if not self._gate.is_set():
                return
            try:
                await self._page.keyboard.press(k)
            except Exception:
                return
            await asyncio.sleep(random.uniform(0.25, 1.1))

    async def _pin_url(self) -> None:
        """Return to the live list if anything navigated away.

        Belt and braces: the session organic is paused while in-play runs, but a click can still follow
        a link, and a camp that has quietly drifted onto the pre-match page produces no arbs and no error.

        GIVES UP IF THE URL ITSELF IS WRONG. If PINNACLE_INPLAY_URL redirects — a stale path, a locale the
        account does not have, a sport with no live list right now — then goto lands somewhere else, the next
        tick sees the drift again, and this navigates on a loop every few seconds forever. That reads exactly
        like "the page keeps going back to the matchup page" while actually being this function fighting a
        redirect. A URL that does not hold after a real navigation is a CONFIGURATION error, so say so once
        and stop, rather than hammering the site to no effect.
        """
        if getattr(self, "_pin_broken", False):
            return
        try:
            cur = (self._page.url or "")
            if LIVE_URL.split("?")[0] in cur:
                return
            self._log(f"page drifted to {cur[:70]} — returning to the live list")
            await self._page.goto(LIVE_URL, wait_until="domcontentloaded")
            await asyncio.sleep(1.5)                      # let a redirect settle before judging
            landed = (self._page.url or "")
            if LIVE_URL.split("?")[0] not in landed:
                self._pin_broken = True
                self._log(f"PINNACLE_INPLAY_URL does not hold: navigating to {LIVE_URL} landed on "
                          f"{landed[:90]}. That is a redirect, not drift — so pinning is DISABLED for this "
                          f"session rather than looping on it. Open the live list by hand, copy the real URL, "
                          f"and set PINNACLE_INPLAY_URL to it.")
        except Exception:
            pass

    async def _scroll_cycle(self) -> None:
        """Down a bit, then up more. Net drift is upward, so the list never comes to rest at its end."""
        # Roughly a third of the time, use the keyboard instead — see _keyboard_scroll: it is the only
        # form of scrolling that resets the idle logout.
        if random.random() < 0.35:
            await self._keyboard_scroll()
            return
        down = random.uniform(160, 620)
        up = down + random.uniform(40, 320)          # ALWAYS returns further than it went
        for total, sign in ((down, 1), (up, -1)):
            done = 0.0
            while done < total:
                if not self._gate.is_set():
                    return
                step = min(random.uniform(90, 240), total - done)
                try:
                    await self._page.mouse.wheel(0, step * sign)
                except Exception:
                    return
                done += step
                await asyncio.sleep(random.uniform(0.04, 0.16))
            await asyncio.sleep(random.uniform(0.3, 1.4))    # people pause between flicks
        self._scroll_pos = max(0.0, self._scroll_pos + down - up)

    async def _peek_slip(self) -> None:
        """Open a RANDOM visible market's Quick Bet, look at it, close it. Places nothing."""
        try:
            btns = self._page.locator(ODDS_BTN)
            n = await btns.count()
        except Exception:
            return
        if not n:
            return
        try:
            if not await self._click(self._page, btns.nth(random.randrange(n))):
                return
        except Exception:
            return
        self._peeks += 1
        # Hold it the way a person reads a slip, then dismiss. Long enough to be a look, short enough
        # that an unattended open slip is never left lying around.
        await asyncio.sleep(random.uniform(2.5, 6.5))
        await self._close_slip()

    async def _close_slip(self) -> None:
        try:
            x = self._page.locator(CLOSE_BTN).first
            if await x.count() and await self._click(self._page, x):
                return
        except Exception:
            pass
        try:
            await self._page.keyboard.press("Escape")
        except Exception:
            pass

    async def _popover_alive(self) -> bool:
        """Is the Quick Bet still on the page? Locator count only — no JS, nothing the page can see."""
        # EMPTINESS, NOT EXISTENCE. `#quick-bet-portal` is always mounted — Pinnacle leaves the container
        # in the DOM and puts the popover INTO it — so a count-based test answers "the slip is alive"
        # forever. That is why a camp whose slip had died was never reported: this said it was fine.
        # Proven 2026-08-21 by a panel dump showing the portal present with no controls and no text.
        try:
            txt = await self._page.locator(PORTAL).first.inner_text()
            return bool((txt or "").strip())
        except Exception:
            return False

    async def _camped_keepalive(self) -> bool:
        """Keyboard-scroll the board UNDER the armed slip. Returns False if the slip did not survive.

        THE GAP THIS CLOSES. Camping did nothing that resets Pinnacle's idle logout. The camped branch ran
        `_hover_drift` and nothing else, and the keepalive work established that mouse movement does NOT
        reset the timer — keyboard input and navigation clicks do. So the longer a camp held, the closer it
        drifted to being logged out, and the symptom (a long-lived slip dying) looked like the venue killing
        it rather than the session simply expiring underneath.

        WHY THIS IS SAFE WHERE SCROLLING WAS NOT. The original rule was "no scrolling while armed" because a
        scroll moves the board under the slip. That is true of the BOARD and irrelevant to the popover: the
        Quick Bet is an overlay, not a row. What would genuinely be dangerous is keyboard input landing in
        the STAKE FIELD — PageDown in a focused number input edits the bet, not the page — so this refuses to
        run at all unless focus is on the body.

        The slip is re-checked afterwards, and a slip that did not survive is reported rather than assumed.
        """
        try:
            focused = await self._page.evaluate(
                "() => { const a = document.activeElement;"
                " return a ? (a.tagName + '|' + (a.getAttribute('type') || '')) : ''; }")
        except Exception:
            return True                       # cannot tell -> do nothing, and do not call the camp dead
        if focused and focused.split("|")[0].upper() in ("INPUT", "TEXTAREA", "SELECT"):
            return True                       # the stake field has focus; keys would edit the bet
        await self._keyboard_scroll()
        if not await self._popover_alive():
            self._log("the armed slip did not survive a keyboard scroll — treating the camp as lost.")
            return False
        return True

    async def _hover_drift(self) -> None:
        """Small drift over the armed slip — a held mouse, not a parked coordinate.

        A hand resting on a mouse never produces two identical positions: it wanders a couple of pixels,
        pauses, wanders back. Sitting perfectly still for minutes is as distinctive as teleporting.
        """
        try:
            box = await self._page.locator(PORTAL).first.bounding_box()
        except Exception:
            box = None
        if not box:
            return
        cx = box["x"] + box["width"] * random.uniform(0.35, 0.65)
        cy = box["y"] + box["height"] * random.uniform(0.35, 0.65)
        # LOOK AT THE OTHER SIDE NOW AND THEN. A person holding a slip glances at the opposing price —
        # it is the number that tells them whether their side still looks right. Purely a MOVE: the cursor
        # travels to the other cell and comes back, and nothing is ever pressed there.
        #
        # IT MUST NEVER BECOME A CLICK. Clicking the opposite odds button re-arms the slip on the OTHER
        # OUTCOME, and camp_fire would then buy the wrong side of the match against a Kalshi leg chosen for
        # this one — the same both-legs-on-one-outcome failure that cost real money on 2026-08-19, arrived
        # at from the other direction.
        if random.random() < 0.3:
            try:
                cells = self._page.locator("#quick-bet-portal button.market-btn")
                n = await cells.count()
                if n >= 2:
                    ob = await cells.nth(random.randint(0, n - 1)).bounding_box()
                    if ob:
                        await self._page.mouse.move(ob["x"] + ob["width"] * 0.5,
                                                    ob["y"] + ob["height"] * 0.5)
                        await asyncio.sleep(random.uniform(0.6, 1.8))
            except Exception:
                pass
        for _ in range(random.randint(2, 6)):
            if not self._gate.is_set():
                return
            try:
                await self._page.mouse.move(cx + random.uniform(-4, 4), cy + random.uniform(-3, 3))
            except Exception:
                return
            await asyncio.sleep(random.uniform(0.4, 2.2))

    # ── loop ─────────────────────────────────────────────────────────────────
    async def _loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(random.uniform(self._min_gap, self._max_gap))
                if not self._gate.is_set():
                    continue
                if self._page.is_closed():
                    return
                if self._camping:
                    # No scrolling and no peeking while armed: a scroll moves the board under the slip
                    # and a peek would open a DIFFERENT market's slip over the one being held. No URL
                    # pin either — a goto would destroy the armed popover, which is the thing being
                    # protected.
                    #
                    # But DO notice when it is already gone. A camp whose popover has died is not a camp,
                    # and leaving the flag set means the next fire presses Place on whatever is there.
                    # Reported the moment it happens rather than whenever someone next asks.
                    if not await self._popover_alive():
                        self._log("ARMED SLIP IS GONE — the camp is dead. Not hovering a popover that "
                                  "no longer exists; call /camp/stop and re-arm.")
                        if self._on_lost is not None:
                            try:
                                await self._on_lost()
                            except Exception:
                                pass
                        continue
                    # KEEPALIVE, THEN DRIFT. Only occasionally: the point is to touch the keyboard often
                    # enough to hold the session, not to sit there paging up and down for twenty minutes.
                    if random.random() < 0.35:
                        if not await self._camped_keepalive():
                            if self._on_lost is not None:
                                try:
                                    await self._on_lost()
                                except Exception:
                                    pass
                            continue
                    await self._hover_drift()
                    continue
                await self._pin_url()
                await self._scroll_cycle()
                if random.random() < self._peek_chance:
                    await self._peek_slip()
            except asyncio.CancelledError:
                return
            except Exception as e:
                self._log(f"in-play idle: {type(e).__name__}: {e}")
                await asyncio.sleep(20)

    def status(self) -> dict:
        return {"camping": self._camping, "peeks": self._peeks,
                "paused": not self._gate.is_set(),
                "url": LIVE_URL}
