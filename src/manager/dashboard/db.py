"""DashboardDb — read-only asyncpg pool for the dashboard API.

Opens a dedicated ``asyncpg`` connection pool using the ``dashboard_reader``
Postgres role.  All queries are strictly ``SELECT``-only.  The pool is separate
from the write pool used by the supervisor/repositories so the dashboard cannot
accidentally write to the DB even in the face of a code bug.

Credential resolution order:
1. ``DASHBOARD_PG_USER`` / ``DASHBOARD_PG_PASSWORD`` environment variables
   (expected to be the ``dashboard_reader`` role).
2. Fallback to ``POSTGRES_USER`` / ``POSTGRES_PASSWORD`` with a ``warning``
   log — the DB-level role restriction is then NOT enforced.

Connection target: ``POSTGRES_HOST`` / ``POSTGRES_PORT`` / ``POSTGRES_DB``
(same as the write pool — different role, same database).
"""

from __future__ import annotations

__all__ = ["DashboardDb"]

import logging
import os
from datetime import datetime
from typing import Any

import asyncpg  # type: ignore[import-untyped]

logger = logging.getLogger(__name__)

# Failure events: audit_log kinds treated as failures for /api/failures.
_FAILURE_KINDS = frozenset(
    [
        "order_rejected",
        "bot_crash",
        "bot_cooldown",
        "live_downgrade",
        "kill_switch_tripped",
        "signal_stale",
        "snapshot_failed",
    ]
)


def _build_dashboard_dsn() -> str:
    """Assemble the asyncpg DSN for the dashboard reader role.

    Returns:
        A ``postgresql://`` DSN string.
    """
    host = os.environ.get("POSTGRES_HOST", "localhost")
    port = os.environ.get("POSTGRES_PORT", "5432")
    db = os.environ.get("POSTGRES_DB", "bidder")

    user = os.environ.get("DASHBOARD_PG_USER")
    password = os.environ.get("DASHBOARD_PG_PASSWORD")

    if not user:
        user = os.environ.get("POSTGRES_USER", "bidder")
        password = os.environ.get("POSTGRES_PASSWORD", "")
        logger.warning(
            "DASHBOARD_PG_USER not set — falling back to POSTGRES_USER='%s'. "
            "The dashboard_reader role restriction is NOT enforced.",
            user,
        )
    else:
        password = password or ""

    return f"postgresql://{user}:{password}@{host}:{port}/{db}"


class DashboardDb:
    """Read-only asyncpg pool with typed query helpers.

    Args:
        dsn: Optional explicit DSN; if omitted the DSN is built from env vars.
        min_size: Minimum pool connections.
        max_size: Maximum pool connections.
    """

    def __init__(
        self,
        dsn: str | None = None,
        *,
        min_size: int = 1,
        max_size: int = 5,
    ) -> None:
        self._dsn = dsn or _build_dashboard_dsn()
        self._min_size = min_size
        self._max_size = max_size
        self._pool: asyncpg.Pool[asyncpg.Record] | None = None

    async def open(self) -> None:
        """Open the asyncpg connection pool.

        Must be called before any query helper.
        """
        self._pool = await asyncpg.create_pool(
            self._dsn,
            min_size=self._min_size,
            max_size=self._max_size,
        )
        logger.info("DashboardDb pool opened (min=%d, max=%d).", self._min_size, self._max_size)

    async def close(self) -> None:
        """Close the connection pool gracefully."""
        if self._pool is not None:
            await self._pool.close()
            self._pool = None
            logger.info("DashboardDb pool closed.")

    async def ping(self) -> bool:
        """Return True if the database is reachable.

        Returns:
            True on success, False on any connection/query error.
        """
        if self._pool is None:
            return False
        try:
            async with self._pool.acquire() as conn:
                await conn.fetchval("SELECT 1")
            return True
        except Exception as exc:
            logger.warning("DashboardDb ping failed: %s", exc)
            return False

    def _require_pool(self) -> asyncpg.Pool[asyncpg.Record]:
        if self._pool is None:
            raise RuntimeError("DashboardDb.open() must be called before querying.")
        return self._pool

    # ------------------------------------------------------------------
    # Query helpers (one per dashboard endpoint that touches the DB)
    # ------------------------------------------------------------------

    async def fetch_all_bot_states(self) -> list[dict[str, Any]]:
        """Return the latest snapshot row for every bot.

        Returns:
            List of dicts with keys: bot_id, strategy, mode, market_id,
            snapshot_at, version, state.
        """
        pool = self._require_pool()
        rows = await pool.fetch("""
            SELECT
                bs.bot_id,
                bs.strategy,
                bs.mode,
                bs.market_id,
                bs.snapshot_at,
                bs.version,
                bs.state
            FROM bot_state bs
            ORDER BY bs.bot_id
        """)
        return [dict(r) for r in rows]

    async def fetch_bot_state(self, bot_id: str) -> dict[str, Any] | None:
        """Return the latest snapshot for a single bot, or None.

        Args:
            bot_id: Bot identifier.

        Returns:
            Dict with snapshot data or None if not found.
        """
        pool = self._require_pool()
        row = await pool.fetchrow(
            """
            SELECT
                bs.bot_id,
                bs.strategy,
                bs.mode,
                bs.market_id,
                bs.snapshot_at,
                bs.version,
                bs.state
            FROM bot_state bs
            WHERE bs.bot_id = $1
            """,
            bot_id,
        )
        return dict(row) if row else None

    async def fetch_bot_audit(self, bot_id: str, limit: int = 50) -> list[dict[str, Any]]:
        """Return the most recent audit rows for a bot.

        Args:
            bot_id: Bot identifier.
            limit: Maximum rows to return.

        Returns:
            List of dicts with audit_log columns.
        """
        pool = self._require_pool()
        rows = await pool.fetch(
            """
            SELECT id, ts, bot_id, kind, client_order_id, exchange_order_id, payload
            FROM audit_log
            WHERE bot_id = $1
            ORDER BY ts DESC, id DESC
            LIMIT $2
            """,
            bot_id,
            limit,
        )
        return [dict(r) for r in rows]

    async def fetch_strategy_rollups(self) -> list[dict[str, Any]]:
        """Return aggregated metrics per strategy from perf_rollup.

        Returns:
            List of dicts aggregated by strategy.
        """
        pool = self._require_pool()
        rows = await pool.fetch("""
            SELECT
                strategy,
                SUM(wins)::bigint          AS wins,
                SUM(losses)::bigint        AS losses,
                SUM(gross_pnl)             AS gross_pnl,
                SUM(realized_pnl)          AS realized_pnl,
                SUM(unrealized_pnl)        AS unrealized_pnl,
                SUM(n_orders)::bigint      AS n_orders,
                COUNT(DISTINCT bot_id)     AS n_bots,
                COUNT(DISTINCT market_id)  AS n_markets,
                MAX(last_fill_at)          AS last_fill_at
            FROM perf_rollup
            GROUP BY strategy
            ORDER BY strategy
        """)
        return [dict(r) for r in rows]

    async def fetch_strategy_detail(self, name: str) -> list[dict[str, Any]]:
        """Return per-bot rows for one strategy from perf_rollup.

        Args:
            name: Strategy name.

        Returns:
            List of per-bot dicts for the given strategy.
        """
        pool = self._require_pool()
        rows = await pool.fetch(
            """
            SELECT
                bot_id,
                market_id,
                wins,
                losses,
                gross_pnl,
                realized_pnl,
                unrealized_pnl,
                n_orders,
                last_fill_at
            FROM perf_rollup
            WHERE strategy = $1
            ORDER BY bot_id
            """,
            name,
        )
        return [dict(r) for r in rows]

    async def fetch_market_exposures(self) -> list[dict[str, Any]]:
        """Return aggregated metrics per market from perf_rollup.

        Returns:
            List of dicts aggregated by market_id.
        """
        pool = self._require_pool()
        rows = await pool.fetch("""
            SELECT
                market_id,
                SUM(gross_pnl)         AS gross_pnl,
                SUM(realized_pnl)      AS realized_pnl,
                SUM(unrealized_pnl)    AS unrealized_pnl,
                COUNT(DISTINCT bot_id) AS n_bots,
                SUM(n_orders)::bigint  AS n_orders,
                MAX(last_fill_at)      AS last_fill_at
            FROM perf_rollup
            GROUP BY market_id
            ORDER BY market_id
        """)
        return [dict(r) for r in rows]

    async def fetch_audit_page(
        self,
        *,
        limit: int = 100,
        before: datetime | None = None,
        bot_id: str | None = None,
        kind: str | None = None,
        since: datetime | None = None,
    ) -> list[dict[str, Any]]:
        """Return a paginated, filtered slice of audit_log.

        Args:
            limit: Max rows (capped at 200 by the endpoint layer).
            before: Only rows with ts < before (keyset pagination).
            bot_id: Filter by bot.
            kind: Filter by event kind.
            since: Only rows with ts >= since.

        Returns:
            List of audit row dicts, newest first.
        """
        pool = self._require_pool()

        clauses: list[str] = []
        params: list[Any] = []
        p = 1

        if before is not None:
            clauses.append(f"ts < ${p}")
            params.append(before)
            p += 1
        if since is not None:
            clauses.append(f"ts >= ${p}")
            params.append(since)
            p += 1
        if bot_id is not None:
            clauses.append(f"bot_id = ${p}")
            params.append(bot_id)
            p += 1
        if kind is not None:
            clauses.append(f"kind = ${p}")
            params.append(kind)
            p += 1

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)

        sql = f"""
            SELECT id, ts, bot_id, kind, client_order_id, exchange_order_id, payload
            FROM audit_log
            {where}
            ORDER BY ts DESC, id DESC
            LIMIT ${p}
        """
        rows = await pool.fetch(sql, *params)
        return [dict(r) for r in rows]

    async def fetch_failures(self, days: int = 7) -> list[dict[str, Any]]:
        """Return failure events from the last N days.

        Args:
            days: Lookback window in days.

        Returns:
            List of failure event dicts, newest first.
        """
        pool = self._require_pool()
        kinds = list(_FAILURE_KINDS)
        rows = await pool.fetch(
            """
            SELECT id, ts, bot_id, kind, client_order_id, exchange_order_id, payload
            FROM audit_log
            WHERE kind = ANY($1::text[])
              AND ts >= NOW() - ($2 || ' days')::interval
            ORDER BY ts DESC, id DESC
            LIMIT 500
            """,
            kinds,
            str(days),
        )
        return [dict(r) for r in rows]

    async def fetch_all_bot_states_with_strategy(self) -> list[dict[str, Any]]:
        """Return bot states including strategy from the state JSONB.

        Used by /api/capital to aggregate balances.

        Returns:
            List of dicts with keys: bot_id, market_id, snapshot_at, state.
        """
        pool = self._require_pool()
        rows = await pool.fetch("""
            SELECT bot_id, market_id, snapshot_at, state
            FROM bot_state
            ORDER BY bot_id
        """)
        return [dict(r) for r in rows]

    async def max_snapshot_at(self) -> datetime | None:
        """Return the most recent snapshot_at across all bot_state rows.

        Returns:
            Max timestamp or None if table is empty.
        """
        pool = self._require_pool()
        raw = await pool.fetchval("SELECT MAX(snapshot_at) FROM bot_state")
        return raw if isinstance(raw, datetime) else None

    async def max_audit_ts(self) -> datetime | None:
        """Return the most recent ts across all audit_log rows.

        Returns:
            Max timestamp or None if table is empty.
        """
        pool = self._require_pool()
        raw = await pool.fetchval("SELECT MAX(ts) FROM audit_log")
        return raw if isinstance(raw, datetime) else None

    async def max_perf_rollup_fill(self) -> datetime | None:
        """Return the most recent last_fill_at across perf_rollup rows.

        Returns:
            Max timestamp or None if table/view is empty.
        """
        pool = self._require_pool()
        try:
            raw = await pool.fetchval("SELECT MAX(last_fill_at) FROM perf_rollup")
            return raw if isinstance(raw, datetime) else None
        except Exception:
            return None
