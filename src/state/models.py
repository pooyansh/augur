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

    ``mode`` and ``strategy`` are plain columns, not part of the JSONB
    contract above — they're written by ``BaseBot._persist_snapshot`` from
    ``BotConfig`` (the bot's actual configured mode/strategy_name), not by
    strategy authors. They exist purely so the dashboard can report a bot's
    real mode/strategy without depending on a key no strategy is required
    (or expected) to include in its own ``snapshot()``.

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
    mode = Column(String(20), nullable=True)
    strategy = Column(Text, nullable=True)

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


class SignalSample(Base):
    """Append-only record of every signal observation fetched by the runner.

    INVARIANT: no DELETE or UPDATE ever touches this table.
    Used by the backtest replay harness to stream historical samples through
    strategies without touching live sources.

    Columns:
        id: BIGSERIAL primary key.
        signal: Signal name (e.g. ``"btc_15min"``).
        params_hash: blake2s(8) hex of the sorted params dict — deduplication
            key shared by all bots subscribing to the same (name, params).
        source: ``SignalSource.name`` of the source that produced this row.
        observed_at: UTC timestamp when the sample was fetched from the source.
        payload: Parsed canonical value as JSONB (output of ``Signal.parse``).
        latency_ms: Round-trip fetch latency in milliseconds.

    Indexes:
        - ``(signal, params_hash, observed_at DESC)`` — efficient range scan for
          replay queries and staleness checks.
    """

    __tablename__ = "signal_samples"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    signal = Column(Text, nullable=False)
    params_hash = Column(Text, nullable=False)
    source = Column(Text, nullable=False)
    observed_at = Column(DateTime(timezone=True), nullable=False)
    payload = Column(JSONB, nullable=False)
    latency_ms = Column(Integer, nullable=False)

    __table_args__ = (
        # Primary replay / staleness scan index.
        Index(
            "ix_signal_samples_signal_params_observed",
            "signal",
            "params_hash",
            "observed_at",
        ),
    )


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
