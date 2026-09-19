"""Value types shared across all three layers.

Plain dataclasses with explicit ``to_json`` so every layer's output drops
straight into the n8n workflows without a serialisation shim.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from enum import Enum
from typing import Any

__all__ = [
    "AssetKind",
    "OptionKind",
    "Holding",
    "ChainRow",
    "MarkedPosition",
    "utcnow",
    "today_utc",
]


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def today_utc() -> date:
    return utcnow().date()


class AssetKind(str, Enum):
    SHARES = "shares"
    OPTION = "option"


class OptionKind(str, Enum):
    CALL = "call"
    PUT = "put"


@dataclass(frozen=True)
class Holding:
    """One line of the book as the user declares it in ``portfolio.yaml``.

    ``qty`` is shares for ``AssetKind.SHARES`` and **contracts** for options
    (negative for short). ``cost_basis`` is per share / per share-of-contract,
    i.e. the quoted premium, not the premium times 100.
    """

    ticker: str
    kind: AssetKind
    qty: float
    cost_basis: float
    option_kind: OptionKind | None = None
    strike: float | None = None
    expiry: date | None = None
    target: float | None = None
    stop: float | None = None
    multiplier: int = 100
    note: str = ""

    def __post_init__(self) -> None:
        if self.kind is AssetKind.OPTION:
            missing = [
                name
                for name, val in (
                    ("option_kind", self.option_kind),
                    ("strike", self.strike),
                    ("expiry", self.expiry),
                )
                if val is None
            ]
            if missing:
                raise ValueError(f"{self.ticker}: option holding missing {', '.join(missing)}")
            if self.strike is not None and self.strike <= 0:
                raise ValueError(f"{self.ticker}: strike must be > 0")
        if self.qty == 0:
            raise ValueError(f"{self.ticker}: qty must be non-zero")

    @property
    def is_option(self) -> bool:
        return self.kind is AssetKind.OPTION

    @property
    def is_short(self) -> bool:
        return self.qty < 0

    @property
    def contract_multiplier(self) -> int:
        """Shares controlled per unit of ``qty``. Always 1 for shares."""
        return self.multiplier if self.is_option else 1

    def occ_symbol(self) -> str:
        """OCC-style identifier, also used as the stable DB key for the line."""
        if not self.is_option:
            return self.ticker
        assert self.expiry is not None and self.strike is not None
        cp = "C" if self.option_kind is OptionKind.CALL else "P"
        strike_int = int(round(self.strike * 1000))
        return f"{self.ticker}{self.expiry:%y%m%d}{cp}{strike_int:08d}"

    def dte(self, asof: date | None = None) -> int | None:
        """Actual calendar days to expiry. ``None`` for shares."""
        if not self.is_option or self.expiry is None:
            return None
        return (self.expiry - (asof or today_utc())).days

    def to_json(self) -> dict[str, Any]:
        out = asdict(self)
        out["kind"] = self.kind.value
        out["option_kind"] = self.option_kind.value if self.option_kind else None
        out["expiry"] = self.expiry.isoformat() if self.expiry else None
        out["occ_symbol"] = self.occ_symbol()
        return out


@dataclass(frozen=True)
class ChainRow:
    """One strike from a pulled option chain, as the provider reported it."""

    ticker: str
    expiry: date
    strike: float
    option_kind: OptionKind
    bid: float | None = None
    ask: float | None = None
    last: float | None = None
    iv: float | None = None
    volume: float | None = None
    open_interest: float | None = None

    @property
    def mark(self) -> float | None:
        """Mark = mid (bid+ask)/2 when both exist, else last.

        A zero bid is a real quote (worthless contract), so only ``None`` and
        negatives disqualify a side. A crossed book (bid > ask) is bad data and
        falls through to last.
        """
        bid, ask = self.bid, self.ask
        if bid is not None and ask is not None and bid >= 0 and ask > 0 and bid <= ask:
            return (bid + ask) / 2.0
        if self.last is not None and self.last >= 0:
            return self.last
        return None

    @property
    def spread_pct(self) -> float | None:
        """Bid-ask spread as a fraction of mark -- a liquidity tell."""
        mark = self.mark
        if mark is None or mark <= 0 or self.bid is None or self.ask is None:
            return None
        if self.ask < self.bid:
            return None
        return (self.ask - self.bid) / mark

    def to_json(self) -> dict[str, Any]:
        return {
            "ticker": self.ticker,
            "expiry": self.expiry.isoformat(),
            "strike": self.strike,
            "option_kind": self.option_kind.value,
            "bid": self.bid,
            "ask": self.ask,
            "last": self.last,
            "iv": self.iv,
            "volume": self.volume,
            "open_interest": self.open_interest,
            "mark": self.mark,
        }


@dataclass
class MarkedPosition:
    """A holding valued against today's market -- the Layer 1 output row."""

    holding: Holding
    asof: date
    spot: float | None = None
    mark: float | None = None
    iv: float | None = None
    delta: float = 0.0
    gamma: float = 0.0
    theta: float = 0.0  # $/day, position-level
    vega: float = 0.0  # $/IV point, position-level
    dte: int | None = None
    stale: bool = False
    warnings: list[str] = field(default_factory=list)

    @property
    def exposure_shares(self) -> float:
        """Share-equivalent count this line controls (sign-aware)."""
        return self.holding.qty * self.holding.contract_multiplier

    @property
    def current_value(self) -> float | None:
        """Mark-to-market value. Short lines are negative (a liability)."""
        if self.mark is None:
            return None
        return self.mark * self.exposure_shares

    @property
    def cost_value(self) -> float:
        return self.holding.cost_basis * self.exposure_shares

    @property
    def unrealised_pnl(self) -> float | None:
        cv = self.current_value
        return None if cv is None else cv - self.cost_value

    @property
    def unrealised_pnl_pct(self) -> float | None:
        """P&L as a fraction of capital at risk (absolute cost basis)."""
        pnl = self.unrealised_pnl
        denom = abs(self.cost_value)
        if pnl is None or denom < 1e-12:
            return None
        return pnl / denom

    def _progress(self, level: float | None) -> float | None:
        """Fraction of the way from cost basis to ``level`` on mark.

        Clamped at 0 below the basis; may exceed 1.0 once the level is passed,
        which is the honest reading ("120% of the way to target").
        """
        if level is None or self.mark is None:
            return None
        basis = self.holding.cost_basis
        span = level - basis
        if abs(span) < 1e-12:
            return None
        prog = (self.mark - basis) / span
        return max(prog, 0.0) if math.isfinite(prog) else None

    @property
    def progress_to_target(self) -> float | None:
        return self._progress(self.holding.target)

    @property
    def progress_to_stop(self) -> float | None:
        return self._progress(self.holding.stop)

    def to_json(self) -> dict[str, Any]:
        def pct(x: float | None) -> float | None:
            return None if x is None else round(x * 100.0, 2)

        return {
            "symbol": self.holding.occ_symbol(),
            "ticker": self.holding.ticker,
            "kind": self.holding.kind.value,
            "qty": self.holding.qty,
            "asof": self.asof.isoformat(),
            "spot": self.spot,
            "mark": self.mark,
            "cost_basis": self.holding.cost_basis,
            "current_value": self.current_value,
            "cost_value": self.cost_value,
            "unrealised_pnl": self.unrealised_pnl,
            "unrealised_pnl_pct": pct(self.unrealised_pnl_pct),
            "dte": self.dte,
            "iv": self.iv,
            "greeks": {
                "delta": round(self.delta, 4),
                "gamma": round(self.gamma, 6),
                "theta": round(self.theta, 4),
                "vega": round(self.vega, 4),
            },
            "progress_to_target_pct": pct(self.progress_to_target),
            "progress_to_stop_pct": pct(self.progress_to_stop),
            "stale": self.stale,
            "warnings": self.warnings,
        }
