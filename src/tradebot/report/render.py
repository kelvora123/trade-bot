"""Render a run into Markdown for humans and JSON for n8n."""

from __future__ import annotations

import json
import math
from datetime import date
from pathlib import Path
from typing import Any

__all__ = ["render_markdown", "write_reports", "growth_math"]


def _pct(x: float | None, digits: int = 1) -> str:
    return "n/a" if x is None else f"{x:.{digits}f}%"


def _money(x: float | None, digits: int = 2) -> str:
    return "n/a" if x is None else f"{x:,.{digits}f}"


def growth_math(start: float, target: float, currency: str = "GBP") -> dict[str, Any]:
    """What a start -> target goal actually requires, compounded.

    Included because a growth target is a statement about a required monthly
    return, and that number is the one fact that decides whether a plan is a
    plan or a wish. Reporting it every run keeps it in view.
    """
    if start <= 0 or target <= start:
        return {"applicable": False}

    multiple = target / start
    horizons = {}
    for label, months in (("1 year", 12), ("2 years", 24), ("5 years", 60), ("10 years", 120)):
        horizons[label] = round((multiple ** (1.0 / months) - 1.0) * 100, 2)

    # Months required at a few reference rates. 2%/month is a strong retail
    # result sustained; 5% is exceptional; 10% is not sustained by anyone.
    months_needed = {
        f"{rate}%/month": round(math.log(multiple) / math.log(1 + rate / 100), 1)
        for rate in (2, 5, 10, 20)
    }
    return {
        "applicable": True,
        "currency": currency,
        "start": start,
        "target": target,
        "multiple_required": round(multiple, 1),
        "monthly_return_required_pct": horizons,
        "months_required_at_rate": months_needed,
    }


def render_markdown(
    valuation: dict[str, Any],
    analytics: dict[str, Any],
    macro: dict[str, Any] | None,
    news: dict[str, Any] | None,
    diffs: list[dict[str, Any]] | None = None,
    paper: dict[str, Any] | None = None,
    growth: dict[str, Any] | None = None,
) -> str:
    asof = valuation.get("asof", "unknown")
    t = valuation.get("totals", {})
    out: list[str] = [
        f"# Portfolio run - {asof}",
        "",
        "> Informational only. This report surfaces facts about the book and the market",
        "> environment. It does not recommend trades.",
        "",
        "## Valuation",
        "",
        f"- Current value: **{_money(t.get('current_value'))}**",
        f"- Cost basis: {_money(t.get('cost_value'))}",
        f"- Unrealised P&L: **{_money(t.get('unrealised_pnl'))}** ({_pct(t.get('unrealised_pnl_pct'), 2)})",
        "",
    ]

    positions = valuation.get("positions", [])
    if positions:
        out += [
            "| Symbol | Qty | Mark | Value | P&L | P&L % | DTE | IV | -> Target | -> Stop |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
        for p in positions:
            iv = p.get("iv")
            out.append(
                f"| `{p['symbol']}` | {p['qty']:g} | "
                f"{_money(p.get('mark'))} | "
                f"{_money(p.get('current_value'))} | {_money(p.get('unrealised_pnl'))} | "
                f"{_pct(p.get('unrealised_pnl_pct'), 2)} | "
                f"{p.get('dte') if p.get('dte') is not None else '-'} | "
                f"{_pct(iv * 100 if iv else None)} | {_pct(p.get('progress_to_target_pct'))} | "
                f"{_pct(p.get('progress_to_stop_pct'))} |"
            )
        out.append("")

    warn = valuation.get("warnings") or []
    if warn:
        out += ["### Data warnings", ""] + [f"- {w}" for w in warn] + [""]

    # ---------------------------------------------------------- analytics
    out += ["## Portfolio analytics", ""]
    alloc = analytics.get("allocation", {})
    by_ticker = alloc.get("by_ticker_pct", {})
    if by_ticker:
        out.append("**By ticker:** " + ", ".join(f"{k} {v:.1f}%" for k, v in by_ticker.items()))
    by_sector = alloc.get("by_sector_pct", {})
    if by_sector:
        out.append("**By sector:** " + ", ".join(f"{k} {v:.1f}%" for k, v in by_sector.items()))
    out.append("")

    flags = alloc.get("concentration_flags") or []
    if flags:
        out += ["### Concentration flags (informational)", ""] + [f"- {f['message']}" for f in flags] + [""]

    g = analytics.get("aggregate_greeks", {})
    if g:
        out += [
            "### Aggregate exposures",
            "",
            f"- Net delta: **{g.get('net_delta_shares', 0):,.1f}** share-equivalents "
            f"({_money(g.get('net_delta_dollars'))} notional)",
            f"- Daily theta: **{_money(g.get('daily_theta_dollars'))}/day** "
            "(what the book loses per day if nothing moves)",
            f"- Net vega: **{_money(g.get('net_vega_dollars_per_iv_point'))}** per 1 IV point",
            f"- Net gamma: {g.get('net_gamma_shares_per_point', 0):,.4f} delta per 1 point of spot",
            "",
        ]

    iv_env = analytics.get("iv_environment") or []
    if iv_env:
        out += [
            "### IV environment",
            "",
            "| Symbol | IV | IV rank | Percentile | Status |",
            "|---|---:|---:|---:|---|",
        ]
        for e in iv_env:
            out.append(
                f"| `{e['symbol']}` | {e['current_iv_pct']:.1f}% | "
                f"{e['iv_rank'] if e.get('iv_rank') is not None else '-'} | "
                f"{e['iv_percentile'] if e.get('iv_percentile') is not None else '-'} | "
                f"{e['status']} |"
            )
        building = [e for e in iv_env if e.get("status") == "building history"]
        if building:
            out += ["", f"_{len(building)} contract(s) still building history -- {building[0]['note']}_"]
        out.append("")

    watch = analytics.get("expiry_watch") or []
    if watch:
        out += [
            f"### Expiry watch (<= {watch[0]['threshold_days']} DTE)",
            "",
            "_Facts only; this report does not advise rolling._",
            "",
        ] + [f"- {e['fact']}" for e in watch] + [""]

    for note in analytics.get("notes") or []:
        out.append(f"> {note}")
    if analytics.get("notes"):
        out.append("")

    # -------------------------------------------------------------- diffs
    changed = [d for d in (diffs or []) if d.get("new_strike_count") or d.get("new_expiries")]
    if changed:
        out += ["## Chain changes since last run", ""]
        for d in changed:
            bits = []
            if d.get("new_strike_count"):
                bits.append(f"{d['new_strike_count']} new strike(s)")
            if d.get("new_expiries"):
                bits.append(f"new expiries: {', '.join(d['new_expiries'])}")
            out.append(f"- **{d['ticker']}** (vs {d.get('baseline_date')}): {'; '.join(bits)}")
        out.append("")

    # -------------------------------------------------------------- macro
    if macro:
        out += [
            "## Macro gate",
            "",
            f"**{macro.get('score')}/100 - {macro.get('regime')}**  (0 = stressed, 100 = calm)",
            "",
        ]
        comps = macro.get("components", {})
        if comps:
            out += ["| Component | Value | Score | Weight |", "|---|---:|---:|---:|"]
            for name, c in comps.items():
                out.append(
                    f"| {name.replace('_', ' ')} | {c.get('value')} | {c.get('score')} | {c.get('weight')} |"
                )
            out.append("")
        if macro.get("missing_inputs"):
            out += [f"_Computed without: {', '.join(macro['missing_inputs'])}._", ""]

    # --------------------------------------------------------------- news
    if news:
        out += ["## News", ""]
        if not news.get("enabled"):
            out += ["_News layer disabled. No API calls made._", ""]
        else:
            for n in news.get("per_ticker") or []:
                flag = " **[affects a position]**" if n.get("position_relevant") else ""
                out.append(f"### {n['ticker']} - {n['sentiment']}{flag}")
                out.append("")
                out.append(n.get("summary", ""))
                if n.get("key_drivers"):
                    out += [""] + [f"- {d}" for d in n["key_drivers"]]
                out.append("")
            u = news.get("usage", {})
            out += [
                f"_API calls: {u.get('api_calls', 0)}"
                + (" (served from cache)" if u.get("served_from_cache") else "")
                + f", tokens in/out: {u.get('input_tokens', 0)}/{u.get('output_tokens', 0)}"
                + (
                    f", est. cost ${u['estimated_cost_usd']:.5f}"
                    if u.get("estimated_cost_usd") is not None
                    else ""
                )
                + "._",
                "",
            ]
        for note in news.get("notes") or []:
            out.append(f"> {note}")
        out.append("")

    # -------------------------------------------------------------- paper
    if paper:
        out += [
            "## Paper account",
            "",
            f"- Cash: {_money(paper.get('cash'))} {paper.get('currency', '')}",
            f"- Positions: {_money(paper.get('positions_value'))} "
            f"({paper.get('open_positions', 0)} open)",
            f"- **Equity: {_money(paper.get('equity'))}**",
            f"- Realised to date: {_money(paper.get('realised_pnl_to_date'))}",
            "",
        ]
        perf = paper.get("performance") or {}
        eq, tr = perf.get("equity", {}), perf.get("trades", {})
        if eq.get("days_tracked", 0) >= 2:
            out += [
                "### Performance",
                "",
                f"- Total return: **{_pct(eq.get('total_return_pct'), 2)}** "
                f"over {eq.get('days_tracked')} days tracked",
                f"- Max drawdown: **{_pct(eq.get('max_drawdown_pct'), 2)}** "
                f"({_money(eq.get('max_drawdown_amount'))})",
                f"- Best / worst day: {_pct(eq.get('best_day_pct'), 2)} / "
                f"{_pct(eq.get('worst_day_pct'), 2)}",
            ]
            if eq.get("annualised_return_pct") is not None:
                out.append(f"- Annualised: {_pct(eq.get('annualised_return_pct'), 2)}")
            if eq.get("sharpe") is not None:
                out.append(f"- Sharpe: {eq['sharpe']:.2f}")
            out.append("")
        if tr.get("closed"):
            out += [
                f"- Closed trades: **{tr['closed']}** "
                f"({tr.get('wins', 0)}W / {tr.get('losses', 0)}L, "
                f"win rate {_pct(tr.get('win_rate_pct'))})",
                f"- Profit factor: "
                f"{tr['profit_factor']:.2f}" if tr.get("profit_factor") is not None
                else "- Profit factor: n/a",
                f"- Avg win / avg loss: {_money(tr.get('avg_win'))} / {_money(tr.get('avg_loss'))}",
                "",
            ]
        for c in perf.get("caveats") or []:
            out.append(f"> {c}")
        if perf.get("caveats"):
            out.append("")

    if growth and growth.get("applicable"):
        out += [
            "## Growth target reality check",
            "",
            f"Turning {growth['start']:,.0f} into {growth['target']:,.0f} {growth['currency']} "
            f"is a **{growth['multiple_required']:,.0f}x** return. Required compounded monthly rate:",
            "",
            "| Horizon | Monthly return needed |",
            "|---|---:|",
        ]
        for horizon, rate in growth["monthly_return_required_pct"].items():
            out.append(f"| {horizon} | {rate:,.2f}% |")
        out += ["", "Time required at reference rates:", ""]
        for rate, months in growth["months_required_at_rate"].items():
            out.append(f"- At {rate}: **{months:,.0f} months** ({months / 12:,.1f} years)")
        out.append("")

    out += ["---", "", f"_Generated {asof}. Not financial advice._"]
    return "\n".join(out)


def write_reports(
    reports_dir: Path, asof: date, markdown: str, payload: dict[str, Any]
) -> tuple[Path, Path]:
    """Write both the dated Markdown report and the machine-readable JSON."""
    reports_dir.mkdir(parents=True, exist_ok=True)
    md_path = reports_dir / f"report_{asof:%Y-%m-%d}.md"
    json_path = reports_dir / f"report_{asof:%Y-%m-%d}.json"
    md_path.write_text(markdown)
    json_path.write_text(json.dumps(payload, indent=2, default=str))
    # Stable filenames so n8n and CI can reference the newest run without globbing.
    (reports_dir / "latest.md").write_text(markdown)
    (reports_dir / "latest.json").write_text(json.dumps(payload, indent=2, default=str))
    return md_path, json_path
