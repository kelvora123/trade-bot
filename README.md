# Simplebot

A small, readable, **paper-first** day-trading bot for a UK retail account. One config file,
one command to backtest, one to paper trade, one to go live. Deterministic Python makes every
trading decision; an optional AI coach reviews the log after the close and proposes changes
that must pass a backtest gate before anything is applied.

> **Read this first.** The research behind this bot ([docs/RESEARCH.md](docs/RESEARCH.md))
> found that, for a £100–£5,000 account, trading costs are larger than the edges the published
> intraday strategies offer. The best-replicated strategy (opening range breakout on QQQ) breaks
> even at about 2 cents per share of slippage, while Trading 212 charges a 0.15% FX fee on each
> side of every USD trade, and UK retail accounts cannot buy SPY or QQQ at all. The bot exists
> to let you test that for yourself safely, with every cost switched on, before any real money
> is involved. Expect most backtests to say "no".

## What you get

- `backtest`: walk-forward (first 70% of days trains, last 30% is out of sample), always after
  commission, spread, slippage and FX fee, with a buy-and-hold benchmark and a
  "random entry, same exits" benchmark on every report.
- Three strategies, each a 30-line pure function: opening range breakout (`orb`),
  intraday momentum (`intraday_momentum`) and VWAP reversion (`vwap_revert`).
- Risk limits enforced in code, not in the strategy: max 0.5% of equity at risk per trade,
  2% daily loss stop (flatten and stop for the day), max 3 open positions, flatten everything
  10 minutes before the close.
- `paper`: the same engine running live on the built-in simulator or on the Trading 212 demo
  account, as a long-lived process, with every decision logged to SQLite and an optional daily
  Telegram summary.
- A learning loop: a deterministic walk-forward re-fit, plus an optional AI coach
  (Claude via the Anthropic API, or a local model via Ollama) whose proposals are gated by
  the backtest and never touch your risk settings.
- Optional, off by default: Claude (or a local model) making buy and sell decisions inside
  the trading loop, as a last resort when the rules have nothing, as a final say on every rule
  signal, or as the primary decider. Sizing, stops, the daily loss stop and the end-of-day
  flatten stay in code whatever it says. See "Claude in the loop".
- `live`: only unlocks when `mode: live` is set **and** `SIMPLEBOT_LIVE_CONFIRM=yes` is in
  `.env` **and** the strategy has passed Rule 6 (beats buy-and-hold out of sample after costs
  and has 20+ paper days within the expected range).

## 15-minute setup

```bash
git clone https://github.com/kelvora123/trade-bot.git && cd trade-bot
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"          # add claude and/or ibkr extras if you want them: ".[dev,claude,ibkr]"
cp .env.example .env             # fill in only what you use; .env is git-ignored
python -m simplebot backtest     # ORB on QQQ+SPY, last 60 days of 5-minute Yahoo bars
```

No internet or just curious? `python -m simplebot backtest --data synthetic` runs on fake data
(clearly labelled; it says nothing about markets).

The command prints one line per strategy and writes `reports/backtest_<strategy>_<time>.md`
and `.html`. The report always states, out of sample and after costs: net return, max
drawdown, Sharpe, number of trades, win rate, average R, total costs paid, and the same for
buy-and-hold and for random entries with the same exits. If the trade count is under 30, the
report says so: treat those numbers as noise.

Useful variants:

```bash
python -m simplebot backtest --strategy all        # one comparison table for all strategies
python -m simplebot backtest --grid                # grid-search params on the training window first
python -m simplebot backtest --data alpaca --days 500   # years of 1m/5m US bars with a free Alpaca account
python -m simplebot report                         # re-render the last saved report
```

## Paper trading

```bash
python -m simplebot paper
```

With `broker: paper` (the default) this runs the engine on the built-in simulator, which fills
market orders at the latest price moved against you by half the spread plus slippage, and
charges the configured fees. With `broker: trading212` and demo keys in `.env`, orders go to
your Trading 212 **demo** account instead. Either way the bot:

- polls bars every `poll_seconds` during market hours, feeds each completed bar to the engine,
  and checks software stops against the latest price in between;
- logs every signal, skip (with the reason), order, fill, exit, halt and daily summary to
  `simplebot.sqlite`;
- sends a daily summary to Telegram if `TELEGRAM_TOKEN` and `TELEGRAM_CHAT_ID` are set;
- flattens everything on Ctrl-C / SIGTERM.

Run it as a service: see [deploy/](deploy/) for a systemd unit (`simplebot-paper.service`), a
timer for the coach, a Dockerfile and a compose file.

## config.yaml

| Key | Meaning |
|---|---|
| `mode` | `paper` or `live`. Live also needs `SIMPLEBOT_LIVE_CONFIRM=yes` in `.env`. |
| `broker` | `paper` (simulator), `trading212` (demo in paper mode, live in live mode) or `ibkr`. |
| `account`, `account_currency` | Trading 212 account type; the FX fee applies to instruments in another currency. |
| `symbols` | Yahoo-style symbols. LSE symbols end in `.L`. |
| `timeframe` | `1m`, `5m` or `15m` bars. |
| `strategy`, `params` | Which strategy and its parameters. `params` is the only line the learning loop ever rewrites. |
| `starting_cash` | Backtest and simulator starting cash, in the instrument's currency. |
| `allow_short` | Keep `false` for Trading 212 Invest/ISA accounts. |
| `max_entries_per_day` | Per symbol. |
| `risk.*` | Risk per trade %, daily loss %, max positions, flatten window. Never changed by code. |
| `costs.*` | Commission per order, FX fee % per side, spread bps, slippage bps per side. |
| `data.*` | Provider (`yahoo`, `alpaca`, `csv`, `synthetic`), lookback days, cache dir. |
| `learning.*` | Re-fit cadence and the gate thresholds (minimum out-of-sample trades, minimum Sharpe gain). |
| `coach.*` | `provider` (`none`, `claude`, `ollama`), model names, lookback, `auto_apply_params`, daily cost cap. |
| `decider.*` | Claude in the trading loop: `provider`, `role` (`last_resort`, `final_say`, `primary`), model, `every_minutes`, `max_stop_pct`, `min_confidence`, `max_daily_cost_usd`, `effort`, `timeout_seconds`. |
| `symbol_map` | Your symbol to the broker's ticker, e.g. `{EQQQ.L: EQQQl_EQ}`. |
| `poll_seconds`, `journal_path`, `reports_dir`, `telegram` | Plumbing. |

## Writing a strategy

A strategy is a module with one pure function. `bars` is every completed bar up to and
including the one that just closed, for one symbol, in the exchange's timezone. Return a
`Signal` to enter at the market now, or `None`. No I/O, no broker calls.

```python
# simplebot/strategies/my_breakout.py  (or any .py file: --strategy-file path/to/it.py)
from simplebot.strategies import Signal
from simplebot.strategies.common import daily_atr, today_bars

DEFAULT_PARAMS = {"lookback": 6, "target_r": 3}
PARAM_GRID = {"lookback": [3, 6, 12], "target_r": [2, 3, 5]}  # optional, for the learning loop


def signal(bars, params):
    p = {**DEFAULT_PARAMS, **params}
    today = today_bars(bars)
    if len(today) != p["lookback"] + 1:  # fire once, right after the lookback window
        return None
    high = float(today["high"].iloc[:-1].max())
    close = float(today["close"].iloc[-1])
    if close <= high:
        return None
    atr = daily_atr(bars) or close * 0.01
    stop = close - 0.5 * atr
    return Signal("long", close, stop, close + p["target_r"] * (close - stop), reason="breakout")
```

Register a built-in in `simplebot/strategies/__init__.py` or just point `--strategy-file` at it.
Stops and targets are enforced by `risk.py`, exits at end of day too; the strategy only proposes.

## Risk rules (all in `simplebot/risk.py`)

- Position size = (equity x `risk_per_trade_pct`) / (entry - stop), capped by what cash can
  buy after spread, slippage and fees. No leverage. Small accounts are usually cash-capped,
  which the report counts (`cash_capped_entries`).
- Daily loss stop: when equity falls `max_daily_loss_pct` below the day's starting equity,
  everything is flattened and no new entries are taken until the next day.
- Max open positions, max entries per symbol per day, no shorts unless `allow_short: true`.
- End-of-day flatten `flatten_minutes_before_close` minutes before that exchange's close.
- Software stops and targets: checked on every completed bar and on every poll in between.
  If both are touched in one bar the stop is assumed to have hit first; gaps fill at the open,
  never at the level.

## Costs (always on)

`costs.spread_bps` (half paid on each side), `costs.slippage_bps` (each side),
`costs.commission` (per order) and `costs.fx_fee_pct` (each side, when the instrument's
currency is not `account_currency`). Defaults model a Trading 212 GBP Invest account trading USD
instruments: 0 commission, 0.15% FX each way, 2 bps spread, 3 bps slippage, which is a 0.38%
round trip before the trade has done anything. Set `fx_fee_pct: 0` only if you have confirmed
your account trades in the instrument's own currency. For Interactive Brokers use
`commission: 1` (the USD 1 minimum) and a realistic slippage figure.

## The learning loop

Two layers, neither inside the trading loop, both gated.

**The gate** (`simplebot/learn.py`): a challenger replaces the current parameters only if its
out-of-sample, after-cost backtest beats the champion's Sharpe by `learning.min_sharpe_gain`
with at least `learning.min_oos_trades` trades and a positive net return. Nothing bypasses it,
and nothing automated can touch `risk:`.

**Layer 1, deterministic:** `python -m simplebot refit` grid-searches each strategy's
`PARAM_GRID` on the training window and sends the winner through the gate. If it passes, the
`params:` line in `config.yaml` is rewritten (comments kept) and the change is recorded in the
journal's `params_history`, which the backtest report lists with before/after Sharpe. Run it
weekly from cron or a timer. Any applied change resets the 20-day paper clock for Rule 6.

**Layer 2, the AI coach:** `python -m simplebot coach` runs once after the close, as its own
process (never from the trading process):

- reads the last `coach.lookback_days` of the decision log, the latest backtest, the parameter
  history and its own previous notes, so it remembers what it already tried;
- writes a plain-English review to `reports/coach/<date>.md` and up to three proposals as
  structured JSON: `params` (new values for existing parameters), `filter` or `strategy`
  (a Python file);
- every `params` proposal goes through the gate; rejected ones are written back into the
  coach's notes with the numbers. A proposal that passes is applied only if
  `coach.auto_apply_params: true`, otherwise it is listed for you;
- code proposals are written to `simplebot/strategies/proposed/`, backtested in a subprocess
  with every secret stripped from the environment, and reported. They are never applied or
  run in paper/live until you review them and merge them yourself;
- `coach.provider: claude` uses the official `anthropic` SDK (`pip install -e ".[claude]"`,
  `ANTHROPIC_API_KEY` in `.env`, model from `coach.model`); a daily review costs a few cents and
  is capped by `coach.max_daily_cost_usd`. `coach.provider: ollama` uses a local model through
  Ollama at `OLLAMA_HOST`, free and offline, with weaker proposals.
- The coach never sees broker keys, cannot import the broker modules (a test enforces it), and
  a refusal, timeout or error simply means "no proposal today".

A coach cannot turn a losing edge into a winning one. It finds and tests hypotheses faster than
you would. Most proposals should be rejected; that is the system working.

## Claude in the loop (optional, off by default)

This suspends hard rule 2 on purpose, at the owner's request. Set `decider.provider: claude`
(or `ollama`) and pick a role:

| `decider.role` | What happens |
|---|---|
| `last_resort` (default) | The rule-based strategy decides whenever it has a signal. Only when it has nothing is the model asked, at most every `every_minutes` per symbol, whether to buy, sell or hold. |
| `final_say` | Every rule-based signal is sent to the model, which approves it, rejects it, or tightens the stop and target. The model never initiates a trade. |
| `primary` | The model is asked every `every_minutes` whether to buy, sell, close or hold, with the rule-based signal shown as advice. It can also close an open position early. |

What the model sees: today's bars, the last ten daily bars, VWAP, the opening range, ATR, the
open position, the rule-based signal, the account state, the risk limits and the round-trip
cost. What it answers: one structured JSON object with `action`, `stop_price`, `target_price`,
`confidence` and a one-line `reason`. Every answer is journaled with its cost.

What stays in code no matter what the model says:

- position size from `risk_per_trade_pct` and the stop, capped by cash;
- the stop must be on the correct side of the price and no further than `max_stop_pct`;
  anything else is treated as hold; a stop that is too wide is tightened;
- answers below `min_confidence` are hold;
- the daily loss stop, max positions, max entries per day, `allow_short` and the end-of-day
  flatten all apply to the model's trades exactly as to the rules';
- once `max_daily_cost_usd` is spent the model is not asked again that day;
- any error, timeout, refusal or unreadable answer means hold. The model never sees broker keys
  and `decider.py` cannot import the broker modules (a test enforces it).

Costs and speed: one decision is roughly 3,500 input tokens. On `claude-opus-5-5` that is about
2 cents per call, so the default cadence of 30 minutes on two symbols is about 50 cents a
trading day; `claude-sonnet-5-5` halves it and `claude-haiku-5-5` is near free. Each call takes
a few seconds, which is fine for a loop that polls every 30 seconds.

Backtesting it: `python -m simplebot backtest --llm --max-llm-cost 5` consults the model over
the out-of-sample period only. The command prints the estimated number of calls and cost first
and stops asking once the cap is hit. It is slow (every call is a real API round trip) and
not deterministic: two runs give different trades. Rule 6 still applies: `check` compares the
decider setting used in the saved backtest with the one in `config.yaml`, so a model-driven
configuration has to earn its way to live like any other.

Be clear-eyed about what this is. A language model reading a table of bars has no information
edge over the rules, it adds latency and cost per decision, and it cannot be replayed. The
honest reason to run it is to find out, on the demo account, whether it does better than the
rules after costs. The journal and the daily summary will tell you.

## Going live

1. Run `python -m simplebot check`. It prints whether Rule 6 is met: a saved backtest for the
   configured strategy that beats buy-and-hold on Sharpe out of sample after costs, matching
   params, at least 20 paper trading days with trades, paper average R within 0.5R of the
   backtest, and no more than two daily-loss halts.
2. Set `mode: live` and a real broker in `config.yaml`, and `SIMPLEBOT_LIVE_CONFIRM=yes` in
   `.env` (use live API keys).
3. `python -m simplebot live`. It prints a loud banner and starts at whatever size your
   `risk_per_trade_pct` and cash allow; keep both small.

On any unexpected error in live mode the bot cancels open orders, flattens, sends an alert
and stops. On Ctrl-C it flattens first.

### Trading 212 specifics (from the research, verify against your account)

- Authentication is HTTP Basic with the API key and secret from the app (Settings, API).
  Demo and live keys are different.
- **Live accepts market orders only through the API.** Stops and targets are therefore
  software stops: the bot polls prices every `poll_seconds` and sends a market exit when a level
  is crossed. **Gap risk:** a fast move or a feed outage can produce a loss larger than the
  planned 0.5%. The daily loss stop limits the damage but cannot prevent it.
- The API provides **no prices**. Bars come from the data provider; Yahoo quotes can lag.
- UK retail accounts **cannot buy US-domiciled ETFs** (SPY, QQQ). Use LSE-listed UCITS
  equivalents (EQQQ, CSPX/VUSA) in London hours, or US single stocks. Find the broker ticker
  with `python -m simplebot instruments --search EQQQ` and put it in `symbol_map`.
- The 0.15% FX fee applies to every conversion. Whether the API can trade from a USD balance
  on a multi-currency Invest account is unverified; test it in demo before assuming
  `fx_fee_pct: 0`. ISAs cannot hold USD.
- Rate limits are enforced with a token bucket per endpoint and 429s back off automatically.

### Interactive Brokers (optional)

`broker: ibkr` uses [ib_async](https://github.com/ib-api-reloaded/ib_async) against a running
TWS or IB Gateway (`IBKR_HOST`, `IBKR_PORT`, `IBKR_CLIENT_ID` in `.env`; 4002/7497 are the paper
ports). The adapter can place native bracket orders (market entry with a child stop and
take-profit held at IBKR). It has only been exercised against a fake gateway in tests; treat it
as a starting point and paper trade it first.

## Data providers

| Provider | Needs | Bars | History | Notes |
|---|---|---|---|---|
| `yahoo` (default) | nothing | 1m, 5m, 15m | 1m: ~30 days (7-day chunks); 5m/15m: 60 days | Keyless, personal use, rate limited. LSE (`.L`) intraday coverage unverified. |
| `alpaca` | free account keys | 1m, 5m | since 2016 | IEX feed; on the free plan the last 15 minutes are withheld, so paper trading on it lags. US only. |
| `csv` | `data/<SYMBOL>_<tf>m.csv` | any | yours | Columns `timestamp,open,high,low,close,volume`. |
| `synthetic` | nothing | any | any | Deterministic fake data for tests and demos only. |

Bars are cached as parquet in `cache/`, so history keeps accumulating the longer the bot runs,
beyond any provider's own window.

## Tests, CI and secrets

```bash
ruff check . && ruff format --check .
pytest -q
python scripts/secret_scan.py --all
```

Tests cover the cost model, sizing, every risk limit, the end-of-day flatten, the software stop
logic, the paper broker, the Trading 212 adapter (with a fake HTTP session), the IBKR adapter
(with a fake gateway), the backtester and benchmarks, the gate, the coach (with a fake model),
the runner and the CLI. GitHub Actions runs lint, the secret scan, the tests and a synthetic
backtest on every push; it never runs the bot.

Secrets live only in `.env`, which is git-ignored. `pip install pre-commit && pre-commit install`
adds a hook that blocks commits containing anything that looks like a key, plus `ruff`.

## Project layout

```
config.yaml                 the only file you edit
.env.example                keys and tokens go in .env (git-ignored)
simplebot/
  main.py                   CLI: backtest | paper | live | report | coach | refit | check | instruments
  config.py sessions.py     config loading/validation, exchange hours
  data.py                   bar providers and the parquet cache
  costs.py                  the cost model
  strategies/               orb.py, intraday_momentum.py, vwap_revert.py, random_entry.py (benchmark)
  risk.py                   sizing, daily loss stop, position limits, flatten window, stop checks
  brokers/                  base.py (interface), paper.py, trading212.py, ibkr.py, ratelimit.py
  engine.py                 the bar-by-bar engine shared by backtest, paper and live
  backtest.py report.py     walk-forward backtester and benchmarks; Markdown/HTML reports
  journal.py                SQLite decision log
  runner.py notify.py       the long-running loop; Telegram
  learn.py coach.py llm.py  the learning loop: re-fit + gate; the AI coach; Claude/Ollama clients
tests/                      pytest suite
scripts/secret_scan.py      pre-commit / CI secret scan
deploy/                     systemd units, coach timer, Dockerfile, compose
docs/RESEARCH.md            Step 0 research with sources
```

## The hard rules

1. Paper first. Live needs `mode: live` and `SIMPLEBOT_LIVE_CONFIRM=yes`.
2. No LLM calls in the trading loop unless you set `decider.provider`. Even then sizing, stops,
   the daily loss stop and the flatten stay in code, and the coach still only proposes.
3. Costs are always on. There is no gross-only number anywhere.
4. Risk limits are enforced in code, not in the strategy.
5. Secrets only from `.env`; a pre-commit scan blocks keys.
6. A strategy may go live only after beating buy-and-hold out of sample after costs and
   20+ paper days within the expected range. `python -m simplebot check` is the judge.
