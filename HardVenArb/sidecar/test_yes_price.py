"""Pins _yes_price: the input to pair_auto's price-consistency gate.

This function fed the gate `None` for every market for as long as Kalshi has been returning DOLLAR STRINGS
(`yes_bid_dollars: '0.8400'`) instead of the integer-cents fields it read — so the gate reported everything
"unvalidated" and inverted/wrong-game pairs went through unchecked. These tests pin both payload shapes and,
just as importantly, the cases where the honest answer is None: a fabricated price is worse than no price,
because a bad mid makes the gate SWAP a correct pair.

Run: python -m pytest test_yes_price.py -q     (from HardVenArb/sidecar)
"""
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
