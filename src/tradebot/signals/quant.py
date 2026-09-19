"""Deterministic replacement for AutoHedge's Quant-Analyst LLM agent.

That agent is prompted to return "technical_score (0-1), volume_score (0-1),
trend_strength (0-1), volatility, probability_score (0-1), key_levels (support,
resistance, pivot)" from a language model. Every one of those is computable
from the price series, so this module computes them.

Two things are deliberately *not* reproduced:

* **There is no probability score.** A weighted blend of indicators is an
  ordinal score, not a probability, and calling it one invites position sizing
  against a number that was never calibrated. This emits ``conviction`` and
  says plainly what it is. Where enough history exists, ``hit_rate`` reports
  the measured base rate of the same setup instead -- an actual frequency.
* **No recommendation.** This scores conditions; sizing is the risk layer's
  job and the decision is the operator's.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from . import indicators as ind

__all__ = ["QuantRead", "analyse", "MIN_BARS"]

MIN_BARS = 60  # below this the longer windows have no value to report


def _last(series: Sequence[ind.Num]) -> float | None:
    for v in reversed(series):
        if v is not None:
            return v
    return None


def _scale(value: float, lo: float, hi: float) -> float:
    """Map a value onto 0-1, clamped. ``lo`` may exceed ``hi`` to invert."""
    span = hi - lo
    if abs(span) < 1e-12:
        return 0.5
    return max(0.0, min(1.0, (value - lo) / span))


@dataclass
class QuantRead:
    ticker: str
    asof: str
    bars: int
    close: float | None = None
    trend_score: float | None = None
    momentum_score: float | None = None
    volume_score: float | None = None
    volatility_annualised: float | None = None
    atr: float | None = None
    atr_pct: float | None = None
    rsi: float | None = None
    conviction: float | None = None
    direction: str = "flat"
    hit_rate: float | None = None
    hit_rate_samples: int = 0
    levels: dict[str, float | None] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        def r(x: float | None, d: int = 4) -> float | None:
            return None if x is None else round(x, d)

        return {
            "ticker": self.ticker,
            "asof": self.asof,
            "bars_used": self.bars,
            "close": r(self.close, 4),
            "direction": self.direction,
            "scores": {
                "trend": r(self.trend_score, 3),
                "momentum": r(self.momentum_score, 3),
                "volume": r(self.volume_score, 3),
                "conviction": r(self.conviction, 3),
            },
            "volatility": {
                "annualised_pct": None if self.volatility_annualised is None
                else round(self.volatility_annualised * 100, 2),
                "atr": r(self.atr, 4),
                "atr_pct_of_price": None if self.atr_pct is None else round(self.atr_pct * 100, 2),
            },
            "rsi": r(self.rsi, 2),
            "key_levels": {k: r(v, 4) for k, v in self.levels.items()},
            "measured_hit_rate": {
                "value_pct": None if self.hit_rate is None else round(self.hit_rate * 100, 1),
                "samples": self.hit_rate_samples,
            },
            "conviction_note": (
                "Conviction is a weighted blend of trend, momentum and volume scores on a "
                "0-1 ordinal scale. It is NOT a probability and must not be treated as one. "
                "measured_hit_rate, where present, is an observed frequency from this "
                "series' own history."
            ),
            "notes": self.notes,
        }


def _measured_hit_rate(closes: list[float], long_bias: bool, horizon: int = 10
                       ) -> tuple[float | None, int]:
    """Historical frequency that the trend filter was followed by a gain.

    Deliberately crude and deliberately honest: it walks this series' own
    history, checks where the 20/50 EMA relationship matched today's, and
    counts how often price was higher ``horizon`` bars later. It is a base
    rate on a small sample, not a forecast -- which is exactly why it is
    reported alongside its sample count.
    """
    if len(closes) < 80:
        return None, 0
    fast, slow = ind.ema(closes, 20), ind.ema(closes, 50)
    wins = total = 0
    for i in range(50, len(closes) - horizon):
        if fast[i] is None or slow[i] is None:
            continue
        if (fast[i] > slow[i]) != long_bias:
            continue
        total += 1
        fwd = closes[i + horizon] / closes[i] - 1.0
        if (fwd > 0) == long_bias:
            wins += 1
    return (wins / total, total) if total >= 10 else (None, total)


def analyse(ticker: str, bars: list[dict], asof: str) -> QuantRead:
    """Score price conditions for one underlying. No network, no LLM."""
    read = QuantRead(ticker=ticker, asof=asof, bars=len(bars))
    if len(bars) < 30:
        read.notes.append(
            f"Only {len(bars)} bars of history; need at least 30 to score anything "
            f"and {MIN_BARS} for the full read."
        )
        return read

    closes = [float(b["close"]) for b in bars]
    highs = [float(b["high"]) for b in bars]
    lows = [float(b["low"]) for b in bars]
    vols = [float(b["volume"]) for b in bars if b.get("volume") is not None]
    read.close = closes[-1]

    if len(bars) < MIN_BARS:
        read.notes.append(
            f"{len(bars)} bars available; some windows are shortened below {MIN_BARS}."
        )

    # --- trend: EMA stack plus normalised slope ---------------------------
    fast, slow = _last(ind.ema(closes, 20)), _last(ind.ema(closes, 50))
    sl = _last(ind.slope(closes, 20))
    if fast is not None and slow is not None and slow > 0:
        sep = (fast - slow) / slow                      # EMA separation
        stack = _scale(sep, -0.06, 0.06)
        tilt = _scale(sl or 0.0, -0.004, 0.004)
        read.trend_score = 0.6 * stack + 0.4 * tilt
        read.direction = "long" if fast > slow else "short"

    # --- momentum: RSI distance from 50, plus rate of change --------------
    r = _last(ind.rsi(closes, 14))
    read.rsi = r
    change = _last(ind.roc(closes, 20))
    if r is not None:
        mom_rsi = _scale(r, 30.0, 70.0)
        mom_roc = _scale(change or 0.0, -0.12, 0.12)
        read.momentum_score = 0.5 * mom_rsi + 0.5 * mom_roc

    # --- volume: today's participation against its own 20-day average -----
    if len(vols) >= 21:
        avg = sum(vols[-21:-1]) / 20.0
        if avg > 0:
            read.volume_score = _scale(vols[-1] / avg, 0.4, 2.0)
    else:
        read.notes.append("No usable volume history; volume score omitted.")

    # --- volatility -------------------------------------------------------
    read.volatility_annualised = _last(ind.realised_vol(closes, 20))
    read.atr = _last(ind.atr(highs, lows, closes, 14))
    if read.atr and read.close:
        read.atr_pct = read.atr / read.close

    # --- key levels -------------------------------------------------------
    lower, mid, upper = ind.bollinger(closes, 20, 2.0)
    dn, up = ind.donchian(highs, lows, 20)
    pivot = (highs[-1] + lows[-1] + closes[-1]) / 3.0
    read.levels = {
        "support": _last(dn),
        "resistance": _last(up),
        "pivot": pivot,
        "bollinger_lower": _last(lower),
        "bollinger_mid": _last(mid),
        "bollinger_upper": _last(upper),
        # Classic floor-trader levels, derived from the pivot.
        "r1": 2 * pivot - lows[-1],
        "s1": 2 * pivot - highs[-1],
    }

    # --- conviction: an ordinal blend, explicitly not a probability -------
    parts = [(read.trend_score, 0.45), (read.momentum_score, 0.35), (read.volume_score, 0.20)]
    present = [(v, w) for v, w in parts if v is not None]
    if present:
        total_w = sum(w for _, w in present)
        blended = sum(v * w for v, w in present) / total_w
        # Re-centre on direction: a short setup scores on the downside.
        read.conviction = blended if read.direction != "short" else 1.0 - blended

    read.hit_rate, read.hit_rate_samples = _measured_hit_rate(closes, read.direction == "long")
    if read.hit_rate is None and read.hit_rate_samples:
        read.notes.append(
            f"Only {read.hit_rate_samples} historical instances of this trend state; "
            "too few for a base rate (10 minimum)."
        )
    return read
