"""phase5: create market_history view

Revision ID: c3d4e5f6a7b8
Revises: b1c2d3e4f5a6
Create Date: 2026-05-09 01:00:00.000000

Creates the ``market_history`` SQL view that exposes the latest snapshot per
``(bot_id, market_id)`` pair.  The underlying indexes on
``(market_id, snapshot_at DESC)`` and ``(bot_id, market_id, snapshot_at DESC)``
were created in Phase 3, so this view is cheap from day one.

The view is used by the strategy handoff mechanism: when a new strategy Y
takes over from X on the same market, Y reads X's final snapshot via
``market_history`` to prime its state.

View definition uses ``DISTINCT ON`` (PostgreSQL extension) for simplicity
and efficiency.  The ``ORDER BY`` clause makes the ``DISTINCT ON`` behaviour
deterministic: the most recent snapshot per ``(bot_id, market_id)`` is kept.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c3d4e5f6a7b8"
down_revision: str | Sequence[str] | None = "b1c2d3e4f5a6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the market_history view."""
    op.execute("""
        CREATE OR REPLACE VIEW market_history AS
        SELECT DISTINCT ON (bot_id, market_id)
               bot_id,
               market_id,
               snapshot_at,
               version,
               state
        FROM bot_state
        ORDER BY bot_id, market_id, snapshot_at DESC
    """)


def downgrade() -> None:
    """Drop the market_history view."""
    op.execute("DROP VIEW IF EXISTS market_history")
