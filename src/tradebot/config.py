"""Configuration. Every threshold named in the layer specs is a knob here.

Defaults match the spec exactly (risk-free 0.045, ticker cap 40%, sector cap
60%, IV lookback 252d, min history 20d, rich >70 / cheap <30, DTE watch 45d,
news window 3d). Overridden by ``config/config.yaml``; secrets come only from
the environment, never from YAML.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any

import yaml

__all__ = ["Config", "load_config", "REPO_ROOT"]

REPO_ROOT = Path(__file__).resolve().parents[2]


@dataclass
class ValuationConfig:
    risk_free_rate: float = 0.045
    dividend_yield: float = 0.0
    # Chain IV outside this band is treated as broken and re-solved locally.
    iv_sanity_min: float = 0.01
    iv_sanity_max: float = 5.0
    # A quote wider than this fraction of mark gets a liquidity warning.
    wide_spread_pct: float = 0.15


@dataclass
class AllocationConfig:
    ticker_concentration_cap: float = 0.40
    sector_concentration_cap: float = 0.60


@dataclass
class IVEnvConfig:
    rank_lookback_days: int = 252
    min_history_days: int = 20
    rich_threshold: float = 70.0
    cheap_threshold: float = 30.0


@dataclass
class ExpiryConfig:
    # Surface upcoming time-decay / roll decisions. Informational only --
    # the analytics layer never advises rolling.
    dte_watch_threshold: int = 45


@dataclass
class MacroWeights:
    """Must sum to 1.0. Validated at load."""

    vix_level: float = 0.30
    vix_percentile: float = 0.20
    vix_term_structure: float = 0.20
    breadth: float = 0.20
    credit_spread: float = 0.10

    def as_dict(self) -> dict[str, float]:
        return {f.name: getattr(self, f.name) for f in fields(self)}

    def validate(self) -> None:
        total = sum(self.as_dict().values())
        if abs(total - 1.0) > 1e-6:
            raise ValueError(
                f"macro.weights must sum to 1.0, got {total:.6f} "
                f"({', '.join(f'{k}={v}' for k, v in self.as_dict().items())})"
            )
        for k, v in self.as_dict().items():
            if v < 0:
                raise ValueError(f"macro weight {k} must be >= 0, got {v}")


@dataclass
class MacroConfig:
    enabled: bool = True
    weights: MacroWeights = field(default_factory=MacroWeights)
    vix_percentile_lookback_days: int = 252
    breadth_proxy: str = "RSP/SPY"  # equal- vs cap-weight ratio as breadth proxy
    credit_spread_pair: str = "HYG/TLT"


@dataclass
class NewsConfig:
    """Claude-backed news layer.

    Off by default, and every knob here exists to keep spend near zero:
    one batched call per day for the whole book rather than one per name,
    a content hash so an unchanged headline set never re-bills, and a hard
    daily cap that aborts the layer rather than overrunning.
    """

    enabled: bool = False
    lookback_days: int = 3
    model: str = "claude-haiku-4-5"
    max_headlines_per_ticker: int = 8
    batch_all_tickers: bool = True
    cache_ttl_hours: int = 20
    max_calls_per_day: int = 4
    max_output_tokens: int = 2000


@dataclass
class PaperConfig:
    starting_cash: float = 100.0
    currency: str = "GBP"
    commission_per_contract: float = 0.65
    commission_per_share: float = 0.0
    # Fraction of the full bid-ask spread given up against you from mid.
    # 0.5 fills at the touch (bid on a sell, ask on a buy), which is what a
    # market order actually does. Lower values model price improvement.
    slippage_pct_of_spread: float = 0.5


@dataclass
class ExecutionConfig:
    """Execution mode. Paper is the only accepted value.

    This exists so that "paper only" is a checked invariant rather than an
    absence -- a live adapter added later cannot be switched on by editing a
    config file alone, and both the config loader and PaperBroker reject
    anything else.
    """

    mode: str = "paper"


@dataclass
class PathsConfig:
    snapshots_dir: str = "snapshots"
    reports_dir: str = "reports"
    database: str = "data/tradebot.sqlite"
    portfolio: str = "config/portfolio.yaml"
    sectors: str = "config/sectors.yaml"


@dataclass
class Config:
    valuation: ValuationConfig = field(default_factory=ValuationConfig)
    allocation: AllocationConfig = field(default_factory=AllocationConfig)
    iv_env: IVEnvConfig = field(default_factory=IVEnvConfig)
    expiry: ExpiryConfig = field(default_factory=ExpiryConfig)
    macro: MacroConfig = field(default_factory=MacroConfig)
    news: NewsConfig = field(default_factory=NewsConfig)
    paper: PaperConfig = field(default_factory=PaperConfig)
    execution: ExecutionConfig = field(default_factory=ExecutionConfig)
    paths: PathsConfig = field(default_factory=PathsConfig)

    root: Path = field(default=REPO_ROOT)

    def path(self, attr: str) -> Path:
        """Resolve a configured relative path against the repo root."""
        p = Path(getattr(self.paths, attr))
        return p if p.is_absolute() else (self.root / p)

    @property
    def anthropic_api_key(self) -> str | None:
        """Never read from YAML -- environment only."""
        return os.environ.get("ANTHROPIC_API_KEY") or None


def _merge(instance: Any, data: dict[str, Any], path: str = "") -> Any:
    """Recursively overlay a dict onto a dataclass, rejecting unknown keys.

    Unknown keys are an error rather than a silent ignore: a typo'd threshold
    that quietly keeps the default is exactly the sort of bug that only shows
    up as money.
    """
    known = {f.name: f for f in fields(instance)}
    for key, value in data.items():
        where = f"{path}{key}"
        if key not in known:
            raise ValueError(f"unknown config key {where!r} (valid: {', '.join(sorted(known))})")
        current = getattr(instance, key)
        if is_dataclass(current) and isinstance(value, dict):
            _merge(current, value, f"{where}.")
        else:
            setattr(instance, key, value)
    return instance


def load_config(path: str | Path | None = None, root: Path | None = None) -> Config:
    """Load config from YAML, falling back to spec defaults for anything absent."""
    cfg = Config()
    if root is not None:
        cfg.root = Path(root)

    cfg_path = Path(path) if path else (cfg.root / "config" / "config.yaml")
    if cfg_path.exists():
        raw = yaml.safe_load(cfg_path.read_text()) or {}
        if not isinstance(raw, dict):
            raise ValueError(f"{cfg_path}: expected a mapping at the top level")
        raw.pop("root", None)
        # Weights replace wholesale rather than merging, so a partial override
        # can't leave a set that silently no longer sums to 1.0.
        weights = (raw.get("macro") or {}).pop("weights", None)
        _merge(cfg, raw)
        if weights is not None:
            cfg.macro.weights = MacroWeights(**weights)

    if cfg.execution.mode != "paper":
        raise ValueError(
            f"execution.mode must be 'paper' (got {cfg.execution.mode!r}). "
            "This build ships no live-broker adapter; live trading is not supported."
        )
    cfg.macro.weights.validate()
    if not 0 < cfg.allocation.ticker_concentration_cap <= 1:
        raise ValueError("allocation.ticker_concentration_cap must be in (0, 1]")
    if not 0 < cfg.allocation.sector_concentration_cap <= 1:
        raise ValueError("allocation.sector_concentration_cap must be in (0, 1]")
    if cfg.iv_env.cheap_threshold >= cfg.iv_env.rich_threshold:
        raise ValueError("iv_env.cheap_threshold must be < rich_threshold")
    return cfg
