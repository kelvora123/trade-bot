"""Layer 1: marking rules, Greeks aggregation, and degraded-data handling."""

from __future__ import annotations

from datetime import date

import pytest
from conftest import ASOF, EXPIRY, make_chain

from tradebot.layer1.valuation import atm_iv, value_book
from tradebot.types import AssetKind, ChainRow, Holding, OptionKind


def test_mark_prefers_mid_over_last():
    row = ChainRow("X", EXPIRY, 100.0, OptionKind.CALL, bid=1.0, ask=2.0, last=9.99)
    assert row.mark == 1.5


def test_mark_falls_back_to_last_when_no_quotes():
    assert ChainRow("X", EXPIRY, 100.0, OptionKind.CALL, last=3.3).mark == 3.3


def test_mark_falls_back_when_book_is_crossed():
    row = ChainRow("X", EXPIRY, 100.0, OptionKind.CALL, bid=5.0, ask=2.0, last=3.0)
    assert row.mark == 3.0


def test_zero_bid_is_a_real_quote():
    row = ChainRow("X", EXPIRY, 100.0, OptionKind.CALL, bid=0.0, ask=0.10, last=5.0)
    assert row.mark == pytest.approx(0.05)


def test_mark_is_none_without_any_price():
    assert ChainRow("X", EXPIRY, 100.0, OptionKind.CALL).mark is None


def test_shares_mark_at_spot(cfg, store, aapl_shares):
    r = value_book([aapl_shares], {}, {"AAPL": 230.0}, cfg, ASOF, store)
    p = r.positions[0]
    assert p.mark == 230.0
    assert p.current_value == pytest.approx(2300.0)
    assert p.unrealised_pnl == pytest.approx(2300.0 - 2254.0)
    assert p.delta == 10.0  # one share = one delta


def test_option_value_uses_contract_multiplier(cfg, store, nvda_call):
    r = value_book([nvda_call], {"NVDA": make_chain()}, {"NVDA": 185.0}, cfg, ASOF, store)
    p = r.positions[0]
    assert p.mark == pytest.approx(13.0)         # intrinsic 5 + 8
    assert p.current_value == pytest.approx(2600.0)  # 13 x 2 x 100
    assert p.cost_value == pytest.approx(2500.0)
    assert p.dte == (EXPIRY - ASOF).days


def test_option_greeks_are_position_scaled(cfg, store, nvda_call):
    r = value_book([nvda_call], {"NVDA": make_chain()}, {"NVDA": 185.0}, cfg, ASOF, store)
    p = r.positions[0]
    assert 0 < p.delta < 200            # 2 long calls, max 200 deltas
    assert p.theta < 0                  # long options decay
    assert p.vega > 0
    assert p.iv == pytest.approx(0.4135, abs=1e-3)


def test_short_position_has_negative_value_and_greeks(cfg, store):
    short = Holding("NVDA", AssetKind.OPTION, -1, 12.0, OptionKind.CALL, 180.0, EXPIRY)
    r = value_book([short], {"NVDA": make_chain()}, {"NVDA": 185.0}, cfg, ASOF, store)
    p = r.positions[0]
    assert p.current_value < 0
    assert p.delta < 0
    assert p.theta > 0  # short options collect decay


def test_missing_contract_is_flagged_not_fatal(cfg, store):
    ghost = Holding("NVDA", AssetKind.OPTION, 1, 5.0, OptionKind.CALL, 999.0, EXPIRY)
    r = value_book([ghost], {"NVDA": make_chain()}, {"NVDA": 185.0}, cfg, ASOF, store)
    p = r.positions[0]
    assert p.stale and "not found" in p.warnings[0]
    assert r.warnings


def test_missing_spot_is_flagged_not_fatal(cfg, store, aapl_shares):
    r = value_book([aapl_shares], {}, {"AAPL": None}, cfg, ASOF, store)
    assert r.positions[0].stale
    assert "no spot" in r.positions[0].warnings[0]


def test_broken_chain_iv_is_resolved_locally(cfg, store, nvda_call):
    chain = [
        ChainRow("NVDA", EXPIRY, 180.0, OptionKind.CALL, bid=12.75, ask=13.25, last=13.0, iv=1e-9)
    ]
    r = value_book([nvda_call], {"NVDA": chain}, {"NVDA": 185.0}, cfg, ASOF, store)
    p = r.positions[0]
    assert p.iv is not None and 0.01 < p.iv < 5.0
    assert any("solved locally" in w for w in p.warnings)
    assert p.delta != 0  # Greeks survived


def test_wide_spread_is_warned(cfg, store, nvda_call):
    chain = [ChainRow("NVDA", EXPIRY, 180.0, OptionKind.CALL, bid=8.0, ask=18.0, iv=0.4)]
    r = value_book([nvda_call], {"NVDA": chain}, {"NVDA": 185.0}, cfg, ASOF, store)
    assert any("wide spread" in w for w in r.positions[0].warnings)


def test_expired_contract_is_warned(cfg, store):
    past = date(2025, 1, 17)
    h = Holding("NVDA", AssetKind.OPTION, 1, 5.0, OptionKind.CALL, 180.0, past)
    chain = make_chain(expiry=past)
    r = value_book([h], {"NVDA": chain}, {"NVDA": 185.0}, cfg, ASOF, store)
    assert r.positions[0].dte < 0
    assert any("expired" in w for w in r.positions[0].warnings)


def test_progress_to_target_and_stop(cfg, store, nvda_call):
    r = value_book([nvda_call], {"NVDA": make_chain()}, {"NVDA": 185.0}, cfg, ASOF, store)
    p = r.positions[0]
    # basis 12.50, mark 13.00, target 25.00 -> 0.5/12.5 = 4%
    assert p.progress_to_target == pytest.approx(0.04, abs=1e-6)
    # mark is above basis, so no progress toward a stop below it
    assert p.progress_to_stop == 0.0


def test_atm_iv_picks_strike_nearest_spot():
    iv = atm_iv(make_chain(spot=185.0), 185.0, asof=ASOF)
    assert iv == pytest.approx(0.4135, abs=1e-3)


def test_atm_iv_none_without_usable_rows():
    assert atm_iv([], 185.0, asof=ASOF) is None


def test_atm_iv_is_recorded_for_rank_history(cfg, store, nvda_call):
    value_book([nvda_call], {"NVDA": make_chain()}, {"NVDA": 185.0}, cfg, ASOF, store)
    assert len(store.iv_series("NVDA", ASOF, 252)) == 1


def test_totals_exclude_unmarkable_positions(cfg, store, nvda_call, aapl_shares):
    r = value_book(
        [nvda_call, aapl_shares], {"NVDA": make_chain()}, {"NVDA": 185.0, "AAPL": None}, cfg, ASOF, store
    )
    assert r.total_value == pytest.approx(2600.0)
    assert r.total_cost == pytest.approx(2500.0)
