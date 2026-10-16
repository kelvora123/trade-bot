# Claude Code prompt — "Simplebot" day-trading bot

Paste everything below the line into Claude Code, run from inside the `Trading-bot-new` repo.

---

You are building **Simplebot**, a small, readable Python day-trading bot in this repo (github.com/DealSkout/Trading-bot-new). It must be far simpler to use than Freqtrade: one config file, one command to backtest, one command to paper trade, one command to go live. Work in small commits on a feature branch and open a PR when each phase is done.

## Hard rules (do not break these)
1. **Paper first.** Default mode is `paper`. Live trading only runs if `mode: live` is set in config AND the env var `SIMPLEBOT_LIVE_CONFIRM=yes` is set. Print a loud warning on start in live mode.
2. **No LLM calls in the trading loop.** Signals, sizing and orders are deterministic Python. Claude is used to write the code, not to make trades.
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
  .env.example         # T212_API_KEY, T212_API_SECRET, IBKR_HOST/PORT, TELEGRAM_TOKEN
  simplebot/
    main.py            # CLI: backtest | paper | live | report
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
```

## Broker notes to handle
- If a broker only accepts market orders in live, implement stops/targets as **software stops** in `risk.py`, polling at a safe rate within the API's rate limits, and document the gap-risk clearly in the README.
- Respect rate limits with a shared token-bucket limiter; back off on 429s.
- Use the broker's demo environment for `paper` mode when available; otherwise use `brokers/paper.py`.
- Handle partial fills, rejected orders, and reconnects. On any unexpected error in live mode: cancel open orders, flatten, alert, stop.

## Phases
1. **Data + backtester + ORB strategy + report.** Walk-forward split: train on first 70%, report out-of-sample on last 30%. Include a buy-and-hold benchmark and a "random entry, same exits" benchmark on every report.
2. **Add intraday momentum and VWAP reversion.** Produce one comparison table, after costs, out-of-sample.
3. **Paper trading** on Trading 212 demo, running as a long-lived process (systemd service or Docker on a small VPS / home machine). Daily Telegram summary. Log every decision to SQLite.
4. **IBKR adapter** (optional) with native bracket orders.
5. **Live switch** — only after Rule 6 is met. Start at the smallest possible size.

## Definition of done
- `python -m simplebot backtest` runs end to end from a clean clone in under 5 minutes.
- `README.md` lets a non-expert set up, backtest and paper trade in under 15 minutes.
- Tests cover risk limits, sizing, cost model, and the end-of-day flatten.
- Report clearly states: net return, max drawdown, Sharpe, number of trades, total costs paid, and vs. buy-and-hold.

Ask me before adding any dependency beyond: pandas, numpy, pyarrow, pyyaml, python-dotenv, requests, ib_async, python-telegram-bot, pytest, ruff.
