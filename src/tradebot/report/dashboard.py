"""Render a run payload as an HTML dashboard.

``render_fragment`` returns the page body (style + markup + script) with no
document wrapper, which is what an embedded viewer wants. ``render_standalone``
wraps that in a full HTML document for writing to disk.

Charts are hand-drawn SVG: a portfolio dashboard needs a line, some bars and a
few meters, and none of that justifies a charting dependency on a page that has
to open from a file:// URL with no network.

Colour decisions worth stating, since they are not the obvious ones:

* **P&L uses blue for gain and red for loss, not green and red.** Red/green is
  precisely the pair that red-green colourblind readers cannot separate, and
  sign is the single most important thing on this page. Blue/red is a proper
  diverging pair with a neutral midpoint and reads for everyone.
* The categorical slots used for allocation are validated for CVD separation in
  both light and dark. Two of them fall below 3:1 against the light surface, so
  every bar carries a visible direct label rather than relying on its fill.
"""

from __future__ import annotations

import html
import json
from typing import Any

__all__ = ["render_fragment", "render_standalone", "PAGE_TITLE"]

PAGE_TITLE = "Theta Desk"

SERIES = ["--s1", "--s2", "--s3", "--s4", "--s5", "--s6"]


def _e(x: Any) -> str:
    return html.escape(str(x), quote=True)


def _num(x: Any, digits: int = 2, dash: str = "—") -> str:
    if x is None:
        return dash
    try:
        return f"{float(x):,.{digits}f}"
    except (TypeError, ValueError):
        return dash


def _signed(x: Any, digits: int = 2) -> str:
    if x is None:
        return "—"
    return f"{float(x):+,.{digits}f}"


def _pct(x: Any, digits: int = 1) -> str:
    return "—" if x is None else f"{float(x):,.{digits}f}%"


def _cls(x: Any) -> str:
    """Polarity class for a signed figure."""
    if x is None:
        return "flat"
    return "up" if float(x) > 0 else ("down" if float(x) < 0 else "flat")


# --------------------------------------------------------------------- charts


def _equity_charts(curve: list[list[Any]]) -> str:
    """Equity line over an underwater drawdown panel, sharing one x-scale.

    Two panels rather than one chart with two y-axes: equity in currency and
    drawdown in percent are different scales, and overlaying them would be the
    dual-axis mistake.
    """
    if len(curve) < 2:
        return ('<p class="empty">Not enough equity history yet — the curve '
                'appears once two runs have been recorded.</p>')

    dates = [str(p[0]) for p in curve]
    vals = [float(p[1]) for p in curve]
    n = len(vals)

    peak, under = vals[0], []
    for v in vals:
        peak = max(peak, v)
        under.append((v / peak - 1.0) * 100.0 if peak > 0 else 0.0)

    W, H, HU = 1000.0, 260.0, 96.0
    PL, PR, PT, PB = 56.0, 16.0, 16.0, 22.0
    lo, hi = min(vals), max(vals)
    pad = (hi - lo) * 0.12 or max(hi * 0.02, 1.0)
    lo, hi = lo - pad, hi + pad

    def x(i: int) -> float:
        return PL + (W - PL - PR) * (i / (n - 1))

    def y(v: float) -> float:
        return PT + (H - PT - PB) * (1 - (v - lo) / (hi - lo))

    line = " ".join(f"{'M' if i == 0 else 'L'}{x(i):.1f},{y(v):.1f}" for i, v in enumerate(vals))
    area = f"{line} L{x(n - 1):.1f},{H - PB:.1f} L{x(0):.1f},{H - PB:.1f} Z"

    # y ticks: every label names a value the chart actually reaches
    ticks = []
    for f in (0.0, 0.25, 0.5, 0.75, 1.0):
        v = lo + (hi - lo) * f
        ticks.append(
            f'<line class="grid" x1="{PL}" x2="{W - PR}" y1="{y(v):.1f}" y2="{y(v):.1f}"/>'
            f'<text class="tick ty" x="{PL - 8}" y="{y(v) + 3.5:.1f}">{v / 1000:,.1f}k</text>'
        )

    trough = min(range(n), key=lambda i: under[i])
    du_lo = min(min(under), -0.5)

    def yu(v: float) -> float:
        return 6.0 + (HU - 24.0) * (v / du_lo if du_lo else 0)

    u_line = " ".join(f"{'M' if i == 0 else 'L'}{x(i):.1f},{yu(v):.1f}" for i, v in enumerate(under))
    u_area = f"{u_line} L{x(n - 1):.1f},6 L{x(0):.1f},6 Z"

    eq_json = json.dumps({"d": dates, "v": vals, "u": [round(z, 2) for z in under]})
    step = max(1, n // 6)
    xticks = "".join(
        f'<text class="tick" x="{x(i):.1f}" y="{H - 5:.1f}" text-anchor="middle">{dates[i][5:]}</text>'
        for i in range(0, n, step)
    )

    return f"""
<div class="chart" data-chart="equity">
  <svg viewBox="0 0 {W:.0f} {H:.0f}" preserveAspectRatio="none" role="img"
       aria-label="Paper account equity over {n} tracked days">
    {''.join(ticks)}
    <path class="eq-area" d="{area}"/>
    <path class="eq-line" d="{line}"/>
    <line class="cross" x1="0" x2="0" y1="{PT}" y2="{H - PB}" style="opacity:0"/>
    <circle class="dot" r="4.5" style="opacity:0"/>
    {xticks}
  </svg>
  <div class="tip" hidden></div>
</div>
<div class="uw-head"><span>Underwater — decline from the running peak</span>
  <span class="uw-worst">Deepest {under[trough]:.1f}% on {_e(dates[trough])}</span></div>
<div class="chart uw">
  <svg viewBox="0 0 {W:.0f} {HU:.0f}" preserveAspectRatio="none" role="img"
       aria-label="Drawdown from peak; deepest {under[trough]:.1f} percent">
    <path class="uw-area" d="{u_area}"/>
    <path class="uw-line" d="{u_line}"/>
    <circle class="uw-mark" cx="{x(trough):.1f}" cy="{yu(under[trough]):.1f}" r="4"/>
    <text class="tick" x="{PL - 8}" y="{yu(du_lo) - 2:.1f}" text-anchor="end">{du_lo:.0f}%</text>
  </svg>
</div>
<script type="application/json" id="eq-data">{eq_json}</script>
"""


def _alloc_bars(by_ticker: dict[str, float], by_sector: dict[str, float]) -> str:
    def group(data: dict[str, float], label: str) -> str:
        if not data:
            return ""
        rows = []
        for i, (name, pct) in enumerate(data.items()):
            var = SERIES[i % len(SERIES)]
            rows.append(
                f'<div class="ab-row"><span class="ab-name" title="{_e(name)}">{_e(name)}</span>'
                f'<span class="ab-track"><span class="ab-fill" style="width:{min(pct, 100):.2f}%;'
                f'background:var({var})"></span></span>'
                f'<span class="ab-val">{pct:.1f}%</span></div>'
            )
        return f'<div class="ab-group"><h4>{label}</h4>{"".join(rows)}</div>'

    return group(by_ticker, "By ticker") + group(by_sector, "By sector")


def _pnl_bars(positions: list[dict]) -> str:
    """Diverging bars around a zero axis — gains right, losses left."""
    live = [p for p in positions if p.get("unrealised_pnl") is not None]
    if not live:
        return '<p class="empty">No markable positions.</p>'
    span = max(abs(float(p["unrealised_pnl"])) for p in live) or 1.0

    rows = []
    for p in sorted(live, key=lambda q: -float(q["unrealised_pnl"])):
        v = float(p["unrealised_pnl"])
        w = abs(v) / span * 50.0
        side = "right:50%" if v < 0 else "left:50%"
        rows.append(
            f'<div class="pn-row"><span class="pn-sym">{_e(p["symbol"])}</span>'
            f'<span class="pn-track"><span class="pn-zero"></span>'
            f'<span class="pn-fill {_cls(v)}" style="{side};width:{w:.2f}%"></span></span>'
            f'<span class="pn-val {_cls(v)}">{_signed(v)}</span></div>'
        )
    return f'<div class="pn">{"".join(rows)}</div>'


def _iv_meters(entries: list[dict]) -> str:
    if not entries:
        return '<p class="empty">No option positions with a usable implied volatility.</p>'
    out = []
    for e in entries:
        rank = e.get("iv_rank")
        status = e.get("status", "")
        if rank is None:
            out.append(
                f'<div class="iv-row building"><span class="iv-sym">{_e(e["symbol"])}</span>'
                f'<span class="iv-track"><span class="iv-build" '
                f'style="width:{min(e.get("history_days", 0) / 20 * 100, 100):.0f}%"></span></span>'
                f'<span class="iv-val">{_e(e.get("history_days", 0))}/20 d</span>'
                f'<span class="chip neutral">building history</span></div>'
            )
            continue
        chip = {"rich": "warn", "cheap": "good"}.get(status, "neutral")
        out.append(
            f'<div class="iv-row"><span class="iv-sym">{_e(e["symbol"])}</span>'
            f'<span class="iv-track">'
            f'<span class="iv-zone cheap"></span><span class="iv-zone rich"></span>'
            f'<span class="iv-needle" style="left:{rank:.1f}%"></span></span>'
            f'<span class="iv-val">{rank:.0f}</span>'
            f'<span class="chip {chip}">{_e(status)}</span></div>'
        )
    return f'<div class="iv">{"".join(out)}</div>'


CSS = """
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600&family=IBM+Plex+Sans:wght@400;500;600;700&display=swap">
<style>
:root{
  --bg:#f4f4f1; --surface:#fbfbf9; --surface-2:#f0f0ec; --rule:#dedbd3;
  --ink:#16171c; --ink-2:#55565e; --ink-3:#83848c;
  --s1:#2a78d6; --s2:#eb6834; --s3:#1baf7a; --s4:#eda100; --s5:#e87ba4; --s6:#4a3aa7;
  --up:#2a78d6; --down:#d03b3b; --mid:#c9c7be;
  --good:#0ca30c; --warn:#fab219; --crit:#d03b3b;
  --grid:#e6e4dc; --accent:#2a78d6;
  --mono:"IBM Plex Mono",ui-monospace,SFMono-Regular,Menlo,monospace;
  --sans:"IBM Plex Sans",system-ui,-apple-system,Segoe UI,sans-serif;
}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){
  --bg:#0f0f12; --surface:#17171a; --surface-2:#1e1e22; --rule:#2c2c31;
  --ink:#f3f3f5; --ink-2:#a8a9b2; --ink-3:#7a7b84;
  --s1:#3987e5; --s2:#d95926; --s3:#199e70; --s4:#c98500; --s5:#d55181; --s6:#9085e9;
  --up:#3987e5; --down:#e05c5c; --mid:#3a3a40;
  --good:#0ca30c; --warn:#fab219; --crit:#e05c5c;
  --grid:#25252a; --accent:#3987e5;
}}
:root[data-theme="dark"]{
  --bg:#0f0f12; --surface:#17171a; --surface-2:#1e1e22; --rule:#2c2c31;
  --ink:#f3f3f5; --ink-2:#a8a9b2; --ink-3:#7a7b84;
  --s1:#3987e5; --s2:#d95926; --s3:#199e70; --s4:#c98500; --s5:#d55181; --s6:#9085e9;
  --up:#3987e5; --down:#e05c5c; --mid:#3a3a40;
  --good:#0ca30c; --warn:#fab219; --crit:#e05c5c;
  --grid:#25252a; --accent:#3987e5;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font-family:var(--sans);
  font-size:14px;line-height:1.5;-webkit-font-smoothing:antialiased}
.wrap{max-width:1180px;margin:0 auto;padding-inline:16px;padding-block:24px 56px}
h1,h2,h3,h4{margin:0;font-weight:600;text-wrap:balance}
.mono{font-family:var(--mono);font-variant-numeric:tabular-nums}
.num{font-family:var(--mono);font-variant-numeric:tabular-nums;letter-spacing:-.01em}
.up{color:var(--up)} .down{color:var(--down)} .flat{color:var(--ink-3)}

/* masthead ------------------------------------------------------------- */
.mast{display:flex;flex-wrap:wrap;gap:16px 28px;align-items:flex-end;
  padding-bottom:18px;border-bottom:2px solid var(--ink);margin-bottom:22px}
.mast h1{font-size:22px;letter-spacing:-.02em}
.eyebrow{font-size:10.5px;text-transform:uppercase;letter-spacing:.14em;
  color:var(--ink-3);font-weight:600;margin-bottom:5px}
.mast .hero{margin-left:auto;text-align:right}
.hero .v{font-family:var(--mono);font-size:32px;font-weight:600;letter-spacing:-.03em;
  line-height:1.05;font-variant-numeric:tabular-nums}
.hero .sub{font-size:12px;color:var(--ink-2);margin-top:3px}
.hero .ccy{font-size:15px;color:var(--ink-3)}
.mode{display:inline-flex;align-items:center;gap:6px;padding:3px 9px;border-radius:3px;
  background:var(--surface-2);border:1px solid var(--rule);font-size:11px;
  font-weight:600;letter-spacing:.06em;text-transform:uppercase;color:var(--ink-2)}
.mode::before{content:"";width:6px;height:6px;border-radius:50%;background:var(--good)}

/* kpi strip ------------------------------------------------------------ */
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(152px,1fr));
  gap:1px;background:var(--rule);border:1px solid var(--rule);margin-bottom:30px}
.kpi{background:var(--surface);padding:13px 15px}
.kpi .k{font-size:10.5px;text-transform:uppercase;letter-spacing:.1em;
  color:var(--ink-3);font-weight:600;display:flex;align-items:baseline;gap:5px}
.kpi .g{font-family:var(--mono);font-size:13px;color:var(--accent);font-weight:600}
.kpi .v{font-family:var(--mono);font-size:21px;font-weight:600;margin-top:5px;
  letter-spacing:-.02em;font-variant-numeric:tabular-nums}
.kpi .n{font-size:11.5px;color:var(--ink-3);margin-top:2px}

section{margin-bottom:34px}
.shead{display:flex;align-items:baseline;gap:12px;margin-bottom:12px;
  padding-bottom:7px;border-bottom:1px solid var(--rule)}
.shead h2{font-size:14px;letter-spacing:-.01em}
.shead .note{font-size:11.5px;color:var(--ink-3);margin-left:auto}

/* charts --------------------------------------------------------------- */
.chart{position:relative;background:var(--surface);border:1px solid var(--rule)}
.chart svg{display:block;width:100%;height:270px}
.chart.uw svg{height:100px}
.chart.uw{border-top:none}
.grid{stroke:var(--grid);stroke-width:1}
.tick{fill:var(--ink-3);font-family:var(--mono);font-size:10px}
.ty{text-anchor:end}
.eq-line{fill:none;stroke:var(--accent);stroke-width:2;stroke-linejoin:round;vector-effect:non-scaling-stroke}
.eq-area{fill:var(--accent);opacity:.10}
.uw-line{fill:none;stroke:var(--down);stroke-width:1.5;vector-effect:non-scaling-stroke}
.uw-area{fill:var(--down);opacity:.13}
.uw-mark{fill:var(--down);stroke:var(--surface);stroke-width:2}
.cross{stroke:var(--ink-3);stroke-width:1;stroke-dasharray:3 3}
.dot{fill:var(--accent);stroke:var(--surface);stroke-width:2}
.uw-head{display:flex;gap:12px;align-items:baseline;font-size:11.5px;color:var(--ink-3);
  padding:9px 2px 6px}
.uw-worst{margin-left:auto;font-family:var(--mono);color:var(--down);font-weight:500}
.tip{position:absolute;pointer-events:none;background:var(--ink);color:var(--bg);
  padding:7px 10px;border-radius:4px;font-size:11.5px;font-family:var(--mono);
  white-space:nowrap;transform:translate(-50%,-125%);z-index:5;
  font-variant-numeric:tabular-nums;box-shadow:0 3px 14px rgba(0,0,0,.28)}
.tip b{display:block;font-size:13px;font-weight:600}

/* two-up --------------------------------------------------------------- */
.cols{display:grid;grid-template-columns:1fr 1fr;gap:26px}
@media(max-width:760px){.cols{grid-template-columns:1fr}}
.panel{background:var(--surface);border:1px solid var(--rule);padding:15px 16px}

/* allocation ----------------------------------------------------------- */
.ab-group+.ab-group{margin-top:18px}
.ab-group h4{font-size:10.5px;text-transform:uppercase;letter-spacing:.1em;
  color:var(--ink-3);margin-bottom:9px}
.ab-row{display:grid;grid-template-columns:auto 1fr 52px;gap:10px;align-items:center;
  margin-bottom:6px}
.ab-name{font-family:var(--mono);font-size:11.5px;font-weight:500;overflow:hidden;
  text-overflow:ellipsis;white-space:nowrap;max-width:148px;min-width:56px}
.ab-track{height:16px;background:var(--surface-2);position:relative;border-radius:2px;overflow:hidden}
.ab-fill{position:absolute;inset:0 auto 0 0;border-radius:0 2px 2px 0}
.ab-val{font-family:var(--mono);font-size:12px;text-align:right;
  font-variant-numeric:tabular-nums;color:var(--ink-2)}

/* p&l diverging bars --------------------------------------------------- */
.pn-row{display:grid;grid-template-columns:auto 1fr 84px;gap:10px;align-items:center;
  margin-bottom:6px}
.pn-sym{font-family:var(--mono);font-size:11px;color:var(--ink-2);width:150px;
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.pn-track{position:relative;height:18px;background:var(--surface-2);border-radius:2px}
.pn-zero{position:absolute;left:50%;top:0;bottom:0;width:1px;background:var(--mid)}
.pn-fill{position:absolute;top:2px;bottom:2px;border-radius:2px}
.pn-fill.up{background:var(--up)} .pn-fill.down{background:var(--down)}
.pn-val{font-family:var(--mono);font-size:12px;text-align:right;font-weight:500;
  font-variant-numeric:tabular-nums}
@media(max-width:760px){.pn-sym{width:auto}.pn-row{grid-template-columns:1fr}
  .pn-val{text-align:left}}

/* iv meters ------------------------------------------------------------ */
.iv-row{display:grid;grid-template-columns:150px 1fr 34px 92px;gap:10px;align-items:center;
  margin-bottom:8px}
.iv-sym{font-family:var(--mono);font-size:11px;color:var(--ink-2);overflow:hidden;
  text-overflow:ellipsis;white-space:nowrap}
.iv-track{position:relative;height:18px;background:var(--surface-2);border-radius:2px}
.iv-zone{position:absolute;top:0;bottom:0}
.iv-zone.cheap{left:0;width:30%;background:color-mix(in srgb,var(--good) 16%,transparent)}
.iv-zone.rich{right:0;width:30%;background:color-mix(in srgb,var(--warn) 20%,transparent)}
.iv-needle{position:absolute;top:-2px;bottom:-2px;width:3px;background:var(--ink);
  border-radius:2px;transform:translateX(-1.5px)}
.iv-build{position:absolute;inset:0 auto 0 0;background:var(--mid);border-radius:2px}
.iv-val{font-family:var(--mono);font-size:12px;text-align:right;font-weight:600;
  font-variant-numeric:tabular-nums}
@media(max-width:760px){.iv-row{grid-template-columns:1fr 34px}
  .iv-track{grid-column:1/-1}.iv-row .chip{grid-column:1/-1;justify-self:start}}

.chip{display:inline-flex;align-items:center;gap:5px;font-size:10.5px;font-weight:600;
  padding:2px 7px;border-radius:3px;text-transform:uppercase;letter-spacing:.05em;
  background:var(--surface-2);color:var(--ink-2);border:1px solid var(--rule);white-space:nowrap}
.chip::before{content:"";width:5px;height:5px;border-radius:50%;background:currentColor}
.chip.good{color:var(--good)} .chip.warn{color:var(--warn)} .chip.crit{color:var(--crit)}

/* table ---------------------------------------------------------------- */
.tscroll{overflow-x:auto;background:var(--surface);border:1px solid var(--rule)}
table{border-collapse:collapse;width:100%;min-width:720px}
th{font-size:10px;text-transform:uppercase;letter-spacing:.09em;color:var(--ink-3);
  text-align:right;padding:9px 11px;border-bottom:1px solid var(--rule);font-weight:600;
  white-space:nowrap}
th:first-child,td:first-child{text-align:left}
td{padding:9px 11px;border-bottom:1px solid var(--rule);text-align:right;
  font-family:var(--mono);font-size:12px;font-variant-numeric:tabular-nums;white-space:nowrap}
tbody tr:last-child td{border-bottom:none}
tbody tr:hover{background:var(--surface-2)}
.prog{display:inline-block;width:46px;height:5px;background:var(--surface-2);
  border-radius:3px;overflow:hidden;vertical-align:middle;margin-right:6px}
.prog i{display:block;height:100%;background:var(--accent);border-radius:3px}

/* flags / facts -------------------------------------------------------- */
.flags{display:flex;flex-direction:column;gap:1px;background:var(--rule);
  border:1px solid var(--rule)}
.flag{background:var(--surface);padding:10px 13px;display:flex;gap:10px;align-items:flex-start;
  font-size:12.5px}
.flag .chip{flex:none;margin-top:1px}
.facts{list-style:none;margin:0;padding:0;display:flex;flex-direction:column;gap:7px}
.facts li{font-size:12.5px;color:var(--ink-2);padding-left:14px;position:relative}
.facts li::before{content:"";position:absolute;left:0;top:8px;width:5px;height:5px;
  border-radius:50%;background:var(--ink-3)}
.facts .mono{color:var(--ink);font-size:12px}
.empty{color:var(--ink-3);font-size:12.5px;margin:0;padding:6px 0}

.caveats{background:var(--surface-2);border-left:2px solid var(--ink-3);
  padding:12px 15px;font-size:12.5px;color:var(--ink-2)}
.caveats p{margin:0 0 7px} .caveats p:last-child{margin:0}
.foot{margin-top:40px;padding-top:16px;border-top:1px solid var(--rule);
  font-size:11.5px;color:var(--ink-3);display:flex;flex-wrap:wrap;gap:8px 20px}
@media (prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}}
</style>
"""

SCRIPT = """
<script>
(function(){
  var el = document.getElementById('eq-data');
  if (!el) return;
  var data;
  try { data = JSON.parse(el.textContent); } catch (e) { return; }

  var chart = document.querySelector('[data-chart="equity"]');
  var svg = chart.querySelector('svg');
  var cross = chart.querySelector('.cross');
  var dot = chart.querySelector('.dot');
  var tip = chart.querySelector('.tip');
  var n = data.v.length;
  var PL = 56, PR = 16, W = 1000, H = 260, PT = 16, PB = 22;
  var lo = Math.min.apply(null, data.v), hi = Math.max.apply(null, data.v);
  var pad = (hi - lo) * 0.12 || Math.max(hi * 0.02, 1);
  lo -= pad; hi += pad;
  var fmt = new Intl.NumberFormat(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 2});

  function show(ev){
    var r = svg.getBoundingClientRect();
    var px = (ev.touches ? ev.touches[0].clientX : ev.clientX) - r.left;
    var vx = px / r.width * W;
    var i = Math.round((vx - PL) / (W - PL - PR) * (n - 1));
    i = Math.max(0, Math.min(n - 1, i));
    var cx = PL + (W - PL - PR) * (i / (n - 1));
    var cy = PT + (H - PT - PB) * (1 - (data.v[i] - lo) / (hi - lo));
    cross.setAttribute('x1', cx); cross.setAttribute('x2', cx);
    cross.style.opacity = 1;
    dot.setAttribute('cx', cx); dot.setAttribute('cy', cy);
    dot.style.opacity = 1;
    tip.hidden = false;
    tip.innerHTML = '<b>' + fmt.format(data.v[i]) + '</b>' + data.d[i] +
      (data.u[i] < -0.05 ? '  ·  ' + data.u[i].toFixed(1) + '% off peak' : '  ·  at peak');
    tip.style.left = (cx / W * r.width) + 'px';
    tip.style.top = (cy / H * r.height) + 'px';
  }
  function hide(){
    cross.style.opacity = 0; dot.style.opacity = 0; tip.hidden = true;
  }
  chart.addEventListener('mousemove', show);
  chart.addEventListener('mouseleave', hide);
  chart.addEventListener('touchstart', show, {passive: true});
  chart.addEventListener('touchmove', show, {passive: true});
  chart.addEventListener('touchend', hide);
})();
</script>
"""


def render_fragment(payload: dict[str, Any]) -> str:
    """The dashboard body: style, markup and script, with no document wrapper."""
    v = payload.get("layer1_valuation") or {}
    a = payload.get("layer2_analytics") or {}
    macro = payload.get("layer3_macro")
    news = payload.get("layer3_news") or {}
    paper = payload.get("paper_account") or {}
    growth = payload.get("growth_target") or {}

    totals = v.get("totals", {})
    greeks = a.get("aggregate_greeks", {})
    alloc = a.get("allocation", {})
    perf = paper.get("performance", {})
    eq, tr = perf.get("equity", {}), perf.get("trades", {})
    ccy = paper.get("currency", "")
    asof = v.get("asof") or payload.get("asof", "")

    # ---- masthead -------------------------------------------------------
    pnl = totals.get("unrealised_pnl")
    head = f"""
<div class="mast">
  <div>
    <div class="eyebrow">Options book · {_e(asof)}</div>
    <h1>{PAGE_TITLE}</h1>
    <div style="margin-top:9px"><span class="mode">paper</span></div>
  </div>
  <div class="hero">
    <div class="eyebrow">Account equity</div>
    <div class="v">{_num(paper.get("equity"))}<span class="ccy"> {_e(ccy)}</span></div>
    <div class="sub"><span class="{_cls(pnl)}">{_signed(pnl)} unrealised</span>
      · {_num(paper.get("cash"))} cash · {_e(paper.get("open_positions", 0))} open</div>
  </div>
</div>"""

    # ---- KPI strip: the aggregate Greeks are the point of the page ------
    def kpi(label: str, glyph: str, value: str, note: str, cls: str = "") -> str:
        g = f'<span class="g">{glyph}</span>' if glyph else ""
        return (f'<div class="kpi"><div class="k">{g}{label}</div>'
                f'<div class="v {cls}">{value}</div><div class="n">{note}</div></div>')

    theta = greeks.get("daily_theta_dollars")
    kpis = "".join([
        kpi("Net delta", "Δ", _num(greeks.get("net_delta_shares"), 1),
            f'{_num(greeks.get("net_delta_dollars"), 0)} notional'),
        kpi("Daily theta", "Θ", _signed(theta), "if nothing moves", _cls(theta)),
        kpi("Net vega", "ν", _num(greeks.get("net_vega_dollars_per_iv_point")), "per 1 IV point"),
        kpi("Net gamma", "Γ", _num(greeks.get("net_gamma_shares_per_point"), 2), "delta per 1pt spot"),
        kpi("Max drawdown", "", _pct(eq.get("max_drawdown_pct")),
            f'{_num(eq.get("max_drawdown_amount"), 0)} peak to trough', "down"),
        kpi("Closed trades", "", _e(tr.get("closed", 0)),
            f'{tr.get("wins", 0)}W / {tr.get("losses", 0)}L'
            + (f' · PF {_num(tr.get("profit_factor"))}' if tr.get("profit_factor") else "")),
    ])

    # ---- equity ---------------------------------------------------------
    curve = payload.get("equity_curve") or []
    ret = eq.get("total_return_pct")
    ann = (f' · annualised {_pct(eq.get("annualised_return_pct"), 2)}'
           if eq.get("annualised_return_pct") is not None else " · annualised withheld")
    equity_sec = f"""
<section>
  <div class="shead"><h2>Paper equity</h2>
    <span class="note"><span class="{_cls(ret)}">{_signed(ret)}%</span> over
      {_e(eq.get("days_tracked", 0))} days{ann}</span></div>
  {_equity_charts(curve)}
</section>"""

    # ---- positions ------------------------------------------------------
    rows = []
    for p in v.get("positions", []):
        g = p.get("greeks", {})
        iv = p.get("iv")
        tgt = p.get("progress_to_target_pct")
        prog = (f'<span class="prog"><i style="width:{min(float(tgt), 100):.0f}%"></i></span>{_pct(tgt, 0)}'
                if tgt is not None else "—")
        warn = ('<span class="chip crit">stale</span>' if p.get("stale") else "")
        rows.append(
            f'<tr><td>{_e(p["symbol"])} {warn}</td>'
            f'<td>{_num(p.get("qty"), 0)}</td>'
            f'<td>{_num(p.get("mark"))}</td>'
            f'<td>{_num(p.get("cost_basis"))}</td>'
            f'<td>{_num(p.get("current_value"), 0)}</td>'
            f'<td class="{_cls(p.get("unrealised_pnl"))}">{_signed(p.get("unrealised_pnl"), 0)}</td>'
            f'<td class="{_cls(p.get("unrealised_pnl_pct"))}">{_signed(p.get("unrealised_pnl_pct"), 1)}%</td>'
            f'<td>{_e(p.get("dte")) if p.get("dte") is not None else "—"}</td>'
            f'<td>{_pct(iv * 100 if iv else None, 0)}</td>'
            f'<td>{_signed(g.get("delta"), 1)}</td>'
            f'<td class="{_cls(g.get("theta"))}">{_signed(g.get("theta"), 1)}</td>'
            f'<td style="text-align:left">{prog}</td></tr>'
        )
    positions_sec = f"""
<section>
  <div class="shead"><h2>Positions</h2>
    <span class="note">marked {_e(asof)} · premiums per share, values per position</span></div>
  <div class="tscroll"><table>
    <thead><tr><th>Contract</th><th>Qty</th><th>Mark</th><th>Basis</th><th>Value</th>
      <th>P&amp;L</th><th>P&amp;L %</th><th>DTE</th><th>IV</th><th>Δ</th><th>Θ/day</th>
      <th style="text-align:left">To target</th></tr></thead>
    <tbody>{"".join(rows) or '<tr><td colspan="12">No positions.</td></tr>'}</tbody>
  </table></div>
</section>"""

    # ---- allocation + P&L ----------------------------------------------
    flags = alloc.get("concentration_flags") or []
    flag_html = "".join(
        f'<div class="flag"><span class="chip warn">{_e(f["kind"])}</span>'
        f'<span>{_e(f["message"])}</span></div>' for f in flags
    ) or ('<div class="flag"><span class="chip good">ok</span>'
          '<span>No concentration cap exceeded.</span></div>')

    mid_sec = f"""
<section>
  <div class="cols">
    <div>
      <div class="shead"><h2>Allocation</h2>
        <span class="note">share of absolute book value</span></div>
      <div class="panel">{_alloc_bars(alloc.get("by_ticker_pct", {}), alloc.get("by_sector_pct", {}))}</div>
      <div class="flags" style="margin-top:14px">{flag_html}</div>
    </div>
    <div>
      <div class="shead"><h2>Unrealised P&amp;L</h2>
        <span class="note">gain right, loss left</span></div>
      <div class="panel">{_pnl_bars(v.get("positions", []))}</div>
    </div>
  </div>
</section>"""

    # ---- IV + expiry ----------------------------------------------------
    watch = a.get("expiry_watch") or []
    watch_html = "".join(f'<li>{_e(w["fact"])}</li>' for w in watch) or (
        '<li>Nothing inside the expiry window.</li>')
    thresh = watch[0]["threshold_days"] if watch else 45

    lower_sec = f"""
<section>
  <div class="cols">
    <div>
      <div class="shead"><h2>IV environment</h2><span class="note">rank over 252 days</span></div>
      <div class="panel">{_iv_meters(a.get("iv_environment") or [])}
        <div style="display:flex;gap:14px;margin-top:12px;font-size:10.5px;color:var(--ink-3)">
          <span>0 — cheap</span><span style="margin-left:auto">rich — 100</span></div>
      </div>
    </div>
    <div>
      <div class="shead"><h2>Expiry watch</h2><span class="note">&le; {_e(thresh)} DTE</span></div>
      <div class="panel">
        <ul class="facts">{watch_html}</ul>
        <p style="margin:14px 0 0;font-size:11.5px;color:var(--ink-3);
          padding-top:11px;border-top:1px solid var(--rule)">
          Facts only. This page reports time decay; it does not advise rolling.</p>
      </div>
    </div>
  </div>
</section>"""

    # ---- macro ----------------------------------------------------------
    macro_sec = ""
    if macro:
        comps = "".join(
            f'<div class="ab-row"><span class="ab-name">{_e(k.replace("_", " "))}</span>'
            f'<span class="ab-track"><span class="ab-fill" '
            f'style="width:{float(c.get("score", 0)):.1f}%;background:var(--s1)"></span></span>'
            f'<span class="ab-val">{_num(c.get("score"), 0)}</span></div>'
            for k, c in (macro.get("components") or {}).items()
        )
        macro_sec = f"""
<section>
  <div class="shead"><h2>Macro gate</h2>
    <span class="note">deterministic · 0 stressed, 100 calm</span></div>
  <div class="panel">
    <div style="display:flex;align-items:baseline;gap:12px;margin-bottom:14px">
      <span class="num" style="font-size:30px;font-weight:600">{_num(macro.get("score"), 0)}</span>
      <span style="color:var(--ink-3)">/100</span>
      <span class="chip neutral">{_e(macro.get("regime", ""))}</span></div>
    {comps}
  </div>
</section>"""

    # ---- caveats --------------------------------------------------------
    layers = "valuation · analytics" + (" · macro" if macro else "") + (
        " · news" if news.get("enabled") else "")
    caveats = perf.get("caveats") or []
    news_note = ("News layer disabled — no API calls were made on this run."
                 if not news.get("enabled") else
                 f'News: {news.get("usage", {}).get("api_calls", 0)} API call(s).')
    cav_html = "".join(f"<p>{_e(c)}</p>" for c in caveats)

    growth_html = ""
    if growth.get("applicable"):
        rates = growth.get("months_required_at_rate", {})
        first = next(iter(rates.items()), None)
        if first:
            growth_html = (
                f"<p><strong>Growth target.</strong> "
                f"{_num(growth['start'], 0)} → {_num(growth['target'], 0)} {_e(growth['currency'])} "
                f"is a {_num(growth['multiple_required'], 0)}× return: "
                f"{_num(growth['monthly_return_required_pct'].get('2 years'), 1)}% per month "
                f"sustained for two years, or {_num(first[1] / 12, 0)} years at {_e(first[0])}.</p>"
            )

    footer = f"""
<section>
  <div class="caveats">
    {cav_html}{growth_html}
    <p>{_e(news_note)}</p>
    <p><strong>Not advice.</strong> This page reports what is true of the book and the
    market environment. It does not recommend trades. Option marks are quoted in the
    instrument's own currency and are not FX-converted into the account currency.</p>
  </div>
</section>
<div class="foot">
  <span>Generated {_e(payload.get("generated_at", asof))}</span>
  <span>Layers: {_e(layers)}</span>
  <span style="margin-left:auto">Paper trading · no live-broker adapter</span>
</div>"""

    return (CSS + '<div class="wrap">' + head + f'<div class="kpis">{kpis}</div>'
            + equity_sec + positions_sec + mid_sec + lower_sec + macro_sec + footer
            + "</div>" + SCRIPT)


def render_standalone(payload: dict[str, Any]) -> str:
    """The fragment wrapped in a complete HTML document for writing to disk."""
    return (
        '<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">\n'
        f"<title>{PAGE_TITLE} — {_e(payload.get('asof', ''))}</title>\n"
        "</head>\n<body>\n" + render_fragment(payload) + "\n</body>\n</html>\n"
    )
