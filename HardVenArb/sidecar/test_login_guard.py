"""
test_login_guard.py — the auto-login "already logged in" guard in pinnacle_session._ensure_logged_in.

Regression for the 2026-07-14 churn bug: the login watcher re-submitted the login form right after a clean
capture (session live), rotating the fresh session -> guest-redirect cascade + WS auth-reject storm. The guard
must SKIP the submit while a capture is fresh (logged in) but STILL submit on a genuine logout (stale capture /
first open). Exercises the REAL _ensure_logged_in against a fake Playwright page.

    python test_login_guard.py            # 5/5 expected
"""
import asyncio
import os
import sys
import time
from unittest.mock import AsyncMock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pinnacle_session as ps
from pinnacle_session import PinnacleBrowserSession

GRACE = 180.0


class FakeLocator:
    def __init__(self, page, value):
        self._page = page
        self._value = value

    @property
    def first(self):
        return self

    async def count(self):
        # 1st count() call (the pre-submit form-present check) -> 1; later calls (post-submit "form gone"
        # check) -> 0 so _click_login_button isn't reached.
        self._page.count_calls += 1
        return 1 if self._page.count_calls == 1 else 0

    async def input_value(self):
        return self._value

    async def click(self):
        return None

    async def press(self, key):
        return None

    async def bounding_box(self):
        return None


class FakePage:
    def __init__(self, value):
        self.value = value
        self.count_calls = 0

    def locator(self, sel):
        return FakeLocator(self, self.value)


def make_session(*, logged_session, last_capture_age, ever_logged_in, profile_saved, field_value,
                 last_submit_age=10_000.0):
    s = object.__new__(PinnacleBrowserSession)          # skip __init__ (no Playwright)
    s._page = FakePage(field_value)
    s._organic = None                                   # pause/resume/_human_approach become no-ops
    s._ever_logged_in = ever_logged_in
    s._logged_session = logged_session
    s._last_capture = (time.time() - last_capture_age) if last_capture_age is not None else 0.0
    s._login_healthy_grace = GRACE
    s._login_submit_cooldown = 30.0
    s._last_login_submit = time.time() - last_submit_age
    s._profile_has_saved_login = lambda: profile_saved
    s._click_login_button = AsyncMock()
    return s


def run(coro):
    return asyncio.run(coro)


def main():
    # make the submit path instant (skip the human-beat sleeps)
    async def _no_sleep(*a, **k):
        return None
    ps.asyncio.sleep = _no_sleep

    cases = []

    # 1. BUG CASE — logged in, fresh capture, stray autofilled form -> MUST SKIP (no submit).
    s = make_session(logged_session=True, last_capture_age=5, ever_logged_in=True,
                     profile_saved=True, field_value="secret")
    before = s._last_login_submit
    r = run(s._ensure_logged_in())
    cases.append(("healthy+fresh capture -> SKIP", r is False and s._last_login_submit == before))

    # 2. Dark-gap reopen — logged_session latched, but capture STALE (>grace) -> MUST SUBMIT.
    s = make_session(logged_session=True, last_capture_age=1000, ever_logged_in=True,
                     profile_saved=True, field_value="secret")
    r = run(s._ensure_logged_in())
    cases.append(("stale capture (dark gap) -> SUBMIT", r is True))

    # 3. First-time manual setup — no creds evidence, empty field -> MUST SKIP (never submit blanks).
    s = make_session(logged_session=False, last_capture_age=None, ever_logged_in=False,
                     profile_saved=False, field_value="")
    before = s._last_login_submit
    r = run(s._ensure_logged_in())
    cases.append(("no creds (first-time setup) -> SKIP", r is False and s._last_login_submit == before))

    # 4. Fresh process, expired cookies — never captured this run, but profile has saved creds -> MUST SUBMIT.
    s = make_session(logged_session=False, last_capture_age=None, ever_logged_in=False,
                     profile_saved=True, field_value="")
    r = run(s._ensure_logged_in())
    cases.append(("first open, expired cookies -> SUBMIT", r is True))

    # 5. Cooldown still respected — capture stale (guard passes) but a submit just happened -> MUST SKIP.
    s = make_session(logged_session=True, last_capture_age=1000, ever_logged_in=True,
                     profile_saved=True, field_value="secret", last_submit_age=2)
    r = run(s._ensure_logged_in())
    cases.append(("recent submit (cooldown) -> SKIP", r is False))

    ok = 0
    for name, passed in cases:
        print(f"  [{'PASS' if passed else 'FAIL'}] {name}")
        ok += passed
    print(f"\n{ok}/{len(cases)} passed")
    sys.exit(0 if ok == len(cases) else 1)


if __name__ == "__main__":
    main()
