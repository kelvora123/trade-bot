"""Trading 212 Public API adapter (demo and live).

What this adapter assumes about the API (verify against docs/RESEARCH.md before going live):
  * base URLs https://demo.trading212.com/api/v0/ and https://live.trading212.com/api/v0/
  * auth: HTTP Basic with the API key as username and the API secret as password (the
    current scheme). A bare `Authorization: <key>` header is kept as a fallback for old keys
  * live accounts accept MARKET orders only through the API; limit/stop endpoints work in demo
  * market orders are POST equity/orders/market with {"ticker", "quantity"}; a negative
    quantity sells
  * no price data: the bot fetches bars from the data provider, so stops and targets are
    SOFTWARE stops evaluated on polled prices (see README: gap risk)
Every call goes through a token-bucket limiter keyed by endpoint, and 429s back off.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime

import requests

from simplebot.brokers.base import Broker, BrokerError, Order, Position
from simplebot.brokers.ratelimit import EndpointLimiter

log = logging.getLogger(__name__)

DEMO_URL = "https://demo.trading212.com/api/v0/"
LIVE_URL = "https://live.trading212.com/api/v0/"

# (calls, per_seconds), from the published limits where known (docs/RESEARCH.md), otherwise
# conservative. Market orders: 50/min. Limit/stop: 1 per 2s. Positions: 1/s. Account: 1 per 5s.
DEFAULT_LIMITS: dict[str, tuple[int, float]] = {
    "equity/account/cash": (1, 5.0),
    "equity/account/info": (1, 30.0),
    "equity/portfolio": (1, 1.0),
    "equity/portfolio/": (1, 1.0),
    "equity/orders": (1, 5.0),
    "equity/orders/": (1, 1.0),
    "equity/orders/market": (50, 60.0),
    "equity/orders/limit": (1, 2.0),
    "equity/orders/stop": (1, 2.0),
    "equity/metadata/instruments": (1, 50.0),
    "equity/metadata/exchanges": (1, 30.0),
    "equity/history/orders": (6, 60.0),
}

STATUS_MAP = {
    "FILLED": "filled",
    "PARTIALLY_FILLED": "partially_filled",
    "CANCELLED": "cancelled",
    "CANCELLING": "cancelled",
    "REJECTED": "rejected",
    "REPLACED": "cancelled",
}


class Trading212Broker(Broker):
    name = "trading212"
    supports_fractional = True
    qty_decimals = 2

    def __init__(
        self,
        api_key: str,
        api_secret: str | None = None,
        live: bool = False,
        symbol_map: dict[str, str] | None = None,
        session: requests.Session | None = None,
        limiter: EndpointLimiter | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not api_key:
            raise BrokerError("T212_API_KEY is not set in .env")
        self.live = live
        self.base = LIVE_URL if live else DEMO_URL
        self.session = session or requests.Session()
        if api_secret:
            self.session.auth = (api_key, api_secret)
        else:
            log.warning("T212_API_SECRET is not set; sending the bare key (old auth scheme)")
            self.session.headers["Authorization"] = api_key
        self.symbol_map = {k.upper(): v for k, v in (symbol_map or {}).items()}
        self.limiter = limiter or EndpointLimiter(DEFAULT_LIMITS)
        self.sleep = sleep
        self._instruments: list[dict] | None = None
        self._min_qty: dict[str, float] = {}

    # plumbing -----------------------------------------------------------------------------
    def _request(self, method: str, path: str, **kwargs):
        for attempt in range(6):
            self.limiter.acquire(path)
            r = self.session.request(method, self.base + path, timeout=30, **kwargs)
            if r.status_code == 429:
                wait = min(float(r.headers.get("Retry-After", 2**attempt)), 60.0)
                log.warning("Trading 212 rate limit on %s; backing off %.1fs", path, wait)
                self.sleep(wait)
                continue
            if r.status_code in (401, 403):
                raise BrokerError(f"Trading 212 rejected the API key ({r.status_code}) on {path}")
            if r.status_code >= 500:
                self.sleep(min(2**attempt, 30))
                continue
            if r.status_code >= 400:
                raise BrokerError(f"{method} {path} -> {r.status_code}: {r.text[:300]}")
            return r.json() if r.content else {}
        raise BrokerError(f"{path}: gave up after repeated 429/5xx responses")

    def ticker(self, symbol: str) -> str:
        symbol = symbol.upper()
        if symbol in self.symbol_map:
            return self.symbol_map[symbol]
        if symbol.endswith(".L"):
            raise BrokerError(
                f"map {symbol} to its Trading 212 ticker in config.yaml symbol_map "
                "(run `python -m simplebot instruments --search <name>` to find it)"
            )
        return f"{symbol}_US_EQ"

    def symbol_for(self, ticker: str) -> str:
        for symbol, t in self.symbol_map.items():
            if t == ticker:
                return symbol
        return ticker[: -len("_US_EQ")] if ticker.endswith("_US_EQ") else ticker

    @staticmethod
    def _to_order(js: dict, symbol: str) -> Order:
        filled = float(js.get("filledQuantity") or 0.0)
        value = js.get("filledValue")
        avg = (float(value) / filled) if (value is not None and filled) else None
        qty = abs(float(js.get("quantity") or filled or 0.0))
        side = "sell" if float(js.get("quantity") or 0.0) < 0 else "buy"
        return Order(
            id=str(js.get("id")),
            symbol=symbol,
            side=side,
            qty=qty,
            type=str(js.get("type", "MARKET")).lower(),
            status=STATUS_MAP.get(str(js.get("status", "")).upper(), "new"),
            filled_qty=abs(filled),
            avg_fill_price=avg,
            created_at=datetime.now(UTC),
            note=str(js.get("status", "")),
        )

    # account ------------------------------------------------------------------------------
    def get_cash(self) -> float:
        return float(self._request("GET", "equity/account/cash")["free"])

    def get_equity(self) -> float:
        return float(self._request("GET", "equity/account/cash")["total"])

    def get_positions(self) -> list[Position]:
        out = []
        for p in self._request("GET", "equity/portfolio") or []:
            qty = float(p.get("quantity") or 0.0)
            if not qty:
                continue
            out.append(
                Position(
                    symbol=self.symbol_for(str(p["ticker"])),
                    qty=abs(qty),
                    side="long" if qty > 0 else "short",
                    avg_price=float(p.get("averagePrice") or 0.0),
                )
            )
        return out

    # orders -------------------------------------------------------------------------------
    def round_qty(self, qty: float) -> float:
        return super().round_qty(qty)

    def place_order(
        self,
        symbol: str,
        side: str,
        qty: float,
        order_type: str = "market",
        limit_price: float | None = None,
    ) -> Order:
        if order_type != "market":
            raise BrokerError("this adapter sends market orders only; stops are software stops")
        t = self.ticker(symbol)
        qty = self.round_qty(qty)
        if qty <= 0:
            return Order(id="", symbol=symbol, side=side, qty=0, status="rejected", note="qty 0")
        body = {"ticker": t, "quantity": qty if side == "buy" else -qty}
        js = self._request("POST", "equity/orders/market", json=body)
        return self._to_order(js, symbol)

    def get_order(self, order_id: str, symbol: str = "") -> Order:
        js = self._request("GET", f"equity/orders/{order_id}")
        return self._to_order(js, symbol or self.symbol_for(str(js.get("ticker", ""))))

    def await_fill(self, order: Order, timeout: float = 30.0) -> Order:
        if order.is_done or not order.id:
            return order
        deadline = time.monotonic() + timeout
        current = order
        while time.monotonic() < deadline:
            current = self.get_order(order.id, order.symbol)
            if current.is_done:
                return current
            self.sleep(1.0)
        return current

    def cancel(self, order_id: str) -> None:
        self._request("DELETE", f"equity/orders/{order_id}")

    def open_orders(self) -> list[Order]:
        return [
            self._to_order(js, self.symbol_for(str(js.get("ticker", ""))))
            for js in (self._request("GET", "equity/orders") or [])
        ]

    # metadata -----------------------------------------------------------------------------
    def instruments(self) -> list[dict]:
        if self._instruments is None:
            self._instruments = list(self._request("GET", "equity/metadata/instruments") or [])
            for inst in self._instruments:
                try:
                    self._min_qty[str(inst["ticker"])] = float(inst.get("minTradeQuantity") or 0)
                except (KeyError, TypeError, ValueError):
                    continue
        return self._instruments

    def find_instruments(self, query: str) -> list[dict]:
        q = query.lower()
        return [
            i
            for i in self.instruments()
            if q in str(i.get("ticker", "")).lower()
            or q in str(i.get("name", "")).lower()
            or q in str(i.get("shortName", "")).lower()
            or q in str(i.get("isin", "")).lower()
        ]
