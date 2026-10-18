"""Alpaca Trading API adapter (paper and live), US stocks and ETFs.

What this adapter assumes about the API (https://docs.alpaca.markets/reference):
  * base URLs https://paper-api.alpaca.markets/v2/ (paper) and https://api.alpaca.markets/v2/
    (live). Paper and live accounts have SEPARATE keys
  * auth: headers APCA-API-KEY-ID and APCA-API-SECRET-KEY
  * market orders: POST orders {"symbol", "qty", "side", "type": "market",
    "time_in_force": "day"}; fractional quantities are allowed for day market orders
    (fractional SHORT sales are not, so shorts are rounded down to whole shares)
  * the account is in USD and US stock trades are commission-free; regulatory fees on
    sells are tiny and covered by the cost model's slippage
  * rate limit about 200 requests per minute per account; 429 responses back off
  * 403 means a bad key OR a refused order (e.g. insufficient buying power), so the
    message is passed through rather than guessed at
Stops and targets stay SOFTWARE stops checked by the engine, exactly as with Trading 212,
so behaviour is the same across brokers.
"""

from __future__ import annotations

import logging
import math
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime

import requests

from simplebot.brokers.base import Broker, BrokerError, Order, Position
from simplebot.brokers.ratelimit import EndpointLimiter

log = logging.getLogger(__name__)

PAPER_URL = "https://paper-api.alpaca.markets/v2/"
LIVE_URL = "https://api.alpaca.markets/v2/"

DEFAULT_LIMITS: dict[str, tuple[int, float]] = {"": (180, 60.0)}  # one bucket for the account

STATUS_MAP = {
    "filled": "filled",
    "partially_filled": "partially_filled",
    "canceled": "cancelled",
    "expired": "cancelled",
    "done_for_day": "cancelled",
    "replaced": "cancelled",
    "pending_cancel": "cancelled",
    "rejected": "rejected",
    "suspended": "rejected",
}


class AlpacaBroker(Broker):
    name = "alpaca"
    supports_fractional = True
    qty_decimals = 4

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        live: bool = False,
        session: requests.Session | None = None,
        limiter: EndpointLimiter | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not api_key or not api_secret:
            raise BrokerError("ALPACA_API_KEY and ALPACA_API_SECRET must both be set in .env")
        self.live = live
        self.base = LIVE_URL if live else PAPER_URL
        self.session = session or requests.Session()
        self.session.headers["APCA-API-KEY-ID"] = api_key
        self.session.headers["APCA-API-SECRET-KEY"] = api_secret
        self.limiter = limiter or EndpointLimiter(DEFAULT_LIMITS)
        self.sleep = sleep

    # plumbing -----------------------------------------------------------------------------
    def _request(self, method: str, path: str, **kwargs):
        for attempt in range(6):
            self.limiter.acquire(path)
            r = self.session.request(method, self.base + path, timeout=30, **kwargs)
            if r.status_code == 429:
                wait = min(float(r.headers.get("Retry-After", 2**attempt)), 60.0)
                log.warning("Alpaca rate limit on %s; backing off %.1fs", path, wait)
                self.sleep(wait)
                continue
            if r.status_code == 401:
                raise BrokerError(
                    f"Alpaca rejected the API key (401) on {path}. Paper and live accounts have"
                    " different keys: check ALPACA_API_KEY/ALPACA_API_SECRET match this mode."
                )
            if r.status_code >= 500:
                self.sleep(min(2**attempt, 30))
                continue
            if r.status_code >= 400:
                raise BrokerError(f"{method} {path} -> {r.status_code}: {_message(r)}")
            return r.json() if r.content else {}
        raise BrokerError(f"{path}: gave up after repeated 429/5xx responses")

    @staticmethod
    def _to_order(js: dict) -> Order:
        filled = float(js.get("filled_qty") or 0.0)
        avg = js.get("filled_avg_price")
        return Order(
            id=str(js.get("id", "")),
            symbol=str(js.get("symbol", "")),
            side=str(js.get("side", "buy")).lower(),
            qty=abs(float(js.get("qty") or filled or 0.0)),
            type=str(js.get("type", "market")).lower(),
            status=STATUS_MAP.get(str(js.get("status", "")).lower(), "new"),
            filled_qty=abs(filled),
            avg_fill_price=float(avg) if avg not in (None, "") else None,
            created_at=datetime.now(UTC),
            note=str(js.get("status", "")),
        )

    # account ------------------------------------------------------------------------------
    def account(self) -> dict:
        return self._request("GET", "account")

    def get_cash(self) -> float:
        """Cash the bot may spend. Uses cash (not margin buying power): the bot never borrows."""
        js = self.account()
        cash = float(js.get("cash") or 0.0)
        bp = js.get("non_marginable_buying_power")
        return max(0.0, min(cash, float(bp)) if bp not in (None, "") else cash)

    def get_equity(self) -> float:
        return float(self.account().get("equity") or 0.0)

    def get_positions(self) -> list[Position]:
        out = []
        for p in self._request("GET", "positions") or []:
            qty = float(p.get("qty") or 0.0)
            if not qty:
                continue
            side = str(p.get("side") or ("long" if qty > 0 else "short")).lower()
            out.append(
                Position(
                    symbol=str(p["symbol"]),
                    qty=abs(qty),
                    side="short" if side == "short" else "long",
                    avg_price=float(p.get("avg_entry_price") or 0.0),
                )
            )
        return out

    # orders -------------------------------------------------------------------------------
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
        qty = self.round_qty(qty)
        if side == "sell" and qty != math.floor(qty) and not self._holds_long(symbol, qty):
            qty = float(math.floor(qty))  # opening or adding to a short: whole shares only
        if qty <= 0:
            return Order(id="", symbol=symbol, side=side, qty=0, status="rejected", note="qty 0")
        body = {
            "symbol": symbol.upper(),
            "qty": f"{qty:.{self.qty_decimals}f}".rstrip("0").rstrip("."),
            "side": side,
            "type": "market",
            "time_in_force": "day",
            "client_order_id": f"simplebot-{uuid.uuid4().hex[:20]}",
        }
        return self._to_order(self._request("POST", "orders", json=body))

    def _holds_long(self, symbol: str, qty: float) -> bool:
        for p in self.get_positions():
            if p.symbol.upper() == symbol.upper() and p.side == "long" and p.qty + 1e-9 >= qty:
                return True
        return False

    def get_order(self, order_id: str, symbol: str = "") -> Order:
        return self._to_order(self._request("GET", f"orders/{order_id}"))

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
        self._request("DELETE", f"orders/{order_id}")

    def open_orders(self) -> list[Order]:
        rows = self._request("GET", "orders", params={"status": "open", "limit": 500}) or []
        return [self._to_order(js) for js in rows]

    def clock(self) -> dict:
        """Alpaca's market clock: {"is_open", "next_open", "next_close", ...}."""
        return self._request("GET", "clock")


def _message(r) -> str:
    try:
        js = r.json()
        if isinstance(js, dict) and js.get("message"):
            return str(js["message"])
    except Exception:
        pass
    return str(getattr(r, "text", ""))[:300]
