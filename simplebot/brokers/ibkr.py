"""Interactive Brokers adapter via ib_async (optional dependency).

Needs TWS or IB Gateway running with API access enabled. Entries can use native bracket
orders (market parent + take-profit limit + stop) so the exchange holds the stop even if this
bot dies. Not exercised against a live gateway in CI: tests use a fake IB object.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime

from simplebot.brokers.base import Broker, BrokerError, Order, Position

try:  # pragma: no cover - import guard
    import ib_async
except ImportError:  # pragma: no cover
    ib_async = None


class IBKRBroker(Broker):
    name = "ibkr"
    supports_fractional = False
    qty_decimals = 0
    supports_native_brackets = True

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 4002,
        client_id: int = 7,
        ib=None,
        fill_timeout: float = 30.0,
    ) -> None:
        if ib is None:
            if ib_async is None:
                raise BrokerError("pip install ib_async to use broker: ibkr")
            ib = ib_async.IB()
            ib.connect(host, port, clientId=client_id, timeout=20)
        self.ib = ib
        self.fill_timeout = fill_timeout
        self._contracts: dict[str, object] = {}

    # contracts ------------------------------------------------------------------------
    def contract(self, symbol: str):
        if symbol not in self._contracts:
            if ib_async is None:
                raise BrokerError("ib_async is not installed")
            if symbol.upper().endswith(".L"):
                c = ib_async.Stock(symbol[:-2], "LSE", "GBP")
            else:
                c = ib_async.Stock(symbol, "SMART", "USD")
            self.ib.qualifyContracts(c)
            self._contracts[symbol] = c
        return self._contracts[symbol]

    @staticmethod
    def _symbol_of(contract) -> str:
        exch = getattr(contract, "exchange", "") or getattr(contract, "primaryExchange", "")
        return f"{contract.symbol}.L" if exch == "LSE" else str(contract.symbol)

    # account --------------------------------------------------------------------------
    def _summary_value(self, tag: str) -> float:
        for row in self.ib.accountSummary():
            if row.tag == tag and getattr(row, "currency", "BASE") in ("BASE", ""):
                return float(row.value)
        for row in self.ib.accountSummary():
            if row.tag == tag:
                return float(row.value)
        raise BrokerError(f"IBKR account summary has no {tag}")

    def get_cash(self) -> float:
        return self._summary_value("TotalCashValue")

    def get_equity(self) -> float:
        return self._summary_value("NetLiquidation")

    def get_positions(self) -> list[Position]:
        out = []
        for p in self.ib.positions():
            qty = float(p.position)
            if not qty:
                continue
            out.append(
                Position(
                    symbol=self._symbol_of(p.contract),
                    qty=abs(qty),
                    side="long" if qty > 0 else "short",
                    avg_price=float(p.avgCost),
                )
            )
        return out

    # orders ---------------------------------------------------------------------------
    def _trade_to_order(self, trade, symbol: str) -> Order:
        st = trade.orderStatus
        status = {
            "Filled": "filled",
            "Cancelled": "cancelled",
            "ApiCancelled": "cancelled",
            "Inactive": "rejected",
        }.get(st.status, "partially_filled" if float(st.filled or 0) > 0 else "new")
        fees = 0.0
        for f in getattr(trade, "fills", []) or []:
            rep = getattr(f, "commissionReport", None)
            if rep is not None and getattr(rep, "commission", None):
                fees += float(rep.commission)
        return Order(
            id=str(trade.order.orderId),
            symbol=symbol,
            side="buy" if trade.order.action == "BUY" else "sell",
            qty=float(trade.order.totalQuantity),
            status=status,
            filled_qty=float(st.filled or 0.0),
            avg_fill_price=float(st.avgFillPrice) if st.avgFillPrice else None,
            fees=fees,
            created_at=datetime.now(UTC),
        )

    def place_order(
        self,
        symbol: str,
        side: str,
        qty: float,
        order_type: str = "market",
        limit_price: float | None = None,
    ) -> Order:
        if ib_async is None:
            raise BrokerError("ib_async is not installed")
        action = "BUY" if side == "buy" else "SELL"
        qty = self.round_qty(qty)
        if qty <= 0:
            return Order(id="", symbol=symbol, side=side, qty=0, status="rejected", note="qty 0")
        if order_type == "limit" and limit_price is not None:
            order = ib_async.LimitOrder(action, qty, limit_price)
        else:
            order = ib_async.MarketOrder(action, qty)
        trade = self.ib.placeOrder(self.contract(symbol), order)
        return self.await_fill(self._trade_to_order(trade, symbol))

    def place_bracket(
        self, symbol: str, side: str, qty: float, stop: float, target: float | None
    ) -> Order:
        """Market entry with a child stop (and optional take-profit) held at IBKR."""
        if ib_async is None:
            raise BrokerError("ib_async is not installed")
        action, opposite = ("BUY", "SELL") if side == "buy" else ("SELL", "BUY")
        qty = self.round_qty(qty)
        parent = ib_async.MarketOrder(action, qty)
        parent.orderId = self.ib.client.getReqId()
        parent.transmit = False
        children = []
        if target is not None:
            tp = ib_async.LimitOrder(opposite, qty, target)
            tp.parentId = parent.orderId
            tp.transmit = False
            children.append(tp)
        sl = ib_async.StopOrder(opposite, qty, stop)
        sl.parentId = parent.orderId
        sl.transmit = True
        children.append(sl)
        contract = self.contract(symbol)
        trade = self.ib.placeOrder(contract, parent)
        for child in children:
            self.ib.placeOrder(contract, child)
        return self.await_fill(self._trade_to_order(trade, symbol))

    def await_fill(self, order: Order, timeout: float | None = None) -> Order:
        timeout = self.fill_timeout if timeout is None else timeout
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            trade = self._find_trade(order.id)
            if trade is None:
                return order
            current = self._trade_to_order(trade, order.symbol)
            if current.is_done:
                return current
            self.ib.sleep(0.5)
        return order

    def _find_trade(self, order_id: str):
        for t in self.ib.trades():
            if str(t.order.orderId) == str(order_id):
                return t
        return None

    def cancel(self, order_id: str) -> None:
        trade = self._find_trade(order_id)
        if trade is not None:
            self.ib.cancelOrder(trade.order)

    def open_orders(self) -> list[Order]:
        return [self._trade_to_order(t, self._symbol_of(t.contract)) for t in self.ib.openTrades()]

    def close(self) -> None:
        try:
            self.ib.disconnect()
        except Exception:  # pragma: no cover
            pass
