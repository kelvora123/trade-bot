"""SQLite persistence.

Layer 1 mirrors every pulled chain here, which is what lets Layer 2 compute an
IV rank over a 252-day lookback and diff today's chain against the prior run
for new-strike / new-expiry detection. Without the history this database
accumulates, those two features cannot exist -- hence "required" in the spec.

All writes are idempotent on (date, key) so re-running a day repairs rather
than duplicates.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from typing import Any

__all__ = ["Store"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS chain_snapshots (
    asof            TEXT NOT NULL,
    ticker          TEXT NOT NULL,
    expiry          TEXT NOT NULL,
    strike          REAL NOT NULL,
    option_kind     TEXT NOT NULL,
    bid             REAL,
    ask             REAL,
    last            REAL,
    mark            REAL,
    iv              REAL,
    volume          REAL,
    open_interest   REAL,
    PRIMARY KEY (asof, ticker, expiry, strike, option_kind)
);
CREATE INDEX IF NOT EXISTS ix_chain_ticker_asof ON chain_snapshots (ticker, asof);

-- Per-contract IV history: the series an IV rank is computed from.
CREATE TABLE IF NOT EXISTS iv_history (
    asof            TEXT NOT NULL,
    symbol          TEXT NOT NULL,   -- OCC symbol
    ticker          TEXT NOT NULL,
    iv              REAL NOT NULL,
    PRIMARY KEY (asof, symbol)
);
CREATE INDEX IF NOT EXISTS ix_iv_symbol_asof ON iv_history (symbol, asof);
CREATE INDEX IF NOT EXISTS ix_iv_ticker_asof ON iv_history (ticker, asof);

-- Underlying-level ATM IV, a more stable rank series than a single contract.
CREATE TABLE IF NOT EXISTS underlying_iv_history (
    asof            TEXT NOT NULL,
    ticker          TEXT NOT NULL,
    atm_iv          REAL NOT NULL,
    spot            REAL,
    PRIMARY KEY (asof, ticker)
);

CREATE TABLE IF NOT EXISTS spot_history (
    asof            TEXT NOT NULL,
    ticker          TEXT NOT NULL,
    close           REAL NOT NULL,
    PRIMARY KEY (asof, ticker)
);

-- Daily OHLCV per underlying. Closes alone cannot produce a true range, so
-- ATR-based stops and Donchian breakouts need highs and lows stored too.
CREATE TABLE IF NOT EXISTS ohlcv_history (
    asof            TEXT NOT NULL,
    ticker          TEXT NOT NULL,
    open            REAL NOT NULL,
    high            REAL NOT NULL,
    low             REAL NOT NULL,
    close           REAL NOT NULL,
    volume          REAL,
    PRIMARY KEY (asof, ticker)
);
CREATE INDEX IF NOT EXISTS ix_ohlcv_ticker ON ohlcv_history (ticker, asof);

CREATE TABLE IF NOT EXISTS runs (
    asof            TEXT NOT NULL,
    layer           TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    payload         TEXT NOT NULL,   -- JSON blob of that layer's output
    PRIMARY KEY (asof, layer)
);

CREATE TABLE IF NOT EXISTS macro_scores (
    asof            TEXT PRIMARY KEY,
    score           REAL NOT NULL,
    regime          TEXT NOT NULL,
    components      TEXT NOT NULL    -- JSON
);

-- Content-addressed news cache. A day whose headline set hashes to an entry
-- already here costs zero API tokens.
CREATE TABLE IF NOT EXISTS news_cache (
    cache_key       TEXT PRIMARY KEY,   -- sha256 of (model, sorted headline ids)
    asof            TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    payload         TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS llm_usage (
    asof            TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    model           TEXT NOT NULL,
    input_tokens    INTEGER NOT NULL,
    output_tokens   INTEGER NOT NULL,
    purpose         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_usage_asof ON llm_usage (asof);

CREATE TABLE IF NOT EXISTS paper_fills (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              TEXT NOT NULL,
    symbol          TEXT NOT NULL,
    ticker          TEXT NOT NULL,
    side            TEXT NOT NULL,
    qty             REAL NOT NULL,
    price           REAL NOT NULL,
    commission      REAL NOT NULL,
    slippage        REAL NOT NULL,
    reason          TEXT DEFAULT ''
);

-- The simulated book. Paper fills accumulate here into real positions, which
-- is what lets `tradebot run` mark a paper portfolio rather than a static file.
CREATE TABLE IF NOT EXISTS paper_positions (
    symbol          TEXT PRIMARY KEY,
    ticker          TEXT NOT NULL,
    kind            TEXT NOT NULL,
    option_kind     TEXT,
    strike          REAL,
    expiry          TEXT,
    qty             REAL NOT NULL,
    avg_price       REAL NOT NULL,
    multiplier      INTEGER NOT NULL DEFAULT 100,
    target          REAL,
    stop            REAL,
    opened_at       TEXT NOT NULL,
    realised_pnl    REAL NOT NULL DEFAULT 0,
    -- Commission paid to open, carried until the position closes so a
    -- round-trip's P&L reflects both legs' costs, not just the exit's.
    entry_costs     REAL NOT NULL DEFAULT 0
);

-- Closed round-trips, kept separately so win rate and profit factor survive
-- the position row being deleted at close.
CREATE TABLE IF NOT EXISTS paper_trades (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol          TEXT NOT NULL,
    ticker          TEXT NOT NULL,
    opened_at       TEXT NOT NULL,
    closed_at       TEXT NOT NULL,
    qty             REAL NOT NULL,
    entry_price     REAL NOT NULL,
    exit_price      REAL NOT NULL,
    multiplier      INTEGER NOT NULL,
    pnl             REAL NOT NULL,
    costs           REAL NOT NULL DEFAULT 0,
    reason          TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS paper_cash (
    id              INTEGER PRIMARY KEY CHECK (id = 1),
    cash            REAL NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS paper_equity (
    asof            TEXT PRIMARY KEY,
    cash            REAL NOT NULL,
    positions_value REAL NOT NULL,
    equity          REAL NOT NULL
);
"""


class Store:
    """Thin SQLite wrapper. Not thread-safe; one instance per process run."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        try:
            yield self.conn
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    # ---------------------------------------------------------------- chains

    def save_chain(self, asof: date, rows: list[Any]) -> int:
        """Mirror a pulled chain. Idempotent on (asof, contract)."""
        payload = [
            (
                asof.isoformat(),
                r.ticker,
                r.expiry.isoformat(),
                float(r.strike),
                r.option_kind.value,
                r.bid,
                r.ask,
                r.last,
                r.mark,
                r.iv,
                r.volume,
                r.open_interest,
            )
            for r in rows
        ]
        with self.tx() as c:
            c.executemany(
                "INSERT OR REPLACE INTO chain_snapshots "
                "(asof,ticker,expiry,strike,option_kind,bid,ask,last,mark,iv,volume,open_interest) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                payload,
            )
        return len(payload)

    def chain_contracts(self, ticker: str, asof: date) -> set[tuple[str, float, str]]:
        """The (expiry, strike, kind) set present for a ticker on a date."""
        cur = self.conn.execute(
            "SELECT expiry, strike, option_kind FROM chain_snapshots WHERE ticker=? AND asof=?",
            (ticker, asof.isoformat()),
        )
        return {(r["expiry"], r["strike"], r["option_kind"]) for r in cur}

    def previous_chain_date(self, ticker: str, before: date) -> date | None:
        """Most recent prior snapshot date for a ticker -- the diff baseline."""
        cur = self.conn.execute(
            "SELECT MAX(asof) AS d FROM chain_snapshots WHERE ticker=? AND asof<?",
            (ticker, before.isoformat()),
        )
        row = cur.fetchone()
        return date.fromisoformat(row["d"]) if row and row["d"] else None

    # ------------------------------------------------------------------- iv

    def record_iv(self, asof: date, symbol: str, ticker: str, iv: float) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT OR REPLACE INTO iv_history (asof,symbol,ticker,iv) VALUES (?,?,?,?)",
                (asof.isoformat(), symbol, ticker, float(iv)),
            )

    def record_underlying_iv(self, asof: date, ticker: str, atm_iv: float, spot: float | None) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT OR REPLACE INTO underlying_iv_history (asof,ticker,atm_iv,spot) VALUES (?,?,?,?)",
                (asof.isoformat(), ticker, float(atm_iv), spot),
            )

    def iv_series(self, ticker: str, asof: date, lookback_days: int) -> list[float]:
        """Underlying ATM IV history within the lookback window, oldest first."""
        cur = self.conn.execute(
            "SELECT atm_iv FROM underlying_iv_history "
            "WHERE ticker=? AND asof<=? AND asof>=date(?, ?) ORDER BY asof",
            (ticker, asof.isoformat(), asof.isoformat(), f"-{int(lookback_days)} days"),
        )
        return [float(r["atm_iv"]) for r in cur]

    def record_ohlcv(self, asof: date, ticker: str, o: float, h: float, low: float,
                     c: float, volume: float | None = None) -> None:
        if h < low:
            raise ValueError(f"{ticker} {asof}: high {h} below low {low}")
        with self.tx() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO ohlcv_history (asof,ticker,open,high,low,close,volume) "
                "VALUES (?,?,?,?,?,?,?)",
                (asof.isoformat(), ticker, float(o), float(h), float(low), float(c), volume),
            )

    def ohlcv(self, ticker: str, asof: date, lookback_days: int) -> list[dict[str, Any]]:
        """Daily bars within the lookback window, oldest first."""
        cur = self.conn.execute(
            "SELECT asof,open,high,low,close,volume FROM ohlcv_history "
            "WHERE ticker=? AND asof<=? AND asof>=date(?, ?) ORDER BY asof",
            (ticker, asof.isoformat(), asof.isoformat(), f"-{int(lookback_days)} days"),
        )
        return [dict(r) for r in cur]

    def record_spot(self, asof: date, ticker: str, close: float) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT OR REPLACE INTO spot_history (asof,ticker,close) VALUES (?,?,?)",
                (asof.isoformat(), ticker, float(close)),
            )

    # ----------------------------------------------------------------- runs

    def save_run(self, asof: date, layer: str, payload: dict[str, Any], created_at: str) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT OR REPLACE INTO runs (asof,layer,created_at,payload) VALUES (?,?,?,?)",
                (asof.isoformat(), layer, created_at, json.dumps(payload, default=str)),
            )

    def load_run(self, asof: date, layer: str) -> dict[str, Any] | None:
        cur = self.conn.execute(
            "SELECT payload FROM runs WHERE asof=? AND layer=?", (asof.isoformat(), layer)
        )
        row = cur.fetchone()
        return json.loads(row["payload"]) if row else None

    def save_macro(self, asof: date, score: float, regime: str, components: dict[str, Any]) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT OR REPLACE INTO macro_scores (asof,score,regime,components) VALUES (?,?,?,?)",
                (asof.isoformat(), float(score), regime, json.dumps(components, default=str)),
            )

    # ---------------------------------------------------------------- news

    def get_news_cache(self, cache_key: str) -> dict[str, Any] | None:
        cur = self.conn.execute("SELECT payload FROM news_cache WHERE cache_key=?", (cache_key,))
        row = cur.fetchone()
        return json.loads(row["payload"]) if row else None

    def put_news_cache(self, cache_key: str, asof: date, payload: dict[str, Any], created_at: str) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT OR REPLACE INTO news_cache (cache_key,asof,created_at,payload) VALUES (?,?,?,?)",
                (cache_key, asof.isoformat(), created_at, json.dumps(payload, default=str)),
            )

    def record_llm_usage(
        self, asof: date, created_at: str, model: str, input_tokens: int, output_tokens: int, purpose: str
    ) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT INTO llm_usage (asof,created_at,model,input_tokens,output_tokens,purpose) "
                "VALUES (?,?,?,?,?,?)",
                (asof.isoformat(), created_at, model, int(input_tokens), int(output_tokens), purpose),
            )

    def llm_calls_today(self, asof: date) -> int:
        cur = self.conn.execute("SELECT COUNT(*) AS n FROM llm_usage WHERE asof=?", (asof.isoformat(),))
        return int(cur.fetchone()["n"])

    def llm_tokens_total(self) -> tuple[int, int]:
        cur = self.conn.execute(
            "SELECT COALESCE(SUM(input_tokens),0) AS i, COALESCE(SUM(output_tokens),0) AS o FROM llm_usage"
        )
        row = cur.fetchone()
        return int(row["i"]), int(row["o"])

    # ---------------------------------------------------------------- paper

    def record_fill(self, **kw: Any) -> None:
        cols = ("ts", "symbol", "ticker", "side", "qty", "price", "commission", "slippage", "reason")
        with self.tx() as c:
            c.execute(
                f"INSERT INTO paper_fills ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                tuple(kw.get(k) for k in cols),
            )

    def record_equity(self, asof: date, cash: float, positions_value: float, equity: float) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT OR REPLACE INTO paper_equity (asof,cash,positions_value,equity) VALUES (?,?,?,?)",
                (asof.isoformat(), float(cash), float(positions_value), float(equity)),
            )

    def load_cash(self, default: float) -> float:
        """Persisted cash balance, seeded from config on first use."""
        row = self.conn.execute("SELECT cash FROM paper_cash WHERE id=1").fetchone()
        if row is None:
            self.save_cash(default, "seed")
            return default
        return float(row["cash"])

    def save_cash(self, cash: float, updated_at: str) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT OR REPLACE INTO paper_cash (id,cash,updated_at) VALUES (1,?,?)",
                (float(cash), updated_at),
            )

    def get_position(self, symbol: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM paper_positions WHERE symbol=?", (symbol,)
        ).fetchone()
        return dict(row) if row else None

    def all_positions(self) -> list[dict[str, Any]]:
        cur = self.conn.execute("SELECT * FROM paper_positions ORDER BY ticker, symbol")
        return [dict(r) for r in cur]

    def upsert_position(self, **kw: Any) -> None:
        cols = (
            "symbol", "ticker", "kind", "option_kind", "strike", "expiry", "qty",
            "avg_price", "multiplier", "target", "stop", "opened_at", "realised_pnl",
            "entry_costs",
        )
        with self.tx() as c:
            c.execute(
                f"INSERT OR REPLACE INTO paper_positions ({','.join(cols)}) "
                f"VALUES ({','.join('?' * len(cols))})",
                tuple(kw.get(k) for k in cols),
            )

    def delete_position(self, symbol: str) -> None:
        with self.tx() as c:
            c.execute("DELETE FROM paper_positions WHERE symbol=?", (symbol,))

    def record_trade(self, **kw: Any) -> None:
        cols = (
            "symbol", "ticker", "opened_at", "closed_at", "qty", "entry_price",
            "exit_price", "multiplier", "pnl", "costs", "reason",
        )
        with self.tx() as c:
            c.execute(
                f"INSERT INTO paper_trades ({','.join(cols)}) "
                f"VALUES ({','.join('?' * len(cols))})",
                tuple(kw.get(k) for k in cols),
            )

    def closed_trades(self) -> list[dict[str, Any]]:
        cur = self.conn.execute("SELECT * FROM paper_trades ORDER BY closed_at")
        return [dict(r) for r in cur]

    def history_coverage(self) -> list[dict[str, Any]]:
        """Days of stored ATM-IV history per ticker -- progress toward IV rank."""
        cur = self.conn.execute(
            "SELECT ticker, COUNT(*) AS days, MIN(asof) AS first, MAX(asof) AS last "
            "FROM underlying_iv_history GROUP BY ticker ORDER BY ticker"
        )
        return [dict(r) for r in cur]

    def snapshot_count(self) -> int:
        row = self.conn.execute(
            "SELECT COUNT(DISTINCT asof || ticker) AS n FROM chain_snapshots"
        ).fetchone()
        return int(row["n"])

    def equity_curve(self) -> list[tuple[str, float]]:
        cur = self.conn.execute("SELECT asof, equity FROM paper_equity ORDER BY asof")
        return [(r["asof"], float(r["equity"])) for r in cur]
