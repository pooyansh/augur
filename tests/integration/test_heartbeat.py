"""Integration tests for HeartbeatServer and HeartbeatClient.

Uses short-path tmp sockets (tempfile.mkdtemp) to avoid AF_UNIX path length
limits on macOS (~104 chars).  No external services required.

Tests:
1. Client sends 3 beats; server reports age_s < interval after each beat.
2. Client stops; after 2*interval seconds, server reports age_s > 2*interval.
3. Before any beat arrives, age_s is measured from server start.
"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
from pathlib import Path
from typing import Any

import pytest
from src.manager.heartbeat import HeartbeatClient, HeartbeatServer

from tests.fixtures.clocks import ManualClock

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def short_sock_dir() -> Any:
    """Short-path temp directory for UNIX sockets."""
    d = tempfile.mkdtemp(prefix="hb_")
    yield Path(d)
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def clock() -> ManualClock:
    return ManualClock()


@pytest.fixture
async def server(short_sock_dir: Path, clock: ManualClock) -> Any:
    """Start a HeartbeatServer for 'test-bot' and close it after the test."""
    srv = HeartbeatServer(sock_dir=short_sock_dir, clock=clock)
    await srv.start(["test-bot"])
    yield srv
    await srv.close()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_three_beats_reports_young_age(
    server: HeartbeatServer,
    short_sock_dir: Path,
    clock: ManualClock,
) -> None:
    """Client sends 3 beats; server reports age_s close to 0 after each."""
    sock_path = short_sock_dir / "test-bot.sock"
    client = HeartbeatClient(sock_path=sock_path, bot_id="test-bot", clock=clock)

    for i in range(3):
        await client.beat()
        await asyncio.sleep(0.05)  # let the server process the line
        health = server.health("test-bot")
        assert health.last_beat_at is not None, f"Beat {i}: no beat recorded"
        # Clock hasn't advanced — age_s should be very small (< 2 s is generous).
        assert health.age_s < 2.0, f"Beat {i}: age_s={health.age_s} unexpectedly large"

    await client.close()


@pytest.mark.asyncio
async def test_stale_after_client_stops(
    server: HeartbeatServer,
    short_sock_dir: Path,
    clock: ManualClock,
) -> None:
    """After the client stops beating, age_s grows when the clock advances."""
    sock_path = short_sock_dir / "test-bot.sock"
    interval_s = 30.0

    client = HeartbeatClient(sock_path=sock_path, bot_id="test-bot", clock=clock)
    await client.beat()
    await asyncio.sleep(0.05)

    # Confirm the server saw the beat.
    health = server.health("test-bot")
    assert health.last_beat_at is not None

    # Close the client — no more beats.
    await client.close()

    # Advance the ManualClock by 2 * interval.
    clock.advance(2 * interval_s)

    health = server.health("test-bot")
    assert health.age_s >= 2 * interval_s, f"Expected age_s >= {2 * interval_s}, got {health.age_s}"


@pytest.mark.asyncio
async def test_no_beats_age_from_server_start(
    server: HeartbeatServer,
    clock: ManualClock,
) -> None:
    """If no beats arrive, age_s is measured from server start time."""
    clock.advance(10.0)
    health = server.health("test-bot")
    assert health.last_beat_at is None
    assert health.age_s >= 10.0
