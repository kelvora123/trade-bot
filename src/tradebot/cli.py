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
from .paper.broker import OrderRejected, PaperBroker
from .portfolio import load_portfolio, load_sectors
from .report.render import growth_math, render_markdown, write_reports
from .store import Store
from .types import today_utc, utcnow

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

    holdings = load_portfolio(cfg.path("portfolio"))
    sectors = load_sectors(cfg.path("sectors"))
    if not holdings:
        log.warning("portfolio is empty; nothing to value")

    with Store(cfg.path("database")) as store:
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
    run.set_defaults(func=cmd_run)

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
