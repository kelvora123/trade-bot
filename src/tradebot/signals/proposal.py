"""Deterministic replacement for AutoHedge's Execution-Agent.

That agent is prompted to emit "order type, quantity, entry price, stop loss,
take profit, time in force" from a language model, and AutoHedge then executes
it. This module computes the same fields arithmetically and **stops there**.

The difference is the point of the whole design. A proposal here is a
candidate, printed for a human, that has to be entered by hand with
``tradebot buy``. Nothing in this package can place an order on its own: the
build is paper-only and the proposal engine has no broker handle at all.

Entry is the last close, the stop is an ATR multiple away, and the target is a
fixed multiple of the risk distance -- so the reward-to-risk ratio is a
property of the construction rather than a number anyone picked.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .quant import QuantRead
from .risk import RiskConfig, RiskRead

__all__ = ["Proposal", "build_proposal"]


@dataclass
class Proposal:
    ticker: str
    asof: str
    action: str = "no_trade"
    direction: str = "flat"
    qty: int = 0
    entry: float | None = None
    stop: float | None = None
    target: float | None = None
    risk_per_unit: float | None = None
    reward_risk: float | None = None
    conviction: float | None = None
    risk_band: str = "unknown"
    rationale: list[str] = field(default_factory=list)
    blocks: list[str] = field(default_factory=list)

    @property
    def actionable(self) -> bool:
        return self.action == "propose" and self.qty > 0

    def to_json(self) -> dict[str, Any]:
        def r(x: float | None, d: int = 4) -> float | None:
            return None if x is None else round(x, d)

        return {
            "ticker": self.ticker,
            "asof": self.asof,
            "action": self.action,
            "direction": self.direction,
            "qty": self.qty,
            "entry": r(self.entry),
            "stop": r(self.stop),
            "target": r(self.target),
            "risk_per_unit": r(self.risk_per_unit),
            "reward_to_risk": r(self.reward_risk, 2),
            "conviction": r(self.conviction, 3),
            "risk_band": self.risk_band,
            "rationale": self.rationale,
            "blocks": self.blocks,
            "execution": (
                "PROPOSAL ONLY. Nothing has been ordered. This package is paper-only "
                "and has no broker adapter; enter it yourself with `tradebot buy` if "
                "you agree with it."
            ),
        }

    def as_command(self) -> str | None:
        """The exact paper-trading command this proposal corresponds to."""
        if not self.actionable or self.entry is None:
            return None
        verb = "buy" if self.direction == "long" else "sell"
        parts = [
            f"tradebot {verb} {self.ticker}", f"--qty {self.qty}",
            f"--price {self.entry:.4f}", "--shares",
        ]
        if self.stop is not None:
            parts.append(f"--stop {self.stop:.4f}")
        if self.target is not None:
            parts.append(f"--target {self.target:.4f}")
        return " ".join(parts)


def build_proposal(
    quant: QuantRead,
    risk: RiskRead,
    cfg: RiskConfig,
    min_conviction: float = 0.55,
    min_reward_risk: float = 1.5,
) -> Proposal:
    """Assemble entry, stop, target and size from the two deterministic reads."""
    p = Proposal(
        ticker=quant.ticker,
        asof=quant.asof,
        direction=quant.direction,
        conviction=quant.conviction,
        risk_band=risk.band,
    )

    if quant.close is None or quant.atr is None:
        p.blocks.append("No usable price or ATR; cannot construct an order.")
        return p
    if quant.direction == "flat":
        p.blocks.append("No directional read: the trend filter is undecided.")
        return p

    entry = quant.close
    stop_dist = quant.atr * cfg.atr_stop_multiple
    long = quant.direction == "long"
    stop = entry - stop_dist if long else entry + stop_dist
    target = entry + stop_dist * cfg.reward_multiple if long else entry - stop_dist * cfg.reward_multiple

    p.entry, p.stop, p.target = entry, stop, target
    p.risk_per_unit = stop_dist
    p.reward_risk = cfg.reward_multiple
    p.qty = risk.sizing.qty if risk.sizing else 0

    p.rationale = [
        f"Trend {quant.direction} on the 20/50 EMA stack"
        + (f" (score {quant.trend_score:.2f})" if quant.trend_score is not None else ""),
        f"Entry {entry:,.4f} at the last close; stop {stop:,.4f} is "
        f"{cfg.atr_stop_multiple:g}x ATR ({quant.atr:,.4f}) away",
        f"Target {target:,.4f} gives {cfg.reward_multiple:g}:1 reward to risk by construction",
    ]
    if quant.rsi is not None:
        p.rationale.append(f"RSI(14) at {quant.rsi:.1f}")
    if quant.volatility_annualised is not None:
        p.rationale.append(f"Realised volatility {quant.volatility_annualised * 100:.0f}% annualised")
    if risk.sizing and risk.sizing.qty:
        p.rationale.append(
            f"Size {risk.sizing.qty} risks {risk.sizing.risk_amount:,.2f} "
            f"({risk.sizing.risk_pct_of_equity * 100:.2f}% of equity), bound by "
            f"{risk.sizing.binding_constraint}"
        )
    if quant.hit_rate is not None:
        p.rationale.append(
            f"This trend state preceded a {'gain' if long else 'decline'} "
            f"{quant.hit_rate * 100:.0f}% of the time over {quant.hit_rate_samples} "
            "past instances in this series (a base rate, not a forecast)"
        )

    # ---- gates ----------------------------------------------------------
    p.blocks.extend(risk.blocks)
    if quant.conviction is not None and quant.conviction < min_conviction:
        p.blocks.append(
            f"Conviction {quant.conviction:.2f} is below the {min_conviction:.2f} threshold."
        )
    if cfg.reward_multiple < min_reward_risk:
        p.blocks.append(
            f"Reward-to-risk {cfg.reward_multiple:g} is below the {min_reward_risk:g} minimum."
        )
    if p.qty < 1 and not p.blocks:
        p.blocks.append("Risk sizing returned zero units.")

    p.action = "no_trade" if p.blocks else "propose"
    return p
