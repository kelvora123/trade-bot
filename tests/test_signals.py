"""The deterministic signal layer that replaces the LLM agent chain.

The headline test here is `test_pipeline_is_deterministic`: identical input
yields byte-identical output, every time. That is the property an LLM-agent
pipeline cannot offer at any temperature, and it is why this layer is
arithmetic rather than inference.
"""

from __future__ import annotations

import json
import random

import pytest

from tradebot.signals import indicators as ind
from tradebot.signals.proposal import build_proposal
from tradebot.signals.quant import analyse
from tradebot.signals.risk import RiskConfig, assess, size_position


def series(n=200, drift=0.0015, vol=0.012, seed=11, start=100.0):
    rng = random.Random(seed)
    px, bars = start, []
    for i in range(n):
        px *= 1 + rng.gauss(drift, vol)
        hi = px * (1 + abs(rng.gauss(0, 0.005)))
        lo = px * (1 - abs(rng.gauss(0, 0.005)))
        bars.append({"asof": f"d{i}", "open": px, "high": hi, "low": lo,
                     "close": px, "volume": 1_000_000 * (1 + rng.random())})
    return bars


# ----------------------------------------------------------------- indicators


def test_sma_is_the_mean_of_its_window():
    assert ind.sma([1, 2, 3, 4, 5], 3)[2:] == [2.0, 3.0, 4.0]


def test_sma_is_none_before_the_window_fills():
    assert ind.sma([1, 2, 3], 3)[:2] == [None, None]


def test_ema_matches_the_recurrence():
    v = [1, 2, 3, 4, 5]
    out = ind.ema(v, 3)
    alpha = 2 / 4
    expected = 2.0  # seeded on the first full SMA
    for x in v[3:]:
        expected = alpha * x + (1 - alpha) * expected
    assert out[-1] == pytest.approx(expected)


def test_rsi_matches_wilders_published_example():
    closes = [44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42, 45.84,
              46.08, 45.89, 46.03, 45.61, 46.28, 46.28]
    assert ind.rsi(closes, 14)[14] == pytest.approx(70.5, abs=0.1)


def test_rsi_stays_in_bounds():
    for s in (series(seed=1), series(seed=2, drift=-0.003)):
        for v in ind.rsi([b["close"] for b in s], 14):
            if v is not None:
                assert 0.0 <= v <= 100.0


def test_rsi_is_100_on_an_unbroken_advance():
    assert ind.rsi(list(range(1, 40)), 14)[-1] == pytest.approx(100.0)


def test_atr_is_never_negative():
    s = series()
    for v in ind.atr([b["high"] for b in s], [b["low"] for b in s], [b["close"] for b in s]):
        if v is not None:
            assert v >= 0


def test_donchian_excludes_the_current_bar():
    """A breakout test that includes today's bar can never fire."""
    highs = [1, 2, 3, 10, 4]
    lows = [1, 1, 1, 1, 1]
    dn, up = ind.donchian(highs, lows, 3)
    assert up[3] == 3      # the prior three bars, not the 10 on bar 3
    assert up[4] == 10     # now the 10 is in the window


def test_bollinger_bands_straddle_the_mean():
    s = [b["close"] for b in series()]
    lo, mid, hi = ind.bollinger(s, 20)
    assert lo[-1] < mid[-1] < hi[-1]


def test_indicators_never_look_ahead():
    """Truncating the future must not change any past value."""
    s = [b["close"] for b in series(n=120)]
    full = ind.rsi(s, 14)
    partial = ind.rsi(s[:100], 14)
    assert full[:100] == partial


def test_max_drawdown_measures_peak_to_trough():
    assert ind.max_drawdown([100, 120, 90, 110]) == pytest.approx(0.25)


def test_percentile_interpolates():
    assert ind.percentile([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 50) == pytest.approx(5.5)


def test_realised_vol_rises_with_noise():
    calm = ind.realised_vol([b["close"] for b in series(vol=0.004, seed=3)], 20)[-1]
    wild = ind.realised_vol([b["close"] for b in series(vol=0.03, seed=3)], 20)[-1]
    assert wild > calm * 2


# ---------------------------------------------------------------------- quant


def test_uptrend_reads_long_and_downtrend_reads_short():
    assert analyse("UP", series(drift=0.004, seed=5), "d").direction == "long"
    assert analyse("DN", series(drift=-0.004, seed=5), "d").direction == "short"


def test_scores_stay_in_unit_range():
    q = analyse("T", series(), "d")
    for v in (q.trend_score, q.momentum_score, q.volume_score, q.conviction):
        if v is not None:
            assert 0.0 <= v <= 1.0


def test_short_history_degrades_without_crashing():
    q = analyse("T", series(n=10), "d")
    assert q.trend_score is None
    assert any("bars of history" in n for n in q.notes)


def test_key_levels_bracket_the_price():
    q = analyse("T", series(), "d")
    assert q.levels["support"] < q.levels["resistance"]
    assert q.levels["bollinger_lower"] < q.levels["bollinger_upper"]


def test_conviction_is_never_called_a_probability():
    """False precision is the specific failure this design exists to avoid."""
    payload = analyse("T", series(), "d").to_json()
    assert "probability" not in json.dumps(payload["scores"]).lower()
    assert "NOT a probability" in payload["conviction_note"]


def test_hit_rate_is_a_measured_frequency_with_its_sample_size():
    q = analyse("T", series(n=300), "d")
    if q.hit_rate is not None:
        assert 0.0 <= q.hit_rate <= 1.0
        assert q.hit_rate_samples >= 10


# ----------------------------------------------------------------------- risk


def test_being_stopped_costs_the_configured_fraction():
    """The core sizing invariant: a stop-out is a fixed cost, by construction."""
    cfg = RiskConfig(risk_per_trade_pct=0.01, max_position_pct=1.0)
    s = size_position(equity=10_000, entry=100.0, stop=95.0, cfg=cfg)
    loss = s.qty * (100.0 - 95.0)
    assert loss <= 100.0                      # never over the 1% budget
    assert loss == pytest.approx(100.0, abs=5.0)  # and close to it


def test_wider_stop_means_smaller_size():
    cfg = RiskConfig(max_position_pct=1.0)
    tight = size_position(10_000, 100.0, 98.0, cfg).qty
    wide = size_position(10_000, 100.0, 90.0, cfg).qty
    assert wide < tight


def test_risk_stays_constant_as_the_stop_widens():
    """Different instruments, different stops -- same money at risk."""
    cfg = RiskConfig(risk_per_trade_pct=0.01, max_position_pct=1.0)
    for stop in (99.0, 95.0, 90.0, 80.0):
        s = size_position(100_000, 100.0, stop, cfg)
        assert s.risk_amount <= 1_000.0
        assert s.risk_amount == pytest.approx(1_000.0, rel=0.02)


def test_notional_cap_can_bind_instead_of_risk():
    cfg = RiskConfig(risk_per_trade_pct=0.05, max_position_pct=0.10)
    s = size_position(10_000, 100.0, 99.0, cfg)
    assert s.binding_constraint == "max_position_pct"
    assert s.notional <= 1_000.0 + 100.0


def test_portfolio_heat_cap_blocks_further_size():
    cfg = RiskConfig(max_portfolio_heat_pct=0.06)
    s = size_position(10_000, 100.0, 95.0, cfg, open_risk=600.0)
    assert s.qty == 0
    assert s.binding_constraint == "portfolio_heat"
    assert any("heat cap" in r for r in s.reasons)


def test_account_too_small_returns_zero_with_a_reason():
    """An honest zero beats a size that quietly breaches the budget."""
    cfg = RiskConfig(risk_per_trade_pct=0.01)
    s = size_position(equity=100.0, entry=500.0, stop=450.0, cfg=cfg, multiplier=100)
    assert s.qty == 0 and not s.affordable
    assert any("too small" in r for r in s.reasons)


def test_zero_stop_distance_is_rejected():
    s = size_position(10_000, 100.0, 100.0, RiskConfig())
    assert s.qty == 0 and s.binding_constraint == "stop"


def test_negative_equity_is_rejected():
    assert size_position(-5.0, 100.0, 95.0, RiskConfig()).qty == 0


def test_expected_shortfall_is_at_least_var():
    """ES averages the tail beyond VaR, so it can never be smaller."""
    closes = [b["close"] for b in series(n=300, vol=0.02)]
    r = assess("T", closes, 10_000, 100.0, 95.0, RiskConfig())
    assert r.var_pct is not None
    assert r.expected_shortfall_pct >= r.var_pct - 1e-9


def test_var_unavailable_on_short_history():
    r = assess("T", [100.0] * 10, 10_000, 100.0, 95.0, RiskConfig())
    assert r.var_pct is None
    assert any("Fewer than 31" in n for n in r.notes)


def test_excess_volatility_blocks_the_trade():
    r = assess("T", [b["close"] for b in series()], 10_000, 100.0, 95.0,
               RiskConfig(max_annual_vol=0.5), annual_vol=2.0)
    assert any("exceeds" in b for b in r.blocks)


def test_risk_band_rises_with_volatility():
    closes = [b["close"] for b in series()]
    calm = assess("T", closes, 10_000, 100.0, 95.0, RiskConfig(), annual_vol=0.10)
    wild = assess("T", closes, 10_000, 100.0, 95.0, RiskConfig(), annual_vol=1.20)
    assert wild.risk_score > calm.risk_score


# ------------------------------------------------------------------ proposal


def _proposal(bars, equity=100_000.0, cfg=None, **kw):
    cfg = cfg or RiskConfig(max_annual_vol=99.0)
    q = analyse("T", bars, "2026-09-18")
    stop = q.close - q.atr * cfg.atr_stop_multiple if q.direction == "long" \
        else q.close + q.atr * cfg.atr_stop_multiple
    r = assess("T", [b["close"] for b in bars], equity, q.close, stop, cfg,
               q.volatility_annualised)
    return build_proposal(q, r, cfg, **kw)


def test_reward_to_risk_holds_by_construction():
    p = _proposal(series(drift=0.004, seed=9))
    assert p.reward_risk == 2.0
    assert abs(p.target - p.entry) == pytest.approx(2 * abs(p.entry - p.stop), rel=1e-9)


def test_long_stop_sits_below_entry_and_target_above():
    p = _proposal(series(drift=0.004, seed=9))
    assert p.direction == "long"
    assert p.stop < p.entry < p.target


def test_short_stop_sits_above_entry_and_target_below():
    p = _proposal(series(drift=-0.004, seed=9), min_conviction=0.0)
    assert p.direction == "short"
    assert p.target < p.entry < p.stop


def test_low_conviction_blocks_the_proposal():
    p = _proposal(series(drift=0.004, seed=9), min_conviction=0.99)
    assert p.action == "no_trade"
    assert any("Conviction" in b for b in p.blocks)


def test_proposal_never_claims_to_have_executed():
    p = _proposal(series(drift=0.004, seed=9))
    text = json.dumps(p.to_json())
    assert "PROPOSAL ONLY" in text
    assert "Nothing has been ordered" in text


def test_command_is_emitted_only_for_an_actionable_proposal():
    good = _proposal(series(drift=0.004, seed=9))
    blocked = _proposal(series(drift=0.004, seed=9), min_conviction=0.99)
    assert good.as_command().startswith("tradebot buy")
    assert blocked.as_command() is None


def test_tiny_account_is_told_why_not_the_trade():
    p = _proposal(series(drift=0.004, seed=9), equity=50.0)
    assert p.action == "no_trade"
    assert p.blocks


# ----------------------------------------------------------- the whole point


def test_pipeline_is_deterministic():
    """Same input, byte-identical output. Every time.

    This is the property the LLM-agent design cannot provide, and the reason
    this layer computes rather than prompts.
    """
    bars = series(drift=0.004, seed=42)
    outputs = {json.dumps(_proposal(bars).to_json(), sort_keys=True) for _ in range(12)}
    assert len(outputs) == 1


def test_pipeline_makes_no_network_or_model_calls(monkeypatch):
    """Nothing in this layer may reach out. It is arithmetic, start to finish."""
    import socket

    def blocked(*a, **k):
        raise AssertionError("the signal layer attempted a network connection")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    p = _proposal(series(drift=0.004, seed=13))
    assert p.action in {"propose", "no_trade"}


def test_no_llm_imports_in_the_signal_layer():
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[1] / "src" / "tradebot" / "signals"
    for path in root.glob("*.py"):
        body = path.read_text().lower()
        for banned in ("import anthropic", "import openai", "from openai", "swarms"):
            assert banned not in body, f"{path.name} references {banned}"
