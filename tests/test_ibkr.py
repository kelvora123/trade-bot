import types

import pytest

ib_async = pytest.importorskip("ib_async")

from simplebot.brokers.ibkr import IBKRBroker  # noqa: E402


class FakeTrade:
    def __init__(self, order, contract):
        self.order, self.contract, self.fills = order, contract, []
        self.orderStatus = types.SimpleNamespace(
            status="Filled", filled=order.totalQuantity, avgFillPrice=101.0
        )


class FakeIB:
    def __init__(self):
        self.placed, self._id = [], 10
        self.client = types.SimpleNamespace(getReqId=self._next)

    def _next(self):
        self._id += 1
        return self._id

    def qualifyContracts(self, c):
        return [c]

    def accountSummary(self):
        return [
            types.SimpleNamespace(tag="TotalCashValue", value="500.5", currency="BASE"),
            types.SimpleNamespace(tag="NetLiquidation", value="600", currency="BASE"),
        ]

    def positions(self):
        return [
            types.SimpleNamespace(
                contract=ib_async.Stock("AAPL", "SMART", "USD"), position=-3, avgCost=150.0
            )
        ]

    def placeOrder(self, contract, order):
        if not getattr(order, "orderId", 0):
            order.orderId = self._next()
        t = FakeTrade(order, contract)
        self.placed.append(t)
        return t

    def trades(self):
        return self.placed

    def openTrades(self):
        return []

    def sleep(self, s):
        pass

    def disconnect(self):
        pass


def test_account_and_positions():
    b = IBKRBroker(ib=FakeIB())
    assert b.get_cash() == 500.5 and b.get_equity() == 600.0
    p = b.get_positions()[0]
    assert p.symbol == "AAPL" and p.side == "short" and p.qty == 3


def test_market_and_bracket_orders():
    ib = FakeIB()
    b = IBKRBroker(ib=ib)
    o = b.place_order("AAPL", "buy", 2.7)
    assert o.is_filled and o.qty == 2 and o.avg_fill_price == 101.0
    o2 = b.place_bracket("AAPL", "buy", 3, stop=95.0, target=110.0)
    assert o2.is_filled
    parent, tp, sl = ib.placed[1:]
    assert parent.order.transmit is False and tp.order.parentId == parent.order.orderId
    assert sl.order.parentId == parent.order.orderId and sl.order.transmit is True
    assert isinstance(sl.order, ib_async.StopOrder) and sl.order.auxPrice == 95.0
