"""Regular trading sessions for the exchanges we support. Everything is timezone-aware."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class Market:
    name: str
    tz: str
    open: time
    close: time

    @property
    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.tz)

    @property
    def session_minutes(self) -> int:
        o = self.open.hour * 60 + self.open.minute
        c = self.close.hour * 60 + self.close.minute
        return c - o


US = Market("US", "America/New_York", time(9, 30), time(16, 0))
UK = Market("UK", "Europe/London", time(8, 0), time(16, 30))
MARKETS = {"US": US, "UK": UK}


def market_for(symbol: str) -> Market:
    """Yahoo-style symbols: '.L' means London Stock Exchange; everything else is treated as US."""
    return UK if symbol.upper().endswith(".L") else US


def market_from_tz(tz) -> Market:
    key = getattr(tz, "key", None) or str(tz)
    for m in MARKETS.values():
        if m.tz == key:
            return m
    return US


def currency_for(symbol: str) -> str:
    return "GBP" if symbol.upper().endswith(".L") else "USD"


def session_open(market: Market, day: date) -> datetime:
    return datetime.combine(day, market.open, tzinfo=market.zone)


def session_close(market: Market, day: date) -> datetime:
    return datetime.combine(day, market.close, tzinfo=market.zone)


def session_date(market: Market, ts: datetime) -> date:
    return ts.astimezone(market.zone).date()


def is_in_session(market: Market, ts: datetime) -> bool:
    local = ts.astimezone(market.zone)
    if local.weekday() >= 5:
        return False
    return session_open(market, local.date()) <= local < session_close(market, local.date())


def minutes_to_close(market: Market, ts: datetime) -> float:
    local = ts.astimezone(market.zone)
    return (session_close(market, local.date()) - local).total_seconds() / 60.0


def minutes_since_open(market: Market, ts: datetime) -> float:
    local = ts.astimezone(market.zone)
    return (local - session_open(market, local.date())).total_seconds() / 60.0


def next_session_open(market: Market, ts: datetime) -> datetime:
    local = ts.astimezone(market.zone)
    day = local.date()
    for _ in range(8):
        if day.weekday() < 5 and session_open(market, day) > local:
            return session_open(market, day)
        day = day + timedelta(days=1)
    raise RuntimeError("no session found in the next week")
