"""SQLAlchemy ORM models for durable bot state.

Phase 3 expands the Phase 2 stubs with full column sets, constraints, and
the indexes needed for the deferred ``market_history`` view (Phase 5).
"""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.sql import func


class Base(DeclarativeBase):
    """Shared declarative base for all ORM models."""


class BotState(Base):
    """Periodic JSONB snapshots written by each bot after every successful tick.

    The manager reads the latest row when rehydrating a crashed bot.

    JSONB ``state`` shape contract (every strategy must include):
        - ``intent_seq`` (int): monotonic counter for client_order_id generation
        - ``position`` (Decimal-serialised str): current net position
        - ``last_decision_at`` (ISO-8601 str): UTC timestamp of last on_tick

    Additional strategy-specific keys are allowed alongside these required ones.

    Indexes:
        - ``(market_id, snapshot_at DESC)`` — supports the deferred
          ``market_history`` view (SQL view ships in Phase 5; index added now
          so the view is cheap to create later).
        - ``(bot_id, snapshot_at DESC)`` — fast latest-snapshot lookup for
          rehydration.
    """

    __tablename__ = "bot_state"

    bot_id = Column(String(255), primary_key=True, nullable=False)
    snapshot_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    version = Column(Integer, nullable=False)
    market_id = Column(Text, nullable=False)
    state = Column(JSONB, nullable=False)

    __table_args__ = (
        # Phase 5 market_history view will use this index — deferred, see Phase 5.
        Index("ix_bot_state_market_id_snapshot_at", "market_id", "snapshot_at"),
        Index("ix_bot_state_bot_id_snapshot_at", "bot_id", "snapshot_at"),
    )


class AuditLog(Base):
    """Append-only record of every order intent and result.

    INVARIANT: no DELETE or UPDATE ever touches this table.
    Any UPDATE or DELETE against this table is a bug — use a new correction
    row that references the original via ``client_order_id`` or a separate
    ``original_id`` column if needed in the future.

    Columns:
        id: BIGSERIAL primary key.
        ts: Row-insertion timestamp (server default now()).
        bot_id: Bot that produced this record.
        kind: Event kind string (e.g. ``"order_submitted"``, ``"order_accepted"``).
        client_order_id: Deterministic id from BaseBot.  Optional because some
            audit rows are not order-related (e.g. snapshot failures).
        exchange_order_id: Exchange-assigned id when available.
        payload: Full structured payload as JSONB.
    """

    __tablename__ = "audit_log"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    ts = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    bot_id = Column(Text, nullable=False)
    kind = Column(Text, nullable=False)
    client_order_id = Column(Text, nullable=True)
    exchange_order_id = Column(Text, nullable=True)
    payload = Column(JSONB, nullable=False)

    __table_args__ = (Index("ix_audit_log_bot_id_ts", "bot_id", "ts"),)


class KillSwitch(Base):
    """Global kill-switch state.  Exactly one row expected (id = 1).

    ``BaseBot.place`` checks this before every order.  Tripping it causes all
    bots to cancel open orders and freeze new placement.

    Constraint: ``id`` must equal 1 — enforced at the DB level.
    """

    __tablename__ = "kill_switch"

    id = Column(
        Integer,
        CheckConstraint("id = 1", name="ck_kill_switch_singleton"),
        primary_key=True,
    )
    tripped = Column(Boolean, nullable=False, default=False)
    reason = Column(Text, nullable=True)
    tripped_at = Column(DateTime(timezone=True), nullable=True)
