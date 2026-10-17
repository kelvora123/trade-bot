"""Performance statistics for the paper account.

The point of a paper period is to answer one question honestly: is this
working, or has it just not lost yet? These are the numbers that answer it.

Two deliberate choices:

* **Max drawdown comes from the equity curve, not from closed trades.** A book
  can show a string of winning round-trips while open positions bleed; only the
  equity curve sees that.
* **Nothing is annualised below a minimum sample.** A 9% gain over eleven days
  annualises to something absurd, and reporting it would be the single most
  misleading number here. Below ``MIN_DAYS_FOR_ANNUAL`` the field is ``None``
  with a stated reason.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

__all__ = ["PerformanceStats", "compute_performance", "MIN_DAYS_FOR_ANNUAL", "MIN_TRADES_FOR_RATIO"]

MIN_DAYS_FOR_ANNUAL = 60
MIN_TRADES_FOR_RATIO = 10
TRADING_DAYS = 252


@dataclass
class PerformanceStats:
    days: int
    start_equity: float | None = None
    end_equity: float | None = None
    total_return_pct: float | None = None
    annualised_return_pct: float | None = None
    max_drawdown_pct: float | None = None
    max_drawdown_amount: float | None = None
    best_day_pct: float | None = None
    worst_day_pct: float | None = None
    volatility_annualised_pct: float | None = None
    sharpe: float | None = None
    trades: int = 0
    wins: int = 0
    losses: int = 0
    win_rate_pct: float | None = None
    profit_factor: float | None = None
    avg_win: float | None = None
    avg_loss: float | None = None
    largest_win: float | None = None
    largest_loss: float | None = None
    realised_pnl: float = 0.0
    caveats: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        def r(x: float | None, d: int = 2) -> float | None:
            return None if x is None else round(x, d)

        return {
            "equity": {
                "days_tracked": self.days,
                "start": r(self.start_equity),
                "end": r(self.end_equity),
                "total_return_pct": r(self.total_return_pct),
                "annualised_return_pct": r(self.annualised_return_pct),
                "max_drawdown_pct": r(self.max_drawdown_pct),
                "max_drawdown_amount": r(self.max_drawdown_amount),
                "best_day_pct": r(self.best_day_pct),
                "worst_day_pct": r(self.worst_day_pct),
                "volatility_annualised_pct": r(self.volatility_annualised_pct),
                "sharpe": r(self.sharpe),
            },
            "trades": {
                "closed": self.trades,
                "wins": self.wins,
                "losses": self.losses,
                "win_rate_pct": r(self.win_rate_pct),
                "profit_factor": r(self.profit_factor),
                "avg_win": r(self.avg_win),
                "avg_loss": r(self.avg_loss),
                "largest_win": r(self.largest_win),
                "largest_loss": r(self.largest_loss),
                "realised_pnl": r(self.realised_pnl),
            },
            "caveats": self.caveats,
        }


def _max_drawdown(equities: list[float]) -> tuple[float | None, float | None]:
    """Deepest peak-to-trough decline, as a percentage and an amount."""
    if len(equities) < 2:
        return None, None
    peak = equities[0]
    worst_pct, worst_amt = 0.0, 0.0
    for e in equities:
        peak = max(peak, e)
        if peak <= 0:
            continue
        drop = peak - e
        if drop / peak > worst_pct:
            worst_pct, worst_amt = drop / peak, drop
    return worst_pct * 100.0, worst_amt


def compute_performance(
    curve: list[tuple[str, float]],
    trades: list[dict[str, Any]],
    risk_free_rate: float = 0.045,
) -> PerformanceStats:
    """Summarise the equity curve and closed round-trips."""
    stats = PerformanceStats(days=len(curve))

    # ------------------------------------------------------------- equity
    if len(curve) >= 2:
        equities = [e for _, e in curve]
        stats.start_equity, stats.end_equity = equities[0], equities[-1]

        if equities[0] > 0:
            stats.total_return_pct = (equities[-1] / equities[0] - 1.0) * 100.0

        stats.max_drawdown_pct, stats.max_drawdown_amount = _max_drawdown(equities)

        rets = [
            equities[i] / equities[i - 1] - 1.0
            for i in range(1, len(equities))
            if equities[i - 1] > 0
        ]
        if rets:
            stats.best_day_pct = max(rets) * 100.0
            stats.worst_day_pct = min(rets) * 100.0

        if len(rets) >= 2:
            mean = sum(rets) / len(rets)
            var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
            sd = math.sqrt(var)
            stats.volatility_annualised_pct = sd * math.sqrt(TRADING_DAYS) * 100.0
            if sd > 1e-12:
                daily_rf = risk_free_rate / TRADING_DAYS
                stats.sharpe = (mean - daily_rf) / sd * math.sqrt(TRADING_DAYS)

        # Annualising a short sample produces a number that is worse than no
        # number, so it is withheld with the reason stated.
        if stats.days >= MIN_DAYS_FOR_ANNUAL and equities[0] > 0 and equities[-1] > 0:
            years = stats.days / 365.0
            stats.annualised_return_pct = ((equities[-1] / equities[0]) ** (1 / years) - 1) * 100.0
        else:
            stats.caveats.append(
                f"Annualised return withheld: {stats.days} days tracked, "
                f"{MIN_DAYS_FOR_ANNUAL} needed. Extrapolating a short sample overstates it."
            )
    else:
        stats.caveats.append("Fewer than two equity points; no return statistics yet.")

    # ------------------------------------------------------------- trades
    stats.trades = len(trades)
    if trades:
        pnls = [float(t["pnl"]) for t in trades]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p < 0]
        stats.wins, stats.losses = len(wins), len(losses)
        stats.realised_pnl = sum(pnls)
        stats.win_rate_pct = len(wins) / len(pnls) * 100.0
        stats.avg_win = sum(wins) / len(wins) if wins else None
        stats.avg_loss = sum(losses) / len(losses) if losses else None
        stats.largest_win = max(wins) if wins else None
        stats.largest_loss = min(losses) if losses else None

        gross_loss = abs(sum(losses))
        if gross_loss > 1e-12:
            stats.profit_factor = sum(wins) / gross_loss
        elif wins:
            stats.caveats.append("Profit factor undefined: no losing trades yet.")

        if stats.trades < MIN_TRADES_FOR_RATIO:
            stats.caveats.append(
                f"{stats.trades} closed trade(s): win rate and profit factor are not yet "
                f"meaningful ({MIN_TRADES_FOR_RATIO}+ needed before they mean much)."
            )
    else:
        stats.caveats.append("No closed trades yet.")

    return stats


def history_progress(
    coverage: list[dict[str, Any]], min_days: int, full_days: int
) -> dict[str, Any]:
    """How far the stored snapshot history is from supporting an IV rank."""
    per_ticker = []
    for row in coverage:
        days = int(row["days"])
        per_ticker.append(
            {
                "ticker": row["ticker"],
                "days": days,
                "first": row["first"],
                "last": row["last"],
                "iv_rank_available": days >= min_days,
                "pct_to_minimum": round(min(days / min_days, 1.0) * 100, 1),
                "pct_to_full_lookback": round(min(days / full_days, 1.0) * 100, 1),
            }
        )
    ready = [t for t in per_ticker if t["iv_rank_available"]]
    return {
        "min_days_for_iv_rank": min_days,
        "full_lookback_days": full_days,
        "tickers_tracked": len(per_ticker),
        "tickers_with_iv_rank": len(ready),
        "per_ticker": per_ticker,
    }
