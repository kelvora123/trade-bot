"""SQLite decision log. Every signal, order, fill, skip, halt and daily summary goes here, as
do parameter changes and the coach's notes. One file, zero dependencies beyond the stdlib."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, date, datetime
from typing import Any

import pandas as pd

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY, ts TEXT NOT NULL, mode TEXT, symbol TEXT, kind TEXT NOT NULL,
  message TEXT, data TEXT);
CREATE TABLE IF NOT EXISTS trades (
  id INTEGER PRIMARY KEY, mode TEXT, strategy TEXT, symbol TEXT, side TEXT, qty REAL,
  entry_ts TEXT, entry_price REAL, exit_ts TEXT, exit_price REAL, stop REAL, target REAL,
  pnl REAL, r_multiple REAL, costs REAL, exit_reason TEXT, signal_reason TEXT);
CREATE TABLE IF NOT EXISTS daily (
  date TEXT, mode TEXT, start_equity REAL, end_equity REAL, pnl REAL, trades INTEGER,
  costs REAL, halted INTEGER, PRIMARY KEY (date, mode));
CREATE TABLE IF NOT EXISTS params_history (
  id INTEGER PRIMARY KEY, ts TEXT, strategy TEXT, old_params TEXT, new_params TEXT,
  stats TEXT, source TEXT, applied INTEGER);
CREATE TABLE IF NOT EXISTS coach_notes (id INTEGER PRIMARY KEY, ts TEXT, note TEXT);
CREATE TABLE IF NOT EXISTS llm_spend (date TEXT, component TEXT, usd REAL,
  PRIMARY KEY (date, component));
"""


def _iso(ts: datetime | None) -> str:
    ts = ts or datetime.now(UTC)
    return ts.isoformat()


class Journal:
    def __init__(self, path: str = ":memory:", mode: str = "backtest") -> None:
        self.path = path
        self.mode = mode
        self.conn = sqlite3.connect(path)
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # events ------------------------------------------------------------------------
    def log(
        self,
        kind: str,
        message: str = "",
        symbol: str | None = None,
        data: dict[str, Any] | None = None,
        ts: datetime | None = None,
    ) -> None:
        self.conn.execute(
            "INSERT INTO events (ts, mode, symbol, kind, message, data) VALUES (?,?,?,?,?,?)",
            (_iso(ts), self.mode, symbol, kind, message, json.dumps(data or {}, default=str)),
        )
        self.conn.commit()

    def events(
        self, since: datetime | None = None, kinds: list[str] | None = None, limit: int = 1000
    ) -> pd.DataFrame:
        q, args = "SELECT * FROM events WHERE 1=1", []
        if since is not None:
            q += " AND ts >= ?"
            args.append(_iso(since))
        if kinds:
            q += f" AND kind IN ({','.join('?' * len(kinds))})"
            args.extend(kinds)
        q += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        return pd.read_sql_query(q, self.conn, params=args)

    # trades ------------------------------------------------------------------------
    def record_trade(self, t: dict[str, Any], strategy: str = "") -> None:
        self.conn.execute(
            "INSERT INTO trades (mode, strategy, symbol, side, qty, entry_ts, entry_price, exit_ts,"
            " exit_price, stop, target, pnl, r_multiple, costs, exit_reason, signal_reason)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                self.mode,
                strategy,
                t["symbol"],
                t["side"],
                t["qty"],
                str(t["entry_ts"]),
                t["entry_price"],
                str(t["exit_ts"]),
                t["exit_price"],
                t["stop"],
                t.get("target"),
                t["pnl"],
                t.get("r_multiple"),
                t.get("costs", 0.0),
                t.get("exit_reason", ""),
                t.get("signal_reason", ""),
            ),
        )
        self.conn.commit()

    def trades(self, since: datetime | None = None, mode: str | None = None) -> pd.DataFrame:
        q, args = "SELECT * FROM trades WHERE 1=1", []
        if since is not None:
            q += " AND entry_ts >= ?"
            args.append(_iso(since))
        if mode:
            q += " AND mode = ?"
            args.append(mode)
        return pd.read_sql_query(q + " ORDER BY id", self.conn, params=args)

    # daily -------------------------------------------------------------------------
    def record_daily(
        self,
        day: date,
        start_equity: float,
        end_equity: float,
        pnl: float,
        trades: int,
        costs: float,
        halted: bool,
    ) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO daily (date, mode, start_equity, end_equity, pnl, trades,"
            " costs, halted) VALUES (?,?,?,?,?,?,?,?)",
            (day.isoformat(), self.mode, start_equity, end_equity, pnl, trades, costs, int(halted)),
        )
        self.conn.commit()

    def daily(self, since: date | None = None) -> pd.DataFrame:
        q, args = "SELECT * FROM daily WHERE mode = ?", [self.mode]
        if since is not None:
            q += " AND date >= ?"
            args.append(since.isoformat())
        return pd.read_sql_query(q + " ORDER BY date", self.conn, params=args)

    # learning loop -----------------------------------------------------------------
    def add_params_change(
        self, strategy: str, old: dict, new: dict, stats: dict, source: str, applied: bool
    ) -> None:
        self.conn.execute(
            "INSERT INTO params_history (ts, strategy, old_params, new_params, stats, source,"
            " applied) VALUES (?,?,?,?,?,?,?)",
            (
                _iso(None),
                strategy,
                json.dumps(old, sort_keys=True),
                json.dumps(new, sort_keys=True),
                json.dumps(stats, default=str),
                source,
                int(applied),
            ),
        )
        self.conn.commit()

    def params_history(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT ts, strategy, old_params, new_params, stats, source, applied FROM"
            " params_history ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [
            {
                "ts": r[0],
                "strategy": r[1],
                "old_params": json.loads(r[2]),
                "new_params": json.loads(r[3]),
                "stats": json.loads(r[4]),
                "source": r[5],
                "applied": bool(r[6]),
            }
            for r in rows
        ]

    def last_applied_change(self) -> dict[str, Any] | None:
        for row in self.params_history(limit=100):
            if row["applied"]:
                return row
        return None

    # coach -------------------------------------------------------------------------
    def add_coach_note(self, note: dict[str, Any]) -> None:
        self.conn.execute(
            "INSERT INTO coach_notes (ts, note) VALUES (?, ?)",
            (_iso(None), json.dumps(note, default=str)),
        )
        self.conn.commit()

    def coach_notes(self, limit: int = 10) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT ts, note FROM coach_notes ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [{"ts": r[0], **json.loads(r[1])} for r in rows]

    def llm_spend_today(self, component: str, day: date | None = None) -> float:
        day = day or datetime.now(UTC).date()
        row = self.conn.execute(
            "SELECT usd FROM llm_spend WHERE date = ? AND component = ?",
            (day.isoformat(), component),
        ).fetchone()
        return float(row[0]) if row else 0.0

    def add_llm_spend(self, component: str, usd: float, day: date | None = None) -> None:
        day = day or datetime.now(UTC).date()
        self.conn.execute(
            "INSERT INTO llm_spend (date, component, usd) VALUES (?, ?, ?)"
            " ON CONFLICT(date, component) DO UPDATE SET usd = usd + excluded.usd",
            (day.isoformat(), component, float(usd)),
        )
        self.conn.commit()

    def coach_spend_today(self, day: date | None = None) -> float:
        return self.llm_spend_today("coach", day)

    def add_coach_spend(self, usd: float, day: date | None = None) -> None:
        self.add_llm_spend("coach", usd, day)
