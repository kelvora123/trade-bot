from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tradebot.config import Config  # noqa: E402
from tradebot.store import Store  # noqa: E402
from tradebot.types import AssetKind, ChainRow, Holding, OptionKind  # noqa: E402

ASOF = date(2026, 1, 5)
EXPIRY = date(2026, 3, 20)  # 74 days out


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    c = Config()
    c.root = tmp_path
    return c


@pytest.fixture
def store(tmp_path: Path):
    s = Store(tmp_path / "test.sqlite")
    yield s
    s.close()


@pytest.fixture
def nvda_call() -> Holding:
    return Holding(
        ticker="NVDA",
        kind=AssetKind.OPTION,
        qty=2,
        cost_basis=12.50,
        option_kind=OptionKind.CALL,
        strike=180.0,
        expiry=EXPIRY,
        target=25.0,
        stop=6.0,
    )


@pytest.fixture
def aapl_shares() -> Holding:
    return Holding(ticker="AAPL", kind=AssetKind.SHARES, qty=10, cost_basis=225.40)


def make_chain(ticker: str = "NVDA", expiry: date = EXPIRY, spot: float = 185.0) -> list[ChainRow]:
    """A small synthetic chain spanning strikes around spot."""
    rows = []
    for strike in (160.0, 170.0, 180.0, 190.0, 200.0):
        moneyness = abs(strike - spot) / spot
        iv = 0.40 + moneyness * 0.5  # a crude smile
        for kind in (OptionKind.CALL, OptionKind.PUT):
            intrinsic = max(spot - strike, 0) if kind is OptionKind.CALL else max(strike - spot, 0)
            mid = intrinsic + 8.0
            rows.append(
                ChainRow(
                    ticker=ticker,
                    expiry=expiry,
                    strike=strike,
                    option_kind=kind,
                    bid=round(mid - 0.25, 2),
                    ask=round(mid + 0.25, 2),
                    last=round(mid, 2),
                    iv=round(iv, 4),
                    volume=100,
                    open_interest=500,
                )
            )
    return rows


def seed_iv_history(store: Store, ticker: str, asof: date, days: int, base: float = 0.40) -> None:
    """Write ``days`` of ATM-IV history ending the day before ``asof``."""
    for i in range(days):
        d = asof - timedelta(days=days - i)
        store.record_underlying_iv(d, ticker, base + (i % 10) * 0.01, 185.0)
