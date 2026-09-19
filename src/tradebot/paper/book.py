"""The simulated position book.

Turns a stream of paper fills into positions, an average price, and realised
P&L. The arithmetic here is the part that is easy to get quietly wrong, so it
is isolated from the broker's cash handling and tested directly.

Sign convention: ``qty`` is positive long, negative short, for both shares and
contracts. ``avg_price`` is always per share (for an option, the premium), so a
position's cost is ``avg_price * qty * multiplier``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from typing import Any

from ..store import Store
from ..types import AssetKind, Holding, OptionKind, utcnow

log = logging.getLogger(__name__)

__all__ = ["PaperBook", "PositionChange"]


def _sign(x: float) -> int:
    return (x > 0) - (x < 0)


@dataclass
class PositionChange:
    """What one fill did to a position."""

    symbol: str
    qty_before: float
    qty_after: float
    avg_before: float
    avg_after: float
    realised_pnl: float = 0.0
    closed_qty: float = 0.0
    fully_closed: bool = False

    @property
    def opened(self) -> bool:
        return abs(self.qty_before) < 1e-12 and abs(self.qty_after) > 1e-12

    def to_json(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "qty_before": self.qty_before,
            "qty_after": self.qty_after,
            "avg_price_before": round(self.avg_before, 4),
            "avg_price_after": round(self.avg_after, 4),
            "realised_pnl": round(self.realised_pnl, 2),
            "closed_qty": self.closed_qty,
            "fully_closed": self.fully_closed,
            "opened": self.opened,
        }


class PaperBook:
    """Position state for the paper account, persisted in SQLite."""

    def __init__(self, store: Store) -> None:
        self.store = store

    # ---------------------------------------------------------------- read

    def holdings(self) -> list[Holding]:
        """The paper book as ``Holding`` objects, ready for the valuation layer."""
        out: list[Holding] = []
        for row in self.store.all_positions():
            out.append(
                Holding(
                    ticker=row["ticker"],
                    kind=AssetKind(row["kind"]),
                    qty=row["qty"],
                    cost_basis=row["avg_price"],
                    option_kind=OptionKind(row["option_kind"]) if row["option_kind"] else None,
                    strike=row["strike"],
                    expiry=date.fromisoformat(row["expiry"]) if row["expiry"] else None,
                    target=row["target"],
                    stop=row["stop"],
                    multiplier=int(row["multiplier"]),
                )
            )
        return out

    def is_empty(self) -> bool:
        return not self.store.all_positions()

    def realised_total(self) -> float:
        return sum(t["pnl"] for t in self.store.closed_trades())

    # --------------------------------------------------------------- write

    def apply_fill(
        self,
        holding: Holding,
        side: str,
        qty: float,
        price: float,
        costs: float = 0.0,
        reason: str = "",
        asof: date | None = None,
    ) -> PositionChange:
        """Apply one fill and return what it did to the position.

        Handles the three cases that matter: adding to a position (weighted
        average price), reducing one (realise P&L, average unchanged), and
        crossing through zero (close the old side entirely, open the remainder
        at the fill price).
        """
        if qty <= 0:
            raise ValueError(f"fill qty must be > 0, got {qty}")

        symbol = holding.occ_symbol()
        signed = qty if side == "buy" else -qty
        mult = holding.contract_multiplier
        now = (asof.isoformat() if asof else utcnow().isoformat())

        existing = self.store.get_position(symbol)
        if existing is None:
            self._write(holding, signed, price, now, 0.0, entry_costs=costs)
            return PositionChange(symbol, 0.0, signed, 0.0, price)

        old_qty = float(existing["qty"])
        old_avg = float(existing["avg_price"])
        opened_at = existing["opened_at"]
        realised_so_far = float(existing["realised_pnl"])
        entry_costs = float(existing["entry_costs"] or 0.0)
        new_qty = old_qty + signed

        # --- adding to the same side: weighted average --------------------
        if _sign(signed) == _sign(old_qty) or abs(old_qty) < 1e-12:
            new_avg = (old_qty * old_avg + signed * price) / new_qty if abs(new_qty) > 1e-12 else price
            self._write(
                holding, new_qty, new_avg, opened_at, realised_so_far,
                entry_costs=entry_costs + costs,
            )
            return PositionChange(symbol, old_qty, new_qty, old_avg, new_avg)

        # --- reducing or closing ------------------------------------------
        closed = min(abs(signed), abs(old_qty))
        # Entry commission is carried on the position and released pro-rata as
        # it closes, so a round-trip's P&L nets both legs' costs. Without this,
        # realised P&L overstates by exactly the entry commission and stops
        # reconciling against the cash balance.
        entry_share = entry_costs * (closed / abs(old_qty)) if abs(old_qty) > 1e-12 else 0.0
        total_costs = costs + entry_share
        # Long: profit when the exit is above the average. Short: the reverse.
        # The sign of the old position flips it correctly for both.
        pnl = (price - old_avg) * closed * mult * _sign(old_qty) - total_costs

        if abs(new_qty) < 1e-12:
            # Flat: record the round-trip and drop the position row.
            self.store.record_trade(
                symbol=symbol, ticker=holding.ticker, opened_at=opened_at, closed_at=now,
                qty=closed, entry_price=old_avg, exit_price=price, multiplier=mult,
                pnl=pnl, costs=total_costs, reason=reason,
            )
            self.store.delete_position(symbol)
            return PositionChange(symbol, old_qty, 0.0, old_avg, 0.0, pnl, closed, True)

        if _sign(new_qty) == _sign(old_qty):
            # Partial reduction: average price is unchanged on what remains.
            self.store.record_trade(
                symbol=symbol, ticker=holding.ticker, opened_at=opened_at, closed_at=now,
                qty=closed, entry_price=old_avg, exit_price=price, multiplier=mult,
                pnl=pnl, costs=total_costs, reason=reason,
            )
            self._write(
                holding, new_qty, old_avg, opened_at, realised_so_far + pnl,
                entry_costs=entry_costs - entry_share,
            )
            return PositionChange(symbol, old_qty, new_qty, old_avg, old_avg, pnl, closed)

        # --- crossing zero: close the old side, open the remainder --------
        self.store.record_trade(
            symbol=symbol, ticker=holding.ticker, opened_at=opened_at, closed_at=now,
            qty=closed, entry_price=old_avg, exit_price=price, multiplier=mult,
            pnl=pnl, costs=total_costs, reason=f"{reason} (reversed)".strip(),
        )
        self._write(holding, new_qty, price, now, 0.0, entry_costs=0.0)
        return PositionChange(symbol, old_qty, new_qty, old_avg, price, pnl, closed, False)

    def _write(
        self,
        holding: Holding,
        qty: float,
        avg: float,
        opened_at: str,
        realised: float,
        entry_costs: float = 0.0,
    ) -> None:
        self.store.upsert_position(
            symbol=holding.occ_symbol(),
            ticker=holding.ticker,
            kind=holding.kind.value,
            option_kind=holding.option_kind.value if holding.option_kind else None,
            strike=holding.strike,
            expiry=holding.expiry.isoformat() if holding.expiry else None,
            qty=qty,
            avg_price=avg,
            multiplier=holding.contract_multiplier,
            target=holding.target,
            stop=holding.stop,
            opened_at=opened_at,
            realised_pnl=realised,
            entry_costs=entry_costs,
        )
