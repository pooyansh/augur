"""phase3: expand bot_state, audit_log, kill_switch to full schemas

Revision ID: b1c2d3e4f5a6
Revises: 741531787a10
Create Date: 2026-05-09 00:01:00.000000

Migrates from the Phase 2 minimal stubs to the full Phase 3 schema:

bot_state:
  - Drops the synthetic auto-increment ``id`` PK, promotes ``bot_id`` to PK
    (one snapshot row per bot, upserted after each tick).
  - Adds ``market_id TEXT NOT NULL`` column.
  - Creates composite indexes ``(market_id, snapshot_at DESC)`` and
    ``(bot_id, snapshot_at DESC)`` to support the deferred ``market_history``
    SQL view (Phase 5).

audit_log:
  - Changes ``id`` from INTEGER to BIGSERIAL.
  - Renames ``created_at`` → ``ts`` for brevity.
  - Renames ``event_type`` → ``kind``.
  - Drops the old ``UNIQUE(client_order_id)`` constraint — one order can
    produce multiple audit rows (submitted + accepted/rejected).
  - Drops ``original_id`` (no longer needed; corrections are identified by
    the shared ``client_order_id``).
  - Makes ``client_order_id`` and ``exchange_order_id`` plain TEXT (not UUID)
    to be agnostic to id formats across venues.
  - Recreates the ``(bot_id, ts DESC)`` index.

kill_switch:
  - Renames ``active`` → ``tripped``.
  - Renames ``tripped_by`` → ``reason``.
  - Adds ``CHECK (id = 1)`` singleton constraint.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "b1c2d3e4f5a6"
down_revision: str | Sequence[str] | None = "741531787a10"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Apply Phase 3 schema changes on top of Phase 2 stubs."""

    # ------------------------------------------------------------------
    # bot_state — rebuild from scratch (stub had surrogate PK we drop)
    # ------------------------------------------------------------------
    op.drop_index("ix_bot_state_bot_id", table_name="bot_state")
    op.drop_table("bot_state")

    op.create_table(
        "bot_state",
        sa.Column("bot_id", sa.String(length=255), nullable=False),
        sa.Column(
            "snapshot_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("market_id", sa.Text(), nullable=False),
        sa.Column("state", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.PrimaryKeyConstraint("bot_id"),
    )
    # Supports the deferred market_history view — Phase 5 will create the view;
    # the index is created now so that creation is instant when the time comes.
    op.create_index(
        "ix_bot_state_market_id_snapshot_at",
        "bot_state",
        ["market_id", sa.text("snapshot_at DESC")],
    )
    op.create_index(
        "ix_bot_state_bot_id_snapshot_at",
        "bot_state",
        ["bot_id", sa.text("snapshot_at DESC")],
    )

    # ------------------------------------------------------------------
    # audit_log — rebuild (column renames + type changes)
    # ------------------------------------------------------------------
    op.drop_index("ix_audit_log_bot_id", table_name="audit_log")
    op.drop_table("audit_log")

    op.create_table(
        "audit_log",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column(
            "ts",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("bot_id", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("client_order_id", sa.Text(), nullable=True),
        sa.Column("exchange_order_id", sa.Text(), nullable=True),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        # INVARIANT: any UPDATE or DELETE against audit_log is a bug.
        # Corrections must be new rows that reference the original client_order_id.
    )
    op.create_index("ix_audit_log_bot_id_ts", "audit_log", ["bot_id", sa.text("ts DESC")])

    # ------------------------------------------------------------------
    # kill_switch — rename columns, add singleton CHECK constraint
    # ------------------------------------------------------------------
    op.drop_table("kill_switch")

    op.create_table(
        "kill_switch",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("tripped", sa.Boolean(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("tripped_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("id = 1", name="ck_kill_switch_singleton"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.execute("INSERT INTO kill_switch (id, tripped) VALUES (1, false)")


def downgrade() -> None:
    """Revert Phase 3 changes back to Phase 2 stubs."""
    # kill_switch
    op.drop_table("kill_switch")
    op.create_table(
        "kill_switch",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("tripped_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("tripped_by", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.execute("INSERT INTO kill_switch (id, active) VALUES (1, false)")

    # audit_log
    op.drop_index("ix_audit_log_bot_id_ts", table_name="audit_log")
    op.drop_table("audit_log")
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

    # bot_state
    op.drop_index("ix_bot_state_bot_id_snapshot_at", table_name="bot_state")
    op.drop_index("ix_bot_state_market_id_snapshot_at", table_name="bot_state")
    op.drop_table("bot_state")
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
