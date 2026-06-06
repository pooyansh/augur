"""Structured logger with an optional Postgres audit trail.

:class:`ActivityLogger` is a thin coordination layer that:

* Always logs to stdout via structlog (JSON lines).
* Optionally writes to the ``audit_log`` Postgres table via
  :class:`~src.state.repository.AuditLogger` when a session factory is
  available.
* Never raises on DB failures — the audit trail is best-effort; log output is
  the reliable signal.

Typical usage in a script::

    async with ActivityLogger.create("snipe-bot-1") as logger:
        await logger.log(KIND_BOT_STARTED, {"trigger": "0.30"})
        ...
        await logger.log(KIND_ORDER_INTENT, {...}, client_order_id="abc123")

The class also accepts an explicit ``session_factory`` argument for contexts
where the factory is already constructed (e.g. inside the manager).
"""

from __future__ import annotations

__all__ = ["ActivityLogger"]

import logging
import os
from typing import Any

import structlog

from src.observability.context import set_bot_id
from src.observability.logging import configure_logging

_log = logging.getLogger(__name__)


def _build_session_factory() -> Any | None:
    """Build a SQLAlchemy async session factory from environment variables.

    Mirrors the pattern in ``src/manager/__main__.py::_build_session_factory``.

    Returns:
        An ``async_sessionmaker`` if ``POSTGRES_HOST`` is set, else ``None``.
    """
    host = os.environ.get("POSTGRES_HOST")
    if not host:
        return None

    port = os.environ.get("POSTGRES_PORT", "5432")
    db = os.environ.get("POSTGRES_DB", "bidder")
    user = os.environ.get("POSTGRES_USER", "bidder")
    password = os.environ.get("POSTGRES_PASSWORD", "")
    url = f"postgresql+asyncpg://{user}:{password}@{host}:{port}/{db}"

    from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

    engine: AsyncEngine = create_async_engine(url, pool_pre_ping=True)
    return async_sessionmaker(engine, expire_on_commit=False), engine


class ActivityLogger:
    """Structured logger with an optional Postgres audit trail.

    Always logs to stdout via structlog (JSON).  Writes to the ``audit_log``
    table when Postgres is reachable.  Never raises on DB failures — the DB
    path is best-effort.

    Do not instantiate directly; use :meth:`create`.
    """

    def __init__(
        self,
        bot_id: str,
        *,
        _db_logger: Any | None = None,
        _engine: Any | None = None,
    ) -> None:
        self._bot_id = bot_id
        self._db_logger = _db_logger
        self._engine = _engine  # owned by this instance if we created it
        self._struct_log = structlog.get_logger()

    @classmethod
    async def create(
        cls,
        bot_id: str,
        session_factory: Any | None = None,
    ) -> ActivityLogger:
        """Create and configure an :class:`ActivityLogger`.

        Performs the following startup steps:

        1. Sets ``bot_id`` in the observability ContextVar so all log lines
           carry it automatically.
        2. Calls :func:`~src.observability.logging.configure_logging` (idempotent).
        3. If ``session_factory`` is provided, uses it directly.
        4. Otherwise attempts to build one from ``POSTGRES_HOST`` env var.
        5. If no factory is available, the DB audit trail is disabled (no-op).

        Args:
            bot_id: Stable bot identifier — stamped on every log line and audit row.
            session_factory: Pre-built SQLAlchemy async session factory.  When
                ``None`` the factory is constructed from environment variables.

        Returns:
            A ready-to-use :class:`ActivityLogger`.
        """
        set_bot_id(bot_id)
        configure_logging()

        db_logger = None
        owned_engine = None

        if session_factory is not None:
            from src.state.repository import AuditLogger

            db_logger = AuditLogger(session_factory)
        else:
            result = _build_session_factory()
            if result is not None:
                sf, owned_engine = result
                from src.state.repository import AuditLogger

                db_logger = AuditLogger(sf)

        return cls(bot_id, _db_logger=db_logger, _engine=owned_engine)

    async def log(
        self,
        kind: str,
        payload: dict[str, Any],
        *,
        client_order_id: str | None = None,
        exchange_order_id: str | None = None,
    ) -> None:
        """Log an event to stdout (always) and Postgres (best-effort).

        Args:
            kind: Event kind string from :mod:`src.risk.audit` constants.
            payload: Structured event payload — must be JSON-serialisable.
            client_order_id: Deterministic order id when applicable.
            exchange_order_id: Exchange-assigned order id when available.
        """
        # Always log to stdout via structlog
        self._struct_log.info(kind, bot_id=self._bot_id, **payload)

        # Best-effort Postgres write
        if self._db_logger is not None:
            try:
                await self._db_logger.write(
                    self._bot_id,
                    kind,
                    payload,
                    client_order_id=client_order_id,
                    exchange_order_id=exchange_order_id,
                )
            except Exception as exc:
                structlog.get_logger().warning(
                    "audit_log_write_failed",
                    bot_id=self._bot_id,
                    kind=kind,
                    error=str(exc),
                )

    async def close(self) -> None:
        """Dispose the database engine if this instance owns it.

        Safe to call multiple times — subsequent calls are no-ops.
        """
        if self._engine is not None:
            await self._engine.dispose()
            self._engine = None

    async def __aenter__(self) -> ActivityLogger:
        """Support ``async with ActivityLogger.create(...) as logger`` usage.

        Returns:
            Self.
        """
        return self

    async def __aexit__(self, *_: object) -> None:
        """Dispose engine on context exit."""
        await self.close()
