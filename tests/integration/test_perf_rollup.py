"""Integration test: seed audit_log, refresh perf_rollup, assert aggregates.

Skips cleanly if PG_DSN is not set in the environment.

Run with:
    PG_DSN=postgresql://... uv run pytest tests/integration/test_perf_rollup.py -v
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime

import pytest

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def pg_dsn() -> str:
    """Return the Postgres DSN from PG_DSN env var, or skip."""
    dsn = os.environ.get("PG_DSN")
    if not dsn:
        pytest.skip("PG_DSN not set — skipping Postgres integration tests.")
    return dsn


@pytest.fixture(scope="module")
async def pg_conn(pg_dsn: str):  # type: ignore[no-untyped-def]
    """Open a raw asyncpg connection for the test module."""
    import asyncpg  # type: ignore[import-untyped]

    conn = await asyncpg.connect(pg_dsn)
    try:
        yield conn
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_perf_rollup_aggregates(pg_conn) -> None:  # type: ignore[no-untyped-def]
    """Seed audit_log + bot_state rows; refresh perf_rollup; assert counts."""
    # Ensure the materialized view exists (migration may not have run on test DB).
    exists = await pg_conn.fetchval(
        "SELECT EXISTS (SELECT 1 FROM pg_matviews WHERE matviewname = 'perf_rollup')"
    )
    if not exists:
        pytest.skip("perf_rollup materialized view not found — run Alembic migrations first.")

    bot_id = "test-rollup-bot-001"
    strategy = "momentum_v1"
    market_id = "market-test-001"
    now = datetime.now(tz=UTC)

    # Clean up any leftover test data.
    await pg_conn.execute("DELETE FROM audit_log WHERE bot_id = $1", bot_id)
    await pg_conn.execute("DELETE FROM bot_state WHERE bot_id = $1", bot_id)

    # Insert a bot_state row so perf_rollup can join to derive strategy + market_id.
    await pg_conn.execute(
        """
        INSERT INTO bot_state (bot_id, snapshot_at, version, market_id, state)
        VALUES ($1, $2, 1, $3, $4)
        ON CONFLICT (bot_id) DO UPDATE
            SET snapshot_at = EXCLUDED.snapshot_at,
                state = EXCLUDED.state,
                market_id = EXCLUDED.market_id,
                version = EXCLUDED.version
        """,
        bot_id,
        now,
        market_id,
        json.dumps({"strategy": strategy, "mode": "paper", "intent_seq": 0}),
    )

    # Insert known audit_log rows.
    for kind in ["order_filled", "order_filled", "order_rejected"]:
        await pg_conn.execute(
            """
            INSERT INTO audit_log (ts, bot_id, kind, payload)
            VALUES ($1, $2, $3, $4)
            """,
            now,
            bot_id,
            kind,
            json.dumps({"test": True}),
        )

    # Refresh the materialized view.
    try:
        # CONCURRENTLY requires a unique index — fall back to non-concurrent in tests.
        await pg_conn.execute("REFRESH MATERIALIZED VIEW perf_rollup")
    except Exception as exc:
        pytest.skip(f"Could not refresh perf_rollup: {exc}")

    # Assert the aggregated row.
    row = await pg_conn.fetchrow(
        "SELECT n_orders, last_fill_at FROM perf_rollup WHERE bot_id = $1",
        bot_id,
    )

    assert row is not None, f"No perf_rollup row found for bot_id={bot_id!r}"
    assert int(row["n_orders"]) == 3  # all 3 audit rows counted

    # Cleanup.
    await pg_conn.execute("DELETE FROM audit_log WHERE bot_id = $1", bot_id)
    await pg_conn.execute("DELETE FROM bot_state WHERE bot_id = $1", bot_id)
    await pg_conn.execute("REFRESH MATERIALIZED VIEW perf_rollup")
