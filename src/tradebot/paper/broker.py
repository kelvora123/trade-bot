"""Paper trading account.

Simulated fills with realistic commission and slippage, persisted to SQLite so
an equity curve survives across runs. No live-broker adapter ships in this
repo: paper is the only execution mode, by design.

The affordability check is the point of this module rather than an afterthought.
A US equity option contract controls 100 shares, so a quoted premium of $1.20
costs $120 plus commission to buy -- one contract, no diversification. Small
accounts do not discover this from a backtest; they discover it from a rejected
order, so this rejects loudly and says exactly what was short.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from ..config import Config
from ..store import Store
from ..types import Holding, utcnow
from .book import PaperBook, PositionChange

log = logging.getLogger(__name__)

__all__ = ["PaperBroker", "OrderRejected", "FillResult"]


class OrderRejected(Exception):
    """Raised when an order cannot be filled. The message says why."""


@dataclass
class FillResult:
    symbol: str
    side: str
    qty: float
    price: float
    commission: float
    slippage: float
    cash_delta: float
    change: PositionChange | None = None
    notes: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "side": self.side,
            "qty": self.qty,
            "fill_price": round(self.price, 4),
            "commission": round(self.commission, 2),
            "slippage_cost": round(self.slippage, 2),
            "cash_delta": round(self.cash_delta, 2),
            "position": self.change.to_json() if self.change else None,
            "notes": self.notes,
        }


class PaperBroker:
    """A simulated account. Every fill is recorded; nothing touches a market."""

    def __init__(self, cfg: Config, store: Store, cash: float | None = None) -> None:
        if cfg.execution.mode != "paper":
            # Defence in depth: config validation rejects this too, but the
            # broker refuses to construct rather than trust that it ran.
            raise RuntimeError(
                f"PaperBroker constructed with execution.mode={cfg.execution.mode!r}. "
                "This build is paper-only and ships no live-broker adapter."
            )
        self.cfg = cfg
        self.store = store
        self.currency = cfg.paper.currency
        self.book = PaperBook(store)
        # Cash persists across runs, seeded from config on first use -- otherwise
        # every nightly run would silently reset the account to its opening balance.
        self.cash = store.load_cash(cfg.paper.starting_cash) if cash is None else cash

    # ------------------------------------------------------------- pricing

    def _fill_price(
        self, side: str, bid: float | None, ask: float | None, mark: float
    ) -> tuple[float, float]:
        """Marketable fill price and the per-share slippage it implies.

        A market order does not fill at the mid. It crosses some fraction of
        the spread against you -- half, by default. Backtests that assume mid
        fills are the most common way a strategy looks profitable on paper and
        loses money live.
        """
        frac = self.cfg.paper.slippage_pct_of_spread
        if bid is None or ask is None or ask < bid or ask <= 0:
            return mark, 0.0
        half_spread = (ask - bid) / 2.0
        adverse = half_spread * frac * 2.0
        mid = (bid + ask) / 2.0
        price = mid + adverse if side == "buy" else mid - adverse
        return max(price, 0.0), abs(price - mid)

    def _commission(self, holding: Holding, qty: float) -> float:
        p = self.cfg.paper
        if holding.is_option:
            return abs(qty) * p.commission_per_contract
        return abs(qty) * p.commission_per_share

    # -------------------------------------------------------------- orders

    def affordability(self, holding: Holding, qty: float, mark: float) -> dict[str, Any]:
        """What buying ``qty`` of ``holding`` would actually cost. No side effects."""
        notional = abs(qty) * holding.contract_multiplier * mark
        commission = self._commission(holding, qty)
        total = notional + commission
        return {
            "symbol": holding.occ_symbol(),
            "quoted_premium": mark,
            "contract_multiplier": holding.contract_multiplier,
            "qty": qty,
            "notional": round(notional, 2),
            "commission": round(commission, 2),
            "total_cost": round(total, 2),
            "cash_available": round(self.cash, 2),
            "affordable": total <= self.cash,
            "shortfall": round(max(0.0, total - self.cash), 2),
        }

    def buy(
        self,
        holding: Holding,
        qty: float,
        mark: float,
        bid: float | None = None,
        ask: float | None = None,
        reason: str = "",
        asof: date | None = None,
    ) -> FillResult:
        """Buy to open. Rejects rather than going negative on cash."""
        if qty <= 0:
            raise OrderRejected(f"buy qty must be > 0, got {qty}")

        price, slip_per_share = self._fill_price("buy", bid, ask, mark)
        shares = abs(qty) * holding.contract_multiplier
        notional = shares * price
        commission = self._commission(holding, qty)
        total = notional + commission

        if total > self.cash:
            afford = self.affordability(holding, qty, price)
            raise OrderRejected(
                f"Insufficient cash for {qty:g} x {holding.occ_symbol()}. "
                f"Needs {total:.2f} {self.currency} "
                f"({holding.contract_multiplier} x {price:.2f} = {notional:.2f} premium "
                f"+ {commission:.2f} commission), have {self.cash:.2f}. "
                f"Short by {afford['shortfall']:.2f}."
            )

        self.cash -= total
        self._record(holding, "buy", qty, price, commission, slip_per_share * shares, reason, asof)
        change = self.book.apply_fill(holding, "buy", qty, price, commission, reason, asof)
        self.store.save_cash(self.cash, utcnow().isoformat())
        return FillResult(
            symbol=holding.occ_symbol(),
            side="buy",
            qty=qty,
            price=price,
            commission=commission,
            slippage=slip_per_share * shares,
            cash_delta=-total,
            change=change,
        )

    def sell(
        self,
        holding: Holding,
        qty: float,
        mark: float,
        bid: float | None = None,
        ask: float | None = None,
        reason: str = "",
        asof: date | None = None,
    ) -> FillResult:
        """Sell to close. Proceeds net of commission and slippage."""
        if qty <= 0:
            raise OrderRejected(f"sell qty must be > 0, got {qty}")

        price, slip_per_share = self._fill_price("sell", bid, ask, mark)
        shares = abs(qty) * holding.contract_multiplier
        proceeds = shares * price
        commission = self._commission(holding, qty)
        net = proceeds - commission

        self.cash += net
        self._record(holding, "sell", qty, price, commission, slip_per_share * shares, reason, asof)
        change = self.book.apply_fill(holding, "sell", qty, price, commission, reason, asof)
        self.store.save_cash(self.cash, utcnow().isoformat())
        return FillResult(
            symbol=holding.occ_symbol(),
            side="sell",
            qty=qty,
            price=price,
            commission=commission,
            slippage=slip_per_share * shares,
            cash_delta=net,
            change=change,
        )

    def _record(
        self,
        holding: Holding,
        side: str,
        qty: float,
        price: float,
        commission: float,
        slippage: float,
        reason: str,
        asof: date | None,
    ) -> None:
        self.store.record_fill(
            ts=(asof.isoformat() if asof else utcnow().isoformat()),
            symbol=holding.occ_symbol(),
            ticker=holding.ticker,
            side=side,
            qty=qty,
            price=price,
            commission=commission,
            slippage=slippage,
            reason=reason,
        )
        log.info("paper %s %g %s @ %.4f (comm %.2f)", side, qty, holding.occ_symbol(), price, commission)

    # ------------------------------------------------------------- equity

    def mark_to_market(self, positions_value: float, asof: date) -> dict[str, Any]:
        """Record and return the account's equity for the day."""
        equity = self.cash + positions_value
        self.store.record_equity(asof, self.cash, positions_value, equity)
        return {
            "asof": asof.isoformat(),
            "mode": "paper",
            "cash": round(self.cash, 2),
            "positions_value": round(positions_value, 2),
            "equity": round(equity, 2),
            "realised_pnl_to_date": round(self.book.realised_total(), 2),
            "open_positions": len(self.store.all_positions()),
            "currency": self.currency,
        }
