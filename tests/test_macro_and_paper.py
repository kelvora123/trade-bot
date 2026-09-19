"""Layer 3a determinism, paper-account mechanics, and the news cost controls."""

from __future__ import annotations

from datetime import timedelta

import pytest
from conftest import ASOF, EXPIRY, make_chain

from tradebot.config import MacroWeights, load_config
from tradebot.layer3.macro import compute_macro_gate, linear_score, percentile_of
from tradebot.layer3.news import _cache_key, analyse_news, estimate_cost_usd
from tradebot.paper.broker import OrderRejected, PaperBroker
from tradebot.report.render import growth_math
from tradebot.types import AssetKind, Holding, OptionKind


class FakeProvider:
    """Deterministic macro feeds. ``calls`` proves caching/batching claims."""

    def __init__(self, vix=16.0, vix3m=18.0, trend=True):
        self.vix, self.vix3m, self.trend = vix, vix3m, trend
        self.calls: list[str] = []

    def history_closes(self, ticker, lookback_days):
        self.calls.append(ticker)
        n = 260
        base = {"^VIX": self.vix, "^VIX3M": self.vix3m, "SPY": 500.0, "RSP": 170.0,
                "HYG": 79.0, "TLT": 88.0}.get(ticker, 100.0)
        step = 0.02 if self.trend else -0.02
        return [(ASOF - timedelta(days=n - i), base + i * step) for i in range(n)]

    def spot(self, ticker):
        return 185.0

    def chain(self, ticker, expiries=None):
        return make_chain()

    def sector(self, ticker):
        return None

    def headlines(self, ticker, lookback_days):
        self.calls.append(f"news:{ticker}")
        return [{"title": f"{ticker} reports earnings", "publisher": "Reuters", "ts": 1.0, "summary": ""}]


# ------------------------------------------------------------------- scoring


def test_linear_score_clamps_and_inverts():
    assert linear_score(12, 40, 12) == 100.0
    assert linear_score(40, 40, 12) == 0.0
    assert linear_score(100, 40, 12) == 0.0   # clamped
    assert linear_score(0, 40, 12) == 100.0   # clamped


def test_linear_score_degenerate_range_is_midpoint():
    assert linear_score(5, 10, 10) == 50.0


def test_percentile_of_empty_is_none():
    assert percentile_of(1.0, []) is None


# ---------------------------------------------------------------- macro gate


def test_macro_gate_is_deterministic(cfg):
    """Same data in, same score out -- the defining property of this layer."""
    a = compute_macro_gate(FakeProvider(), cfg, ASOF)
    b = compute_macro_gate(FakeProvider(), cfg, ASOF)
    assert a.score == b.score
    assert a.components == b.components


def test_macro_gate_in_range_and_labelled(cfg):
    g = compute_macro_gate(FakeProvider(), cfg, ASOF)
    assert 0.0 <= g.score <= 100.0
    assert g.regime in {"calm", "constructive", "mixed", "cautious", "stressed"}


def test_low_vix_scores_calmer_than_high_vix(cfg):
    calm = compute_macro_gate(FakeProvider(vix=12.0, vix3m=16.0), cfg, ASOF)
    panic = compute_macro_gate(FakeProvider(vix=45.0, vix3m=38.0), cfg, ASOF)
    assert calm.score > panic.score


def test_backwardation_scores_worse_than_contango(cfg):
    contango = compute_macro_gate(FakeProvider(vix=16.0, vix3m=20.0), cfg, ASOF)
    backward = compute_macro_gate(FakeProvider(vix=20.0, vix3m=16.0), cfg, ASOF)
    assert contango.components["vix_term_structure"]["score"] > \
        backward.components["vix_term_structure"]["score"]
    assert "contango" in contango.components["vix_term_structure"]["state"]


def test_missing_feeds_are_reported_and_renormalised(cfg):
    class Dead(FakeProvider):
        def history_closes(self, ticker, lookback_days):
            if ticker == "^VIX3M":
                return []
            return super().history_closes(ticker, lookback_days)

    g = compute_macro_gate(Dead(), cfg, ASOF)
    assert "vix_term_structure" in g.missing
    assert 0.0 <= g.score <= 100.0  # still a valid blend


def test_total_feed_failure_yields_neutral_unknown(cfg):
    class AllDead(FakeProvider):
        def history_closes(self, ticker, lookback_days):
            return []

    g = compute_macro_gate(AllDead(), cfg, ASOF)
    assert g.regime == "unknown" and g.score == 50.0


def test_weights_must_sum_to_one():
    bad = MacroWeights(vix_level=0.5, vix_percentile=0.5, vix_term_structure=0.5,
                       breadth=0.0, credit_spread=0.0)
    with pytest.raises(ValueError, match="must sum to 1.0"):
        bad.validate()


def test_config_rejects_unknown_keys(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "config.yaml").write_text("valuation:\n  risk_fre_rate: 0.05\n")
    with pytest.raises(ValueError, match="unknown config key"):
        load_config(tmp_path / "config" / "config.yaml", root=tmp_path)


def test_config_rejects_inverted_iv_thresholds(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "config.yaml").write_text(
        "iv_env:\n  rich_threshold: 20.0\n  cheap_threshold: 80.0\n"
    )
    with pytest.raises(ValueError, match="cheap_threshold"):
        load_config(tmp_path / "config" / "config.yaml", root=tmp_path)


# --------------------------------------------------------------- paper broker


def test_buy_deducts_premium_times_multiplier(cfg, store, nvda_call):
    cfg.paper.starting_cash = 10_000.0
    b = PaperBroker(cfg, store)
    fill = b.buy(nvda_call, 1, mark=13.0, bid=12.75, ask=13.25)
    # 1 contract x 100 shares x ~13.00 + 0.65 commission
    assert fill.cash_delta == pytest.approx(-(100 * fill.price + 0.65))
    assert b.cash == pytest.approx(10_000.0 - (100 * fill.price + 0.65))


def test_market_buy_fills_above_mid(cfg, store, nvda_call):
    cfg.paper.starting_cash = 10_000.0
    b = PaperBroker(cfg, store)
    fill = b.buy(nvda_call, 1, mark=13.0, bid=12.75, ask=13.25)
    assert fill.price > 13.0          # crossed the spread
    assert fill.slippage > 0


def test_market_sell_fills_below_mid(cfg, store, nvda_call):
    cfg.paper.starting_cash = 10_000.0
    b = PaperBroker(cfg, store)
    fill = b.sell(nvda_call, 1, mark=13.0, bid=12.75, ask=13.25)
    assert fill.price < 13.0


def test_hundred_pounds_cannot_buy_a_real_contract(cfg, store, nvda_call):
    """The structural blocker for a GBP100 account, surfaced as a rejection."""
    cfg.paper.starting_cash = 100.0
    b = PaperBroker(cfg, store)
    with pytest.raises(OrderRejected) as exc:
        b.buy(nvda_call, 1, mark=13.0, bid=12.75, ask=13.25)
    msg = str(exc.value)
    assert "Insufficient cash" in msg and "Short by" in msg
    assert b.cash == 100.0  # unchanged


def test_affordability_report_has_no_side_effects(cfg, store, nvda_call):
    cfg.paper.starting_cash = 100.0
    b = PaperBroker(cfg, store)
    r = b.affordability(nvda_call, 1, 13.0)
    assert r["affordable"] is False
    assert r["total_cost"] == pytest.approx(1300.65)
    assert r["shortfall"] == pytest.approx(1200.65)
    assert b.cash == 100.0


def test_cheap_contract_is_affordable_on_small_account(cfg, store):
    cfg.paper.starting_cash = 100.0
    b = PaperBroker(cfg, store)
    cheap = Holding("F", AssetKind.OPTION, 1, 0.30, OptionKind.CALL, 12.0, EXPIRY)
    fill = b.buy(cheap, 1, mark=0.30, bid=0.28, ask=0.32)
    assert b.cash < 100.0 and b.cash > 60.0
    assert fill.qty == 1


def test_fills_are_persisted(cfg, store, nvda_call):
    cfg.paper.starting_cash = 10_000.0
    b = PaperBroker(cfg, store)
    b.buy(nvda_call, 1, mark=13.0, bid=12.75, ask=13.25)
    rows = store.conn.execute("SELECT * FROM paper_fills").fetchall()
    assert len(rows) == 1 and rows[0]["side"] == "buy"


def test_equity_curve_recorded(cfg, store):
    b = PaperBroker(cfg, store)
    b.mark_to_market(500.0, ASOF)
    curve = store.equity_curve()
    assert curve == [(ASOF.isoformat(), 600.0)]  # 100 cash + 500 positions


def test_rejects_non_positive_qty(cfg, store, nvda_call):
    b = PaperBroker(cfg, store)
    with pytest.raises(OrderRejected):
        b.buy(nvda_call, 0, mark=1.0)


# ------------------------------------------------------------------- news


def test_news_disabled_makes_no_calls(cfg, store):
    p = FakeProvider()
    r = analyse_news(p, ["NVDA"], cfg, store, ASOF)
    assert r.api_calls == 0
    assert p.calls == []  # never even fetched headlines
    assert "disabled" in r.notes[0]


def test_news_without_api_key_makes_no_calls(cfg, store, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    cfg.news.enabled = True
    r = analyse_news(FakeProvider(), ["NVDA"], cfg, store, ASOF)
    assert r.api_calls == 0
    assert "ANTHROPIC_API_KEY not set" in r.notes[0]


def test_news_cache_key_is_order_independent():
    a = {"NVDA": [{"title": "B"}, {"title": "A"}]}
    b = {"NVDA": [{"title": "A"}, {"title": "B"}]}
    assert _cache_key("m", a) == _cache_key("m", b)


def test_news_cache_key_changes_with_model():
    g = {"NVDA": [{"title": "A"}]}
    assert _cache_key("claude-haiku-4-5", g) != _cache_key("claude-opus-5", g)


def test_news_cache_hit_avoids_api_call(cfg, store, monkeypatch):
    """An unchanged headline set must cost nothing."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    cfg.news.enabled = True
    grouped = {"NVDA": [{"title": "NVDA reports earnings"}]}
    store.put_news_cache(
        _cache_key(cfg.news.model, grouped), ASOF,
        {"per_ticker": [{"ticker": "NVDA", "sentiment": "neutral"}]}, "now",
    )
    r = analyse_news(FakeProvider(), ["NVDA"], cfg, store, ASOF)
    assert r.cached and r.api_calls == 0
    assert r.per_ticker[0]["ticker"] == "NVDA"


def test_news_respects_daily_call_cap(cfg, store, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    cfg.news.enabled = True
    cfg.news.max_calls_per_day = 2
    for _ in range(2):
        store.record_llm_usage(ASOF, "now", cfg.news.model, 10, 10, "news_analysis")
    r = analyse_news(FakeProvider(), ["NVDA"], cfg, store, ASOF)
    assert r.api_calls == 0
    assert any("cap reached" in n for n in r.notes)


def test_cost_estimate_uses_list_prices():
    assert estimate_cost_usd("claude-haiku-4-5", 1_000_000, 0) == pytest.approx(1.00)
    assert estimate_cost_usd("claude-opus-5", 0, 1_000_000) == pytest.approx(25.00)
    assert estimate_cost_usd("nonexistent-model", 100, 100) is None


# ------------------------------------------------------------- growth math


def test_growth_math_reports_the_real_multiple():
    g = growth_math(100, 400_000)
    assert g["multiple_required"] == pytest.approx(4000.0)
    assert g["monthly_return_required_pct"]["2 years"] > 40.0
    assert g["months_required_at_rate"]["2%/month"] > 400


def test_growth_math_not_applicable_when_target_below_start():
    assert growth_math(1000, 500)["applicable"] is False
