"""Pure indicator math.

Every function is deterministic, dependency-light and side-effect free. This is
the layer that replaces AutoHedge's Quant-Analyst LLM agent: an RSI is a
recurrence relation over closes, not something to ask a language model for.

Convention: input is a sequence oldest-first; output is a list of the same
length with ``None`` where there is not yet enough history. Nothing looks
ahead -- the value at index ``i`` uses only data at ``<= i``.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

__all__ = [
    "sma", "ema", "rsi", "true_range", "atr", "bollinger", "donchian",
    "realised_vol", "roc", "slope", "zscore", "max_drawdown", "percentile",
]

Num = float | None


def _clean(values: Sequence[float]) -> list[float]:
    out = [float(v) for v in values]
    if any(math.isnan(v) or math.isinf(v) for v in out):
        raise ValueError("series contains NaN or infinity")
    return out


def sma(values: Sequence[float], window: int) -> list[Num]:
    if window < 1:
        raise ValueError(f"window must be >= 1, got {window}")
    v = _clean(values)
    out: list[Num] = [None] * len(v)
    running = 0.0
    for i, x in enumerate(v):
        running += x
        if i >= window:
            running -= v[i - window]
        if i >= window - 1:
            out[i] = running / window
    return out


def ema(values: Sequence[float], span: int) -> list[Num]:
    """Exponential moving average, seeded with the first full SMA."""
    if span < 1:
        raise ValueError(f"span must be >= 1, got {span}")
    v = _clean(values)
    out: list[Num] = [None] * len(v)
    if len(v) < span:
        return out
    alpha = 2.0 / (span + 1.0)
    prev = sum(v[:span]) / span
    out[span - 1] = prev
    for i in range(span, len(v)):
        prev = alpha * v[i] + (1 - alpha) * prev
        out[i] = prev
    return out


def rsi(closes: Sequence[float], window: int = 14) -> list[Num]:
    """Wilder's RSI in [0, 100]."""
    c = _clean(closes)
    out: list[Num] = [None] * len(c)
    if len(c) <= window:
        return out

    gains = losses = 0.0
    for i in range(1, window + 1):
        d = c[i] - c[i - 1]
        gains += max(d, 0.0)
        losses += max(-d, 0.0)
    avg_gain, avg_loss = gains / window, losses / window

    def _rsi(g: float, loss: float) -> float:
        if loss <= 1e-12:
            return 100.0 if g > 1e-12 else 50.0
        return 100.0 - 100.0 / (1.0 + g / loss)

    out[window] = _rsi(avg_gain, avg_loss)
    for i in range(window + 1, len(c)):
        d = c[i] - c[i - 1]
        avg_gain = (avg_gain * (window - 1) + max(d, 0.0)) / window
        avg_loss = (avg_loss * (window - 1) + max(-d, 0.0)) / window
        out[i] = _rsi(avg_gain, avg_loss)
    return out


def true_range(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float]) -> list[Num]:
    h, low_s, c = _clean(highs), _clean(lows), _clean(closes)
    if not (len(h) == len(low_s) == len(c)):
        raise ValueError("highs, lows and closes must be the same length")
    out: list[Num] = [None] * len(c)
    for i in range(1, len(c)):
        out[i] = max(h[i] - low_s[i], abs(h[i] - c[i - 1]), abs(low_s[i] - c[i - 1]))
    return out


def atr(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float],
        window: int = 14) -> list[Num]:
    """Wilder-smoothed average true range -- the volatility unit stops are set in."""
    tr = true_range(highs, lows, closes)
    out: list[Num] = [None] * len(tr)
    vals = [t for t in tr[1:] if t is not None]
    if len(vals) < window:
        return out
    prev = sum(vals[:window]) / window
    out[window] = prev
    for i in range(window + 1, len(tr)):
        t = tr[i]
        if t is None:
            continue
        prev = (prev * (window - 1) + t) / window
        out[i] = prev
    return out


def bollinger(closes: Sequence[float], window: int = 20, num_sd: float = 2.0
              ) -> tuple[list[Num], list[Num], list[Num]]:
    """(lower, middle, upper) bands. Middle is the SMA."""
    c = _clean(closes)
    mid = sma(c, window)
    lower: list[Num] = [None] * len(c)
    upper: list[Num] = [None] * len(c)
    for i in range(window - 1, len(c)):
        m = mid[i]
        if m is None:
            continue
        var = sum((x - m) ** 2 for x in c[i - window + 1: i + 1]) / window
        sd = math.sqrt(var)
        lower[i], upper[i] = m - num_sd * sd, m + num_sd * sd
    return lower, mid, upper


def donchian(highs: Sequence[float], lows: Sequence[float], window: int
             ) -> tuple[list[Num], list[Num]]:
    """Channel over the *prior* ``window`` bars, excluding the current one.

    Excluding the current bar is what makes a breakout test honest: at bar i we
    ask whether price exceeded the range of the bars before it.
    """
    h, low_s = _clean(highs), _clean(lows)
    up: list[Num] = [None] * len(h)
    dn: list[Num] = [None] * len(h)
    for i in range(window, len(h)):
        up[i] = max(h[i - window: i])
        dn[i] = min(low_s[i - window: i])
    return dn, up


def realised_vol(closes: Sequence[float], window: int = 20,
                 periods_per_year: int = 252) -> list[Num]:
    """Annualised stdev of log returns, as a decimal (0.45 == 45%)."""
    c = _clean(closes)
    out: list[Num] = [None] * len(c)
    if any(x <= 0 for x in c):
        return out
    rets = [math.log(c[i] / c[i - 1]) for i in range(1, len(c))]
    for i in range(window, len(c)):
        w = rets[i - window: i]
        m = sum(w) / len(w)
        var = sum((r - m) ** 2 for r in w) / (len(w) - 1) if len(w) > 1 else 0.0
        out[i] = math.sqrt(var) * math.sqrt(periods_per_year)
    return out


def roc(values: Sequence[float], window: int) -> list[Num]:
    """Rate of change over ``window`` bars, as a fraction."""
    v = _clean(values)
    out: list[Num] = [None] * len(v)
    for i in range(window, len(v)):
        base = v[i - window]
        if abs(base) > 1e-12:
            out[i] = v[i] / base - 1.0
    return out


def slope(values: Sequence[float], window: int) -> list[Num]:
    """Least-squares slope per bar, normalised by level so it compares across
    a $0.40 token and a $90,000 one."""
    v = _clean(values)
    out: list[Num] = [None] * len(v)
    xs = list(range(window))
    xbar = sum(xs) / window
    denom = sum((x - xbar) ** 2 for x in xs)
    if denom <= 0:
        return out
    for i in range(window - 1, len(v)):
        w = v[i - window + 1: i + 1]
        ybar = sum(w) / window
        num = sum((xs[k] - xbar) * (w[k] - ybar) for k in range(window))
        level = abs(v[i])
        if level > 1e-12:
            out[i] = (num / denom) / level
    return out


def zscore(values: Sequence[float], window: int) -> list[Num]:
    v = _clean(values)
    out: list[Num] = [None] * len(v)
    for i in range(window - 1, len(v)):
        w = v[i - window + 1: i + 1]
        m = sum(w) / window
        var = sum((x - m) ** 2 for x in w) / (window - 1) if window > 1 else 0.0
        sd = math.sqrt(var)
        if sd > 1e-12:
            out[i] = (v[i] - m) / sd
    return out


def max_drawdown(values: Sequence[float]) -> float:
    """Deepest peak-to-trough decline as a positive fraction."""
    v = _clean(values)
    if len(v) < 2:
        return 0.0
    peak, worst = v[0], 0.0
    for x in v:
        peak = max(peak, x)
        if peak > 0:
            worst = max(worst, (peak - x) / peak)
    return worst


def percentile(values: Sequence[float], q: float) -> float | None:
    """Linear-interpolated percentile. ``q`` in [0, 100]."""
    v = sorted(_clean(values))
    if not v:
        return None
    if len(v) == 1:
        return v[0]
    pos = (len(v) - 1) * max(0.0, min(100.0, q)) / 100.0
    lo = int(math.floor(pos))
    hi = min(lo + 1, len(v) - 1)
    return v[lo] + (v[hi] - v[lo]) * (pos - lo)
