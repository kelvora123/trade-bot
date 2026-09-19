"""Layer 2: allocation, concentration, aggregate Greeks, IV rank, expiry watch."""

from __future__ import annotations

from datetime import timedelta

import pytest
from conftest import ASOF, EXPIRY, make_chain, seed_iv_history

from tradebot.layer1.valuation import value_book
from tradebot.layer2.analytics import analyse, iv_percentile, iv_rank
from tradebot.types import AssetKind, Holding, OptionKind

# ------------------------------------------------------------------ IV stats


def test_iv_rank_spans_the_range():
    series = [0.20, 0.30, 0.40, 0.50, 0.60]
    assert iv_rank(0.60, series) == pytest.approx(100.0)
    assert iv_rank(0.20, series) == pytest.approx(0.0)
    assert iv_rank(0.40, series) == pytest.approx(50.0)


def test_iv_rank_none_on_flat_history():
    assert iv_rank(0.4, [0.4, 0.4, 0.4]) is None


def test_iv_rank_none_on_empty_history():
    assert iv_rank(0.4, []) is None


def test_iv_rank_clamps_outside_range():
    assert iv_rank(0.99, [0.2, 0.3]) == 100.0
    assert iv_rank(0.01, [0.2, 0.3]) == 0.0


def test_iv_percentile_counts_days_below():
    assert iv_percentile(0.45, [0.1, 0.2, 0.3, 0.9]) == pytest.approx(75.0)


# ---------------------------------------------------------------- allocation


def test_allocation_sums_to_one(cfg, store, nvda_call, aapl_shares):
    val = value_book(
        [nvda_call, aapl_shares], {"NVDA": make_chain()}, {"NVDA": 185.0, "AAPL": 230.0}, cfg, ASOF, store
    )
    r = analyse(val, store, cfg, {"NVDA": "Technology", "AAPL": "Technology"})
    assert sum(r.by_ticker.values()) == pytest.approx(1.0)
    assert sum(r.by_sector.values()) == pytest.approx(1.0)


def test_sector_map_groups_tickers(cfg, store, nvda_call, aapl_shares):
    val = value_book(
        [nvda_call, aapl_shares], {"NVDA": make_chain()}, {"NVDA": 185.0, "AAPL": 230.0}, cfg, ASOF, store
    )
    r = analyse(val, store, cfg, {"NVDA": "Technology", "AAPL": "Technology"})
    assert r.by_sector["Technology"] == pytest.approx(1.0)


def test_unmapped_ticker_falls_back_to_unknown(cfg, store, nvda_call):
    val = value_book([nvda_call], {"NVDA": make_chain()}, {"NVDA": 185.0}, cfg, ASOF, store)
    r = analyse(val, store, cfg, {})
    assert "Unknown" in r.by_sector
    assert any("no sector mapping" in n for n in r.notes)


def test_shorts_count_toward_concentration_not_against_it(cfg, store):
    """A short leg must not net away a long leg's allocation weight."""
    long_h = Holding("NVDA", AssetKind.OPTION, 2, 12.0, OptionKind.CALL, 180.0, EXPIRY)
    short_h = Holding("NVDA", AssetKind.OPTION, -2, 8.0, OptionKind.CALL, 200.0, EXPIRY)
    val = value_book([long_h, short_h], {"NVDA": make_chain()}, {"NVDA": 185.0}, cfg, ASOF, store)
    r = analyse(val, store, cfg, {"NVDA": "Technology"})
    assert r.total_value > 0
    assert r.by_ticker["NVDA"] == pytest.approx(1.0)


def test_ticker_concentration_flag_fires(cfg, store, nvda_call):
    val = value_book([nvda_call], {"NVDA": make_chain()}, {"NVDA": 185.0}, cfg, ASOF, store)
    r = analyse(val, store, cfg, {"NVDA": "Technology"})
    flags = [f for f in r.concentration_flags if f["kind"] == "ticker"]
    assert flags and flags[0]["name"] == "NVDA"
    assert flags[0]["weight_pct"] == pytest.approx(100.0)


def test_sector_concentration_flag_fires(cfg, store, nvda_call):
    val = value_book([nvda_call], {"NVDA": make_chain()}, {"NVDA": 185.0}, cfg, ASOF, store)
    r = analyse(val, store, cfg, {"NVDA": "Technology"})
    assert any(f["kind"] == "sector" for f in r.concentration_flags)


def test_no_flags_when_within_caps(cfg, store):
    """Five equally weighted names sit under both the 40% and 60% caps."""
    holdings = [Holding(t, AssetKind.SHARES, 10, 100.0) for t in ("A", "B", "C", "D", "E")]
    spots = {t: 100.0 for t in ("A", "B", "C", "D", "E")}
    sectors = {t: f"Sector{i}" for i, t in enumerate(("A", "B", "C", "D", "E"))}
    val = value_book(holdings, {}, spots, cfg, ASOF, store)
    r = analyse(val, store, cfg, sectors)
    assert r.concentration_flags == []


# ----------------------------------------------------------- aggregate greeks


def test_aggregate_greeks_sum_across_positions(cfg, store, nvda_call, aapl_shares):
    val = value_book(
        [nvda_call, aapl_shares], {"NVDA": make_chain()}, {"NVDA": 185.0, "AAPL": 230.0}, cfg, ASOF, store
    )
    r = analyse(val, store, cfg, {})
    g = r.aggregate_greeks
    expected = sum(p.delta for p in val.positions)
    assert g["net_delta_shares"] == pytest.approx(expected, abs=0.01)
    assert g["daily_theta_dollars"] < 0  # long options decay
    assert g["net_vega_dollars_per_iv_point"] > 0
    assert g["positions_included"] == 2


def test_stale_positions_excluded_from_greeks(cfg, store, nvda_call, aapl_shares):
    val = value_book(
        [nvda_call, aapl_shares], {"NVDA": make_chain()}, {"NVDA": 185.0, "AAPL": None}, cfg, ASOF, store
    )
    r = analyse(val, store, cfg, {})
    assert r.aggregate_greeks["positions_excluded_stale"] == 1


# --------------------------------------------------------------- iv env


def test_iv_env_reports_building_history_below_minimum(cfg, store, nvda_call):
    seed_iv_history(store, "NVDA", ASOF, days=5)
    val = value_book([nvda_call], {"NVDA": make_chain()}, {"NVDA": 185.0}, cfg, ASOF, store)
    entry = analyse(val, store, cfg, {}).iv_environment[0]
    assert entry["status"] == "building history"
    assert entry["iv_rank"] is None
    assert "20" in entry["note"]


def test_iv_env_computes_rank_once_history_is_sufficient(cfg, store, nvda_call):
    seed_iv_history(store, "NVDA", ASOF, days=40)
    val = value_book([nvda_call], {"NVDA": make_chain()}, {"NVDA": 185.0}, cfg, ASOF, store)
    entry = analyse(val, store, cfg, {}).iv_environment[0]
    assert entry["status"] in {"rich", "cheap", "normal"}
    assert entry["iv_rank"] is not None
    assert 0 <= entry["iv_rank"] <= 100


def test_iv_env_flags_rich(cfg, store, nvda_call):
    """Current IV at the top of a low-vol year must read rich."""
    for i in range(40):
        store.record_underlying_iv(ASOF - timedelta(days=40 - i), "NVDA", 0.10 + i * 0.001, 185.0)
    val = value_book([nvda_call], {"NVDA": make_chain()}, {"NVDA": 185.0}, cfg, ASOF, store)
    entry = analyse(val, store, cfg, {}).iv_environment[0]
    assert entry["status"] == "rich"
    assert entry["iv_rank"] == pytest.approx(100.0)


def test_iv_env_flags_cheap(cfg, store, nvda_call):
    for i in range(40):
        store.record_underlying_iv(ASOF - timedelta(days=40 - i), "NVDA", 0.80 + i * 0.001, 185.0)
    val = value_book([nvda_call], {"NVDA": make_chain()}, {"NVDA": 185.0}, cfg, ASOF, store)
    entry = analyse(val, store, cfg, {}).iv_environment[0]
    assert entry["status"] == "cheap"


def test_shares_have_no_iv_entry(cfg, store, aapl_shares):
    val = value_book([aapl_shares], {}, {"AAPL": 230.0}, cfg, ASOF, store)
    assert analyse(val, store, cfg, {}).iv_environment == []


# -------------------------------------------------------------- expiry watch


def test_expiry_watch_ignores_distant_contracts(cfg, store, nvda_call):
    val = value_book([nvda_call], {"NVDA": make_chain()}, {"NVDA": 185.0}, cfg, ASOF, store)
    assert analyse(val, store, cfg, {}).expiry_watch == []  # 74 DTE > 45


def test_expiry_watch_catches_near_dated(cfg, store):
    near = ASOF + timedelta(days=30)
    h = Holding("NVDA", AssetKind.OPTION, 1, 8.0, OptionKind.CALL, 180.0, near)
    val = value_book([h], {"NVDA": make_chain(expiry=near)}, {"NVDA": 185.0}, cfg, ASOF, store)
    watch = analyse(val, store, cfg, {}).expiry_watch
    assert len(watch) == 1
    assert watch[0]["dte"] == 30
    assert "decaying at" in watch[0]["fact"]


def test_expiry_watch_states_facts_not_advice(cfg, store):
    near = ASOF + timedelta(days=10)
    h = Holding("NVDA", AssetKind.OPTION, 1, 8.0, OptionKind.CALL, 180.0, near)
    val = value_book([h], {"NVDA": make_chain(expiry=near)}, {"NVDA": 185.0}, cfg, ASOF, store)
    r = analyse(val, store, cfg, {})
    blob = " ".join(e["fact"] for e in r.expiry_watch).lower()
    for word in ("roll", "should", "recommend", "consider", "suggest"):
        assert word not in blob
    assert "does not advise" in r.to_json()["disclaimer"]


def test_expiry_watch_sorted_by_urgency(cfg, store):
    holdings, chains = [], []
    for days in (40, 5, 20):
        exp = ASOF + timedelta(days=days)
        holdings.append(Holding("NVDA", AssetKind.OPTION, 1, 8.0, OptionKind.CALL, 180.0, exp))
        chains += make_chain(expiry=exp)
    val = value_book(holdings, {"NVDA": chains}, {"NVDA": 185.0}, cfg, ASOF, store)
    watch = analyse(val, store, cfg, {}).expiry_watch
    assert [e["dte"] for e in watch] == [5, 20, 40]


def test_empty_book_does_not_crash(cfg, store):
    val = value_book([], {}, {}, cfg, ASOF, store)
    r = analyse(val, store, cfg, {})
    assert r.total_value == 0.0
    assert any("no markable value" in n for n in r.notes)
