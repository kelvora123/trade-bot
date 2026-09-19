"""Layer 3b: Claude-backed news analysis.

The reference design runs one API call per held name per day. This module does
the same job for a fraction of the spend, via four deliberate changes:

1. **One batched call for the whole book**, not one per name. A 10-name book
   goes from 10 requests/day to 1 -- the system prompt and instructions are
   sent once instead of ten times, which is where most of the input tokens on
   a small job actually go.
2. **Content-addressed caching.** The cache key is a hash of the model plus the
   exact headline set. If no new headline appeared since the last run, the
   answer is served from SQLite and costs nothing. Markets are quiet most days;
   this is the single biggest saving.
3. **A hard daily call cap** (``news.max_calls_per_day``). Reached, the layer
   returns a degraded result rather than spending more.
4. **Haiku by default.** Summarising and classifying headlines is a bulk
   extraction job. Set ``news.model: claude-opus-5`` in config if you want the
   stronger model on it.

Off by default (``news.enabled: false``): the rest of the system runs, and
costs nothing, without ever calling an API.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from ..config import Config
from ..store import Store
from ..types import utcnow

log = logging.getLogger(__name__)

__all__ = ["analyse_news", "NewsResult", "estimate_cost_usd"]

SYSTEM_PROMPT = (
    "You are a markets analyst summarising news for a specific options and equity book. "
    "You are given recent headlines grouped by ticker. For each ticker, report only what "
    "the headlines actually say happened. Do not speculate about price direction, do not "
    "recommend trades, and do not predict. If the headlines for a ticker are routine noise "
    "(analyst-rating roundups, listicles, generic market recaps), say so plainly and set "
    "sentiment to neutral. Flag position_relevant only when a headline describes a concrete, "
    "dated corporate event -- earnings, guidance, M&A, regulatory action, executive change, "
    "product recall, litigation -- that a position holder would want to know about today."
)

# Per-MTok USD list prices, for the run report's cost estimate only.
_PRICING: dict[str, tuple[float, float]] = {
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-sonnet-5": (2.00, 10.00),
    "claude-opus-5": (5.00, 25.00),
}


def estimate_cost_usd(model: str, input_tokens: int, output_tokens: int) -> float | None:
    rates = _PRICING.get(model)
    if rates is None:
        return None
    return input_tokens / 1e6 * rates[0] + output_tokens / 1e6 * rates[1]


def _cache_key(model: str, grouped: dict[str, list[dict]]) -> str:
    """Hash of model + the exact headline set, order-independent.

    Titles rather than publisher timestamps: the same story is re-emitted with
    a fresh timestamp by several outlets, and keying on time would re-bill for
    news already analysed.
    """
    material = {t: sorted(h["title"] for h in items) for t, items in sorted(grouped.items())}
    blob = json.dumps({"model": model, "headlines": material}, sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()


def _schema(tickers: list[str]) -> dict[str, Any]:
    """JSON schema pinning the output to exactly the spec's four fields."""
    return {
        "type": "object",
        "properties": {
            "per_ticker": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "ticker": {"type": "string", "enum": tickers},
                        "summary": {
                            "type": "string",
                            "description": "Two sentences at most on what actually happened.",
                        },
                        "sentiment": {"type": "string", "enum": ["positive", "neutral", "negative"]},
                        "key_drivers": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "The concrete facts behind the read.",
                        },
                        "position_relevant": {
                            "type": "boolean",
                            "description": "True only for a concrete, dated corporate event.",
                        },
                        "relevance_note": {"type": "string"},
                    },
                    "required": [
                        "ticker",
                        "summary",
                        "sentiment",
                        "key_drivers",
                        "position_relevant",
                        "relevance_note",
                    ],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["per_ticker"],
        "additionalProperties": False,
    }


@dataclass
class NewsResult:
    asof: date
    enabled: bool
    per_ticker: list[dict[str, Any]] = field(default_factory=list)
    cached: bool = False
    api_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    model: str = ""
    notes: list[str] = field(default_factory=list)

    @property
    def estimated_cost_usd(self) -> float | None:
        if not self.api_calls:
            return 0.0
        return estimate_cost_usd(self.model, self.input_tokens, self.output_tokens)

    def to_json(self) -> dict[str, Any]:
        cost = self.estimated_cost_usd
        return {
            "layer": "3b_news_analysis",
            "asof": self.asof.isoformat(),
            "enabled": self.enabled,
            "model": self.model or None,
            "per_ticker": self.per_ticker,
            "usage": {
                "api_calls": self.api_calls,
                "served_from_cache": self.cached,
                "input_tokens": self.input_tokens,
                "output_tokens": self.output_tokens,
                "estimated_cost_usd": None if cost is None else round(cost, 5),
            },
            "notes": self.notes,
            "disclaimer": "Summary of published headlines. Not advice, not a forecast.",
        }


def _collect(provider: Any, tickers: list[str], cfg: Config) -> tuple[dict[str, list[dict]], list[str]]:
    grouped: dict[str, list[dict]] = {}
    notes: list[str] = []
    for ticker in tickers:
        try:
            items = provider.headlines(ticker, cfg.news.lookback_days)
        except Exception as exc:
            log.warning("headline fetch failed for %s: %s", ticker, exc)
            notes.append(f"{ticker}: headline fetch failed ({exc.__class__.__name__})")
            continue
        if items:
            grouped[ticker] = items[: cfg.news.max_headlines_per_ticker]
    return grouped, notes


def _render_prompt(grouped: dict[str, list[dict]], cfg: Config) -> str:
    lines = [
        f"Book tickers: {', '.join(sorted(grouped))}",
        f"Headline window: last {cfg.news.lookback_days} days.",
        "",
    ]
    for ticker in sorted(grouped):
        lines.append(f"## {ticker}")
        for h in grouped[ticker]:
            pub = f" ({h['publisher']})" if h.get("publisher") else ""
            lines.append(f"- {h['title']}{pub}")
            if h.get("summary"):
                lines.append(f"  {h['summary'][:200]}")
        lines.append("")
    lines.append("Return one entry per ticker listed above, and none for any other ticker.")
    return "\n".join(lines)


def analyse_news(provider: Any, tickers: list[str], cfg: Config, store: Store, asof: date) -> NewsResult:
    """Summarise recent headlines for every held name in one batched call."""
    result = NewsResult(asof=asof, enabled=cfg.news.enabled, model=cfg.news.model)

    if not cfg.news.enabled:
        result.notes.append("News layer disabled (news.enabled=false). No API calls made.")
        return result

    if not cfg.anthropic_api_key:
        result.notes.append("ANTHROPIC_API_KEY not set; news layer skipped. No API calls made.")
        return result

    grouped, notes = _collect(provider, sorted(set(tickers)), cfg)
    result.notes.extend(notes)
    if not grouped:
        result.notes.append("No headlines found in the lookback window. No API calls made.")
        return result

    key = _cache_key(cfg.news.model, grouped)
    cached = store.get_news_cache(key)
    if cached is not None:
        result.per_ticker = cached.get("per_ticker", [])
        result.cached = True
        result.notes.append("Served from cache: headline set unchanged since it was last analysed.")
        return result

    if store.llm_calls_today(asof) >= cfg.news.max_calls_per_day:
        result.notes.append(
            f"Daily API call cap reached ({cfg.news.max_calls_per_day}); news layer skipped today."
        )
        return result

    try:
        import anthropic
    except ImportError:
        result.notes.append("`anthropic` package not installed; news layer skipped.")
        return result

    client = anthropic.Anthropic(api_key=cfg.anthropic_api_key)
    prompt = _render_prompt(grouped, cfg)
    tickers_present = sorted(grouped)

    request: dict[str, Any] = {
        "model": cfg.news.model,
        "max_tokens": cfg.news.max_output_tokens,
        # Stable content first so the system prompt stays a cacheable prefix
        # across runs; only the headline block below it changes day to day.
        "system": [{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
        "messages": [{"role": "user", "content": prompt}],
        "output_config": {"format": {"type": "json_schema", "schema": _schema(tickers_present)}},
    }

    try:
        counted = client.messages.count_tokens(
            model=cfg.news.model, system=SYSTEM_PROMPT, messages=request["messages"]
        )
        log.info("news layer: %d input tokens for %d tickers", counted.input_tokens, len(tickers_present))
    except Exception as exc:  # token counting is advisory; never block on it
        log.debug("count_tokens failed: %s", exc)

    try:
        response = client.messages.create(**request)
    except anthropic.BadRequestError as exc:
        result.notes.append(f"News layer: bad request ({exc.message}). Skipped.")
        return result
    except anthropic.AuthenticationError:
        result.notes.append("News layer: ANTHROPIC_API_KEY rejected. Skipped.")
        return result
    except anthropic.NotFoundError:
        result.notes.append(f"News layer: model {cfg.news.model!r} not found. Skipped.")
        return result
    except anthropic.RateLimitError:
        # A nightly job has no deadline; the next run picks it up.
        result.notes.append("News layer: rate limited. Skipped this run; will retry next run.")
        return result
    except anthropic.APIStatusError as exc:
        result.notes.append(f"News layer: API error {exc.status_code}. Skipped.")
        return result
    except anthropic.APIConnectionError:
        result.notes.append("News layer: could not reach the API. Skipped.")
        return result

    if response.stop_reason == "refusal":
        result.notes.append("News layer: request was declined by the model's safety system. Skipped.")
        return result

    try:
        text = next(b.text for b in response.content if b.type == "text")
        parsed = json.loads(text)
        result.per_ticker = parsed.get("per_ticker", [])
    except (StopIteration, json.JSONDecodeError, AttributeError) as exc:
        result.notes.append(f"News layer: could not parse response ({exc.__class__.__name__}). Skipped.")
        return result

    result.api_calls = 1
    result.input_tokens = response.usage.input_tokens
    result.output_tokens = response.usage.output_tokens

    created = utcnow().isoformat()
    store.put_news_cache(key, asof, {"per_ticker": result.per_ticker}, created)
    store.record_llm_usage(
        asof, created, cfg.news.model, result.input_tokens, result.output_tokens, "news_analysis"
    )
    result.notes.append(
        f"One batched call covered {len(tickers_present)} tickers "
        f"(the per-name design would have made {len(tickers_present)})."
    )
    return result
