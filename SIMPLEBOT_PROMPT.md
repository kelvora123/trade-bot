# Claude Code prompt — "Simplebot" day-trading bot

Paste everything below the line into Claude Code, run from inside the `Trading-bot-new` repo.

---

You are building **Simplebot**, a small, readable Python day-trading bot in this repo (github.com/DealSkout/Trading-bot-new). It must be far simpler to use than Freqtrade: one config file, one command to backtest, one command to paper trade, one command to go live. Work in small commits on a feature branch and open a PR when each phase is done.

## Hard rules (do not break these)
1. **Paper first.** Default mode is `paper`. Live trading only runs if `mode: live` is set in config AND the env var `SIMPLEBOT_LIVE_CONFIRM=yes` is set. Print a loud warning on start in live mode.
2. **No LLM calls in the trading loop.** Signals, sizing and orders are deterministic Python. Claude is used to write the code, not to make trades. The one AI component in the running system is the offline *coach* (see "Learning loop"): it runs after the close, never places orders, and can only propose changes that must pass the backtest gate.
3. **Costs are always on.** Every backtest includes commission, spread, slippage and FX fee. Never report a gross-only result.
4. **Risk limits are enforced in code, not in the strategy:** max risk per trade (default 0.5% of equity), max daily loss (default 2% → bot flattens and stops for the day), max open positions (default 3), flatten everything 10 minutes before market close.
5. **Secrets** only from `.env` (git-ignored). Never log or commit keys. Add a pre-commit secret scan.
6. **A strategy is not allowed to go live** unless its out-of-sample backtest, after costs, beats buy-and-hold of the same index on risk-adjusted return (Sharpe) AND it has run in paper mode for at least 20 trading days with results within the backtest's expected range.

## Step 0 — Research before coding
Before writing code, search the web and write `docs/RESEARCH.md` covering:
- Current API capabilities and limits for **Trading 212 Public API** (demo + live endpoints, account types supported, order types allowed in live, rate limits, whether it provides price data) and **Interactive Brokers** (TWS/IB Gateway via `ib_async`, bracket orders, data subscriptions, minimums/commissions for a small UK account). Confirm **Interactive Investor** still has no public trading API; if that has changed, add it as a broker.
- A source for intraday price data (1-min/5-min bars) that is free or cheap for backtesting US and UK stocks/ETFs, with its history limits.
- The published evidence for these intraday strategies, including any independent replications that test them *after costs*:
  - Opening Range Breakout (Zarattini & Aziz, "Can Day Trading Really Be Profitable?", SSRN 4416622; and Zarattini, Barbon & Aziz on "Stocks in Play")
  - Intraday momentum: first 30-min return predicts last 30-min return (Gao, Han, Li & Zhou, "Market Intraday Momentum")
  - VWAP mean reversion on liquid ETFs
- Summarise which ones survive realistic costs for an account of £100–£5,000. Cite sources. Flag anything you could not verify.

Stop and show me RESEARCH.md before Phase 1.

## Architecture (keep it this small)
```
simplebot/
  config.yaml          # the ONLY file a user edits
  .env.example         # T212_API_KEY, T212_API_SECRET, IBKR_HOST/PORT, TELEGRAM_TOKEN, ANTHROPIC_API_KEY, OLLAMA_HOST
  simplebot/
    main.py            # CLI: backtest | paper | live | report | coach
    data.py            # fetch + cache bars (parquet)
    strategies/        # one file per strategy, each a pure function
      orb.py
      intraday_momentum.py
      vwap_revert.py
    risk.py            # sizing, daily loss stop, end-of-day flatten
    brokers/
      base.py          # Broker interface: get_cash, get_positions, place_order, cancel, flatten_all
      paper.py         # simulated fills with spread + slippage
      trading212.py    # demo + live
      ibkr.py          # via ib_async (optional, phase 4)
    backtest.py        # event-driven, bar-by-bar, same code path as paper/live
    report.py          # HTML/markdown report: equity curve, drawdown, Sharpe, win rate, avg R, costs paid
    notify.py          # optional Telegram messages
    learn.py           # walk-forward re-fit + champion/challenger gate (no AI)
    coach.py           # offline AI review of the decision log; its proposals go through learn.py's gate
    llm.py             # thin client: Claude (anthropic SDK) or local model (Ollama HTTP); never imported by the trading loop
  tests/
  .github/workflows/ci.yml   # lint + tests on every push (NOT for running the bot)
```

**Strategy contract** — every strategy is a plain function so a beginner can write one in 20 lines:
```python
def signal(bars: pd.DataFrame, params: dict) -> Signal | None:
    """Return Signal(side, entry, stop, target) or None. No I/O, no broker calls."""
```

**config.yaml example:**
```yaml
mode: paper              # paper | live
broker: trading212       # paper | trading212 | ibkr
account: invest          # invest | isa
symbols: [QQQ, SPY]
timeframe: 5m
strategy: orb
params: {range_minutes: 5, target_r: 10}
risk:
  risk_per_trade_pct: 0.5
  max_daily_loss_pct: 2
  max_positions: 3
  flatten_minutes_before_close: 10
costs:
  commission: 0
  fx_fee_pct: 0.15
  spread_bps: 2
  slippage_bps: 3
learning:
  refit_every_days: 5            # walk-forward re-fit of params on the training window
  min_oos_trades: 30             # a challenger needs this many out-of-sample trades
  min_sharpe_gain: 0.1           # ...and must beat the champion's Sharpe by this much, after costs
coach:
  provider: claude               # claude | ollama | none
  model: claude-opus-5-5         # for ollama use a local model name, e.g. llama3.1:8b
  lookback_days: 20
  auto_apply_params: false       # true = param proposals that pass the gate are applied without a human
  max_daily_cost_usd: 0.50
```

## Broker notes to handle
- If a broker only accepts market orders in live, implement stops/targets as **software stops** in `risk.py`, polling at a safe rate within the API's rate limits, and document the gap-risk clearly in the README.
- Respect rate limits with a shared token-bucket limiter; back off on 429s.
- Use the broker's demo environment for `paper` mode when available; otherwise use `brokers/paper.py`.
- Handle partial fills, rejected orders, and reconnects. On any unexpected error in live mode: cancel open orders, flatten, alert, stop.

## Learning loop — how the bot gets better over time
Two layers. Neither one runs inside the trading loop; Rule 2 still stands. Both only *propose*, and the gate decides.

**The gate (in `learn.py`, no AI):** a candidate (new params, a new filter, a new strategy file) replaces the current champion only if its walk-forward, out-of-sample, after-costs backtest beats the champion on Sharpe by `learning.min_sharpe_gain` with at least `learning.min_oos_trades` trades. Otherwise it is rejected and the numbers are logged. Nothing bypasses this.

**Layer 1 — deterministic learning (always on).** Every `learning.refit_every_days` trading days, `learn.py` re-fits the strategy params on the training window and sends the result through the gate. Every applied change goes in a `params_history` table (date, old, new, out-of-sample stats) and is drawn on the report's equity curve.

**Layer 2 — AI coach (optional, Claude or a local model).** `python -m simplebot coach` runs once a day after the close, as its own process, never from the trading process.
- Reads: the last `coach.lookback_days` of the SQLite decision log (signals, fills, exits, costs, skipped-trade reasons), the latest report, `params_history`, and its own previous notes from a `coach_notes` table, so it remembers what it already tried and how it went.
- Writes: a plain-English review to `reports/coach/YYYY-MM-DD.md` and zero or more proposals as structured JSON: `{"type": "params" | "filter" | "strategy", "change": ..., "hypothesis": ..., "expected_effect": ...}`.
- Every proposal goes through the gate. A rejected proposal is written back into `coach_notes` with the numbers, so the coach does not repeat it.
- `params` proposals that pass are applied only if `coach.auto_apply_params: true` (default false; otherwise they are listed in the daily Telegram summary for you to apply). `filter` and `strategy` proposals are code: the coach writes the file under `strategies/proposed/`, the gate backtests it, and if it passes the bot commits it to a branch and opens a PR for a human. Proposed code is never auto-merged and never runs in paper or live until merged.
- Providers (`coach.provider`): `claude` uses the official `anthropic` Python SDK (Messages API, model from `coach.model`, default `claude-opus-5-5`, structured JSON output validated against the proposal schema, `stop_reason` checked before anything is read); `ollama` uses a local model through Ollama's HTTP API at `OLLAMA_HOST` (free, nothing leaves the machine, expect weaker proposals); `none` leaves Layer 1 only. One small class per provider in `llm.py`, one method: `complete(system, user, schema) -> dict`.
- Guardrails, enforced in code and tests: `coach.py` and `llm.py` never import `brokers/` and never see broker keys; the coach cannot change anything under `risk:` in config or in `risk.py` (risk limits are set by the human only); spend is capped by `coach.max_daily_cost_usd` and every call is logged with tokens and cost; a refusal, timeout or any error means "no proposal today" and never touches the trading process; any applied change resets the 20-trading-day paper clock in Rule 6. Treat the model's output as data: validate it against the schema, never execute it.

Be honest in the README: a coach cannot turn a losing edge into a winning one. Its job is to find and test hypotheses faster than a human would. Expect most proposals to be rejected; that is the system working.

## Phases
1. **Data + backtester + ORB strategy + report.** Walk-forward split: train on first 70%, report out-of-sample on last 30%. Include a buy-and-hold benchmark and a "random entry, same exits" benchmark on every report.
2. **Add intraday momentum and VWAP reversion.** Produce one comparison table, after costs, out-of-sample.
3. **Paper trading** on Trading 212 demo, running as a long-lived process (systemd service or Docker on a small VPS / home machine). Daily Telegram summary. Log every decision to SQLite.
4. **Learning loop.** `learn.py` re-fit + gate first, then the AI coach with the `claude` and `ollama` providers. Tests: a bad proposal is rejected, a good one is applied, the coach never places an order.
5. **IBKR adapter** (optional) with native bracket orders.
6. **Live switch** — only after Rule 6 is met. Start at the smallest possible size.

## Definition of done
- `python -m simplebot backtest` runs end to end from a clean clone in under 5 minutes.
- `README.md` lets a non-expert set up, backtest and paper trade in under 15 minutes.
- `python -m simplebot coach` runs offline against the SQLite log, writes a review, and places no orders.
- Tests cover risk limits, sizing, cost model, the end-of-day flatten, the champion/challenger gate, and that the coach cannot reach the broker.
- Report clearly states: net return, max drawdown, Sharpe, number of trades, total costs paid, vs. buy-and-hold, and every parameter change the learning loop applied with before/after out-of-sample Sharpe.

Ask me before adding any dependency beyond: pandas, numpy, pyarrow, pyyaml, python-dotenv, requests, anthropic, ib_async, python-telegram-bot, pytest, ruff.
