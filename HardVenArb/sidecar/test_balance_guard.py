"""
test_balance_guard.py — an UNREADABLE wallet must never halt the schedule; a genuinely empty one must.

Regression for the 2026-08-07 deadlock. Sequence: login captured cleanly -> `GET /wallet/balance` returned 401
-> the adapter collapsed that to `0.0` -> BalanceGuard saw "wallet 0.00 < floor 5" -> lifecycle HALTED a funded
account. The halt then closes the browser, and a closed browser can never re-authenticate, so the bot cannot
recover on its own and the halt survives restarts by design.

The rule under test: UNKNOWN != ZERO, on every path (adapter returns None, adapter raises, session logged out)
— while the guard still does its actual job on a real breach.

    python test_balance_guard.py            # 8/8 expected
"""
import asyncio
import os
import sys

os.environ["HARDVEN_MIN_BALANCE"] = "5"
os.environ["HARDVEN_MIN_BALANCE_USD"] = "10"
os.environ["HARDVEN_BALANCE_GUARD"] = "1"

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from balance_guard import BalanceGuard


class FakeAdapter:
    name = "pinnacle"

    def __init__(self, value=None, raises=False, ready=True):
        self._value, self._raises, self._ready = value, raises, ready

    def session_status(self):
        return {"ready": self._ready}

    async def balance(self):
        if self._raises:
            raise RuntimeError("connection reset")
        return self._value


class FakeLifecycle:
    def __init__(self):
        self.halts = []

    async def halt(self, reason):
        self.halts.append(reason)


def run(adapter, kalshi=None):
    """One check_now() cycle; returns the halt reasons the lifecycle received."""
    lc = FakeLifecycle()
    g = BalanceGuard(adapter, lc, None)
    if kalshi is not None:
        g.push_kalshi(kalshi)
    asyncio.run(g.check_now())
    return lc.halts


def check(name, halts, want_halt):
    got = bool(halts)
    ok = got == want_halt
    detail = halts[0] if halts else "no halt"
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}: {detail!r} (expected {'halt' if want_halt else 'no halt'})")
    return ok


def main() -> int:
    r = []

    # ── the deadlock: unreadable must NOT halt ────────────────────────────────
    r.append(check("401/unreadable (adapter -> None)", run(FakeAdapter(value=None)), False))
    r.append(check("adapter raises", run(FakeAdapter(raises=True)), False))
    r.append(check("logged out (session not ready)", run(FakeAdapter(value=0.0, ready=False)), False))

    # ── ...but the guard must still guard ─────────────────────────────────────
    r.append(check("genuine empty wallet (0.00, logged in)", run(FakeAdapter(value=0.0)), True))
    r.append(check("below floor (3.00 < 5)", run(FakeAdapter(value=3.0)), True))
    r.append(check("healthy wallet (50.00)", run(FakeAdapter(value=50.0)), False))

    # ── the Kalshi leg keeps its own unknown-vs-zero rule ─────────────────────
    r.append(check("kalshi pushed below floor", run(FakeAdapter(value=50.0), kalshi=2.0), True))
    r.append(check("kalshi never pushed (unknown)", run(FakeAdapter(value=50.0)), False))

    # ── the ACTUAL 2026-08-07 fault was one layer down, in the adapter: a 401 made _http_get return None and
    #    balance() answered 0.0. The guard above cannot catch that (0.0 is a legitimate value), so test the
    #    adapter's own contract directly — this is the case that was really broken.
    from pinnacle_adapter import PinnacleAdapter

    def adapter_balance(http_result):
        a = PinnacleAdapter()
        a._session_source = "env"                      # bypass the browser-login gate
        async def fake_get(path, **kw):
            return http_result
        a._http_get = fake_get
        return asyncio.run(a.balance())

    for name, payload, want in [
        ("adapter: 401 -> _http_get None", None, None),
        ("adapter: non-dict reply", "<html>login</html>", None),
        ("adapter: amount field absent", {"currency": "EUR"}, None),
        ("adapter: genuine zero balance", {"amount": 0, "currency": "EUR"}, 0.0),
        ("adapter: funded wallet", {"amount": 42.5, "currency": "EUR"}, 42.5),
    ]:
        got = adapter_balance(payload)
        ok = got == want and (got is None) == (want is None)
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}: got {got!r} (expected {want!r})")
        r.append(ok)

    # ── POST-LOGIN SETTLE: don't hit the authed wallet while the site is still coming up ──────────
    import time as _t

    def settle_delay(session_age, settle_sec):
        """Seconds balance() actually waits before issuing the authed GET."""
        a = PinnacleAdapter()
        a._session_source = "env"
        a._session_settle_sec = settle_sec
        a._session_started_at = _t.time() - session_age
        issued_at = []
        async def fake_get(path, **kw):
            issued_at.append(_t.perf_counter())
            return {"amount": 42.5, "currency": "EUR"}
        a._http_get = fake_get
        t0 = _t.perf_counter()
        asyncio.run(a.balance())
        return issued_at[0] - t0

    for name, age, settle, lo, hi in [
        ("settle: fresh session waits",      0.0, 0.4, 0.30, 0.9),
        ("settle: partly-aged waits rest",   0.3, 0.4, 0.03, 0.4),
        ("settle: aged session no wait",    60.0, 0.4, 0.00, 0.1),
        ("settle: disabled (0) no wait",     0.0, 0.0, 0.00, 0.1),
    ]:
        d = settle_delay(age, settle)
        ok = lo <= d <= hi
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}: waited {d:.2f}s (expected {lo:.2f}-{hi:.2f}s)")
        r.append(ok)

    # ── an unreadable wallet must not halt, but must not stay SILENT either ───────────────────────
    os.environ["HARDVEN_BALANCE_UNREADABLE_WARN"] = "3"
    lc = FakeLifecycle()
    g = BalanceGuard(FakeAdapter(value=None), lc, None)
    for _ in range(4):
        asyncio.run(g.check_now())
    warned = g._unreadable >= 3
    print(f"  [{'PASS' if warned and not lc.halts else 'FAIL'}] unreadable streak warns but never halts: "
          f"streak={g._unreadable} halts={len(lc.halts)}")
    r.append(warned and not lc.halts)

    # ...and a good read clears the streak so floor enforcement is known-restored
    g._adapter = FakeAdapter(value=50.0)
    asyncio.run(g.check_now())
    print(f"  [{'PASS' if g._unreadable == 0 else 'FAIL'}] good read resets the streak: {g._unreadable}")
    r.append(g._unreadable == 0)

    n = sum(r)
    print(f"\n{n}/{len(r)} passed")
    return 0 if n == len(r) else 1


if __name__ == "__main__":
    sys.exit(main())
