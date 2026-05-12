"""Alerting layer — severity-routed delivery to Slack, Discord, Telegram.

Public API::

    from src.alerts import AlertRouter, Severity, make_default_router
    from src.alerts.dedup import Deduper
    from src.alerts.redact import make_redactor

See :mod:`src.alerts.router` for usage examples.
"""

from __future__ import annotations

from src.alerts.dedup import Deduper
from src.alerts.router import AlertRouter, Severity, make_default_router

__all__ = [
    "AlertRouter",
    "Deduper",
    "Severity",
    "make_default_router",
]
