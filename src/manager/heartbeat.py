"""Unix-socket heartbeat server and client for Phase 5.

The manager process runs a :class:`HeartbeatServer` that listens on a UNIX
domain socket per bot.  Each bot subprocess runs a :class:`HeartbeatClient`
that connects and sends one JSONL line per tick.  The manager's watchdog loop
calls :meth:`HeartbeatServer.health` to detect silent crashes.

Wire format (one JSON object per line, UTF-8)::

    {"bot_id": "echo-paper-1", "ts": "2026-05-09T12:00:00+00:00",
     "snapshot_lag_s": 0.12, "last_error": null}

The server is the socket *server*; the bot is the socket *client*.
"""

from __future__ import annotations

__all__ = [
    "BotHealth",
    "HeartbeatClient",
    "HeartbeatServer",
    "SocketHeartbeat",
]

import asyncio
import contextlib
import json
import logging
import os
import stat
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from src.bots.base import Clock, Heartbeat

logger = logging.getLogger(__name__)

# Default heartbeat interval assumed by the server's health staleness check.
_DEFAULT_HEARTBEAT_INTERVAL_S: float = 30.0


# ---------------------------------------------------------------------------
# BotHealth — reported by the server to the watchdog
# ---------------------------------------------------------------------------


@dataclass
class BotHealth:
    """Latest health record for one bot as seen by the server.

    Attributes:
        bot_id: Stable bot identifier.
        last_beat_at: UTC datetime of the most recent beat, or ``None`` if the
            bot has not sent a beat yet.
        age_s: Seconds since the last beat (or since server start if none).
        last_error: Last error string reported by the bot, or ``None``.
        snapshot_lag_s: Seconds between the last snapshot and the last beat.
    """

    bot_id: str
    last_beat_at: datetime | None
    age_s: float
    last_error: str | None
    snapshot_lag_s: float


# ---------------------------------------------------------------------------
# HeartbeatServer — manager side
# ---------------------------------------------------------------------------


class HeartbeatServer:
    """Async UNIX-socket server that tracks heartbeats from bot subprocesses.

    One socket file is created per bot.  The server is the socket *server*;
    the bot is the socket *client* that connects and writes JSONL beats.

    Args:
        sock_dir: Directory where ``<bot_id>.sock`` files are created.
            Must exist; the server sets 0700 permissions on the directory.
        clock: Injectable clock for testing.
    """

    def __init__(self, sock_dir: Path, clock: Clock | None = None) -> None:
        self._sock_dir = sock_dir
        self._clock = clock or Clock()
        # bot_id -> (last_beat_at, last_error, snapshot_lag_s)
        self._beats: dict[str, tuple[datetime, str | None, float]] = {}
        self._servers: dict[str, asyncio.AbstractServer] = {}
        self._started_at: datetime = self._clock.now()

    async def start(self, bot_ids: list[str]) -> None:
        """Bind a UNIX socket for each bot id and begin accepting connections.

        Args:
            bot_ids: List of bot ids to create sockets for.
        """
        # Ensure the directory exists with restricted permissions.
        self._sock_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self._sock_dir, stat.S_IRWXU)

        for bot_id in bot_ids:
            await self._bind(bot_id)

    async def _bind(self, bot_id: str) -> None:
        """Bind a socket for a single bot id.

        Args:
            bot_id: Bot identifier; socket path is ``sock_dir/<bot_id>.sock``.
        """
        sock_path = self._sock_path(bot_id)
        # Remove stale socket from a previous run.
        if sock_path.exists():
            sock_path.unlink()

        _bot_id = bot_id  # capture for closure

        async def _client_connected(
            r: asyncio.StreamReader,
            w: asyncio.StreamWriter,
        ) -> None:
            await self._handle(_bot_id, r, w)

        server = await asyncio.start_unix_server(
            _client_connected,
            path=str(sock_path),
        )
        self._servers[bot_id] = server
        logger.debug("Heartbeat server listening for bot %s at %s", bot_id, sock_path)

    async def _handle(
        self,
        bot_id: str,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """Handle one connection from a bot client.

        Reads JSONL lines until EOF.  Each valid line updates the beat record
        for ``bot_id``.

        Args:
            bot_id: Bot this connection belongs to.
            reader: Async stream reader.
            writer: Async stream writer (closed on exit).
        """
        logger.debug("Heartbeat connection opened for bot %s", bot_id)
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break  # EOF — client disconnected
                try:
                    msg = json.loads(line.decode("utf-8").strip())
                    ts_str = msg.get("ts", "")
                    beat_at = datetime.fromisoformat(ts_str)
                    snap_lag = float(msg.get("snapshot_lag_s", 0.0))
                    last_error = msg.get("last_error")
                    self._beats[bot_id] = (beat_at, last_error, snap_lag)
                    logger.debug(
                        "Heartbeat received: bot=%s ts=%s snap_lag=%.2fs",
                        bot_id,
                        ts_str,
                        snap_lag,
                    )
                except (json.JSONDecodeError, ValueError, KeyError) as exc:
                    logger.warning("Malformed heartbeat from %s: %s", bot_id, exc)
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
            logger.debug("Heartbeat connection closed for bot %s", bot_id)

    def health(self, bot_id: str) -> BotHealth:
        """Return the current health record for ``bot_id``.

        Args:
            bot_id: The bot to query.

        Returns:
            :class:`BotHealth` with ``age_s`` measured from the last beat.
            If no beat has arrived yet, ``age_s`` is measured from server start.
        """
        now = self._clock.now()
        if bot_id in self._beats:
            last_beat_at, last_error, snap_lag = self._beats[bot_id]
            age_s = (now - last_beat_at).total_seconds()
        else:
            last_beat_at = None
            last_error = None
            snap_lag = 0.0
            age_s = (now - self._started_at).total_seconds()

        return BotHealth(
            bot_id=bot_id,
            last_beat_at=last_beat_at,
            age_s=age_s,
            last_error=last_error,
            snapshot_lag_s=snap_lag,
        )

    def add_bot(self, bot_id: str) -> None:
        """Register a new bot socket at runtime (used by Supervisor.reload).

        Schedules the socket binding as a background task.

        Args:
            bot_id: Bot id to add.
        """
        asyncio.get_event_loop().create_task(self._bind(bot_id))

    def remove_bot(self, bot_id: str) -> None:
        """Stop listening for a bot that has been removed from the roster.

        Args:
            bot_id: Bot id to remove.
        """
        server = self._servers.pop(bot_id, None)
        if server is not None:
            server.close()
        sock_path = self._sock_path(bot_id)
        if sock_path.exists():
            sock_path.unlink(missing_ok=True)
        self._beats.pop(bot_id, None)

    async def close(self) -> None:
        """Close all listening sockets and clean up socket files."""
        for bot_id, server in list(self._servers.items()):
            server.close()
            await server.wait_closed()
            sock_path = self._sock_path(bot_id)
            sock_path.unlink(missing_ok=True)
        self._servers.clear()
        self._beats.clear()

    def _sock_path(self, bot_id: str) -> Path:
        return self._sock_dir / f"{bot_id}.sock"


# ---------------------------------------------------------------------------
# HeartbeatClient — bot subprocess side
# ---------------------------------------------------------------------------


class HeartbeatClient:
    """UNIX-socket client that sends JSONL heartbeats to :class:`HeartbeatServer`.

    Used by the bot subprocess.  Opened lazily on the first :meth:`beat` call
    so the runner module can construct it before the event loop is running.

    Args:
        sock_path: Path to the ``<bot_id>.sock`` file created by the server.
        bot_id: Bot identifier included in every beat message.
        clock: Injectable clock for deterministic tests.
    """

    def __init__(self, sock_path: Path, bot_id: str, clock: Clock | None = None) -> None:
        self._sock_path = sock_path
        self._bot_id = bot_id
        self._clock = clock or Clock()
        self._writer: asyncio.StreamWriter | None = None
        self._last_snapshot_at: datetime | None = None
        self._last_error: str | None = None

    async def _connect(self) -> bool:
        """Attempt to open the UNIX socket connection.

        Returns:
            ``True`` on success; ``False`` if the server is not yet listening.
        """
        try:
            _reader, writer = await asyncio.open_unix_connection(str(self._sock_path))
            self._writer = writer
            logger.debug("HeartbeatClient connected to %s", self._sock_path)
            return True
        except (OSError, ConnectionRefusedError) as exc:
            logger.warning("HeartbeatClient could not connect to %s: %s", self._sock_path, exc)
            return False

    async def beat(self) -> None:
        """Send one heartbeat line to the server.

        Reconnects automatically if the connection was lost.  Connection
        failures are logged as warnings and not propagated — heartbeat
        failures must never abort a tick (invariant 6 applies here too).
        """
        if self._writer is None or self._writer.is_closing():
            ok = await self._connect()
            if not ok:
                return

        now = self._clock.now()
        snap_lag_s = (
            (now - self._last_snapshot_at).total_seconds()
            if self._last_snapshot_at is not None
            else 0.0
        )
        msg = {
            "bot_id": self._bot_id,
            "ts": now.isoformat(),
            "snapshot_lag_s": snap_lag_s,
            "last_error": self._last_error,
        }
        try:
            assert self._writer is not None
            self._writer.write((json.dumps(msg) + "\n").encode("utf-8"))
            await self._writer.drain()
        except (OSError, ConnectionResetError) as exc:
            logger.warning("HeartbeatClient write failed for %s: %s", self._bot_id, exc)
            self._writer = None  # force reconnect next tick

    def record_snapshot(self) -> None:
        """Record the current time as the last snapshot time.

        Called by the runner after each successful :meth:`~src.bots.base.BaseBot._persist_snapshot`.
        """
        self._last_snapshot_at = self._clock.now()

    def record_error(self, error: str | None) -> None:
        """Record the last error string to include in the next beat.

        Args:
            error: Error message or ``None`` to clear.
        """
        self._last_error = error

    async def close(self) -> None:
        """Close the client connection."""
        if self._writer is not None and not self._writer.is_closing():
            self._writer.close()
            with contextlib.suppress(Exception):
                await self._writer.wait_closed()
        self._writer = None


# ---------------------------------------------------------------------------
# SocketHeartbeat — Heartbeat ABC wrapper for BotDeps
# ---------------------------------------------------------------------------


class SocketHeartbeat(Heartbeat):
    """Adapts :class:`HeartbeatClient` to the :class:`~src.bots.base.Heartbeat` ABC.

    Phase 5 injects this into :class:`~src.bots.base.BotDeps` so the bot
    runner uses the real socket heartbeat while tests stay on
    :class:`~src.bots.base.LocalHeartbeat`.

    Args:
        client: The underlying :class:`HeartbeatClient` to delegate to.
    """

    def __init__(self, client: HeartbeatClient) -> None:
        self._client = client

    async def beat(self) -> None:
        """Delegate to the underlying client's :meth:`HeartbeatClient.beat`."""
        await self._client.beat()

    @property
    def client(self) -> HeartbeatClient:
        """Expose the underlying client for snapshot/error recording."""
        return self._client
