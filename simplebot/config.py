"""Load and validate config.yaml. Secrets never live here: they come from .env via os.environ."""

from __future__ import annotations

import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml

VALID_MODES = ("paper", "live")
VALID_BROKERS = ("paper", "trading212", "ibkr")
VALID_ACCOUNTS = ("invest", "isa")
VALID_PROVIDERS = ("yahoo", "alpaca", "csv", "synthetic")
VALID_COACH_PROVIDERS = ("none", "claude", "ollama")
VALID_DECIDER_ROLES = ("last_resort", "final_say", "primary")
TIMEFRAME_RE = re.compile(r"^(\d+)m$")

# Keys under `risk:` are owned by the human. Nothing automated may change them.
RISK_KEYS = frozenset(
    {"risk_per_trade_pct", "max_daily_loss_pct", "max_positions", "flatten_minutes_before_close"}
)


class ConfigError(ValueError):
    pass


@dataclass
class RiskConfig:
    risk_per_trade_pct: float = 0.5
    max_daily_loss_pct: float = 2.0
    max_positions: int = 3
    flatten_minutes_before_close: int = 10


@dataclass
class CostConfig:
    commission: float = 0.0
    fx_fee_pct: float = 0.15
    spread_bps: float = 2.0
    slippage_bps: float = 3.0


@dataclass
class DataConfig:
    provider: str = "yahoo"
    lookback_days: int = 60
    cache_dir: str = "cache"
    csv_dir: str = "data"


@dataclass
class LearningConfig:
    refit_every_days: int = 5
    min_oos_trades: int = 30
    min_sharpe_gain: float = 0.1
    train_fraction: float = 0.7


@dataclass
class CoachConfig:
    provider: str = "none"
    model: str = "claude-opus-5-5"
    ollama_model: str = "llama3.1:8b"
    lookback_days: int = 20
    auto_apply_params: bool = False
    max_daily_cost_usd: float = 0.5


@dataclass
class DeciderConfig:
    """An LLM making buy/sell decisions inside the trading loop. Off unless provider is set."""

    provider: str = "none"
    role: str = "last_resort"
    model: str = "claude-opus-5-5"
    ollama_model: str = "llama3.1:8b"
    every_minutes: int = 30
    max_stop_pct: float = 2.0
    min_confidence: float = 0.6
    max_daily_cost_usd: float = 2.0
    effort: str = "medium"
    timeout_seconds: float = 30.0


@dataclass
class Config:
    mode: str = "paper"
    broker: str = "paper"
    account: str = "invest"
    symbols: list[str] = field(default_factory=lambda: ["QQQ", "SPY"])
    timeframe: str = "5m"
    strategy: str = "orb"
    params: dict[str, Any] = field(default_factory=dict)
    starting_cash: float = 1000.0
    allow_short: bool = False
    max_entries_per_day: int = 1
    risk: RiskConfig = field(default_factory=RiskConfig)
    costs: CostConfig = field(default_factory=CostConfig)
    data: DataConfig = field(default_factory=DataConfig)
    learning: LearningConfig = field(default_factory=LearningConfig)
    coach: CoachConfig = field(default_factory=CoachConfig)
    decider: DeciderConfig = field(default_factory=DeciderConfig)
    symbol_map: dict[str, str] = field(default_factory=dict)
    poll_seconds: int = 30
    journal_path: str = "simplebot.sqlite"
    reports_dir: str = "reports"
    telegram: bool = True
    account_currency: str = "GBP"
    path: str = "config.yaml"

    @property
    def timeframe_minutes(self) -> int:
        return timeframe_minutes(self.timeframe)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("path", None)
        return d


def timeframe_minutes(tf: str) -> int:
    m = TIMEFRAME_RE.match(str(tf).strip())
    if not m or int(m.group(1)) <= 0:
        raise ConfigError(f"timeframe must look like '5m', got {tf!r}")
    return int(m.group(1))


def _build(section_cls, raw: dict | None, name: str):
    raw = raw or {}
    if not isinstance(raw, dict):
        raise ConfigError(f"'{name}' must be a mapping")
    allowed = {f for f in section_cls.__dataclass_fields__}
    unknown = set(raw) - allowed
    if unknown:
        raise ConfigError(f"unknown keys under '{name}': {sorted(unknown)}")
    return section_cls(**raw)


def from_dict(raw: dict[str, Any], path: str = "config.yaml") -> Config:
    raw = dict(raw or {})
    cfg = Config(
        mode=str(raw.get("mode", "paper")).lower(),
        broker=str(raw.get("broker", "paper")).lower(),
        account=str(raw.get("account", "invest")).lower(),
        symbols=[str(s).upper() for s in (raw.get("symbols") or [])],
        timeframe=str(raw.get("timeframe", "5m")),
        strategy=str(raw.get("strategy", "orb")),
        params=dict(raw.get("params") or {}),
        starting_cash=float(raw.get("starting_cash", 1000.0)),
        allow_short=bool(raw.get("allow_short", False)),
        max_entries_per_day=int(raw.get("max_entries_per_day", 1)),
        risk=_build(RiskConfig, raw.get("risk"), "risk"),
        costs=_build(CostConfig, raw.get("costs"), "costs"),
        data=_build(DataConfig, raw.get("data"), "data"),
        learning=_build(LearningConfig, raw.get("learning"), "learning"),
        coach=_build(CoachConfig, raw.get("coach"), "coach"),
        decider=_build(DeciderConfig, raw.get("decider"), "decider"),
        symbol_map={str(k).upper(): str(v) for k, v in (raw.get("symbol_map") or {}).items()},
        poll_seconds=int(raw.get("poll_seconds", 30)),
        journal_path=str(raw.get("journal_path", "simplebot.sqlite")),
        reports_dir=str(raw.get("reports_dir", "reports")),
        telegram=bool(raw.get("telegram", True)),
        account_currency=str(raw.get("account_currency", "GBP")).upper(),
        path=path,
    )
    validate(cfg)
    return cfg


def validate(cfg: Config) -> None:
    if cfg.mode not in VALID_MODES:
        raise ConfigError(f"mode must be one of {VALID_MODES}, got {cfg.mode!r}")
    if cfg.broker not in VALID_BROKERS:
        raise ConfigError(f"broker must be one of {VALID_BROKERS}, got {cfg.broker!r}")
    if cfg.account not in VALID_ACCOUNTS:
        raise ConfigError(f"account must be one of {VALID_ACCOUNTS}, got {cfg.account!r}")
    if not cfg.symbols:
        raise ConfigError("symbols must list at least one symbol")
    timeframe_minutes(cfg.timeframe)
    if cfg.data.provider not in VALID_PROVIDERS:
        raise ConfigError(f"data.provider must be one of {VALID_PROVIDERS}")
    if cfg.coach.provider not in VALID_COACH_PROVIDERS:
        raise ConfigError(f"coach.provider must be one of {VALID_COACH_PROVIDERS}")
    d = cfg.decider
    if d.provider not in VALID_COACH_PROVIDERS:
        raise ConfigError(f"decider.provider must be one of {VALID_COACH_PROVIDERS}")
    if d.role not in VALID_DECIDER_ROLES:
        raise ConfigError(f"decider.role must be one of {VALID_DECIDER_ROLES}")
    if d.every_minutes < 1 or not (0 < d.max_stop_pct <= 10) or not (0 <= d.min_confidence <= 1):
        raise ConfigError(
            "decider: every_minutes >= 1, 0 < max_stop_pct <= 10, 0 <= min_confidence <= 1"
        )
    if d.effort not in ("low", "medium", "high"):
        raise ConfigError("decider.effort must be low, medium or high")
    r = cfg.risk
    if not (0 < r.risk_per_trade_pct <= 5):
        raise ConfigError("risk.risk_per_trade_pct must be between 0 and 5 (percent)")
    if not (0 < r.max_daily_loss_pct <= 20):
        raise ConfigError("risk.max_daily_loss_pct must be between 0 and 20 (percent)")
    if r.max_positions < 1:
        raise ConfigError("risk.max_positions must be >= 1")
    if r.flatten_minutes_before_close < 1:
        raise ConfigError("risk.flatten_minutes_before_close must be >= 1")
    c = cfg.costs
    if min(c.commission, c.fx_fee_pct, c.spread_bps, c.slippage_bps) < 0:
        raise ConfigError("costs must not be negative")
    if not (0.3 <= cfg.learning.train_fraction <= 0.9):
        raise ConfigError("learning.train_fraction must be between 0.3 and 0.9")
    if cfg.starting_cash <= 0:
        raise ConfigError("starting_cash must be positive")
    if cfg.mode == "live" and cfg.broker == "paper":
        raise ConfigError("mode: live needs a real broker (trading212 or ibkr), not 'paper'")


def load_config(path: str | os.PathLike = "config.yaml") -> Config:
    p = Path(path)
    if not p.exists():
        raise ConfigError(f"config file not found: {p}")
    with p.open() as f:
        raw = yaml.safe_load(f) or {}
    if not isinstance(raw, dict):
        raise ConfigError("config.yaml must be a mapping at the top level")
    return from_dict(raw, path=str(p))


def live_confirmed() -> bool:
    """Rule 1: live trading needs `mode: live` AND the env var SIMPLEBOT_LIVE_CONFIRM=yes."""
    return os.environ.get("SIMPLEBOT_LIVE_CONFIRM", "").strip().lower() == "yes"


def write_params(path: str | os.PathLike, params: dict[str, Any]) -> None:
    """Rewrite the `params:` line of config.yaml in place, keeping every other line and comment.

    Used by the learning loop when a parameter change passes the gate. Only `params:` is ever
    touched; `risk:` is never written by code.
    """
    p = Path(path)
    text = p.read_text() if p.exists() else ""
    line = "params: " + yaml.safe_dump(params, default_flow_style=True, sort_keys=True).strip()
    lines = text.splitlines()
    for i, existing in enumerate(lines):
        if re.match(r"^params\s*:", existing):
            end = i + 1  # also drop an indented block-style mapping under `params:`
            while end < len(lines) and (lines[end].strip() == "" or lines[end][0] in " \t"):
                end += 1
            while end > i + 1 and lines[end - 1].strip() == "":
                end -= 1
            lines[i:end] = [line]
            break
    else:
        lines.append(line)
    p.write_text("\n".join(lines) + "\n")
