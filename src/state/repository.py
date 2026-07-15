"""State-layer repository classes.

All repositories take an ``async_sessionmaker`` so the caller controls the
engine (real Postgres in production, aiosqlite or in-memory fake in tests).
"""

from __future__ import annotations

__all__ = [
    "AuditLogger",
    "KillSwitchReader",
    "KillSwitchWriter",
    "StateRepository",
]

import asyncio
import logging
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.state.models import AuditLog, BotState, KillSwitch

logger = logging.getLogger(__name__)

# How long (seconds) to cache the kill-switch DB read before re-querying.
_KILL_SWITCH_CACHE_TTL = 1.0


class StateRepository:
    """Read and write bot state snapshots.

    Args:
        session_factory: SQLAlchemy async session factory bound to a running
            engine.
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sf = session_factory

    async def latest_snapshot(self, bot_id: str) -> dict[str, Any] | None:
        """Return the most recent snapshot state for ``bot_id``, or ``None``.

        Args:
            bot_id: Stable bot identifier.

        Returns:
            The JSONB ``state`` dict from the latest :class:`~src.state.models.BotState`
            row, or ``None`` if no snapshot exists yet.
        """
        async with self._sf() as session:
            result = await session.execute(
                select(BotState)
                .where(BotState.bot_id == bot_id)
                .order_by(BotState.snapshot_at.desc())
                .limit(1)
            )
            row = result.scalar_one_or_none()
            if row is None:
                return None
            state_data: dict[str, Any] = dict(row.state)
            return state_data

    async def write_snapshot(
        self,
        bot_id: str,
        market_id: str,
        version: int,
        state: Mapping[str, Any],
        mode: str | None = None,
        strategy: str | None = None,
    ) -> None:
        """Upsert a snapshot row (insert or replace, keyed on bot_id PK).

        Args:
            bot_id: Stable bot identifier (primary key).
            market_id: Market the bot is running against; indexed for the
                deferred ``market_history`` view (Phase 5).
            version: Monotonically increasing snapshot version counter.
            state: Arbitrary serialisable dict from :meth:`BaseBot.snapshot`.
            mode: The bot's actual configured mode (``"paper"``/``"live"``),
                from ``BotConfig`` — not part of the JSONB ``state`` contract.
                Dashboard-facing only; see :class:`~src.state.models.BotState`.
            strategy: The bot's registered strategy name, from ``BotConfig``.
                Dashboard-facing only, same as ``mode``.
        """
        async with self._sf() as session:
            existing = await session.get(BotState, bot_id)
            if existing is None:
                session.add(
                    BotState(
                        bot_id=bot_id,
                        snapshot_at=datetime.now(tz=UTC),
                        version=version,
                        market_id=market_id,
                        state=dict(state),
                        mode=mode,
                        strategy=strategy,
                    )
                )
            else:
                existing.snapshot_at = datetime.now(tz=UTC)  # type: ignore[assignment]
                existing.version = version  # type: ignore[assignment]
                existing.market_id = market_id  # type: ignore[assignment]
                existing.state = dict(state)  # type: ignore[assignment]
                existing.mode = mode  # type: ignore[assignment]
                existing.strategy = strategy  # type: ignore[assignment]
            await session.commit()


class KillSwitchReader:
    """Read-only view of the global kill switch with a short-lived cache.

    The cache avoids hammering the DB on every ``BaseBot.place`` call while
    keeping the kill-switch latency under ``_KILL_SWITCH_CACHE_TTL`` seconds.

    Args:
        session_factory: SQLAlchemy async session factory.
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sf = session_factory
        self._cached: bool = False
        self._cache_expires_at: float = 0.0

    async def is_tripped(self) -> bool:
        """Return ``True`` if the kill switch is currently active.

        Uses a ~1-second in-memory cache to avoid DB round-trips on every
        ``place()`` call.

        Returns:
            ``True`` when orders must be blocked; ``False`` when normal.
        """
        now = asyncio.get_event_loop().time()
        if now < self._cache_expires_at:
            return self._cached
        async with self._sf() as session:
            row = await session.get(KillSwitch, 1)
            tripped = bool(row.tripped) if row is not None else False
        self._cached = tripped
        self._cache_expires_at = now + _KILL_SWITCH_CACHE_TTL
        return tripped


class KillSwitchWriter:
    """Write access to the global kill switch.

    Args:
        session_factory: SQLAlchemy async session factory.
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sf = session_factory

    async def trip(self, reason: str) -> None:
        """Activate the kill switch, blocking all future orders.

        Args:
            reason: Free-text reason stored in the DB and included in alerts.
        """
        async with self._sf() as session:
            await session.execute(
                update(KillSwitch)
                .where(KillSwitch.id == 1)
                .values(
                    tripped=True,
                    reason=reason,
                    tripped_at=datetime.now(tz=UTC),
                )
            )
            await session.commit()
        logger.warning("Kill switch TRIPPED: %s", reason)

    async def reset(self) -> None:
        """Deactivate the kill switch, allowing orders to flow again."""
        async with self._sf() as session:
            await session.execute(
                update(KillSwitch)
                .where(KillSwitch.id == 1)
                .values(tripped=False, reason=None, tripped_at=None)
            )
            await session.commit()
        logger.info("Kill switch reset.")


class AuditLogger:
    """Append-only writer for the ``audit_log`` table.

    Every call inserts a new row.  Callers must never attempt corrections by
    updating existing rows — corrections are new rows.

    Args:
        session_factory: SQLAlchemy async session factory.
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sf = session_factory

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
            kind: Event kind string (e.g. ``"order_submitted"``).
            payload: Structured event payload stored as JSONB.
            client_order_id: Deterministic id from BaseBot when applicable.
            exchange_order_id: Exchange-assigned id when available.
        """
        async with self._sf() as session:
            session.add(
                AuditLog(
                    ts=datetime.now(tz=UTC),
                    bot_id=bot_id,
                    kind=kind,
                    client_order_id=client_order_id,
                    exchange_order_id=exchange_order_id,
                    payload=dict(payload),
                )
            )
            await session.commit()
