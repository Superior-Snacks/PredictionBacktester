"""Pins _yes_price: the input to pair_auto's price-consistency gate.

This function fed the gate `None` for every market for as long as Kalshi has been returning DOLLAR STRINGS
(`yes_bid_dollars: '0.8400'`) instead of the integer-cents fields it read — so the gate reported everything
"unvalidated" and inverted/wrong-game pairs went through unchecked. These tests pin both payload shapes and,
just as importantly, the cases where the honest answer is None: a fabricated price is worse than no price,
because a bad mid makes the gate SWAP a correct pair.

Run: python -m pytest test_yes_price.py -q     (from HardVenArb/sidecar)
"""
import datetime as dt
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))   # pairHard.py lives one level up
import pairHard                                                    # noqa: E402
from pairHard import MAX_TRUSTED_SPREAD, _price_field, _yes_price  # noqa: E402


# ── the live payload shape: dollar strings ────────────────────────────────────

def test_dollar_strings_give_the_mid():
    # The exact shape /events?with_nested_markets returns today.
    assert _yes_price({"yes_bid_dollars": "0.4000", "yes_ask_dollars": "0.4400"}) == 0.42


def test_regression_cents_field_names_are_absent_from_the_live_payload():
    """The bug: a real /events market carries *_dollars and NO yes_bid/yes_ask, so the old cents-only
    lookup returned None for every market and the gate validated nothing."""
    live = {"ticker": "KXNFLENDSTREAK-40NYJ-2627", "status": "active",
            "yes_bid_dollars": "0.0900", "yes_ask_dollars": "0.1500", "last_price_dollars": "0.1100"}
    assert "yes_bid" not in live and "last_price" not in live
    assert _yes_price(live) == 0.12


def test_cents_payload_still_parses():
    # Fallback for any older/other endpoint that still reports integer cents.
    assert _yes_price({"yes_bid": 40, "yes_ask": 44}) == 0.42


def test_dollars_win_when_both_shapes_are_present():
    got = _yes_price({"yes_bid_dollars": "0.4000", "yes_ask_dollars": "0.4400",
                      "yes_bid": 10, "yes_ask": 14})
    assert got == 0.42


# ── the spread guard ──────────────────────────────────────────────────────────

def test_wide_book_does_not_produce_a_mid():
    """yb=0.03/ya=0.84 is a quote-less market. Midding it invents 0.435 out of two unrelated resting
    orders — and with no last trade to fall back on the only honest answer is None."""
    assert _yes_price({"yes_bid_dollars": "0.0300", "yes_ask_dollars": "0.8400"}) is None


def test_spread_exactly_at_the_limit_is_still_trusted():
    got = _yes_price({"yes_bid_dollars": "0.4000",
                      "yes_ask_dollars": f"{0.40 + MAX_TRUSTED_SPREAD:.4f}"})
    assert got == pytest.approx(0.40 + MAX_TRUSTED_SPREAD / 2, abs=1e-4)


def test_spread_just_past_the_limit_is_not_midded():
    assert _yes_price({"yes_bid_dollars": "0.4000",
                       "yes_ask_dollars": f"{0.40 + MAX_TRUSTED_SPREAD + 0.01:.4f}"}) is None


def test_wide_book_falls_back_to_a_last_trade_inside_it():
    # A print the CURRENT book still brackets is a live-consistent read on who is favoured.
    assert _yes_price({"yes_bid_dollars": "0.0300", "yes_ask_dollars": "0.8400",
                       "last_price_dollars": "0.8000"}) == 0.80


def test_last_trade_outside_the_current_book_is_rejected_as_stale():
    # Book has moved to 0.10/0.20; a 0.80 print is history and would flip the gate's verdict.
    assert _yes_price({"yes_bid_dollars": "0.1000", "yes_ask_dollars": "0.9000",
                       "last_price_dollars": "0.9500"}) is None


def test_guard_is_env_tunable():
    assert MAX_TRUSTED_SPREAD == pytest.approx(0.15)   # documented default


# ── unpriced / malformed markets ──────────────────────────────────────────────

def test_unpriced_market_is_none():
    assert _yes_price({"ticker": "KXFOO", "status": "active"}) is None


def test_zero_prices_are_unpriced_not_zero_probability():
    assert _yes_price({"yes_bid_dollars": "0.0000", "yes_ask_dollars": "0.0000",
                       "last_price_dollars": "0.0000"}) is None


def test_one_sided_book_uses_last_trade_consistent_with_the_side_present():
    assert _yes_price({"yes_ask_dollars": "0.3000", "last_price_dollars": "0.2500"}) == 0.25
    # ...and refuses one the lone ask contradicts.
    assert _yes_price({"yes_ask_dollars": "0.3000", "last_price_dollars": "0.9000"}) is None


def test_crossed_book_is_refused():
    # bid > ask is corrupt data; nothing here is safe to hand the gate.
    assert _yes_price({"yes_bid_dollars": "0.9000", "yes_ask_dollars": "0.1000"}) is None


def test_garbage_strings_do_not_raise():
    assert _yes_price({"yes_bid_dollars": "n/a", "yes_ask_dollars": ""}) is None
    assert _price_field({"a": None}, "a", "b") is None


def test_booleans_are_not_treated_as_prices():
    # bool is an int subclass in Python; True must not become a 1c price.
    assert _price_field({"a": True, "b": True}, "a", "b") is None


# ── the property that makes the gate safe ─────────────────────────────────────

def test_no_price_is_ever_returned_outside_0_1():
    cases = [
        {"yes_bid_dollars": "0.4000", "yes_ask_dollars": "0.4400"},
        {"yes_bid": 1, "yes_ask": 99},
        {"last_price_dollars": "0.9900"},
        {"yes_bid_dollars": "0.0300", "yes_ask_dollars": "0.8400", "last_price_dollars": "0.8000"},
    ]
    for c in cases:
        p = _yes_price(c)
        assert p is None or 0.0 < p <= 1.0, c


def test_a_correct_pair_is_never_swapped_by_a_trusted_mid():
    """The failure that matters. pair_auto swaps sides when |k-(1-b)| < |k-b|. Feed it a TRUSTED mid and
    a book agreeing on the same favourite: the swap must never trigger. (The wide mid this guard now
    suppresses — 0.42 against a book's 0.60 — WOULD have swapped, corrupting a correct pairing.)"""
    for cents, book in ((0.60, 0.62), (0.85, 0.88), (0.20, 0.17), (0.50, 0.52)):
        k = _yes_price({"yes_bid_dollars": f"{cents - 0.02:.4f}",
                        "yes_ask_dollars": f"{cents + 0.02:.4f}"})
        assert k is not None
        assert abs(k - book) <= abs(k - (1.0 - book)), f"would swap a correct pair at k={k} b={book}"


def test_the_suppressed_wide_mid_would_have_swapped():
    """Proves the guard earns its keep rather than just costing coverage."""
    wide = {"yes_bid_dollars": "0.0300", "yes_ask_dollars": "0.8400"}
    bad_mid, book = 0.435, 0.60                      # what midding it would have produced
    assert abs(bad_mid - (1.0 - book)) < abs(bad_mid - book)   # -> gate swaps a CORRECT pair
    assert _yes_price(wide) is None                            # -> guard refuses instead


def test_scaffolded_entry_carries_the_price(monkeypatch):
    """End-to-end on the field name the gate reads: a market that has a price must not land as null."""
    m = {"yes_bid_dollars": "0.4000", "yes_ask_dollars": "0.4400"}
    entry = {"kalshi_ticker": "KXFOO", "kalshi_yes_price": pairHard._yes_price(m)}
    assert entry["kalshi_yes_price"] == 0.42


# ── Kalshi's trading halt (2026-11-19): the price read in it is frozen, and the rows say so ──────────

class _Resp:
    def __init__(self, data):
        self._d = data

    def raise_for_status(self):
        pass

    def json(self):
        return self._d


def test_exchange_status_paused_on_and_unreadable(monkeypatch):
    state, urls = {}, []

    def fake_get(url, timeout=0, **kw):
        urls.append(url)
        return _Resp(state["v"])
    monkeypatch.setattr(pairHard.requests, "get", fake_get)
    for v, want in (({"exchange_active": True, "trading_active": False}, True),     # the weekly maintenance
                    ({"exchange_active": True, "trading_active": True}, False),
                    ({"exchange_active": False, "trading_active": True}, True),
                    ({}, True)):                                                  # no fields = not provably on
        state["v"] = v
        assert pairHard.kalshi_trading_paused() is want, v
    assert all(u.endswith("/exchange/status") for u in urls)

    def down(url, timeout=0, **kw):
        raise pairHard.requests.ConnectionError("down")
    monkeypatch.setattr(pairHard.requests, "get", down)
    assert pairHard.kalshi_trading_paused() is None          # unreadable: prices taken as live, as before


def _scaffold(monkeypatch, tmp_path, statuses):
    """pairHard.main() on one real-shaped market (EFSKRA-EFS's hollow book in the 11-19 halt), status stubbed."""
    ev = {"category": "Sports", "series_ticker": "KXITFMATCH", "title": "Efstathiou vs Kravchenko",
          "event_ticker": "KXITFMATCH-26NOV19EFSKRA",
          "markets": [{"ticker": "KXITFMATCH-26NOV19EFSKRA-EFS", "title": "Efstathiou", "yes_sub_title": "Melios Efstathiou",
                       "expected_expiration_time": (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=3)).isoformat(),
                       "yes_bid_dollars": "0.0800", "yes_ask_dollars": "0.3400", "last_price_dollars": "0.3200"}]}
    seq = list(statuses)
    monkeypatch.setattr(pairHard, "fetch_open_events", lambda: [ev])
    monkeypatch.setattr(pairHard, "kalshi_trading_paused", lambda: seq.pop(0))
    out = tmp_path / "pairs.json"
    monkeypatch.setattr(sys, "argv", ["pairHard.py", "--series", "KXITFMATCH", "--out", str(out), "--fresh"])
    pairHard.main()                                          # (paused before the fetch: no second read needed)
    return json.loads(out.read_text(encoding="utf-8"))


@pytest.mark.parametrize("statuses", [(True, True), (False, True), (True, False)])
def test_rows_read_in_a_halt_carry_kalshi_paused(monkeypatch, tmp_path, statuses):
    """A halt for the whole read, one that began during it, one that ended during it: each froze what was read."""
    rows = _scaffold(monkeypatch, tmp_path, statuses)
    assert rows[0]["kalshi_paused"] is True
    assert rows[0]["kalshi_yes_price"] == 0.32               # the last trade before the halt - what the gate must not judge


@pytest.mark.parametrize("statuses", [(False, False), (None, None), (None, False)])
def test_rows_read_while_trading_or_unknown_carry_no_flag(monkeypatch, tmp_path, statuses):
    rows = _scaffold(monkeypatch, tmp_path, statuses)
    assert "kalshi_paused" not in rows[0]
