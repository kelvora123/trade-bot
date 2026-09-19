# trade-bot

A layered options-portfolio analytics system. It values a book, analyses its
exposures, reads the market environment, and writes a dated report — then
pushes that report and the underlying data back to GitHub via n8n or GitHub
Actions.

It is built from a three-layer prompt architecture, with one deliberate change:
**Claude is used as little as possible.** Layers 1 and 2 are pure arithmetic.
Layer 3's macro gate is deterministic. Only the news reader calls an API, it is
off by default, and when on it makes one batched, cached call per day for the
whole book.

---

## Read this before you start

**This is an analytics system, not an auto-trader.** It surfaces facts about
your positions — what they are worth, what they are decaying at, where IV sits
relative to its own history, what is expiring soon. It does not place orders,
and by design it does not recommend them. The expiry watch reports that a
contract has 12 days left and is losing $4.10/day; it will not tell you to roll
it. That boundary is deliberate and the tests enforce it.

**Paper trading is the only execution mode.** There is no live-broker adapter
in this repo, and adding one is not a small change.

**On turning £100 into £400,000.** That is a 4,000× return. The system prints
the arithmetic on every run, because it is the number that decides whether a
plan is a plan:

| Horizon | Compounded monthly return required |
|---|---:|
| 1 year | **99.6%/month** |
| 2 years | **41.3%/month** |
| 5 years | **14.8%/month** |
| 10 years | **7.2%/month** |

At 2%/month — a genuinely good sustained retail result — it takes **419 months
(35 years)**. At 5%/month, sustained, which almost nobody achieves, 170 months.
The returns required to do it "soon" are not a strategy problem; position sizes
large enough to chase them are the most reliable way to reach £0 first.

**There is also a hard structural floor.** One US equity option contract
controls 100 shares, so a quoted premium of $1.20 costs **$120** plus
commission. With £100 you can buy at most one cheap contract, with no
diversification and no room for the risk controls this system is built around.
The paper broker rejects unaffordable orders with the exact shortfall rather
than pretending:

```
$ tradebot paper --check-affordable NVDA260320C00180000 --qty 1 --mark 13.0
Not affordable: needs 1300.65 GBP, have 100.00, short 1200.65.
```

The realistic path is to run this in paper mode against a realistic simulated
balance, accumulate the snapshot history the IV-rank layer needs (20 days
minimum, 252 for a full ranking), and fund a real account only once the
reports have told you something true for a few months.

---

## The three layers

### Layer 1 — Data & Valuation

Pulls option chains, marks the book, computes Greeks.

- **Mark** = mid `(bid+ask)/2` when both sides exist, else `last`. Shares mark
  at spot. A zero bid is a real quote; a crossed book falls through to last.
- **Greeks** from Black-Scholes computed locally in `layer1/blackscholes.py` —
  stdlib `math` only, no scipy, no QuantLib. Uses the contract's own IV, a
  configurable risk-free rate (default `0.045`), and *actual* calendar days to
  expiry. Verified against reference values and put-call parity to machine
  precision.
- **Units**: vega is per 1 IV point and theta is per calendar day, scaled once
  at the source so nothing downstream double-scales them.
- **Snapshots**: every run writes the full chain to
  `snapshots/TICKER_YYYY-MM-DD.json` and mirrors it to SQLite. This is not
  logging — it is the input to Layer 2's IV rank and to new-strike detection.
- **Degradation**: a missing contract, absent spot, or broken IV flags the
  position and continues. A partial book is worth reporting; a crashed nightly
  run is not. Chain IV outside a sane band is re-solved locally by bisection.

### Layer 2 — Portfolio Analytics

- **Allocation** by ticker and by sector (config lookup first, provider
  metadata as fallback). Uses *absolute* value per line, so a short leg cannot
  net away a long leg's weight and hide the concentration.
- **Concentration flags** above a configurable ticker cap (default 40%) and
  sector cap (default 60%). Informational only.
- **Aggregate Greeks**: net delta in share-equivalents and in dollars, total
  daily theta in dollars (what the book loses per day if nothing moves), net
  vega per 1 IV point, net gamma.
- **IV environment**: current IV plus rank and percentile over a configurable
  lookback (default 252 days). Below `min_history_days` (default 20) it reports
  `building history` rather than a meaningless number. Flags `rich` above 70
  and `cheap` below 30.
  - Ranked against an **underlying ATM-IV series**, not the contract's own IV:
    a single contract's IV drifts as it ages for reasons unrelated to the vol
    environment, which would make a 252-day rank meaningless.
- **Expiry watch**: contracts inside a configurable DTE threshold (default 45)
  with their daily theta in dollars and as a share of position value. States
  facts; does not advise rolling.

### Layer 3 — Macro Gate & News

- **Macro gate** (`layer3/macro.py`): a deterministic 0–100 score — 100 calm,
  0 stressed — blended from weighted components that must sum to 1.0: VIX
  level, VIX 1-year percentile, VIX term structure (VIX vs VIX3M), breadth
  (RSP/SPY as the proxy), and credit (HYG/TLT). Same data in, same score out;
  a test asserts it. A missing feed is dropped and the remaining weights
  renormalised, so a dead endpoint costs precision rather than silently
  biasing the score to 50.
- **News** (`layer3/news.py`): Claude summarises recent headlines per held
  name — what happened, sentiment, key drivers, and whether it specifically
  affects a position. See below.

---

## How Claude usage is minimised

The reference design runs **one API call per held name per day**. This one does
the same job for a fraction of the spend:

| | Reference | Here |
|---|---|---|
| Calls/day, 10-name book | 10 | **1** (batched) |
| Unchanged headlines | re-billed daily | **served from cache, £0** |
| Default state | on | **off** |
| Model | unspecified | `claude-haiku-4-5` (configurable) |
| Runaway protection | none | hard `max_calls_per_day` |

1. **One batched call for the whole book.** The system prompt and instructions
   are sent once instead of N times, which is where most input tokens go on a
   small job.
2. **Content-addressed cache.** The key is a hash of the model plus the exact
   headline set, keyed on titles rather than timestamps (the same story is
   re-emitted with fresh timestamps by different outlets). No new headline
   means no API call. Markets are quiet most days — this is the largest saving.
3. **Hard daily cap.** At `news.max_calls_per_day` the layer degrades and says
   so, rather than spending more.
4. **Haiku by default.** Summarising headlines is bulk extraction. Set
   `news.model: claude-opus-5` if you want the stronger model on it.

Layers 1, 2 and 3a never call an API at all. Track spend with `tradebot usage`.

---

## Quick start

```bash
git clone https://github.com/kelvora123/trade-bot.git && cd trade-bot
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

cp config/portfolio.example.yaml config/portfolio.yaml
$EDITOR config/portfolio.yaml

tradebot run --no-news          # full pipeline, zero API cost
```

### Commands

| Command | What it does |
|---|---|
| `tradebot run` | All layers; writes `reports/report_YYYY-MM-DD.{md,json}` and `latest.*` |
| `tradebot run --offline` | Replays stored snapshots — no network |
| `tradebot run --no-news` | Forces the Claude layer off |
| `tradebot value` | Layer 1 only, as JSON |
| `tradebot macro` | The deterministic macro gate only (free) |
| `tradebot paper --check-affordable SYM --qty 1 --mark 13.0` | Prices a hypothetical order |
| `tradebot usage` | Token spend and estimated cost to date |

### Portfolio format

`qty` is contracts for options (negative for short); `cost_basis` is the quoted
premium **per share**, not premium × 100.

```yaml
holdings:
  - ticker: NVDA
    kind: option
    option_kind: call
    qty: 2
    strike: 180.0
    expiry: 2026-03-20
    cost_basis: 12.50
    target: 25.00
    stop: 6.00
```

### Enabling the news layer

```bash
export ANTHROPIC_API_KEY=sk-ant-...     # or put it in .env
# set news.enabled: true in config/config.yaml
tradebot run --news
tradebot usage                           # confirm what it cost
```

---

## n8n → GitHub

Two importable workflows in `n8n/workflows/`. Both push results back to this
repository. See **[n8n/README.md](n8n/README.md)** for setup.

- **`daily-run-and-push.json`** — self-hosted n8n with the repo checked out
  locally. Pulls, runs the bot, collects `reports/` and `snapshots/`, and
  commits each file through the GitHub Contents API (looking up the blob SHA
  first so it creates *or* updates correctly).
- **`dispatch-github-actions.json`** — n8n Cloud, where there is no local
  checkout. Triggers `.github/workflows/daily-run.yml` via `workflow_dispatch`,
  waits, then reads `reports/latest.json` back and flattens it to the handful
  of numbers worth alerting on.

`.github/workflows/daily-run.yml` also runs on its own schedule as a backstop
if n8n is down.

---

## Testing

```bash
pytest -q                    # 102 tests, fully offline
ruff check src tests scripts
python scripts/validate_n8n.py
```

The suite needs no network, no API key, and no market data. `OfflineProvider`
replays chains from SQLite, which makes any past run reproducible — the
snapshots are an audit trail *and* a data source.

Coverage worth knowing about: Black-Scholes against published reference values
and put-call parity; the marking rules including crossed books and zero bids;
that shorts count toward concentration rather than netting against it; that the
macro gate is deterministic and survives dead feeds; that an unchanged headline
set costs nothing; and that £100 cannot buy a $1,300 contract.

---

## Layout

```
src/tradebot/
  types.py           Holding, ChainRow, MarkedPosition
  config.py          every threshold from the layer specs, validated at load
  store.py           SQLite: snapshots, IV history, runs, news cache, paper ledger
  portfolio.py       portfolio.yaml loader
  layer1/  blackscholes.py  provider.py  snapshots.py  valuation.py
  layer2/  analytics.py
  layer3/  macro.py  news.py
  paper/   broker.py
  report/  render.py
  cli.py
n8n/workflows/       importable workflow JSON
.github/workflows/   CI and the daily run
```

---

*Informational software. Not financial advice. Options can expire worthless and
short options can lose more than the premium received.*
