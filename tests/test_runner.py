from datetime import timedelta
from zoneinfo import ZoneInfo

from simplebot.backtest import cost_model
from simplebot.brokers.paper import PaperBroker
from simplebot.engine import Engine
from simplebot.journal import Journal
from simplebot.notify import Notifier
from simplebot.risk import RiskManager
from simplebot.runner import Runner
from tests.conftest import StubStrategy, bars_from_closes


def make_runner(cfg, now_holder, df):
    cfg.symbols = ["QQQ"]
    broker = PaperBroker(1000, cost_model(cfg))
    engine = Engine(
        cfg,
        StubStrategy(fire_at=2, stop_pct=1.0),
        {},
        broker,
        RiskManager(cfg.risk),
        Journal(":memory:", mode="paper"),
        mode="paper",
    )
    seen = []

    def fetch(symbol):
        seen.append(symbol)
        return df[df.index <= now_holder["now"]]

    runner = Runner(
        cfg,
        "paper",
        engine,
        fetch,
        engine.journal,
        Notifier(enabled=False),
        clock=lambda: now_holder["now"],
        sleep=lambda s: None,
    )
    return runner, engine, seen


def test_runner_feeds_new_bars_and_ticks(cfg):
    ny = ZoneInfo("America/New_York")
    df = bars_from_closes([100, 100, 100, 100], wick=0.0)
    now = {"now": df.index[1] + timedelta(minutes=5, seconds=1)}  # bars 0 and 1 complete
    runner, engine, seen = make_runner(cfg, now, df)
    assert runner.step() is True
    assert "QQQ" in engine.open and runner.last_bar["QQQ"] == df.index[1]
    runner.step()  # same bar -> tick only
    assert runner.last_bar["QQQ"] == df.index[1] and len(seen) == 2
    now["now"] = df.index[2] + timedelta(minutes=5, seconds=1)
    runner.step()
    assert runner.last_bar["QQQ"] == df.index[2]
    now["now"] = df.index[0].replace(hour=17).astimezone(ny)
    assert runner.step() is False  # after the close: summary once
    assert runner.summarised == engine.risk.state.day
    runner.run_forever(max_steps=1)
    assert runner.steps == 1 and not engine.open  # shutdown flattened the open trade
