"""phase2: stub bot_state, audit_log, kill_switch

Revision ID: 741531787a10
Revises:
Create Date: 2026-05-09 00:00:00.000000

NOTE: Schemas are intentionally minimal stubs. Phase 3 will expand columns,
add indices, constraints, and the market_history view.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "741531787a10"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create stub tables: bot_state, audit_log, kill_switch."""
    # ------------------------------------------------------------------
    # bot_state — JSONB snapshots written after every successful tick.
    # ------------------------------------------------------------------
    op.create_table(
        "bot_state",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("bot_id", sa.String(length=255), nullable=False),
        sa.Column(
            "snapshot_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("state", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_bot_state_bot_id", "bot_state", ["bot_id"])

    # ------------------------------------------------------------------
    # audit_log — append-only order/event ledger; no deletes, no updates.
    # ------------------------------------------------------------------
    op.create_table(
        "audit_log",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("bot_id", sa.String(length=255), nullable=False),
        sa.Column("client_order_id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("original_id", sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("client_order_id"),
    )
    op.create_index("ix_audit_log_bot_id", "audit_log", ["bot_id"])

    # ------------------------------------------------------------------
    # kill_switch — global freeze flag; exactly one row (id=1).
    # ------------------------------------------------------------------
    op.create_table(
        "kill_switch",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("tripped_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("tripped_by", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    # Seed the single control row with kill_switch OFF.
    op.execute("INSERT INTO kill_switch (id, active) VALUES (1, false)")


def downgrade() -> None:
    """Drop stub tables."""
    op.drop_table("kill_switch")
    op.drop_index("ix_audit_log_bot_id", table_name="audit_log")
    op.drop_table("audit_log")
    op.drop_index("ix_bot_state_bot_id", table_name="bot_state")
    op.drop_table("bot_state")
