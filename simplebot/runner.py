"""The long-running paper/live loop. Polls bars, feeds completed bars to the Engine, checks
software stops between bars, sends the daily summary, and on any unexpected error in live
mode cancels everything, flattens, alerts and stops."""

from __future__ import annotations

import logging
import signal
import time
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta

import pandas as pd

from simplebot import sessions
from simplebot.brokers.base import BrokerError
from simplebot.config import Config
from simplebot.engine import Engine, EngineError
from simplebot.journal import Journal
from simplebot.notify import Notifier

log = logging.getLogger(__name__)

HISTORY_SESSIONS = 25


class Runner:
    def __init__(
        self,
        cfg: Config,
        mode: str,
        engine: Engine,
        fetch_bars: Callable[[str], pd.DataFrame],
        journal: Journal,
        notifier: Notifier | None = None,
        clock: Callable[[], datetime] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        flatten_on_exit: bool = True,
    ) -> None:
        self.cfg = cfg
        self.mode = mode
        self.engine = engine
        self.fetch_bars = fetch_bars
        self.journal = journal
        self.notifier = notifier
        self.clock = clock or (lambda: datetime.now(UTC))
        self.sleep = sleep
        self.flatten_on_exit = flatten_on_exit
        self.stop_requested = False
        self.last_bar: dict[str, pd.Timestamp] = {}
        self.summarised: date | None = None
        self.consecutive_errors = 0
        self.steps = 0

    # ------------------------------------------------------------------ lifecycle
    def request_stop(self, *_args) -> None:
        self.stop_requested = True

    def install_signal_handlers(self) -> None:
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, self.request_stop)
            except ValueError:  # not in the main thread
                pass

    def say(self, text: str) -> None:
        log.info(text)
        if self.notifier is not None:
            self.notifier.send(text)

    def run_forever(self, max_steps: int | None = None) -> None:
        self.say(
            f"Simplebot {self.mode} started: {self.cfg.strategy} {self.engine.params} on"
            f" {self.cfg.symbols} ({self.cfg.timeframe}), broker {self.engine.broker.name}"
        )
        try:
            while not self.stop_requested and (max_steps is None or self.steps < max_steps):
                self.steps += 1
                try:
                    active = self.step()
                    self.consecutive_errors = 0
                except (EngineError, BrokerError, Exception) as e:
                    self.consecutive_errors += 1
                    self.journal.log("error", repr(e))
                    log.exception("loop error")
                    if self.mode == "live":
                        self.engine.emergency_stop(repr(e))
                        raise
                    wait = min(300.0, 5.0 * 2**self.consecutive_errors)
                    self.say(f"paper loop error ({e!r}); retrying in {wait:.0f}s")
                    self.sleep(wait)
                    continue
                self.sleep(self.cfg.poll_seconds if active else 60.0)
        finally:
            self.shutdown()

    def shutdown(self) -> None:
        if self.flatten_on_exit and self.engine.open:
            try:
                self.engine._flatten("shutdown", self.clock())
            except Exception as e:  # pragma: no cover - last resort
                log.exception("flatten on shutdown failed: %s", e)
        try:
            self.engine.broker.close()
        except Exception:  # pragma: no cover
            pass
        self.say(f"Simplebot {self.mode} stopped. {self.engine.daily_summary()}")

    # ------------------------------------------------------------------ one iteration
    def step(self) -> bool:
        """Process every symbol once. Returns True if any market is in session."""
        now = self.clock()
        tf = timedelta(minutes=self.cfg.timeframe_minutes)
        active = False
        for symbol in self.cfg.symbols:
            market = sessions.market_for(symbol)
            if not sessions.is_in_session(market, now):
                continue
            active = True
            bars = self.fetch_bars(symbol)
            if bars.empty:
                continue
            completed = bars[(bars.index + tf) <= now]
            if completed.empty:
                continue
            last = completed.index[-1]
            if self.last_bar.get(symbol) != last:
                self.last_bar[symbol] = last
                n = HISTORY_SESSIONS * (market.session_minutes // self.cfg.timeframe_minutes)
                window = completed.tail(n)
                window.attrs = dict(bars.attrs)
                self.engine.on_bar(symbol, window)
            else:
                self.engine.on_tick(symbol, float(bars["close"].iloc[-1]), now)
        if not active:
            self.maybe_daily_summary(now)
        try:
            self.journal.set_state(
                "heartbeat",
                {"ts": now.isoformat(), "active": active, "steps": self.steps, "mode": self.mode},
                ts=now,
            )
        except Exception as e:  # never let bookkeeping stop the loop
            log.warning("heartbeat write failed: %s", e)
        return active

    def maybe_daily_summary(self, now: datetime) -> None:
        day = self.engine.risk.state.day
        if day is None or self.summarised == day:
            return
        if any(sessions.is_in_session(sessions.market_for(s), now) for s in self.cfg.symbols):
            return
        self.summarised = day
        self.engine.end_of_data(
            now
        )  # writes the daily row; flattens anything left (should be none)
        self.say(self.engine.daily_summary())
