"""Integration test: snapshot → kill bot → rehydrate produces consistent state.

Uses in-memory fakes — no real Postgres required.  The test runs the NullStrategy
through 3 full ticks by manually invoking on_tick and _persist_snapshot, then
simulates a crash-recovery by constructing a fresh bot and calling rehydrate()
from the stored snapshot.

A separate suite (marked ``pytest.mark.integration``) exercises the real
Postgres path and is skipped when ``PG_DSN`` is not set.
"""

from __future__ import annotations

import pytest
from src.bots.base import Decision
from src.signals.base import SignalSnapshot

from tests.fixtures.clocks import ManualClock
from tests.fixtures.null_strategy import make_null_bot
from tests.fixtures.state import InMemoryStateRepository

# ---------------------------------------------------------------------------
# Fast path — in-memory only
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_snapshot_and_rehydrate_preserves_state() -> None:
    """Run 3 ticks, snapshot, rehydrate into a fresh bot, assert state matches."""
    state = InMemoryStateRepository()
    clock = ManualClock()
    bot = make_null_bot(state=state, clock=clock, bot_id="rehydrate-test-bot")

    # Simulate 3 ticks manually (bypassing the sleep loop)
    for _ in range(3):
        signals = SignalSnapshot(samples={}, received_at=clock.now(), stale=frozenset())
        await bot.on_tick(signals)
        await bot._persist_snapshot()
        clock.advance(1)

    assert bot.tick_count == 3

    # Read back snapshot
    stored = await state.latest_snapshot("rehydrate-test-bot")
    assert stored is not None, "Snapshot must exist after 3 ticks"

    # Build a fresh bot with the same state store and rehydrate
    fresh_bot = make_null_bot(state=state, clock=clock, bot_id="rehydrate-test-bot")
    assert fresh_bot.tick_count == 0, "Fresh bot starts with tick_count=0"

    fresh_bot.rehydrate(stored)

    # The rehydrated bot must have the same intent_seq and tick_count
    assert fresh_bot._intent_seq == bot._intent_seq, "Rehydrated intent_seq must match original"
    assert fresh_bot.tick_count == bot.tick_count, "Rehydrated tick_count must match original"
    assert fresh_bot._position_notional == bot._position_notional, (
        "Rehydrated position must match original"
    )


@pytest.mark.asyncio
async def test_rehydrate_is_idempotent() -> None:
    """Calling rehydrate twice with the same snapshot must leave state unchanged."""
    state = InMemoryStateRepository()
    clock = ManualClock()
    bot = make_null_bot(state=state, clock=clock)

    signals = SignalSnapshot(samples={}, received_at=clock.now(), stale=frozenset())
    await bot.on_tick(signals)
    await bot._persist_snapshot()

    stored = await state.latest_snapshot(bot._config.bot_id)
    assert stored is not None

    bot.rehydrate(stored)
    seq_after_first = bot._intent_seq
    count_after_first = bot.tick_count

    # Second rehydrate with the same snapshot
    bot.rehydrate(stored)
    assert bot._intent_seq == seq_after_first, "Second rehydrate must not change intent_seq"
    assert bot.tick_count == count_after_first, "Second rehydrate must not change tick_count"


@pytest.mark.asyncio
async def test_snapshot_failure_does_not_abort_tick() -> None:
    """If the state repository raises on write, the tick must continue normally.

    Invariant 6: snapshot failures emit a warn; the tick is NOT aborted.
    """

    class BrokenRepo(InMemoryStateRepository):
        async def write_snapshot(self, *args: object, **kwargs: object) -> None:  # type: ignore[override]
            raise RuntimeError("Simulated DB failure")

    broken_state = BrokenRepo()
    bot = make_null_bot(state=broken_state)

    signals = SignalSnapshot(samples={}, received_at=ManualClock().now(), stale=frozenset())

    # on_tick succeeds
    decision = await bot.on_tick(signals)
    assert isinstance(decision, Decision)

    # _persist_snapshot must not raise even though the repo is broken
    await bot._persist_snapshot()  # should warn and return, not raise


# ---------------------------------------------------------------------------
# Real Postgres path — skipped when PG_DSN not set
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_snapshot_rehydrate_postgres() -> None:
    """Full round-trip through a real Postgres instance.

    Requires ``PG_DSN`` environment variable (e.g.
    ``postgresql+asyncpg://user:pass@localhost/test``).
    """
    import os

    pg_dsn = os.environ.get("PG_DSN")
    if not pg_dsn:
        pytest.skip("PG_DSN not set — skipping Postgres integration test")

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from src.state.models import Base
    from src.state.repository import StateRepository

    engine = create_async_engine(pg_dsn, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    repo = StateRepository(session_factory)

    clock = ManualClock()
    bot = make_null_bot(state=repo, clock=clock, bot_id="pg-rehydrate-test")

    for _ in range(3):
        signals = SignalSnapshot(samples={}, received_at=clock.now(), stale=frozenset())
        await bot.on_tick(signals)
        await bot._persist_snapshot()
        clock.advance(1)

    stored = await repo.latest_snapshot("pg-rehydrate-test")
    assert stored is not None

    fresh = make_null_bot(state=repo, clock=clock, bot_id="pg-rehydrate-test")
    fresh.rehydrate(stored)

    assert fresh._intent_seq == bot._intent_seq
    assert fresh.tick_count == bot.tick_count

    # Cleanup
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()
