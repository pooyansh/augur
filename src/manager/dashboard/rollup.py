"""PerfRollupRefresher — background task that keeps perf_rollup fresh.

Runs ``REFRESH MATERIALIZED VIEW CONCURRENTLY perf_rollup`` every 60 seconds
using a short-lived asyncpg connection opened with the **write** credentials
(the dashboard_reader role has no REFRESH privilege).

Errors are logged at ``warn`` and never propagate to the caller.  The tick loop
continues regardless of transient DB failures.
"""

from __future__ import annotations

__all__ = ["PerfRollupRefresher"]

import asyncio
import contextlib
import logging
import os

import asyncpg  # type: ignore[import-untyped]

logger = logging.getLogger(__name__)

_REFRESH_INTERVAL_S = 60


def _build_write_dsn() -> str:
    """Build the write-role DSN from standard env vars.

    Returns:
        ``postgresql://`` DSN string using POSTGRES_* env vars.
    """
    host = os.environ.get("POSTGRES_HOST", "localhost")
    port = os.environ.get("POSTGRES_PORT", "5432")
    db = os.environ.get("POSTGRES_DB", "bidder")
    user = os.environ.get("POSTGRES_USER", "bidder")
    password = os.environ.get("POSTGRES_PASSWORD", "")
    return f"postgresql://{user}:{password}@{host}:{port}/{db}"


class PerfRollupRefresher:
    """Periodically refreshes the ``perf_rollup`` materialized view.

    Args:
        dsn: Optional explicit write-role DSN.  Defaults to env-var-derived DSN.
        interval_s: Refresh interval in seconds (default 60).
    """

    def __init__(
        self,
        dsn: str | None = None,
        *,
        interval_s: int = _REFRESH_INTERVAL_S,
    ) -> None:
        self._dsn = dsn or _build_write_dsn()
        self._interval_s = interval_s
        self._task: asyncio.Task[None] | None = None
        self._stopping = False

    async def start(self) -> None:
        """Launch the background refresh loop as an asyncio task."""
        self._stopping = False
        self._task = asyncio.create_task(self._loop(), name="perf-rollup-refresher")
        logger.info(
            "PerfRollupRefresher started (interval=%ds).",
            self._interval_s,
        )

    async def stop(self) -> None:
        """Cancel the background loop and wait for it to finish."""
        self._stopping = True
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        logger.info("PerfRollupRefresher stopped.")

    async def _loop(self) -> None:
        """Refresh loop — sleep, then refresh, forever."""
        while not self._stopping:
            try:
                await asyncio.sleep(self._interval_s)
            except asyncio.CancelledError:
                break
            await self._refresh()

    async def _refresh(self) -> None:
        """Run one REFRESH MATERIALIZED VIEW CONCURRENTLY.

        Opens a fresh connection, executes the refresh, then closes it.
        All exceptions are caught and logged at ``warn``.
        """
        conn: asyncpg.Connection[asyncpg.Record] | None = None
        try:
            conn = await asyncpg.connect(self._dsn)
            await conn.execute("REFRESH MATERIALIZED VIEW CONCURRENTLY perf_rollup")
            logger.debug("perf_rollup refreshed.")
        except Exception as exc:
            logger.warning("perf_rollup refresh failed: %s", exc)
        finally:
            if conn is not None:
                with contextlib.suppress(Exception):
                    await conn.close()
