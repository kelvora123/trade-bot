"""Load the book from ``config/portfolio.yaml``."""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import Any

import yaml

from .types import AssetKind, Holding, OptionKind

__all__ = ["load_portfolio", "load_sectors"]


def _as_date(value: Any, ctx: str) -> date:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    try:
        return date.fromisoformat(str(value))
    except ValueError as exc:
        raise ValueError(f"{ctx}: expiry {value!r} is not an ISO date (YYYY-MM-DD)") from exc


def load_portfolio(path: str | Path) -> list[Holding]:
    """Parse the holdings file into validated ``Holding`` objects."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(
            f"portfolio file not found: {path}. Copy config/portfolio.example.yaml to get started."
        )

    raw = yaml.safe_load(path.read_text()) or {}
    entries = raw.get("holdings") or []
    if not isinstance(entries, list):
        raise ValueError(f"{path}: 'holdings' must be a list")

    holdings: list[Holding] = []
    for i, item in enumerate(entries):
        ctx = f"{path}: holdings[{i}]"
        if not isinstance(item, dict):
            raise ValueError(f"{ctx}: each holding must be a mapping")

        ticker = str(item.get("ticker", "")).strip().upper()
        if not ticker:
            raise ValueError(f"{ctx}: 'ticker' is required")

        kind_raw = str(item.get("kind", "shares")).lower()
        try:
            kind = AssetKind(kind_raw)
        except ValueError as exc:
            raise ValueError(f"{ctx}: kind must be 'shares' or 'option', got {kind_raw!r}") from exc

        option_kind = None
        if kind is AssetKind.OPTION:
            ok_raw = str(item.get("option_kind", "")).lower()
            try:
                option_kind = OptionKind(ok_raw)
            except ValueError as exc:
                raise ValueError(
                    f"{ctx}: option_kind must be 'call' or 'put', got {ok_raw!r}"
                ) from exc

        try:
            holdings.append(
                Holding(
                    ticker=ticker,
                    kind=kind,
                    qty=float(item["qty"]),
                    cost_basis=float(item["cost_basis"]),
                    option_kind=option_kind,
                    strike=float(item["strike"]) if item.get("strike") is not None else None,
                    expiry=_as_date(item["expiry"], ctx) if item.get("expiry") else None,
                    target=float(item["target"]) if item.get("target") is not None else None,
                    stop=float(item["stop"]) if item.get("stop") is not None else None,
                    multiplier=int(item.get("multiplier", 100)),
                    note=str(item.get("note", "")),
                )
            )
        except KeyError as exc:
            raise ValueError(f"{ctx}: missing required field {exc.args[0]!r}") from exc
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{ctx}: {exc}") from exc

    return holdings


def load_sectors(path: str | Path) -> dict[str, str]:
    """Ticker -> sector map. Absent file is fine; provider info is the fallback."""
    path = Path(path)
    if not path.exists():
        return {}
    raw = yaml.safe_load(path.read_text()) or {}
    sectors = raw.get("sectors") or raw
    if not isinstance(sectors, dict):
        return {}
    return {str(k).upper(): str(v) for k, v in sectors.items()}
