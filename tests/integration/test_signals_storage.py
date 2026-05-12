"""Integration tests for SignalStorage against a real Postgres instance.

Skipped cleanly when PG_DSN is absent.
"""

from __future__ import annotations

import os
import time
from datetime import UTC, datetime, timedelta

import pytest

pytestmark = pytest.mark.integration

PG_DSN = os.environ.get("PG_DSN", "")

if not PG_DSN:
    pytest.skip("PG_DSN not set — skipping Postgres integration tests", allow_module_level=True)


@pytest.fixture()
async def session_factory() -> object:
    """Build a session factory and run the Phase 3a migration."""
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

    dsn = PG_DSN.replace("postgresql://", "postgresql+asyncpg://")
    engine = create_async_engine(dsn, echo=False)

    # Create the signal_samples table directly (no full Alembic run in tests).
    from sqlalchemy import text

    async with engine.begin() as conn:
        await conn.execute(
            text("""
                CREATE TABLE IF NOT EXISTS signal_samples (
                    id BIGSERIAL PRIMARY KEY,
                    signal TEXT NOT NULL,
                    params_hash TEXT NOT NULL,
                    source TEXT NOT NULL,
                    observed_at TIMESTAMPTZ NOT NULL,
                    payload JSONB NOT NULL,
                    latency_ms INTEGER NOT NULL
                )
            """)
        )
        await conn.execute(
            text("""
                CREATE INDEX IF NOT EXISTS ix_signal_samples_signal_params_observed
                ON signal_samples (signal, params_hash, observed_at DESC)
            """)
        )

    sf: async_sessionmaker[AsyncSession] = async_sessionmaker(engine, expire_on_commit=False)
    yield sf

    # Cleanup.
    async with engine.begin() as conn:
        await conn.execute(text("DROP TABLE IF EXISTS signal_samples"))
    await engine.dispose()


@pytest.mark.asyncio
async def test_append_and_replay(session_factory: object) -> None:
    """Written samples can be read back via replay in chronological order."""
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
    from src.signals.storage import SignalSample, SignalStorage

    sf: async_sessionmaker[AsyncSession] = session_factory  # type: ignore[assignment]
    storage = SignalStorage(sf)

    t0 = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    params_hash = "aabbccdd00112233"

    # Insert 5 samples.
    for i in range(5):
        await storage.append(
            signal="btc_15min",
            params_hash=params_hash,
            source="test",
            observed_at=t0 + timedelta(minutes=15 * i),
            payload={"price_usd": str(100 + i)},
            latency_ms=50,
        )

    # Replay full range.
    rows: list[SignalSample] = []
    async for row in storage.replay(
        signal="btc_15min",
        params_hash=params_hash,
        start=t0,
        end=t0 + timedelta(hours=2),
    ):
        rows.append(row)

    assert len(rows) == 5
    # Verify ascending order.
    import itertools

    for a, b in itertools.pairwise(rows):
        assert a.observed_at < b.observed_at


@pytest.mark.asyncio
async def test_replay_range_filter(session_factory: object) -> None:
    """Replay respects start/end boundaries."""
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
    from src.signals.storage import SignalStorage

    sf: async_sessionmaker[AsyncSession] = session_factory  # type: ignore[assignment]
    storage = SignalStorage(sf)

    t0 = datetime(2026, 2, 1, 0, 0, 0, tzinfo=UTC)
    params_hash = "deadbeef11223344"

    for i in range(10):
        await storage.append(
            signal="btc_15min",
            params_hash=params_hash,
            source="test",
            observed_at=t0 + timedelta(hours=i),
            payload={"price_usd": str(200 + i)},
            latency_ms=30,
        )

    # Only retrieve hours 2-5 (inclusive).
    window_start = t0 + timedelta(hours=2)
    window_end = t0 + timedelta(hours=5)
    rows = []
    async for row in storage.replay(
        signal="btc_15min",
        params_hash=params_hash,
        start=window_start,
        end=window_end,
    ):
        rows.append(row)

    assert len(rows) == 4  # hours 2, 3, 4, 5
    assert all(window_start <= r.observed_at <= window_end for r in rows)


@pytest.mark.asyncio
async def test_replay_performance_30_days(session_factory: object) -> None:
    """Replay 30 days of btc_15min samples (~2880 rows) in < 10 seconds.

    Phase 3a performance gate (plan/03a-signals.md exit criterion H).
    """
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
    from src.signals.storage import SignalStorage

    sf: async_sessionmaker[AsyncSession] = session_factory  # type: ignore[assignment]
    storage = SignalStorage(sf)

    t0 = datetime(2026, 3, 1, 0, 0, 0, tzinfo=UTC)
    params_hash = "perf0000aaaa1111"
    total_samples = 30 * 24 * 4  # 2880 samples (one per 15 min for 30 days)

    # Insert in batches for speed.
    from sqlalchemy import text

    async with sf() as session:
        rows_data = [
            {
                "signal": "btc_15min",
                "params_hash": params_hash,
                "source": "test",
                "observed_at": t0 + timedelta(minutes=15 * i),
                "payload": f'{{"price_usd": "{100 + i % 1000}"}}',
                "latency_ms": 50,
            }
            for i in range(total_samples)
        ]
        await session.execute(
            text("""
                INSERT INTO signal_samples
                    (signal, params_hash, source, observed_at, payload, latency_ms)
                SELECT
                    unnest(ARRAY[:signal_vals]::text[]),
                    unnest(ARRAY[:phash_vals]::text[]),
                    unnest(ARRAY[:source_vals]::text[]),
                    unnest(ARRAY[:ts_vals]::timestamptz[]),
                    unnest(ARRAY[:payload_vals]::jsonb[]),
                    unnest(ARRAY[:latency_vals]::int[])
            """),
            {
                "signal_vals": [r["signal"] for r in rows_data],
                "phash_vals": [r["params_hash"] for r in rows_data],
                "source_vals": [r["source"] for r in rows_data],
                "ts_vals": [r["observed_at"] for r in rows_data],
                "payload_vals": [r["payload"] for r in rows_data],
                "latency_vals": [r["latency_ms"] for r in rows_data],
            },
        )
        await session.commit()

    end = t0 + timedelta(days=30)

    t_start = time.monotonic()
    count = 0
    async for _row in storage.replay(
        signal="btc_15min",
        params_hash=params_hash,
        start=t0,
        end=end,
    ):
        count += 1
    elapsed = time.monotonic() - t_start

    assert count == total_samples, f"Expected {total_samples} rows, got {count}"
    assert elapsed < 10.0, f"Replay took {elapsed:.2f}s — exceeds 10s gate"
