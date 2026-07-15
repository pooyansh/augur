"""bot_state: add mode and strategy columns

Revision ID: 6c600ed35213
Revises: 9d3e3db75278
Create Date: 2026-07-15 16:30:00.000000

The dashboard's /api/bots and /api/bots/{id} tried to read "mode" and
"strategy" out of the JSONB state blob (bs.state->>'strategy',
_parse_state(state).get("mode", "paper")). Neither key is ever written by
any strategy's snapshot() — the documented required snapshot keys are only
intent_seq/position/last_decision_at/_version (.claude/rules/02-state-handoff.md)
— so the dashboard always showed strategy="" and mode="paper" regardless of
a bot's actual configuration, including bots genuinely running live.

BaseBot._persist_snapshot already has both values on self._config at write
time; this migration adds real columns so they're durable and queryable
without depending on strategy authors to know a dashboard-only key exists.

Nullable: existing rows predate this column and will show NULL until their
next snapshot write (bots snapshot every tick, so this self-heals quickly
for any running bot).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "6c600ed35213"
down_revision: str | Sequence[str] | None = "9d3e3db75278"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add bot_state.mode and bot_state.strategy columns."""
    op.add_column("bot_state", sa.Column("mode", sa.String(length=20), nullable=True))
    op.add_column("bot_state", sa.Column("strategy", sa.Text(), nullable=True))


def downgrade() -> None:
    """Drop bot_state.mode and bot_state.strategy columns."""
    op.drop_column("bot_state", "strategy")
    op.drop_column("bot_state", "mode")
