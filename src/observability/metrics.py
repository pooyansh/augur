"""Prometheus metric definitions for the prediction-market bot platform.

All metrics are registered in the module-level CollectorRegistry (REGISTRY).
Import this module early in the process lifecycle so the registry is
populated before /metrics is first scraped.

CARDINALITY DISCIPLINE — bot_id and strategy are labels; market_id is NOT a label.
Adding new labels requires updating .claude/rules/09-observability.md and the
Grafana dashboards in the same PR.  Orphan metrics (defined here but not
reflected in dashboards) are a code smell.
"""

from __future__ import annotations

__all__ = [
    "BOT_HEARTBEAT_AGE_SECONDS",
    "BOT_SNAPSHOT_LAG_SECONDS",
    "BOT_TICK_LATENCY",
    "BOT_TICK_OVERRUN_TOTAL",
    "EXCHANGE_RATE_LIMIT_REMAINING",
    "ORDER_FILL_LATENCY",
    "ORDER_INTENT_TOTAL",
    "PNL_REALIZED_USD",
    "PNL_UNREALIZED_USD",
    "POSITION_NOTIONAL_USD",
    "REGISTRY",
    "SIGNAL_FETCH_TOTAL",
    "SIGNAL_STALENESS_SECONDS",
    "WALLET_ALLOWANCE_RATIO",
    "render_metrics",
]

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

# ---------------------------------------------------------------------------
# Central registry — all metrics below are registered here.
# ---------------------------------------------------------------------------

REGISTRY = CollectorRegistry(auto_describe=True)

# ---------------------------------------------------------------------------
# Bot-level metrics
# ---------------------------------------------------------------------------

BOT_TICK_LATENCY: Histogram = Histogram(
    "bot_tick_latency_seconds",
    "Wall-clock time from tick start to on_tick return.",
    ["bot_id", "strategy"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30),
    registry=REGISTRY,
)

BOT_TICK_OVERRUN_TOTAL: Counter = Counter(
    "bot_tick_overrun_total",
    "Number of ticks that ran longer than the configured schedule interval.",
    ["bot_id", "strategy"],
    registry=REGISTRY,
)

BOT_SNAPSHOT_LAG_SECONDS: Gauge = Gauge(
    "bot_snapshot_lag_seconds",
    "Wall-clock age of the latest committed snapshot in seconds.",
    ["bot_id"],
    registry=REGISTRY,
)

BOT_HEARTBEAT_AGE_SECONDS: Gauge = Gauge(
    "bot_heartbeat_age_seconds",
    "Seconds since the manager last received a heartbeat from this bot.",
    ["bot_id"],
    registry=REGISTRY,
)

# ---------------------------------------------------------------------------
# Order metrics
# ---------------------------------------------------------------------------

ORDER_INTENT_TOTAL: Counter = Counter(
    "order_intent_total",
    "Total order intents by result label.",
    ["bot_id", "strategy", "result"],
    registry=REGISTRY,
)

ORDER_FILL_LATENCY: Histogram = Histogram(
    "order_fill_latency_seconds",
    "Time from order intent submitted to fill observed.",
    ["bot_id", "strategy"],
    buckets=(0.1, 0.25, 0.5, 1, 2, 5, 15, 60),
    registry=REGISTRY,
)

# ---------------------------------------------------------------------------
# Signal metrics
# ---------------------------------------------------------------------------

SIGNAL_FETCH_TOTAL: Counter = Counter(
    "signal_fetch_total",
    "Total signal fetch attempts by signal name, source, and result.",
    ["signal", "source", "result"],
    registry=REGISTRY,
)

SIGNAL_STALENESS_SECONDS: Gauge = Gauge(
    "signal_staleness_seconds",
    "Seconds since the last successful fetch for this signal.",
    ["signal"],
    registry=REGISTRY,
)

# ---------------------------------------------------------------------------
# Exchange / wallet metrics
# ---------------------------------------------------------------------------

EXCHANGE_RATE_LIMIT_REMAINING: Gauge = Gauge(
    "exchange_rate_limit_remaining",
    "Remaining rate-limit credits as reported by the exchange.",
    ["exchange"],
    registry=REGISTRY,
)

WALLET_ALLOWANCE_RATIO: Gauge = Gauge(
    "wallet_allowance_ratio",
    "On-chain allowance as a fraction of the configured cap (0-1). "
    "Drift indicates a misconfigured float.",
    ["exchange"],
    registry=REGISTRY,
)

# ---------------------------------------------------------------------------
# PnL / position metrics
# ---------------------------------------------------------------------------

PNL_REALIZED_USD: Gauge = Gauge(
    "pnl_realized_usd",
    "Realized profit-and-loss in USD for this bot.",
    ["bot_id", "strategy"],
    registry=REGISTRY,
)

PNL_UNREALIZED_USD: Gauge = Gauge(
    "pnl_unrealized_usd",
    "Unrealized profit-and-loss in USD for this bot.",
    ["bot_id", "strategy"],
    registry=REGISTRY,
)

POSITION_NOTIONAL_USD: Gauge = Gauge(
    "position_notional_usd",
    "Current position notional value in USD for this bot.",
    ["bot_id", "strategy"],
    registry=REGISTRY,
)


# ---------------------------------------------------------------------------
# Render helper
# ---------------------------------------------------------------------------


def render_metrics() -> tuple[bytes, str]:
    """Render all registered Prometheus metrics to the exposition format.

    Returns:
        A 2-tuple of ``(body_bytes, content_type_string)`` suitable for use
        in a FastAPI route response.
    """
    return generate_latest(REGISTRY), CONTENT_TYPE_LATEST
