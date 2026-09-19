"""Dated chain snapshots: JSON on disk, mirrored to SQLite.

The spec calls these "required so later layers can diff today vs prior run for
new-strike / new-expiry detection and IV history" -- so this module is not
incidental logging, it is the input to Layer 2.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from ..store import Store
from ..types import ChainRow, utcnow

log = logging.getLogger(__name__)

__all__ = ["write_snapshot", "load_snapshot", "ChainDiff", "diff_against_previous"]


def snapshot_path(root: Path, ticker: str, asof: date) -> Path:
    return root / f"{ticker.upper()}_{asof:%Y-%m-%d}.json"


def write_snapshot(
    snapshots_dir: Path, store: Store, ticker: str, asof: date, rows: list[ChainRow], spot: float | None
) -> Path:
    """Persist the full pulled chain to a dated JSON file and mirror to SQLite."""
    snapshots_dir.mkdir(parents=True, exist_ok=True)
    path = snapshot_path(snapshots_dir, ticker, asof)
    payload = {
        "ticker": ticker.upper(),
        "asof": asof.isoformat(),
        "pulled_at": utcnow().isoformat(),
        "spot": spot,
        "row_count": len(rows),
        "rows": [r.to_json() for r in rows],
    }
    path.write_text(json.dumps(payload, indent=2, default=str))
    store.save_chain(asof, rows)
    if spot is not None:
        store.record_spot(asof, ticker.upper(), spot)
    log.info("snapshot %s: %d rows -> %s", ticker, len(rows), path.name)
    return path


def load_snapshot(snapshots_dir: Path, ticker: str, asof: date) -> dict[str, Any] | None:
    path = snapshot_path(snapshots_dir, ticker, asof)
    if not path.exists():
        return None
    return json.loads(path.read_text())


@dataclass(frozen=True)
class ChainDiff:
    """What changed in a ticker's chain since the previous stored run."""

    ticker: str
    baseline: date | None
    new_strikes: list[dict[str, Any]]
    new_expiries: list[str]
    removed_expiries: list[str]

    @property
    def has_changes(self) -> bool:
        return bool(self.new_strikes or self.new_expiries or self.removed_expiries)

    def to_json(self) -> dict[str, Any]:
        return {
            "ticker": self.ticker,
            "baseline_date": self.baseline.isoformat() if self.baseline else None,
            "new_strike_count": len(self.new_strikes),
            "new_strikes": self.new_strikes[:50],
            "new_expiries": self.new_expiries,
            "removed_expiries": self.removed_expiries,
            "note": (
                "No prior snapshot for this ticker -- diff starts from the next run."
                if self.baseline is None
                else None
            ),
        }


def diff_against_previous(store: Store, ticker: str, asof: date) -> ChainDiff:
    """Detect new strikes and new/removed expiries vs the last stored run."""
    baseline = store.previous_chain_date(ticker, asof)
    if baseline is None:
        return ChainDiff(ticker, None, [], [], [])

    today = store.chain_contracts(ticker, asof)
    prior = store.chain_contracts(ticker, baseline)

    today_expiries = {c[0] for c in today}
    prior_expiries = {c[0] for c in prior}

    new_strikes = [
        {"expiry": exp, "strike": strike, "option_kind": kind}
        for exp, strike, kind in sorted(today - prior)
        # A strike on a brand-new expiry isn't a "new strike" -- the whole
        # expiry is new, and reporting both double-counts the same event.
        if exp in prior_expiries
    ]
    return ChainDiff(
        ticker=ticker,
        baseline=baseline,
        new_strikes=new_strikes,
        new_expiries=sorted(today_expiries - prior_expiries),
        removed_expiries=sorted(prior_expiries - today_expiries),
    )
