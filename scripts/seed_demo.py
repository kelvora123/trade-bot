#!/usr/bin/env python3
"""Generate a realistic demo dataset so the dashboard has real numbers to show.

Builds a plausible multi-sector book with option and equity legs, a year of
ATM-IV history per underlying, a paper equity curve with a genuine drawdown,
and a mix of winning and losing closed trades. Everything downstream -- IV
rank, concentration flags, expiry watch, drawdown, profit factor -- is then
computed by the real code paths rather than invented.

    python scripts/seed_demo.py --root /tmp/demo
"""

from __future__ import annotations

import argparse
import math
import random
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tradebot.config import load_config  # noqa: E402
from tradebot.layer1.snapshots import write_snapshot  # noqa: E402
from tradebot.store import Store  # noqa: E402
from tradebot.types import ChainRow, OptionKind  # noqa: E402

ASOF = date(2026, 9, 18)
RNG = random.Random(20260918)  # fixed seed: the demo is reproducible

# ticker -> (spot, base annual vol, sector)
UNIVERSE = {
    "NVDA": (184.50, 0.44, "Technology"),
    "AAPL": (238.20, 0.26, "Technology"),
    "JPM": (312.40, 0.22, "Financial Services"),
    "XOM": (118.75, 0.28, "Energy"),
}

# (ticker, kind, option_kind, strike, expiry, qty, basis, target, stop)
BOOK = [
    ("NVDA", "option", OptionKind.CALL, 180.0, date(2026, 12, 18), 3, 14.20, 28.00, 7.00),
    ("NVDA", "option", OptionKind.PUT, 165.0, date(2026, 10, 16), -2, 4.10, 1.00, 9.00),
    ("AAPL", "option", OptionKind.CALL, 230.0, date(2026, 10, 16), 2, 11.80, 24.00, 6.00),
    ("JPM", "shares", None, None, None, 12, 288.00, 360.00, 260.00),
    ("XOM", "option", OptionKind.CALL, 115.0, date(2026, 11, 20), 4, 6.40, 13.00, 3.20),
]


def smile(spot: float, strike: float, base_vol: float, dte: int) -> float:
    """A plausible vol smile: OTM puts bid up, short dated steeper."""
    m = math.log(strike / spot)
    skew = -0.55 * m                       # puts richer than calls
    curve = 1.9 * m * m                    # both wings lift
    term = 0.14 * (30.0 / max(dte, 7)) ** 0.5
    return max(0.08, base_vol + skew + curve + term - 0.14)


def build_chain(
    ticker: str, spot: float, base_vol: float, expiry: date, asof: date,
    must_include: set[float] | None = None,
) -> list[ChainRow]:
    dte = max((expiry - asof).days, 1)
    t = dte / 365.0
    rows: list[ChainRow] = []
    step = round(spot * 0.025, 0) or 1.0
    strikes = {round((round(spot / step) + i) * step, 2) for i in range(-6, 7)}
    # A real chain always contains the strike you hold; without this the held
    # contract cannot be marked and the position shows as stale.
    strikes |= (must_include or set())
    for strike in sorted(strikes):
        if strike <= 0:
            continue
        for kind in (OptionKind.CALL, OptionKind.PUT):
            iv = smile(spot, strike, base_vol, dte)
            # Rough but well-behaved premium: intrinsic plus time value.
            intrinsic = max(spot - strike, 0) if kind is OptionKind.CALL else max(strike - spot, 0)
            decay = math.exp(-((math.log(strike / spot)) ** 2) / (2 * iv * iv * t))
            tv = spot * iv * math.sqrt(t) * 0.39 * decay
            mid = max(0.02, intrinsic + tv)
            spread = max(0.02, mid * 0.025)
            rows.append(
                ChainRow(
                    ticker=ticker, expiry=expiry, strike=strike, option_kind=kind,
                    bid=round(mid - spread / 2, 2), ask=round(mid + spread / 2, 2),
                    last=round(mid, 2), iv=round(iv, 4),
                    volume=RNG.randint(20, 4000), open_interest=RNG.randint(100, 30000),
                )
            )
    return rows


def _book_value(cfg, store) -> float:
    """Mark the seeded book with the real valuation code, offline."""
    from tradebot.layer1.provider import OfflineProvider
    from tradebot.layer1.valuation import value_book
    from tradebot.paper.book import PaperBook

    provider = OfflineProvider(store, ASOF)
    holdings = PaperBook(store).holdings()
    spots, chains = {}, {}
    for t in {h.ticker for h in holdings}:
        spots[t] = provider.spot(t)
        chains[t] = provider.chain(t)
    return value_book(holdings, chains, spots, cfg, ASOF).total_value


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--days", type=int, default=120, help="days of history to synthesise")
    args = ap.parse_args()

    root = Path(args.root)
    (root / "config").mkdir(parents=True, exist_ok=True)
    repo = Path(__file__).resolve().parents[1]
    for f in ("config.yaml", "sectors.yaml"):
        (root / "config" / f).write_text((repo / "config" / f).read_text())

    cfg = load_config(root=root)
    cfg.paper.starting_cash = 25_000.0

    with Store(cfg.path("database")) as store:
        # --- history: ATM IV and spot, walked with mean reversion ----------
        for ticker, (spot, base_vol, _) in UNIVERSE.items():
            vol, px = base_vol, spot * 0.88
            for i in range(args.days):
                d = ASOF - timedelta(days=args.days - i)
                vol += (base_vol - vol) * 0.06 + RNG.gauss(0, 0.018)
                vol = max(0.10, min(1.2, vol))
                px *= 1 + RNG.gauss(0.0012, vol / math.sqrt(252))
                store.record_underlying_iv(d, ticker, round(vol, 4), round(px, 2))
                store.record_spot(d, ticker, round(px, 2))
            store.record_spot(ASOF, ticker, spot)

        # --- today's chains -------------------------------------------------
        expiries = sorted({e for _, _, _, _, e, *_ in BOOK if e})
        for ticker, (spot, base_vol, _) in UNIVERSE.items():
            held = {k for tk, _, _, k, _, *_ in BOOK if tk == ticker and k}
            rows: list[ChainRow] = []
            for exp in expiries:
                rows += build_chain(ticker, spot, base_vol, exp, ASOF, must_include=held)
            # Yesterday's snapshot too, so the chain diff has a baseline.
            write_snapshot(cfg.path("snapshots_dir"), store, ticker,
                           ASOF - timedelta(days=1), rows[:-2], spot * 0.995)
            write_snapshot(cfg.path("snapshots_dir"), store, ticker, ASOF, rows, spot)
            store.record_underlying_iv(ASOF, ticker, base_vol, spot)

        # --- the paper book --------------------------------------------------
        for ticker, kind, ok, strike, expiry, qty, basis, target, stop in BOOK:
            store.upsert_position(
                symbol=(f"{ticker}{expiry:%y%m%d}{'C' if ok is OptionKind.CALL else 'P'}"
                        f"{int(round(strike * 1000)):08d}" if kind == "option" else ticker),
                ticker=ticker, kind=kind,
                option_kind=ok.value if ok else None, strike=strike,
                expiry=expiry.isoformat() if expiry else None,
                qty=qty, avg_price=basis, multiplier=100 if kind == "option" else 1,
                target=target, stop=stop,
                opened_at=(ASOF - timedelta(days=RNG.randint(20, 80))).isoformat(),
                realised_pnl=0.0, entry_costs=abs(qty) * 0.65 if kind == "option" else 0.0,
            )

        # --- closed trades: a realistic mix, not a winning streak ------------
        closed = [
            ("AMD261016C00160000", "AMD", 2, 8.40, 14.85, 1_282.7, "target"),
            ("MSFT260918C00520000", "MSFT", 1, 12.10, 4.35, -777.6, "stop"),
            ("NVDA260821C00175000", "NVDA", 2, 9.75, 17.20, 1_487.4, "target"),
            ("TSLA260717P00300000", "TSLA", 3, 6.20, 2.05, -1_247.0, "stop"),
            ("JPM260619C00280000", "JPM", 1, 10.50, 19.95, 1_943.4, "target"),
            ("XOM260515C00110000", "XOM", 4, 3.85, 2.10, -702.6, "time stop"),
            ("AAPL260417C00220000", "AAPL", 2, 9.20, 13.60, 878.7, "partial"),
            ("SPY260320P00560000", "SPY", 1, 7.40, 11.85, 443.7, "target"),
            ("NVDA260220C00140000", "NVDA", 1, 15.30, 9.10, -621.3, "stop"),
            ("META260116C00600000", "META", 1, 18.60, 27.40, 878.7, "target"),
            ("GOOGL251219C00185000", "GOOGL", 2, 7.10, 5.25, -372.6, "stop"),
            ("AMZN251121C00230000", "AMZN", 2, 8.90, 12.40, 698.7, "target"),
        ]
        for i, (sym, tk, qty, entry, exit_, pnl, reason) in enumerate(closed):
            opened = ASOF - timedelta(days=200 - i * 14)
            store.record_trade(
                symbol=sym, ticker=tk, opened_at=opened.isoformat(),
                closed_at=(opened + timedelta(days=RNG.randint(9, 45))).isoformat(),
                qty=qty, entry_price=entry, exit_price=exit_, multiplier=100,
                pnl=pnl, costs=qty * 1.30, reason=reason,
            )

        # --- equity curve ----------------------------------------------------
        # Built backwards from the book's real mark-to-market value, so the
        # final day joins the curve smoothly. Seeding it forwards leaves the
        # last point disagreeing with the actual positions, which shows up as
        # an impossible one-day move.
        book_value = _book_value(cfg, store)
        path = [25_000.0]
        for i in range(args.days):
            drift = 0.0018 if not (58 < i < 78) else -0.0075   # one drawdown stretch
            path.append(path[-1] * (1 + drift + RNG.gauss(0, 0.008)))

        # Scale the walk so it ends at a chosen equity, then derive the cash
        # balance that makes that equity true given the real positions.
        target_final = 31_500.0
        scale = target_final / path[-1]
        for i in range(args.days):
            d = ASOF - timedelta(days=args.days - i)
            eq = path[i] * scale
            store.record_equity(d, round(eq * 0.42, 2), round(eq * 0.58, 2), round(eq, 2))
        store.save_cash(round(target_final - book_value, 2), ASOF.isoformat())
        print(f"book marks at {book_value:,.2f}; cash set so equity lands at {target_final:,.2f}")

    print(f"seeded {args.days} days of history into {cfg.path('database')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
