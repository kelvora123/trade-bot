import pytest

from simplebot.config import ConfigError, from_dict, live_confirmed, load_config, write_params


def test_defaults_and_validation(tmp_path):
    cfg = from_dict({"symbols": ["qqq"]})
    assert cfg.mode == "paper" and cfg.symbols == ["QQQ"] and cfg.timeframe_minutes == 5
    assert cfg.risk.risk_per_trade_pct == 0.5 and cfg.coach.provider == "none"
    with pytest.raises(ConfigError):
        from_dict({"symbols": ["QQQ"], "mode": "yolo"})
    with pytest.raises(ConfigError):
        from_dict({"symbols": ["QQQ"], "mode": "live", "broker": "paper"})
    with pytest.raises(ConfigError):
        from_dict({"symbols": ["QQQ"], "timeframe": "5h"})
    with pytest.raises(ConfigError):
        from_dict({"symbols": ["QQQ"], "risk": {"nope": 1}})
    with pytest.raises(ConfigError):
        from_dict({"symbols": []})
    with pytest.raises(ConfigError):
        from_dict({"symbols": ["QQQ"], "risk": {"risk_per_trade_pct": 50}})


def test_live_confirm_env(monkeypatch):
    monkeypatch.delenv("SIMPLEBOT_LIVE_CONFIRM", raising=False)
    assert not live_confirmed()
    monkeypatch.setenv("SIMPLEBOT_LIVE_CONFIRM", "YES")
    assert live_confirmed()
    monkeypatch.setenv("SIMPLEBOT_LIVE_CONFIRM", "true")
    assert not live_confirmed()


def test_write_params_keeps_comments(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text(
        "# keep me\nmode: paper\nsymbols: [QQQ]\nparams: {range_minutes: 5}\n"
        "risk:\n  max_positions: 3  # also keep\n"
    )
    write_params(p, {"range_minutes": 15, "target_r": 3})
    text = p.read_text()
    assert "# keep me" in text and "# also keep" in text
    cfg = load_config(p)
    assert cfg.params == {"range_minutes": 15, "target_r": 3}
    assert cfg.risk.max_positions == 3
