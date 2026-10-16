"""The bar-by-bar engine. Backtest, paper and live all run this exact code path; only the
broker and the source of bars differ.

Per completed bar, in this order:
  1. mark the latest price
  2. software stop / target check on the open trade (Rule 4)
  3. daily loss stop: flatten and halt for the day
  4. end-of-day flatten window: flatten and block new entries
  5. ask the strategy for a signal; size it; send a market order
Everything is written to the journal.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from types import ModuleType
from typing import Any

import pandas as pd

from simplebot import sessions
from simplebot.brokers.base import Broker, BrokerError
from simplebot.config import Config
from simplebot.journal import Journal
from simplebot.risk import RiskManager
from simplebot.strategies import Signal, strategy_name

log = logging.getLogger(__name__)


class EngineError(Exception):
    pass


@dataclass
class OpenTrade:
    symbol: str
    side: str
    qty: float
    entry_price: float
    entry_ts: datetime
    stop: float
    target: float | None
    fees: float
    implicit_cost: float
    risk_per_unit: float
    reason: str


@dataclass
class TradeRecord:
    symbol: str
    side: str
    qty: float
    entry_ts: datetime
    entry_price: float
    exit_ts: datetime
    exit_price: float
    stop: float
    target: float | None
    pnl: float
    r_multiple: float
    costs: float
    exit_reason: str
    signal_reason: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class Engine:
    def __init__(
        self,
        cfg: Config,
        strategy: ModuleType,
        params: dict[str, Any],
        broker: Broker,
        risk: RiskManager,
        journal: Journal,
        mode: str = "backtest",
        notifier=None,
    ) -> None:
        self.cfg = cfg
        self.strategy = strategy
        self.strategy_name = strategy_name(strategy)
        self.params = dict(params)
        self.broker = broker
        self.risk = risk
        self.journal = journal
        self.mode = mode
        self.notifier = notifier
        self.open: dict[str, OpenTrade] = {}
        self.trades: list[TradeRecord] = []
        self.equity_curve: list[tuple[datetime, float]] = []
        self.last_prices: dict[str, float] = {}
        self.day_pnl = 0.0
        self.day_costs = 0.0
        self.day_trades = 0
        self.errors = 0
        self.capped_entries = 0
        self.skipped = 0

    # ----------------------------------------------------------------------- helpers
    def _say(self, text: str) -> None:
        log.info(text)
        if self.notifier is not None:
            try:
                self.notifier.send(text)
            except Exception as e:  # notifications must never break trading
                log.warning("notify failed: %s", e)

    def equity(self) -> float:
        return float(self.broker.get_equity())

    def _start_day(self, day, ts: datetime) -> None:
        prev = self.risk.state.day
        if prev is not None:
            self.journal.record_daily(
                prev,
                self.risk.state.day_start_equity,
                self.equity(),
                self.day_pnl,
                self.day_trades,
                self.day_costs,
                self.risk.state.halted,
            )
        self.risk.start_day(day, self.equity())
        self.day_pnl = self.day_costs = 0.0
        self.day_trades = 0
        self.journal.log("day_start", f"{day} equity={self.equity():.2f}", ts=ts)

    # ----------------------------------------------------------------------- main hooks
    def on_bar(self, symbol: str, bars: pd.DataFrame) -> None:
        bar = bars.iloc[-1]
        ts = bars.index[-1].to_pydatetime()
        market = sessions.market_from_tz(bars.index.tz)
        tf = int(bars.attrs.get("timeframe") or self.cfg.timeframe_minutes)
        bar_end = ts + timedelta(minutes=tf)
        day = sessions.session_date(market, ts)
        if day != self.risk.state.day:
            self._start_day(day, ts)

        price = float(bar["close"])
        self.broker.mark(symbol, price, ts)
        self.last_prices[symbol] = price

        trade = self.open.get(symbol)
        if trade is not None:
            decision = RiskManager.check_exit(
                trade.side,
                trade.stop,
                trade.target,
                float(bar["open"]),
                float(bar["high"]),
                float(bar["low"]),
            )
            if decision is not None:
                self._exit(symbol, decision.reason, decision.price, ts)

        if not self.risk.state.halted and self.risk.daily_loss_breached(self.equity()):
            self.journal.log("halt", self.risk.state.halt_reason, ts=ts)
            self._say(
                f"DAILY LOSS STOP: {self.risk.state.halt_reason}. Flattening, done for today."
            )
            self._flatten("daily_loss", ts)

        mtc = sessions.minutes_to_close(market, bar_end)
        if self.risk.should_flatten(mtc):
            same_market = [s for s in self.open if sessions.market_for(s) is market]
            if same_market:
                self._flatten("eod", ts, symbols=same_market)
            self.risk.block_entries(symbol)

        if symbol not in self.open and not self.risk.state.halted:
            self._maybe_enter(symbol, bars, ts, mtc)

        self.equity_curve.append((ts, self.equity()))

    def on_tick(self, symbol: str, price: float, ts: datetime) -> None:
        """Between bars (live/paper): check software stops against the latest price."""
        self.broker.mark(symbol, price, ts)
        self.last_prices[symbol] = price
        trade = self.open.get(symbol)
        if trade is None:
            return
        decision = RiskManager.check_exit(trade.side, trade.stop, trade.target, price, price, price)
        if decision is not None:
            self._exit(symbol, decision.reason, decision.price, ts)

    def end_of_data(self, ts: datetime | None = None) -> None:
        if self.open:
            last = ts or max(t.entry_ts for t in self.open.values())
            self._flatten("end_of_data", last)
        if self.risk.state.day is not None:
            self.journal.record_daily(
                self.risk.state.day,
                self.risk.state.day_start_equity,
                self.equity(),
                self.day_pnl,
                self.day_trades,
                self.day_costs,
                self.risk.state.halted,
            )

    # ----------------------------------------------------------------------- entries
    def _maybe_enter(self, symbol: str, bars: pd.DataFrame, ts: datetime, mtc: float) -> None:
        try:
            sig = self.strategy.signal(bars, self.params)
        except Exception as e:
            self.errors += 1
            self.journal.log("error", f"strategy raised: {e!r}", symbol=symbol, ts=ts)
            log.exception("strategy %s raised", self.strategy_name)
            return
        if sig is None:
            return
        if not isinstance(sig, Signal):
            self.journal.log("error", "strategy returned a non-Signal", symbol=symbol, ts=ts)
            return
        ok, why = self.risk.can_enter(symbol, sig.side, len(self.open), mtc)
        if not ok:
            self.skipped += 1
            self.journal.log("skip", why, symbol=symbol, data={"signal": asdict(sig)}, ts=ts)
            return
        qty, why = self.risk.position_size(
            self.equity(),
            self.broker.get_cash(),
            sig.entry,
            sig.stop,
            self.broker.round_qty,
            symbol=symbol,
        )
        if qty <= 0:
            self.skipped += 1
            self.journal.log("skip", why, symbol=symbol, data={"signal": asdict(sig)}, ts=ts)
            return
        if "capped" in why:
            self.capped_entries += 1
        self._enter(symbol, sig, qty, ts)

    def _enter(self, symbol: str, sig: Signal, qty: float, ts: datetime) -> None:
        side = "buy" if sig.side == "long" else "sell"
        self.journal.log("signal", sig.reason, symbol=symbol, data=asdict(sig), ts=ts)
        try:
            order = self.broker.place_order(symbol, side, qty)
            order = self.broker.await_fill(order)
        except BrokerError as e:
            self.errors += 1
            self.journal.log("order_rejected", str(e), symbol=symbol, ts=ts)
            if self.mode == "live":
                raise EngineError(f"entry order failed in live mode: {e}") from e
            return
        if order.status == "rejected" or order.filled_qty <= 0:
            self.journal.log("order_rejected", order.note or order.status, symbol=symbol, ts=ts)
            return
        if order.status == "partially_filled":
            self.journal.log(
                "partial_fill", f"filled {order.filled_qty} of {qty}", symbol=symbol, ts=ts
            )
            try:
                self.broker.cancel(order.id)
            except BrokerError:
                pass
        qty = order.filled_qty
        price = float(order.avg_fill_price or sig.entry)
        self.open[symbol] = OpenTrade(
            symbol=symbol,
            side=sig.side,
            qty=qty,
            entry_price=price,
            entry_ts=ts,
            stop=sig.stop,
            target=sig.target,
            fees=order.fees,
            implicit_cost=order.implicit_cost,
            risk_per_unit=sig.risk_per_unit,
            reason=sig.reason,
        )
        self.risk.record_entry(symbol)
        self.journal.log(
            "entry",
            f"{sig.side} {qty} {symbol} @ {price:.4f} stop {sig.stop:.4f}"
            f" target {sig.target if sig.target is None else round(sig.target, 4)}",
            symbol=symbol,
            data={"qty": qty, "price": price, "order_id": order.id},
            ts=ts,
        )
        if self.mode != "backtest":
            self._say(f"ENTRY {sig.side} {qty} {symbol} @ {price:.2f} (stop {sig.stop:.2f})")

    # ----------------------------------------------------------------------- exits
    def _exit(self, symbol: str, reason: str, ref_price: float, ts: datetime) -> None:
        trade = self.open.get(symbol)
        if trade is None:
            return
        side = "sell" if trade.side == "long" else "buy"
        self.broker.set_exec_price(symbol, ref_price)
        try:
            order = self.broker.place_order(symbol, side, trade.qty)
            order = self.broker.await_fill(order)
        except BrokerError as e:
            self.errors += 1
            self.journal.log("order_rejected", f"exit failed: {e}", symbol=symbol, ts=ts)
            if self.mode == "live":
                raise EngineError(f"exit order failed in live mode: {e}") from e
            return
        if order.status == "rejected" or order.filled_qty <= 0:
            self.errors += 1
            self.journal.log(
                "order_rejected", f"exit {order.note or order.status}", symbol=symbol, ts=ts
            )
            if self.mode == "live":
                raise EngineError(f"exit order rejected in live mode: {order.note}")
            return
        exit_price = float(order.avg_fill_price or ref_price)
        direction = 1.0 if trade.side == "long" else -1.0
        gross = (exit_price - trade.entry_price) * trade.qty * direction
        explicit = trade.fees + order.fees
        implicit = trade.implicit_cost + order.implicit_cost
        pnl = gross - explicit  # implicit costs are already inside the fill prices
        risk_amount = trade.risk_per_unit * trade.qty
        r_multiple = pnl / risk_amount if risk_amount > 0 else 0.0
        rec = TradeRecord(
            symbol=symbol,
            side=trade.side,
            qty=trade.qty,
            entry_ts=trade.entry_ts,
            entry_price=trade.entry_price,
            exit_ts=ts,
            exit_price=exit_price,
            stop=trade.stop,
            target=trade.target,
            pnl=pnl,
            r_multiple=r_multiple,
            costs=explicit + implicit,
            exit_reason=reason,
            signal_reason=trade.reason,
        )
        self.trades.append(rec)
        self.journal.record_trade(rec.to_dict(), strategy=self.strategy_name)
        self.journal.log(
            "exit",
            f"{reason}: {trade.side} {trade.qty} {symbol} @ {exit_price:.4f}"
            f" pnl {pnl:+.2f} ({r_multiple:+.2f}R)",
            symbol=symbol,
            data={"pnl": pnl, "r": r_multiple, "costs": rec.costs},
            ts=ts,
        )
        self.day_pnl += pnl
        self.day_costs += rec.costs
        self.day_trades += 1
        del self.open[symbol]
        if self.mode != "backtest":
            self._say(f"EXIT ({reason}) {symbol} @ {exit_price:.2f} pnl {pnl:+.2f}")

    def _flatten(self, reason: str, ts: datetime, symbols: list[str] | None = None) -> None:
        for symbol in list(symbols or self.open):
            if symbol in self.open:
                self._exit(
                    symbol, reason, self.last_prices.get(symbol, self.open[symbol].entry_price), ts
                )
        if symbols is None and self.mode != "backtest":
            try:
                self.broker.flatten_all()  # anything the broker holds that we do not know about
            except BrokerError as e:
                self.journal.log("error", f"flatten_all failed: {e}", ts=ts)
                if self.mode == "live":
                    raise EngineError(f"flatten failed in live mode: {e}") from e
        self.journal.log("flatten", reason, ts=ts)

    def emergency_stop(self, why: str) -> None:
        """Live-mode panic button: cancel everything, flatten, tell the human."""
        self.journal.log("emergency_stop", why)
        try:
            self.broker.flatten_all()
        finally:
            self.open.clear()
            self._say(f"EMERGENCY STOP: {why}. All orders cancelled and positions flattened.")

    # ----------------------------------------------------------------------- reporting
    def daily_summary(self) -> str:
        s = self.risk.state
        eq = self.equity()
        lines = [
            f"Simplebot {self.mode} summary for {s.day}",
            f"equity {eq:.2f} (day pnl {self.day_pnl:+.2f}, costs {self.day_costs:.2f})",
            f"trades today: {self.day_trades}; open: {len(self.open)}",
            f"strategy {self.strategy_name} params {self.params}",
        ]
        if s.halted:
            lines.append(f"HALTED: {s.halt_reason}")
        return "\n".join(lines)
