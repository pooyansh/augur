"""Kill-switch re-exports and cascade helper.

:class:`~src.state.repository.KillSwitchReader` and
:class:`~src.state.repository.KillSwitchWriter` live in the state layer
(``src.state.repository``) where the SQLAlchemy session factory lives.  This
module re-exports them under the ``src.risk`` namespace so strategy code and
tests can import from a single place.

:class:`KillSwitchCascade` is the stateful helper used by
:meth:`~src.bots.base.BaseBot.run` to issue ``cancel_all`` exactly once per
trip event and reset when the switch is cleared.
"""

from __future__ import annotations

__all__ = [
    "KillSwitchCascade",
    "KillSwitchReader",
    "KillSwitchWriter",
]

import logging
from collections.abc import Mapping
from typing import Any, Protocol, runtime_checkable

from src.state.repository import KillSwitchReader, KillSwitchWriter

logger = logging.getLogger(__name__)


@runtime_checkable
class _Adapter(Protocol):
    """Structural protocol for the exchange adapter's cancel_all method."""

    async def cancel_all(self, market_id: str | None = None) -> int: ...


@runtime_checkable
class _AuditWriter(Protocol):
    """Structural protocol for the audit logger's write method."""

    async def write(
        self,
        bot_id: str,
        kind: str,
        payload: Mapping[str, Any],
        *,
        client_order_id: str | None = None,
        exchange_order_id: str | None = None,
    ) -> None: ...


class KillSwitchCascade:
    """Idempotent cancel-all issuer for the kill-switch path.

    :meth:`~src.bots.base.BaseBot.run` creates one of these per bot and calls
    :meth:`on_trip` every tick where the switch is tripped.  The cascade
    issues ``adapter.cancel_all`` exactly once per consecutive tripped period —
    not once per tripped tick — and resets when the switch is cleared.

    Args:
        audit: Append-only audit logger used to record cancel failures.
        bot_id: Bot identifier, written into audit records.
    """

    def __init__(self, audit: _AuditWriter, bot_id: str) -> None:
        self._audit = audit
        self._bot_id = bot_id
        self._cancel_issued: bool = False

    def reset(self) -> None:
        """Clear the issued flag so the next trip re-issues ``cancel_all``.

        Call this when ``is_tripped()`` returns ``False`` after a trip period.
        """
        self._cancel_issued = False

    async def on_trip(self, adapter: _Adapter, market_id: str) -> None:
        """Issue ``adapter.cancel_all`` once for this trip period.

        Idempotent: a second call within the same trip period is a no-op.
        A cancel failure emits a ``warn`` audit row but does not propagate —
        the kill switch is still honoured (no orders will be placed).

        Args:
            adapter: The exchange adapter bound to this bot.  Must expose
                ``async cancel_all(market_id: str | None) -> int``.
            market_id: The market to cancel; passed through to the adapter.
        """
        if self._cancel_issued:
            return

        self._cancel_issued = True
        try:
            cancelled: int = await adapter.cancel_all(market_id)
            logger.warning(
                "Kill switch tripped — cancel_all issued for bot=%s market=%s cancelled=%d",
                self._bot_id,
                market_id,
                cancelled,
            )
        except Exception as exc:
            logger.warning(
                "Kill switch cascade: cancel_all failed for bot=%s: %s",
                self._bot_id,
                exc,
            )
            # Best-effort: write a warn audit row so the operator knows.
            try:
                await self._audit.write(
                    bot_id=self._bot_id,
                    kind="kill_switch_cancel_failed",
                    payload={"error": str(exc), "market_id": market_id},
                )
            except Exception as audit_exc:
                logger.warning(
                    "Kill switch cascade: audit write also failed for bot=%s: %s",
                    self._bot_id,
                    audit_exc,
                )
