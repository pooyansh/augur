"""Translate heartbeat metric payloads into Prometheus metric updates.

The bot embeds a ``metrics`` field in its heartbeat JSON line.  The manager's
heartbeat server calls :func:`apply_heartbeat_metrics` on every received line
that contains that field.

Counter fields are treated as deltas (additive — the bot sends the increment
since the last beat).  Histogram fields are raw sample lists (each value is
observed individually).  Gauge fields are absolute ``set()`` calls.
"""

from __future__ import annotations

__all__ = ["HeartbeatRecord", "apply_heartbeat_metrics"]

import logging
from typing import Any, TypedDict

logger = logging.getLogger(__name__)


class HeartbeatRecord(TypedDict, total=False):
    """Typed shape of a heartbeat JSON line from a bot.

    All fields are optional for backward-compatibility: the server ignores
    lines without ``metrics`` and still processes the base health fields.
    """

    bot_id: str
    ts: str
    snapshot_lag_s: float
    last_error: str | None
    metrics: dict[str, Any]


def apply_heartbeat_metrics(record: HeartbeatRecord) -> None:
    """Update the central Prometheus registry from a bot's heartbeat record.

    Backward-compatible: if ``record`` has no ``metrics`` key, this is a no-op.

    Counter semantics: the value in ``order_intent_total`` sub-keys is the
    delta since the last beat.  We call ``.inc(delta)`` rather than ``set``.

    Histogram semantics: ``*_obs`` lists contain raw sample values emitted
    since the last beat.  We call ``.observe(v)`` for each.

    Gauge semantics: ``position_notional_usd``, ``pnl_*``, ``snapshot_lag_s``
    are absolute — we call ``.set(v)``.

    Args:
        record: Parsed heartbeat dict from the bot subprocess.
    """
    metrics = record.get("metrics")
    if not metrics:
        return

    bot_id = record.get("bot_id", "unknown")

    # We need the strategy label to satisfy Histogram/Counter label sets.
    # The heartbeat does not carry strategy directly — use an empty string
    # as a sentinel until the supervisor can enrich it.  This is acceptable
    # because the Supervisor updates BOT_HEARTBEAT_AGE_SECONDS with the
    # full strategy label separately.
    strategy = metrics.get("strategy", "")

    try:
        _apply(bot_id, strategy, metrics)
    except Exception as exc:
        logger.warning("apply_heartbeat_metrics failed for bot %s: %s", bot_id, exc)


def _apply(bot_id: str, strategy: str, metrics: dict[str, Any]) -> None:
    """Inner apply — imported lazily to avoid circular import at module load time."""
    from src.observability.metrics import (
        BOT_SNAPSHOT_LAG_SECONDS,
        BOT_TICK_LATENCY,
        BOT_TICK_OVERRUN_TOTAL,
        ORDER_FILL_LATENCY,
        ORDER_INTENT_TOTAL,
        PNL_REALIZED_USD,
        PNL_UNREALIZED_USD,
        POSITION_NOTIONAL_USD,
    )

    # Tick latency — raw observation list
    tick_lat_obs: list[float] = metrics.get("tick_latency_seconds_obs", [])
    for v in tick_lat_obs:
        BOT_TICK_LATENCY.labels(bot_id=bot_id, strategy=strategy).observe(float(v))

    # Tick overrun — counter delta
    overrun_delta: int = int(metrics.get("tick_overrun_total", 0))
    if overrun_delta > 0:
        BOT_TICK_OVERRUN_TOTAL.labels(bot_id=bot_id, strategy=strategy).inc(overrun_delta)

    # Order intent — dict of {result: delta}
    intent_counts: dict[str, int] = metrics.get("order_intent_total", {})
    for result, delta in intent_counts.items():
        delta_int = int(delta)
        if delta_int > 0:
            ORDER_INTENT_TOTAL.labels(bot_id=bot_id, strategy=strategy, result=result).inc(
                delta_int
            )

    # Order fill latency — raw observation list
    fill_lat_obs: list[float] = metrics.get("order_fill_latency_seconds_obs", [])
    for v in fill_lat_obs:
        ORDER_FILL_LATENCY.labels(bot_id=bot_id, strategy=strategy).observe(float(v))

    # Position notional — absolute gauge
    if "position_notional_usd" in metrics:
        POSITION_NOTIONAL_USD.labels(bot_id=bot_id, strategy=strategy).set(
            float(metrics["position_notional_usd"])
        )

    # PnL gauges — absolute
    if "pnl_realized_usd" in metrics:
        PNL_REALIZED_USD.labels(bot_id=bot_id, strategy=strategy).set(
            float(metrics["pnl_realized_usd"])
        )
    if "pnl_unrealized_usd" in metrics:
        PNL_UNREALIZED_USD.labels(bot_id=bot_id, strategy=strategy).set(
            float(metrics["pnl_unrealized_usd"])
        )

    # Snapshot lag — absolute gauge
    if "snapshot_lag_s" in metrics:
        BOT_SNAPSHOT_LAG_SECONDS.labels(bot_id=bot_id).set(float(metrics["snapshot_lag_s"]))
