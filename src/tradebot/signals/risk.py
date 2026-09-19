"""Deterministic replacement for AutoHedge's Risk-Manager LLM agent.

That agent is prompted to return "recommended position size, maximum drawdown
risk, market risk exposure, overall risk score" from a language model. Position
sizing is division; VaR and Expected Shortfall are order statistics over a
return series. None of it needs a model, and a model cannot be held to an
invariant the way this can.

The sizing rule is **risk-per-trade**, not a notional percentage: the position
is sized so that price reaching the stop costs a fixed fraction of equity.
That makes the loss on a stopped trade a constant regardless of instrument or
volatility, which is the property that actually keeps an account alive. A
volatility cap and a notional cap sit on top, and the binding constraint is
always named in the output.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from .indicators import percentile

__all__ = ["RiskConfig", "PositionSize", "RiskRead", "size_position", "assess"]


@dataclass
class RiskConfig:
    risk_per_trade_pct: float = 0.01      # 1% of equity at risk if stopped
    max_position_pct: float = 0.25        # notional cap per position
    max_portfolio_heat_pct: float = 0.06  # summed open risk cap
    atr_stop_multiple: float = 2.0        # stop distance in ATR units
    reward_multiple: float = 2.0          # target distance as a multiple of risk
    var_confidence: float = 0.95
    max_annual_vol: float = 1.50          # refuse to size above this vol


@dataclass
class PositionSize:
    qty: int = 0
    notional: float = 0.0
    risk_amount: float = 0.0
    risk_pct_of_equity: float = 0.0
    stop_distance: float = 0.0
    binding_constraint: str = ""
    affordable: bool = True
    reasons: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {
            "qty": self.qty,
            "notional": round(self.notional, 2),
            "risk_amount": round(self.risk_amount, 2),
            "risk_pct_of_equity": round(self.risk_pct_of_equity * 100, 3),
            "stop_distance": round(self.stop_distance, 4),
            "binding_constraint": self.binding_constraint,
            "affordable": self.affordable,
            "reasons": self.reasons,
        }


def size_position(
    equity: float,
    entry: float,
    stop: float,
    cfg: RiskConfig,
    multiplier: int = 1,
    open_risk: float = 0.0,
) -> PositionSize:
    """Size so that being stopped out costs ``risk_per_trade_pct`` of equity.

    Returns qty 0 with a stated reason rather than a token position when any
    constraint forbids the trade. An honest zero is more useful than a size
    that quietly breaches a limit.
    """
    out = PositionSize()
    if equity <= 0:
        out.reasons.append("Account equity is zero or negative.")
        out.binding_constraint = "equity"
        return out
    if entry <= 0:
        out.reasons.append(f"Entry price must be positive, got {entry}.")
        out.binding_constraint = "entry"
        return out

    dist = abs(entry - stop)
    out.stop_distance = dist
    if dist < 1e-9:
        out.reasons.append("Stop equals entry: risk per unit is zero, so size is undefined.")
        out.binding_constraint = "stop"
        return out

    # Remaining risk budget after what is already open.
    budget = equity * cfg.risk_per_trade_pct
    headroom = equity * cfg.max_portfolio_heat_pct - open_risk
    if headroom <= 0:
        out.reasons.append(
            f"Portfolio heat cap reached: {open_risk:,.2f} already at risk against a "
            f"{cfg.max_portfolio_heat_pct * 100:.1f}% cap ({equity * cfg.max_portfolio_heat_pct:,.2f})."
        )
        out.binding_constraint = "portfolio_heat"
        return out
    allowed_risk = min(budget, headroom)

    risk_per_unit = dist * multiplier
    qty_by_risk = allowed_risk / risk_per_unit
    qty_by_notional = (equity * cfg.max_position_pct) / (entry * multiplier)
    qty = int(math.floor(min(qty_by_risk, qty_by_notional)))

    out.binding_constraint = (
        "risk_per_trade" if qty_by_risk <= qty_by_notional else "max_position_pct"
    )
    if headroom < budget:
        out.binding_constraint = "portfolio_heat"

    if qty < 1:
        cost_of_one = entry * multiplier
        risk_of_one = risk_per_unit
        out.affordable = False
        out.reasons.append(
            f"Smallest tradeable size risks {risk_of_one:,.2f} "
            f"({risk_of_one / equity * 100:.2f}% of equity) against a "
            f"{cfg.risk_per_trade_pct * 100:.2f}% budget, and costs {cost_of_one:,.2f} "
            f"to open. The account is too small for this instrument at this stop distance."
        )
        return out

    out.qty = qty
    out.notional = qty * entry * multiplier
    out.risk_amount = qty * risk_per_unit
    out.risk_pct_of_equity = out.risk_amount / equity
    return out


@dataclass
class RiskRead:
    ticker: str
    var_pct: float | None = None
    expected_shortfall_pct: float | None = None
    var_amount: float | None = None
    annual_vol: float | None = None
    historical_max_drawdown_pct: float | None = None
    risk_score: float | None = None
    band: str = "unknown"
    sizing: PositionSize | None = None
    blocks: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        def r(x: float | None, d: int = 2) -> float | None:
            return None if x is None else round(x, d)

        return {
            "ticker": self.ticker,
            "var": {
                "confidence_pct": 95,
                "one_day_var_pct": r(self.var_pct),
                "one_day_var_amount": r(self.var_amount),
                "expected_shortfall_pct": r(self.expected_shortfall_pct),
                "method": "historical simulation over the stored return series",
            },
            "annual_vol_pct": r(None if self.annual_vol is None else self.annual_vol * 100),
            "historical_max_drawdown_pct": r(self.historical_max_drawdown_pct),
            "risk_score": r(self.risk_score, 3),
            "risk_band": self.band,
            "sizing": self.sizing.to_json() if self.sizing else None,
            "blocks": self.blocks,
            "notes": self.notes,
        }


def _historical_var(returns: Sequence[float], confidence: float) -> tuple[float | None, float | None]:
    """Historical-simulation VaR and Expected Shortfall, as positive percentages.

    Historical rather than parametric on purpose: asset returns are fat-tailed,
    and a normal-assumption VaR understates exactly the days that matter.
    """
    if len(returns) < 30:
        return None, None
    cut = percentile(list(returns), (1.0 - confidence) * 100.0)
    if cut is None:
        return None, None
    tail = [r for r in returns if r <= cut]
    es = sum(tail) / len(tail) if tail else cut
    return abs(cut) * 100.0, abs(es) * 100.0


def assess(
    ticker: str,
    closes: Sequence[float],
    equity: float,
    entry: float,
    stop: float,
    cfg: RiskConfig,
    annual_vol: float | None = None,
    multiplier: int = 1,
    open_risk: float = 0.0,
) -> RiskRead:
    """Quantify the risk of a candidate position and size it."""
    from .indicators import max_drawdown

    read = RiskRead(ticker=ticker, annual_vol=annual_vol)
    c = [float(x) for x in closes]

    if len(c) >= 31 and all(x > 0 for x in c):
        rets = [c[i] / c[i - 1] - 1.0 for i in range(1, len(c))]
        read.var_pct, read.expected_shortfall_pct = _historical_var(rets, cfg.var_confidence)
        read.historical_max_drawdown_pct = max_drawdown(c) * 100.0
    else:
        read.notes.append("Fewer than 31 closes: VaR and Expected Shortfall unavailable.")

    read.sizing = size_position(equity, entry, stop, cfg, multiplier, open_risk)
    if read.var_pct is not None and read.sizing.notional:
        read.var_amount = read.sizing.notional * read.var_pct / 100.0

    # Composite risk score: higher means riskier. Volatility dominates, with
    # tail loss and historical drawdown contributing.
    parts: list[tuple[float, float]] = []
    if annual_vol is not None:
        parts.append((min(annual_vol / 1.0, 1.0), 0.5))
    if read.expected_shortfall_pct is not None:
        parts.append((min(read.expected_shortfall_pct / 10.0, 1.0), 0.3))
    if read.historical_max_drawdown_pct is not None:
        parts.append((min(read.historical_max_drawdown_pct / 60.0, 1.0), 0.2))
    if parts:
        total_w = sum(w for _, w in parts)
        read.risk_score = sum(v * w for v, w in parts) / total_w
        read.band = (
            "low" if read.risk_score < 0.25 else
            "moderate" if read.risk_score < 0.5 else
            "elevated" if read.risk_score < 0.75 else "high"
        )

    if annual_vol is not None and annual_vol > cfg.max_annual_vol:
        read.blocks.append(
            f"Realised volatility {annual_vol * 100:.0f}% exceeds the "
            f"{cfg.max_annual_vol * 100:.0f}% ceiling; no size recommended."
        )
    if not read.sizing.affordable:
        read.blocks.extend(read.sizing.reasons)
    return read
