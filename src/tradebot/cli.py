"""Command-line entry point.

    tradebot run          # all layers, write reports
    tradebot run --offline --no-news
    tradebot value        # layer 1 only
    tradebot macro        # layer 3a only (deterministic, free)
    tradebot paper ...    # paper account operations
    tradebot usage        # what the LLM layer has cost so far
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date
from typing import Any

from .config import load_config
from .layer1.provider import OfflineProvider, ProviderError, YFinanceProvider
from .layer1.snapshots import diff_against_previous, write_snapshot
from .layer1.valuation import value_book
from .layer2.analytics import analyse
from .layer3.macro import compute_macro_gate
from .layer3.news import analyse_news
from .paper.book import PaperBook
from .paper.broker import OrderRejected, PaperBroker
from .paper.performance import compute_performance, history_progress
from .portfolio import load_portfolio, load_sectors
from .report.render import growth_math, render_markdown, write_reports
from .store import Store
from .types import AssetKind, Holding, OptionKind, today_utc, utcnow

log = logging.getLogger("tradebot")


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("yfinance").setLevel(logging.ERROR)
    logging.getLogger("peewee").setLevel(logging.ERROR)


def _build_provider(args: argparse.Namespace, store: Store, asof: date) -> Any:
    if args.offline:
        log.info("offline mode: replaying stored snapshots")
        return OfflineProvider(store, asof)
    try:
        return YFinanceProvider()
    except ProviderError as exc:
        log.error("%s", exc)
        sys.exit(2)


def cmd_run(args: argparse.Namespace) -> int:
    """Full pipeline: value the book, analyse it, read the environment, report."""
    cfg = load_config(args.config, root=args.root)
    asof = date.fromisoformat(args.asof) if args.asof else today_utc()

    if args.no_news:
        cfg.news.enabled = False
    if args.news:
        cfg.news.enabled = True
    if args.no_macro:
        cfg.macro.enabled = False

    sectors = load_sectors(cfg.path("sectors"))

    with Store(cfg.path("database")) as store:
        holdings, book_source = _resolve_book(args.book, cfg, store)
        if not holdings:
            log.warning("book is empty (source: %s); nothing to value", book_source)
        else:
            log.info("valuing %d position(s) from the %s book", len(holdings), book_source)
        provider = _build_provider(args, store, asof)
        tickers = sorted({h.ticker for h in holdings})

        # ---- Layer 1: pull, snapshot, mark ------------------------------
        spots: dict[str, float | None] = {}
        chains: dict[str, list] = {}
        diffs: list[dict[str, Any]] = []
        for ticker in tickers:
            spots[ticker] = provider.spot(ticker)
            wanted = sorted({h.expiry for h in holdings if h.ticker == ticker and h.expiry})
            rows = provider.chain(ticker, wanted or None)
            chains[ticker] = rows
            if rows and not args.offline:
                write_snapshot(cfg.path("snapshots_dir"), store, ticker, asof, rows, spots[ticker])
            diffs.append(diff_against_previous(store, ticker, asof).to_json())

        valuation = value_book(holdings, chains, spots, cfg, asof, store)
        val_json = valuation.to_json()
        store.save_run(asof, "layer1", val_json, utcnow().isoformat())

        # ---- Layer 2: analytics -----------------------------------------
        analytics = analyse(valuation, store, cfg, sectors, provider)
        ana_json = analytics.to_json()
        store.save_run(asof, "layer2", ana_json, utcnow().isoformat())

        # ---- Layer 3: environment ---------------------------------------
        macro_json = None
        if cfg.macro.enabled:
            gate = compute_macro_gate(provider, cfg, asof)
            macro_json = gate.to_json()
            store.save_macro(asof, gate.score, gate.regime, gate.components)
            store.save_run(asof, "layer3_macro", macro_json, utcnow().isoformat())

        news = analyse_news(provider, tickers, cfg, store, asof)
        news_json = news.to_json()
        store.save_run(asof, "layer3_news", news_json, utcnow().isoformat())

        # ---- Paper account ------------------------------------------------
        broker = PaperBroker(cfg, store)
        paper_json = broker.mark_to_market(valuation.total_value, asof)
        paper_json["book_source"] = book_source
        perf = compute_performance(
            store.equity_curve(), store.closed_trades(), cfg.valuation.risk_free_rate
        )
        paper_json["performance"] = perf.to_json()

        growth = growth_math(cfg.paper.starting_cash, args.target, cfg.paper.currency)
        payload = {
            "asof": asof.isoformat(),
            "generated_at": utcnow().isoformat(),
            "layer1_valuation": val_json,
            "layer2_analytics": ana_json,
            "layer3_macro": macro_json,
            "layer3_news": news_json,
            "chain_diffs": diffs,
            "paper_account": paper_json,
            "growth_target": growth,
        }
        markdown = render_markdown(val_json, ana_json, macro_json, news_json, diffs, paper_json, growth)
        md_path, json_path = write_reports(cfg.path("reports_dir"), asof, markdown, payload)

    if args.json:
        print(json.dumps(payload, indent=2, default=str))
    else:
        print(markdown)
    log.info("wrote %s and %s", md_path.name, json_path.name)
    return 0


def _resolve_book(choice: str, cfg: Any, store: Store) -> tuple[list[Holding], str]:
    """Pick between the simulated paper book and the declared portfolio file.

    Default is ``auto``: the paper book when it holds anything, the file
    otherwise. In paper-only operation the paper book is the live one, and
    silently valuing a stale YAML file instead would be the wrong answer.
    """
    book = PaperBook(store)
    if choice == "file":
        return load_portfolio(cfg.path("portfolio")), "file"
    if choice == "paper":
        return book.holdings(), "paper"
    if not book.is_empty():
        return book.holdings(), "paper"
    try:
        return load_portfolio(cfg.path("portfolio")), "file"
    except FileNotFoundError:
        return [], "paper (empty)"


def _holding_from_args(args: argparse.Namespace, store: Store) -> Holding:
    """Build the Holding for a trade, reusing an open position's terms if any."""
    existing = store.get_position(args.symbol)
    if existing is not None:
        return Holding(
            ticker=existing["ticker"],
            kind=AssetKind(existing["kind"]),
            qty=args.qty,
            cost_basis=existing["avg_price"],
            option_kind=OptionKind(existing["option_kind"]) if existing["option_kind"] else None,
            strike=existing["strike"],
            expiry=date.fromisoformat(existing["expiry"]) if existing["expiry"] else None,
            target=existing["target"],
            stop=existing["stop"],
            multiplier=int(existing["multiplier"]),
        )

    if args.shares:
        return Holding(args.symbol.upper(), AssetKind.SHARES, args.qty, args.price,
                       target=args.target, stop=args.stop, multiplier=1)

    missing = [f for f, v in (("--strike", args.strike), ("--expiry", args.expiry),
                              ("--type", args.type), ("--ticker", args.ticker)) if not v]
    if missing:
        raise ValueError(
            f"opening a new option position needs {', '.join(missing)} "
            "(or pass --shares for an equity position)"
        )
    return Holding(
        ticker=args.ticker.upper(),
        kind=AssetKind.OPTION,
        qty=args.qty,
        cost_basis=args.price,
        option_kind=OptionKind(args.type),
        strike=args.strike,
        expiry=date.fromisoformat(args.expiry),
        target=args.target,
        stop=args.stop,
    )


def cmd_trade(args: argparse.Namespace) -> int:
    """Record a simulated buy or sell against the paper book."""
    cfg = load_config(args.config, root=args.root)
    asof = date.fromisoformat(args.asof) if args.asof else today_utc()
    with Store(cfg.path("database")) as store:
        broker = PaperBroker(cfg, store)
        holding = _holding_from_args(args, store)
        fn = broker.buy if args.command == "buy" else broker.sell
        fill = fn(holding, abs(args.qty), mark=args.price, bid=args.bid, ask=args.ask,
                  reason=args.reason, asof=asof)
        out = fill.to_json()
        out["cash_after"] = round(broker.cash, 2)
    print(json.dumps(out, indent=2))
    return 0


def cmd_close(args: argparse.Namespace) -> int:
    """Close an open paper position in full at the given price."""
    cfg = load_config(args.config, root=args.root)
    asof = date.fromisoformat(args.asof) if args.asof else today_utc()
    with Store(cfg.path("database")) as store:
        existing = store.get_position(args.symbol)
        if existing is None:
            log.error("no open paper position for %r", args.symbol)
            return 1
        broker = PaperBroker(cfg, store)
        args.qty = abs(existing["qty"])
        holding = _holding_from_args(args, store)
        # A long position is closed by selling; a short by buying it back.
        fn = broker.sell if existing["qty"] > 0 else broker.buy
        fill = fn(holding, args.qty, mark=args.price, bid=args.bid, ask=args.ask,
                  reason=args.reason or "close", asof=asof)
        out = fill.to_json()
        out["cash_after"] = round(broker.cash, 2)
    print(json.dumps(out, indent=2))
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    """Where the paper run stands: the book, performance, history accumulated."""
    cfg = load_config(args.config, root=args.root)
    with Store(cfg.path("database")) as store:
        broker = PaperBroker(cfg, store)
        curve = store.equity_curve()
        perf = compute_performance(curve, store.closed_trades(), cfg.valuation.risk_free_rate)
        progress = history_progress(
            store.history_coverage(), cfg.iv_env.min_history_days, cfg.iv_env.rank_lookback_days
        )
        payload = {
            "mode": cfg.execution.mode,
            "currency": cfg.paper.currency,
            "starting_cash": cfg.paper.starting_cash,
            "cash": round(broker.cash, 2),
            "open_positions": store.all_positions(),
            "equity_points": len(curve),
            "latest_equity": curve[-1][1] if curve else None,
            "performance": perf.to_json(),
            "history_progress": progress,
            "snapshots_stored": store.snapshot_count(),
        }
        tokens_in, tokens_out = store.llm_tokens_total()
        payload["llm_spend"] = {"input_tokens": tokens_in, "output_tokens": tokens_out}
    print(json.dumps(payload, indent=2, default=str))
    return 0


def cmd_value(args: argparse.Namespace) -> int:
    """Layer 1 only."""
    cfg = load_config(args.config, root=args.root)
    asof = date.fromisoformat(args.asof) if args.asof else today_utc()
    holdings = load_portfolio(cfg.path("portfolio"))
    with Store(cfg.path("database")) as store:
        provider = _build_provider(args, store, asof)
        spots, chains = {}, {}
        for ticker in sorted({h.ticker for h in holdings}):
            spots[ticker] = provider.spot(ticker)
            wanted = sorted({h.expiry for h in holdings if h.ticker == ticker and h.expiry})
            chains[ticker] = provider.chain(ticker, wanted or None)
        result = value_book(holdings, chains, spots, cfg, asof, store)
    print(json.dumps(result.to_json(), indent=2, default=str))
    return 0


def cmd_macro(args: argparse.Namespace) -> int:
    """Layer 3a only -- deterministic and free."""
    cfg = load_config(args.config, root=args.root)
    asof = date.fromisoformat(args.asof) if args.asof else today_utc()
    with Store(cfg.path("database")) as store:
        provider = _build_provider(args, store, asof)
        gate = compute_macro_gate(provider, cfg, asof)
        store.save_macro(asof, gate.score, gate.regime, gate.components)
    print(json.dumps(gate.to_json(), indent=2, default=str))
    return 0


def cmd_paper(args: argparse.Namespace) -> int:
    """Inspect the paper account or price up a hypothetical order."""
    cfg = load_config(args.config, root=args.root)
    with Store(cfg.path("database")) as store:
        broker = PaperBroker(cfg, store)
        if args.check_affordable:
            holdings = {h.occ_symbol(): h for h in load_portfolio(cfg.path("portfolio"))}
            holding = holdings.get(args.check_affordable)
            if holding is None:
                log.error("symbol %r not in portfolio. Known: %s", args.check_affordable, ", ".join(holdings))
                return 1
            report = broker.affordability(holding, args.qty, args.mark)
            print(json.dumps(report, indent=2))
            if not report["affordable"]:
                print(
                    f"\nNot affordable: needs {report['total_cost']:.2f} {cfg.paper.currency}, "
                    f"have {report['cash_available']:.2f}, short {report['shortfall']:.2f}.",
                    file=sys.stderr,
                )
                return 1
            return 0

        curve = store.equity_curve()
        print(
            json.dumps(
                {
                    "starting_cash": cfg.paper.starting_cash,
                    "currency": cfg.paper.currency,
                    "current_cash": round(broker.cash, 2),
                    "equity_points": len(curve),
                    "equity_curve": curve[-30:],
                },
                indent=2,
            )
        )
    return 0


def cmd_usage(args: argparse.Namespace) -> int:
    """What the LLM layer has cost so far."""
    from .layer3.news import estimate_cost_usd

    cfg = load_config(args.config, root=args.root)
    with Store(cfg.path("database")) as store:
        tokens_in, tokens_out = store.llm_tokens_total()
        rows = store.conn.execute(
            "SELECT asof, COUNT(*) n, SUM(input_tokens) i, SUM(output_tokens) o "
            "FROM llm_usage GROUP BY asof ORDER BY asof DESC LIMIT 30"
        ).fetchall()
    cost = estimate_cost_usd(cfg.news.model, tokens_in, tokens_out)
    print(
        json.dumps(
            {
                "model": cfg.news.model,
                "news_layer_enabled": cfg.news.enabled,
                "total_input_tokens": tokens_in,
                "total_output_tokens": tokens_out,
                "estimated_total_cost_usd": None if cost is None else round(cost, 4),
                "by_day": [
                    {"asof": r["asof"], "calls": r["n"], "in": r["i"], "out": r["o"]} for r in rows
                ],
            },
            indent=2,
        )
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="tradebot", description="Layered options portfolio analytics.")
    p.add_argument("--config", help="path to config.yaml")
    p.add_argument("--root", help="repo root (defaults to the package's parent)")
    p.add_argument("--asof", help="run date, YYYY-MM-DD (default: today UTC)")
    p.add_argument("--offline", action="store_true", help="replay stored snapshots; no network")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="run all layers and write reports")
    run.add_argument("--json", action="store_true", help="print JSON instead of Markdown")
    run.add_argument("--news", action="store_true", help="force the Claude news layer on")
    run.add_argument("--no-news", action="store_true", help="force the Claude news layer off")
    run.add_argument("--no-macro", action="store_true", help="skip the macro gate")
    run.add_argument("--target", type=float, default=400_000.0, help="growth target for the reality check")
    run.add_argument("--book", choices=["auto", "paper", "file"], default="auto",
                     help="which book to value (default: paper if it has positions)")
    run.set_defaults(func=cmd_run)

    for verb in ("buy", "sell"):
        t = sub.add_parser(verb, help=f"record a simulated {verb} (paper only)")
        t.add_argument("symbol", help="OCC symbol of an open position, or a ticker when opening")
        t.add_argument("--qty", type=float, required=True, help="contracts, or shares with --shares")
        t.add_argument("--price", type=float, required=True, help="premium per share (not x100)")
        t.add_argument("--bid", type=float, help="for realistic spread-crossing slippage")
        t.add_argument("--ask", type=float)
        t.add_argument("--shares", action="store_true", help="equity rather than an option")
        t.add_argument("--ticker", help="underlying, when opening a new option position")
        t.add_argument("--type", choices=["call", "put"])
        t.add_argument("--strike", type=float)
        t.add_argument("--expiry", help="YYYY-MM-DD")
        t.add_argument("--target", type=float)
        t.add_argument("--stop", type=float)
        t.add_argument("--reason", default="")
        t.set_defaults(func=cmd_trade)

    cl = sub.add_parser("close", help="close an open paper position in full")
    cl.add_argument("symbol")
    cl.add_argument("--price", type=float, required=True)
    cl.add_argument("--bid", type=float)
    cl.add_argument("--ask", type=float)
    cl.add_argument("--reason", default="")
    cl.set_defaults(func=cmd_close, shares=False, ticker=None, type=None,
                    strike=None, expiry=None, target=None, stop=None)

    st = sub.add_parser("status", help="paper book, performance, and history progress")
    st.set_defaults(func=cmd_status)

    val = sub.add_parser("value", help="layer 1 only: mark the book")
    val.set_defaults(func=cmd_value)

    mac = sub.add_parser("macro", help="layer 3a only: the deterministic macro gate")
    mac.set_defaults(func=cmd_macro)

    pap = sub.add_parser("paper", help="paper account status and order pricing")
    pap.add_argument("--check-affordable", metavar="OCC_SYMBOL", help="price up a hypothetical buy")
    pap.add_argument("--qty", type=float, default=1.0)
    pap.add_argument("--mark", type=float, default=1.0, help="quoted premium per share")
    pap.set_defaults(func=cmd_paper)

    use = sub.add_parser("usage", help="LLM token spend to date")
    use.set_defaults(func=cmd_usage)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _setup_logging(args.verbose)
    try:
        return args.func(args)
    except (FileNotFoundError, ValueError, OrderRejected) as exc:
        log.error("%s", exc)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
