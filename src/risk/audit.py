"""Audit-log re-export and typed record shape.

:class:`~src.state.repository.AuditLogger` lives in the state layer.  This
module re-exports it under ``src.risk`` and adds :class:`AuditRecord` — a
typed dataclass that callers assemble before passing to
:meth:`~src.state.repository.AuditLogger.write`.

Using :class:`AuditRecord` is optional but recommended for non-trivial
payloads: it makes the field contract explicit at the call-site and prevents
typos in ``kind`` strings.

Invariant 5 (audit log is append-only) is enforced by the DB schema (no
``UPDATE``/``DELETE`` privileges on ``audit_log``) and by the
:class:`~src.state.repository.AuditLogger` implementation which only ever
inserts.  Corrections are new rows that reference the original row via
``payload["references_audit_id"]``.
"""

from __future__ import annotations

__all__ = [
    "KIND_BOT_COOLDOWN",
    "KIND_BOT_STARTED",
    "KIND_BOT_STOPPED",
    "KIND_ERROR",
    "KIND_FEED_ERROR",
    "KIND_FEED_RECONNECTED",
    "KIND_FEED_STARTED",
    "KIND_KILL_SWITCH_CANCEL_FAILED",
    "KIND_LIVE_DOWNGRADE",
    "KIND_MARKET_RESOLVED",
    "KIND_MARKET_SETTLED",
    "KIND_ORDER_ACCEPTED",
    "KIND_ORDER_INTENT",
    "KIND_ORDER_REJECTED",
    "KIND_ORDER_RESULT",
    "KIND_ORDER_SUBMITTED",
    "KIND_RISK_CAP_EXCEEDED",
    "KIND_TRIGGER_FIRED",
    "KIND_TRIGGER_MISSED",
    "AuditLogger",
    "AuditRecord",
]

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from src.state.repository import AuditLogger


@dataclass
class AuditRecord:
    """Typed representation of one audit-log row.

    Use :meth:`to_write_kwargs` to unpack into
    :meth:`~src.state.repository.AuditLogger.write`.

    Args:
        bot_id: Bot that produced the event.
        kind: Event kind string (e.g. ``"order_submitted"``).
            Use the constants in :mod:`src.risk.audit` where available.
        payload: Structured event payload stored as JSONB.
        client_order_id: Deterministic id from BaseBot when applicable.
        exchange_order_id: Exchange-assigned id when available.
        ts: UTC datetime of the event; defaults to now.
    """

    bot_id: str
    kind: str
    payload: dict[str, Any] = field(default_factory=dict)
    client_order_id: str | None = None
    exchange_order_id: str | None = None
    ts: datetime = field(default_factory=lambda: datetime.now(tz=UTC))

    def to_write_kwargs(self) -> dict[str, Any]:
        """Return kwargs suitable for :meth:`~src.state.repository.AuditLogger.write`.

        Returns:
            Dict with keys: ``bot_id``, ``kind``, ``payload``,
            ``client_order_id``, ``exchange_order_id``.
        """
        return {
            "bot_id": self.bot_id,
            "kind": self.kind,
            "payload": self.payload,
            "client_order_id": self.client_order_id,
            "exchange_order_id": self.exchange_order_id,
        }


# ---------------------------------------------------------------------------
# Canonical kind strings
# ---------------------------------------------------------------------------

KIND_ORDER_SUBMITTED = "order_submitted"
KIND_ORDER_ACCEPTED = "order_accepted"
KIND_ORDER_REJECTED = "order_rejected"
KIND_KILL_SWITCH_CANCEL_FAILED = "kill_switch_cancel_failed"
KIND_LIVE_DOWNGRADE = "live_downgrade"
KIND_BOT_COOLDOWN = "bot_cooldown"
KIND_RISK_CAP_EXCEEDED = "risk_cap_exceeded"

# Feed / market lifecycle
KIND_MARKET_RESOLVED = "market_resolved"
KIND_MARKET_SETTLED = "market_settled"
KIND_FEED_STARTED = "feed_started"
KIND_FEED_RECONNECTED = "feed_reconnected"
KIND_FEED_ERROR = "feed_error"

# Order events (generic, usable by all bots and scripts)
KIND_ORDER_INTENT = "order_intent"
KIND_ORDER_RESULT = "order_result"

# Bot lifecycle
KIND_BOT_STARTED = "bot_started"
KIND_BOT_STOPPED = "bot_stopped"
KIND_TRIGGER_FIRED = "trigger_fired"
KIND_TRIGGER_MISSED = "trigger_missed"
KIND_ERROR = "error"
