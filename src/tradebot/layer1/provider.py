"""Market data providers.

``YFinanceProvider`` is the live source. ``OfflineProvider`` replays chains
already stored in SQLite, which is what lets the test suite and CI run with no
network and makes a run reproducible after the fact -- the snapshots are not
just an audit trail, they are a replayable data source.
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Protocol, runtime_checkable

from ..store import Store
from ..types import ChainRow, OptionKind

log = logging.getLogger(__name__)

__all__ = ["MarketDataProvider", "YFinanceProvider", "OfflineProvider", "ProviderError"]


class ProviderError(RuntimeError):
    """Raised when a provider cannot serve data it was asked for."""


@runtime_checkable
class MarketDataProvider(Protocol):
    def spot(self, ticker: str) -> float | None: ...
    def chain(self, ticker: str, expiries: list[date] | None = None) -> list[ChainRow]: ...
    def sector(self, ticker: str) -> str | None: ...
    def headlines(self, ticker: str, lookback_days: int) -> list[dict]: ...
    def history_closes(self, ticker: str, lookback_days: int) -> list[tuple[date, float]]: ...


def _f(value: object) -> float | None:
    """Coerce a provider field to float, mapping NaN and junk to None."""
    if value is None:
        return None
    try:
        out = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return None if out != out else out  # NaN check


class YFinanceProvider:
    """Live provider backed by ``yfinance``.

    Imported lazily so the package stays importable (and testable) without it.
    yfinance scrapes an undocumented endpoint: fields go missing, expiries
    vanish, and calls rate-limit. Every accessor here degrades to ``None`` or
    an empty list rather than raising, because a partial book is still worth
    reporting and a crashed nightly run is not.
    """

    def __init__(self, *, max_expiries: int | None = None) -> None:
        try:
            import yfinance  # noqa: F401
        except ImportError as exc:  # pragma: no cover - env dependent
            raise ProviderError(
                "yfinance is not installed. Install it with `pip install -e .` "
                "or run with --offline to replay stored snapshots."
            ) from exc
        self.max_expiries = max_expiries
        self._cache: dict[str, object] = {}

    def _ticker(self, ticker: str):
        import yfinance as yf

        if ticker not in self._cache:
            self._cache[ticker] = yf.Ticker(ticker)
        return self._cache[ticker]

    def spot(self, ticker: str) -> float | None:
        t = self._ticker(ticker)
        try:
            fast = getattr(t, "fast_info", None)
            if fast is not None:
                for key in ("last_price", "lastPrice", "regular_market_price"):
                    px = _f(fast.get(key) if hasattr(fast, "get") else getattr(fast, key, None))
                    if px and px > 0:
                        return px
        except Exception as exc:
            log.debug("fast_info failed for %s: %s", ticker, exc)
        try:
            hist = t.history(period="5d", auto_adjust=False)
            if not hist.empty:
                return _f(hist["Close"].iloc[-1])
        except Exception as exc:
            log.warning("spot lookup failed for %s: %s", ticker, exc)
        return None

    def chain(self, ticker: str, expiries: list[date] | None = None) -> list[ChainRow]:
        t = self._ticker(ticker)
        try:
            available = [date.fromisoformat(e) for e in t.options]
        except Exception as exc:
            log.warning("no expiries for %s: %s", ticker, exc)
            return []

        wanted = [e for e in available if e in set(expiries)] if expiries else available
        if expiries:
            for miss in sorted(set(expiries) - set(available)):
                log.warning("%s: expiry %s not offered by provider", ticker, miss)
        if self.max_expiries:
            wanted = wanted[: self.max_expiries]

        rows: list[ChainRow] = []
        for exp in wanted:
            try:
                oc = t.option_chain(exp.isoformat())
            except Exception as exc:
                log.warning("chain pull failed %s %s: %s", ticker, exp, exc)
                continue
            for frame, kind in ((oc.calls, OptionKind.CALL), (oc.puts, OptionKind.PUT)):
                for rec in frame.to_dict("records"):
                    strike = _f(rec.get("strike"))
                    if strike is None or strike <= 0:
                        continue
                    rows.append(
                        ChainRow(
                            ticker=ticker,
                            expiry=exp,
                            strike=strike,
                            option_kind=kind,
                            bid=_f(rec.get("bid")),
                            ask=_f(rec.get("ask")),
                            last=_f(rec.get("lastPrice")),
                            iv=_f(rec.get("impliedVolatility")),
                            volume=_f(rec.get("volume")),
                            open_interest=_f(rec.get("openInterest")),
                        )
                    )
        return rows

    def sector(self, ticker: str) -> str | None:
        try:
            info = self._ticker(ticker).info or {}
            return info.get("sector") or None
        except Exception as exc:
            log.debug("sector lookup failed for %s: %s", ticker, exc)
            return None

    def headlines(self, ticker: str, lookback_days: int) -> list[dict]:
        try:
            raw = self._ticker(ticker).news or []
        except Exception as exc:
            log.warning("news lookup failed for %s: %s", ticker, exc)
            return []

        cutoff = datetime.now().timestamp() - lookback_days * 86400
        out: list[dict] = []
        for item in raw:
            content = item.get("content", item)
            ts = item.get("providerPublishTime")
            if ts is None:
                pub = content.get("pubDate") or content.get("displayTime")
                if pub:
                    try:
                        ts = datetime.fromisoformat(str(pub).replace("Z", "+00:00")).timestamp()
                    except ValueError:
                        ts = None
            if ts is not None and float(ts) < cutoff:
                continue
            title = content.get("title") or item.get("title")
            if not title:
                continue
            provider = content.get("provider") or {}
            out.append(
                {
                    "title": str(title),
                    "publisher": (
                        provider.get("displayName") if isinstance(provider, dict) else str(provider)
                    )
                    or item.get("publisher")
                    or "",
                    "ts": float(ts) if ts is not None else None,
                    "summary": (content.get("summary") or "")[:400],
                }
            )
        return out

    def history_closes(self, ticker: str, lookback_days: int) -> list[tuple[date, float]]:
        try:
            hist = self._ticker(ticker).history(period=f"{max(lookback_days, 5)}d", auto_adjust=False)
        except Exception as exc:
            log.warning("history failed for %s: %s", ticker, exc)
            return []
        return [(idx.date(), float(row["Close"])) for idx, row in hist.iterrows()]


class OfflineProvider:
    """Replays chains and spots already in SQLite. No network at all."""

    def __init__(self, store: Store, asof: date) -> None:
        self.store = store
        self.asof = asof

    def spot(self, ticker: str) -> float | None:
        cur = self.store.conn.execute(
            "SELECT close FROM spot_history WHERE ticker=? AND asof<=? ORDER BY asof DESC LIMIT 1",
            (ticker, self.asof.isoformat()),
        )
        row = cur.fetchone()
        return float(row["close"]) if row else None

    def chain(self, ticker: str, expiries: list[date] | None = None) -> list[ChainRow]:
        sql = "SELECT * FROM chain_snapshots WHERE ticker=? AND asof=?"
        params: list[object] = [ticker, self.asof.isoformat()]
        if expiries:
            sql += f" AND expiry IN ({','.join('?' * len(expiries))})"
            params += [e.isoformat() for e in expiries]
        return [
            ChainRow(
                ticker=r["ticker"],
                expiry=date.fromisoformat(r["expiry"]),
                strike=float(r["strike"]),
                option_kind=OptionKind(r["option_kind"]),
                bid=r["bid"],
                ask=r["ask"],
                last=r["last"],
                iv=r["iv"],
                volume=r["volume"],
                open_interest=r["open_interest"],
            )
            for r in self.store.conn.execute(sql, params)
        ]

    def sector(self, ticker: str) -> str | None:
        return None  # config lookup is the only sector source offline

    def headlines(self, ticker: str, lookback_days: int) -> list[dict]:
        return []

    def history_closes(self, ticker: str, lookback_days: int) -> list[tuple[date, float]]:
        cur = self.store.conn.execute(
            "SELECT asof, close FROM spot_history WHERE ticker=? AND asof<=? "
            "AND asof>=date(?, ?) ORDER BY asof",
            (ticker, self.asof.isoformat(), self.asof.isoformat(), f"-{int(lookback_days)} days"),
        )
        return [(date.fromisoformat(r["asof"]), float(r["close"])) for r in cur]
