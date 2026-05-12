"""phase6b: create perf_rollup materialized view and dashboard_reader role

Revision ID: e5f6a7b8c9d0
Revises: c3d4e5f6a7b8
Create Date: 2026-05-09 02:00:00.000000

Changes:
1. Creates ``perf_rollup`` materialized view keyed on ``(strategy, market_id, bot_id)``.
   Sourced from ``audit_log`` rows with kind IN ('order_filled', 'order_rejected',
   'settled'), joined to ``bot_state`` to derive strategy and market_id from the
   latest snapshot per bot.

   NOTE on wins/losses: ``audit_log.payload->>'result'`` semantics are not yet
   defined in the schema (Phase 3 stub).  For now, wins=0 and losses=0 are
   hardcoded; n_orders and last_fill_at are computed correctly from the audit_log.
   TODO(phase-7): wire actual win/loss logic once payload shape is confirmed.

2. Creates a UNIQUE INDEX on (strategy, market_id, bot_id) so that
   ``REFRESH MATERIALIZED VIEW CONCURRENTLY`` works.

3. Creates secondary indexes on (strategy) and (market_id) for fast filtering.

4. Creates the ``dashboard_reader`` PG role (idempotent) and GRANTs SELECT on:
   bot_state, audit_log, kill_switch, signal_samples, market_history, perf_rollup.
   If DASHBOARD_PG_PASSWORD is set at migration time, the password is set.
   Otherwise the role is created with NOLOGIN; ops must set the password out of band.

Down migration drops the view, revokes grants, and drops the role.
"""

from __future__ import annotations

import os
from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e5f6a7b8c9d0"
down_revision: str | Sequence[str] | None = "c3d4e5f6a7b8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Tables / views the dashboard_reader role gets SELECT on.
_READABLE_OBJECTS = [
    "bot_state",
    "audit_log",
    "kill_switch",
    "signal_samples",
    "market_history",
    "perf_rollup",
]


def upgrade() -> None:
    """Create perf_rollup materialized view and dashboard_reader role."""

    # ------------------------------------------------------------------
    # 1. Materialized view: perf_rollup
    # ------------------------------------------------------------------
    # NOTE: wins and losses default to 0 until the payload result field is
    # formally defined (see module docstring TODO).
    # n_orders: count of ALL matching audit_log rows per (bot, market, strategy).
    # last_fill_at: max(ts) from audit_log for kind = 'order_filled'.
    # gross_pnl, realized_pnl, unrealized_pnl: sourced from the latest bot_state
    # snapshot JSONB, falling back to 0 if not present.
    op.execute("""
        CREATE MATERIALIZED VIEW IF NOT EXISTS perf_rollup AS
        WITH latest_snapshot AS (
            -- Latest snapshot per bot (DISTINCT ON relies on the existing index).
            SELECT DISTINCT ON (bot_id)
                   bot_id,
                   state->>'strategy'                    AS strategy,
                   market_id,
                   COALESCE((state->>'gross_pnl')::numeric,    0) AS gross_pnl,
                   COALESCE((state->>'realized_pnl')::numeric, 0) AS realized_pnl,
                   COALESCE((state->>'unrealized_pnl')::numeric, 0) AS unrealized_pnl
            FROM bot_state
            ORDER BY bot_id, snapshot_at DESC
        ),
        order_stats AS (
            -- Aggregate order counts and last fill timestamp per bot.
            SELECT
                al.bot_id,
                COUNT(*)                                       AS n_orders,
                MAX(CASE WHEN al.kind = 'order_filled'
                         THEN al.ts END)                       AS last_fill_at
            FROM audit_log al
            WHERE al.kind IN ('order_filled', 'order_rejected', 'settled')
            GROUP BY al.bot_id
        )
        SELECT
            ls.strategy,
            ls.market_id,
            ls.bot_id,
            -- TODO(phase-7): derive from payload->>'result' once shape confirmed.
            0::bigint                  AS wins,
            0::bigint                  AS losses,
            ls.gross_pnl,
            ls.realized_pnl,
            ls.unrealized_pnl,
            COALESCE(os.n_orders, 0)   AS n_orders,
            os.last_fill_at
        FROM latest_snapshot ls
        LEFT JOIN order_stats os USING (bot_id)
        WHERE ls.strategy IS NOT NULL
    """)

    # ------------------------------------------------------------------
    # 2. Unique index (required for REFRESH CONCURRENTLY)
    # ------------------------------------------------------------------
    op.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS
            uq_perf_rollup_strategy_market_bot
        ON perf_rollup (strategy, market_id, bot_id)
    """)

    # Secondary indexes for fast filtering.
    op.execute("""
        CREATE INDEX IF NOT EXISTS ix_perf_rollup_strategy
        ON perf_rollup (strategy)
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS ix_perf_rollup_market_id
        ON perf_rollup (market_id)
    """)

    # ------------------------------------------------------------------
    # 3. dashboard_reader role (idempotent)
    # ------------------------------------------------------------------
    pg_password = os.environ.get("DASHBOARD_PG_PASSWORD")

    if pg_password:
        # Create role with login and set password.
        op.execute(f"""
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_roles WHERE rolname = 'dashboard_reader'
                ) THEN
                    CREATE ROLE dashboard_reader
                        LOGIN
                        PASSWORD '{pg_password}'
                        NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT;
                END IF;
            END
            $$
        """)
    else:
        # Create role with NOLOGIN; ops sets password out of band.
        op.execute("""
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_roles WHERE rolname = 'dashboard_reader'
                ) THEN
                    CREATE ROLE dashboard_reader
                        NOLOGIN
                        NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT;
                    RAISE NOTICE
                        'dashboard_reader created with NOLOGIN. '
                        'Set password out of band: ALTER ROLE dashboard_reader PASSWORD ''...''';
                END IF;
            END
            $$
        """)

    # ------------------------------------------------------------------
    # 4. GRANT SELECT on readable objects
    # ------------------------------------------------------------------
    for obj in _READABLE_OBJECTS:
        op.execute(f"GRANT SELECT ON {obj} TO dashboard_reader")


def downgrade() -> None:
    """Revoke grants, drop perf_rollup, and drop dashboard_reader role."""

    # Revoke all grants first.
    for obj in reversed(_READABLE_OBJECTS):
        # REVOKE is a no-op if the grant or role doesn't exist — safe.
        op.execute(f"REVOKE ALL PRIVILEGES ON {obj} FROM dashboard_reader")

    # Drop indexes before the view.
    op.execute("DROP INDEX IF EXISTS ix_perf_rollup_market_id")
    op.execute("DROP INDEX IF EXISTS ix_perf_rollup_strategy")
    op.execute("DROP INDEX IF EXISTS uq_perf_rollup_strategy_market_bot")

    # Drop the materialized view.
    op.execute("DROP MATERIALIZED VIEW IF EXISTS perf_rollup")

    # Drop the role (IF EXISTS guards against already-dropped).
    op.execute("""
        DROP ROLE IF EXISTS dashboard_reader
    """)
