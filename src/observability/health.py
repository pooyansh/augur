"""Health checker for the /healthz endpoint.

:class:`HealthChecker` performs two checks on every call:
1. Postgres ping (``SELECT 1``).
2. All registered bots' heartbeat ages are within the configured tolerance.

Returns a :class:`HealthReport` dict that the FastAPI route serialises.
"""

from __future__ import annotations

__all__ = ["HealthChecker", "HealthReport"]

import logging
from datetime import UTC, datetime
from typing import Any, TypedDict

logger = logging.getLogger(__name__)

# Default tolerance: a bot heartbeat older than this is unhealthy.
_DEFAULT_HEARTBEAT_TOLERANCE_S: float = 60.0


class BotHealthEntry(TypedDict):
    """Health entry for a single bot."""

    bot_id: str
    heartbeat_age_s: float
    ok: bool


class HealthReport(TypedDict):
    """Full health report returned by :meth:`HealthChecker.check`."""

    healthy: bool
    postgres_ok: bool
    bots: list[BotHealthEntry]
    ts: str


class HealthChecker:
    """Async health checker that backs the /healthz endpoint.

    Args:
        supervisor: Optional :class:`~src.manager.supervisor.Supervisor` instance.
            If None, the bots check is skipped (all-ok).
        session_factory: Optional SQLAlchemy async_sessionmaker for Postgres ping.
            If None, postgres_ok is reported as False.
        heartbeat_tolerance_s: Seconds after which a bot heartbeat is considered stale.
    """

    def __init__(
        self,
        supervisor: Any | None = None,
        session_factory: Any | None = None,
        heartbeat_tolerance_s: float = _DEFAULT_HEARTBEAT_TOLERANCE_S,
    ) -> None:
        self._supervisor = supervisor
        self._session_factory = session_factory
        self._tolerance = heartbeat_tolerance_s

    async def check(self) -> HealthReport:
        """Run all health checks and return a :class:`HealthReport`.

        Returns:
            HealthReport with healthy=True only if Postgres is reachable and all
            bots have recent heartbeats.  Returns 503-worthy data otherwise.
        """
        postgres_ok = await self._ping_postgres()
        bot_entries = self._check_bots()

        all_bots_ok = all(b["ok"] for b in bot_entries) if bot_entries else True
        healthy = postgres_ok and all_bots_ok

        return HealthReport(
            healthy=healthy,
            postgres_ok=postgres_ok,
            bots=bot_entries,
            ts=datetime.now(tz=UTC).isoformat(),
        )

    async def _ping_postgres(self) -> bool:
        """Attempt a ``SELECT 1`` against Postgres.

        Returns:
            True if the ping succeeded; False if session_factory is None or
            the query raised any exception.
        """
        if self._session_factory is None:
            return False
        try:
            from sqlalchemy import text

            async with self._session_factory() as session:
                await session.execute(text("SELECT 1"))
            return True
        except Exception as exc:
            logger.warning("Postgres ping failed: %s", exc)
            return False

    def _check_bots(self) -> list[BotHealthEntry]:
        """Read heartbeat ages from the supervisor.

        Returns:
            List of :class:`BotHealthEntry` — one per supervised bot.
            Empty list if no supervisor is configured.
        """
        if self._supervisor is None:
            return []

        try:
            statuses = self._supervisor.status()
        except Exception as exc:
            logger.warning("Supervisor.status() failed: %s", exc)
            return []

        entries: list[BotHealthEntry] = []
        for s in statuses:
            age = float(s.heartbeat_age_s)
            entries.append(
                BotHealthEntry(
                    bot_id=s.bot_id,
                    heartbeat_age_s=age,
                    ok=age <= self._tolerance,
                )
            )
        return entries
