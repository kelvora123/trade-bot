"""Fetch and cache intraday bars.

Providers:
  yahoo     keyless; 5m bars for the last 60 days, 1m bars for the last 30 days (7 days per request)
  alpaca    free Alpaca account; years of 1m/5m US bars (IEX feed); keys from .env
  csv       bring your own: data/<SYMBOL>_<tf>m.csv with timestamp,open,high,low,close,volume
  synthetic deterministic fake data for tests and demos (clearly labelled, never for decisions)

Bars are indexed by the bar START time, timezone-aware in the exchange's local zone, and
only regular-session bars are returned. The parquet cache keeps growing the longer you run
the bot, so history accumulates beyond a provider's own window.
"""

from __future__ import annotations

import hashlib
import logging
import os
import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import requests

from simplebot import sessions
from simplebot.config import Config

log = logging.getLogger(__name__)

COLUMNS = ["open", "high", "low", "close", "volume"]
PRICE_COLUMNS = ["open", "high", "low", "close"]
YAHOO_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
YAHOO_HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) simplebot/0.1"}
ALPACA_URL = "https://data.alpaca.markets/v2/stocks/{symbol}/bars"


class DataError(Exception):
    pass


def safe_name(symbol: str) -> str:
    return symbol.upper().replace(".", "_").replace("/", "_")


def empty_bars() -> pd.DataFrame:
    idx = pd.DatetimeIndex([], tz="UTC", name="ts")
    return pd.DataFrame({c: pd.Series(dtype="float64") for c in COLUMNS}, index=idx)


def merge(frames: list[pd.DataFrame]) -> pd.DataFrame:
    frames = [f for f in frames if f is not None and len(f)]
    if not frames:
        return empty_bars()
    df = pd.concat(frames)
    df = df[~df.index.duplicated(keep="last")].sort_index()
    df.index.name = "ts"
    return df[COLUMNS].astype("float64")


def regular_session_only(bars: pd.DataFrame, market: sessions.Market) -> pd.DataFrame:
    if bars.empty:
        return bars
    local = bars.tz_convert(market.tz)
    t = local.index
    minutes = t.hour * 60 + t.minute
    o = market.open.hour * 60 + market.open.minute
    c = market.close.hour * 60 + market.close.minute
    keep = (minutes >= o) & (minutes < c) & (t.dayofweek < 5)
    return local[keep]


# --------------------------------------------------------------------------- cache


def cache_path(cfg: Config, symbol: str) -> Path:
    return Path(cfg.data.cache_dir) / f"{safe_name(symbol)}_{cfg.timeframe_minutes}m.parquet"


def read_cache(path: Path) -> pd.DataFrame:
    if not path.exists():
        return empty_bars()
    try:
        df = pd.read_parquet(path)
    except Exception as e:  # corrupt cache is not fatal; we refetch
        log.warning("ignoring unreadable cache %s: %s", path, e)
        return empty_bars()
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    return merge([df.tz_convert("UTC")])


def write_cache(path: Path, bars: pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    bars.tz_convert("UTC").to_parquet(path)


# --------------------------------------------------------------------------- providers


def parse_yahoo_chart(js: dict) -> pd.DataFrame:
    chart = js.get("chart") or {}
    if chart.get("error"):
        raise DataError(f"Yahoo error: {chart['error'].get('description', chart['error'])}")
    results = chart.get("result") or []
    if not results or not results[0].get("timestamp"):
        return empty_bars()
    result = results[0]
    quote = result["indicators"]["quote"][0]
    idx = pd.to_datetime(result["timestamp"], unit="s", utc=True)
    df = pd.DataFrame({c: quote.get(c) for c in COLUMNS}, index=idx).astype("float64")
    currency = (result.get("meta") or {}).get("currency")
    if currency == "GBp":  # London quotes in pence; keep everything in pounds
        df[PRICE_COLUMNS] = df[PRICE_COLUMNS] / 100.0
    df = df.dropna(subset=["close"])
    df["volume"] = df["volume"].fillna(0.0)
    return merge([df])


def fetch_yahoo(
    symbol: str, tf_minutes: int, days: int, session: requests.Session | None = None
) -> pd.DataFrame:
    sess = session or requests.Session()
    now = int(time.time())
    windows: list[tuple[int, int]] = []
    if tf_minutes == 1:
        days = min(days, 30)
        end = now
        while end > now - days * 86400:
            start = max(end - 7 * 86400, now - days * 86400)
            windows.append((start, end))
            end = start
    else:
        days = min(days, 60)
        windows.append((now - days * 86400, now))
    frames = []
    for period1, period2 in windows:
        params = {
            "interval": f"{tf_minutes}m",
            "period1": period1,
            "period2": period2,
            "includePrePost": "false",
        }
        r = sess.get(
            YAHOO_URL.format(symbol=symbol), params=params, headers=YAHOO_HEADERS, timeout=30
        )
        if r.status_code == 429:
            raise DataError("Yahoo rate-limited us (HTTP 429); wait a minute and retry")
        r.raise_for_status()
        frames.append(parse_yahoo_chart(r.json()))
    return merge(frames)


def parse_alpaca_bars(js: dict) -> pd.DataFrame:
    rows = js.get("bars") or []
    if not rows:
        return empty_bars()
    df = pd.DataFrame(
        {
            "open": [b["o"] for b in rows],
            "high": [b["h"] for b in rows],
            "low": [b["l"] for b in rows],
            "close": [b["c"] for b in rows],
            "volume": [b["v"] for b in rows],
        },
        index=pd.to_datetime([b["t"] for b in rows], utc=True),
    )
    return merge([df])


def fetch_alpaca(
    symbol: str, tf_minutes: int, days: int, session: requests.Session | None = None
) -> pd.DataFrame:
    key, secret = os.environ.get("ALPACA_API_KEY"), os.environ.get("ALPACA_API_SECRET")
    if not key or not secret:
        raise DataError("data.provider is alpaca but ALPACA_API_KEY/ALPACA_API_SECRET are not set")
    sess = session or requests.Session()
    headers = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}
    start = (datetime.now(UTC) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    params: dict = {
        "timeframe": f"{tf_minutes}Min",
        "start": start,
        "limit": 10000,
        "feed": "iex",
        "adjustment": "raw",
    }
    frames = []
    for _ in range(200):  # hard cap on pages
        r = sess.get(ALPACA_URL.format(symbol=symbol), params=params, headers=headers, timeout=30)
        if r.status_code == 429:
            time.sleep(5)
            continue
        r.raise_for_status()
        js = r.json()
        frames.append(parse_alpaca_bars(js))
        token = js.get("next_page_token")
        if not token:
            break
        params["page_token"] = token
    return merge(frames)


def load_csv(path: Path, market: sessions.Market) -> pd.DataFrame:
    if not path.exists():
        raise DataError(f"csv file not found: {path}")
    df = pd.read_csv(path)
    df.columns = [c.strip().lower() for c in df.columns]
    candidates = ("timestamp", "ts", "datetime", "date", "time")
    ts_col = next((c for c in candidates if c in df.columns), None)
    if ts_col is None or any(c not in df.columns for c in COLUMNS):
        raise DataError(f"{path} needs columns timestamp,open,high,low,close,volume")
    idx = pd.to_datetime(df[ts_col], utc=False)
    if idx.dt.tz is None:
        idx = idx.dt.tz_localize(market.tz)
    df.index = pd.DatetimeIndex(idx).tz_convert("UTC")
    return merge([df[COLUMNS].astype("float64")])


def stable_seed(*parts) -> int:
    h = hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()
    return int(h[:8], 16)


def synthetic_bars(
    symbol: str,
    tf_minutes: int,
    days: int,
    now: datetime | None = None,
    seed: int | None = 0,
) -> pd.DataFrame:
    """Deterministic fake intraday bars: random walk with mild autocorrelation and a U-shaped
    volume profile. Good enough to exercise every code path; never a statement about markets."""
    market = sessions.market_for(symbol)
    n_bars = max(1, market.session_minutes // tf_minutes)
    rng = np.random.default_rng(stable_seed(symbol, seed))
    end_day = (now or datetime.now(UTC)).astimezone(market.zone).date()
    session_days: list[date] = []
    d = end_day
    while len(session_days) < days:
        if d.weekday() < 5:
            session_days.append(d)
        d -= timedelta(days=1)
    session_days.reverse()

    price = 50.0 + (stable_seed(symbol) % 400)
    daily_vol = 0.012
    bar_vol = daily_vol / np.sqrt(n_bars)
    idx: list[datetime] = []
    o_, h_, l_, c_, v_ = [], [], [], [], []
    for day in session_days:
        price *= float(np.exp(rng.normal(0, 0.004)))  # overnight gap
        prev_r = 0.0
        day_drift = rng.normal(0, bar_vol * 0.3)
        t0 = sessions.session_open(market, day)
        for i in range(n_bars):
            r = day_drift + 0.08 * prev_r + rng.normal(0, bar_vol)
            prev_r = r
            open_ = price
            close = open_ * float(np.exp(r))
            wick = abs(rng.normal(0, bar_vol * 0.6))
            high = max(open_, close) * (1 + wick)
            low = min(open_, close) * (1 - wick)
            u = (i / max(1, n_bars - 1) - 0.5) ** 2
            vol = float(rng.lognormal(mean=np.log(20_000 * (0.5 + 3 * u)), sigma=0.3))
            idx.append(t0 + timedelta(minutes=i * tf_minutes))
            o_.append(open_)
            h_.append(high)
            l_.append(low)
            c_.append(close)
            v_.append(vol)
            price = close
    df = pd.DataFrame(
        {"open": o_, "high": h_, "low": l_, "close": c_, "volume": v_},
        index=pd.DatetimeIndex(idx, name="ts"),
    )
    return df.tz_convert("UTC")


# --------------------------------------------------------------------------- entry point


def get_bars(
    cfg: Config,
    symbol: str,
    provider: str | None = None,
    lookback_days: int | None = None,
    session: requests.Session | None = None,
    now: datetime | None = None,
) -> pd.DataFrame:
    """Return regular-session bars for `symbol`, newest last, in the exchange's timezone."""
    provider = provider or cfg.data.provider
    tf = cfg.timeframe_minutes
    days = lookback_days or cfg.data.lookback_days
    market = sessions.market_for(symbol)

    if provider == "synthetic":
        bars = synthetic_bars(symbol, tf, days, now=now)
    elif provider == "csv":
        bars = load_csv(Path(cfg.data.csv_dir) / f"{safe_name(symbol)}_{tf}m.csv", market)
    elif provider in ("yahoo", "alpaca"):
        path = cache_path(cfg, symbol)
        cached = read_cache(path)
        fetch = fetch_yahoo if provider == "yahoo" else fetch_alpaca
        try:
            fresh = fetch(symbol, tf, days, session=session)
        except (requests.RequestException, DataError) as e:
            if cached.empty:
                raise DataError(
                    f"could not fetch {symbol} from {provider} and no cache exists: {e}. "
                    "Try `--data synthetic` for a demo, or put a CSV in data/."
                ) from e
            log.warning("fetch failed for %s (%s); using cached bars only", symbol, e)
            fresh = empty_bars()
        bars = merge([cached, fresh])
        if not fresh.empty:
            write_cache(path, bars)
    else:
        raise DataError(f"unknown data provider {provider!r}")

    bars = regular_session_only(bars, market)
    bars.attrs.update({"symbol": symbol, "market": market.name, "timeframe": tf})
    return bars
