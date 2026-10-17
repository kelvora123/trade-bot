"""Black-Scholes correctness against known values and structural invariants."""

from __future__ import annotations

import math

import pytest

from tradebot.layer1.blackscholes import greeks, implied_vol, norm_cdf, price

S, K, T, R, SIG = 100.0, 100.0, 1.0, 0.05, 0.20


def test_known_reference_prices():
    assert price(S, K, T, R, SIG, "call") == pytest.approx(10.4506, abs=1e-4)
    assert price(S, K, T, R, SIG, "put") == pytest.approx(5.5735, abs=1e-4)


def test_put_call_parity():
    c = price(S, K, T, R, SIG, "call")
    p = price(S, K, T, R, SIG, "put")
    assert c - p == pytest.approx(S - K * math.exp(-R * T), abs=1e-9)


def test_known_reference_greeks():
    g = greeks(S, K, T, R, SIG, "call")
    assert g.delta == pytest.approx(0.6368, abs=1e-4)
    assert g.gamma == pytest.approx(0.018762, abs=1e-5)
    assert g.vega == pytest.approx(0.3752, abs=1e-4)  # per 1 IV point
    assert g.theta < 0  # long options decay


def test_call_and_put_delta_differ_by_one():
    c = greeks(S, K, T, R, SIG, "call").delta
    p = greeks(S, K, T, R, SIG, "put").delta
    assert c - p == pytest.approx(1.0, abs=1e-9)


def test_gamma_and_vega_identical_for_call_and_put():
    c, p = greeks(S, K, T, R, SIG, "call"), greeks(S, K, T, R, SIG, "put")
    assert c.gamma == pytest.approx(p.gamma, abs=1e-12)
    assert c.vega == pytest.approx(p.vega, abs=1e-12)


def test_expired_option_is_intrinsic_with_step_delta():
    g = greeks(110.0, 100.0, 0.0, R, SIG, "call")
    assert g.price == pytest.approx(10.0)
    assert g.delta == 1.0
    assert (g.gamma, g.theta, g.vega) == (0.0, 0.0, 0.0)

    otm = greeks(90.0, 100.0, 0.0, R, SIG, "call")
    assert otm.price == 0.0 and otm.delta == 0.0


def test_zero_vol_falls_back_to_intrinsic():
    assert price(110.0, 100.0, 1.0, 0.0, 0.0, "call") == pytest.approx(10.0)


def test_implied_vol_roundtrips():
    target = price(S, K, T, R, 0.37, "call")
    assert implied_vol(target, S, K, T, R, "call") == pytest.approx(0.37, abs=1e-4)


def test_implied_vol_rejects_sub_intrinsic_price():
    assert implied_vol(1.0, 150.0, 100.0, 1.0, R, "call") is None


def test_implied_vol_rejects_expired():
    assert implied_vol(5.0, S, K, 0.0, R, "call") is None


def test_norm_cdf_bounds():
    assert norm_cdf(0.0) == pytest.approx(0.5)
    assert norm_cdf(-8.0) < 1e-14
    assert norm_cdf(8.0) > 1 - 1e-14


def test_deeper_itm_has_higher_delta():
    a = greeks(100.0, 120.0, T, R, SIG, "call").delta
    b = greeks(100.0, 80.0, T, R, SIG, "call").delta
    assert b > a


def test_rejects_bad_inputs():
    with pytest.raises(ValueError):
        price(-1.0, K, T, R, SIG, "call")
    with pytest.raises(ValueError):
        price(S, 0.0, T, R, SIG, "call")
    with pytest.raises(ValueError):
        price(S, K, T, R, SIG, "straddle")


def test_scaled_multiplies_by_contract_size():
    g = greeks(S, K, T, R, SIG, "call")
    scaled = g.scaled(2, 100)
    assert scaled.delta == pytest.approx(g.delta * 200)
    assert scaled.theta == pytest.approx(g.theta * 200)
