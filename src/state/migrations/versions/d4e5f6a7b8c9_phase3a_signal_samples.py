"""phase3a: create signal_samples table

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-05-09 02:00:00.000000

Creates the ``signal_samples`` table for the Phase 3a signals platform.

Design invariants (see .claude/rules/08-signals.md):
- This table is APPEND-ONLY.  No UPDATE or DELETE should ever be issued.
  Any migration that adds UPDATE/DELETE statements against this table is a bug.
- The composite index ``(signal, params_hash, observed_at DESC)`` supports
  both the replay range-scan and staleness lookups efficiently.
- ``params_hash`` is blake2s(8) of the sorted JSON params — stable and short.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "d4e5f6a7b8c9"
down_revision: str | Sequence[str] | None = "c3d4e5f6a7b8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the signal_samples append-only table."""
    op.create_table(
        "signal_samples",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("signal", sa.Text(), nullable=False),
        sa.Column("params_hash", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("latency_ms", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        # INVARIANT: any UPDATE or DELETE against signal_samples is a bug.
        # All writes must go through SignalStorage.append — never raw SQL updates.
    )

    # Composite index for replay range scans and staleness lookups.
    # Covering (signal, params_hash) with observed_at DESC sorts newest-first
    # which aligns with staleness checks that want the most recent sample.
    op.create_index(
        "ix_signal_samples_signal_params_observed",
        "signal_samples",
        ["signal", "params_hash", sa.text("observed_at DESC")],
    )


def downgrade() -> None:
    """Drop the signal_samples table and its index."""
    op.drop_index("ix_signal_samples_signal_params_observed", table_name="signal_samples")
    op.drop_table("signal_samples")
