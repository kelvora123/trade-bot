"""Layer 3a: the deterministic macro gate.

A 0-100 read on the general environment the book sits in, blended from four
configurable, weighted components. No LLM touches this: same data in, same
score out, which is the property that makes it auditable and cheap.

Orientation: **100 is calm / supportive, 0 is stressed.** Every component is
normalised to that direction before weighting so the blend is meaningful.

The gate describes the weather. It does not size positions or issue signals.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from typing import Any

from ..config import Config

log = logging.getLogger(__name__)

__all__ = ["compute_macro_gate", "MacroGate", "linear_score", "percentile_of"]

# Reference symbols. VIX3M is the 3-month vol index; the VIX/VIX3M ratio is the
# standard term-structure tell (below 1 = contango = calm).
VIX = "^VIX"
VIX3M = "^VIX3M"
SPY = "SPY"
RSP = "RSP"  # equal-weight S&P, used as the breadth proxy
HYG = "HYG"  # high-yield credit
TLT = "TLT"  # long Treasuries


def linear_score(value: float, at_zero: float, at_hundred: float) -> float:
    """Map a value onto 0-100 linearly, clamped at both ends.

    ``at_zero`` may be greater than ``at_hundred`` (an inverted mapping, e.g.
    high VIX scoring low), which is why this cannot just be a min/max ratio.
    """
    span = at_hundred - at_zero
    if abs(span) < 1e-12:
        return 50.0
    return max(0.0, min(100.0, (value - at_zero) / span * 100.0))


def percentile_of(value: float, series: list[float]) -> float | None:
    """Percentage of ``series`` strictly below ``value``."""
    if not series:
        return None
    return sum(1 for v in series if v < value) / len(series) * 100.0


@dataclass
class MacroGate:
    asof: date
    score: float
    regime: str
    components: dict[str, Any]
    missing: list[str]

    def to_json(self) -> dict[str, Any]:
        return {
            "layer": "3a_macro_gate",
            "asof": self.asof.isoformat(),
            "score": round(self.score, 1),
            "regime": self.regime,
            "scale": "0 = stressed, 100 = calm. Deterministic: same data in, same score out.",
            "components": self.components,
            "missing_inputs": self.missing,
            "disclaimer": "Describes the market environment. Not a trade signal.",
        }


def _regime(score: float) -> str:
    if score >= 75:
        return "calm"
    if score >= 55:
        return "constructive"
    if score >= 40:
        return "mixed"
    if score >= 25:
        return "cautious"
    return "stressed"


def _last(series: list[tuple[date, float]]) -> float | None:
    return series[-1][1] if series else None


def _ratio_series(a: list[tuple[date, float]], b: list[tuple[date, float]]) -> list[float]:
    """Aligned a/b ratio series, matched on date so gaps cannot skew it."""
    bm = dict(b)
    return [av / bm[d] for d, av in a if d in bm and bm[d] not in (0, None)]


def compute_macro_gate(provider: Any, cfg: Config, asof: date) -> MacroGate:
    """Blend the four weighted components into a single 0-100 score.

    Components whose inputs are unavailable are dropped and the remaining
    weights are renormalised, so a missing feed degrades the score's precision
    rather than silently biasing it toward 50.
    """
    w = cfg.macro.weights
    lookback = cfg.macro.vix_percentile_lookback_days
    components: dict[str, Any] = {}
    missing: list[str] = []

    def fetch(sym: str) -> list[tuple[date, float]]:
        try:
            return provider.history_closes(sym, lookback)
        except Exception as exc:
            log.warning("macro: history fetch failed for %s: %s", sym, exc)
            return []

    vix_hist = fetch(VIX)
    vix = _last(vix_hist)

    # 1. VIX level -- 12 is a calm tape, 40 is a panic.
    if vix is not None:
        components["vix_level"] = {
            "value": round(vix, 2),
            "score": round(linear_score(vix, at_zero=40.0, at_hundred=12.0), 1),
            "weight": w.vix_level,
        }
    else:
        missing.append("vix_level")

    # 2. VIX 1-year percentile -- where today's vol sits in its own year.
    vix_series = [v for _, v in vix_hist]
    if vix is not None and len(vix_series) >= 30:
        pct = percentile_of(vix, vix_series) or 0.0
        components["vix_percentile"] = {
            "value": round(pct, 1),
            "score": round(100.0 - pct, 1),  # high percentile = stressed
            "weight": w.vix_percentile,
            "history_days": len(vix_series),
        }
    else:
        missing.append("vix_percentile")

    # 3. Term structure -- VIX below VIX3M (contango) is the normal, calm state.
    vix3m = _last(fetch(VIX3M))
    if vix is not None and vix3m and vix3m > 0:
        ratio = vix / vix3m
        components["vix_term_structure"] = {
            "value": round(ratio, 4),
            "state": "backwardation (stress)" if ratio > 1.0 else "contango (normal)",
            "score": round(linear_score(ratio, at_zero=1.15, at_hundred=0.85), 1),
            "weight": w.vix_term_structure,
        }
    else:
        missing.append("vix_term_structure")

    # 4. Breadth -- equal-weight vs cap-weight as the proxy. A falling RSP/SPY
    #    ratio means the index is being carried by a handful of megacaps, which
    #    is the same thing a low "percent above 200dma" reading tells you.
    rsp, spy = fetch(RSP), fetch(SPY)
    ratios = _ratio_series(rsp, spy)
    if len(ratios) >= 30:
        pct = percentile_of(ratios[-1], ratios) or 0.0
        components["breadth"] = {
            "proxy": cfg.macro.breadth_proxy,
            "value": round(ratios[-1], 5),
            "percentile": round(pct, 1),
            "score": round(pct, 1),  # broad participation = supportive
            "weight": w.breadth,
            "history_days": len(ratios),
        }
    else:
        missing.append("breadth")

    # 5. Credit -- HYG/TLT. Credit leads equities; a falling ratio is risk-off.
    hyg, tlt = fetch(HYG), fetch(TLT)
    cr = _ratio_series(hyg, tlt)
    if len(cr) >= 30:
        pct = percentile_of(cr[-1], cr) or 0.0
        components["credit_spread"] = {
            "pair": cfg.macro.credit_spread_pair,
            "value": round(cr[-1], 5),
            "percentile": round(pct, 1),
            "score": round(pct, 1),
            "weight": w.credit_spread,
            "history_days": len(cr),
        }
    else:
        missing.append("credit_spread")

    weights = w.as_dict()
    available = sum(weights[k] for k in components)
    if available < 1e-9:
        return MacroGate(asof, 50.0, "unknown", components, missing)

    # Renormalise across whatever survived so the blend still spans 0-100.
    score = sum(components[k]["score"] * weights[k] for k in components) / available
    if missing:
        log.warning("macro gate computed without: %s", ", ".join(missing))
    return MacroGate(asof, score, _regime(score), components, missing)
