"""Black-Scholes-Merton pricing and Greeks, computed locally.

Deliberately stdlib-only (``math``) -- no scipy, no QuantLib. The normal CDF
comes from ``math.erf``, which is accurate to full double precision, so there
is nothing to gain from a dependency here.

Conventions, chosen to match how the analytics layer reports them:
  * ``t`` is time to expiry in YEARS (actual days / 365).
  * ``sigma`` is annualised implied vol as a decimal (0.45 == 45%).
  * ``r`` is the annual risk-free rate as a decimal (0.045 default).
  * ``q`` is the continuous dividend yield.
  * ``delta`` and ``gamma`` are per 1 share.
  * ``vega`` is returned per **1 IV point** (i.e. already divided by 100).
  * ``theta`` is returned per **calendar day** (i.e. already divided by 365).

Those last two are the units a human reads on a position blotter, and doing the
scaling once here stops it being done twice (or not at all) further up.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

__all__ = ["Greeks", "norm_cdf", "norm_pdf", "price", "greeks", "implied_vol"]

_SQRT_2 = math.sqrt(2.0)
_SQRT_2PI = math.sqrt(2.0 * math.pi)

# Below these, the contract is treated as degenerate (expired / no vol) and we
# fall back to intrinsic value with step-function delta.
_MIN_T = 1e-9
_MIN_SIGMA = 1e-9


@dataclass(frozen=True)
class Greeks:
    """Per-share Greeks in report-ready units."""

    price: float
    delta: float
    gamma: float
    theta: float  # $ per calendar day
    vega: float  # $ per 1 IV point
    rho: float  # $ per 1 rate point

    def scaled(self, contracts: float, multiplier: int = 100) -> Greeks:
        """Scale to a position of ``contracts`` contracts (100 shares each)."""
        k = contracts * multiplier
        return Greeks(
            price=self.price * k,
            delta=self.delta * k,
            gamma=self.gamma * k,
            theta=self.theta * k,
            vega=self.vega * k,
            rho=self.rho * k,
        )


def norm_cdf(x: float) -> float:
    """Standard normal CDF via the error function."""
    return 0.5 * (1.0 + math.erf(x / _SQRT_2))


def norm_pdf(x: float) -> float:
    """Standard normal PDF."""
    return math.exp(-0.5 * x * x) / _SQRT_2PI


def _d1_d2(s: float, k: float, t: float, r: float, sigma: float, q: float) -> tuple[float, float]:
    vol_t = sigma * math.sqrt(t)
    d1 = (math.log(s / k) + (r - q + 0.5 * sigma * sigma) * t) / vol_t
    return d1, d1 - vol_t


def _validate(s: float, k: float, sigma: float, kind: str) -> str:
    kind = kind.lower().strip()
    if kind in ("c", "call"):
        kind = "call"
    elif kind in ("p", "put"):
        kind = "put"
    else:
        raise ValueError(f"kind must be 'call' or 'put', got {kind!r}")
    if s <= 0.0:
        raise ValueError(f"spot must be > 0, got {s}")
    if k <= 0.0:
        raise ValueError(f"strike must be > 0, got {k}")
    if sigma < 0.0:
        raise ValueError(f"sigma must be >= 0, got {sigma}")
    return kind


def price(s: float, k: float, t: float, r: float, sigma: float, kind: str, q: float = 0.0) -> float:
    """Black-Scholes-Merton option price."""
    kind = _validate(s, k, sigma, kind)
    if t <= _MIN_T or sigma <= _MIN_SIGMA:
        intrinsic = (s - k) if kind == "call" else (k - s)
        return max(intrinsic, 0.0)

    d1, d2 = _d1_d2(s, k, t, r, sigma, q)
    disc_r = math.exp(-r * t)
    disc_q = math.exp(-q * t)
    if kind == "call":
        return s * disc_q * norm_cdf(d1) - k * disc_r * norm_cdf(d2)
    return k * disc_r * norm_cdf(-d2) - s * disc_q * norm_cdf(-d1)


def greeks(s: float, k: float, t: float, r: float, sigma: float, kind: str, q: float = 0.0) -> Greeks:
    """Full Greek set for one share of the underlying contract."""
    kind = _validate(s, k, sigma, kind)

    if t <= _MIN_T or sigma <= _MIN_SIGMA:
        # Expired or vol-less: intrinsic value, step delta, everything else dead.
        itm = (s > k) if kind == "call" else (s < k)
        intrinsic = max((s - k) if kind == "call" else (k - s), 0.0)
        delta = (1.0 if kind == "call" else -1.0) if itm else 0.0
        return Greeks(price=intrinsic, delta=delta, gamma=0.0, theta=0.0, vega=0.0, rho=0.0)

    d1, d2 = _d1_d2(s, k, t, r, sigma, q)
    sqrt_t = math.sqrt(t)
    disc_r = math.exp(-r * t)
    disc_q = math.exp(-q * t)
    pdf_d1 = norm_pdf(d1)

    gamma = disc_q * pdf_d1 / (s * sigma * sqrt_t)
    vega_annual = s * disc_q * pdf_d1 * sqrt_t
    common_theta = -(s * disc_q * pdf_d1 * sigma) / (2.0 * sqrt_t)

    if kind == "call":
        delta = disc_q * norm_cdf(d1)
        theta_annual = common_theta - r * k * disc_r * norm_cdf(d2) + q * s * disc_q * norm_cdf(d1)
        rho_annual = k * t * disc_r * norm_cdf(d2)
    else:
        delta = disc_q * (norm_cdf(d1) - 1.0)
        theta_annual = common_theta + r * k * disc_r * norm_cdf(-d2) - q * s * disc_q * norm_cdf(-d1)
        rho_annual = -k * t * disc_r * norm_cdf(-d2)

    return Greeks(
        price=price(s, k, t, r, sigma, kind, q),
        delta=delta,
        gamma=gamma,
        theta=theta_annual / 365.0,  # per calendar day
        vega=vega_annual / 100.0,  # per 1 IV point
        rho=rho_annual / 100.0,  # per 1 rate point
    )


def implied_vol(
    target_price: float,
    s: float,
    k: float,
    t: float,
    r: float,
    kind: str,
    q: float = 0.0,
    *,
    lo: float = 1e-4,
    hi: float = 5.0,
    tol: float = 1e-6,
    max_iter: int = 100,
) -> float | None:
    """Solve for implied vol by bisection. Returns ``None`` if unsolvable.

    Used only as a *fallback* when a chain row is missing IV or reports an
    obviously broken one. Bisection rather than Newton because vega collapses
    for deep ITM/OTM contracts and Newton diverges exactly where the chain data
    is worst; bisection is slower but cannot run away.
    """
    kind = _validate(s, k, 0.0, kind)
    if t <= _MIN_T or target_price <= 0.0:
        return None

    intrinsic = max((s - k) if kind == "call" else (k - s), 0.0)
    if target_price < intrinsic - tol:
        return None  # below intrinsic: arbitrage or bad quote, not a vol

    f_lo = price(s, k, t, r, lo, kind, q) - target_price
    f_hi = price(s, k, t, r, hi, kind, q) - target_price
    if f_lo * f_hi > 0.0:
        return None  # target outside the bracket

    for _ in range(max_iter):
        mid = 0.5 * (lo + hi)
        f_mid = price(s, k, t, r, mid, kind, q) - target_price
        if abs(f_mid) < tol or (hi - lo) < tol:
            return mid
        if f_lo * f_mid < 0.0:
            hi = mid
        else:
            lo, f_lo = mid, f_mid
    return 0.5 * (lo + hi)
