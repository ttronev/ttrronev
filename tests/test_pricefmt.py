"""shared/pricefmt.py — rounding must be a no-op for the prices the stack was
built on, and must stop destroying sub-cent prices."""
import math

from shared.pricefmt import (round_price, fmt_price, price_decimals,
                             price_key, SUB_CENT_SIG_DIGITS)

# Real magnitudes from the 19-pair universe (2026-08): SHIB, DOGE, HBAR, ADA,
# SOL, BNB, BTC.
LEGACY_SAFE = [0.01, 0.0312345678, 0.0912345678, 0.3456789123, 1.23456789,
               9.87654321, 96.9812345678, 105.3312349, 612.123456789,
               78419.123456789, 123456.7890123]


def test_identical_to_legacy_round_from_one_cent_up():
    for p in LEGACY_SAFE:
        assert round_price(p) == round(p, 6), p
        assert price_decimals(p) == 6, p


def test_negative_and_degenerate_inputs():
    assert round_price(None) is None
    assert round_price(0.0) == 0.0
    assert round_price(-96.9812345678) == round(-96.9812345678, 6)
    assert math.isnan(round_price(float("nan")))


def test_shib_keeps_its_digits():
    shib = 5.158e-06                       # live.json 2026-08-28
    assert round(shib, 6) == 5e-06         # the bug: one significant digit
    assert round_price(shib) == shib       # the fix
    # 50 distinct prices across a 20% span used to collapse to 2 values.
    grid = [5.0e-06 + i * 2e-08 for i in range(50)]
    assert len({round(p, 6) for p in grid}) <= 2
    assert len({round_price(p) for p in grid}) == 50


def test_sub_cent_prices_keep_their_significant_digits():
    # Stored level prices feed the 0.3% touch band: rounding error must be
    # negligible against it at every scale.
    p = 9.87654321e-03
    while p > 1e-10:
        r = round_price(p)
        assert abs(r - p) / p < 10 ** -(SUB_CENT_SIG_DIGITS - 1), (p, r)
        p /= 7.3


def test_fmt_price_scales():
    assert fmt_price(96.98) == "$96.98"
    assert fmt_price(105.334) == "$105.33"
    assert fmt_price(78419.9) == "$78,419.9"
    assert fmt_price(0.6512) == "$0.6512"
    assert fmt_price(0.0912) == "$0.091200"
    assert fmt_price(5.158e-06) == "$0.000005158"
    assert fmt_price(None) == "n/a"
    assert fmt_price(float("nan")) == "n/a"


def test_two_cheap_levels_are_distinguishable_in_text():
    # The old ':.2f' printed both of these as "$0.09".
    assert fmt_price(0.0912) != fmt_price(0.0934)


def test_price_key_matches_legacy_keys():
    # alerts_state.json keys written before this module must keep matching.
    for p in LEGACY_SAFE:
        assert price_key(p) == str(round(p, 6)), p
    assert price_key(5.158e-06) != price_key(5.4e-06)
