"""Layer 2: portfolio analytics.

Allocation and concentration, aggregate Greeks, the IV environment, and the
expiry watch. Everything here is descriptive by design: the spec's rule is
"surface the fact, do not advise", so this layer reports what is true of the
book and never recommends an action. Flags are informational.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from ..config import Config
from ..layer1.valuation import ValuationResult
from ..store import Store
from ..types import AssetKind, MarkedPosition

log = logging.getLogger(__name__)

__all__ = ["analyse", "AnalyticsResult", "iv_rank", "iv_percentile"]

UNKNOWN_SECTOR = "Unknown"


# ------------------------------------------------------------------ IV stats


def iv_rank(current: float, series: list[float]) -> float | None:
    """Where current IV sits in the lookback's *range*, 0-100.

    Degenerate when the window is flat (max == min); returns None rather than
    dividing by zero or silently reporting 50.
    """
    if not series:
        return None
    lo, hi = min(series), max(series)
    span = hi - lo
    if span < 1e-12:
        return None
    return max(0.0, min(100.0, (current - lo) / span * 100.0))


def iv_percentile(current: float, series: list[float]) -> float | None:
    """Fraction of lookback days with IV strictly below current, 0-100."""
    if not series:
        return None
    below = sum(1 for v in series if v < current)
    return below / len(series) * 100.0


# ------------------------------------------------------------------- results


@dataclass
class AnalyticsResult:
    asof: date
    total_value: float
    by_ticker: dict[str, float]
    by_sector: dict[str, float]
    concentration_flags: list[dict[str, Any]]
    aggregate_greeks: dict[str, float]
    iv_environment: list[dict[str, Any]]
    expiry_watch: list[dict[str, Any]]
    notes: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {
            "layer": "2_portfolio_analytics",
            "asof": self.asof.isoformat(),
            "total_value": round(self.total_value, 2),
            "allocation": {
                "by_ticker_pct": {k: round(v * 100, 2) for k, v in self.by_ticker.items()},
                "by_sector_pct": {k: round(v * 100, 2) for k, v in self.by_sector.items()},
                "concentration_flags": self.concentration_flags,
            },
            "aggregate_greeks": self.aggregate_greeks,
            "iv_environment": self.iv_environment,
            "expiry_watch": self.expiry_watch,
            "notes": self.notes,
            "disclaimer": "Informational only. This layer surfaces facts and does not advise action.",
        }


# ----------------------------------------------------------------- allocation


def _resolve_sector(ticker: str, sector_map: dict[str, str], provider: Any | None) -> str:
    """Config lookup first, provider ``info`` as fallback, then Unknown."""
    if ticker in sector_map:
        return sector_map[ticker]
    if provider is not None:
        try:
            found = provider.sector(ticker)
            if found:
                sector_map[ticker] = found  # memoise for the rest of the run
                return found
        except Exception as exc:
            log.debug("sector fallback failed for %s: %s", ticker, exc)
    return UNKNOWN_SECTOR


def _allocation(
    positions: list[MarkedPosition], sector_map: dict[str, str], provider: Any | None
) -> tuple[dict[str, float], dict[str, float], float]:
    """Share of total value by ticker and by sector.

    Uses **absolute** value per line as the denominator. A short option is a
    negative value, and netting it against longs would report a book with
    offsetting positions as having almost no allocation anywhere -- which hides
    exactly the concentration this is meant to expose.
    """
    ticker_value: dict[str, float] = defaultdict(float)
    for p in positions:
        cv = p.current_value
        if cv is None:
            continue
        ticker_value[p.holding.ticker] += abs(cv)

    total = sum(ticker_value.values())
    if total < 1e-12:
        return {}, {}, 0.0

    by_ticker = {t: v / total for t, v in sorted(ticker_value.items(), key=lambda kv: -kv[1])}
    sector_value: dict[str, float] = defaultdict(float)
    for t, v in ticker_value.items():
        sector_value[_resolve_sector(t, sector_map, provider)] += v
    by_sector = {s: v / total for s, v in sorted(sector_value.items(), key=lambda kv: -kv[1])}
    return by_ticker, by_sector, total


def _concentration_flags(
    by_ticker: dict[str, float], by_sector: dict[str, float], cfg: Config
) -> list[dict[str, Any]]:
    caps = cfg.allocation
    flags = []
    for name, w in by_ticker.items():
        if w > caps.ticker_concentration_cap:
            # Format the message from the already-rounded weight so the flag
            # text and the dashboard's bar label never disagree by a decimal.
            pct = round(w * 100, 2)
            flags.append(
                {
                    "kind": "ticker",
                    "name": name,
                    "weight_pct": pct,
                    "cap_pct": round(caps.ticker_concentration_cap * 100, 2),
                    "message": (
                        f"{name} is {pct:.1f}% of book value, above the "
                        f"{caps.ticker_concentration_cap * 100:.0f}% concentration cap."
                    ),
                }
            )
    for name, w in by_sector.items():
        if w > caps.sector_concentration_cap:
            pct = round(w * 100, 2)
            flags.append(
                {
                    "kind": "sector",
                    "name": name,
                    "weight_pct": pct,
                    "cap_pct": round(caps.sector_concentration_cap * 100, 2),
                    "message": (
                        f"Sector {name} is {pct:.1f}% of book value, above the "
                        f"{caps.sector_concentration_cap * 100:.0f}% concentration cap."
                    ),
                }
            )
    return flags


# ------------------------------------------------------------ aggregate greeks


def _aggregate_greeks(positions: list[MarkedPosition]) -> dict[str, float]:
    """Book-level exposures in the units a human reads on a blotter."""
    live = [p for p in positions if not p.stale]
    net_delta_shares = sum(p.delta for p in live)
    daily_theta = sum(p.theta for p in live)
    net_vega = sum(p.vega for p in live)
    net_gamma = sum(p.gamma for p in live)
    delta_dollars = sum(p.delta * (p.spot or 0.0) for p in live)
    return {
        "net_delta_shares": round(net_delta_shares, 2),
        "net_delta_dollars": round(delta_dollars, 2),
        "net_gamma_shares_per_point": round(net_gamma, 4),
        "daily_theta_dollars": round(daily_theta, 2),
        "net_vega_dollars_per_iv_point": round(net_vega, 2),
        "positions_included": len(live),
        "positions_excluded_stale": len(positions) - len(live),
    }


# ------------------------------------------------------------- iv environment


def _iv_environment(
    positions: list[MarkedPosition], store: Store, cfg: Config, asof: date
) -> list[dict[str, Any]]:
    """Per-option IV with rank/percentile over the stored snapshot history."""
    env = cfg.iv_env
    out: list[dict[str, Any]] = []
    for p in positions:
        if p.holding.kind is not AssetKind.OPTION or p.iv is None:
            continue
        series = store.iv_series(p.holding.ticker, asof, env.rank_lookback_days)
        n = len(series)
        entry: dict[str, Any] = {
            "symbol": p.holding.occ_symbol(),
            "ticker": p.holding.ticker,
            "current_iv_pct": round(p.iv * 100, 2),
            "history_days": n,
            "lookback_days": env.rank_lookback_days,
        }
        if n < env.min_history_days:
            entry.update(
                {
                    "iv_rank": None,
                    "iv_percentile": None,
                    "status": "building history",
                    "note": (
                        f"{n} of {env.min_history_days} days of snapshot history collected; "
                        "IV rank available once the minimum is reached."
                    ),
                }
            )
        else:
            rank = iv_rank(p.iv, series)
            pct = iv_percentile(p.iv, series)
            if rank is None:
                status = "flat history"
            elif rank > env.rich_threshold:
                status = "rich"
            elif rank < env.cheap_threshold:
                status = "cheap"
            else:
                status = "normal"
            entry.update(
                {
                    "iv_rank": None if rank is None else round(rank, 1),
                    "iv_percentile": None if pct is None else round(pct, 1),
                    "status": status,
                    "rich_above": env.rich_threshold,
                    "cheap_below": env.cheap_threshold,
                }
            )
        out.append(entry)
    return out


# --------------------------------------------------------------- expiry watch


def _expiry_watch(positions: list[MarkedPosition], cfg: Config) -> list[dict[str, Any]]:
    """Options inside the DTE threshold, so decay/roll decisions are visible.

    Reports the facts -- days left, daily theta, what that theta is as a share
    of the position's remaining value -- and stops there. Per spec, this layer
    does not advise rolling.
    """
    threshold = cfg.expiry.dte_watch_threshold
    out: list[dict[str, Any]] = []
    for p in positions:
        if p.holding.kind is not AssetKind.OPTION or p.dte is None or p.dte > threshold:
            continue
        value = p.current_value
        theta_pct = None
        if value is not None and abs(value) > 1e-9:
            theta_pct = round(abs(p.theta) / abs(value) * 100, 2)
        out.append(
            {
                "symbol": p.holding.occ_symbol(),
                "ticker": p.holding.ticker,
                "dte": p.dte,
                "expiry": p.holding.expiry.isoformat() if p.holding.expiry else None,
                "threshold_days": threshold,
                "current_value": None if value is None else round(value, 2),
                "daily_theta_dollars": round(p.theta, 2),
                "daily_theta_pct_of_value": theta_pct,
                "expired": p.dte < 0,
                "fact": (
                    f"{p.holding.occ_symbol()} has {p.dte} days to expiry and is decaying at "
                    f"{abs(p.theta):.2f}/day"
                    + (f" ({theta_pct:.2f}% of position value per day)." if theta_pct is not None else ".")
                ),
            }
        )
    return sorted(out, key=lambda e: e["dte"])


# -------------------------------------------------------------------- entry


def analyse(
    valuation: ValuationResult,
    store: Store,
    cfg: Config,
    sector_map: dict[str, str] | None = None,
    provider: Any | None = None,
) -> AnalyticsResult:
    """Run the full Layer 2 analytics pass over a marked book."""
    sector_map = dict(sector_map or {})
    positions = valuation.positions
    by_ticker, by_sector, total = _allocation(positions, sector_map, provider)

    notes: list[str] = []
    if total < 1e-12:
        notes.append("Book has no markable value; allocation and concentration are unavailable.")
    unknown_w = by_sector.get(UNKNOWN_SECTOR, 0.0)
    if unknown_w > 0.10:
        notes.append(
            f"{unknown_w * 100:.0f}% of book value has no sector mapping; "
            "add entries to config/sectors.yaml for accurate sector concentration."
        )

    result = AnalyticsResult(
        asof=valuation.asof,
        total_value=total,
        by_ticker=by_ticker,
        by_sector=by_sector,
        concentration_flags=_concentration_flags(by_ticker, by_sector, cfg),
        aggregate_greeks=_aggregate_greeks(positions),
        iv_environment=_iv_environment(positions, store, cfg, valuation.asof),
        expiry_watch=_expiry_watch(positions, cfg),
        notes=notes,
    )
    return result
