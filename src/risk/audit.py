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
