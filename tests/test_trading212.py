import pytest

from simplebot.brokers.base import BrokerError
from simplebot.brokers.ratelimit import EndpointLimiter, TokenBucket
from simplebot.brokers.trading212 import DEMO_URL, LIVE_URL, Trading212Broker


class FakeResponse:
    def __init__(self, status, payload=None, headers=None):
        self.status_code, self._payload, self.headers = status, payload, headers or {}
        self.content = b"x" if payload is not None else b""
        self.text = str(payload)

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, script):
        self.script, self.calls, self.headers, self.auth = list(script), [], {}, None

    def request(self, method, url, timeout=None, **kw):
        self.calls.append((method, url, kw))
        return self.script.pop(0)


def broker(script, limiter=None, **kw):
    s = FakeSession(script)
    b = Trading212Broker(
        "key",
        "secret",
        session=s,
        limiter=limiter or EndpointLimiter({}, clock=lambda: 0.0, sleep=lambda s: None),
        sleep=lambda s: None,
        **kw,
    )
    return b, s


def test_auth_and_urls():
    b, s = broker([])
    assert s.auth == ("key", "secret") and b.base == DEMO_URL
    b2, _ = broker([], live=True)
    assert b2.base == LIVE_URL
    with pytest.raises(BrokerError):
        Trading212Broker("", "x")


def test_cash_positions_and_orders():
    script = [
        FakeResponse(200, {"free": 123.4, "total": 150.0}),
        FakeResponse(200, [{"ticker": "AAPL_US_EQ", "quantity": 2.5, "averagePrice": 190.0}]),
        FakeResponse(200, {"id": 77, "status": "NEW", "quantity": -2.5, "type": "MARKET"}),
        FakeResponse(
            200,
            {
                "id": 77,
                "status": "FILLED",
                "quantity": -2.5,
                "filledQuantity": -2.5,
                "filledValue": -500.0,
            },
        ),
    ]
    b, s = broker(script)
    assert b.get_cash() == 123.4
    pos = b.get_positions()
    assert pos[0].symbol == "AAPL" and pos[0].qty == 2.5 and pos[0].side == "long"
    o = b.place_order("AAPL", "sell", 2.5)
    assert s.calls[2][2]["json"] == {"ticker": "AAPL_US_EQ", "quantity": -2.5}
    assert o.status == "new" and o.side == "sell"
    done = b.await_fill(o, timeout=5)
    assert done.is_filled and done.avg_fill_price == 200.0 and done.filled_qty == 2.5


def test_symbol_map_required_for_lse():
    b, _ = broker([])
    with pytest.raises(BrokerError):
        b.ticker("VUSA.L")
    b2, _ = broker([], symbol_map={"VUSA.L": "VUSAl_EQ"})
    assert b2.ticker("vusa.l") == "VUSAl_EQ" and b2.symbol_for("VUSAl_EQ") == "VUSA.L"


def test_429_backoff_then_success():
    waits = []
    b, s = broker(
        [
            FakeResponse(429, headers={"Retry-After": "3"}),
            FakeResponse(200, {"free": 1, "total": 1}),
        ]
    )
    b.sleep = waits.append
    assert b.get_cash() == 1 and waits == [3.0] and len(s.calls) == 2


def test_auth_error_raises():
    b, _ = broker([FakeResponse(401, {"message": "bad"})])
    with pytest.raises(BrokerError):
        b.get_cash()


def test_token_bucket_waits():
    clock = {"t": 0.0}
    slept = []
    tb = TokenBucket(2, 10.0, clock=lambda: clock["t"], sleep=slept.append)
    tb.acquire()
    tb.acquire()
    tb.acquire()  # third call must wait for a refill (rate = 0.2/s -> 5s)
    assert slept and abs(slept[0] - 5.0) < 1e-9
    lim = EndpointLimiter({"equity/orders": (1, 5.0), "equity/orders/market": (50, 60.0)})
    assert lim.key_for("equity/orders/market") == "equity/orders/market"
    assert lim.key_for("equity/orders/123") == "equity/orders"
