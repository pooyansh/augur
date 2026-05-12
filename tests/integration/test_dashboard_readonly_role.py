"""Integration test: dashboard_reader role cannot write to bot_state.

Connects as dashboard_reader and attempts INSERT / UPDATE / DELETE on bot_state;
all must raise permission errors.

Skips cleanly if PG_DSN is not set in the environment.

Run with:
    PG_DSN=postgresql://... DASHBOARD_PG_USER=dashboard_reader \
    DASHBOARD_PG_PASSWORD=... uv run pytest tests/integration/test_dashboard_readonly_role.py -v
"""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def reader_dsn() -> str:
    """Build the dashboard_reader DSN from env vars, or skip."""
    user = os.environ.get("DASHBOARD_PG_USER")
    password = os.environ.get("DASHBOARD_PG_PASSWORD")
    if not user or not password:
        pytest.skip(
            "DASHBOARD_PG_USER / DASHBOARD_PG_PASSWORD not set — "
            "skipping dashboard_reader role tests."
        )

    host = os.environ.get("POSTGRES_HOST", "localhost")
    port = os.environ.get("POSTGRES_PORT", "5432")
    db = os.environ.get("POSTGRES_DB", "bidder")
    return f"postgresql://{user}:{password}@{host}:{port}/{db}"


@pytest.fixture(scope="module")
async def reader_conn(reader_dsn: str):  # type: ignore[no-untyped-def]
    """Open an asyncpg connection as dashboard_reader."""
    import asyncpg  # type: ignore[import-untyped]

    conn = await asyncpg.connect(reader_dsn)
    try:
        yield conn
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_reader_cannot_insert_bot_state(reader_conn) -> None:  # type: ignore[no-untyped-def]
    """dashboard_reader must not be able to INSERT into bot_state."""
    import asyncpg  # type: ignore[import-untyped]

    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        await reader_conn.execute(
            """
            INSERT INTO bot_state (bot_id, snapshot_at, version, market_id, state)
            VALUES ('readonly-test-bot', NOW(), 1, 'mkt-000', '{}')
            """
        )


@pytest.mark.asyncio
async def test_reader_cannot_update_bot_state(reader_conn) -> None:  # type: ignore[no-untyped-def]
    """dashboard_reader must not be able to UPDATE bot_state."""
    import asyncpg  # type: ignore[import-untyped]

    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        await reader_conn.execute("UPDATE bot_state SET version = 999 WHERE bot_id = 'nonexistent'")


@pytest.mark.asyncio
async def test_reader_cannot_delete_bot_state(reader_conn) -> None:  # type: ignore[no-untyped-def]
    """dashboard_reader must not be able to DELETE from bot_state."""
    import asyncpg  # type: ignore[import-untyped]

    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        await reader_conn.execute("DELETE FROM bot_state WHERE bot_id = 'nonexistent'")


@pytest.mark.asyncio
async def test_reader_cannot_insert_audit_log(reader_conn) -> None:  # type: ignore[no-untyped-def]
    """dashboard_reader must not be able to INSERT into audit_log."""
    import asyncpg  # type: ignore[import-untyped]

    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        await reader_conn.execute(
            """
            INSERT INTO audit_log (ts, bot_id, kind, payload)
            VALUES (NOW(), 'readonly-test-bot', 'test_event', '{}')
            """
        )


@pytest.mark.asyncio
async def test_reader_can_select_bot_state(reader_conn) -> None:  # type: ignore[no-untyped-def]
    """dashboard_reader MUST be able to SELECT from bot_state."""
    rows = await reader_conn.fetch("SELECT bot_id FROM bot_state LIMIT 1")
    # No exception = SELECT permission is granted.  Row count doesn't matter.
    assert isinstance(rows, list)


@pytest.mark.asyncio
async def test_reader_can_select_audit_log(reader_conn) -> None:  # type: ignore[no-untyped-def]
    """dashboard_reader MUST be able to SELECT from audit_log."""
    rows = await reader_conn.fetch("SELECT id FROM audit_log LIMIT 1")
    assert isinstance(rows, list)


@pytest.mark.asyncio
async def test_reader_can_select_perf_rollup(reader_conn) -> None:  # type: ignore[no-untyped-def]
    """dashboard_reader MUST be able to SELECT from perf_rollup."""
    rows = await reader_conn.fetch("SELECT strategy FROM perf_rollup LIMIT 1")
    assert isinstance(rows, list)
