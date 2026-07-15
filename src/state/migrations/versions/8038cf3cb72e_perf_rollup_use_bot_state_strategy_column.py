"""perf_rollup: derive strategy from bot_state.strategy, not JSONB

Revision ID: 8038cf3cb72e
Revises: 6c600ed35213
Create Date: 2026-07-15 16:35:00.000000

perf_rollup's latest_snapshot CTE derived "strategy" via
state->>'strategy', a JSONB key no strategy's snapshot() ever writes (see
6c600ed35213). Combined with the view's own "WHERE ls.strategy IS NOT
NULL" filter, this meant perf_rollup silently excluded every bot from
every refresh since the view was created — /api/strategies and
/api/markets have been returning empty data regardless of actual trading
activity, with no error to signal it.

Materialized views can't have their defining query ALTERed in place, so
this drops and recreates perf_rollup (plus its indexes and dashboard_reader
grant, which don't survive a DROP) sourced from the new bot_state.strategy
column instead.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "8038cf3cb72e"
down_revision: str | Sequence[str] | None = "6c600ed35213"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD_VIEW_SQL = """
    CREATE MATERIALIZED VIEW IF NOT EXISTS perf_rollup AS
    WITH latest_snapshot AS (
        SELECT DISTINCT ON (bot_id)
               bot_id,
               {strategy_expr}                        AS strategy,
               market_id,
               COALESCE((state->>'gross_pnl')::numeric,    0) AS gross_pnl,
               COALESCE((state->>'realized_pnl')::numeric, 0) AS realized_pnl,
               COALESCE((state->>'unrealized_pnl')::numeric, 0) AS unrealized_pnl
        FROM bot_state
        ORDER BY bot_id, snapshot_at DESC
    ),
    order_stats AS (
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
"""


def _drop_view_objects() -> None:
    op.execute("DROP INDEX IF EXISTS ix_perf_rollup_market_id")
    op.execute("DROP INDEX IF EXISTS ix_perf_rollup_strategy")
    op.execute("DROP INDEX IF EXISTS uq_perf_rollup_strategy_market_bot")
    op.execute("DROP MATERIALIZED VIEW IF EXISTS perf_rollup")


def _create_view_objects() -> None:
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS
            uq_perf_rollup_strategy_market_bot
        ON perf_rollup (strategy, market_id, bot_id)
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_perf_rollup_strategy ON perf_rollup (strategy)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_perf_rollup_market_id ON perf_rollup (market_id)")
    # GRANTs on a dropped-and-recreated object don't survive the drop.
    op.execute("GRANT SELECT ON perf_rollup TO dashboard_reader")


def upgrade() -> None:
    """Rebuild perf_rollup sourcing strategy from the real bot_state column."""
    _drop_view_objects()
    op.execute(_OLD_VIEW_SQL.format(strategy_expr="strategy"))
    _create_view_objects()


def downgrade() -> None:
    """Revert to deriving strategy from the JSONB state blob."""
    _drop_view_objects()
    op.execute(_OLD_VIEW_SQL.format(strategy_expr="state->>'strategy'"))
    _create_view_objects()
