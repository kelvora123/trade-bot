import pytest

from simplebot.brokers.alpaca import LIVE_URL, PAPER_URL, AlpacaBroker
from simplebot.brokers.base import BrokerError
from simplebot.brokers.ratelimit import EndpointLimiter


class FakeResponse:
    def __init__(self, status, payload=None, headers=None):
        self.status_code, self._payload, self.headers = status, payload, headers or {}
        self.content = b"x" if payload is not None else b""
        self.text = str(payload)

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, script):
        self.script, self.calls, self.headers = list(script), [], {}

    def request(self, method, url, timeout=None, **kw):
        self.calls.append((method, url, kw))
        return self.script.pop(0)


def broker(script, **kw):
    s = FakeSession(script)
    b = AlpacaBroker(
        "key",
        "secret",
        session=s,
        limiter=EndpointLimiter({}, clock=lambda: 0.0, sleep=lambda s: None),
        sleep=lambda s: None,
        **kw,
    )
    return b, s


ACCOUNT = {"cash": "1000.50", "equity": "1012.25", "non_marginable_buying_power": "900.00"}


def test_auth_headers_and_urls():
    b, s = broker([])
    assert s.headers["APCA-API-KEY-ID"] == "key" and s.headers["APCA-API-SECRET-KEY"] == "secret"
    assert b.base == PAPER_URL and not b.live
    b2, _ = broker([], live=True)
    assert b2.base == LIVE_URL
    with pytest.raises(BrokerError):
        AlpacaBroker("key", "")


def test_account_cash_never_uses_margin():
    b, s = broker([FakeResponse(200, ACCOUNT), FakeResponse(200, ACCOUNT)])
    assert b.get_cash() == 900.0  # the smaller of cash and non-marginable buying power
    assert b.get_equity() == 1012.25
    assert s.calls[0][1] == PAPER_URL + "account"


def test_positions_long_and_short():
    b, _ = broker(
        [
            FakeResponse(
                200,
                [
                    {"symbol": "SPY", "qty": "2.5", "side": "long", "avg_entry_price": "500"},
                    {"symbol": "QQQ", "qty": "-3", "side": "short", "avg_entry_price": "400"},
                    {"symbol": "IWM", "qty": "0", "side": "long", "avg_entry_price": "200"},
                ],
            )
        ]
    )
    pos = b.get_positions()
    assert [(p.symbol, p.side, p.qty, p.avg_price) for p in pos] == [
        ("SPY", "long", 2.5, 500.0),
        ("QQQ", "short", 3.0, 400.0),
    ]


def test_market_order_and_fill():
    placed = {"id": "o1", "symbol": "SPY", "side": "buy", "qty": "1.2345", "status": "accepted"}
    filled = {**placed, "status": "filled", "filled_qty": "1.2345", "filled_avg_price": "501.5"}
    b, s = broker([FakeResponse(200, placed), FakeResponse(200, filled)])
    o = b.place_order("spy", "buy", 1.23456789)
    method, url, kw = s.calls[0]
    assert (method, url) == ("POST", PAPER_URL + "orders")
    body = kw["json"]
    assert body["symbol"] == "SPY" and body["qty"] == "1.2345" and body["side"] == "buy"
    assert body["type"] == "market" and body["time_in_force"] == "day"
    assert body["client_order_id"].startswith("simplebot-")
    assert o.status == "new" and o.id == "o1"
    done = b.await_fill(o)
    assert done.is_filled and done.avg_fill_price == 501.5 and done.filled_qty == 1.2345


def test_closing_a_fractional_long_keeps_the_fraction():
    pos = [{"symbol": "SPY", "qty": "1.5", "side": "long", "avg_entry_price": "500"}]
    placed = {"id": "o2", "symbol": "SPY", "side": "sell", "qty": "1.5", "status": "new"}
    b, s = broker([FakeResponse(200, pos), FakeResponse(200, placed)])
    b.place_order("SPY", "sell", 1.5)
    assert s.calls[1][2]["json"]["qty"] == "1.5"


def test_fractional_short_rounds_down_to_whole_shares():
    placed = {"id": "o3", "symbol": "SPY", "side": "sell", "qty": "2", "status": "new"}
    b, s = broker([FakeResponse(200, []), FakeResponse(200, placed)])
    b.place_order("SPY", "sell", 2.7)
    assert s.calls[1][2]["json"]["qty"] == "2"
    b2, _ = broker([FakeResponse(200, [])])
    assert b2.place_order("SPY", "sell", 0.6).status == "rejected"  # under one share


def test_statuses_map_to_the_engine_vocabulary():
    for raw, mapped in [
        ("filled", "filled"),
        ("partially_filled", "partially_filled"),
        ("canceled", "cancelled"),
        ("expired", "cancelled"),
        ("rejected", "rejected"),
        ("pending_new", "new"),
    ]:
        assert AlpacaBroker._to_order({"id": "x", "status": raw}).status == mapped


def test_errors_and_backoff():
    b, s = broker([FakeResponse(429, {}, {"Retry-After": "1"}), FakeResponse(200, ACCOUNT)])
    assert b.get_equity() == 1012.25 and len(s.calls) == 2
    b, _ = broker([FakeResponse(401, {"message": "unauthorized"})])
    with pytest.raises(BrokerError, match="different keys"):
        b.get_equity()
    b, _ = broker([FakeResponse(403, {"message": "insufficient buying power"})])
    with pytest.raises(BrokerError, match="insufficient buying power"):
        b.place_order("SPY", "buy", 1)


def test_open_orders_cancel_and_flatten():
    pos = [{"symbol": "SPY", "qty": "2", "side": "long", "avg_entry_price": "500"}]
    sell = {
        "id": "o9",
        "symbol": "SPY",
        "side": "sell",
        "qty": "2",
        "status": "filled",
        "filled_qty": "2",
        "filled_avg_price": "499",
    }
    script = [
        FakeResponse(200, [{"id": "o5", "symbol": "QQQ", "side": "buy", "qty": "1"}]),
        FakeResponse(204, None),  # cancel
        FakeResponse(200, pos),  # positions for flatten
        FakeResponse(200, sell),  # the closing order (whole shares: no position lookup)
    ]
    b, s = broker(script)
    orders = b.flatten_all()
    assert s.calls[0][2]["params"] == {"status": "open", "limit": 500}
    assert s.calls[1][:2] == ("DELETE", PAPER_URL + "orders/o5")
    assert orders[0].is_filled and orders[0].symbol == "SPY"


def test_make_broker_and_account_command(cfg, monkeypatch, capsys):
    from simplebot import main as m

    cfg.broker = "alpaca"
    monkeypatch.setenv("ALPACA_API_KEY", "k")
    monkeypatch.setenv("ALPACA_API_SECRET", "s")
    b = m.make_broker(cfg, "paper")
    assert isinstance(b, AlpacaBroker) and b.base == PAPER_URL
    assert m.make_broker(cfg, "live").base == LIVE_URL

    class Fake:
        live = False

        def get_equity(self):
            return 1000.0

        def get_cash(self):
            return 950.0

        def get_positions(self):
            return []

        def open_orders(self):
            return []

    monkeypatch.setattr(m, "make_broker", lambda c, mode: Fake())

    class Args:
        mode = None

    assert m.cmd_account(cfg, Args()) == 0
    out = capsys.readouterr().out
    assert "connected to alpaca (paper / practice)" in out and "1,000.00" in out
    cfg.broker = "paper"
    assert m.cmd_account(cfg, Args()) == 1
