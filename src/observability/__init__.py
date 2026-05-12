"""Observability package — structured logging, metrics, and health checks.

Public surface:
    configure_logging: Set up structlog JSON renderer with redaction.
    bind_tick_id: Context manager to bind a tick_id for the current log context.
    tick_id_var: ContextVar holding the current tick_id.
    bot_id_var: ContextVar holding the current bot_id.
    current_tick_id: Helper returning the current tick_id or None.
    set_bot_id: Helper to set bot_id in the contextvar.
    render_metrics: Render Prometheus metrics to bytes.
    HealthChecker: Async health check for /healthz.
    apply_heartbeat_metrics: Translate a heartbeat metrics payload to Prometheus updates.
"""

from __future__ import annotations

__all__ = [
    "HealthChecker",
    "apply_heartbeat_metrics",
    "bind_tick_id",
    "bot_id_var",
    "configure_logging",
    "current_tick_id",
    "render_metrics",
    "set_bot_id",
    "tick_id_var",
]

from src.observability.context import bot_id_var, current_tick_id, set_bot_id, tick_id_var
from src.observability.health import HealthChecker
from src.observability.heartbeat_metrics import apply_heartbeat_metrics
from src.observability.logging import bind_tick_id, configure_logging
from src.observability.metrics import render_metrics
