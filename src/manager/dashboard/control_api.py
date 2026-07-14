"""Dashboard control router — the dashboard's sole write-like surface.

``src/manager/dashboard/api.py`` is deliberately GET-only (see the module
docstring there and `.claude/rules/06b-dashboard.md` invariant 1). This module
is a **separate** router carrying the one narrow, deliberate exception:
stopping a bot on demand.

This route never touches Postgres — it calls directly into the in-process
``Supervisor`` (see `.claude/rules/06b-dashboard.md`, the subsection right
after invariant 1, for the full rationale). It is audit-logged and remains
loopback-only, mounted on the same app as the read-only router.

# TODO(phase-9): Add bearer-token / OIDC auth here when public exposure lands.
"""

from __future__ import annotations

__all__ = ["make_control_router"]

import logging
from typing import Any

from fastapi import APIRouter

from src.risk.audit import KIND_BOT_STOP_REQUESTED

logger = logging.getLogger(__name__)


def make_control_router(supervisor: Any, audit: Any) -> APIRouter:
    """Build the dashboard control router.

    Args:
        supervisor: The in-process ``Supervisor`` instance whose ``stop_bot``
            coroutine this route calls directly.
        audit: The manager's ``AuditLogger`` instance, reused (not
            reconstructed) so the stop request lands in the same append-only
            audit trail as every other bot-lifecycle event.

    Returns:
        An ``APIRouter`` prefixed ``/api/control`` with exactly one route:
        ``POST /api/control/bots/{bot_id}/stop``. This singularity is
        intentional — see `.claude/rules/06b-dashboard.md`.
    """
    control_router = APIRouter(prefix="/api/control")

    @control_router.post("/bots/{bot_id}/stop")
    async def stop_bot(bot_id: str) -> dict[str, Any]:
        """Stop one running bot by id.

        Args:
            bot_id: Stable bot identifier.

        Returns:
            ``{"bot_id": ..., "stopped": bool}``. ``stopped=False`` means the
            bot wasn't running — a no-op, not an error.
        """
        stopped = await supervisor.stop_bot(bot_id)
        await audit.write(
            bot_id=bot_id,
            kind=KIND_BOT_STOP_REQUESTED,
            payload={
                "bot_id": bot_id,
                "source": "dashboard",
                "result": "stopped" if stopped else "not_running",
            },
        )
        return {"bot_id": bot_id, "stopped": stopped}

    return control_router
