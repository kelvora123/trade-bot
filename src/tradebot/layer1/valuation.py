"""Layer 1: mark the book and compute Greeks.

Marking rule, per spec:
  * options -- mid (bid+ask)/2 when both exist, else last
  * shares  -- current spot

Greeks come from the contract's own IV via local Black-Scholes, using a
configurable annual risk-free rate and *actual* days to expiry (calendar days
/ 365, not a trading-day approximation).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from typing import Any

from ..config import Config
from ..store import Store
from ..types import AssetKind, ChainRow, Holding, MarkedPosition, OptionKind
from .blackscholes import greeks as bs_greeks
from .blackscholes import implied_vol

log = logging.getLogger(__name__)

__all__ = ["value_book", "ValuationResult", "atm_iv"]


def _match_row(rows: list[ChainRow], holding: Holding) -> ChainRow | None:
    """Find the chain row for a holding, tolerating float strike drift."""
    for r in rows:
        if (
            r.expiry == holding.expiry
            and r.option_kind == holding.option_kind
            and abs(r.strike - (holding.strike or 0.0)) < 1e-6
        ):
            return r
    return None


def atm_iv(
    rows: list[ChainRow], spot: float, *, prefer_dte: int = 30, asof: date | None = None
) -> float | None:
    """Underlying-level at-the-money IV.

    Used as the IV-rank series because a single contract's IV is contaminated
    by its own drift toward expiry: as a contract ages its IV moves for
    reasons that have nothing to do with the vol environment, which would make
    a 252-day rank meaningless. An ATM-IV series with a roughly constant target
    tenor is the stable comparison.
    """
    from ..types import today_utc

    asof = asof or today_utc()
    candidates = [r for r in rows if r.iv and 0.01 < r.iv < 5.0 and r.expiry > asof]
    if not candidates or spot <= 0:
        return None

    # Pick the expiry closest to the target tenor, then the strike nearest spot.
    best_expiry = min(candidates, key=lambda r: abs((r.expiry - asof).days - prefer_dte)).expiry
    near = [r for r in candidates if r.expiry == best_expiry]
    if not near:
        return None

    nearest_strike = min(abs(r.strike - spot) for r in near)
    # Average the call and put at that strike: put-call parity says they should
    # agree, so averaging cancels some of the quote noise.
    at_strike = [r.iv for r in near if abs(abs(r.strike - spot) - nearest_strike) < 1e-9 and r.iv]
    return sum(at_strike) / len(at_strike) if at_strike else None


@dataclass
class ValuationResult:
    asof: date
    positions: list[MarkedPosition]
    spots: dict[str, float]
    total_value: float
    total_cost: float
    warnings: list[str]

    @property
    def total_pnl(self) -> float:
        return self.total_value - self.total_cost

    @property
    def total_pnl_pct(self) -> float | None:
        denom = abs(self.total_cost)
        return None if denom < 1e-12 else self.total_pnl / denom

    def to_json(self) -> dict[str, Any]:
        return {
            "layer": "1_data_valuation",
            "asof": self.asof.isoformat(),
            "spots": self.spots,
            "totals": {
                "current_value": round(self.total_value, 2),
                "cost_value": round(self.total_cost, 2),
                "unrealised_pnl": round(self.total_pnl, 2),
                "unrealised_pnl_pct": (
                    None if self.total_pnl_pct is None else round(self.total_pnl_pct * 100, 2)
                ),
            },
            "positions": [p.to_json() for p in self.positions],
            "warnings": self.warnings,
        }


def value_book(
    holdings: list[Holding],
    chains: dict[str, list[ChainRow]],
    spots: dict[str, float | None],
    cfg: Config,
    asof: date,
    store: Store | None = None,
) -> ValuationResult:
    """Mark every holding and compute per-position Greeks."""
    v = cfg.valuation
    marked: list[MarkedPosition] = []
    book_warnings: list[str] = []
    clean_spots: dict[str, float] = {t: s for t, s in spots.items() if s is not None and s > 0}

    # Record the ATM-IV series once per underlying, for Layer 2's IV rank.
    if store is not None:
        for ticker, rows in chains.items():
            spot = clean_spots.get(ticker)
            if spot is None:
                continue
            iv = atm_iv(rows, spot, asof=asof)
            if iv is not None:
                store.record_underlying_iv(asof, ticker, iv, spot)

    for h in holdings:
        pos = MarkedPosition(holding=h, asof=asof, dte=h.dte(asof))
        spot = clean_spots.get(h.ticker)
        pos.spot = spot

        if spot is None:
            pos.stale = True
            pos.warnings.append("no spot price available")
            book_warnings.append(f"{h.ticker}: no spot price; position excluded from totals")
            marked.append(pos)
            continue

        if h.kind is AssetKind.SHARES:
            pos.mark = spot
            pos.delta = h.qty  # one share = one delta, by definition
            marked.append(pos)
            continue

        row = _match_row(chains.get(h.ticker, []), h)
        if row is None:
            pos.stale = True
            pos.warnings.append(f"contract {h.occ_symbol()} not found in chain")
            book_warnings.append(f"{h.occ_symbol()}: not present in today's chain")
            marked.append(pos)
            continue

        pos.mark = row.mark
        if pos.mark is None:
            pos.stale = True
            pos.warnings.append("no bid/ask/last -- cannot mark")

        if row.spread_pct is not None and row.spread_pct > v.wide_spread_pct:
            pos.warnings.append(f"wide spread: {row.spread_pct * 100:.1f}% of mark")

        dte = pos.dte if pos.dte is not None else 0
        if dte < 0:
            pos.warnings.append("contract has expired")
            book_warnings.append(f"{h.occ_symbol()}: expired {abs(dte)}d ago")
        t_years = max(dte, 0) / 365.0

        # Prefer the chain's IV; re-solve locally only when it is absent or
        # outside a sane band (yfinance regularly reports 1e-5 or 0 on illiquid
        # strikes, which would zero out every Greek on the line).
        iv = row.iv
        if iv is None or not (v.iv_sanity_min <= iv <= v.iv_sanity_max):
            solved = (
                implied_vol(
                    pos.mark,
                    spot,
                    h.strike or 0.0,
                    t_years,
                    v.risk_free_rate,
                    (h.option_kind or OptionKind.CALL).value,
                    v.dividend_yield,
                )
                if pos.mark
                else None
            )
            if solved is not None:
                pos.warnings.append(
                    f"chain IV {iv!r} out of range; solved locally to {solved * 100:.1f}%"
                )
                iv = solved
            else:
                pos.warnings.append(f"unusable IV ({iv!r}); Greeks unavailable")
                iv = None
        pos.iv = iv

        if iv is not None and h.strike:
            g = bs_greeks(
                spot,
                h.strike,
                t_years,
                v.risk_free_rate,
                iv,
                (h.option_kind or OptionKind.CALL).value,
                v.dividend_yield,
            ).scaled(h.qty, h.multiplier)
            pos.delta, pos.gamma, pos.theta, pos.vega = g.delta, g.gamma, g.theta, g.vega
            if store is not None:
                store.record_iv(asof, h.occ_symbol(), h.ticker, iv)

        marked.append(pos)

    total_value = sum(p.current_value or 0.0 for p in marked)
    total_cost = sum(p.cost_value for p in marked if p.current_value is not None)
    return ValuationResult(asof, marked, clean_spots, total_value, total_cost, book_warnings)
