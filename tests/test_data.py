import pandas as pd
import pytest

from simplebot import data
from simplebot.data import DataError, get_bars, parse_yahoo_chart, synthetic_bars
from tests.conftest import NOW


def yahoo_json(currency="USD"):
    return {
        "chart": {
            "result": [
                {
                    "meta": {"currency": currency},
                    "timestamp": [1760000000, 1760000300, 1760000600],
                    "indicators": {
                        "quote": [
                            {
                                "open": [1.0, 2.0, None],
                                "high": [1.5, 2.5, None],
                                "low": [0.5, 1.5, None],
                                "close": [1.2, 2.2, None],
                                "volume": [10, None, None],
                            }
                        ]
                    },
                }
            ],
            "error": None,
        }
    }


def test_parse_yahoo_chart_drops_nan_and_handles_pence():
    df = parse_yahoo_chart(yahoo_json())
    assert len(df) == 2 and df.index.tz is not None and df["volume"].iloc[1] == 0.0
    gb = parse_yahoo_chart(yahoo_json("GBp"))
    assert abs(gb["close"].iloc[0] - 0.012) < 1e-12
    with pytest.raises(DataError):
        parse_yahoo_chart({"chart": {"result": None, "error": {"description": "nope"}}})


def test_synthetic_bars_regular_session_only(cfg):
    bars = get_bars(cfg, "QQQ", provider="synthetic", lookback_days=10, now=NOW)
    assert bars.attrs["symbol"] == "QQQ" and bars.attrs["timeframe"] == 5
    assert len(set(bars.index.date)) == 10
    assert (bars.index.hour * 60 + bars.index.minute).min() == 9 * 60 + 30
    assert (bars.index.hour * 60 + bars.index.minute).max() == 15 * 60 + 55
    assert (bars["high"] >= bars[["open", "close"]].max(axis=1)).all()
    same = synthetic_bars("QQQ", 5, 10, now=NOW)
    assert same["close"].iloc[-1] == bars["close"].iloc[-1]  # deterministic


def test_cache_merge_and_fallback(cfg, monkeypatch):
    calls = []

    def fake_fetch(symbol, tf, days, session=None):
        calls.append(symbol)
        return synthetic_bars(symbol, tf, 3, now=NOW)

    monkeypatch.setattr(data, "fetch_yahoo", fake_fetch)
    first = get_bars(cfg, "QQQ", provider="yahoo", lookback_days=3)
    assert data.cache_path(cfg, "QQQ").exists() and len(first) == 3 * 78

    def failing_fetch(symbol, tf, days, session=None):
        raise DataError("offline")

    monkeypatch.setattr(data, "fetch_yahoo", failing_fetch)
    cached = get_bars(cfg, "QQQ", provider="yahoo", lookback_days=3)
    assert len(cached) == len(first)
    with pytest.raises(DataError):
        get_bars(cfg, "SPY", provider="yahoo", lookback_days=3)


def test_csv_provider(cfg, tmp_path):
    df = synthetic_bars("QQQ", 5, 2, now=NOW).tz_convert("America/New_York")
    out = tmp_path / "data"
    out.mkdir()
    df.rename_axis("timestamp").to_csv(out / "QQQ_5m.csv")
    loaded = get_bars(cfg, "QQQ", provider="csv")
    assert len(loaded) == len(df)
    assert abs(float(loaded["close"].iloc[-1]) - float(df["close"].iloc[-1])) < 1e-9
    assert isinstance(loaded.index, pd.DatetimeIndex) and str(loaded.index.tz) == "America/New_York"
