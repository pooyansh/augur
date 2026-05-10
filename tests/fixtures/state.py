"""In-memory fakes for the state layer — no Postgres required.

These replace the real :class:`~src.state.repository.StateRepository`,
:class:`~src.state.repository.KillSwitchReader`, and
:class:`~src.state.repository.AuditLogger` in unit tests and the Hypothesis
property suite.
"""

from __future__ import annotations

__all__ = [
    "InMemoryAuditLogger",
    "InMemoryKillSwitch",
    "InMemoryStateRepository",
]

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any


class InMemoryStateRepository:
    """In-memory replacement for :class:`~src.state.repository.StateRepository`.

    Stores exactly one snapshot per ``bot_id`` (matching the real DB schema
    where ``bot_id`` is the primary key and rows are upserted).
    """

    def __init__(self) -> None:
        self._store: dict[str, dict[str, Any]] = {}

    async def latest_snapshot(self, bot_id: str) -> dict[str, Any] | None:
        """Return the stored snapshot for ``bot_id``, or ``None``."""
        return self._store.get(bot_id)

    async def write_snapshot(
        self,
        bot_id: str,
        market_id: str,
        version: int,
        state: Mapping[str, Any],
    ) -> None:
        """Upsert the snapshot for ``bot_id``."""
        self._store[bot_id] = dict(state)


class InMemoryKillSwitch:
    """Combined reader and writer for the kill switch, backed by a simple bool.

    Implements the :class:`~src.state.repository.KillSwitchReader` interface
    (``is_tripped()``) and adds direct ``trip``/``reset`` methods for test
    convenience.
    """

    def __init__(self, *, tripped: bool = False) -> None:
        self._tripped = tripped

    async def is_tripped(self) -> bool:
        """Return the current kill-switch state."""
        return self._tripped

    def trip(self, reason: str = "test") -> None:
        """Activate the kill switch synchronously (test helper)."""
        self._tripped = True

    def reset(self) -> None:
        """Deactivate the kill switch synchronously (test helper)."""
        self._tripped = False


class InMemoryAuditLogger:
    """Append-only in-memory audit log.

    Recorded rows are accessible via :attr:`rows` for assertions.
    """

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    async def write(
        self,
        bot_id: str,
        kind: str,
        payload: Mapping[str, Any],
        *,
        client_order_id: str | None = None,
        exchange_order_id: str | None = None,
    ) -> None:
        """Append one audit record.

        Args:
            bot_id: Bot that produced the event.
            kind: Event kind string.
            payload: Structured event payload.
            client_order_id: Optional deterministic order id.
            exchange_order_id: Optional exchange-assigned id.
        """
        self.rows.append(
            {
                "ts": datetime.now(tz=UTC),
                "bot_id": bot_id,
                "kind": kind,
                "client_order_id": client_order_id,
                "exchange_order_id": exchange_order_id,
                "payload": dict(payload),
            }
        )

    def accepted_rows(self) -> list[dict[str, Any]]:
        """Return only rows with kind ``"order_accepted"``."""
        return [r for r in self.rows if r["kind"] == "order_accepted"]

    def submitted_rows(self) -> list[dict[str, Any]]:
        """Return only rows with kind ``"order_submitted"``."""
        return [r for r in self.rows if r["kind"] == "order_submitted"]
