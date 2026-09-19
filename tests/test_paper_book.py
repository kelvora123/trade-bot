"""The paper book: position math, P&L reconciliation, and the paper-only guard."""

from __future__ import annotations

import pytest
from conftest import EXPIRY

from tradebot.config import load_config
from tradebot.paper.book import PaperBook
from tradebot.paper.broker import OrderRejected, PaperBroker
from tradebot.paper.performance import (
    MIN_DAYS_FOR_ANNUAL,
    compute_performance,
    history_progress,
)
from tradebot.types import AssetKind, Holding, OptionKind


def opt(qty=1, basis=0.30, strike=12.0, ticker="F"):
    return Holding(ticker, AssetKind.OPTION, qty, basis, OptionKind.CALL, strike, EXPIRY)


@pytest.fixture
def broker(cfg, store):
    cfg.paper.starting_cash = 10_000.0
    return PaperBroker(cfg, store)


# ------------------------------------------------------------- position math


def test_opening_sets_qty_and_average(broker):
    ch = broker.buy(opt(), 2, mark=0.30, bid=0.28, ask=0.32).change
    assert ch.opened and ch.qty_after == 2
    assert ch.avg_after == pytest.approx(0.32)  # filled at the ask


def test_adding_weights_the_average(broker):
    broker.buy(opt(), 1, mark=0.30, bid=0.30, ask=0.30)
    ch = broker.buy(opt(), 1, mark=0.40, bid=0.40, ask=0.40).change
    assert ch.qty_after == 2
    assert ch.avg_after == pytest.approx(0.35)


def test_adding_unequal_sizes_weights_by_quantity(broker):
    broker.buy(opt(), 3, mark=0.20, bid=0.20, ask=0.20)
    ch = broker.buy(opt(), 1, mark=0.60, bid=0.60, ask=0.60).change
    assert ch.avg_after == pytest.approx((3 * 0.20 + 1 * 0.60) / 4)


def test_partial_close_realises_and_keeps_average(broker):
    broker.buy(opt(), 2, mark=0.40, bid=0.40, ask=0.40)
    ch = broker.sell(opt(), 1, mark=0.60, bid=0.60, ask=0.60).change
    assert ch.qty_after == 1
    assert ch.avg_after == pytest.approx(0.40)  # unchanged on the remainder
    assert ch.realised_pnl > 0
    assert not ch.fully_closed


def test_full_close_removes_the_position(broker, store):
    broker.buy(opt(), 1, mark=0.40, bid=0.40, ask=0.40)
    ch = broker.sell(opt(), 1, mark=0.60, bid=0.60, ask=0.60).change
    assert ch.fully_closed and ch.qty_after == 0
    assert store.all_positions() == []
    assert len(store.closed_trades()) == 1


def test_losing_trade_realises_negative(broker):
    broker.buy(opt(), 1, mark=0.50, bid=0.50, ask=0.50)
    ch = broker.sell(opt(), 1, mark=0.20, bid=0.20, ask=0.20).change
    assert ch.realised_pnl < 0


def test_short_position_profits_when_price_falls(broker):
    broker.sell(opt(), 1, mark=0.50, bid=0.50, ask=0.50)
    ch = broker.buy(opt(), 1, mark=0.20, bid=0.20, ask=0.20).change
    assert ch.fully_closed
    assert ch.realised_pnl == pytest.approx((0.50 - 0.20) * 100 - 1.30)


def test_short_position_loses_when_price_rises(broker):
    broker.sell(opt(), 1, mark=0.20, bid=0.20, ask=0.20)
    assert broker.buy(opt(), 1, mark=0.50, bid=0.50, ask=0.50).change.realised_pnl < 0


def test_crossing_zero_closes_old_side_and_opens_new(broker, store):
    broker.buy(opt(), 1, mark=0.40, bid=0.40, ask=0.40)
    ch = broker.sell(opt(), 3, mark=0.60, bid=0.60, ask=0.60).change
    assert ch.qty_after == -2               # now short the remainder
    assert ch.avg_after == pytest.approx(0.60)  # reopened at the fill price
    assert ch.closed_qty == 1
    assert len(store.closed_trades()) == 1


# -------------------------------------------------------- P&L reconciliation


def test_realised_pnl_reconciles_with_cash(cfg, store):
    """Round-trip P&L must equal the cash it produced, entry costs included."""
    cfg.paper.starting_cash = 10_000.0
    b = PaperBroker(cfg, store)
    b.buy(opt(), 1, mark=0.30, bid=0.28, ask=0.32)
    b.buy(opt(), 1, mark=0.40, bid=0.38, ask=0.42)
    b.sell(opt(), 1, mark=0.60, bid=0.58, ask=0.62)
    b.sell(opt(), 1, mark=0.55, bid=0.53, ask=0.57)

    cash_change = b.cash - 10_000.0
    realised = sum(t["pnl"] for t in store.closed_trades())
    assert realised == pytest.approx(cash_change, abs=0.005)


def test_partial_close_amortises_entry_cost_proportionally(cfg, store):
    cfg.paper.starting_cash = 10_000.0
    b = PaperBroker(cfg, store)
    b.buy(opt(), 4, mark=0.40, bid=0.40, ask=0.40)        # entry cost 4 x 0.65
    b.sell(opt(), 1, mark=0.40, bid=0.40, ask=0.40)       # flat on price
    trade = store.closed_trades()[0]
    # One quarter of the entry cost, plus this exit's own commission.
    assert trade["costs"] == pytest.approx(0.65 + (4 * 0.65) / 4)
    assert trade["pnl"] == pytest.approx(-trade["costs"])


def test_cash_persists_across_broker_instances(cfg, store):
    cfg.paper.starting_cash = 10_000.0
    first = PaperBroker(cfg, store)
    first.buy(opt(), 1, mark=0.40, bid=0.40, ask=0.40)
    after = first.cash
    # A nightly run constructs a fresh broker; it must not reset the account.
    assert PaperBroker(cfg, store).cash == pytest.approx(after)


# ------------------------------------------------------------ book as source


def test_book_round_trips_to_holdings(broker, store):
    broker.buy(opt(qty=2, strike=15.0), 2, mark=0.40, bid=0.40, ask=0.40)
    holdings = PaperBook(store).holdings()
    assert len(holdings) == 1
    h = holdings[0]
    assert h.ticker == "F" and h.qty == 2 and h.strike == 15.0
    assert h.option_kind is OptionKind.CALL and h.expiry == EXPIRY


def test_empty_book_reports_empty(store):
    assert PaperBook(store).is_empty()


# ------------------------------------------------------------ paper-only


def test_config_rejects_non_paper_mode(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "config.yaml").write_text("execution:\n  mode: live\n")
    with pytest.raises(ValueError, match="execution.mode must be 'paper'"):
        load_config(tmp_path / "config" / "config.yaml", root=tmp_path)


def test_broker_refuses_to_construct_outside_paper_mode(cfg, store):
    cfg.execution.mode = "live"
    with pytest.raises(RuntimeError, match="paper-only"):
        PaperBroker(cfg, store)


def test_no_live_broker_adapter_is_importable():
    """Paper-only is a property of the build, not just of the config."""
    import pkgutil

    import tradebot

    names = [m.name for m in pkgutil.walk_packages(tradebot.__path__, "tradebot.")]
    banned = ("ibkr", "alpaca", "interactive_brokers", "live_broker", "tastytrade")
    assert not [n for n in names if any(b in n.lower() for b in banned)]


def test_small_account_still_rejects_unaffordable_orders(cfg, store):
    cfg.paper.starting_cash = 100.0
    b = PaperBroker(cfg, store)
    with pytest.raises(OrderRejected, match="Short by"):
        b.buy(opt(basis=13.0), 1, mark=13.0, bid=12.75, ask=13.25)
    assert store.all_positions() == []  # nothing recorded on a rejection


# ------------------------------------------------------------- performance


def test_performance_withholds_annualised_on_short_sample():
    curve = [(f"2026-01-{i:02d}", 100.0 + i) for i in range(1, 11)]
    stats = compute_performance(curve, [])
    assert stats.total_return_pct is not None
    assert stats.annualised_return_pct is None
    assert any("Annualised return withheld" in c for c in stats.caveats)


def test_performance_annualises_once_sample_is_long_enough():
    curve = [(f"2026-{1 + i // 28:02d}-{1 + i % 28:02d}", 100.0 + i) for i in range(MIN_DAYS_FOR_ANNUAL + 5)]
    assert compute_performance(curve, []).annualised_return_pct is not None


def test_max_drawdown_measured_on_equity_not_trades():
    """Winning trades must not hide an equity drawdown from open positions."""
    curve = [("2026-01-01", 100.0), ("2026-01-02", 150.0), ("2026-01-03", 90.0)]
    stats = compute_performance(curve, [{"pnl": 10.0}, {"pnl": 20.0}])
    assert stats.max_drawdown_pct == pytest.approx(40.0)  # 150 -> 90
    assert stats.wins == 2  # trades all won regardless


def test_profit_factor_and_win_rate():
    trades = [{"pnl": 100.0}, {"pnl": 50.0}, {"pnl": -75.0}]
    stats = compute_performance([], trades)
    assert stats.win_rate_pct == pytest.approx(200 / 3)
    assert stats.profit_factor == pytest.approx(2.0)


def test_profit_factor_undefined_without_losses():
    stats = compute_performance([], [{"pnl": 10.0}])
    assert stats.profit_factor is None
    assert any("no losing trades" in c for c in stats.caveats)


def test_performance_on_empty_history_is_all_caveats():
    stats = compute_performance([], [])
    assert stats.total_return_pct is None
    assert stats.trades == 0
    assert len(stats.caveats) >= 2


def test_history_progress_tracks_the_iv_rank_threshold():
    coverage = [
        {"ticker": "NVDA", "days": 5, "first": "2026-01-01", "last": "2026-01-05"},
        {"ticker": "AAPL", "days": 30, "first": "2025-12-01", "last": "2026-01-05"},
    ]
    p = history_progress(coverage, min_days=20, full_days=252)
    assert p["tickers_with_iv_rank"] == 1
    by_ticker = {t["ticker"]: t for t in p["per_ticker"]}
    assert by_ticker["NVDA"]["iv_rank_available"] is False
    assert by_ticker["NVDA"]["pct_to_minimum"] == pytest.approx(25.0)
    assert by_ticker["AAPL"]["iv_rank_available"] is True
